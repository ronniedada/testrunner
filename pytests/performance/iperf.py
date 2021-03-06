import json
import time
import socket
import functools
from multiprocessing import Process, Event
from multiprocessing.sharedctypes import Value
from collections import defaultdict

from lib.membase.api import httplib2
from lib.membase.api.rest_client import RestConnection

from pytests.performance.eperf import EPerfClient, EVPerfClient


class PerfWrapper(object):

    """Class of decorators to run complicated tests (multiclient, rebalance,
    rampup, xdcr and etc.) based on general performance tests (from eperf and
    perf modules).
    """

    @staticmethod
    def multiply(test):
        @functools.wraps(test)
        def wrapper(self, *args, **kargs):
            """This wrapper allows to launch multiple tests on the same
            client (machine). Number of concurrent processes depends on phase
            and "total_clients" parameter in *.conf file.

            There is no need to specify "prefix" and "num_clients".

            Processes don't share memory. However they share stdout/stderr.
            """
            is_bi_xperf = isinstance(self, XPerfTests) and 'bi' in self.id()

            total_clients = self.parami('total_clients', 1)

            if is_bi_xperf:
                total_clients *= 2
            else:
                # Limit number of workers during load phase
                if self.parami('load_phase', 0):
                    total_clients = min(4, total_clients)

            self.input.test_params['num_clients'] = total_clients

            if self.parami('index_phase', 0):
                # Single-threaded tasks (hot load phase, index phase)
                self.input.test_params['prefix'] = 0
                return test(self, *args, **kargs)
            else:
                # Concurrent tasks (load_phase, access phase)
                executors = list()

                if is_bi_xperf:
                    prefix_range = xrange(total_clients / 2)
                else:
                    prefix_range = xrange(total_clients)

                for prefix in prefix_range:
                    self.input.test_params['prefix'] = prefix
                    self.is_leader = bool(prefix == 0)
                    executor = Process(target=test, args=(self, ))
                    executor.daemon = True
                    executor.start()
                    executors.append(executor)

                for executor in executors:
                    executor.join()

                return executors[0]
        return wrapper

    @staticmethod
    def rampup(test):
        @functools.wraps(test)
        def wrapper(self, *args, **kargs):
            """This wrapper launches two groups of processes:
            -- constant background load (mainly memcached sets/gets)
            -- constant foreground load (mainly view queries)

            However during the test number of foreground processes increases.
            So consistency only means number of operation per second.

            Processes use shared objects (ctype wrappers) for synchronization.
            """
            if self.parami('index_phase', 0) or self.parami('load_phase', 0):
                return PerfWrapper.multiply(test)(self, *args, **kargs)

            total_bg_clients = self.parami('total_bg_clients', 1)
            total_fg_clients = self.parami('total_fg_clients', 1)
            total_clients = total_bg_clients + total_fg_clients
            self.input.test_params['num_clients'] = total_clients

            executors = list()
            self.shutdown_event = Event()

            # Background load (memcached)
            original_delay = self.parami('start_delay', 30)
            original_fg_max_ops_per_sec = self.parami('fg_max_ops_per_sec',
                                                      1000)
            self.input.test_params['start_delay'] = 5
            self.input.test_params['fg_max_ops_per_sec'] = 100
            for prefix in range(0, total_bg_clients):
                self.input.test_params['prefix'] = prefix
                self.is_leader = bool(prefix == 0)
                executor = Process(target=test, args=(self, ))
                executor.start()
                executors.append(executor)

            # Foreground load (queries)
            self.input.test_params['start_delay'] = original_delay
            self.input.test_params['fg_max_ops_per_sec'] = \
                original_fg_max_ops_per_sec
            self.input.test_params['bg_max_ops_per_sec'] = 1

            self.active_fg_workers = Value('i', 0)  # signed int

            for prefix in range(total_bg_clients, total_clients):
                self.input.test_params['prefix'] = prefix
                self.is_leader = False
                executor = Process(target=test, args=(self, ))
                executor.start()
                executors.append(executor)

            # Shutdown
            for executor in executors:
                executor.join()
                self.active_fg_workers.value -= 1

            return executors[-1]
        return wrapper

    @staticmethod
    def xperf(bidir=False):
        def decorator(test):
            @functools.wraps(test)
            def wrapper(self, *args, **kargs):
                """Define remote cluster and start replication (only before
                load phase).
                """
                if self.parami('load_phase', 0) and \
                        not self.parami('hot_load_phase', 0):
                    master = self.input.clusters[0][0]
                    slave = self.input.clusters[1][0]
                    try:
                        self.start_replication(master, slave, bidir=bidir)
                    except Exception, why:
                        print why

                # Execute performance test
                region = XPerfTests.get_region()
                if 'bi' in self.id():
                    # Resolve conflicting keyspaces
                    self.input.test_params['cluster_prefix'] = region

                if region == 'east':
                    self.input.servers = self.input.clusters[0]
                    self.input.test_params['bucket'] = self.get_buckets()[0]
                    return PerfWrapper.multiply(test)(self, *args, **kargs)
                elif region == 'west':
                    self.input.servers = self.input.clusters[1]
                    self.input.test_params['bucket'] = \
                        self.get_buckets(reversed=True)[0]
                    return PerfWrapper.multiply(test)(self, *args, **kargs)
            return wrapper
        return decorator

    @staticmethod
    def xperf_load(test):
        @functools.wraps(test)
        def wrapper(self, *args, **kargs):
                """Run load phase, start unidirectional replication, collect
                and print XDCR related metrics"""
                PerfWrapper.multiply(test)(self, *args, **kargs)

                self.start_replication(self.input.clusters[0][0],
                                       self.input.clusters[1][0])

                self.collect_stats()
        return wrapper

    @staticmethod
    def rebalance(test):
        @functools.wraps(test)
        def wrapper(self, *args, **kargs):
            """Trigger cluster rebalance (in and out) when ~half of queries
            reached the goal.
            """
            total_clients = self.parami('total_clients', 1)
            rebalance_after = self.parami('rebalance_after', 0) / total_clients
            self.level_callbacks = [('cur-queries', rebalance_after,
                                     self.latched_rebalance)]
            return PerfWrapper.multiply(test)(self, *args, **kargs)
        return wrapper


class MultiClientTests(EVPerfClient):

    """Load tests with consistent number of clients. Each client performs the
    same work.
    """

    @PerfWrapper.multiply
    def test_evperf2(self):
        """3 design ddocs, 8 views per ddoc"""
        super(MultiClientTests, self).test_evperf2()

    @PerfWrapper.multiply
    def test_vperf2(self):
        """1 design ddoc, 8 views"""
        super(MultiClientTests, self).test_vperf2()

    @PerfWrapper.multiply
    def test_vperf4(self):
        """3 design ddocs, 2-2-4 views"""
        super(MultiClientTests, self).test_vperf4()


class RampUpTests(EVPerfClient):

    """Ramup-up load tests with increasing number of foreground workers.
    """

    @PerfWrapper.rampup
    def test_view_rampup_1(self):
        """1 design ddoc, 8 views"""
        super(RampUpTests, self).test_vperf2()


class XPerfTests(EPerfClient):

    """XDCR large-scale performance tests
    """

    def start_replication(self, master, slave, replication_type='continuous',
                          buckets=None, bidir=False, suffix='A'):
        """Add remote cluster and start replication"""

        master_rest_conn = RestConnection(master)
        remote_reference = 'remote_cluster_' + suffix

        master_rest_conn.add_remote_cluster(slave.ip, slave.port,
                                            slave.rest_username,
                                            slave.rest_password,
                                            remote_reference)

        if not buckets:
            buckets = self.get_buckets()
        else:
            buckets = self.get_buckets(reversed=True)

        for bucket in buckets:
            master_rest_conn.start_replication(replication_type, bucket,
                                               remote_reference)

        if self.parami('num_buckets', 1) > 1 and suffix == 'A':
            self.start_replication(slave, master, replication_type, buckets,
                                   suffix='B')

        if bidir:
            self.start_replication(slave, master, replication_type, buckets,
                                   suffix='B')

    def get_buckets(self, reversed=False):
        """Return list of buckets to be replicated"""

        num_buckets = self.parami('num_buckets', 1)
        if num_buckets > 1:
            num_replicated_buckets = self.parami('num_replicated_buckets',
                                                 num_buckets)
            buckets = ['bucket-{0}'.format(i) for i in range(num_buckets)]
            if not reversed:
                return buckets[:num_replicated_buckets]
            else:
                return buckets[-1:-1 - num_replicated_buckets:-1]
        else:
            return [self.param('bucket', 'default')]

    @staticmethod
    def get_region():
        """Try to identify public hostname and return corresponding EC2 region.

        In case of socket exception (it may happen when client is local VM) use
        VM hostname.

        Reference: http://bit.ly/instancedata
        """

        try:
            uri = 'http://169.254.169.254/latest/meta-data/public-hostname'
            http = httplib2.Http(timeout=5)
            response, hostname = http.request(uri)
        except (socket.timeout, socket.error):
            hostname = socket.gethostname()
        if 'west' in hostname:
            return 'west'
        else:
            return 'east'

    def get_samples(self, rest, server='explorer'):
        # TODO: fix hardcoded cluster names

        api = rest.baseUrl + \
            "pools/default/buckets/default/" + \
            "nodes/{0}.server.1:8091/stats?zoom=minute".format(server)

        _, content, _ = rest._http_request(api)

        return json.loads(content)['op']['samples']

    def collect_stats(self):
        # TODO: fix hardcoded cluster names

        # Initialize rest connection to master and slave servers
        master_rest_conn = RestConnection(self.input.clusters[0][0])
        slave_rest_conn = RestConnection(self.input.clusters[1][0])

        # Define list of metrics and stats containers
        metrics = ('mem_used', 'curr_items', 'vb_active_ops_create',
                   'ep_bg_fetched', 'cpu_utilization_rate')
        stats = {'slave': defaultdict(list), 'master': defaultdict(list)}

        # Calculate approximate number of relicated items per node
        num_nodes = self.parami('num_nodes', 1) / 2
        total_items = self.parami('items', 1000000)
        items = 0.99 * total_items / num_nodes

        # Get number of relicated items
        curr_items = self.get_samples(slave_rest_conn)['curr_items']

        # Collect stats until all items are replicated
        while curr_items[-1] < items:
            # Collect stats every 20 seconds
            time.sleep(19)

            # Slave stats
            samples = self.get_samples(slave_rest_conn)
            for metric in metrics:
                stats['slave'][metric].extend(samples[metric][:20])

            # Master stats
            samples = self.get_samples(master_rest_conn, 'nirvana')
            for metric in metrics:
                stats['master'][metric].extend(samples[metric][:20])

            # Update number of replicated items
            curr_items = stats['slave']['curr_items']

        # Aggregate and display stats
        vb_active_ops_create = sum(stats['slave']['vb_active_ops_create']) /\
            len(stats['slave']['vb_active_ops_create'])
        print "slave> AVG vb_active_ops_create: {0}, items/sec"\
            .format(vb_active_ops_create)

        ep_bg_fetched = sum(stats['slave']['ep_bg_fetched']) /\
            len(stats['slave']['ep_bg_fetched'])
        print "slave> AVG ep_bg_fetched: {0}, reads/sec".format(ep_bg_fetched)

        for server in stats:
            mem_used = max(stats[server]['mem_used'])
            print "{0}> MAX memory used: {1}, MB".format(server,
                                                         mem_used / 1024 ** 2)
            cpu_rate = sum(stats[server]['cpu_utilization_rate']) /\
                len(stats[server]['cpu_utilization_rate'])
            print "{0}> AVG CPU rate: {1}, %".format(server, cpu_rate)

    @PerfWrapper.xperf()
    def test_mixed_unidir(self):
        "Mixed KV workload"
        super(XPerfTests, self).test_eperf_mixed()

    @PerfWrapper.xperf(bidir=True)
    def test_mixed_bidir(self):
        "Mixed KV workload"
        super(XPerfTests, self).test_eperf_mixed()


class XVPerfTests(XPerfTests, EVPerfClient):

    """XDCR large-scale performance tests with views
    """

    @PerfWrapper.xperf()
    def test_vperf_unidir(self):
        """1 design ddoc, 8 views"""
        super(XVPerfTests, self).test_vperf2()

    @PerfWrapper.xperf(bidir=True)
    def test_vperf_bidir(self):
        """1 design ddoc, 8 views"""
        super(XVPerfTests, self).test_vperf2()

    @PerfWrapper.xperf()
    def test_vperf_3d_unidir(self):
        """3 design ddocs, 2-2-4 views"""
        super(XVPerfTests, self).test_vperf4()

    @PerfWrapper.xperf(bidir=True)
    def test_vperf_3d_bidir(self):
        """3 design ddocs, 2-2-4 views"""
        super(XVPerfTests, self).test_vperf4()

    @PerfWrapper.xperf_load
    def test_vperf_load(self):
        """1 design ddoc, 8 views"""
        super(XVPerfTests, self).test_vperf2()


class RebalanceTests(EVPerfClient):

    """Performance tests with rebalance during test execution.
    """

    @PerfWrapper.rebalance
    def test_view_rebalance_1(self):
        """1 design ddoc, 8 views"""
        super(RebalanceTests, self).test_vperf2()

    @PerfWrapper.rebalance
    def test_view_rebalance_2(self):
        """3 design ddocs, 2-2-4 views"""
        super(RebalanceTests, self).test_vperf4()
