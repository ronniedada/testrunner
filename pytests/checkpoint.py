import time
import uuid
import logger
import unittest

from threading import Thread
from tasks.future import TimeoutError
from TestInput import TestInputSingleton
from couchbase.cluster import Cluster
from couchbase.stats_tools import StatsCommon
from membase.api.rest_client import RestConnection
from membase.helper.bucket_helper import BucketOperationHelper
from membase.helper.cluster_helper import ClusterOperationHelper
from memcached.helper.data_helper import MemcachedClientHelper
from membase.helper.failover_helper import FailoverHelper

log = logger.Logger.get_logger()

ACTIVE="active"
REPLICA1="replica1"
REPLICA2="replica2"
Replica3="replica3"

class CheckpointTests(unittest.TestCase):

    def setUp(self):
        self.cluster = Cluster()

        self.input = TestInputSingleton.input
        self.servers = self.input.servers
        self.num_servers = self.input.param("servers", 1)
        self.items = self.input.param("items", 12000)
        self.chk_size = self.input.param("chk_size", 5000)

        master = self.servers[0]
        num_replicas = self.input.param("replicas", 1)
        self.bucket = 'default'

        # Start: Should be in a before class function
        BucketOperationHelper.delete_all_buckets_or_assert(self.servers, self)
        for server in self.servers:
            ClusterOperationHelper.cleanup_cluster([server])
        ClusterOperationHelper.wait_for_ns_servers_or_assert([master], self)
        # End: Should be in a before class function

        self.quota = self.cluster.init_node(master)
        self.old_vbuckets = self._get_vbuckets(master)
        ClusterOperationHelper.set_vbuckets(master, 1)
        self.cluster.create_default_bucket(master, self.quota, num_replicas)
        self.cluster.rebalance(self.servers[:self.num_servers],
                               self.servers[1:self.num_servers], [])

    def tearDown(self):
        master = self.servers[0]
        ClusterOperationHelper.set_vbuckets(master, self.old_vbuckets)
        rest = RestConnection(master)
        rest.stop_rebalance()
        self.cluster.rebalance(self.servers[:self.num_servers], [],
                               self.servers[1:self.num_servers])
        self.cluster.bucket_delete(master, self.bucket)
        self.cluster.shutdown()

    def checkpoint_create_items(self):
        param = 'checkpoint'
        stat_key = 'vb_0:open_checkpoint_id'
        num_items = 6000

        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        self._set_checkpoint_size(self.servers[:self.num_servers], self.bucket, '5000')
        chk_stats = StatsCommon.get_stats(self.servers[:self.num_servers], self.bucket,
                                           param, stat_key)
        load_thread = self.generate_load(master, self.bucket, num_items, "key1")
        load_thread.join()
        stats = []
        for server, value in chk_stats.items():
            StatsCommon.build_stat_check(server, param, stat_key, '>', value, stats)
        task = self.cluster.async_wait_for_stats(stats, self.bucket)
        try:
            timeout = 30 if (num_items * .001) < 30 else num_items * .001
            task.result(timeout)
        except TimeoutError:
            self.fail("New checkpoint not created")

    def checkpoint_create_time(self):
        param = 'checkpoint'
        timeout = 60
        stat_key = 'vb_0:open_checkpoint_id'

        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        self._set_checkpoint_timeout(self.servers[:self.num_servers], self.bucket, str(timeout))
        chk_stats = StatsCommon.get_stats(self.servers[:self.num_servers], self.bucket,
                                          param, stat_key)
        stats = []
        for server, value in chk_stats.items():
            StatsCommon.build_stat_check(server, param, stat_key, '>', value, stats)
        load_thread = self.generate_load(master, self.bucket, 1, "key1")
        load_thread.join()
        log.info("Sleeping for {0} seconds)".format(timeout))
        time.sleep(timeout)
        task = self.cluster.async_wait_for_stats(stats, self.bucket)
        try:
            task.result(60)
        except TimeoutError:
            self.fail("New checkpoint not created")
        self._set_checkpoint_timeout(self.servers[:self.num_servers], self.bucket, str(600))

    def checkpoint_collapse(self):
        param = 'checkpoint'
        chk_size = 5000
        num_items = 25000
        stat_key = 'vb_0:last_closed_checkpoint_id'
        stat_chk_itms = 'vb_0:num_checkpoint_items'

        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        slave1 = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, REPLICA1)
        slave2 = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, REPLICA2)
        self._set_checkpoint_size(self.servers[:self.num_servers], self.bucket, str(chk_size))
        m_stats = StatsCommon.get_stats([master], self.bucket, param, stat_key)
        self._stop_replication(slave2, self.bucket)
        load_thread = self.generate_load(master, self.bucket, num_items, "key1")
        load_thread.join()

        stats = []
        chk_pnt = str(int(m_stats[m_stats.keys()[0]]) + (num_items / chk_size))
        chk_items = num_items - (chk_size * 2)
        StatsCommon.build_stat_check(master, param, stat_key, '==', chk_pnt, stats)
        StatsCommon.build_stat_check(slave1, param, stat_key, '==', chk_pnt, stats)
        StatsCommon.build_stat_check(slave1, param, stat_chk_itms, '>=', str(num_items), stats)
        task = self.cluster.async_wait_for_stats(stats, self.bucket)
        try:
            task.result(60)
        except TimeoutError:
            self.fail("Checkpoint not collapsed")

        stats = []
        self._start_replication(slave2, self.bucket)
        chk_pnt = str(int(m_stats[m_stats.keys()[0]]) + (num_items / chk_size))
        StatsCommon.build_stat_check(slave2, param, stat_key, '==', chk_pnt, stats)
        StatsCommon.build_stat_check(slave1, param, stat_chk_itms, '<', num_items, stats)
        task = self.cluster.async_wait_for_stats(stats, self.bucket)
        try:
            task.result(60)
        except TimeoutError:
            self.fail("Checkpoints not replicated to secondary slave")

    def checkpoint_deduplication(self):
        param = 'checkpoint'
        stat_key = 'vb_0:num_checkpoint_items'

        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        slave1 = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, REPLICA1)
        self._set_checkpoint_size(self.servers[:self.num_servers], self.bucket, '5000')
        self._stop_replication(slave1, self.bucket)
        load_thread = self.generate_load(master, self.bucket, 4500, "key1")
        load_thread.join()
        load_thread = self.generate_load(master, self.bucket, 1000, "key1")
        load_thread.join()
        self._start_replication(slave1, self.bucket)

        stats = []
        StatsCommon.build_stat_check(master, param, stat_key, '==', 4501, stats)
        StatsCommon.build_stat_check(slave1, param, stat_key, '==', 4501, stats)
        task = self.cluster.async_wait_for_stats(stats, self.bucket)
        try:
            task.result(60)
        except TimeoutError:
            self.fail("Items weren't deduplicated")

    def checkpoint_failover(self):
        """Testing checkpoints in a setup- failover slave1 and stop replication on slave2
        Expect backfill to trigger in, when more data is loaded on the master and slave2

        Failure Condition:
          Error: Checkpoint stats from either nodes dont match.

        TODO :
           Eliminate/Merge _get_stats_checkpoint with existing build_stats_check function
        """
        param = 'checkpoint'
        stat_key = 'vb_0:last_closed_checkpoint_id'
        master = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, ACTIVE)
        slave1 = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, REPLICA1)
        slave2 = self._get_server_by_state(self.servers[:self.num_servers], self.bucket, REPLICA2)
        self._set_checkpoint_size(self.servers[:self.num_servers], self.bucket, str(self.chk_size))

        delta = self._calc_checkpoint_delta(self.items)
        chk_pnt_master = delta
        chk_pnt_slave1 = delta
        chk_pnt_slave2 = delta

        log.info("Loaded %s items on master[%s], slave1[%s], slave2[%s]"
        % (self.items, master, slave1, slave2))
        # load items items
        load_thread = self.generate_load(master, self.bucket, self.items, "key1")
        load_thread.join()

        self._get_checkpoint_stats(master, chk_pnt_master)
        self._get_checkpoint_stats(slave1, chk_pnt_slave1)
        self._get_checkpoint_stats(slave2, chk_pnt_slave2)

        # Throttle slave2
        log.info("Throttle slave2[node  %s] and load %s items" % (slave2, self.items))
        self._stop_replication(slave2, self.bucket)
        # load items
        load_thread = self.generate_load(master, self.bucket, self.items, "key2")
        load_thread.join()

        delta = self._calc_checkpoint_delta(self.items)
        chk_pnt_master = chk_pnt_master + delta
        chk_pnt_slave1 = chk_pnt_slave1 + delta

        self._get_checkpoint_stats(master, chk_pnt_master)
        self._get_checkpoint_stats(slave1, chk_pnt_slave1)
        self._get_checkpoint_stats(slave2, chk_pnt_slave2)

        #Failover slave1
        if self._failover(master, slave1) is True:
            log.info("Failed over slave1[node  %s] and loading %s items" % (slave1, self.items))
        else:
            self.fail("Error! Unable to failover node %s" % slave1)

        # load items
        load_thread = self.generate_load(master, self.bucket, self.items, "key3")
        load_thread.join()

        delta = self._calc_checkpoint_delta(self.items)
        chk_pnt_master = chk_pnt_master + delta
        #error on execution -slave1 chkpoint ==master chkpoint

        self._get_checkpoint_stats(master, chk_pnt_master)
        self._get_checkpoint_stats(slave1, chk_pnt_slave1)
        self._get_checkpoint_stats(slave2, chk_pnt_slave2)

        # Enable Replication on slave2
        log.info("Enable replication on slave2[node  %s]" % slave2)
        self._start_replication(slave2, self.bucket)

        time.sleep(2)
        chk_pnt_slave2 = chk_pnt_master
        self._get_checkpoint_stats(master, chk_pnt_master)
        self._get_checkpoint_stats(slave1, chk_pnt_slave1)
        self._get_checkpoint_stats(slave2, chk_pnt_slave2)

        # TODO:Add kv-store integrity stuff on this.


    def _set_checkpoint_size(self, servers, bucket, size):
        for server in servers:
            client = MemcachedClientHelper.direct_client(server, bucket)
            client.set_flush_param('chk_max_items', size)

    def _set_checkpoint_timeout(self, servers, bucket, time):
        for server in servers:
            client = MemcachedClientHelper.direct_client(server, bucket)
            client.set_flush_param('chk_period', time)

    def _stop_replication(self, server, bucket):
        client = MemcachedClientHelper.direct_client(server, bucket)
        client.set_flush_param('tap_throttle_queue_cap', '0')

    def _start_replication(self, server, bucket):
        client = MemcachedClientHelper.direct_client(server, bucket)
        client.set_flush_param('tap_throttle_queue_cap', '1000000')

    def _get_vbuckets(self, server):
        rest = RestConnection(server)
        command = "ns_config:search(couchbase_num_vbuckets_default)"
        status, content = rest.diag_eval(command)

        try:
            vbuckets = int(re.sub('[^\d]', '', content))
        except:
            vbuckets = 1024
        return vbuckets

    def _get_server_by_state(self, servers, bucket, vb_state):
        rest = RestConnection(servers[0])
        vbuckets = rest.get_vbuckets(self.bucket)[0]
        addr = None
        if vb_state == ACTIVE:
            addr = vbuckets.master
        elif vb_state == REPLICA1:
            addr = vbuckets.replica[0].encode("ascii", "ignore")
        elif vb_state == REPLICA2:
            addr = vbuckets.replica[1].encode("ascii", "ignore")
        elif vb_state == REPLICA3:
            addr = vbuckets.replica[2].encode("ascii", "ignore")
        else:
            return None

        addr = addr.split(':', 1)[0]
        for server in servers:
            if addr == server.ip:
                return server
        return None

    def generate_load(self, server, bucket, num_items, prefix):
        class LoadGen(Thread):
            def __init__(self, server, bucket, num_items, prefix):
                Thread.__init__(self)
                self.server = server
                self.bucket = bucket
                self.num_items = num_items
                self.prefix = prefix

            def run(self):
                client = MemcachedClientHelper.direct_client(server, bucket)
                for i in range(num_items):
                    key = "key-{0}-{1}".format(prefix, i)
                    value = "value-{0}".format(str(uuid.uuid4())[:7])
                    client.set(key, 0, 0, value, 0)
                log.info("Loaded {0} key".format(num_items))

        load_thread = LoadGen(server, bucket, num_items, prefix)
        load_thread.start()
        return load_thread

    def _failover(self, master, failover_node):
        rest = RestConnection(master)
        return rest.fail_over(failover_node)

    def _get_checkpoint_stats(self, server, checkpoint):
        start = time.time()
        while time.time() - start <= 60:
            mc_conn = MemcachedClientHelper.direct_client(server, self.bucket)
            stats = mc_conn.stats("checkpoint")
            last_closed_chkpoint = stats["vb_0:last_closed_checkpoint_id"]

            log.info(
                "Bucket {0} node{1}:{2} \n open_checkpoint : {3}, closed checkpoint : {4} last_closed {5} expected {6}"
                .format(self.bucket, server.ip, server.port, stats["vb_0:open_checkpoint_id"],
                    stats["vb_0:last_closed_checkpoint_id"], last_closed_chkpoint, checkpoint))
            if int(last_closed_chkpoint) == checkpoint:
                log.info(
                    "Success! Checkpoint_id: {0} matches expected : {1} for node {2}:{3}".format(
                        last_closed_chkpoint, checkpoint, server.ip,
                        server.port))
                break
            else:
                log.info(
                    "Checkpoint_id: {0} does not match expected : {1} for node {0}:{1} do not match yet. Try again .."
                    .format(last_closed_chkpoint, checkpoint, server.ip, server.port))
                time.sleep(2)
        else:
            self.fail("Unable to get checkpoint stats from the node{0}:{1}".format(server.ip, server.port))

    def _calc_checkpoint_delta(self, new_items):
        return int(new_items / self.chk_size)

