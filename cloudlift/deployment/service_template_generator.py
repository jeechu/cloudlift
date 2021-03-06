import json
import re

from awacs.aws import PolicyDocument, Statement, Allow, Principal
from awacs.ecr import GetAuthorizationToken, BatchCheckLayerAvailability, GetDownloadUrlForLayer, BatchGetImage
from awacs.logs import CreateLogStream, PutLogEvents
from awacs.secretsmanager import GetSecretValue
from awacs.sts import AssumeRole
from awacs.ssm import GetParameters
from cfn_flip import to_yaml
from stringcase import pascalcase
from troposphere import GetAtt, Output, Parameter, Ref, Sub, Split, Select
from troposphere.applicationautoscaling import ScalableTarget
from troposphere.applicationautoscaling import ScalingPolicy, TargetTrackingScalingPolicyConfiguration, \
    PredefinedMetricSpecification
from troposphere.cloudwatch import Alarm, MetricDimension
from troposphere.ec2 import SecurityGroup
from troposphere.ecs import (AwsvpcConfiguration, ContainerDefinition,
                             DeploymentConfiguration, Environment, Secret,
                             LoadBalancer, LogConfiguration,
                             NetworkConfiguration, PlacementStrategy,
                             PortMapping, Service, TaskDefinition, PlacementConstraint, SystemControl,
                             HealthCheck)
from troposphere.elasticloadbalancingv2 import (Action, Certificate, Listener, ListenerRule, Condition,
                                                HostHeaderConfig, PathPatternConfig)
from troposphere.elasticloadbalancingv2 import LoadBalancer as ALBLoadBalancer
from troposphere.elasticloadbalancingv2 import LoadBalancer as NLBLoadBalancer
from troposphere.elasticloadbalancingv2 import (Matcher, RedirectConfig,
                                                TargetGroup,
                                                TargetGroupAttribute)
from troposphere.elasticloadbalancingv2 import SubnetMapping
from troposphere.iam import Role, Policy
from troposphere import Tags

from cloudlift.config import DecimalEncoder
from cloudlift.config import get_account_id
from cloudlift.config.region import get_environment_level_alb_listener, get_client_for
from cloudlift.config.service_configuration import DEFAULT_TARGET_GROUP_DEREGISTRATION_DELAY, \
    DEFAULT_LOAD_BALANCING_ALGORITHM, DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS, DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS, \
    DEFAULT_HEALTH_CHECK_HEALTHY_THRESHOLD_COUNT, DEFAULT_HEALTH_CHECK_UNHEALTHY_THRESHOLD_COUNT
from cloudlift.deployment.deployer import build_config, get_automated_injected_secret_name
from cloudlift.deployment.template_generator import TemplateGenerator
from cloudlift.exceptions import UnrecoverableException
from cloudlift.deployment.task_definition_builder import TaskDefinitionBuilder, container_name
from cloudlift.deployment.launch_types import LAUNCH_TYPE_FARGATE, LAUNCH_TYPE_EC2, get_launch_type


class ServiceTemplateGenerator(TemplateGenerator):
    PLACEMENT_STRATEGIES = [
        PlacementStrategy(
            Type='spread',
            Field='attribute:ecs.availability-zone'
        ),
        PlacementStrategy(
            Type='spread',
            Field='instanceId'
        )]

    def __init__(self, service_configuration, environment_stack, env_sample_file, ecr_image_uri, desired_counts=None,deployment_identifier=None):
        super(ServiceTemplateGenerator, self).__init__(service_configuration.environment)
        self._derive_configuration(service_configuration)
        self.env_sample_file_path = env_sample_file
        self.environment_stack = environment_stack
        self.ecr_image_uri = ecr_image_uri
        self.desired_counts = desired_counts or {}
        self.deployment_identifier = deployment_identifier

    def _derive_configuration(self, service_configuration):
        self.application_name = service_configuration.service_name
        self.configuration = service_configuration.get_config()

    def generate_service(self):
        self._add_service_parameters()
        self._add_service_outputs()
        self.ecs_service_role = self._add_ecs_service_iam_role()
        self._add_cluster_services()
        self._add_ecr_outputs()
        return to_yaml(self.template.to_json())

    def _add_cluster_services(self):
        for ecs_service_name, config in self.configuration['services'].items():
            self._add_service(ecs_service_name, config)

    def _add_service_alarms(self, svc):
        cloudlift_timedout_deployments_alarm = Alarm(
            'FailedCloudliftDeployments' + str(svc.name),
            EvaluationPeriods=1,
            Dimensions=[
                MetricDimension(
                    Name='ClusterName',
                    Value=self.cluster_name
                ),
                MetricDimension(
                    Name='ServiceName',
                    Value=GetAtt(svc, 'Name')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Cloudlift deployment timed out',
            Namespace='ECS/DeploymentMetrics',
            Period=60,
            ComparisonOperator='GreaterThanThreshold',
            Statistic='Average',
            Threshold='0',
            MetricName='FailedCloudliftDeployments',
            TreatMissingData='notBreaching'
        )
        self.template.add_resource(cloudlift_timedout_deployments_alarm)
        # How to add service task count alarm
        # http://docs.aws.amazon.com/AmazonECS/latest/developerguide/cloudwatch-metrics.html#cw_running_task_count
        ecs_no_running_tasks_alarm = Alarm(
            'EcsNoRunningTasksAlarm' + str(svc.name),
            EvaluationPeriods=1,
            Dimensions=[
                MetricDimension(
                    Name='ClusterName',
                    Value=self.cluster_name
                ),
                MetricDimension(
                    Name='ServiceName',
                    Value=GetAtt(svc, 'Name')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Alarm if the task count goes to zero, denoting \
service is down',
            Namespace='AWS/ECS',
            Period=60,
            ComparisonOperator='LessThanThreshold',
            Statistic='SampleCount',
            Threshold='1',
            MetricName='CPUUtilization',
            TreatMissingData='breaching'
        )
        self.template.add_resource(ecs_no_running_tasks_alarm)

    def _add_scalable_target(self, ecs_svc, config):
        resource_id = Sub('service/' + self.cluster_name + '/' + '${service_name}',
                          service_name=GetAtt(ecs_svc, "Name"))
        scalable_target = ScalableTarget(
            str(ecs_svc.name) + "ScalableTarget",
            MinCapacity=int(config.get('min_capacity')),
            MaxCapacity=int(config.get('max_capacity')),
            ResourceId=resource_id,
            RoleARN=f'arn:aws:iam::{self.account_id}:role/aws-service-role/ecs.application-autoscaling.amazonaws.com/AWSServiceRoleForApplicationAutoScaling_ECSService',
            ScalableDimension='ecs:service:DesiredCount',
            ServiceNamespace='ecs'
        )
        self.template.add_resource(scalable_target)
        return scalable_target

    def _add_scalable_target_alarms(self, service_name, ecs_svc, config):
        max_scalable_target_alarm = Alarm(
            'MaxScalableTargetAlarm' + service_name,
            EvaluationPeriods=3,
            DatapointsToAlarm=3,
            Dimensions=[
                MetricDimension(
                    Name='ServiceName',
                    Value=GetAtt(ecs_svc, 'Name')
                ),
                MetricDimension(
                    Name='ClusterName',
                    Value=self.cluster_name
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Triggers if desired task count of a service is equal to max_capacity,' +
                             ' review auto scaling configuration if this alarm triggers',
            Namespace='ECS/ContainerInsights',
            Period=300,
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            Statistic='Maximum',
            Threshold=int(config.get('max_capacity')),
            MetricName='DesiredTaskCount',
            TreatMissingData='notBreaching'
        )
        self.template.add_resource(max_scalable_target_alarm)

    def _add_alb_request_count_scaling_policy(self, ecs_svc, alb_arn, target_group, config, scalable_target):
        try:
            target_value = int(config.get('target_value'))
            scale_in_cool_down = int(config.get('scale_in_cool_down_seconds'))
            scale_out_cool_down = int(config.get('scale_out_cool_down_seconds'))
        except TypeError as e:
            raise UnrecoverableException('The following value has to be integer: {}'.format(e))

        if type(alb_arn) == str:
            alb_name = alb_arn.split('/')[2]
            alb_id = alb_arn.split('/')[3]
        else:
            alb_name = Select(2, Split('/', Ref(alb_arn)))
            alb_id = Select(3, Split('/', Ref(alb_arn)))

        tg_name = Select(1, Split('/', Ref(target_group)))
        tg_id = Select(2, Split('/', Ref(target_group)))
        self.template.add_resource(
            ScalingPolicy(
                str(ecs_svc.name) + 'ALBRequestCountPerTargetScalingPolicy',
                PolicyName='requestCountPerTarget',
                PolicyType='TargetTrackingScaling',
                TargetTrackingScalingPolicyConfiguration=TargetTrackingScalingPolicyConfiguration(
                    ScaleInCooldown=scale_in_cool_down,
                    ScaleOutCooldown=scale_out_cool_down,
                    TargetValue=target_value,
                    PredefinedMetricSpecification=PredefinedMetricSpecification(
                        PredefinedMetricType='ALBRequestCountPerTarget',
                        ResourceLabel=Sub("app/${alb_name}/${alb_id}/targetgroup/${tg_name}/${tg_id}", alb_id=alb_id,
                                          alb_name=alb_name, tg_name=tg_name, tg_id=tg_id)
                    )
                ),
                ScalingTargetId=Ref(scalable_target)
            )
        )

    def _add_service(self, service_name, config):
        launch_type = get_launch_type(config)
        secrets_name = config.get('secrets_name')
        container_configurations = build_config(self.env, self.application_name, service_name,
                                                self.env_sample_file_path,
                                                container_name(service_name), secrets_name)
        if secrets_name:
            self.template.add_output(Output(service_name + "SecretsName",
                                            Description="AWS secrets manager name to pull the secrets from",
                                            Value=secrets_name))

        task_role = self._add_task_role(service_name, config.get('task_role_attached_managed_policy_arns', []))
        task_execution_role = self._add_task_execution_role(service_name, secrets_name)

        builder = TaskDefinitionBuilder(
            environment=self.env,
            service_name=service_name,
            configuration=config,
            region=self.region,
            application_name=self.application_name
        )

        td = builder.build_cloudformation_resource(
            container_configurations=container_configurations,
            ecr_image_uri=self.ecr_image_uri,
            fallback_task_role=Ref(task_role),
            fallback_task_execution_role=Ref(task_execution_role),
            deployment_identifier=self.deployment_identifier
        )

        self.template.add_resource(td)
        maximum_percent = config['deployment'].get('maximum_percent', 200) if 'deployment' in config else 200
        deployment_configuration = DeploymentConfiguration(MinimumHealthyPercent=100,
                                                           MaximumPercent=int(maximum_percent))
        autoscaling_config = config['autoscaling'] if 'autoscaling' in config else {}
        desired_count = self._get_desired_task_count_for_service(service_name,
                                                                 min_count=int(
                                                                     autoscaling_config.get('min_capacity', 0)))
        if 'udp_interface' in config:
            lb, target_group_name = self._add_ecs_nlb(service_name, config['udp_interface'], launch_type)
            nlb_enabled = 'nlb_enabled' in config['udp_interface'] and config['udp_interface']['nlb_enabled']
            launch_type_svc = {}

            if nlb_enabled:
                elb, service_listener, nlb_sg = self._add_nlb(service_name, config, target_group_name)
                launch_type_svc['DependsOn'] = service_listener.title
                launch_type_svc['NetworkConfiguration'] = NetworkConfiguration(
                    AwsvpcConfiguration=AwsvpcConfiguration(
                        Subnets=[Ref(self.private_subnet1), Ref(self.private_subnet2)],
                        SecurityGroups=[Ref(nlb_sg)])
                )
                self.template.add_output(
                    Output(
                        service_name + "URL",
                        Description="The URL at which the service is accessible",
                        Value=Sub("udp://${" + elb.name + ".DNSName}")
                    )
                )

            if launch_type == LAUNCH_TYPE_EC2:
                launch_type_svc['PlacementStrategies'] = self.PLACEMENT_STRATEGIES
            svc = Service(
                service_name,
                LoadBalancers=[lb],
                Cluster=self.cluster_name,
                TaskDefinition=Ref(td),
                DesiredCount=desired_count,
                LaunchType=launch_type,
                Tags=Tags(environment=self.env, application=self.application_name, service=service_name),
                PropagateTags="TASK_DEFINITION",
                **launch_type_svc,
            )
            self.template.add_output(
                Output(
                    service_name + 'EcsServiceName',
                    Description='The ECS name which needs to be entered',
                    Value=GetAtt(svc, 'Name')
                )
            )
            self.template.add_resource(svc)
        elif 'http_interface' in config:
            lb, target_group_name, target_group = self._add_ecs_lb(service_name, config, launch_type)

            security_group_ingress = {
                'IpProtocol': 'TCP',
                'ToPort': int(config['http_interface']['container_port']),
                'FromPort': int(config['http_interface']['container_port']),
            }
            launch_type_svc = {}

            alb_enabled = 'alb' in config['http_interface']
            if alb_enabled:
                alb_config = config['http_interface']['alb']
                create_new_alb = alb_config.get('create_new', False)

                if create_new_alb:
                    alb, service_listener, alb_sg = self._add_alb(service_name, config, target_group_name)
                    launch_type_svc['DependsOn'] = service_listener.title

                    self.template.add_output(
                        Output(
                            service_name + "URL",
                            Description="The URL at which the service is accessible",
                            Value=Sub("https://${" + alb.name + ".DNSName}")
                        )
                    )
                    if launch_type == LAUNCH_TYPE_FARGATE:
                        # needed for FARGATE security group creation.
                        security_group_ingress['SourceSecurityGroupId'] = Ref(alb_sg)
                else:
                    listener_arn = alb_config['listener_arn'] if 'listener_arn' in alb_config \
                        else get_environment_level_alb_listener(self.env)
                    self.attach_to_existing_listener(alb_config, service_name, target_group_name, listener_arn)
                    alb_full_name = self.get_alb_full_name_from_listener_arn(listener_arn)
                    self.create_target_group_alarms(target_group_name, target_group, alb_full_name,
                                                    alb_config)
            if launch_type == LAUNCH_TYPE_FARGATE:
                # if launch type is ec2, then services inherit the ec2 instance security group
                # otherwise, we need to specify a security group for the service
                service_security_group = SecurityGroup(
                    pascalcase("FargateService" + self.env + service_name),
                    GroupName=pascalcase("FargateService" + self.env + service_name),
                    SecurityGroupIngress=[security_group_ingress],
                    VpcId=Ref(self.vpc),
                    GroupDescription=pascalcase("FargateService" + self.env + service_name)
                )
                self.template.add_resource(service_security_group)

                launch_type_svc['NetworkConfiguration'] = NetworkConfiguration(
                    AwsvpcConfiguration=AwsvpcConfiguration(
                        Subnets=[
                            Ref(self.private_subnet1),
                            Ref(self.private_subnet2)
                        ],
                        SecurityGroups=[
                            Ref(service_security_group)
                        ]
                    )
                )
            else:
                launch_type_svc['Role'] = self.ecs_service_role
                launch_type_svc['PlacementStrategies'] = self.PLACEMENT_STRATEGIES

            svc = Service(
                service_name,
                LoadBalancers=[lb],
                Cluster=self.cluster_name,
                TaskDefinition=Ref(td),
                DesiredCount=desired_count,
                DeploymentConfiguration=deployment_configuration,
                LaunchType=launch_type,
                Tags=Tags(environment=self.env, application=self.application_name, service=service_name),
                PropagateTags="TASK_DEFINITION",
                **launch_type_svc,
            )
            if autoscaling_config:
                scalable_target = self._add_scalable_target(svc, autoscaling_config)
                self._add_scalable_target_alarms(service_name, svc, autoscaling_config)

                if 'alb_arn' in autoscaling_config['request_count_per_target']:
                    alb_arn = autoscaling_config['request_count_per_target']['alb_arn']
                elif 'http_interface' in config and alb_enabled and create_new_alb:
                    alb_arn = alb
                else:
                    raise UnrecoverableException('Unable to fetch alb arn, please provide alb_arn in config')
                self._add_alb_request_count_scaling_policy(
                    svc,
                    alb_arn,
                    target_group,
                    autoscaling_config['request_count_per_target'],
                    scalable_target
                )

            self.template.add_output(
                Output(
                    service_name + 'EcsServiceName',
                    Description='The ECS name which needs to be entered',
                    Value=GetAtt(svc, 'Name')
                )
            )

            self.template.add_resource(svc)
        elif 'tcp_interface' in config:
            launch_type_svc = {}
            launch_type_svc['NetworkConfiguration'] = NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=[Ref(self.private_subnet1), Ref(self.private_subnet2)],
                    SecurityGroups=[config['tcp_interface']['target_security_group']])
            )
            svc = Service(
                service_name,
                LoadBalancers=[
                    LoadBalancer(ContainerName=container_name(service_name),
                                 TargetGroupArn=config['tcp_interface']['target_group_arn'],
                                 ContainerPort=int(config['tcp_interface']['container_port']))],
                Cluster=self.cluster_name,
                TaskDefinition=Ref(td),
                DesiredCount=desired_count,
                LaunchType=launch_type,
                PlacementStrategies=self.PLACEMENT_STRATEGIES,
                Tags=Tags(environment=self.env, application=self.application_name, service=service_name),
                PropagateTags="TASK_DEFINITION",
                **launch_type_svc
            )
            self.template.add_output(
                Output(
                    service_name + 'EcsServiceName',
                    Description='The ECS name which needs to be entered',
                    Value=GetAtt(svc, 'Name')
                )
            )
            self.template.add_resource(svc)
        else:
            launch_type_svc = {}
            if launch_type == LAUNCH_TYPE_FARGATE:
                # if launch type is ec2, then services inherit the ec2 instance security group
                # otherwise, we need to specify a security group for the service
                service_security_group = SecurityGroup(
                    pascalcase("FargateService" + self.env + service_name),
                    GroupName=pascalcase("FargateService" + self.env + service_name),
                    SecurityGroupIngress=[],
                    VpcId=Ref(self.vpc),
                    GroupDescription=pascalcase("FargateService" + self.env + service_name)
                )
                self.template.add_resource(service_security_group)
                launch_type_svc = {
                    'NetworkConfiguration': NetworkConfiguration(
                        AwsvpcConfiguration=AwsvpcConfiguration(
                            Subnets=[
                                Ref(self.private_subnet1),
                                Ref(self.private_subnet2)
                            ],
                            SecurityGroups=[
                                Ref(service_security_group)
                            ]
                        )
                    )
                }
            else:
                launch_type_svc = {
                    'PlacementStrategies': self.PLACEMENT_STRATEGIES
                }
            svc = Service(
                service_name,
                Cluster=self.cluster_name,
                TaskDefinition=Ref(td),
                DesiredCount=desired_count,
                DeploymentConfiguration=deployment_configuration,
                LaunchType=launch_type,
                Tags=Tags(environment=self.env, application=self.application_name, service=service_name),
                PropagateTags="TASK_DEFINITION",
                **launch_type_svc
            )
            self.template.add_output(
                Output(
                    service_name + 'EcsServiceName',
                    Description='The ECS name which needs to be entered',
                    Value=GetAtt(svc, 'Name')
                )
            )
            self.template.add_resource(svc)
        self._add_service_alarms(svc)

    def get_alb_full_name_from_listener_arn(self, listener_arn):
        return "/".join(listener_arn.split('/')[1:-1])

    def attach_to_existing_listener(self, alb_config, service_name, target_group_name, listener_arn):
        conditions = []
        if 'host' in alb_config:
            conditions.append(
                Condition(
                    Field="host-header",
                    HostHeaderConfig=HostHeaderConfig(
                        Values=[alb_config['host']],
                    ),
                )
            )
        if 'path' in alb_config:
            conditions.append(
                Condition(
                    Field="path-pattern",
                    PathPatternConfig=PathPatternConfig(
                        Values=[alb_config['path']],
                    ),
                )
            )

        priority = alb_config['priority']
        self.template.add_resource(
            ListenerRule(
                service_name + "ListenerRule",
                ListenerArn=listener_arn,
                Priority=int(priority),
                Conditions=conditions,
                Actions=[Action(
                    Type="forward",
                    TargetGroupArn=Ref(target_group_name),
                )]
            )
        )

    def _add_task_role(self, service_name, managed_policy_arns):
        role = Role(
            service_name + "Role",
            ManagedPolicyArns=managed_policy_arns,
            AssumeRolePolicyDocument=PolicyDocument(
                Statement=[
                    Statement(Effect=Allow,
                              Action=[AssumeRole],
                              Principal=Principal("Service", ["ecs-tasks.amazonaws.com"]))
                ]
            )
        )
        self.template.add_resource(role)
        return role

    def _add_task_execution_role(self, service_name, secrets_name):
        automated_injected_secret_name = get_automated_injected_secret_name(self.env,
                                                                            self.application_name,
                                                                            service_name)
        # https://docs.aws.amazon.com/code-samples/latest/catalog/iam_policies-secretsmanager-asm-user-policy-grants-access-to-secret-by-name-with-wildcard.json.html
        allow_secrets = [Statement(Effect=Allow, Action=[GetSecretValue], Resource=[
            f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:{automated_injected_secret_name}-??????",
            f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:{secrets_name}-??????",
        ])] \
            if secrets_name else []

        task_execution_role = self.template.add_resource(Role(
            service_name + "TaskExecutionRole",
            AssumeRolePolicyDocument=PolicyDocument(
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal("Service", ["ecs-tasks.amazonaws.com"])
                    )
                ]
            ),
            Policies=[
                Policy(PolicyName=service_name + "TaskExecutionRolePolicy",
                       PolicyDocument=PolicyDocument(
                           Statement=[
                               *allow_secrets,
                               Statement(Effect=Allow,
                                         Action=[GetAuthorizationToken, BatchCheckLayerAvailability,
                                                 GetDownloadUrlForLayer, BatchGetImage, CreateLogStream, PutLogEvents],
                                         Resource=["*"]),
                               Statement(Effect=Allow,
                                         Action=[GetParameters],
                                         Resource=[f"arn:aws:ssm:{self.region}:{self.account_id}:parameter/{self.env}/{self.application_name}/*"])
                           ]
                       ))]
        ))
        return task_execution_role

    def _add_ecs_lb(self, service_name, config, launch_type):
        target_group_name = "TargetGroup" + service_name
        health_check_path = config['http_interface']['health_check_path'] if 'health_check_path' in config[
            'http_interface'] else "/elb-check"
        if config['http_interface']['internal']:
            target_group_name = target_group_name + 'Internal'

        target_group_config = {}
        if launch_type == LAUNCH_TYPE_FARGATE:
            target_group_config['TargetType'] = 'ip'

        service_target_group = TargetGroup(
            target_group_name,
            HealthCheckPath=health_check_path,
            HealthyThresholdCount=int(config['http_interface'].get('health_check_healthy_threshold_count',
                                                                   DEFAULT_HEALTH_CHECK_HEALTHY_THRESHOLD_COUNT)),
            HealthCheckIntervalSeconds=int(config['http_interface'].get('health_check_interval_seconds',
                                                                        DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS)),
            HealthCheckTimeoutSeconds=int(config['http_interface'].get('health_check_timeout_seconds',
                                                                       DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS)),
            UnhealthyThresholdCount=int(config['http_interface'].get('health_check_unhealthy_threshold_count',
                                                                     DEFAULT_HEALTH_CHECK_UNHEALTHY_THRESHOLD_COUNT)),
            TargetGroupAttributes=[
                TargetGroupAttribute(
                    Key='deregistration_delay.timeout_seconds',
                    Value=str(config['http_interface'].get('deregistration_delay',
                                                           DEFAULT_TARGET_GROUP_DEREGISTRATION_DELAY))
                ),
                TargetGroupAttribute(
                    Key='load_balancing.algorithm.type',
                    Value=str(
                        config['http_interface'].get('load_balancing_algorithm', DEFAULT_LOAD_BALANCING_ALGORITHM))
                )
            ],
            VpcId=Ref(self.vpc),
            Protocol="HTTP",
            Matcher=Matcher(HttpCode="200-399"),
            Port=int(config['http_interface']['container_port']),
            Tags=Tags(environment=self.env, application=self.application_name, service=service_name),
            **target_group_config
        )

        self.template.add_resource(service_target_group)

        lb = LoadBalancer(
            ContainerName=container_name(service_name),
            TargetGroupArn=Ref(service_target_group),
            ContainerPort=int(config['http_interface']['container_port'])
        )

        return lb, target_group_name, service_target_group

    def _add_ecs_nlb(self, service_name, elb_config, launch_type):
        target_group_name = "TargetGroup" + service_name
        health_check_path = elb_config['health_check_path'] if 'health_check_path' in elb_config else "/elb-check"
        if elb_config['internal']:
            target_group_name = target_group_name + 'Internal'

        target_group_config = {'Port': int(elb_config['container_port']),
                               'HealthCheckPort': int(elb_config['health_check_port']), 'TargetType': 'ip'}
        service_target_group = TargetGroup(
            target_group_name,
            Protocol='UDP',
            # Health check healthy threshold and unhealthy
            # threshold must be the same for target groups with the UDP protocol
            HealthyThresholdCount=int(elb_config.get('health_check_healthy_threshold_count',
                                                     DEFAULT_HEALTH_CHECK_HEALTHY_THRESHOLD_COUNT)),
            HealthCheckIntervalSeconds=int(elb_config.get('health_check_interval_seconds',
                                                          DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS)),
            HealthCheckTimeoutSeconds=int(elb_config.get('health_check_timeout_seconds',
                                                         DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS)),
            UnhealthyThresholdCount=int(elb_config.get('health_check_healthy_threshold_count',
                                                       DEFAULT_HEALTH_CHECK_HEALTHY_THRESHOLD_COUNT)),
            TargetGroupAttributes=[
                TargetGroupAttribute(
                    Key='deregistration_delay.timeout_seconds',
                    Value=str(elb_config.get('deregistration_delay', DEFAULT_TARGET_GROUP_DEREGISTRATION_DELAY))
                )
            ],
            VpcId=Ref(self.vpc),
            Tags=[
                {'Key': 'service', 'Value': f"{self.application_name}-{service_name}"},
                {'Key': 'environment', 'Value': self.env},
                {'Key': 'Name', 'Value': "{self.env}-{self.application_name}-{service_name}-tg".format(**locals())}
            ]
            **target_group_config
        )

        self.template.add_resource(service_target_group)

        lb = LoadBalancer(
            ContainerName=container_name(service_name),
            TargetGroupArn=Ref(service_target_group),
            ContainerPort=int(elb_config['container_port'])
        )

        return lb, target_group_name

    def _add_alb(self, service_name, config, target_group_name):
        sg_name = 'SG' + self.env + service_name
        svc_alb_sg = SecurityGroup(
            re.sub(r'\W+', '', sg_name),
            GroupName=self.env + '-' + service_name,
            SecurityGroupIngress=self._generate_alb_security_group_ingress(config),
            VpcId=Ref(self.vpc),
            GroupDescription=Sub(service_name + "-alb-sg")
        )
        self.template.add_resource(svc_alb_sg)

        alb_name = service_name + pascalcase(self.env)
        if config['http_interface']['internal']:
            alb_subnets = [
                Ref(self.private_subnet1),
                Ref(self.private_subnet2)
            ]
            scheme = "internal"
            alb_name += 'Internal'
            alb_name = alb_name[:32]
            alb = ALBLoadBalancer(
                'ALB' + service_name,
                Subnets=alb_subnets,
                SecurityGroups=[
                    self.alb_security_group,
                    Ref(svc_alb_sg)
                ],
                Name=alb_name,
                Tags=[
                    {'Value': alb_name, 'Key': 'Name'}
                ],
                Scheme=scheme
            )
        else:
            alb_subnets = [
                Ref(self.public_subnet1),
                Ref(self.public_subnet2)
            ]
            alb_name = alb_name[:32]
            alb = ALBLoadBalancer(
                'ALB' + service_name,
                Subnets=alb_subnets,
                SecurityGroups=[
                    self.alb_security_group,
                    Ref(svc_alb_sg)
                ],
                Name=alb_name,
                Tags=[
                    {'Value': alb_name, 'Key': 'Name'}
                ]
            )

        self.template.add_resource(alb)

        target_group_action = Action(
            TargetGroupArn=Ref(target_group_name),
            Type="forward"
        )
        service_listener = self._add_service_listener(
            service_name,
            target_group_action,
            alb,
            config['http_interface']['internal']
        )
        return alb, service_listener, svc_alb_sg

    def _add_nlb(self, service_name, config, target_group_name):
        sg_name = 'SG' + self.env + service_name
        elb_config = config['udp_interface']
        svc_alb_sg = SecurityGroup(
            re.sub(r'\W+', '', sg_name),
            GroupName=self.env + '-' + service_name,
            SecurityGroupIngress=self._generate_nlb_security_group_ingress(elb_config),
            VpcId=Ref(self.vpc),
            GroupDescription=Sub(service_name + "-alb-sg")
        )
        self.template.add_resource(svc_alb_sg)

        nlb_name = service_name + pascalcase(self.env)
        if elb_config['internal']:
            alb_subnets = [
                Ref(self.private_subnet1),
                Ref(self.private_subnet2)
            ]
            scheme = "internal"
            nlb_name += 'Internal'
        else:
            scheme = 'internet-facing'
            alb_subnets = [
                Ref(self.public_subnet1),
                Ref(self.public_subnet2)
            ]
        subnet_info = {}
        subnet_mappings = []
        if 'eip_allocaltion_id1' in elb_config:
            subnet_mappings.append(
                SubnetMapping(SubnetId=alb_subnets[0], AllocationId=elb_config['eip_allocaltion_id1']))
        else:
            subnet_mappings.append(
                SubnetMapping(SubnetId=alb_subnets[0]))

        if 'eip_allocaltion_id2' in elb_config:
            subnet_mappings.append(
                SubnetMapping(SubnetId=alb_subnets[1], AllocationId=elb_config['eip_allocaltion_id2']))
        else:
            subnet_mappings.append(
                SubnetMapping(SubnetId=alb_subnets[1]))

        subnet_info['SubnetMappings'] = subnet_mappings

        nlb_name = nlb_name[:32]
        nlb = NLBLoadBalancer(
            'NLB' + service_name,
            SecurityGroups=[],
            Name=nlb_name,
            Tags=[
                {'Value': nlb_name, 'Key': 'Name'}
            ],
            Scheme=scheme,
            Type='network',
            **subnet_info
        )

        self.template.add_resource(nlb)

        target_group_action = Action(
            TargetGroupArn=Ref(target_group_name),
            Type="forward"
        )

        service_listener = Listener(
            "LoadBalancerListener" + service_name,
            Protocol="UDP",
            DefaultActions=[target_group_action],
            LoadBalancerArn=Ref(nlb),
            Port=int(config['udp_interface']['container_port']),
        )
        self.template.add_resource(service_listener)

        self._add_nlb_alarms(service_name, nlb)
        return nlb, service_listener, svc_alb_sg

    def _add_service_listener(self, service_name, target_group_action,
                              alb, internal):
        ssl_cert = Certificate(CertificateArn=self.ssl_certificate_arn)
        service_listener = Listener(
            "SslLoadBalancerListener" + service_name,
            Protocol="HTTPS",
            DefaultActions=[target_group_action],
            LoadBalancerArn=Ref(alb),
            Port=443,
            Certificates=[ssl_cert],
            SslPolicy="ELBSecurityPolicy-FS-1-2-Res-2019-08"
        )
        self.template.add_resource(service_listener)
        if internal:
            # Allow HTTP traffic on internal services
            http_service_listener = Listener(
                "LoadBalancerListener" + service_name,
                Protocol="HTTP",
                DefaultActions=[target_group_action],
                LoadBalancerArn=Ref(alb),
                Port=80
            )
            self.template.add_resource(http_service_listener)
        else:
            # Redirect HTTP to HTTPS on external services
            redirection_config = RedirectConfig(
                StatusCode='HTTP_301',
                Protocol='HTTPS',
                Port='443'
            )
            http_redirection_action = Action(
                RedirectConfig=redirection_config,
                Type="redirect"
            )
            http_redirection_listener = Listener(
                "LoadBalancerRedirectionListener" + service_name,
                Protocol="HTTP",
                DefaultActions=[http_redirection_action],
                LoadBalancerArn=Ref(alb),
                Port=80
            )
            self.template.add_resource(http_redirection_listener)
        return service_listener

    def _add_nlb_alarms(self, service_name, nlb):
        unhealthy_alarm = Alarm(
            'NlbUnhealthyHostAlarm' + service_name,
            EvaluationPeriods=1,
            Dimensions=[
                MetricDimension(
                    Name='LoadBalancer',
                    Value=GetAtt(nlb, 'LoadBalancerFullName')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Triggers if any host is marked unhealthy',
            Namespace='AWS/NetworkELB',
            Period=60,
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            Statistic='Sum',
            Threshold='1',
            MetricName='UnHealthyHostCount',
            TreatMissingData='notBreaching'
        )

    def create_target_group_alarms(self, target_group_name, target_group, alb_full_name, alb_config):
        unhealthy_alarm = Alarm(
            'TargetGroupUnhealthyHostAlarm' + target_group_name,
            EvaluationPeriods=1,
            Dimensions=[
                MetricDimension(
                    Name='LoadBalancer',
                    Value=alb_full_name
                ),
                MetricDimension(
                    Name='TargetGroup',
                    Value=GetAtt(target_group, 'TargetGroupFullName')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Triggers if any host is marked unhealthy',
            Namespace='AWS/ApplicationELB',
            Period=60,
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            Statistic='Sum',
            Threshold='1',
            MetricName='UnHealthyHostCount',
            TreatMissingData='notBreaching'
        )
        self.template.add_resource(unhealthy_alarm)

        high_5xx_alarm = Alarm(
            'HighTarget5XXAlarm' + target_group_name,
            EvaluationPeriods=1,
            Dimensions=[
                MetricDimension(
                    Name='LoadBalancer',
                    Value=alb_full_name
                ),
                MetricDimension(
                    Name='TargetGroup',
                    Value=GetAtt(target_group, 'TargetGroupFullName')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Triggers if target returns 5xx error code',
            Namespace='AWS/ApplicationELB',
            Period=60,
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            Statistic='Sum',
            Threshold=int(alb_config.get('target_5xx_error_threshold')),
            MetricName='HTTPCode_Target_5XX_Count',
            TreatMissingData='notBreaching'
        )
        self.template.add_resource(high_5xx_alarm)

        latency_alarms = self._get_latency_alarms(alb_config, alb_full_name,
                                                  target_group, target_group_name)

        for alarm in latency_alarms:
            self.template.add_resource(alarm)

    def _get_latency_alarms(self, alb_config, alb_full_name, target_group, target_group_name):
        high_p95_latency_alarm = Alarm(
            'HighP95LatencyAlarm' + target_group_name,
            EvaluationPeriods=int(alb_config.get('target_p95_latency_evaluation_periods', 5)),
            Dimensions=[
                MetricDimension(
                    Name='LoadBalancer',
                    Value=alb_full_name
                ),
                MetricDimension(
                    Name='TargetGroup',
                    Value=GetAtt(target_group, 'TargetGroupFullName')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Triggers if p95 latency of target group is higher than threshold',
            Namespace='AWS/ApplicationELB',
            Period=int(alb_config.get('target_p95_latency_period_seconds', 60)),
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            ExtendedStatistic='p95',
            Threshold=int(alb_config.get('target_p95_latency_threshold_seconds', 15)),
            MetricName='TargetResponseTime',
            TreatMissingData='notBreaching'
        )
        high_p99_latency_alarm = Alarm(
            'HighP99LatencyAlarm' + target_group_name,
            EvaluationPeriods=int(alb_config.get('target_p99_latency_evaluation_periods', 5)),
            Dimensions=[
                MetricDimension(
                    Name='LoadBalancer',
                    Value=alb_full_name
                ),
                MetricDimension(
                    Name='TargetGroup',
                    Value=GetAtt(target_group, 'TargetGroupFullName')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Triggers if p99 latency of target group is higher than threshold',
            Namespace='AWS/ApplicationELB',
            Period=int(alb_config.get('target_p99_latency_period_seconds', 60)),
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            ExtendedStatistic='p99',
            Threshold=int(alb_config.get('target_p99_latency_threshold_seconds', 25)),
            MetricName='TargetResponseTime',
            TreatMissingData='notBreaching'
        )
        return [high_p95_latency_alarm, high_p99_latency_alarm]

    def _add_alb_alarms(self, service_name, alb, alb_config):
        elb_5XX_error_codes_to_monitor = ["502", "503", "504"]
        for elb_5xx_error_code in elb_5XX_error_codes_to_monitor:
            threshold = alb_config.get(f'elb_{elb_5xx_error_code}_error_threshold', "5")
            self.template.add_resource(Alarm(
                f'ELB{elb_5xx_error_code}Count' + service_name,
                EvaluationPeriods=1,
                Dimensions=[
                    MetricDimension(
                        Name='LoadBalancer',
                        Value=GetAtt(alb, 'LoadBalancerFullName')
                    )
                ],
                AlarmActions=[Ref(self.notification_sns_arn)],
                OKActions=[Ref(self.notification_sns_arn)],
                AlarmDescription=f'Triggers if {elb_5xx_error_code} response originated from load balancer',
                Namespace='AWS/ApplicationELB',
                Period=60,
                ComparisonOperator='GreaterThanOrEqualToThreshold',
                Statistic='Sum',
                Threshold=threshold,
                MetricName=f'HTTPCode_ELB_{elb_5xx_error_code}_Count',
                TreatMissingData='notBreaching'
            ))

        rejected_connections_alarm = Alarm(
            'ElbRejectedConnectionsAlarm' + service_name,
            EvaluationPeriods=1,
            Dimensions=[
                MetricDimension(
                    Name='LoadBalancer',
                    Value=GetAtt(alb, 'LoadBalancerFullName')
                )
            ],
            AlarmActions=[Ref(self.notification_sns_arn)],
            OKActions=[Ref(self.notification_sns_arn)],
            AlarmDescription='Triggers if load balancer has \
rejected connections because the load balancer \
had reached its maximum number of connections.',
            Namespace='AWS/ApplicationELB',
            Period=60,
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            Statistic='Sum',
            Threshold='1',
            MetricName='RejectedConnectionCount',
            TreatMissingData='notBreaching'
        )
        self.template.add_resource(rejected_connections_alarm)

    def _generate_alb_security_group_ingress(self, config):
        ingress_rules = []
        for access_ip in config['http_interface']['restrict_access_to']:
            if access_ip.find('/') == -1:
                access_ip = access_ip + '/32'
            ingress_rules.append({
                'ToPort': 80,
                'IpProtocol': 'TCP',
                'FromPort': 80,
                'CidrIp': access_ip
            })
            ingress_rules.append({
                'ToPort': 443,
                'IpProtocol': 'TCP',
                'FromPort': 443,
                'CidrIp': access_ip
            })
        return ingress_rules

    def _generate_nlb_security_group_ingress(self, elb_config):
        ingress_rules = []
        for access_ip in elb_config['restrict_access_to']:
            if access_ip.find('/') == -1:
                access_ip = access_ip + '/32'
            port = elb_config['container_port']
            health_check_port = elb_config['health_check_port']
            ingress_rules.append({
                'ToPort': int(port),
                'IpProtocol': 'UDP',
                'FromPort': int(port),
                'CidrIp': access_ip
            })
            ingress_rules.append(
                {
                    'ToPort': int(health_check_port),
                    'IpProtocol': 'TCP',
                    'FromPort': int(health_check_port),
                    'CidrIp': access_ip
                })
        return ingress_rules

    def _add_ecs_service_iam_role(self):
        injected_service_role_arn = self.configuration.get('service_role_arn')
        role_name = Sub('ecs-svc-${AWS::StackName}-${AWS::Region}')
        assume_role_policy = {
            u'Statement': [
                {
                    u'Action': [u'sts:AssumeRole'],
                    u'Effect': u'Allow',
                    u'Principal': {
                        u'Service': [u'ecs.amazonaws.com']
                    }
                }
            ]
        }
        ecs_service_role = Role(
            'ECSServiceRole',
            Path='/',
            ManagedPolicyArns=[
                'arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceRole'
            ],
            RoleName=role_name,
            AssumeRolePolicyDocument=assume_role_policy
        )
        self.template.add_resource(ecs_service_role)
        if injected_service_role_arn:
            return injected_service_role_arn
        else:
            return Ref(ecs_service_role)

    def _add_service_outputs(self):
        self.template.add_output(Output(
            "CloudliftOptions",
            Description="Options used with cloudlift when \
building this service",
            Value=json.dumps(
                self.configuration,
                cls=DecimalEncoder
            )
        ))
        self._add_stack_outputs()

    def _add_ecr_outputs(self):
        ecr_repo_config = self.configuration.get('ecr_repo')
        self.template.add_output(
            Output(
                "ECRRepoName",
                Description="ECR repo to for docker images",
                Value=ecr_repo_config.get('name')
            )
        )

        if 'account_id' in ecr_repo_config:
            self.template.add_output(
                Output(
                    "ECRAccountID",
                    Description="Account ID to which the ECR repo belongs to",
                    Value=ecr_repo_config.get('account_id')
                )
            )
        if 'assume_role_arn' in ecr_repo_config:
            self.template.add_output(
                Output(
                    "ECRAssumeRoleARN",
                    Description="Role to assume to interact with ECR",
                    Value=ecr_repo_config.get('assume_role_arn')
                )
            )

    def _add_service_parameters(self):
        self.notification_sns_arn = Parameter(
            "NotificationSnsArn",
            Description='',
            Type="String",
            Default=self.notifications_arn)
        self.template.add_parameter(self.notification_sns_arn)
        self.vpc = Parameter(
            "VPC",
            Description='',
            Type="AWS::EC2::VPC::Id",
            Default=list(
                filter(
                    lambda x: x['OutputKey'] == "VPC",
                    self.environment_stack['Outputs']
                )
            )[0]['OutputValue']
        )
        self.template.add_parameter(self.vpc)
        self.public_subnet1 = Parameter(
            "PublicSubnet1",
            Description='',
            Type="AWS::EC2::Subnet::Id",
            Default=list(
                filter(
                    lambda x: x['OutputKey'] == "PublicSubnet1",
                    self.environment_stack['Outputs']
                )
            )[0]['OutputValue']
        )
        self.template.add_parameter(self.public_subnet1)
        self.public_subnet2 = Parameter(
            "PublicSubnet2",
            Description='',
            Type="AWS::EC2::Subnet::Id",
            Default=list(
                filter(
                    lambda x: x['OutputKey'] == "PublicSubnet2",
                    self.environment_stack['Outputs']
                )
            )[0]['OutputValue']
        )
        self.template.add_parameter(self.public_subnet2)
        self.private_subnet1 = Parameter(
            "PrivateSubnet1",
            Description='',
            Type="AWS::EC2::Subnet::Id",
            Default=list(
                filter(
                    lambda x: x['OutputKey'] == "PrivateSubnet1",
                    self.environment_stack['Outputs']
                )
            )[0]['OutputValue']
        )
        self.template.add_parameter(self.private_subnet1)
        self.private_subnet2 = Parameter(
            "PrivateSubnet2",
            Description='',
            Type="AWS::EC2::Subnet::Id",
            Default=list(
                filter(
                    lambda x: x['OutputKey'] == "PrivateSubnet2",
                    self.environment_stack['Outputs']
                )
            )[0]['OutputValue']
        )
        self.template.add_parameter(self.private_subnet2)
        self.template.add_parameter(Parameter(
            "Environment",
            Description='',
            Type="String",
            Default="production"
        ))
        self.alb_security_group = list(
            filter(
                lambda x: x['OutputKey'] == "SecurityGroupAlb",
                self.environment_stack['Outputs']
            )
        )[0]['OutputValue']

    def _get_desired_task_count_for_service(self, service_name, min_count=0):
        return max(self.desired_counts.get(service_name, 1), min_count)

    @property
    def account_id(self):
        return get_account_id()

    @property
    def repo_name(self):
        return self.application_name + '-repo'

    @property
    def notifications_arn(self):
        """
        Get the SNS arn either from service configuration or the cluster
        """
        if 'notifications_arn' in self.configuration:
            return self.configuration['notifications_arn']
        else:
            return TemplateGenerator.notifications_arn.fget(self)
