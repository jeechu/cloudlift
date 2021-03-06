"""
This is built from the package ecs_deploy.ecs
https://github.com/fabfuel/ecs-deploy/blob/develop/ecs_deploy/ecs.py

The package defaults to copying old environment config, applying
value changes onto it and merging new configs. This means, environment
variables cannot be deleted.

To update the package:
Copy the new contents, but ensure that apply_container_environment_and_secrets is retained
or updated. If the package supports deleting configs, use that.
"""

from datetime import datetime
from json import dumps

from boto3.session import Session
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.config import Config
from dateutil.tz.tz import tzlocal
from cloudlift.exceptions import UnrecoverableException


class EcsClient(object):
    def __init__(self, access_key_id=None, secret_access_key=None,
                 region=None, profile=None):
        session = Session(aws_access_key_id=access_key_id,
                          aws_secret_access_key=secret_access_key,
                          region_name=region,
                          profile_name=profile)
        config = Config(retries=dict(
            max_attempts=10,
            mode='standard',
        ))
        self.boto = session.client(u'ecs', config=config)

    def describe_services(self, cluster_name, service_name):
        return self.boto.describe_services(
            cluster=cluster_name,
            services=[service_name]
        )

    def list_task_definitions(self, family):
        response = self.boto.list_task_definitions(familyPrefix=family, status='ACTIVE', sort='DESC')
        return response.get('taskDefinitionArns', []), response.get('nextToken', None)

    def list_task_definitions_for_next_token(self, family, next_token):
        response = self.boto.list_task_definitions(familyPrefix=family, status='ACTIVE', sort='DESC',
                                                   nextToken=next_token)
        return response.get('taskDefinitionArns', []), response.get('nextToken', None)

    def describe_task_definition(self, task_definition_arn):
        try:
            return self.boto.describe_task_definition(
                taskDefinition=task_definition_arn,
                include=[
                    'TAGS',
                ]
            )
        except ClientError:
            raise UnknownTaskDefinitionError(
                u'Unknown task definition arn: %s' % task_definition_arn
            )

    def list_tasks(self, cluster_name, service_name):
        return self.boto.list_tasks(
            cluster=cluster_name,
            serviceName=service_name
        )

    def describe_tasks(self, cluster_name, task_arns):
        return self.boto.describe_tasks(cluster=cluster_name, tasks=task_arns)

    def register_task_definition(self, tags, **kwargs):
        return self.boto.register_task_definition(
            tags=tags,
            **kwargs
        )

    def deregister_task_definition(self, task_definition_arn):
        return self.boto.deregister_task_definition(
            taskDefinition=task_definition_arn
        )

    def update_service(self, cluster, service, desired_count, task_definition):
        return self.boto.update_service(
            cluster=cluster,
            service=service,
            desiredCount=desired_count,
            taskDefinition=task_definition
        )

    def run_task(self, cluster, task_definition, count, started_by, overrides):
        return self.boto.run_task(
            cluster=cluster,
            taskDefinition=task_definition,
            count=count,
            startedBy=started_by,
            overrides=overrides
        )


class EcsService(dict):
    def __init__(self, cluster, service_definition=None, **kwargs):
        self._cluster = cluster
        super(EcsService, self).__init__(service_definition, **kwargs)

    def set_desired_count(self, desired_count):
        self[u'desiredCount'] = desired_count

    def set_task_definition(self, task_definition):
        self[u'taskDefinition'] = task_definition.arn

    @property
    def cluster(self):
        return self._cluster

    @property
    def name(self):
        return self.get(u'serviceName')

    @property
    def task_definition(self):
        return self.get(u'taskDefinition')

    @property
    def desired_count(self):
        return self.get(u'desiredCount')

    @property
    def deployment_created_at(self):
        for deployment in self.get(u'deployments'):
            if deployment.get(u'status') == u'PRIMARY':
                return deployment.get(u'createdAt')
        return datetime.now()

    @property
    def deployment_updated_at(self):
        for deployment in self.get(u'deployments'):
            if deployment.get(u'status') == u'PRIMARY':
                return deployment.get(u'updatedAt')
        return datetime.now()

    @property
    def errors(self):
        return self.get_warnings(
            since=self.deployment_updated_at
        )

    @property
    def older_errors(self):
        return self.get_warnings(
            since=self.deployment_created_at,
            until=self.deployment_updated_at
        )

    def get_warnings(self, since=None, until=None):
        since = since or self.deployment_created_at
        until = until or datetime.now(tz=tzlocal())
        errors = {}
        for event in self.get(u'events'):
            if u'unable' not in event[u'message']:
                continue
            if since < event[u'createdAt'] < until:
                errors[event[u'createdAt']] = event[u'message']
        return errors


class EcsTaskDefinition(dict):
    def __init__(self, task_definition=None, **kwargs):
        super(EcsTaskDefinition, self).__init__(task_definition, **kwargs)
        self._diff = []

    @property
    def tags(self):
        return {tag['key']: tag['value'] for tag in self.get('tags', [])}

    @property
    def containers(self):
        return self.get(u'containerDefinitions')

    @property
    def container_names(self):
        for container in self.get(u'containerDefinitions'):
            yield container[u'name']

    @property
    def volumes(self):
        return self.get(u'volumes')

    @property
    def arn(self):
        return self.get(u'taskDefinitionArn')

    @property
    def requires_compatibilities(self):
        return self.get(u'requiresCompatibilities')

    @property
    def execution_role_arn(self):
        return self.get(u'executionRoleArn')

    @property
    def network_mode(self):
        return self.get(u'networkMode')

    @property
    def cpu(self):
        return self.get(u'cpu')

    @property
    def memory(self):
        return self.get(u'memory')

    @property
    def family(self):
        return self.get(u'family')

    @property
    def role_arn(self):
        return self.get(u'taskRoleArn')

    @property
    def revision(self):
        return self.get(u'revision')

    @property
    def family_revision(self):
        return '%s:%d' % (self.get(u'family'), self.get(u'revision'))

    @property
    def diff(self):
        return self._diff

    @property
    def placement_constraints(self):
        return self.get(u'placementConstraints')

    def get_overrides(self):
        override = dict()
        overrides = []
        for diff in self.diff:
            if override.get('name') != diff.container:
                override = dict(name=diff.container)
                overrides.append(override)
            if diff.field == 'command':
                override['command'] = self.get_overrides_command(diff.value)
            elif diff.field == 'environment':
                override['environment'] = self.get_overrides_env(diff.value)
        return overrides

    @staticmethod
    def get_overrides_command(command):
        return command.split(' ')

    @staticmethod
    def get_overrides_env(env):
        return [{"name": e, "value": env[e]} for e in env]

    def apply_memory_hard_limit(self, memory):
        for container in self.containers:
            container['memory'] = memory

    def compute_diffs(self, container_to_deploy, ecr_image_uri):
        for container in self.containers:
            container_name = container[u'name']
            if container_name != container_to_deploy:
                continue

            diff = EcsTaskDefinitionDiff(
                container=container_name,
                field=u'image',
                value=ecr_image_uri,
                old_value=container[u'image']
            )
            self._diff.append(diff)

    def apply_container_environment_and_secrets(self, container, new_environment_and_secrets):
        new_environment = new_environment_and_secrets.get('environment', {})
        old_environment = {env['name']: env['value'] for env in container.get('environment', {})}
        container[u'environment'] = [{"name": e, "value": new_environment[e]} for e in new_environment]
        self._diff.append(EcsTaskDefinitionDiff(container['name'], 'environment', new_environment, old_environment))

        new_secrets = new_environment_and_secrets.get('secrets', {})
        old_secrets = {env['name']: env['valueFrom'] for env in container.get('secrets', {})}
        container[u'secrets'] = [{"name": s, "valueFrom": new_secrets[s]} for s in new_secrets]
        self._diff.append(EcsTaskDefinitionDiff(container['name'], 'secrets', new_secrets, old_secrets))


class EcsTaskDefinitionDiff(object):
    def __init__(self, container, field, value, old_value):
        self.container = container
        self.field = field
        self.value = value
        self.old_value = old_value

    def __repr__(self):
        if self.container:
            return u"Changed %s of container '%s' to: %s (was: %s)" % (
                self.field,
                self.container,
                dumps(self.value),
                dumps(self.old_value)
            )
        else:
            return u"Changed %s to: %s (was: %s)" % (
                self.field,
                dumps(self.value),
                dumps(self.old_value)
            )


class EcsAction(object):
    def __init__(self, client, cluster_name, service_name):
        self._client = client
        self._cluster_name = cluster_name
        self._service_name = service_name

        try:
            if service_name:
                self._service = self.get_service()
        except IndexError:
            raise EcsConnectionError(
                u'An error occurred when calling the DescribeServices '
                u'operation: Service not found.'
            )
        except ClientError as e:
            raise EcsConnectionError(str(e))
        except NoCredentialsError:
            raise EcsConnectionError(
                u'Unable to locate credentials. Configure credentials '
                u'by running "aws configure".'
            )

    def get_service(self):
        services_definition = self._client.describe_services(
            cluster_name=self._cluster_name,
            service_name=self._service_name
        )
        return EcsService(
            cluster=self._cluster_name,
            service_definition=services_definition[u'services'][0]
        )

    def get_current_task_definition(self, service):
        task_definition_arn = service.task_definition
        return self.getEcsTaskDefinitionByArn(task_definition_arn)

    def _find_task_definition_by_deployment_identifier(self, task_definition_arns, deployment_identifier):
        for task_definition_arn in task_definition_arns:
            ecs_task_definition = self.getEcsTaskDefinitionByArn(task_definition_arn)
            if ecs_task_definition.tags.get('deployment_identifier') == deployment_identifier:
                return ecs_task_definition
        return None

    def get_task_definition_by_deployment_identifier(self, service, deployment_identifier):
        current_task_definition = self.get_current_task_definition(service)
        task_definition_arns, next_token = self._client.list_task_definitions(family=current_task_definition.family)
        td = self._find_task_definition_by_deployment_identifier(task_definition_arns, deployment_identifier)
        if td:
            return td

        while next_token is not None:
            task_definition_arns, next_token = self._client.list_task_definitions_for_next_token(
                family=current_task_definition.family, next_token=next_token,
            )
            td = self._find_task_definition_by_deployment_identifier(task_definition_arns, deployment_identifier)
            if td:
                return td

        raise UnrecoverableException(
            f'task definition does not exist for deployment_identifier: {deployment_identifier}')

    def getEcsTaskDefinitionByArn(self, task_definition_arn):
        task_definition_payload = self._client.describe_task_definition(
            task_definition_arn=task_definition_arn,
        )
        task_definition_payload[u'taskDefinition']['tags'] = task_definition_payload['tags']
        task_definition = EcsTaskDefinition(
            task_definition=task_definition_payload[u'taskDefinition']
        )
        return task_definition

    def get_task_definition(self, task_definition):
        task_definition_payload = self._client.describe_task_definition(
            task_definition_arn=task_definition
        )
        task_definition = EcsTaskDefinition(
            task_definition=task_definition_payload[u'taskDefinition']
        )
        return task_definition

    def update_task_definition(self, task_definition):
        response = self._client.register_task_definition(
            **task_definition
        )
        new_task_definition = EcsTaskDefinition(response[u'taskDefinition'])
        if 'previous_task_definition_arn' in task_definition.tags:
            self._client.deregister_task_definition(task_definition.tags.get('previous_task_definition_arn'))
        return new_task_definition

    def update_service(self, service):
        response = self._client.update_service(
            cluster=service.cluster,
            service=service.name,
            desired_count=service.desired_count,
            task_definition=service.task_definition
        )
        return EcsService(self._cluster_name, response[u'service'])

    def get_running_tasks_count(self, service, task_arns):
        running_count = 0
        tasks_details = self._client.describe_tasks(
            cluster_name=self._cluster_name,
            task_arns=task_arns
        )
        for task in tasks_details[u'tasks']:
            arn = task[u'taskDefinitionArn']
            status = task[u'lastStatus']
            if arn == service.task_definition and status == u'RUNNING':
                running_count += 1
        return running_count

    @property
    def client(self):
        return self._client

    @property
    def service(self):
        return self._service

    @property
    def cluster_name(self):
        return self._cluster_name

    @property
    def service_name(self):
        return self._service_name


class DeployAction(EcsAction):
    def deploy(self, task_definition):
        self._service.set_task_definition(task_definition)
        return self.update_service(self._service)


class ScaleAction(EcsAction):
    def scale(self, desired_count):
        self._service.set_desired_count(desired_count)
        return self.update_service(self._service)


class RunAction(EcsAction):
    def __init__(self, client, cluster_name):
        super(RunAction, self).__init__(client, cluster_name, None)
        self._client = client
        self._cluster_name = cluster_name
        self.started_tasks = []

    def run(self, task_definition, count, started_by):
        result = self._client.run_task(
            cluster=self._cluster_name,
            task_definition=task_definition.family_revision,
            count=count,
            started_by=started_by,
            overrides=dict(containerOverrides=task_definition.get_overrides())
        )
        self.started_tasks = result['tasks']
        return True


class EcsError(Exception):
    pass


class EcsConnectionError(EcsError):
    pass


class UnknownContainerError(EcsError):
    pass


class TaskPlacementError(EcsError):
    pass


class UnknownTaskDefinitionError(EcsError):
    pass
