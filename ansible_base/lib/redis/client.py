import copy
import logging
import os
from typing import Union
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext as _
from django_redis.client import DefaultClient
from redis import Redis
from redis.cluster import ClusterNode, RedisCluster
from redis.exceptions import RedisClusterException

logger = logging.getLogger('ansible_base.lib.redis.client')


# We are going to build our own cluster class to override the mget function
# In a redis cluster, keys might not be in the same slot and this will throw off mget.
# Instead, we are going to try and use mget and then, if we get the slot error, we will try the mget_nonatomic to make it work
class DABRedisCluster(RedisCluster):
    def mget(self, *args, **kwargs):
        try:
            return super().mget(*args, **kwargs)
        except RedisClusterException as e:
            if 'MGET - all keys must map to the same key slot' in str(e):
                return super().mget_nonatomic(*args, **kwargs)
            raise


class RedisClient(DefaultClient):
    def _get_client_args(self):
        return self._params.get('OPTIONS', {}).get('CLIENT_CLASS_KWARGS', {})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        connection_kwargs = self._get_client_args()
        self.clustered = connection_kwargs.get('clustered', False)
        self.clustered_hosts = connection_kwargs.get('clustered_hosts', '')

    def connect(self, index: int = 0) -> Union[Redis, RedisCluster]:
        """
        Given a connection index, returns a new raw redis client/connection
        instance. Index is used for replication setups and indicates that
        connection string should be used. In normal setups, index is 0.
        """

        # Make a deep copy of the CLIENT_CLASS_KWARGS so we don't accidentally modify the actual settings
        kwargs = copy.deepcopy(self._get_client_args())

        # remove our settings which are invalid to the parent classes
        kwargs.pop('clustered', None)
        kwargs.pop('clustered_hosts', None)

        # If we can't parse this just let it raise because other things will fail anyway
        parsed_url = urlparse(self._server[index])
        if parsed_url.scheme in ['file', 'unix']:
            # Attempt to attach to a socket if its a file or unix scheme
            kwargs['unix_socket_path'] = parsed_url.path
        elif parsed_url.scheme in ['redis', 'rediss']:
            # Extract information from a rediss url
            for arg_name, parse_name in [('host', 'hostname'), ('port', 'port'), ('username', 'username'), ('password', 'password')]:
                attribute = getattr(parsed_url, parse_name, None)
                if attribute:
                    kwargs[arg_name] = attribute

            # Add the DB from the URL (if passed)
            try:
                kwargs['db'] = int(parsed_url.path.split('/')[1])
            except (IndexError, ValueError):
                pass
        else:
            raise ImproperlyConfigured('This redis client can only accept file, unix, redis or rediss URLs')

        # Add any additional query params from the URL as kwargs
        for key, value in parse_qs(parsed_url.query).items():
            kwargs[key] = value[-1]

        if kwargs.get('ssl', None):
            for file_setting in ['ssl_certfile', 'ssl_keyfile', 'ssl_ca_certs']:
                file = kwargs.get(file_setting, None)
                if file and not os.access(file, os.R_OK):
                    raise ImproperlyConfigured(_('Unable to read file {} from setting {}').format(file, file_setting))

        # Connect to either a cluster or a standalone redis
        if self.clustered:
            logger.debug("Connecting to Redis clustered")
            if self.clustered_hosts:
                kwargs.pop('host', None)
                kwargs.pop('port', None)
                startup_nodes = []

                translated_generic_exception = ImproperlyConfigured(_('Unable to parse cluster_hosts, see logs for more details'))

                # Make sure we have a string for clustered_hosts
                if not isinstance(self.clustered_hosts, str):
                    logger.error(f"Specified clustered_hosts is not a string, got: {self.clustered_hosts}")
                    raise translated_generic_exception

                host_ports = self.clustered_hosts.split(',')
                for host_port in host_ports:
                    try:
                        node, port_string = host_port.split(':')
                    except ValueError:
                        logger.error(f"Specified cluster_host {host_port} is not valid; it needs to be in the format <host>:<port>")
                        raise translated_generic_exception

                    # Make sure we have an int for the port
                    try:
                        port = int(port_string)
                    except ValueError:
                        logger.error(f'Specified port on {host_port} is not an int')
                        raise translated_generic_exception

                    startup_nodes.append(ClusterNode(node, port))

                kwargs['startup_nodes'] = startup_nodes
            return DABRedisCluster(**kwargs)
        else:
            logger.debug("Connecting to Redis standalone")
            return Redis(**kwargs)
