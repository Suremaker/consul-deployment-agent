# Copyright (c) Trainline Limited, 2016-2017. All rights reserved. See
# LICENSE.txt in the project root for license information.

import argparse
import logging
import logging.config
import os.path
import platform
import sys
import yaml
import key_naming_convention
from consul - deployment - agent - health - checks import ConsulApi, ConsulError
from consul_data_loader import ConsulDataLoader
from deployment import Deployment
from environment import Environment, EnvironmentError
from retrying import retry, RetryError
from actions import InstallAction, IgnoreAction, UninstallAction

from version import semantic_version

parser = argparse.ArgumentParser()
parser.add_argument(
    '-config-dir', help='Location of configuration files (e.g. config.yml and config-logging.yml)')
parser.add_argument('-v', '--version', action='version',
                    version=semantic_version)

config = {
    'aws': {'access_key_id': None, 'aws_secret_access_key': None, 'deployment_logs': {'bucket_name': None, 'key_prefix': None}},
    'consul': {'host': 'localhost', 'port': 8500, 'scheme': 'http', 'acl_token': None, 'version': 'v1'},
    'sensu': {
        'healthcheck_search_paths': ['/etc/some_fake_path', '/opt/sensu_server_scripts'],
        'sensu_check_path': '/etc/sensu/conf.d/checks.local'
    },
    'logging': {
        'version': 1,
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'stream': 'ext://sys.stdout'
            }
        },
        'root': {
            'level': 'DEBUG',
            'handlers': ['console']
        }
    },
    'startup': {
        'delay_in_ms_between_readiness_check': 5000,
        'max_wait_for_instance_readiness_in_ms': 1800000,
        'semaphore_filepath': None,
        'wait_for_instance_readiness': False
    }
}


def load_configuration(args):
    if not args.config_dir:
        config_filepath = 'config.yml'
        config_logging_filepath = 'config-logging.yml'
    else:
        config_filepath = os.path.join(args.config_dir, 'config.yml')
        config_logging_filepath = os.path.join(
            args.config_dir, 'config-logging.yml')
    if os.path.isfile(config_filepath):
        config_settings = yaml.load(file(config_filepath, 'r'))
        if 'sensu' in config_settings and config_settings['sensu'] is not None:
            config['sensu'] = config_settings['sensu']
        if 'aws' in config_settings and config_settings['aws'] is not None:
            config['aws']['access_key_id'] = config_settings['aws'].get(
                'access_key_id')
            config['aws']['aws_secret_access_key'] = config_settings['aws'].get(
                'aws_secret_access_key')
            if 'deployment_logs' in config_settings['aws'] and config_settings['aws']['deployment_logs'] is not None:
                config['aws']['deployment_logs']['bucket_name'] = config_settings['aws']['deployment_logs'].get(
                    'bucket_name')
                config['aws']['deployment_logs']['key_prefix'] = config_settings['aws']['deployment_logs'].get(
                    'key_prefix')
        if 'consul' in config_settings and config_settings['consul'] is not None:
            config['consul']['acl_token'] = config_settings['consul'].get(
                'acl_token')
        if 'startup' in config_settings and config_settings['startup'] is not None:
            config['startup']['semaphore_filepath'] = config_settings['startup'].get(
                'semaphore_filepath')
            config['startup']['wait_for_instance_readiness'] = config_settings['startup'].get(
                'wait_for_instance_readiness', False)
    if os.path.isfile(config_logging_filepath):
        config['logging'] = yaml.load(file(config_logging_filepath, 'r'))
    return config


def wait_for_instance_readiness(config):
    def retry_if_false(result):
        return not result

    @retry(retry_on_result=retry_if_false, wait_fixed=config['startup']['delay_in_ms_between_readiness_check'], stop_max_delay=config['startup']['max_wait_for_instance_readiness_in_ms'])
    def wait_for_semaphore_file(semaphore_filepath):
        logging.debug('Checking presence and content of semaphore file at {0}'.format(
            semaphore_filepath))
        if os.path.isfile(filepath):
            logging.info('Semaphore file exists, validating content')
            with open(filepath, 'r') as semaphore_file:
                content = semaphore_file.read().replace('\n', '')
                logging.debug('Semaphore file content: {0}'.format(content))
                if content.lower() == 'ok':
                    return True
        logging.debug('Instance not ready for deployments, waiting {0} ms.'.format(
            config['startup']['delay_in_ms_between_readiness_check']))
        return False
    filepath = config['startup']['semaphore_filepath']
    if filepath is None or not filepath:
        logging.warning(
            'No semaphore file path provided, assuming instance is ready for deployments.')
        return
    try:
        wait_for_semaphore_file(filepath)
        logging.info('Instance ready for deployments.')
    except RetryError:
        logging.warning(
            'Instance readiness timeout has been reached, will assume instance is ready for deployments.')


def execute(action, action_info, environment, consul_api):
    if isinstance(action, InstallAction):
        deployment_config = {
            'cause': 'Deployment',
            'deployment_id': action.deployment_id,
            'environment': environment,
            'last_deployment_id': action_info['last_deployment_id'],
            'platform': platform.system().lower(),
            'sensu': config['sensu'],
            'service': action.service
        }
        deployment = Deployment(config=deployment_config,
                                consul_api=consul_api, aws_config=config['aws'])
        return deployment.run()
    elif isinstance(action, IgnoreAction):
        logging.info(
            'Found Ignore action, not installing \'{0}\''.format(action.service.id))
        return {'id': action.deployment_id, 'is_success': True}
    elif isinstance(action, UninstallAction):
        logging.info('Uninstall action not yet supported!')
        return {'id': action.deployment_id, 'is_success': True}


def converge(consul_api, environment):
    try:
        data_loader = ConsulDataLoader(consul_api)

        server_role = data_loader.load_server_role(environment)
        logging.info('Server role configuration: {0}'.format(server_role))

        registered_services = data_loader.load_service_catalogue()
        logging.debug('Registered services:')
        for service in registered_services:
            logging.debug(service)

        logging.info('Start converging to server role configuration.')
        missing_action = server_role.find_action_to_execute(
            registered_services)
        while missing_action is not None:
            action, action_info = missing_action
            report = execute(action, action_info, environment, consul_api)
            # if not report['is_success']:
            server_role.quarantine_action(report['id'])
            missing_action = server_role.find_action_to_execute(
                data_loader.load_service_catalogue())

        logging.info('Finished converging to server role configuration.')
        return True
    except:
        logging.exception(sys.exc_info()[1])
        return False


def main():
    logging.config.dictConfig(config['logging'])
    logging.info(
        'Start initialisation, consul-deployment-agent version: {0}'.format(semantic_version))
    try:
        environment = Environment()
        logging.info('Environment configuration: {0}'.format(environment))
    except EnvironmentError as error:
        logging.exception(error)
        logging.critical(
            'Failed to load instance configuration. Exiting with error code 1.')
        sys.exit(1)

    try:
        consul_api = ConsulApi(config['consul'])
        consul_api.check_connectivity()
    except ConsulError as error:
        logging.exception(error)
        logging.critical('Exiting with error code 1.')
        sys.exit(1)

    if config['startup']['wait_for_instance_readiness']:
        wait_for_instance_readiness(config)

    if converge(consul_api, environment):
        logging.info('Initialisation completed.')
    else:
        logging.error('Initialisation failed.')

    server_role_key = key_naming_convention.get_server_role_key(environment)
    while True:
        try:
            consul_api.wait_for_change(server_role_key)
            logging.info(
                'Change detected in Consul {0} key space.'.format(server_role_key))
            logging.info(
                'Start converging to updated server role configuration...')
            if converge(consul_api, environment):
                logging.info(
                    'Finished converging to updated server role configuration.')
            else:
                logging.error(
                    'Failed to converge to updated server role configuration.')
        except ConsulError as error:
            logging.error(
                'Error detecting changes in Consul key-value store. Skipping converging configuration.')
            logging.exception(error)


if __name__ == '__main__':
    args = parser.parse_args()
    config = load_configuration(args)
    main()
