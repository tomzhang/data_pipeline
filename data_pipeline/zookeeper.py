# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

import signal
import sys
from contextlib import contextmanager

import kazoo.client
import yelp_lib.config_loader
from kazoo.exceptions import LockTimeout
from kazoo.retry import KazooRetry
from yelp_lib.classutil import cached_property

from data_pipeline.config import get_config

log = get_config().logger

KAZOO_CLIENT_DEFAULTS = {
    'timeout': 30
}


class ZK(object):
    """A class for zookeeper interactions"""

    @property
    def max_tries(self):
        return 3

    @cached_property
    def ecosystem(self):
        return open('/nail/etc/ecosystem').read().strip()

    def __init__(self):
        retry_policy = KazooRetry(max_tries=self.max_tries)
        self.zk_client = self.get_kazoo_client(command_retry=retry_policy)
        self.zk_client.start()

    def _get_local_zk(self):
        path = get_config().zookeeper_discovery_path.format(ecosystem=self.ecosystem)
        """Get (with caching) the local zookeeper cluster definition."""
        return yelp_lib.config_loader.load(path, '/')

    def _get_kazoo_client_for_cluster_def(self, cluster_def, **kwargs):
        """Get a KazooClient for a list of host-port pairs `cluster_def`."""
        host_string = ','.join('%s:%s' % (host, port) for host, port in cluster_def)

        for default_kwarg, default_value in KAZOO_CLIENT_DEFAULTS.iteritems():
            if default_kwarg not in kwargs:
                kwargs[default_kwarg] = default_value

        return kazoo.client.KazooClient(host_string, **kwargs)

    def get_kazoo_client(self, **kwargs):
        """Get a KazooClient for a local zookeeper cluster."""
        return self._get_kazoo_client_for_cluster_def(self._get_local_zk(), **kwargs)

    def close(self):
        """Clean up the zookeeper client."""
        log.info("Stopping zookeeper")
        self.zk_client.stop()
        log.info("Closing zookeeper")
        self.zk_client.close()

    def _exit_gracefully(self, sig, frame):
        self.close()
        if sig == signal.SIGINT:
            self.original_int_handler(sig, frame)
        elif sig == signal.SITERM:
            self.original_term_handler(sig, frame)

    def register_signal_handlers(self):
        self.original_int_handler = signal.getsignal(signal.SIGINT)
        self.original_term_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._exit_gracefully)
        signal.signal(signal.SIGTERM, self._exit_gracefully)


class ZKLock(ZK):
    @contextmanager
    def lock(self, name, namespace, timeout=10):
        """Sets up zookeeper lock so that only one copy of the batch is run per cluster.
        This would make sure that data integrity is maintained (See DATAPIPE-309 for an example).
        Use it as a context manager (with ZK().lock(name, namespace)."""
        self.lock = self.zk_client.Lock("/{} - {}".format(name, namespace), namespace)
        try:
            self.lock.acquire(timeout=timeout)
            self.register_signal_handlers()
            yield
            self.close()
        except LockTimeout:
            log.warning("Already one instance running against this source! exit. See y/oneandonly for help.")
            self.close()
            sys.exit(1)
            yield  # needed for tests where we mock sys.exit

    def close(self):
        if self.lock.is_acquired:
            log.info("Releasing the lock...")
            self.lock.release()
        super(ZKLock, self).close()
