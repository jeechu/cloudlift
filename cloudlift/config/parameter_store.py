import re

from cloudlift.exceptions import UnrecoverableException

from cloudlift.config import get_client_for
from cloudlift.config.logging import log_err
from cloudlift.utils import chunks


class ParameterStore(object):
    def __init__(self, service_name, environment):
        self.service_name = service_name
        self.environment = environment
        self.path_prefix = "/%s/%s/" % (self.environment, self.service_name)
        # TODO: Use the below two lines when all parameter store actions
        # require MFA
        #
        # mfa_region = get_region_for_environment(environment)
        # mfa_session = mfa.get_mfa_session(mfa_region)
        # ssm_client = mfa_session.client('ssm')
        self.client = get_client_for('ssm', environment)

    def get_existing_config_as_string(self, sidecar_name=None):
        environment_configs, sidecar_configs = self.get_existing_config()

        result_configs = sidecar_configs.get(sidecar_name, {}) \
            if sidecar_name is not None and sidecar_name != "" \
            else environment_configs

        return '\n'.join('{}={}'.format(key, val) for key, val in sorted(
            result_configs.items()
        ))

    def get_existing_config(self):
        environment_configs = {}
        sidecars_configs = {}
        next_token = None
        while True:
            if next_token:
                response = self.client.get_parameters_by_path(
                    Path=self.path_prefix,
                    Recursive=True,
                    WithDecryption=True,
                    MaxResults=10,
                    NextToken=next_token
                )
            else:
                response = self.client.get_parameters_by_path(
                    Path=self.path_prefix,
                    Recursive=True,
                    WithDecryption=True,
                    MaxResults=10
                )
            for parameter in response['Parameters']:
                parameter_name = parameter['Name'].split(self.path_prefix)[1]
                if parameter_name.startswith('sidecars/'):
                    sidecar_name, sidecar_parameter = parameter_name.replace('sidecars/', '', 1).split('/')
                    if sidecar_name not in sidecars_configs:
                        sidecars_configs[sidecar_name] = {}

                    sidecars_configs[sidecar_name].update({sidecar_parameter: parameter['Value']})
                else:
                    environment_configs[parameter_name] = parameter['Value']

            try:
                next_token = response['NextToken']
            except KeyError:
                break
        return environment_configs, sidecars_configs

    def get_existing_config_paths(self):
        ''' This returns complete path of parameter store keys for service secrets '''
        environment_configs = {}
        sidecars_configs = {}
        next_token = None
        while True:
            if next_token:
                response = self.client.get_parameters_by_path(
                    Path=self.path_prefix,
                    Recursive=False,
                    WithDecryption=False,
                    MaxResults=10,
                    NextToken=next_token
                )
            else:
                response = self.client.get_parameters_by_path(
                    Path=self.path_prefix,
                    Recursive=False,
                    WithDecryption=False,
                    MaxResults=10
                )
            for parameter in response['Parameters']:
                parameter_name = parameter['Name'].split(self.path_prefix)[1]
                if parameter_name.startswith('sidecars/'):
                    sidecar_name, sidecar_parameter = parameter_name.replace('sidecars/', '', 1).split('/')
                    if sidecar_name not in sidecars_configs:
                        sidecars_configs[sidecar_name] = {}

                    sidecars_configs[sidecar_name].update({sidecar_parameter: parameter['Name']})
                else:
                    environment_configs[parameter_name] = parameter['Name']

            try:
                next_token = response['NextToken']
            except:
                break
        return environment_configs, sidecars_configs

    def set_config(self, differences, sidecar_name=None):
        self._validate_changes(differences)
        path_prefix = self.path_prefix if sidecar_name is None else '{}sidecars/{}/'.format(self.path_prefix,
                                                                                            sidecar_name)
        for parameter_change in differences:
            if parameter_change[0] == 'change':
                self.client.put_parameter(
                    Name='%s%s' % (path_prefix, parameter_change[1]),
                    Value=parameter_change[2][1],
                    Type='SecureString',
                    KeyId='alias/aws/ssm',
                    Overwrite=True
                )
            elif parameter_change[0] == 'add':
                for added_parameter in parameter_change[2]:
                    self.client.put_parameter(
                        Name='%s%s' % (path_prefix, added_parameter[0]),
                        Value=added_parameter[1],
                        Type='SecureString',
                        KeyId='alias/aws/ssm',
                        Overwrite=False
                    )
            elif parameter_change[0] == 'remove':
                deleted_parameters = ["%s%s" % (path_prefix, item[0]) for item in parameter_change[2]]
                for chunked_parameters in chunks(deleted_parameters, 10):
                    self.client.delete_parameters(
                        Names=chunked_parameters
                    )

    def _validate_changes(self, differences):
        errors = []
        for parameter_change in differences:
            if parameter_change[0] == 'change':
                if not self._is_a_valid_parameter_key(parameter_change[1]):
                    errors.append("'%s' is not a valid key." % parameter_change[1])
            elif parameter_change[0] == 'add':
                for added_parameter in parameter_change[2]:
                    if not self._is_a_valid_parameter_key(added_parameter[0]):
                        errors.append("'%s' is not a valid key." % added_parameter[0])
            elif parameter_change[0] == 'remove':
                # No validation required
                pass
        if errors:
            for error in errors:
                log_err(error)
            raise UnrecoverableException("Environment variables validation failed with above errors.")
        return True

    def _is_a_valid_parameter_key(self, key):
        return bool(re.match(r"^[\w|\.|\-|\/]+$", key))
