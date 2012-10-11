"""
Executive performance dashboard tests.
"""

# general imports
import time
import json
import os
import gzip
import copy
import threading
import socket
from multiprocessing import Process
from functools import wraps

# membase imports
from membase.helper.cluster_helper import ClusterOperationHelper
from membase.performance.stats import CallbackStatsCollector
from membase.api.rest_client import RestConnection
from remote.remote_util import RemoteMachineShellConnection

# testrunner imports
from TestInput import TestInputSingleton
from perf_defaults import PerfDefaults
from performance import perf
from perf_engines.cbsoda import StoreCouchbase
from perf_engines import mcsoda
from scripts.perf.rel_cri_stats import CBStatsCollector
from cbkarma.rest_client import CbKarmaClient

def multi_buckets(test):
    """
    Enable multiple buckets for perf tests

    - buckets are named as bucket-0, bucket-1,...
    - the whole key space is splitted and distributed to each bucket, w.r.t. bucket-id
    - different buckets are handled by different client processes
    """
    @wraps(test)
    def wrapper(self, *args, **kwargs):

        num_buckets = self.parami('num_buckets', 1)
        if num_buckets <= 1:
            return test(self, *args, **kwargs)

        print "\n[multi_buckets] started"
        buckets = self._get_bucket_names(num_buckets)

        num_clients = self.parami("num_clients", 1)
        self.set_param("num_clients", num_clients * num_buckets)

        prefix = self.parami("prefix", 0)

        procs = []
        for bucket_id in reversed(range(num_buckets)):
            self.set_param("bucket", buckets[bucket_id])
            new_prefix = prefix + bucket_id * num_clients
            self.set_param("prefix", str(new_prefix))
            self.is_leader = new_prefix == 0

            if bucket_id == 0:
                print "[multi_buckets] start test for %s" % buckets[bucket_id]
                test(self, *args, **kwargs)
            else:
                print "[multi_buckets] start test in a new process for %s"\
                      % buckets[bucket_id]
                proc = Process(target=test, args=(self, ))
                proc.daemon = True
                proc.start()
                procs.append(proc)

        print "[multi_buckets] %s finished, waiting for others"\
              % buckets[bucket_id]

        for proc in procs:
            proc.join()

        print "[multi_buckets] stopped"

    return wrapper


def measure_sched_delays(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if self.parami("prefix", -1) or\
            not self.parami("sched_delays", PerfDefaults.sched_delays):
            return func(self, *args, **kwargs)

        file = "%s-%s" % (self.param("spec", ""),
                          time.strftime(PerfDefaults.strftime))

        self.remove_sched_delay_logs()
        self.start_measure_sched_delay(file)
        print "started measuring sched delays"

        ret = func(self, *args, **kwargs)

        self.stop_measure_sched_delay()
        print "stopped measuring sched delays"

        return ret

    return wrapper

class EPerfMaster(perf.PerfBase):

    """
    specURL = http://hub.internal.couchbase.org/confluence/pages/viewpage.action?pageId=1901816

    """

    def setUp(self):
        self.dgm = False
        self.is_master = True
        self.input = TestInputSingleton.input
        self.mem_quota = self.parami("mem_quota",
                                     PerfDefaults.mem_quota)  # for 10G system
        self.level_callbacks = []
        self.latched_rebalance_done = False
        self.is_multi_node = False
        self.is_leader = self.parami("prefix", 0) == 0
        self.superSetUp()

    def superSetUp(self):
        super(EPerfMaster, self).setUp()

    def tearDown(self):
        self.superTearDown()

    def superTearDown(self):
        super(EPerfMaster, self).tearDown()

    def get_all_stats(self):
        """One node, the 'master', should aggregate stats from client and
        server nodes and push results to couchdb.
        Assuming clients ssh_username/password are same as server
        Save servers list as clients
        Replace servers ip with clients ip"""
        clients = self.input.servers
        for i in range(len(clients)):
            clients[i].ip = self.input.clients[i]
        remotepath = '/tmp'

        i = 0
        for client in clients:
            shell = RemoteMachineShellConnection(client)
            filename = '{0}.json'.format(i)
            destination = "{0}/{1}".format(os.getcwd(), filename)
            print "Getting client stats file {0} from {1}".format(filename,
                                                                  client)
            if not shell.get_file(remotepath, filename, destination):
                print "Unable to fetch the json file {0} on Client {1} @ {2}"\
                    .format(remotepath + '/' + filename, i, client.ip)
                exit(1)
            i += 1

        self.aggregate_all_stats(len(clients))

    def merge_dict(self, output, input):
        """
        Merge two dicts. Add values together if key exists in both dicts.
        """
        if output is None or not output:
            return input

        if input is None or not input:
            return output

        for key, value in input.iteritems():
            if key in output.keys():
                output[key] += value
            else:
                output[key] = value

        return output

    def calc_avg_qps(self, ops_array):
        qps = (ops.get('queriesPerSec', 0) for ops in ops_array)
        try:
            # Access phase w/ fg thread
            return sum(qps) / len(ops_array)
        except:
            # Otherwise
            return 0

    def _merge_query_ops(self, base_ops, new_ops):
        """Normalize and merge query ops arrays into single one.

        Arguments:
        base_ops -- first array of ops stats
        new_ops  -- second array of ops stats
        """

        # Ignore empty arrays
        if not base_ops and not new_ops:
            return base_ops

        # Define normalized timeline
        end_time = 0
        start_time = float('inf')
        num_steps = 0
        for ops_array in [base_ops, new_ops]:
            end_time = max(max(ops['endTime'] for ops in ops_array),
                           end_time)
            start_time = min(min(ops['startTime'] for ops in ops_array),
                             start_time)
        num_steps = max(len(base_ops), len(new_ops))
        step = float(end_time - start_time) / num_steps

        # Merge (terrible time complexity)
        merged_ops = list()
        indexes = [0, 0]
        for i in xrange(num_steps):
            # Current time frame
            start_of_step = start_time + step * i
            end_of_step = start_of_step + step
            total_queries = 0
            # Aggregation
            for idx, ops_array in enumerate([base_ops[indexes[0]:],
                                             new_ops[indexes[1]:]]):
                for ops in ops_array:
                    if ops['endTime'] <= end_of_step:
                        total_queries += ops['totalQueries']
                        ops['totalQueries'] = 0
                        indexes[idx] += 1
                    else:
                        break
            qps = total_queries / step
            merged_ops.append({'startTime': start_of_step,
                               'endTime': end_of_step,
                               'totalQueries': total_queries,
                               'queriesPerSec': qps})
        return merged_ops

    def aggregate_all_stats(self, len_clients, type="loop"):
        i = 0

        # read [client #].[type].json.gz file until find one
        file = None
        while i < len_clients:
            try:
                file = gzip.open("{0}.{1}.json.gz".format(i, type), 'rb')
                break
            except IOError:
                print "[stats aggregation error] cannot open file: " + \
                      "{0}.{1}.json.gz".format(i, type)
                i += 1

        if file is None:
            return

        final_json = file.read()
        file.close()
        final_json = json.loads(final_json)
        i += 1
        merge_keys = []
        for latency in final_json.keys():
            if latency.startswith('latency'):
                merge_keys.append(str(latency))

        # Average QPS
        if type == 'loop':
            average_qps = self.calc_avg_qps(final_json['ops'])
            final_json['qps'] = {'average': average_qps}
        else:
            final_json['qps'] = {'average': 0}

        for i in range(i, len_clients):
            try:
                file = gzip.open("{0}.{1}.json.gz".format(i, type), 'rb')
            except IOError:
                # cannot find stats produced by this client, check stats
                # collection. the results might be incomplete.
                print "[stats aggregation error] cannot open file: " +\
                      "{0}.{1}.json.gz".format(i, type)
                continue

            dict = file.read()
            file.close()
            dict = json.loads(dict)
            for key, value in dict.items():
                if key in merge_keys:
                    if key.endswith("histogram"):
                        self.merge_dict(final_json[key], value)
                        continue
                    final_json[key].extend(value)
            if type == 'loop' and self.parami("fg_max_ops", 0):
                final_json['qps']['average'] += self.calc_avg_qps(dict['ops'])
                final_json['ops'] = self._merge_query_ops(final_json['ops'],
                                                          dict['ops'])

        file = gzip.open("{0}.{1}.json.gz".format('final', type), 'wb')
        file.write("{0}".format(json.dumps(final_json)))
        file.close()

    def min_value_size(self, avg=2048):
        # Returns an array of different value sizes so that
        # the average value size is 2k and the ratio of
        # sizes is 33% 1k, 33% 2k, 33% 3k, 1% 10k.
        mvs = []
        for i in range(33):
            mvs.append(avg / 2)
            mvs.append(avg)
            mvs.append(avg * 1.5)
        mvs.append(avg * 5)
        return mvs

    def set_nru_freq(self, freq):
        """Set up NRU access scanner running frequency"""
        for server in self.input.servers:
            shell = RemoteMachineShellConnection(server)
            cmd = "/opt/couchbase/bin/cbepctl localhost:11210 "\
                  "set flush_param alog_sleep_time {0}".format(freq)
            self._exec_and_log(shell, cmd)
            shell.disconnect()

    def set_nru_task(self, tm_hour=0):
        """UTC/GMT time (hour) to schedule NRU access scanner"""
        gmt = time.gmtime()
        if not tm_hour:
            tm_hour = (gmt.tm_hour + 1) % 24

        min_left = 60 - gmt.tm_min
        gmt_now = time.strftime(PerfDefaults.strftime, time.gmtime())
        for server in self.input.servers:
            shell = RemoteMachineShellConnection(server)
            cmd = "/opt/couchbase/bin/cbepctl localhost:11210 "\
                  "set flush_param alog_task_time {0}".format(tm_hour)
            self._exec_and_log(shell, cmd)
            shell.disconnect()
            print "access scanner will be executed on %s in %d minutes, " \
                  "at %d UTC, curr_time is %s" \
                   % (server.ip, min_left, tm_hour, gmt_now)

    def rebuild_alog(self):
        """Rebuild access log"""
        freq = 1
        self.set_nru_freq(freq)
        print "frequency has been set to %s minute" % freq

        # wait for 5 minutes
        time.sleep(300)

        # set back to 24 hrs
        self.set_nru_freq(1440)

    def set_reb_cons_view(self, node, disable=True):
        """Set up consistent view for rebalance task"""
        rest = RestConnection(node)
        rest.set_reb_cons_view(disable=disable)

    def start_measure_sched_delay(self, file="sched-delay"):
        for server in self.input.servers:
            shell = RemoteMachineShellConnection(server)
            cmd = "nohup collect-sched-delays.rb %s > /dev/null 2>&1 &" % file
            self._exec_and_log(shell, cmd)
            shell.disconnect()

    def stop_measure_sched_delay(self):
        for server in self.input.servers:
            shell = RemoteMachineShellConnection(server)
            cmd = "killall -9 -r .*measure-sched-delays"
            self._exec_and_log(shell, cmd)
            shell.disconnect()

    def remove_sched_delay_logs(self):
        for server in self.input.servers:
            shell = RemoteMachineShellConnection(server)
            cmd = "rm -rf *.cpu*"
            self._exec_and_log(shell, cmd)
            shell.disconnect()

    # Gets the vbucket count
    def gated_start(self, clients):
        """
        @deprecate
        Remove when we delete test functions.
        No useful? we are calling setupBased1() on SetUp() in perf.py
        """
        if not self.is_master:
            self.setUpBase1()

    def wait_for_xdc_replication(self):
        rest = RestConnection(self.input.servers[0])
        bucket = self.param('bucket', 'default')
        while rest.get_xdc_queue_size(bucket) > 0:
            print "Waiting for XDC replication to finish"
            time.sleep(30)

    def _dashboard(phase):
        def _outer(function):
            def _inner(self, *args, **kargs):
                if self.input.dashboard:
                    # Create new rest client if it doesn't exist
                    if not hasattr(self, 'cbkarma_client'):
                        hostname, port = self.input.dashboard[0].split(':')
                        self.cbkarma_client = CbKarmaClient(hostname, port)

                    # Define new test id if it doesn't exist
                    if not hasattr(self, 'test_id'):
                        self.test_id = self.param("test_id",
                                                  self.cbkarma_client.init()[-1])

                    # Metadata
                    build = self.rest.get_pools_info()['implementationVersion']
                    build = build.replace('-enterprise', '')
                    build = build.replace('-community', '')
                    spec = self.param('spec', 'unknown')
                    ini_filename = self.param('ini', 'unknown')
                    ini = os.path.basename(ini_filename).split('.')[0]
                    if self.parami('hot_load_phase', 0):
                        prefix = 'hot_'
                    else:
                        prefix = ''
                    client_phase = prefix + phase + '-' + \
                        str(self.parami("prefix", 0))

                    # Change status to 'started'
                    if self.parami(phase + "_phase", 0):
                        self.cbkarma_client.update(self.test_id, build=build,
                                                   spec=spec, ini=ini,
                                                   phase=client_phase,
                                                   status='started')

                    # Execute current phase
                    result = function(self, *args, **kargs)

                    # Change status to 'done'
                    if self.parami(phase + "_phase", 0):
                        self.cbkarma_client.update(self.test_id, build=build,
                                                   spec=spec, ini=ini,
                                                   phase=client_phase,
                                                   status='done')

                    # Sleep a second in order to avoid timestamp conflict
                    time.sleep(1)
                else:
                    result = function(self, *args, **kargs)

                return result
            return _inner
        return _outer

    @_dashboard(phase='load')
    def load_phase(self, num_nodes):

        if self.parami("hot_load_phase", 0) == 1:
            num_items = self.parami("hot_init_items", PerfDefaults.items)
            self.rebuild_alog()
        else:
            num_items = self.parami("items", PerfDefaults.items)

        # Cluster nodes if master
        if self.is_master:
            self.rebalance_nodes(num_nodes)

        if self.parami("load_phase", 1) > 0:
            print "Loading"

            nru_freq = self.parami('nru_freq', PerfDefaults.nru_freq)
            if nru_freq != PerfDefaults.nru_freq \
                and self.is_leader:
                self.set_nru_freq(nru_freq)

            num_clients = self.parami("num_clients",
                                      len(self.input.clients) or 1)
            start_at = self.load_phase_clients_start_at(num_items, num_clients)
            items = self.parami("num_items", num_items) / num_clients
            self.is_multi_node = False
            mvs = self.min_value_size(self.parami("avg_value_size",
                                                  PerfDefaults.avg_value_size))
            if 'xperf' in self.param('conf_file', '') and 'bi' in self.id():
                prefix = self.params('cluster_prefix', '')
            else:
                prefix= ''

            self.load(items,
                      self.param('size', mvs),
                      kind=self.param('kind', 'json'),
                      protocol=self.mk_protocol(host=self.input.servers[0].ip,
                                                port=self.input.servers[0].port),
                      use_direct=self.parami('use_direct', 1),
                      doc_cache=self.parami('doc_cache', 0),
                      prefix=prefix,
                      start_at=start_at,
                      is_eperf=True)
            self.restartProxy()

            if self.parami('wait_for_xdc_replication', 0):
                self.wait_for_xdc_replication()

    def load_phase_clients_start_at(self, num_items, num_clients):
        """
        - normal load-phase or hot-load-phase with only one bucket:
            keep the normal multi-client start_at calculation

        - hot-load-phase with multiple buckets:
            - shift an 'offset' distance to match the key space in the load phase
            - apply the normal multi-client calculation
        """
        num_buckets = self.parami('num_buckets', 1)
        if self.parami("hot_load_phase", 0) == 0 or num_buckets <= 1:
            return int(self.paramf("start_at", 1.0) *
                       self.parami("prefix", 0) * num_items / num_clients)

        # multi-bucket, hot-load-phase
        num_loaded_items = self.parami("items", PerfDefaults.items)
        cpb = num_clients / num_buckets
        offset = num_loaded_items / num_buckets * \
                 (self.parami("prefix", 0) / cpb)
        return int(offset + self.paramf("start_at", 1.0) *
                   self.parami("prefix", 0) % cpb * num_items / num_clients)

    def access_phase_clients_start_at(self):
        self.access_phase_items = self.parami("items", PerfDefaults.items)
        num_clients = self.parami("num_clients", len(self.input.clients) or 1)
        start_at = int(self.paramf("start_at", 1.0) *
                       self.parami("prefix", 0) * self.access_phase_items /
                       num_clients)
        return num_clients, start_at

    @_dashboard(phase='access')
    @measure_sched_delays
    def access_phase(self,
                     ratio_sets=0,
                     ratio_misses=0,
                     ratio_creates=0,
                     ratio_deletes=0,
                     ratio_hot=0,
                     ratio_hot_gets=0,
                     ratio_hot_sets=0,
                     ratio_expirations=0,
                     max_creates=0,
                     hot_shift=10,
                     ratio_queries=0,
                     queries=None,
                     proto_prefix="membase-binary",
                     host=None,
                     port=None,
                     ddoc=None):
        if self.parami("access_phase", 1) > 0:
            print "Accessing"
            items = self.parami("items", PerfDefaults.items)
            num_clients, start_at = self.access_phase_clients_start_at()
            start_delay = self.parami("start_delay", PerfDefaults.start_delay)
            if start_delay > 0:
                time.sleep(start_delay * self.parami("prefix", 0))
            max_creates = self.parami("max_creates", max_creates) / num_clients
            self.is_multi_node = False
            if host is None:
                host = self.input.servers[0].ip
            if port is None:
                port = self.input.servers[0].port
            mvs = self.min_value_size(self.parami("avg_value_size",
                                      PerfDefaults.avg_value_size))

            if self.parami('nru_task', PerfDefaults.nru_task) \
                and self.is_leader:
                self.set_nru_task()
                self.nru_monitor = NRUMonitor(
                    self.parami("nru_polling_freq", PerfDefaults.nru_polling_freq),
                    self.parami("nru_reb_delay", PerfDefaults.nru_reb_delay),
                    self)
                self.nru_monitor.daemon = True
                self.nru_monitor.start()

            if 'xperf' in self.param('conf_file', '') and 'bi' in self.id():
                prefix = self.params('cluster_prefix', '')
            else:
                prefix= ''

            self.loop(num_ops=0,
                      num_items=items,
                      max_items=items + max_creates + 1,
                      max_creates=max_creates,
                      min_value_size=self.param('size', mvs),
                      kind=self.param('kind', 'json'),
                      protocol=self.mk_protocol(host, port, proto_prefix),
                      clients=self.parami('clients', 1),
                      ratio_sets=ratio_sets,
                      ratio_misses=ratio_misses,
                      ratio_creates=ratio_creates,
                      ratio_deletes=ratio_deletes,
                      ratio_hot=ratio_hot,
                      ratio_hot_gets=ratio_hot_gets,
                      ratio_hot_sets=ratio_hot_sets,
                      ratio_expirations=ratio_expirations,
                      expiration=self.parami('expiration', 60 * 5),  # 5 minutes.
                      test_name=self.id(),
                      use_direct=self.parami('use_direct', 1),
                      doc_cache=self.parami('doc_cache', 0),
                      prefix=prefix,
                      collect_server_stats=self.is_leader,
                      start_at=start_at,
                      report=self.parami('report', int(max_creates * 0.1)),
                      exit_after_creates=self.parami('exit_after_creates', 1),
                      hot_shift=self.parami('hot_shift', hot_shift),
                      is_eperf=True,
                      ratio_queries=ratio_queries,
                      queries=queries,
                      ddoc=ddoc)

    def clear_hot_keys(self):
        """
        callback to clear hot keys from the stack
        """
        stack = self.cur.get('hot-stack', None)
        if not stack:
            print "unable to clear hot stack : stack does not exist"
            return False

        stack.clear()
        return True

    # restart the cluster and wait for it to warm up
    @_dashboard(phase='warmup')
    def warmup_phase(self):
        if not self.is_leader:
            return

        server = self.input.servers[0]
        client_id = self.parami("prefix", 0)
        test_params = {'test_time': time.time(),
                       'test_name': self.id(),
                       'json': 0}

        sc = self.start_stats(self.spec_reference + ".warmup",
                              test_params=test_params, client_id=client_id)

        start_time = time.time()

        self.warmup(collect_stats=False, flush_os_cache=True)
        sc.capture_mb_snapshot(server)

        self.warmup(collect_stats=False, flush_os_cache=False)
        sc.capture_mb_snapshot(server)

        end_time = time.time()

        ops = {'tot-sets': 0,
               'tot-gets': 0,
               'tot-items': 0,
               'tot-creates': 0,
               'tot-misses': 0,
               "start-time": start_time,
               "end-time": end_time}

        self.end_stats(sc, ops, self.spec_reference + ".warmup")

    @_dashboard(phase='index')
    def index_phase(self, ddocs):
        """Create design documents and views"""

        if self.parami("index_phase", 1) > 0:
            # Start stats collector
            if self.parami("collect_stats", 1):
                client_id = self.parami("prefix", 0)
                test_params = {'test_time': time.time(),
                               'test_name': self.id(),
                               'json': 0}

                sc = self.start_stats(self.spec_reference + ".index",
                                      test_params=test_params,
                                      client_id=client_id)

                start_time = time.time()

            # Create design documents and views
            master = self.input.servers[0]
            self.set_up_rest(master)

            bucket = self.param('bucket', 'default')

            for ddoc_name, d in ddocs.items():
                d = copy.copy(d)
                d["language"] = "javascript"
                d_json = json.dumps(d)
                api = "{0}{1}/_design/{2}".format(self.rest.capiBaseUrl,
                                                  bucket, ddoc_name)
                self.rest._http_request(api, 'PUT', d_json,
                                        headers=self.rest._create_capi_headers())

            # Initialize indexing
            for ddoc_name, d in ddocs.items():
                for view_name, x in d["views"].items():
                    self.rest.query_view(ddoc_name, view_name, bucket,
                                         {"limit": 10})

            # Wait until there are no active indexing tasks
            if self.parami('wait_for_indexer', 1):
                ClusterOperationHelper.wait_for_completion(self.rest,
                                                           'indexer')

            # Wait until there are no active view compaction tasks
            if self.parami('wait_for_compaction', 1):
                ClusterOperationHelper.wait_for_completion(self.rest,
                                                           'view_compaction')

            # Stop stats collector
            if self.parami("collect_stats", 1):
                end_time = time.time()

                ops = {'start-time': start_time,
                       'end-time': end_time}

                self.end_stats(sc, ops, self.spec_reference + ".index")

    @_dashboard(phase='debug')
    def debug_phase(self, ddocs=None):
        """Run post-access phase debugging tasks"""

        if ddocs:
            # Query with debug argument equals true
            bucket = self.param('bucket', 'default')
            ddoc_name = ddocs.keys()[0]
            view_name = ddocs[ddoc_name]['views'].keys()[0]

            response = self.rest.query_view(ddoc_name, view_name, bucket,
                                            {'debug': 'true'})
            debug_info = response['debug_info']

            for subset, data in debug_info.iteritems():
                self.log.info(subset)
                self.log.info(data)

    def latched_rebalance(self, cur=None, delay=0.01, sync=False):
        if not self.latched_rebalance_done:
            self.latched_rebalance_done = True
            self.delayed_rebalance(self.parami("num_nodes_after",
                                               PerfDefaults.num_nodes_after),
                                   delay,
                                   self.parami("reb_max_retries",
                                               PerfDefaults.reb_max_retries),
                                   sync)

    # ---------------------------------------------

    @multi_buckets
    def test_eperf_read(self):
        """
        Eperf read test, using parameters from conf/*.conf file.
        """
        self.spec("test_eperf_read")

        self.gated_start(self.input.clients)
        self.load_phase(self.parami("num_nodes", PerfDefaults.num_nodes))
        if self.parami("access_phase", 1) == 1:
            self.access_phase(ratio_sets=self.paramf('ratio_sets',
                                                     PerfDefaults.ratio_sets),
                              ratio_misses=self.paramf('ratio_misses',
                                                       PerfDefaults.ratio_misses),
                              ratio_creates=self.paramf('ratio_creates',
                                                        PerfDefaults.ratio_creates),
                              ratio_deletes=self.paramf('ratio_deletes',
                                                        PerfDefaults.ratio_deletes),
                              ratio_hot=self.paramf('ratio_hot',
                                                    PerfDefaults.ratio_hot),
                              ratio_hot_gets=self.paramf('ratio_hot_gets',
                                                         PerfDefaults.ratio_hot_gets),
                              ratio_hot_sets=self.paramf('ratio_hot_sets',
                                                         PerfDefaults.ratio_hot_sets),
                              ratio_expirations=self.paramf('ratio_expirations',
                                                            PerfDefaults.ratio_expirations),
                              max_creates=self.parami("max_creates",
                                                      PerfDefaults.max_creates))

    @multi_buckets
    def test_eperf_write(self):
        """
        Eperf write test, using parameters from conf/*.conf file.
        """
        self.spec("test_eperf_write")

        self.gated_start(self.input.clients)
        self.load_phase(self.parami("num_nodes", PerfDefaults.num_nodes))
        if self.parami("access_phase", 1) == 1:
            self.access_phase(ratio_sets=self.paramf('ratio_sets',
                                                     PerfDefaults.ratio_sets),
                              ratio_misses=self.paramf('ratio_misses',
                                                       PerfDefaults.ratio_misses),
                              ratio_creates=self.paramf('ratio_creates',
                                                        PerfDefaults.ratio_creates),
                              ratio_deletes=self.paramf('ratio_deletes',
                                                        PerfDefaults.ratio_deletes),
                              ratio_hot=self.paramf('ratio_hot',
                                                    PerfDefaults.ratio_hot),
                              ratio_hot_gets=self.paramf('ratio_hot_gets',
                                                         PerfDefaults.ratio_hot_gets),
                              ratio_hot_sets=self.paramf('ratio_hot_sets',
                                                         PerfDefaults.ratio_hot_sets),
                              ratio_expirations=self.paramf('ratio_expirations',
                                                            PerfDefaults.ratio_expirations),
                              max_creates=self.parami("max_creates",
                                                      PerfDefaults.max_creates))

    @multi_buckets
    def test_eperf_mixed(self, save_snapshot=False):
        """
        Eperf mixed test, using parameters from conf/*.conf file.
        """
        self.spec("test_eperf_mixed")

        self.gated_start(self.input.clients)
        self.load_phase(self.parami("num_nodes", PerfDefaults.num_nodes))

        if self.parami("index_phase", 0) and self.param("woq_pattern", 0):
            view_gen = ViewGen()
            ddocs = view_gen.generate_ddocs([1])
            self.index_phase(ddocs)

        if self.parami("access_phase", 1) == 1:

            if self.parami("cb_stats", PerfDefaults.cb_stats) == 1:
                # starts cbstats collection
                cbStatsCollector = CBStatsCollector()
                cb_exc = self.param("cb_stats_exc", PerfDefaults.cb_stats_exc)
                frequency = self.parami("cb_stats_freq",
                                        PerfDefaults.cb_stats_freq)
                cbStatsCollector.collect_cb_stats(servers=self.input.servers,
                                                  cb_exc=cb_exc,
                                                  frequency=frequency)

            self.access_phase(ratio_sets=self.paramf('ratio_sets',
                                                     PerfDefaults.ratio_sets),
                              ratio_misses=self.paramf('ratio_misses',
                                                       PerfDefaults.ratio_misses),
                              ratio_creates=self.paramf('ratio_creates',
                                                        PerfDefaults.ratio_creates),
                              ratio_deletes=self.paramf('ratio_deletes',
                                                        PerfDefaults.ratio_deletes),
                              ratio_hot=self.paramf('ratio_hot',
                                                    PerfDefaults.ratio_hot),
                              ratio_hot_gets=self.paramf('ratio_hot_gets',
                                                         PerfDefaults.ratio_hot_gets),
                              ratio_hot_sets=self.paramf('ratio_hot_sets',
                                                         PerfDefaults.ratio_hot_sets),
                              ratio_expirations=self.paramf('ratio_expirations',
                                                            PerfDefaults.ratio_expirations),
                              max_creates=self.parami("max_creates",
                                                      PerfDefaults.max_creates))

            if self.parami("cb_stats", PerfDefaults.cb_stats) == 1:
                cbStatsCollector.stop()

        if self.parami("warmup", PerfDefaults.warmup) == 1:
            self.warmup_phase()

        if save_snapshot:
            self.save_snapshots(self.param("snapshot_filename", ""),
                                self.param("bucket", PerfDefaults.bucket))

    def test_eperf_warmup(self):
        """
        Eperf warmup test, using parameters from conf/*.conf file.
        """
        self.spec("test_eperf_warmup")
        file_base = self.param("snapshot_file_base", "")
        bucket = self.param("bucket", PerfDefaults.bucket)

        if self.load_snapshots(file_base, bucket) \
                and self.parami("warmup", PerfDefaults.warmup):
            self.gated_start(self.input.clients)
            print "[test_eperf_warmup] loaded snapshot, start warmup phase"
            self.warmup_phase()
            return

        print "[test_eperf_warmup] unable to find snapshot file, rerun the test"
        self.test_eperf_mixed(save_snapshot=True)

    @multi_buckets
    def test_eperf_rebalance(self):
        """
        Eperf rebalance test, using parameters from conf/*.conf file.
        """
        self.spec("test_eperf_rebalance")

        self.gated_start(self.input.clients)
        self.load_phase(self.parami("num_nodes", PerfDefaults.num_nodes))
        num_clients = self.parami("num_clients", len(self.input.clients) or 1)

        if not self.parami("nru_task", PerfDefaults.nru_task) and \
            not self.parami("reb_no_fg", PerfDefaults.reb_no_fg):
            rebalance_after = self.parami("rebalance_after",
                                      PerfDefaults.rebalance_after)
            self.level_callbacks = [('cur-creates', rebalance_after / num_clients,
                                getattr(self, "latched_rebalance"))]

        if self.parami("reb_cons_view", PerfDefaults.reb_cons_view):
            self.set_reb_cons_view(self.input.servers[0], disable=False)

        if self.parami("access_phase", 1) == 1:
            if self.parami("cb_stats", PerfDefaults.cb_stats) == 1:
                # starts cbstats collection
                cbStatsCollector = CBStatsCollector()
                cb_exc = self.param("cb_stats_exc", PerfDefaults.cb_stats_exc)
                frequency = self.parami("cb_stats_freq",
                                        PerfDefaults.cb_stats_freq)
                cbStatsCollector.collect_cb_stats(servers=self.input.servers,
                                                  cb_exc=cb_exc,
                                                  frequency=frequency)

            if self.parami("reb_no_fg", PerfDefaults.reb_no_fg):
                max_creates = 1
            else:
                max_creates= \
                    self.parami("max_creates", PerfDefaults.max_creates)

            self.access_phase(ratio_sets=self.paramf('ratio_sets',
                                                     PerfDefaults.ratio_sets),
                              ratio_misses=self.paramf('ratio_misses',
                                                       PerfDefaults.ratio_misses),
                              ratio_creates=self.paramf('ratio_creates',
                                                        PerfDefaults.ratio_creates),
                              ratio_deletes=self.paramf('ratio_deletes',
                                                        PerfDefaults.ratio_deletes),
                              ratio_hot=self.paramf('ratio_hot',
                                                    PerfDefaults.ratio_hot),
                              ratio_hot_gets=self.paramf('ratio_hot_gets',
                                                         PerfDefaults.ratio_hot_gets),
                              ratio_hot_sets=self.paramf('ratio_hot_sets',
                                                         PerfDefaults.ratio_hot_sets),
                              ratio_expirations=self.paramf('ratio_expirations',
                                                            PerfDefaults.ratio_expirations),
                              max_creates=max_creates,
                              proto_prefix="memcached-binary",
                              port="11211")

            if self.parami("reb_no_fg", PerfDefaults.reb_no_fg):

                start_time = time.time()

                if not self.parami("prefix", 0):
                    self.latched_rebalance(delay=0, sync=True)

                if self.parami("loop_wait_until_drained",
                               PerfDefaults.loop_wait_until_drained):
                    self.wait_until_drained()

                if self.parami("loop_wait_until_repl",
                               PerfDefaults.loop_wait_until_repl):
                    self.wait_until_repl()

                if self.parami("collect_stats", 1):
                    ops = {"tot-sets": max_creates,
                           "tot-gets": 0,
                           "tot-items": 0,
                           "tot-creates": max_creates,
                           "tot-misses": 0,
                           "start-time": start_time,
                           "end-time": time.time()}

                    self.end_stats(self.sc, ops, self.spec_reference + ".loop")

            if self.parami("cb_stats", PerfDefaults.cb_stats) == 1:
                cbStatsCollector.stop()

    def test_eperf_thruput(self):
        """test front-end throughput"""
        self.spec("test_eperf_thruput")

        self.gated_start(self.input.clients)
        self.load_phase(self.parami("num_nodes", PerfDefaults.num_nodes))

        if self.parami("access_phase", 1) == 1:
            self.access_phase(ratio_sets=self.paramf('ratio_sets',
                                                     PerfDefaults.ratio_sets),
                              ratio_misses=self.paramf('ratio_misses',
                                                       PerfDefaults.ratio_misses),
                              ratio_creates=self.paramf('ratio_creates',
                                                        PerfDefaults.ratio_creates),
                              ratio_deletes=self.paramf('ratio_deletes',
                                                        PerfDefaults.ratio_deletes),
                              ratio_hot=self.paramf('ratio_hot',
                                                    PerfDefaults.ratio_hot),
                              ratio_hot_gets=self.paramf('ratio_hot_gets',
                                                         PerfDefaults.ratio_hot_gets),
                              ratio_hot_sets=self.paramf('ratio_hot_sets',
                                                         PerfDefaults.ratio_hot_sets),
                              ratio_expirations=self.paramf('ratio_expirations',
                                                            PerfDefaults.ratio_expirations),
                              max_creates=self.parami("max_creates",
                                                      PerfDefaults.max_creates))

    def test_evperf_workload2(self):
        self.spec("evperf_workload2")

        # Load phase
        num_nodes = self.parami('num_nodes', PerfDefaults.num_nodes)
        self.load_phase(num_nodes)

        # Index phase
        view_gen = ViewGen()
        ddocs = view_gen.generate_ddocs([2, 2, 4])
        self.index_phase(ddocs)

        # Access phase
        limit = self.parami('limit', 10)
        query_suffix = self.param("query_suffix", "")
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, ddocs, bucket,
                                            use_all_docs=True)

        self.bg_max_ops_per_sec = self.parami("bg_max_ops_per_sec", 100)
        self.fg_max_ops = self.parami("fg_max_ops", 1000000)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.3),
                          ratio_misses=self.paramf('ratio_misses', 0.05),
                          ratio_creates=self.paramf('ratio_creates', 0.33),
                          ratio_deletes=self.paramf('ratio_deletes', 0.25),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=self.parami("max_creates", 30000000),
                          ratio_queries=self.paramf('ratio_queries', 0.3571),
                          queries=queries,
                          proto_prefix="couchbase",
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

    def test_evperf_workload3(self):
        """Like workload 2 but has queries on view with reduce step"""

        self.spec('evperf_workload3')

        # Load phase
        num_nodes = self.parami('num_nodes', PerfDefaults.num_nodes)
        self.load_phase(num_nodes)

        # Index phase
        view_gen = ViewGen()
        ddocs = view_gen.generate_ddocs([2, 2, 4], add_reduce=True)
        self.index_phase(ddocs)

        # Access phase
        limit = self.parami('limit', 10)
        query_suffix = self.param('query_suffix', '')
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, ddocs, bucket,
                                            use_all_docs=True,
                                            use_reduce=True)

        self.bg_max_ops_per_sec = self.parami('bg_max_ops_per_sec', 100)
        self.fg_max_ops = self.parami('fg_max_ops', 1000000)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.3),
                          ratio_misses=self.paramf('ratio_misses', 0.05),
                          ratio_creates=self.paramf('ratio_creates', 0.33),
                          ratio_deletes=self.paramf('ratio_deletes', 0.25),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=self.parami('max_creates', 30000000),
                          ratio_queries=self.paramf('ratio_queries', 0.3571),
                          queries=queries,
                          proto_prefix='couchbase',
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

    def test_vperf1(self):
        """1 design document, 1 view"""

        self.spec("vperf1")

        self.gated_start(self.input.clients)

        # Load phase
        num_nodes = self.parami('num_nodes', PerfDefaults.num_nodes)
        self.load_phase(num_nodes)

        # Index phase
        view_gen = ViewGen()
        ddocs = {'A': {'views': {'city': {'map': view_gen.MAP_FUNCTIONS[0]}}}}
        self.index_phase(ddocs)

        # Access phase
        q = {'city': '/default/_design/A/_view/city?limit=20&startkey="{city}"'}

        queries_by_kind = [[q['city']]]
        remaining = [1]
        query_suffix = self.param("query_suffix", "")
        queries = view_gen.compute_queries(queries_by_kind, remaining,
                                           query_suffix)
        queries = view_gen.join_queries(queries)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets',
                                                 PerfDefaults.ratio_sets),
                          ratio_misses=self.paramf('ratio_misses',
                                                   PerfDefaults.ratio_misses),
                          ratio_creates=self.paramf('ratio_creates',
                                                    PerfDefaults.ratio_creates),
                          ratio_deletes=self.paramf('ratio_deletes',
                                                    PerfDefaults.ratio_deletes),
                          ratio_hot=self.paramf('ratio_hot',
                                                PerfDefaults.ratio_hot),
                          ratio_hot_gets=self.paramf('ratio_hot_gets',
                                                     PerfDefaults.ratio_hot_gets),
                          ratio_hot_sets=self.paramf('ratio_hot_sets',
                                                     PerfDefaults.ratio_hot_sets),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        PerfDefaults.ratio_expirations),
                          max_creates=self.parami("max_creates",
                                                  PerfDefaults.max_creates),
                          queries=queries,
                          proto_prefix="couchbase",
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

        if self.parami("debug_phase", 0):
            self.debug_phase(ddocs)

    def test_vperf2(self):
        """1 design document, 8 views"""

        self.spec("vperf2")

        self.gated_start(self.input.clients)

        # Load phase
        self.load_phase(self.parami('num_nodes', 10))

        # Index phase
        view_gen = ViewGen()
        ddocs = view_gen.generate_ddocs([8])
        self.index_phase(ddocs)

        # Access phase
        limit = self.parami('limit', 10)
        query_suffix = self.param("query_suffix", "")
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, ddocs, bucket,
                                            use_all_docs=False)

        self.bg_max_ops_per_sec = self.parami('bg_max_ops_per_sec', 100)
        self.fg_max_ops = self.parami('fg_max_ops', 1000000)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        if self.parami('num_nodes_before', 0):
            server_sn = self.parami('prefix', 0) % \
                self.parami('num_nodes_before', 0)
        else:
            server_sn = self.parami('prefix', 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.3),
                          ratio_misses=self.paramf('ratio_misses', 0.05),
                          ratio_creates=self.paramf('ratio_creates', 0.33),
                          ratio_deletes=self.paramf('ratio_deletes', 0.25),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=self.parami("max_creates", 30000000),
                          ratio_queries=self.paramf('ratio_queries', 0.3571),
                          queries=queries,
                          proto_prefix="couchbase",
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

        if self.parami("debug_phase", 0):
            self.debug_phase(ddocs)

    def test_vperf3(self):
        """8 design documents, 1 view"""

        self.spec("vperf3")

        self.gated_start(self.input.clients)

        # Load phase
        self.load_phase(self.parami('num_nodes', 10))

        # Index phase
        view_gen = ViewGen()
        ddocs = view_gen.generate_ddocs([1, 1, 1, 1, 1, 1, 1, 1])
        self.index_phase(ddocs)

        # Access phase
        limit = self.parami('limit', 10)
        query_suffix = self.param("query_suffix", "")
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, ddocs, bucket,
                                            use_all_docs=False)

        self.bg_max_ops_per_sec = self.parami('bg_max_ops_per_sec', 100)
        self.fg_max_ops = self.parami('fg_max_ops', 1000000)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.3),
                          ratio_misses=self.paramf('ratio_misses', 0.05),
                          ratio_creates=self.paramf('ratio_creates', 0.33),
                          ratio_deletes=self.paramf('ratio_deletes', 0.25),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=self.parami("max_creates", 30000000),
                          ratio_queries=self.paramf('ratio_queries', 0.3571),
                          queries=queries,
                          proto_prefix="couchbase",
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

        if self.parami("debug_phase", 0):
            self.debug_phase(ddocs)

    def test_vperf4(self):
        """Like workload 2 but without queries on primary index"""

        self.spec("vperf4")

        self.gated_start(self.input.clients)

        # Load phase
        self.load_phase(self.parami('num_nodes', 10))

        # Index phase
        view_gen = ViewGen()
        ddocs = view_gen.generate_ddocs([2, 2, 4])
        self.index_phase(ddocs)

        # Access phase
        limit = self.parami('limit', 10)
        query_suffix = self.param("query_suffix", "")
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, ddocs, bucket,
                                            use_all_docs=False,
                                            use_reduce=False)

        self.bg_max_ops_per_sec = self.parami('bg_max_ops_per_sec', 100)
        self.fg_max_ops = self.parami('fg_max_ops', 1000000)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.3),
                          ratio_misses=self.paramf('ratio_misses', 0.05),
                          ratio_creates=self.paramf('ratio_creates', 0.33),
                          ratio_deletes=self.paramf('ratio_deletes', 0.25),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=self.parami("max_creates", 30000000),
                          ratio_queries=self.paramf('ratio_queries', 0.3571),
                          queries=queries,
                          proto_prefix="couchbase",
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

        if self.parami("debug_phase", 0):
            self.debug_phase(ddocs)

    def test_vperf5(self):
        """Only queries on primary index"""

        self.spec("vperf5")

        self.gated_start(self.input.clients)

        # Load phase
        self.load_phase(self.parami('num_nodes', 10))

        # Index phase
        pass

        # Access phase
        limit = self.parami('limit', 10)
        query_suffix = self.param("query_suffix", "")
        view_gen = ViewGen()
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, bucket,
                                            ddocs=None)

        self.bg_max_ops_per_sec = self.parami('bg_max_ops_per_sec', 100)
        self.fg_max_ops = self.parami('fg_max_ops', 1000000)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.3),
                          ratio_misses=self.paramf('ratio_misses', 0.05),
                          ratio_creates=self.paramf('ratio_creates', 0.33),
                          ratio_deletes=self.paramf('ratio_deletes', 0.25),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=self.parami("max_creates", 30000000),
                          ratio_queries=self.paramf('ratio_queries', 0.3571),
                          queries=queries,
                          proto_prefix="couchbase",
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

        if self.parami("debug_phase", 0):
            self.debug_phase(ddocs)

    def test_vperf6(self):
        """Only queries on pseudo primary index"""

        self.spec("vperf6")

        self.gated_start(self.input.clients)

        # Load phase
        self.load_phase(self.parami('num_nodes', 10))

        # Index phase
        view_gen = ViewGen()
        ddocs = view_gen.generate_all_docs_view()
        self.index_phase(ddocs)

        # Access phase
        limit = self.parami('limit', 10)
        query_suffix = self.param("query_suffix", "")
        view_gen = ViewGen()
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, ddocs, bucket,
                                            pseudo=True)

        self.bg_max_ops_per_sec = self.parami('bg_max_ops_per_sec', 100)
        self.fg_max_ops = self.parami('fg_max_ops', 1000000)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.3),
                          ratio_misses=self.paramf('ratio_misses', 0.05),
                          ratio_creates=self.paramf('ratio_creates', 0.33),
                          ratio_deletes=self.paramf('ratio_deletes', 0.25),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=self.parami("max_creates", 30000000),
                          ratio_queries=self.paramf('ratio_queries', 0.3571),
                          queries=queries,
                          proto_prefix="couchbase",
                          host=host,
                          ddoc=view_gen.DDOC_NAMES[0])

        if self.parami("debug_phase", 0):
            self.debug_phase(ddocs)

    def test_evperf2(self):
        """3 design documents, 8 views per design document"""

        self.spec("evperf2")

        self.gated_start(self.input.clients)
        num_nodes = self.parami('num_nodes', PerfDefaults.num_nodes)

        # Load phase
        if self.parami("load_phase", 0):
            self.load_phase(num_nodes)

        # Index phase
        view_gen = ViewGen()
        ddocs = view_gen.generate_ddocs([8, 8, 8])
        if self.parami("index_phase", 0):
            self.index_phase(ddocs)

        # Access phase
        limit = self.parami('limit', PerfDefaults.limit)
        query_suffix = self.param("query_suffix", "")
        bucket = self.params('bucket', 'default')
        queries = view_gen.generate_queries(limit, query_suffix, ddocs, bucket,
                                            use_all_docs=False, extend=True)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        if self.parami("access_phase", 0):
            self.access_phase(ratio_sets=self.paramf('ratio_sets',
                                                     PerfDefaults.ratio_sets),
                              ratio_misses=self.paramf('ratio_misses',
                                                       PerfDefaults.ratio_misses),
                              ratio_creates=self.paramf('ratio_creates',
                                                        PerfDefaults.ratio_creates),
                              ratio_deletes=self.paramf('ratio_deletes',
                                                        PerfDefaults.ratio_deletes),
                              ratio_hot=self.paramf('ratio_hot',
                                                    PerfDefaults.ratio_hot),
                              ratio_hot_gets=self.paramf('ratio_hot_gets',
                                                         PerfDefaults.ratio_hot_gets),
                              ratio_hot_sets=self.paramf('ratio_hot_sets',
                                                         PerfDefaults.ratio_hot_sets),
                              ratio_expirations=self.paramf('ratio_expirations',
                                                            PerfDefaults.ratio_expirations),
                              max_creates=self.parami("max_creates",
                                                      PerfDefaults.max_creates),
                              queries=queries,
                              proto_prefix="couchbase",
                              host=host,
                              ddoc=view_gen.DDOC_NAMES[0])

        if self.parami("debug_phase", 0):
            self.debug_phase(ddocs)

    def test_stats_collector(self):
        """Fake test for stats collection"""

        test_params = {'test_time': time.time(),
                       'test_name': self.id(),
                       'json': 0}

        sc = self.start_stats('loop',
                              test_params=test_params,
                              client_id=0)

        start_time = time.time()

        try:
            time.sleep(self.parami('sleep_time', 3600))
        except KeyboardInterrupt:
            print "Stats collection was interrupted"

        end_time = time.time()

        ops = {'start-time': start_time,
               'end-time': end_time}

        self.end_stats(sc, ops, 'loop')

    def test_minimal(self):
        """Minimal performance test which covers load and access phases"""

        self.spec("test-minimal")

        self.rest.create_bucket(bucket='default', ramQuotaMB=128,
                                bucketType='couchbase')

        self.gated_start(self.input.clients)

        # Load phase
        self.load_phase(self.parami('num_nodes', 10))

        # Access phase
        max_creates = self.parami('max_creates', 100)

        # Rotate host so multiple clients don't hit the same HTTP/REST server.
        server_sn = self.parami("prefix", 0) % len(self.input.servers)
        host = self.input.servers[server_sn].ip

        self.access_phase(ratio_sets=self.paramf('ratio_sets', 0.5),
                          ratio_misses=self.paramf('ratio_misses', 0.2),
                          ratio_creates=self.paramf('ratio_creates', 0.5),
                          ratio_deletes=self.paramf('ratio_deletes', 0.5),
                          ratio_hot=self.paramf('ratio_hot', 0.2),
                          ratio_hot_gets=self.paramf('ratio_hot_gets', 0.95),
                          ratio_hot_sets=self.paramf('ratio_hot_sets', 0.95),
                          ratio_expirations=self.paramf('ratio_expirations',
                                                        0.03),
                          max_creates=max_creates,
                          host=host)

        self.rest.delete_bucket(bucket='default')


class EPerfClient(EPerfMaster):

    def setUp(self):
        self.dgm = False
        self.is_master = False
        self.level_callbacks = []
        self.latched_rebalance_done = False
        self.setUpBase0()
        self.is_leader = self.parami("prefix", 0) == 0
        self.bg_max_ops_per_sec = 0
        self.fg_max_ops = 0
        self.get_bucket_conf()

        pass  # Skip super's setUp().  The master should do the real work.

    def tearDown(self):
        if self.sc is not None:
            self.sc.stop()
            self.sc = None

        pass  # Skip super's tearDown().  The master should do the real work.

    def mk_stats(self, verbosity):
        if self.parami("prefix", 0) == 0 and self.level_callbacks:
            sc = CallbackStatsCollector(verbosity)
            sc.level_callbacks = self.level_callbacks
        else:
            sc = super(EPerfMaster, self).mk_stats(verbosity)
        return sc

    def test_wait_until_drained(self):
        self.wait_until_drained()


# The EVPerfClient subclass deploys another background thread to drive
# a concurrent amount of memcached traffic, during the "loop" or
# access phase.  Otherwise, with the basic EPerfClient, the http
# queries would gate the ops/second performance due to mcsoda's
# lockstep batching.
#
class EVPerfClient(EPerfClient):

    def mcsoda_run(self, cfg, cur, protocol, host_port, user, pswd,
                   stats_collector=None, stores=None, ctl=None,
                   heartbeat=0, why=None, bucket="default"):
        self.bg_thread = None
        self.bg_thread_ctl = None

        num_clients, start_at = self.access_phase_clients_start_at()

        # If non-zero background ops/second, the background thread,
        # with cloned parameters, doesn't do any queries, only basic
        # key-value ops.
        if why == "loop" and \
                self.parami("bg_max_ops_per_sec", self.bg_max_ops_per_sec):
            self.bg_thread_cfg = copy.deepcopy(cfg)
            self.bg_thread_cfg['max-ops-per-sec'] = \
                self.parami("bg_max_ops_per_sec", self.bg_max_ops_per_sec)
            self.bg_thread_cfg['ratio-queries'] = \
                self.paramf("bg_ratio_queries", 0.0)
            self.bg_thread_cfg['queries'] = self.param("bg_queries", "")
            self.bg_thread_cur = copy.deepcopy(cur)
            self.bg_thread_ctl = {'run_ok': True}

            bg_protocol = self.param('bg_protocol', 'membase-binary')
            if bg_protocol == 'membase-binary':
                self.bg_stores = [mcsoda.StoreMembaseBinary()]
                bg_host_port = host_port
                bg_protocol = protocol
            elif bg_protocol == 'memcached-binary':
                self.bg_stores = [mcsoda.StoreMemcachedBinary()]
                bg_host_port = host_port.split(':')[0] + ':11211'
                bg_protocol = 'memcached-binary'

            self.bg_thread = threading.Thread(target=mcsoda.run,
                                              args=(self.bg_thread_cfg,
                                                    self.bg_thread_cur,
                                                    bg_protocol, bg_host_port,
                                                    user, pswd,
                                                    stats_collector,
                                                    self.bg_stores,
                                                    self.bg_thread_ctl,
                                                    heartbeat, "loop-bg",
                                                    bucket))
            self.bg_thread.daemon = True
            self.bg_thread.start()

            # Also, the main mcsoda run should do no memcached/key-value
            # requests.
            cfg['ratio-sets'] = self.paramf("fg_ratio_sets", 0.0)
            cfg['ratio-queries'] = self.paramf("fg_ratio_queries", 1.0)

        if why == "loop" and self.parami("fg_max_ops", self.fg_max_ops):
            cfg['max-ops'] = start_at + \
                self.parami("fg_max_ops", self.fg_max_ops) / num_clients
            ctl = {'run_ok': True}
            if self.parami("fg_max_ops_per_sec", 0):
                cfg['max-ops-per-sec'] = self.parami("fg_max_ops_per_sec", 0)
            if getattr(self, "active_fg_workers", None) is not None:
                self.active_fg_workers.value += 1
                cfg['active_fg_workers'] = self.active_fg_workers
            if getattr(self, "shutdown_event", None) is not None:
                ctl['shutdown_event'] = self.shutdown_event
            cfg['node_prefix'] = self.parami('prefix', 0)

        cfg['stats_ops'] = self.parami("mcsoda_fg_stats_ops",
                                       PerfDefaults.mcsoda_fg_stats_ops)
        rv_cur, start_time, end_time = \
            super(EVPerfClient, self).mcsoda_run(cfg, cur, protocol, host_port,
                                                 user, pswd,
                                                 stats_collector=stats_collector,
                                                 stores=stores,
                                                 ctl=ctl,
                                                 heartbeat=heartbeat,
                                                 why="loop-fg",
                                                 bucket=bucket)
        if self.bg_thread_ctl:
            self.bg_thread_ctl['run_ok'] = False
            while self.bg_thread.is_alive():
                time.sleep(1)

        # Send histograms to dashboard
        if self.input.dashboard and why == "loop":
            description = socket.gethostname() + '-fg'
            self.cbkarma_client.histo(self.test_id, description, rv_cur)
            description = socket.gethostname() + '-bg'
            self.cbkarma_client.histo(self.test_id, description,
                                      self.bg_thread_cur)

        return rv_cur, start_time, end_time


class ViewGen:

    DDOC_NAMES = ["A", "B", "C", "D", "E", "F", "G", "H"]

    VIEW_NAMES = ['city1', 'city2', 'realm1', 'experts1', 'experts2', 'realm2',
                  'realm3', 'category']

    MAP_FUNCTIONS = [
        """
        function(doc, meta) {
            if (doc.city != null) {
                emit(doc.city, null);
            }
        }
        """,
        """
        function(doc, meta) {
            if (doc.city != null) {
                emit(doc.city, ["Name:" + doc.name, "E-mail:" + doc.email]);
            }
        }
        """,
        """
        function(doc, meta) {
            if (doc.realm != null) {
                emit(doc.realm, null);
            }
        }
        """,
        """
        function(doc, meta) {
            if (doc.category == 2) {
                emit([doc.name, doc.coins], null);
            }
        }
        """,
        """
        function(doc, meta) {
            emit([doc.category, doc.coins], null);
        }
        """,
        """
        function(doc, meta) {
            emit([doc.realm, doc.coins], null)
        }
        """,
        """
        function(doc, meta) {
            emit([doc.realm, doc.coins], [meta.id, doc.name, doc.email]);
        }
        """,
        """
        function(doc, meta) {
            emit([doc.category, doc.realm, doc.coins], [meta.id, doc.name, doc.email]);
        }
        """,
        """
        function (doc, meta) {
            if (doc.achievements.length > 0) {
                emit(doc.category, doc.coins);
            }
        }
        """]

    REDUCE_FUNCTIONS = ["_count", "_sum", "_stats"]

    def generate_ddocs(self, pattern=None, add_reduce=False):
        """Generate dictionary with design documents and views.
        Pattern looks like:
            [8, 8, 8] -- 8 ddocs (8 views, 8 views, 8 views)
            [2, 2, 4] -- 3 ddocs (2 views, 2 views, 4 views)
            [8] -- 1 ddoc (8 views)
            [1, 1, 1, 1, 1, 1, 1, 1] -- 8 ddocs (1 view)
        If `add_reduce` argument is True, additional ddoc with single
        map-reduce view is added
        """

        ddocs = dict()

        index_of_map = 0
        index_of_ddoc = 0

        for number_of_views in pattern:
            ddoc_name = self.DDOC_NAMES[index_of_ddoc]
            ddocs[ddoc_name] = {'views': {}}
            for index_of_view in range(number_of_views):
                try:
                    view_name = self.VIEW_NAMES[index_of_map]
                except IndexError:
                    index_of_map = 0
                    view_name = self.VIEW_NAMES[index_of_map]
                ddocs[ddoc_name]['views'][view_name] = {}
                ddocs[ddoc_name]['views'][view_name]['map'] = \
                    self.MAP_FUNCTIONS[index_of_map]
                index_of_map += 1
            index_of_ddoc += 1

        if add_reduce:
            ddocs['reduce'] = {
                'views': {
                    'reduce': {
                        'map': self.MAP_FUNCTIONS[-1],
                        'reduce': self.REDUCE_FUNCTIONS[-1]
                    }
                }
            }

        return ddocs

    def generate_all_docs_view(self):
        """ Return view definition which mimics primary index (aka _all_docs).
        """

        MAP_FUNCTION = """
            function (doc, meta) {
                emit(meta.id, {"rev": meta.rev});
            }"""

        return {'all': {'views': {'docs': {'map': MAP_FUNCTION}}}}

    def generate_queries(self, limit, query_suffix, ddocs, bucket='default',
                         use_all_docs=False, use_reduce=False, pseudo=False,
                         extend=False):
        """Generate string from permuted queries.

        Optional arguments:
        use_all_docs -- add query on primary index
        use_reduce -- add query on view with reduce step
        pseudo -- only queries on pseudo "all docs" index

        if ddocs is None it returns only queries on primary index
        """

        # Base path
        b = '/{0}/'.format(bucket)

        # Only all_docs case
        if ddocs is None:
            queries_by_kind = \
                [[b + '_all_docs?limit=' + str(limit) + '&startkey="{key}"']]
            remaining = [1]
            queries = self.compute_queries(queries_by_kind, remaining,
                                           query_suffix)
            return self.join_queries(queries)

        # Pseudo all docs case
        if pseudo:
            queries_by_kind = \
                [[b + '_design/all/_view/docs?limit=' + str(limit) + '&startkey="{key}"']]
            remaining = [1]
            queries = self.compute_queries(queries_by_kind, remaining,
                                           query_suffix)
            return self.join_queries(queries)

        # General case
        ddoc_names = \
            [name for name, ddoc in sorted(ddocs.iteritems()) for view in ddoc["views"]]

        q = {}
        q['city'] = b + '_design/' + ddoc_names[0] + '/_view/city1?limit=' + str(limit) + '&startkey="{city}"'
        q['city2'] = b + '_design/' + ddoc_names[1] + '/_view/city2?limit=' + str(limit) + '&startkey="{city}"'
        q['realm'] = b + '_design/' + ddoc_names[2] + '/_view/realm1?limit=30&startkey="{realm}"'
        q['experts'] = b + '_design/' + ddoc_names[3] + '/_view/experts1?limit=30&startkey="{name}"'
        q['coins-beg'] = b + '_design/' + ddoc_names[4] + '/_view/experts2?limit=30&startkey=[0,{int10}]&endkey=[0,{int100}]'
        q['coins-exp'] = b + '_design/' + ddoc_names[4] + '/_view/experts2?limit=30&startkey=[2,{int10}]&endkey=[2,{int100}]'
        q['and0'] = b + '_design/' + ddoc_names[5] + '/_view/realm2?limit=30&startkey=["{realm}",{coins}]'
        q['and1'] = b + '_design/' + ddoc_names[6] + '/_view/realm3?limit=30&startkey=["{realm}",{coins}]'
        q['and2'] = b + '_design/' + ddoc_names[7] + '/_view/category?limit=30&startkey=[0,"{realm}",{coins}]'

        queries_by_kind = [
            [  # 45% / 5 = 9
                q['city'],
                q['city2'],
                q['realm'],
                q['experts']],
            [  # 30% / 5 = 6
                q['coins-beg'],
                q['coins-exp']],
            [  # 25% / 5 = 5
                q['and0'],
                q['and1'],
                q['and2']]]

        remaining = [9, 6, 5]

        if use_all_docs:
            q['all_docs'] = b + '_all_docs?limit=' + str(limit) + \
                '&startkey="{key}"'
            queries_by_kind = [[q['all_docs']]] + queries_by_kind
            remaining = [5] + remaining

        if use_reduce:
            q['reduce'] = b + '_design/reduce/_view/reduce?limit=' + str(limit)
            queries_by_kind = queries_by_kind + [[q['reduce']]]
            remaining = remaining + [5]

        queries = self.compute_queries(queries_by_kind, remaining,
                                       query_suffix)
        if extend:
            queries = self.extend_queries(queries, ddocs)
        queries = self.join_queries(queries)

        return queries

    @staticmethod
    def create_stats_view(slave, bucket, prefix):
        """Create view definition which counts number of replicated items
        """

        rest = RestConnection(slave)

        MAP_FUNCTION = """
            function (doc) {{
                if (doc._id.indexOf("{0}") === 0) {{
                    emit(doc._id, null);
                }}
            }}""".format(prefix)

        ddoc = {'views': {'replicated': {'map': MAP_FUNCTION,
                                         'reduce': ViewGen.REDUCE_FUNCTIONS[0]}},
                'language': 'javascript',
                '_id': '_design/items'}

        api = '{0}{1}/_design/items'.format(rest.capiBaseUrl, bucket)

        rest._http_request(api, 'PUT', json.dumps(ddoc),
                           headers=rest._create_capi_headers())

        ClusterOperationHelper.wait_for_completion(rest, 'indexer')

    def compute_queries(self, queries_by_kind, remaining, suffix=""):
        """Return a list of permuted queries"""
        i = 0
        queries = []

        while remaining.count(0) < len(remaining):
            kind = i % len(remaining)
            count = remaining[kind]
            if count > 0:
                remaining[kind] = count - 1
                k = queries_by_kind[kind]
                queries.append(k[count % len(k)] + suffix)
            i = i + 1

        return queries

    def extend_queries(self, queries, ddocs):
        """Extend number of queries if number of views is more than 8. It only
        makes sense when originally there were only queries on single design
        document ([8] pattern). Otherwise it's better to avoid this method.
        """

        rename = lambda query, name: query.replace('/A/', '/{0}/'.format(name))

        ddoc_names = [ddoc_name for ddoc_name in sorted(ddocs.keys())]

        return [rename(query, name) for query in queries for name in ddoc_names]

    def join_queries(self, queries):
        """Join queries into string"""
        queries = ';'.join(queries)
        queries = queries.replace('[', '%5B')
        queries = queries.replace(']', '%5D')
        queries = queries.replace(',', '%2C')
        return queries

class NRUMonitor(threading.Thread):

     CMD_NUM_RUNS = "/opt/couchbase/bin/cbstats localhost:11210 "\
           "all | grep ep_num_access_scanner_runs"

     CMD_NUM_ITEMS = "/opt/couchbase/bin/cbstats localhost:11210 "\
            "all | grep ep_access_scanner_num_items"

     CMD_RUNTIME = "/opt/couchbase/bin/cbstats localhost:11210 "\
            "all | grep ep_access_scanner_last_runtime"

     def __init__(self, freq, reb_delay, eperf):
         self.freq = freq
         self.reb_delay = reb_delay
         self.eperf = eperf
         self.shell = None
         super(NRUMonitor, self).__init__()

     def run(self):
         print "[NRUMonitor] started running"

         # TODO: evaluate all servers, smarter polling freq
         server = self.eperf.input.servers[0]
         self.shell = RemoteMachineShellConnection(server)

         nru_num = self.nru_num = self.get_nru_num()
         if self.nru_num < 0:
             return

         while nru_num <= self.nru_num:
             print "[NRUMonitor] nru_num = %d, sleep for %d seconds" \
                % (nru_num, self.freq)
             time.sleep(self.freq)
             nru_num = self.get_nru_num()
             if nru_num < 0:
                 break

         gmt_now = time.strftime(PerfDefaults.strftime, time.gmtime())
         speed, num_items, run_time =  self.get_nru_speed()

         print "[NRUMonitor] access scanner finished at: %s, speed: %s, "\
            "num_items: %s, run_time: %s" \
            % (gmt_now, speed, num_items, run_time)

         self.eperf.clear_hot_keys()

         print "[NRUMonitor] scheduled rebalance after %d seconds"\
             % self.reb_delay

         self.shell.disconnect()
         self.eperf.latched_rebalance(delay=self.reb_delay, sync=True)

         gmt_now = time.strftime(PerfDefaults.strftime, time.gmtime())
         print "[NRUMonitor] rebalance finished: %s" % gmt_now

         print "[NRUMonitor] stopped running"

     def get_nru_num(self):
         """Retrieve how many times nru access scanner has been run"""
         return self._get_shell_int(NRUMonitor.CMD_NUM_RUNS)

     def get_nru_speed(self):
         """
         Retrieve runtime and num_items for the last access scanner run
         Calculate access running speed

         @return (speed, num_items, run_time)
         """
         num_items = self._get_shell_int(NRUMonitor.CMD_NUM_ITEMS)

         if num_items <= 0:
             return -1, -1, -1

         run_time = self._get_shell_int(NRUMonitor.CMD_RUNTIME)

         if run_time <= 0:
             return -1, num_items, -1

         speed = num_items / run_time

         return speed, num_items, run_time

     def _get_shell_int(self, cmd):
         """
         Fire a shell command and return output as integer
         """
         if not cmd:
             print "<_get_shell_int> invalid cmd"
             return -1

         output, error = self.shell.execute_command(cmd)

         if error:
             print "<_get_shell_int> unable to execute cmd '%s' from %s: %s"\
                % (cmd, self.shell.ip, error)
             return -1

         if not output:
             print "<_get_shell_int> unable to execute cmd '%s' from %s: "\
                "empty output" % (cmd, self.shell.ip)
             return -1

         try:
             num = int(output[0].split(":")[1])
         except  (AttributeError, IndexError, ValueError), e:
             print "<_get_shell_int> unable to execute cmd '%s' from %s:"\
                "output - %s, error - %s" % (cmd, self.shell.ip, output, e)
             return -1

         if num < 0:
             print "<_get_shell_int> invalid number: %d" % num
             return -1

         return num
