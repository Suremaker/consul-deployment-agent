# Copyright (c) Trainline Limited, 2016-2017. All rights reserved. See LICENSE.txt in the project root for license information.

import boto.ec2, boto.utils, json, logging, socket, sys
from os import getenv

class EnvironmentError(RuntimeError):
    pass

class Environment(object):
    def __init__(self):
        self.environment_name = self.instance_id = self.ip_address = self.region = self.server_role = None
        logging.debug('Detecting if current instance is running in AWS.')
        if boto.utils.get_instance_metadata(timeout=1, num_retries=1) == {}:
            logging.debug('Not running in AWS, using default environment values.')
            self.environment_name = getenv('TTL_ENVIRONMENT', 'local')
            self.environment_type = getenv('TTL_ENVIRONMENT_TYPE', 'local')
            self.server_role = getenv('TTL_ROLE', 'test')
            self.instance_tags = {
                'Environment': self.environment_name,
                'EnvironmentType': self.environment_type,
                'aws:autoscaling:groupName': 'local_asg_name'
            }
            self.cluster = 'test_cluster'
            self.instance_id = socket.gethostname()
            self.ip_address = [l for l in ([ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")][:1], [[(s.connect(('8.8.8.8', 53)), s.getsockname()[0], s.close()) for s in [socket.socket(socket.AF_INET, socket.SOCK_DGRAM)]][0][1]]) if l][0][0]
        else:
            logging.debug('Running in AWS, will attempt to retrieve environment information from EC2 instance metadata.')
            self._populate_from_ec2()
        self._validate()

    def __str__(self):
        return json.dumps(
            {'environment_name':self.environment_name,
             'environment_type':self.environment_type,
             'instance_id':self.instance_id,
             'ip_address':self.ip_address,
             'region':self.region,
             'server_role':self.server_role,
             'instance_tags': self.instance_tags})

    def _populate_from_ec2(self):
        instance_metadata = boto.utils.get_instance_metadata()
        logging.debug('EC2 instance metadata: %s' % instance_metadata)
        self.instance_id = str(instance_metadata['instance-id'])
        self.ip_address = str(instance_metadata['local-ipv4'])
        try:
            logging.debug('Retrieving AWS region from EC2 instance identity.')
            instance_identity = boto.utils.get_instance_identity().get('document', {})
            logging.debug('EC2 instance identity: %s' % instance_identity)
            self.region = str(instance_identity.get('region'))
            ec2conn = boto.ec2.connect_to_region(self.region)
            reservations = ec2conn.get_all_instances(instance_ids=[self.instance_id])
            for reservation in reservations:
                for instance in reservation.instances:
                    self.instance_tags = instance.tags
                    self.environment_name = str(instance.tags.get('Environment'))
                    self.environment_type = str(instance.tags.get('EnvironmentType'))
                    self.server_role = str(instance.tags.get('Role'))
                    self.cluster = str(instance.tags.get('OwningCluster'))
        except:
            logging.exception(sys.exc_info()[1])
            raise EnvironmentError('Failed to retrieve instance information from EC2.')

    def _validate(self):
        def check_not_none(property_name, value):
            if not value:
                raise EnvironmentError('\'{0}\' key must be specified.'.format(property_name))
        check_not_none('environment_name', self.environment_name)
        check_not_none('instance_id', self.instance_id)
        check_not_none('ip_address', self.ip_address)
        check_not_none('server_role', self.server_role)
        check_not_none('cluster', self.cluster)
