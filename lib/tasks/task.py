import time
import logger
import random
import string
import copy
import json
import re
import math
import crc32
from threading import Thread
from memcacheConstants import ERR_NOT_FOUND
from membase.api.rest_client import RestConnection, Bucket
from membase.api.exception import BucketCreationException
from membase.helper.bucket_helper import BucketOperationHelper
from memcached.helper.data_helper import KVStoreAwareSmartClient, VBucketAwareMemcached, MemcachedClientHelper
from couchbase.document import DesignDocument, View
from mc_bin_client import MemcachedError
from tasks.future import Future
from membase.api.exception import DesignDocCreationException, QueryViewException, ReadDocumentException, RebalanceFailedException, \
                                        GetBucketInfoFailed, CompactViewFailed, SetViewInfoNotFound, FailoverFailedException, ServerUnavailableException, BucketFlushFailed

from couchbase.documentgenerator import BatchedDocumentGenerator

#TODO: Setup stacktracer
#TODO: Needs "easy_install pygments"
#import stacktracer
#stacktracer.trace_start("trace.html",interval=30,auto=True) # Set auto flag to always update file!


PENDING = 'PENDING'
EXECUTING = 'EXECUTING'
CHECKING = 'CHECKING'
FINISHED = 'FINISHED'

class Task(Future):
    def __init__(self, name):
        Future.__init__(self)
        self.log = logger.Logger.get_logger()
        self.state = PENDING
        self.name = name
        self.cancelled = False
        self.retries = 0
        self.res = None

    def step(self, task_manager):
        if not self.done():
            if self.state == PENDING:
                self.state = EXECUTING
                task_manager.schedule(self)
            elif self.state == EXECUTING:
                self.execute(task_manager)
            elif self.state == CHECKING:
                self.check(task_manager)
            elif self.state != FINISHED:
                raise Exception("Bad State in {0}: {1}".format(self.name, self.state))

    def execute(self, task_manager):
        raise NotImplementedError

    def check(self, task_manager):
        raise NotImplementedError

class NodeInitializeTask(Task):
    def __init__(self, server, disabled_consistent_view=None):
        Task.__init__(self, "node_init_task")
        self.server = server
        self.quota = 0
        self.disable_consistent_view = disabled_consistent_view

    def execute(self, task_manager):
        try:
            rest = RestConnection(self.server)
        except ServerUnavailableException as error:
                self.state = FINISHED
                self.set_exception(error)
                return
        username = self.server.rest_username
        password = self.server.rest_password

        rest.set_reb_cons_view(self.disable_consistent_view)

        rest.init_cluster(username, password)
        info = rest.get_nodes_self()
        if info is None:
            self.state = FINISHED
            self.set_exception(Exception('unable to get information on a server %s, it is available?' % (self.server.ip)))
            return
        self.quota = int(info.mcdMemoryReserved * 2 / 3)
        rest.init_cluster_memoryQuota(username, password, self.quota)
        self.state = CHECKING
        task_manager.schedule(self)

    def check(self, task_manager):
        self.state = FINISHED
        self.set_result(self.quota)


class BucketCreateTask(Task):
    def __init__(self, server, bucket='default', replicas=1, size=0, port=11211,
                 password=None):
        Task.__init__(self, "bucket_create_task")
        self.server = server
        self.bucket = bucket
        self.replicas = replicas
        self.port = port
        self.size = size
        self.password = password

    def execute(self, task_manager):
        rest = RestConnection(self.server)
        if self.size <= 0:
            info = rest.get_nodes_self()
            self.size = info.memoryQuota * 2 / 3

        authType = 'none' if self.password is None else 'sasl'

        try:
            rest.create_bucket(bucket=self.bucket,
                               ramQuotaMB=self.size,
                               replicaNumber=self.replicas,
                               proxyPort=self.port,
                               authType=authType,
                               saslPassword=self.password)
            self.state = CHECKING
            task_manager.schedule(self)
        except BucketCreationException as e:
            self.state = FINISHED
            self.set_exception(e)
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    def check(self, task_manager):
        try:
            if BucketOperationHelper.wait_for_memcached(self.server, self.bucket):
                self.log.info("bucket '{0}' was created with per node RAM quota: {1}".format(self.bucket, self.size))
                self.set_result(True)
                self.state = FINISHED
                return
            else:
                self.log.info("vbucket map not ready after try {0}".format(self.retries))
        except Exception:
            self.log.info("vbucket map not ready after try {0}".format(self.retries))
        self.retries = self.retries + 1
        task_manager.schedule(self)


class BucketDeleteTask(Task):
    def __init__(self, server, bucket="default"):
        Task.__init__(self, "bucket_delete_task")
        self.server = server
        self.bucket = bucket

    def execute(self, task_manager):
        rest = RestConnection(self.server)
        try:
            if rest.delete_bucket(self.bucket):
                self.state = CHECKING
                task_manager.schedule(self)
            else:
                self.state = FINISHED
                self.set_result(False)
        #catch and set all unexpected exceptions

        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)


    def check(self, task_manager):
        rest = RestConnection(self.server)
        try:
            if BucketOperationHelper.wait_for_bucket_deletion(self.bucket, rest, 200):
                self.set_result(True)
            else:
                self.set_result(False)
            self.state = FINISHED
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

class RebalanceTask(Task):
    def __init__(self, servers, to_add=[], to_remove=[], do_stop=False, progress=30):
        Task.__init__(self, "rebalance_task")
        self.servers = servers
        self.to_add = to_add
        self.to_remove = to_remove
        self.start_time = None
        self.rest = RestConnection(self.servers[0])

    def execute(self, task_manager):
        try:
            self.add_nodes(task_manager)
            self.start_rebalance(task_manager)
            self.state = CHECKING
            task_manager.schedule(self)
        except Exception as e:
            self.state = FINISHED
            self.set_exception(e)

    def add_nodes(self, task_manager):
        master = self.servers[0]
        for node in self.to_add:
            self.log.info("adding node {0}:{1} to cluster".format(node.ip, node.port))
            self.rest.add_node(master.rest_username, master.rest_password,
                          node.ip, node.port)

    def start_rebalance(self, task_manager):
        nodes = self.rest.node_statuses()

        #Determine whether its a cluster_run/not
        cluster_run = True

        firstIp = self.servers[0].ip
        if len(self.servers) == 1 and self.servers[0].port == '8091':
            cluster_run = False
        else:
            for node in self.servers:
                if node.ip != firstIp:
                    cluster_run = False
                    break
        ejectedNodes = []

        for server in self.to_remove:
            for node in nodes:
                if cluster_run:
                    if int(server.port) == int(node.port):
                        ejectedNodes.append(node.id)
                else:
                    if server.ip == node.ip and int(server.port) == int(node.port):
                        ejectedNodes.append(node.id)
        self.rest.rebalance(otpNodes=[node.id for node in nodes], ejectedNodes=ejectedNodes)
        self.start_time = time.time()

    def check(self, task_manager):
        progress = -100
        try:
            progress = self.rest._rebalance_progress()
        except RebalanceFailedException as ex:
            self.state = FINISHED
            self.set_exception(ex)
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught in {0} sec".
                          format(time.time() - self.start_time))
            self.set_exception(e)
        if progress != -1 and progress != 100:
            task_manager.schedule(self, 10)
        else:
            success_cleaned = []
            for removed in self.to_remove:
                rest = RestConnection(removed)
                start = time.time()
                while time.time() - start < 10:
                    if len(rest.get_pools_info()["pools"]) == 0:
                        success_cleaned.append(removed)
                        break
                    else:
                        time.sleep(0.1)
            result = True
            for node in set(self.to_remove) - set(success_cleaned):
                self.log.error("node {0}:{1} was not cleaned after removing from cluster".format(
                           node.ip, node.port))
                result = False

            self.log.info("rebalancing was completed with progress: {0}% in {1} sec".
                          format(progress, time.time() - self.start_time))
            self.state = FINISHED
            self.set_result(result)

class StatsWaitTask(Task):
    EQUAL = '=='
    NOT_EQUAL = '!='
    LESS_THAN = '<'
    LESS_THAN_EQ = '<='
    GREATER_THAN = '>'
    GREATER_THAN_EQ = '>='

    def __init__(self, servers, bucket, param, stat, comparison, value):
        Task.__init__(self, "stats_wait_task")
        self.servers = servers
        self.bucket = bucket
        if isinstance(bucket, Bucket):
            self.bucket = bucket.name
        self.param = param
        self.stat = stat
        self.comparison = comparison
        self.value = value
        self.conns = {}

    def execute(self, task_manager):
        self.state = CHECKING
        task_manager.schedule(self)

    def check(self, task_manager):
        stat_result = 0
        for server in self.servers:
            client = self._get_connection(server)
            try:
                stats = client.stats(self.param)
                if not stats.has_key(self.stat):
                    self.state = FINISHED
                    self.set_exception(Exception("Stat {0} not found".format(self.stat)))
                    return
                if stats[self.stat].isdigit():
                    stat_result += long(stats[self.stat])
                else:
                    stat_result = stats[self.stat]
            except EOFError as ex:
                self.state = FINISHED
                self.set_exception(ex)

        if not self._compare(self.comparison, str(stat_result), self.value):
            self.log.info("Not Ready: %s %s %s %s expected on %s" % (self.stat, stat_result,
                      self.comparison, self.value, self._stringify_servers()))
            task_manager.schedule(self, 5)
            return
        self.log.info("Saw %s %s %s %s expected on %s" % (self.stat, stat_result,
                      self.comparison, self.value, self._stringify_servers()))

        for server, conn in self.conns.items():
            conn.close()
        self.state = FINISHED
        self.set_result(True)

    def _stringify_servers(self):
        return ''.join([`server.ip` for server in self.servers])

    def _get_connection(self, server):
        if not self.conns.has_key(server):
            self.conns[server] = MemcachedClientHelper.direct_client(server, self.bucket)
        return self.conns[server]

    def _compare(self, cmp_type, a, b):
        if isinstance(b, (int, long)) and a.isdigit():
            a = long(a)
        elif isinstance(b, (int, long)) and not a.isdigit():
                return False
        if (cmp_type == StatsWaitTask.EQUAL and a == b) or\
            (cmp_type == StatsWaitTask.NOT_EQUAL and a != b) or\
            (cmp_type == StatsWaitTask.LESS_THAN_EQ and a <= b) or\
            (cmp_type == StatsWaitTask.GREATER_THAN_EQ and a >= b) or\
            (cmp_type == StatsWaitTask.LESS_THAN and a < b) or\
            (cmp_type == StatsWaitTask.GREATER_THAN and a > b):
            return True
        return False

class GenericLoadingTask(Thread, Task):
    def __init__(self, server, bucket, kv_store):
        Thread.__init__(self)
        Task.__init__(self, "load_gen_task")
        self.kv_store = kv_store
        self.client = VBucketAwareMemcached(RestConnection(server), bucket)

    def execute(self, task_manager):
        self.start()
        self.state = EXECUTING

    def check(self, task_manager):
        pass

    def run(self):
        while self.has_next() and not self.done():
            self.next()
        self.state = FINISHED
        self.set_result(True)

    def has_next(self):
        raise NotImplementedError

    def next(self):
        raise NotImplementedError

    def _unlocked_create(self, partition, key, value):
        try:
            value_json = json.loads(value)
            value_json['mutated'] = 0
            value = json.dumps(value_json)
        except ValueError:
            index = random.choice(range(len(value)))
            value = value[0:index] + random.choice(string.ascii_uppercase) + value[index + 1:]

        try:
            self.client.set(key, self.exp, self.flag, value)
            if self.only_store_hash:
                value = str(crc32.crc32_hash(value))
            partition.set(key, value, self.exp, self.flag)
        except MemcachedError as error:
            self.state = FINISHED
            self.set_exception(error)

    def _unlocked_read(self, partition, key):
        try:
            o, c, d = self.client.get(key)
        except MemcachedError as error:
            if error.status == ERR_NOT_FOUND and partition.get_valid(key) is None:
                pass
            else:
                self.state = FINISHED
                self.set_exception(error)

    def _unlocked_update(self, partition, key):
        o, c, value = self.client.get(key)
        if value is None:
            return
        try:
            value_json = json.loads(value)
            value_json['mutated'] += 1
            value = json.dumps(value_json)
        except ValueError:
            index = random.choice(range(len(value)))
            value = value[0:index] + random.choice(string.ascii_uppercase) + value[index + 1:]

        try:
            self.client.set(key, self.exp, self.flag, value)
            if self.only_store_hash:
                 value = str(crc32.crc32_hash(value))
            partition.set(key, value, self.exp, self.flag)
        except MemcachedError as error:
            self.state = FINISHED
            self.set_exception(error)

    def _unlocked_delete(self, partition, key):
        try:
            self.client.delete(key)
            partition.delete(key)
        except MemcachedError as error:
            if error.status == ERR_NOT_FOUND and partition.get_valid(key) is None:
                pass
            else:
                self.state = FINISHED
                self.set_exception(error)

class LoadDocumentsTask(GenericLoadingTask):
    def __init__(self, server, bucket, generator, kv_store, op_type, exp, flag=0, only_store_hash=True):
        GenericLoadingTask.__init__(self, server, bucket, kv_store)
        self.generator = generator
        self.op_type = op_type
        self.exp = exp
        self.flag = flag
        self.only_store_hash = only_store_hash

    def has_next(self):
        return self.generator.has_next()

    def next(self):
        key, value = self.generator.next()
        partition = self.kv_store.acquire_partition(key)
        if self.op_type == 'create':
            self._unlocked_create(partition, key, value)
        elif self.op_type == 'read':
            self._unlocked_read(partition, key)
        elif self.op_type == 'update':
            self._unlocked_update(partition, key)
        elif self.op_type == 'delete':
            self._unlocked_delete(partition, key)
        else:
            self.state = FINISHED
            self.set_exception(Exception("Bad operation type: %s" % self.op_type))
        self.kv_store.release_partition(key)

class BatchedLoadDocumentsTask(GenericLoadingTask):
    def __init__(self, server, bucket, generator, kv_store, op_type, exp, flag=0, only_store_hash=True, batch_size=100, pause_secs=1, timeout_secs=30):
        GenericLoadingTask.__init__(self, server, bucket, kv_store)
        self.batch_generator = BatchedDocumentGenerator(generator, batch_size)
        self.op_type = op_type
        self.exp = exp
        self.flag = flag
        self.only_store_hash = only_store_hash
        self.batch_size = batch_size
        self.pause = pause_secs
        self.timeout = timeout_secs

    def has_next(self):
        return self.batch_generator.has_next()

    def next(self):
        key_value = self.batch_generator.next_batch()
        self.log.info("Batch loading documents #: {0}".format(len(key_value)))
        partition_keys_dic = self.kv_store.acquire_partitions(key_value.keys())
        if self.op_type == 'create':
            self._create_batch(partition_keys_dic, key_value)
        elif self.op_type == 'update':
            self._update_batch(partition_keys_dic, key_value)
        elif self.op_type == 'delete':
            self._delete_batch(partition_keys_dic, key_value)
        elif self.op_type == 'read':
            self._read_batch(partition_keys_dic, key_value)
        else:
            self.state = FINISHED
            self.set_exception(Exception("Bad operation type: %s" % self.op_type))
        self.kv_store.release_partitions(partition_keys_dic.keys())


    def _create_batch(self, partition_keys_dic, key_val):
        try:
            self._process_values_for_create(key_val)
            self.client.setMulti(self.exp, self.flag, key_val, self.pause, self.timeout, parallel=False)
            self._populate_kvstore(partition_keys_dic, key_val)
        except MemcachedError as error:
            self.state = FINISHED
            self.set_exception(error)


    def _update_batch(self, partition_keys_dic, key_val):
        try:
            self._process_values_for_update(partition_keys_dic, key_val)
            self.client.setMulti(self.exp, self.flag, key_val, self.pause, self.timeout, parallel=False)
            self._populate_kvstore(partition_keys_dic, key_val)
        except MemcachedError as error:
            self.state = FINISHED
            self.set_exception(error)


    def _delete_batch(self, partition_keys_dic, key_val):
        for partition, keys in partition_keys_dic.items():
            for key in keys:
                try:
                    self.client.delete(key)
                    partition.delete(key)
                except MemcachedError as error:
                    if error.status == ERR_NOT_FOUND and partition.get_valid(key) is None:
                        pass
                    else:
                        self.state = FINISHED
                        self.set_exception(error)
                        return

    def _read_batch(self, partition_keys_dic, key_val):
        try:
            o, c, d = self.client.getMulti(key_val.keys(), self.pause, self.timeout)
        except MemcachedError as error:
                self.state = FINISHED
                self.set_exception(error)

    def _process_values_for_create(self, key_val):
        for key, value in key_val.items():
            try:
                value_json = json.loads(value)
                value_json['mutated'] = 0
                value = json.dumps(value_json)
            except ValueError:
                index = random.choice(range(len(value)))
                value = value[0:index] + random.choice(string.ascii_uppercase) + value[index + 1:]
            finally:
                key_val[key] = value

    def _process_values_for_update(self, partition_keys_dic, key_val):
        for partition, keys in partition_keys_dic.items():
            for key  in keys:
                value = partition.get_valid(key)
                if value is None:
                    del key_val[key]
                    continue
                try:
                    value = key_val[key] # new updated value, however it is not their in orginal code "LoadDocumentsTask"
                    value_json = json.loads(value)
                    value_json['mutated'] += 1
                    value = json.dumps(value_json)
                except ValueError:
                    index = random.choice(range(len(value)))
                    value = value[0:index] + random.choice(string.ascii_uppercase) + value[index + 1:]
                finally:
                    key_val[key] = value


    def _populate_kvstore(self, partition_keys_dic, key_val):
        for partition, keys in partition_keys_dic.items():
            self._populate_kvstore_partition(partition, keys, key_val)

    def _release_locks_on_kvstore(self):
        for part in self._partitions_keyvals_dic.keys:
            self.kv_store.release_lock(part)

    def _populate_kvstore_partition(self, partition, keys, key_val):
        for key in keys:
            if self.only_store_hash:
                key_val[key] = str(crc32.crc32_hash(key_val[key]))
            partition.set(key, key_val[key], self.exp, self.flag)

class WorkloadTask(GenericLoadingTask):
    def __init__(self, server, bucket, kv_store, num_ops, create, read, update, delete, exp):
        GenericLoadingTask.__init__(self, server, bucket, kv_store)
        self.itr = 0
        self.num_ops = num_ops
        self.create = create
        self.read = create + read
        self.update = create + read + update
        self.delete = create + read + update + delete
        self.exp = exp

    def has_next(self):
        if self.num_ops == 0 or self.itr < self.num_ops:
            return True
        return False

    def next(self):
        self.itr += 1
        rand = random.randint(1, self.delete)
        if rand > 0 and rand <= self.create:
            self._create_random_key()
        elif rand > self.create and rand <= self.read:
            self._get_random_key()
        elif rand > self.read and rand <= self.update:
            self._update_random_key()
        elif rand > self.update and rand <= self.delete:
            self._delete_random_key()

    def _get_random_key(self):
        partition, part_num = self.kv_store.acquire_random_partition()
        if partition is None:
            return

        key = partition.get_random_valid_key()
        if key is None:
            self.kv_store.release_partition(part_num)
            return

        self._unlocked_read(partition, key)
        self.kv_store.release_partition(part_num)

    def _create_random_key(self):
        partition, part_num = self.kv_store.acquire_random_partition(False)
        if partition is None:
            return

        key = partition.get_random_deleted_key()
        if key is None:
            self.kv_store.release_partition(part_num)
            return

        value = partition.get_deleted(key)
        if value is None:
            self.kv_store.release_partition(part_num)
            return

        self._unlocked_create(partition, key, value)
        self.kv_store.release_partition(part_num)

    def _update_random_key(self):
        partition, part_num = self.kv_store.acquire_random_partition()
        if partition is None:
            return

        key = partition.get_random_valid_key()
        if key is None:
            self.kv_store.release_partition(part_num)
            return

        self._unlocked_update(partition, key)
        self.kv_store.release_partition(part_num)

    def _delete_random_key(self):
        partition, part_num = self.kv_store.acquire_random_partition()
        if partition is None:
            return

        key = partition.get_random_valid_key()
        if key is None:
            self.kv_store.release_partition(part_num)
            return

        self._unlocked_delete(partition, key)
        self.kv_store.release_partition(part_num)

class ValidateDataTask(GenericLoadingTask):
    def __init__(self, server, bucket, kv_store, max_verify=None, only_store_hash=True):
        GenericLoadingTask.__init__(self, server, bucket, kv_store)
        self.valid_keys, self.deleted_keys = kv_store.key_set()
        self.num_valid_keys = len(self.valid_keys)
        self.num_deleted_keys = len(self.deleted_keys)
        self.itr = 0
        self.max_verify = self.num_valid_keys + self.num_deleted_keys
        self.only_store_hash = only_store_hash
        if max_verify is not None:
            self.max_verify = min(max_verify, self.max_verify)
        self.log.info("%s items will be verified on %s bucket" % (self.max_verify, bucket))

    def has_next(self):
        if self.itr < (self.num_valid_keys + self.num_deleted_keys) and\
            self.itr < self.max_verify:
            if not self.itr % 50000:
                self.log.info("{0} items were verified".format(self.itr))
            return True
        return False

    def next(self):
        if self.itr < self.num_valid_keys:
            self._check_valid_key(self.valid_keys[self.itr])
        else:
            self._check_deleted_key(self.deleted_keys[self.itr - self.num_valid_keys])
        self.itr += 1

    def _check_valid_key(self, key):
        partition = self.kv_store.acquire_partition(key)

        value = partition.get_valid(key)
        flag = partition.get_flag(key)
        if value is None or flag is None:
            self.kv_store.release_partition(key)
            return

        try:
            o, c, d = self.client.get(key)
            if self.only_store_hash:
                if crc32.crc32_hash(d) != int(value):
                    self.state = FINISHED
                    self.set_exception(Exception('Bad hash result: %d != %d' % (crc32.crc32_hash(d), int(value))))
            else:
                value = json.dumps(value)
                if d != json.loads(value):
                    self.state = FINISHED
                    self.set_exception(Exception('Bad result: %s != %s' % (json.dumps(d), value)))
            if o != flag:
                self.state = FINISHED
                self.set_exception(Exception('Bad result for flag value: %s != the value we set: %s' % (o, flag)))

        except MemcachedError as error:
            if error.status == ERR_NOT_FOUND and partition.get_valid(key) is None:
                pass
            else:
                self.state = FINISHED
                self.set_exception(error)
        self.kv_store.release_partition(key)

    def _check_deleted_key(self, key):
        partition = self.kv_store.acquire_partition(key)

        try:
            o, c, d = self.client.delete(key)
            if partition.get_valid(key) is not None:
                self.state = FINISHED
                self.set_exception(Exception('Not Deletes: %s' % (key)))
        except MemcachedError as error:
            if error.status == ERR_NOT_FOUND:
                pass
            else:
                self.state = FINISHED
                self.set_exception(error)
        self.kv_store.release_partition(key)


class BatchedValidateDataTask(GenericLoadingTask):
    def __init__(self, server, bucket, kv_store, max_verify=None, only_store_hash=True, batch_size=100):
        GenericLoadingTask.__init__(self, server, bucket, kv_store)
        self.valid_keys, self.deleted_keys = kv_store.key_set()
        self.num_valid_keys = len(self.valid_keys)
        self.num_deleted_keys = len(self.deleted_keys)
        self.itr = 0
        self.max_verify = self.num_valid_keys + self.num_deleted_keys
        self.only_store_hash = only_store_hash
        if max_verify is not None:
            self.max_verify = min(max_verify, self.max_verify)
        self.log.info("%s items will be verified on %s bucket" % (self.max_verify, bucket))
        self.batch_size = batch_size

    def has_next(self):
        if self.itr < (self.num_valid_keys + self.num_deleted_keys) and self.itr < self.max_verify:
            return True
        return False

    def next(self):
        if self.itr < self.num_valid_keys:
            keys_batch = self.valid_keys[self.itr:self.itr + self.batch_size]
            self.itr += len(keys_batch)
            self._check_valid_keys(keys_batch)
        else:
            self._check_deleted_key(self.deleted_keys[self.itr - self.num_valid_keys])
            self.itr += 1

    def _check_valid_keys(self, keys):
        partition_keys_dic = self.kv_store.acquire_partitions(keys)
        key_vals = self.client.getMulti(keys, parallel=True)
        for partition, keys in partition_keys_dic.items():
            self._check_validity(partition, keys, key_vals)
        self.kv_store.release_partitions(partition_keys_dic.keys())

    def _check_validity(self, partition, keys, key_vals):

        for key in keys:
            value = partition.get_valid(key)
            flag = partition.get_flag(key)
            if value is None:
                continue
            try:
                o, c, d = key_vals[key]
                if self.only_store_hash:
                    if crc32.crc32_hash(d) != int(value):
                        self.state = FINISHED
                        self.set_exception(Exception('Bad hash result: %d != %d' % (crc32.crc32_hash(d), int(value))))
                else:
                    value = json.dumps(value)
                    if d != json.loads(value):
                        self.state = FINISHED
                        self.set_exception(Exception('Bad result: %s != %s' % (json.dumps(d), value)))
                if o != flag:
                    self.state = FINISHED
                    self.set_exception(Exception('Bad result for flag value: %s != the value we set: %s' % (o, flag)))
            except KeyError as error:
                self.state = FINISHED
                self.set_exception(error)


    def _check_deleted_key(self, key):
        partition = self.kv_store.acquire_partition(key)

        try:
            o, c, d = self.client.delete(key)
            if partition.get_valid(key) is not None:
                self.state = FINISHED
                self.set_exception(Exception('Not Deletes: %s' % (key)))
        except MemcachedError as error:
            if error.status == ERR_NOT_FOUND:
                pass
            else:
                self.state = FINISHED
                self.set_exception(error)
        self.kv_store.release_partition(key)


class VerifyRevIdTask(GenericLoadingTask):
    def __init__(self, src_server, dest_server, bucket, kv_store, ops_perf):
        GenericLoadingTask.__init__(self, src_server, bucket, kv_store)
        self.client_dest = VBucketAwareMemcached(RestConnection(dest_server), bucket)

        self.valid_keys, self.deleted_keys = kv_store.key_set()
        self.num_valid_keys = len(self.valid_keys)
        self.num_deleted_keys = len(self.deleted_keys)
        self.itr = 0
        self.err_count = 0
        self.ops_perf = ops_perf

    def has_next(self):
        if self.ops_perf in ["delete", "update"] :
            if self.itr < (self.num_valid_keys + self.num_deleted_keys):
                return True
            self.log.info("Verification done, {0} items have been "
                          "verified ({1}d items: {2})".format(
                           self.itr, self.ops_perf, self.num_deleted_keys))
            return False

    def next(self):
        if self.ops_perf in ["delete", "update"]:
            if self.itr < self.num_valid_keys:
                self._check_key_revId(self.valid_keys[self.itr])
            elif self.itr < (self.num_valid_keys + self.num_deleted_keys):
                # verify deleted/expired keys
                self._check_key_revId(self.deleted_keys[self.itr - self.num_valid_keys])
            self.itr += 1

        ## show progress of verification for every 50k items
        if math.fmod(self.itr, 50000) == 0.0:
            self.log.info("{0} items have been verified".format(self.itr))


    def _check_key_revId(self, key):
        try:
            src = self.client.memcached(key)
            dest = self.client_dest.memcached(key)
            _, flags_src, exp_src, seqno_src, cas_src = src.getMeta(key)
            _, flags_dest, exp_dest, seqno_dest, cas_dest = dest.getMeta(key)

            ## seqno number should never be zero
            if seqno_src == 0:
                self.err_count += 1
                self.log.error(
                    "(Source) Sequence numbers for key {0}\t is 0, Error Count{1}".format(key, self.err_count))
                self.log.error(
                    "Source (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_src, cas_src, exp_src, flags_src))
                self.state = FINISHED

            if seqno_dest == 0:
                self.err_count += 1
                self.log.error(
                    "(Dest) Sequence numbers for key {0}\t is 0,  Error Count{1}".format(key, self.err_count))
                self.log.error(
                    "Dest (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_dest, cas_dest, exp_dest, flags_dest))
                self.state = FINISHED

            ## verify all metadata
            if seqno_src != seqno_dest:
                self.err_count += 1
                self.log.error(
                    "Mismatch on sequence numbers for key {0}\t Source Sequence Num:{1}\t Destination Sequence Num:{2}\tError Count{3}".format(
                        key, seqno_src, seqno_dest, self.err_count))

                self.log.error(
                    "Source (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_src, cas_src, exp_src, flags_src))
                self.log.error(
                    "Dest (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_dest, cas_dest, exp_dest, flags_dest))

                self.state = FINISHED
            elif cas_src != cas_dest:
                self.err_count += 1
                self.log.error(
                    "Mismatch on CAS for key {0}\t Source CAS:{1}\t Destination CAS:{2}\tError Count{3}".format(key,
                        cas_src,
                        cas_dest, self.err_count))

                self.log.error(
                    "Source (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_src, cas_src, exp_src, flags_src))
                self.log.error(
                    "Dest (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_dest, cas_dest, exp_dest, flags_dest))

                self.state = FINISHED
            elif exp_src != exp_dest:
                self.err_count += 1
                self.log.error(
                    "Mismatch on Expiry Flags for key {0}\t Source Expiry Flags:{1}\t Destination Expiry Flags:{2}\tError Count{3}".format(
                        key,
                        exp_src, exp_dest, self.err_count))

                self.log.error(
                    "Source (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_src, cas_src, exp_src, flags_src))
                self.log.error(
                    "Dest (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_dest, cas_dest, exp_dest, flags_dest))

                self.state = FINISHED
            elif flags_src != flags_dest:
                self.err_count += 1
                self.log.error(
                    "Mismatch on Flags for key {0}\t Source Flags:{1}\t Destination Flags:{2}\tError Count{3}".format(
                        key,
                        flags_src, flags_dest, self.err_count))

                self.log.error(
                    "Source (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_src, cas_src, exp_src, flags_src))
                self.log.error(
                    "Dest (Seqno: {0}, CAS: {1}, Exp: {2}, Flag: {3})".format(seqno_dest, cas_dest, exp_dest, flags_dest))

                self.state = FINISHED
        except MemcachedError as error:
            if error.status == ERR_NOT_FOUND:
                pass
            else:
                self.state = FINISHED
                self.set_exception(error)

class ViewCreateTask(Task):
    def __init__(self, server, design_doc_name, view, bucket="default"):
        Task.__init__(self, "create_view_task")
        self.server = server
        self.bucket = bucket
        self.view = view
        prefix = ""
        if self.view:
            prefix = ("", "dev_")[self.view.dev_view]
        self.design_doc_name = prefix + design_doc_name
        self.ddoc_rev_no = 0

    def execute(self, task_manager):

        rest = RestConnection(self.server)

        try:
            # appending view to existing design doc
            content, meta = rest.get_ddoc(self.bucket, self.design_doc_name)
            ddoc = DesignDocument._init_from_json(self.design_doc_name, content)
            #if view is to be updated
            if self.view:
                ddoc.add_view(self.view)
            self.ddoc_rev_no = self._parse_revision(meta['rev'])

        except ReadDocumentException:
            # creating first view in design doc
            if self.view:
                ddoc = DesignDocument(self.design_doc_name, [self.view])
            #create an empty design doc
            else:
                ddoc = DesignDocument(self.design_doc_name, [])

        try:
            rest.create_design_document(self.bucket, ddoc)
            self.state = CHECKING
            task_manager.schedule(self)

        except DesignDocCreationException as e:
            self.state = FINISHED
            self.set_exception(e)

        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    def check(self, task_manager):
        rest = RestConnection(self.server)

        try:
            #only query if the DDoc has a view
            if self.view:
                query = {"stale" : "ok"}
                content = \
                    rest.query_view(self.design_doc_name, self.view.name, self.bucket, query)
                self.log.info("view : {0} was created successfully in ddoc: {1}".format(self.view.name, self.design_doc_name))
            else:
                #if we have reached here, it means design doc was successfully updated
                self.log.info("Design Document : {0} was updated successfully".format(self.design_doc_name))

            self.state = FINISHED
            if self._check_ddoc_revision():
                self.set_result(self.ddoc_rev_no)
            else:
                self.set_exception(Exception("failed to update design document"))
        except QueryViewException as e:
            if e.message.find('not_found') or e.message.find('view_undefined') > -1:
                task_manager.schedule(self, 2)
            else:
                self.state = FINISHED
                self.log.info("Unexpected Exception Caught")
                self.set_exception(e)
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    def _check_ddoc_revision(self):
        valid = False
        rest = RestConnection(self.server)

        try:
            content, meta = rest.get_ddoc(self.bucket, self.design_doc_name)
            new_rev_id = self._parse_revision(meta['rev'])
            if new_rev_id != self.ddoc_rev_no:
                self.ddoc_rev_no = new_rev_id
                valid = True
        except ReadDocumentException:
            pass

        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

        return valid

    def _parse_revision(self, rev_string):
        return int(rev_string.split('-')[0])

class ViewDeleteTask(Task):
    def __init__(self, server, design_doc_name, view, bucket="default"):
        Task.__init__(self, "delete_view_task")
        self.server = server
        self.bucket = bucket
        self.view = view
        prefix = ""
        if self.view:
            prefix = ("", "dev_")[self.view.dev_view]
        self.design_doc_name = prefix + design_doc_name

    def execute(self, task_manager):

        rest = RestConnection(self.server)

        try:
            if self.view:
                # remove view from existing design doc
                content, header = rest.get_ddoc(self.bucket, self.design_doc_name)
                ddoc = DesignDocument._init_from_json(self.design_doc_name, content)
                status = ddoc.delete_view(self.view)
                if not status:
                    self.state = FINISHED
                    self.set_exception(Exception('View does not exist! %s' % (self.view.name)))

                # update design doc
                rest.create_design_document(self.bucket, ddoc)
                self.state = CHECKING
                task_manager.schedule(self, 2)
            else:
                #delete the design doc
                rest.delete_view(self.bucket, self.design_doc_name)
                self.log.info("Design Doc : {0} was successfully deleted".format(self.design_doc_name))
                self.state = FINISHED
                self.set_result(True)

        except (ValueError, ReadDocumentException, DesignDocCreationException) as e:
            self.state = FINISHED
            self.set_exception(e)

        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    def check(self, task_manager):
        rest = RestConnection(self.server)

        try:
            # make sure view was deleted
            query = {"stale" : "ok"}
            content = \
                rest.query_view(self.design_doc_name, self.view.name, self.bucket, query)
            self.state = FINISHED
            self.set_result(False)
        except QueryViewException as e:
            self.log.info("view : {0} was successfully deleted in ddoc: {1}".format(self.view.name, self.design_doc_name))
            self.state = FINISHED
            self.set_result(True)

        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

class ViewQueryTask(Task):
    def __init__(self, server, design_doc_name, view_name,
                 query, expected_rows=None,
                 bucket="default", retry_time=2):
        Task.__init__(self, "query_view_task")
        self.server = server
        self.bucket = bucket
        self.view_name = view_name
        self.design_doc_name = design_doc_name
        self.query = query
        self.expected_rows = expected_rows
        self.retry_time = retry_time
        self.timeout = 900

    def execute(self, task_manager):

        rest = RestConnection(self.server)

        try:
            # make sure view can be queried
            content = \
                rest.query_view(self.design_doc_name, self.view_name, self.bucket, self.query, self.timeout)

            if self.expected_rows is None:
                # no verification
                self.state = FINISHED
                self.set_result(content)
            else:
                self.state = CHECKING
                task_manager.schedule(self)

        except QueryViewException as e:
            # initial query failed, try again
            task_manager.schedule(self, self.retry_time)

        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    def check(self, task_manager):
        rest = RestConnection(self.server)

        try:
            # query and verify expected num of rows returned
            content = \
                rest.query_view(self.design_doc_name, self.view_name, self.bucket, self.query, self.timeout)

            self.log.info("(%d rows) expected, (%d rows) returned" % \
                          (self.expected_rows, len(content['rows'])))

            if len(content['rows']) == self.expected_rows:
                self.log.info("expected number of rows: '{0}' was found for view query".format(self.
                            expected_rows))
                self.state = FINISHED
                self.set_result(True)
            else:
                if "stale" in self.query:
                    if self.query["stale"].lower() == "false":
                        self.state = FINISHED
                        self.set_result(False)

                # retry until expected results or task times out
                task_manager.schedule(self, self.retry_time)
        except QueryViewException as e:
            # subsequent query failed! exit
            self.state = FINISHED
            self.set_exception(e)

        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)


class ModifyFragmentationConfigTask(Task):

    """
        Given a config dictionary attempt to configure fragmentation settings.
        This task will override the default settings that are provided for
        a given <bucket>.
    """

    def __init__(self, server, config=None, bucket="default"):
        Task.__init__(self, "modify_frag_config_task")

        self.server = server
        self.config = {"parallelDBAndVC" : "false",
                       "dbFragmentThreshold" : None,
                       "viewFragmntThreshold" : None,
                       "dbFragmentThresholdPercentage" : 100,
                       "viewFragmntThresholdPercentage" : 100,
                       "allowedTimePeriodFromHour" : None,
                       "allowedTimePeriodFromMin" : None,
                       "allowedTimePeriodToHour" : None,
                       "allowedTimePeriodToMin" : None,
                       "allowedTimePeriodAbort" : None,
                       "autoCompactionDefined" : "true"}
        self.bucket = bucket

        for key in config:
            self.config[key] = config[key]

    def execute(self, task_manager):
        rest = RestConnection(self.server)

        try:
            rest.set_auto_compaction(parallelDBAndVC=self.config["parallelDBAndVC"],
                                     dbFragmentThreshold=self.config["dbFragmentThreshold"],
                                     viewFragmntThreshold=self.config["viewFragmntThreshold"],
                                     dbFragmentThresholdPercentage=self.config["dbFragmentThresholdPercentage"],
                                     viewFragmntThresholdPercentage=self.config["viewFragmntThresholdPercentage"],
                                     allowedTimePeriodFromHour=self.config["allowedTimePeriodFromHour"],
                                     allowedTimePeriodFromMin=self.config["allowedTimePeriodFromMin"],
                                     allowedTimePeriodToHour=self.config["allowedTimePeriodToHour"],
                                     allowedTimePeriodToMin=self.config["allowedTimePeriodToMin"],
                                     allowedTimePeriodAbort=self.config["allowedTimePeriodAbort"],
                                     bucket=self.bucket)

            self.state = CHECKING
            task_manager.schedule(self, 10)

        except Exception as e:
            self.state = FINISHED
            self.set_exception(e)


    def check(self, task_manager):

        rest = RestConnection(self.server)
        try:
            # verify server accepted settings
            content = rest.get_bucket_json(self.bucket)
            if content["autoCompactionSettings"] == False:
                self.set_exception(Exception("Failed to set auto compaction settings"))
            else:
                # retrieved compaction settings
                self.set_result(True)
            self.state = FINISHED

        except GetBucketInfoFailed as e:
            # subsequent query failed! exit
            self.state = FINISHED
            self.set_exception(e)
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)


class MonitorActiveTask(Task):

    """
        Attempt to monitor active task that  is available in _active_tasks API.
        It allows to monitor indexer, bucket compaction.

        Execute function looks at _active_tasks API and tries to identifies task for monitoring
        and its pid by: task type('indexer' , 'bucket_compaction', 'view_compaction' )
        and target value (for example "_design/ddoc" for indexing, bucket "default" for bucket compaction or
        "_design/dev_view" for view compaction).
        wait_task=True means that task should be found in the first attempt otherwise,
        we can assume that the task has been completed( reached 100%).

        Check function monitors task by pid that was identified in execute func
        and matches new progress result with the previous.
        task is failed if:
            progress is not changed  during num_iterations iteration
            new progress was gotten less then previous
        task is passed and completed if:
            progress reached wait_progress value
            task was not found by pid(believe that it's over)
    """

    def __init__(self, server, type, target_value, wait_progress=100, num_iterations=100, wait_task=True):
        Task.__init__(self, "monitor_active_task")
        self.server = server
        self.type = type  # indexer or bucket_compaction
        self.target_key = ""
        if self.type == "indexer":
            self.target_key = "design_documents"
        elif self.type == "bucket_compaction":
            self.target_key = "original_target"
        elif self.type == "view_compaction":
            self.target_key = "design_documents"
        else:
            self.fail("type %s is not defined!" % self.type)
        self.target_value = target_value
        self.wait_progress = wait_progress
        self.num_iterations = num_iterations
        self.wait_task = wait_task

        self.rest = RestConnection(self.server)
        self.current_progress = None
        self.current_iter = 0
        self.task_pid = None


    def execute(self, task_manager):
        tasks = self.rest.active_tasks()
        print tasks
        for task in tasks:
            if task["type"] == self.type and ((
                        self.target_key == "design_documents" and task[self.target_key][0] == self.target_value) or (
                        self.target_key == "original_target" and task[self.target_key] == self.target_value)):
                self.current_progress = task["progress"]
                self.task_pid = task["pid"]
                self.log.info("monitoring active task was found:" + str(task))
                self.log.info("progress %s:%s - %s %%" % (self.type, self.target_value, task["progress"]))
                if self.current_progress >= self.wait_progress:
                    self.log.info("expected progress was gotten: %s" % self.current_progress)
                    self.state = FINISHED
                    self.set_result(True)

                else:
                    self.state = CHECKING
                    task_manager.schedule(self, 5)
                return
        if self.wait_task:
            #task is not performed
            self.state = FINISHED
            self.log.error("expected active task %s:%s was not found" % (self.type, self.target_value))
            self.set_result(False)
        else:
            #task was completed
            self.state = FINISHED
            self.log.info("task for monitoring %s:%s was not found" % (self.type, self.target_value))
            self.set_result(True)


    def check(self, task_manager):
        tasks = self.rest.active_tasks()
        for task in tasks:
            #if task still exists
            if task["pid"] == self.task_pid:
                self.log.info("progress %s:%s - %s %%" % (self.type, self.target_value, task["progress"]))
                #reached expected progress
                if task["progress"] >= self.wait_progress:
                        self.state = FINISHED
                        self.log.error("progress was reached %s" % self.wait_progress)
                        self.set_result(True)
                #progress value was changed
                if task["progress"] > self.current_progress:
                    self.current_progress = task["progress"]
                    self.currebt_iter = 0
                    task_manager.schedule(self, 2)
                #progress value was not changed
                elif task["progress"] == self.current_progress:
                    if self.current_iter < self.num_iterations:
                        time.sleep(2)
                        self.current_iter += 1
                        task_manager.schedule(self, 2)
                    #num iteration with the same progress = num_iterations
                    else:
                        self.state = FINISHED
                        self.log.error("progress for active task was not changed during %s sec" % 2 * self.num_iterations)
                        self.set_result(False)
                else:
                    self.state = FINISHED
                    self.log.error("progress for task %s:%s changed direction!" % (self.type, self.target_value))
                    self.set_result(False)

        #task was completed
        self.state = FINISHED
        self.log.info("task %s:%s was completed" % (self.type, self.target_value))
        self.set_result(True)


class MonitorViewFragmentationTask(Task):

    """
        Attempt to monitor fragmentation that is occurring for a given design_doc.
        execute stage is just for preliminary sanity checking of values and environment.

        Check function looks at index file accross all nodes and attempts to calculate
        total fragmentation occurring by the views within the design_doc.

        Note: If autocompaction is enabled and user attempts to monitor for fragmentation
        value higher than level at which auto_compaction kicks in a warning is sent and
        it is best user to use lower value as this can lead to infinite monitoring.
    """

    def __init__(self, server, design_doc_name, fragmentation_value=10, bucket="default"):

        Task.__init__(self, "monitor_frag_task")
        self.server = server
        self.bucket = bucket
        self.fragmentation_value = fragmentation_value
        self.design_doc_name = design_doc_name


    def execute(self, task_manager):

        # sanity check of fragmentation value
        if  self.fragmentation_value < 0 or self.fragmentation_value > 100:
            err_msg = \
                "Invalid value for fragmentation %d" % self.fragmentation_value
            self.state = FINISHED
            self.set_exception(Exception(err_msg))

        # warning if autocompaction is less than <fragmentation_value>
        try:
            auto_compact_percentage = self._get_current_auto_compaction_percentage()
            if auto_compact_percentage != "undefined" and auto_compact_percentage < self.fragmentation_value:
                self.log.warn("Auto compaction is set to %s. Therefore fragmentation_value %s may not be reached" % (auto_compact_percentage, self.fragmentation_value))

            self.state = CHECKING
            task_manager.schedule(self, 5)
        except GetBucketInfoFailed as e:
            self.state = FINISHED
            self.set_exception(e)
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)


    def _get_current_auto_compaction_percentage(self):
        """ check at bucket level and cluster level for compaction percentage """

        auto_compact_percentage = None
        rest = RestConnection(self.server)

        content = rest.get_bucket_json(self.bucket)
        if content["autoCompactionSettings"] == False:
            # try to read cluster level compaction settings
            content = rest.cluster_status()

        auto_compact_percentage = \
                content["autoCompactionSettings"]["viewFragmentationThreshold"]["percentage"]

        return auto_compact_percentage

    def check(self, task_manager):

        rest = RestConnection(self.server)
        new_frag_value = MonitorViewFragmentationTask.\
            calc_ddoc_fragmentation(rest, self.design_doc_name, bucket=self.bucket)

        self.log.info("current amount of fragmentation = %d" % new_frag_value)
        if new_frag_value > self.fragmentation_value:
            self.state = FINISHED
            self.set_result(True)
        else:
            # try again
            task_manager.schedule(self, 2)

    @staticmethod
    def aggregate_ddoc_info(rest, design_doc_name, bucket="default", with_new_nodes=False):

        nodes = rest.node_statuses()
        info = []
        for node in nodes:
            server_info = {"ip" : node.ip,
                           "port" : node.port,
                           "username" : rest.username,
                           "password" : rest.password}
            rest = RestConnection(server_info)
            try:
                status, content = rest.set_view_info(bucket, design_doc_name)
            except Exception, e:
                if "Error occured reading set_view _info" in e.message and with_new_nodes:
                    print("node {0} {1} is not ready yet?: {2}".format(
                                    node.id, node.port, e.message))
                    continue
                else:
                    print e.message
                    self.state = FINISHED
                    self.set_result(False)
                    return
            if status:
                info.append(content)
        return info

    @staticmethod
    def calc_ddoc_fragmentation(rest, design_doc_name, bucket="default", with_new_nodes=False):

        total_disk_size = 0
        total_data_size = 0
        total_fragmentation = 0

        nodes_ddoc_info = \
            MonitorViewFragmentationTask.aggregate_ddoc_info(rest,
                                                         design_doc_name,
                                                         bucket, with_new_nodes)
        total_disk_size = sum([content['disk_size'] for content in nodes_ddoc_info])
        total_data_size = sum([content['data_size'] for content in nodes_ddoc_info])

        if total_disk_size > 0 and total_data_size > 0:
            total_fragmentation = \
                (total_disk_size - total_data_size) / float(total_disk_size) * 100

        return total_fragmentation

class ViewCompactionTask(Task):

    """
        Executes view compaction for a given design doc. This is technicially view compaction
        as represented by the api and also because the fragmentation is generated by the
        keys emitted by map/reduce functions within views.  Task will check that compaction
        history for design doc is incremented and if any work was really done.
    """

    def __init__(self, server, design_doc_name, bucket="default", with_new_nodes=False):

        Task.__init__(self, "view_compaction_task")
        self.server = server
        self.bucket = bucket
        self.design_doc_name = design_doc_name
        self.ddoc_id = "_design%2f" + design_doc_name
        self.num_of_compactions = 0
        self.precompacted_frag_val = 0
        self.with_new_nodes = with_new_nodes

    def execute(self, task_manager):
        rest = RestConnection(self.server)

        try:
            self.num_of_compactions, self.precompacted_frag_val = \
                self._get_compaction_details()
            rest.ddoc_compaction(self.ddoc_id)
            self.state = CHECKING
            task_manager.schedule(self, 2)
        except (CompactViewFailed, SetViewInfoNotFound) as ex:
            self.state = FINISHED
            self.set_exception(ex)
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    # verify compaction history incremented and some defraging occurred
    def check(self, task_manager):

        try:
            new_compaction_count, compacted_frag_val = self._get_compaction_details()
            self.log.info("stats compaction: ({0},{1})".
                          format(new_compaction_count, compacted_frag_val))

            if new_compaction_count == self.num_of_compactions and self._is_compacting():
                # compaction ran successfully but compactions was not changed
                # perhaps we are still compacting
                self.log.info("design doc {0} is compacting".format(self.design_doc_name))
                task_manager.schedule(self, 2)
            elif new_compaction_count > self.num_of_compactions:
                frag_val_diff = compacted_frag_val - self.precompacted_frag_val
                self.log.info("fragmentation went from %d to %d" % \
                              (self.precompacted_frag_val, compacted_frag_val))

                if frag_val_diff > 0:
                    # compaction ran successfully but datasize still same
                    # perhaps we are still compacting
                    if self._is_compacting():
                        task_manager.schedule(self, 2)

                    # probably we already compacted, but no work needed to be done
                    # returning False
                    self.set_result(False)
                else:
                    self.set_result(True)
                    self.state = FINISHED
            else:
                #Sometimes the compacting is not started immediately
                time.sleep(5)
                if self._is_compacting():
                    task_manager.schedule(self, 2)
                else:
                    self.set_exception(Exception("Check system logs, looks like compaction failed to start"))

        except (SetViewInfoNotFound) as ex:
            self.state = FINISHED
            self.set_exception(ex)
        #catch and set all unexpected exceptions
        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    def _get_compaction_details(self):
        rest = RestConnection(self.server)
        status, content = rest.set_view_info(self.bucket, self.design_doc_name)
        curr_no_of_compactions = content["stats"]["compactions"]
        curr_ddoc_fragemtation = \
            MonitorViewFragmentationTask.calc_ddoc_fragmentation(rest, self.design_doc_name, self.bucket, self.with_new_nodes)
        return (curr_no_of_compactions, curr_ddoc_fragemtation)

    def _is_compacting(self):
        rest = RestConnection(self.server)
        status, content = rest.set_view_info(self.bucket, self.design_doc_name)
        return content["compact_running"] == True

'''task class for failover. This task will only failover nodes but doesn't
 rebalance as there is already a task to do that'''
class FailoverTask(Task):
    def __init__(self, servers, to_failover=[], wait_for_pending=20):
        Task.__init__(self, "failover_task")
        self.servers = servers
        self.to_failover = to_failover
        self.wait_for_pending = wait_for_pending

    def execute(self, task_manager):
        try:
            self._failover_nodes(task_manager)
            self.log.info("{0} seconds sleep after failover, for nodes to go pending....".format(self.wait_for_pending))
            time.sleep(self.wait_for_pending)
            self.state = FINISHED
            self.set_result(True)

        except FailoverFailedException as e:
            self.state = FINISHED
            self.set_exception(e)

        except Exception as e:
            self.state = FINISHED
            self.log.info("Unexpected Exception Caught")
            self.set_exception(e)

    def _failover_nodes(self, task_manager):
        rest = RestConnection(self.servers[0])
        #call REST fail_over for the nodes to be failed over
        for server in self.to_failover:
            for node in rest.node_statuses():
                if server.ip == node.ip and int(server.port) == int(node.port):
                    self.log.info("Failing over {0}:{1}".format(node.ip, node.port))
                    rest.fail_over(node.id)

class GenerateExpectedViewResultsTask(Task):

    """
        Task to produce the set of keys that are expected to be returned
        by querying the provided <view>.  Results can be later passed to
        ViewQueryVerificationTask and compared with actual results from
        server.

        Currently only views with map functions that emit a single string
        or integer as keys are accepted.

        Also NOTE, this task is to be used with doc_generators that
        produce json like documentgenerator.DocumentGenerator
    """
    def __init__(self, doc_generators, view, query):
        Task.__init__(self, "generate_view_query_results_task")
        self.doc_generators = doc_generators
        self.view = view
        self.query = query
        self.emitted_rows = []


    def execute(self, task_manager):
        self.generate_emitted_rows()
        self.filter_emitted_rows()
        self.log.info("Finished generating expected query results")
        self.state = CHECKING
        task_manager.schedule(self)

    def check(self, task_manager):
        self.state = FINISHED
        self.set_result(self.emitted_rows)


    def generate_emitted_rows(self):
        for doc_gen in self.doc_generators:

            query_doc_gen = copy.deepcopy(doc_gen)
            while query_doc_gen.has_next():
                emit_key = re.sub(r',.*', '', re.sub(r'.*emit\([ +]?doc\.', '', self.view.map_func))
                _id, val = query_doc_gen.next()
                val = json.loads(val)

                val_emit_key = val[emit_key]
                if isinstance(val_emit_key, unicode):
                    val_emit_key = val_emit_key.encode('utf-8')
                self.emitted_rows.append({'id' : _id, 'key' : val_emit_key})

    def filter_emitted_rows(self):

        query = self.query

        # parse query flags
        descending_set = 'descending' in query and query['descending'] == "true"
        startkey_set, endkey_set = 'startkey' in query, 'endkey' in query
        startkey_docid_set, endkey_docid_set = 'startkey_docid' in query, 'endkey_docid' in query
        inclusive_end_false = 'inclusive_end' in query and query['inclusive_end'] == "false"
        key_set = 'key' in query

        # sort expected results to match view results
        expected_rows = sorted(self.emitted_rows,
                               cmp=GenerateExpectedViewResultsTask.cmp_result_rows,
                               reverse=descending_set)

        # filter rows according to query flags
        if startkey_set:
            start_key = query['startkey']
        else:
            start_key = expected_rows[0]['key']
        if endkey_set:
            end_key = query['endkey']
        else:
            end_key = expected_rows[-1]['key']

        if descending_set:
            start_key, end_key = end_key, start_key

        if startkey_set or endkey_set:
            if isinstance(start_key, str):
                start_key = start_key.strip("\"")
            if isinstance(end_key, str):
                end_key = end_key.strip("\"")
            expected_rows = [row for row in expected_rows if row['key'] >= start_key and row['key'] <= end_key]

        if key_set:
            key_ = query['key']
            start_key, end_key = key_, key_
            expected_rows = [row for row in expected_rows if row['key'] == key_]


        if descending_set:
            startkey_docid_set, endkey_docid_set = endkey_docid_set, startkey_docid_set

        if startkey_docid_set:
            if not startkey_set:
                self.log.warn("Ignoring startkey_docid filter when startkey is not set")
            else:
                do_filter = False
                if descending_set:
                    if endkey_docid_set:
                        startkey_docid = query['endkey_docid']
                        do_filter = True
                else:
                    startkey_docid = query['startkey_docid']
                    do_filter = True

                if do_filter:
                    expected_rows = \
                        [row for row in expected_rows if row['id'] >= startkey_docid or row['key'] > start_key]

        if endkey_docid_set:
            if not endkey_set:
                self.log.warn("Ignoring endkey_docid filter when endkey is not set")
            else:
                do_filter = False
                if descending_set:
                    if endkey_docid_set:
                        endkey_docid = query['startkey_docid']
                        do_filter = True
                else:
                    endkey_docid = query['endkey_docid']
                    do_filter = True

                if do_filter:
                    expected_rows = \
                        [row for row in expected_rows if row['id'] <= endkey_docid or row['key'] < end_key]


        if inclusive_end_false:
            if endkey_set and not endkey_docid_set:
                # remove all keys that match endkey
                expected_rows = [row for row in expected_rows['key'] if row['key'] != end_key]
            else:
                # remove last key
                expected_rows = expected_rows[:-1]

        self.emitted_rows = expected_rows

    @staticmethod
    def cmp_result_rows(x, y):
        rc = cmp(x['key'], y['key'])
        if rc == 0:
            # sort by id is tie breaker
            rc = cmp(x['id'], y['id'])
        return rc

class ViewQueryVerificationTask(Task):

    """
        * query with stale=false
        * check for duplicates
        * check for missing docs
            * check memcached
            * check couch
    """
    def __init__(self, server, design_doc_name, view_name, query, expected_rows,
                num_verified_docs=20, bucket="default", query_timeout=120):

        Task.__init__(self, "view_query_verification_task")
        self.server = server
        self.design_doc_name = design_doc_name
        self.view_name = view_name
        self.query = query
        self.expected_rows = expected_rows
        self.num_verified_docs = num_verified_docs
        self.bucket = bucket
        self.query_timeout = query_timeout
        self.results = None

        for key in config:
            self.config[key] = config[key]

    def execute(self, task_manager):

        rest = RestConnection(self.server)

        try:
            # query for full view results
            self.query["stale"] = "false"
            self.query["reduce"] = "false"
            self.query["include_docs"] = "true"
            self.results = rest.query_view(self.design_doc_name, self.view_name,
                                           self.bucket, self.query, timeout=self.query_timeout)
        except QueryViewException as e:
            self.set_exception(e)
            self.state = FINISHED


        msg = "Checking view query results: (%d keys expected) vs (%d keys returned)" % \
            (len(self.expected_rows), len(self.results['rows']))
        self.log.info(msg)

        self.state = CHECKING
        task_manager.schedule(self)

    def check(self, task_manager):
        err_infos = []
        rc_status = {"passed" : False,
                     "errors" : err_infos}  # array of dicts with keys 'msg' and 'details'

        # create verification id lists
        expected_ids = [row['id'] for row in self.expected_rows]
        couch_ids = [str(row['id']) for row in self.results['rows']]

        # check results
        self.check_for_duplicate_ids(expected_ids, couch_ids, err_infos)
        self.check_for_missing_ids(expected_ids, couch_ids, err_infos)
        self.check_for_value_corruption(err_infos)

        # check for errors
        if len(rc_status["errors"]) == 0:
           rc_status["passed"] = True

        self.state = FINISHED
        self.set_result(rc_status)

    def check_for_duplicate_ids(self, expected_ids, couch_ids, err_infos):

        extra_id_set = set(couch_ids) - set(expected_ids)

        seen = set()
        for id in couch_ids:
            if id in seen and id not in extra_id_set:
                extra_id_set.add(id)
            else:
                seen.add(id)

        if len(extra_id_set) > 0:
            # extra/duplicate id verification
            dupe_rows = [row for row in self.results['rows'] if row['id'] in extra_id_set]
            err = { "msg" : "duplicate rows found in query results",
                    "details" : dupe_rows }
            err_infos.append(err)

    def check_for_missing_ids(self, expected_ids, couch_ids, err_infos):

        missing_id_set = set(expected_ids) - set(couch_ids)

        if len(missing_id_set) > 0:

            missing_id_errors = self.debug_missing_items(missing_id_set)

            if len(missing_id_errors) > 0:
                err = { "msg" : "missing ids from memcached",
                        "details" : missing_id_errors}
                err_infos.append(err)


    def check_for_value_corruption(self, err_infos):

        if self.num_verified_docs > 0:

            doc_integrity_errors = self.include_doc_integrity()

            if len(doc_integrity_errors) > 0:
                err = { "msg" : "missmatch in document values",
                        "details" : doc_integrity_errors }
                err_infos.append(err)


    def debug_missing_items(self, missing_id_set):
        rest = RestConnection(self.server)
        client = KVStoreAwareSmartClient(rest, self.bucket)
        missing_id_errors = []

        # debug missing documents
        for doc_id in list(missing_id_set)[:self.num_verified_docs]:

            # attempt to retrieve doc from memcached
            mc_item = client.mc_get_full(doc_id)
            if mc_item == None:
                missing_id_errors.append("document %s missing from memcached" % (doc_id))

            # attempt to retrieve doc from disk
            else:

                num_vbuckets = len(rest.get_vbuckets(self.bucket))
                doc_meta = client.get_doc_metadata(num_vbuckets, doc_id)

                if(doc_meta != None):
                    if (doc_meta['key_valid'] != 'valid'):
                        msg = "Error expected in results for key with invalid state %s" % doc_meta
                        missing_id_errors.append(msg)

                else:
                    msg = "query doc_id: %s doesn't exist in bucket: %s" % \
                        (doc_id, self.bucket)
                    missing_id_errors.append(msg)

            if(len(missing_id_errors) == 0):
                msg = "view engine failed to index doc [%s] in query: %s" % (doc_id, self.query)
                missing_id_errors.append(msg)

            return missing_id_errors

    def include_doc_integrity(self):
        rest = RestConnection(self.server)
        client = KVStoreAwareSmartClient(rest, self.bucket)
        doc_integrity_errors = []

        exp_verify_set = [row['doc'] for row in\
            self.results['rows'][:self.num_verified_docs]]

        for view_doc in exp_verify_set:
            doc_id = str(view_doc['_id'])
            mc_item = client.mc_get_full(doc_id)

            if mc_item is not None:
                mc_doc = json.loads(mc_item["value"])

                # compare doc content
                for key in mc_doc.keys():
                    if(mc_doc[key] != view_doc[key]):
                        err_msg = \
                            "error verifying document id %s: retrieved value %s expected %s \n" % \
                                (doc_id, mc_doc[key], view_doc[key])
                        doc_integrity_errors.append(err_msg)
            else:
                doc_integrity_errors.append("doc_id %s could not be retrieved for verification \n" % doc_id)

        return doc_integrity_errors

class BucketFlushTask(Task):
    def __init__(self, server, bucket="default"):
        Task.__init__(self, "bucket_flush_task")
        self.server = server
        self.bucket = bucket
        if isinstance(bucket, Bucket):
            self.bucket = bucket.name

    def execute(self, task_manager):
        rest = RestConnection(self.server)
        try:
            if rest.flush_bucket(self.bucket):
                self.state = CHECKING
                task_manager.schedule(self)
            else:
                self.state = FINISHED
                self.set_result(False)

        except BucketFlushFailed as e:
            self.state = FINISHED
            self.set_exception(e)

        except Exception as e:
            self.state = FINISHED
            self.log.error("Unexpected Exception Caught")
            self.set_exception(e)

    def check(self, task_manager):
        try:
            #check if after flush the vbuckets are ready
            if BucketOperationHelper.wait_for_vbuckets_ready_state(self.server, self.bucket):
                self.set_result(True)
            else:
                self.log.error("Unable to reach bucket {0} on server {1} after flush".format(self.bucket, self.server))
                self.set_result(False)
            self.state = FINISHED
        except Exception as e:
            self.state = FINISHED
            self.log.error("Unexpected Exception Caught")
            self.set_exception(e)
