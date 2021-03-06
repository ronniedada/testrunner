import base64
import json
import urllib
import httplib2
import socket
import time
import logger
from couchbase.document import DesignDocument, View
from memcached.helper.kvstore import KVStore
from exception import ServerAlreadyJoinedException, ServerUnavailableException, InvalidArgumentException
from membase.api.exception import BucketCreationException, ServerSelfJoinException, ClusterRemoteException, \
    RebalanceFailedException, FailoverFailedException, DesignDocCreationException, QueryViewException, \
    ReadDocumentException, GetBucketInfoFailed, CompactViewFailed, SetViewInfoNotFound, AddNodeException, BucketFlushFailed
log = logger.Logger.get_logger()
#helper library methods built on top of RestConnection interface

class RestHelper(object):
    def __init__(self, rest_connection):
        self.rest = rest_connection

    def is_ns_server_running(self, timeout_in_seconds=360):
        end_time = time.time() + timeout_in_seconds
        while time.time() <= end_time:
            try:
                status = self.rest.get_nodes_self(5)
                if status is not None and status.status == 'healthy':
                    return True
            except ServerUnavailableException:
                log.error("server {0}:{1} is unavailable".format(self.rest.ip, self.rest.port))
                time.sleep(1)
        msg = 'unable to connect to the node {0} even after waiting {1} seconds'
        log.info(msg.format(self.rest.ip, timeout_in_seconds))
        return False

    def is_cluster_healthy(self, timeout=120):
        #get the nodes and verify that all the nodes.status are healthy
        nodes = self.rest.node_statuses(timeout)
        return all(node.status == 'healthy' for node in nodes)

    def rebalance_reached(self, percentage=100, track_hangs=False):
        start = time.time()
        progress = 0
        previous_progress = 0
        retry = 0
        while progress is not -1 and progress < percentage and retry < 20:
            #-1 is error , -100 means could not retrieve progress
            progress = self.rest._rebalance_progress()
            if progress == -100:
                log.error("unable to retrieve rebalanceProgress.try again in 2 seconds")
                retry += 1
            else:
                if previous_progress == progress:
                    retry += 0.5
                else:
                    retry = 0
                    previous_progress = progress
            #sleep for 2 seconds
            time.sleep(2)
        if progress < 0:
            log.error("rebalance progress code : {0}".format(progress))
            return False
        else:
            duration = time.time() - start
            log.info('rebalance reached >{0}% in {1} seconds '.format(progress, duration))
            return True


    def is_cluster_rebalanced(self):
        #get the nodes and verify that all the nodes.status are healthy
        return self.rest.rebalance_statuses()


    #this method will rebalance the cluster by passing the remote_node as
    #ejected node
    def remove_nodes(self, knownNodes, ejectedNodes, wait_for_rebalance=True):
        if len(ejectedNodes) == 0:
            return False
        self.rest.rebalance(knownNodes, ejectedNodes)
        if wait_for_rebalance:
            return self.rest.monitorRebalance()
        else:
            return False

    def vbucket_map_ready(self, bucket, timeout_in_seconds=360):
        end_time = time.time() + timeout_in_seconds
        while time.time() <= end_time:
            vBuckets = self.rest.get_vbuckets(bucket)
            if vBuckets:
                return True
            else:
                time.sleep(0.5)
        msg = 'vbucket map is not ready for bucket {0} after waiting {1} seconds'
        log.info(msg.format(bucket, timeout_in_seconds))
        return False


    def bucket_exists(self, bucket):
        try:
            buckets = self.rest.get_buckets()
            names = [item.name for item in buckets]
            log.info("existing buckets : {0}".format(names))
            for item in buckets:
                if item.name == bucket:
                    log.info("found bucket {0}".format(bucket))
                    return True
            return False
        except Exception:
            return False


    def wait_for_node_status(self, node, expected_status, timeout_in_seconds):
        status_reached = False
        end_time = time.time() + timeout_in_seconds
        while time.time() <= end_time and not status_reached:
            nodes = self.rest.node_statuses()
            for n in nodes:
                if node.id == n.id:
                    log.info('node {0} status : {1}'.format(node.id, n.status))
                    if n.status.lower() == expected_status.lower():
                        status_reached = True
                    break
            if not status_reached:
                log.info("sleep for 5 seconds before reading the node.status again")
                time.sleep(5)
        log.info('node {0} status_reached : {1}'.format(node.id, status_reached))
        return status_reached


    def wait_for_replication(self, timeout_in_seconds=120):
        wait_count = 0
        end_time = time.time() + timeout_in_seconds
        while time.time() <= end_time:
            if self.all_nodes_replicated():
                break
            wait_count += 1
            if wait_count == 10:
                log.info('replication state : {0}'.format(self.all_nodes_replicated(debug=True)))
                wait_count = 0
            time.sleep(5)
        log.info('replication state : {0}'.format(self.all_nodes_replicated()))
        return self.all_nodes_replicated()


    def all_nodes_replicated(self, debug=False):
        replicated = True
        nodes = self.rest.node_statuses()
        for node in nodes:
            if debug:
                log.info("node {0} replication state : {1}".format(node.id, node.replication))
            if node.replication != 1.0:
                replicated = False
        return replicated


class RestConnection(object):
    #port is always 8091
    # deprecated, should be removed in the future?
    def __init__(self, ip, username='Administrator', password='password'):
        #throw some error here if the ip is null ?
        self.ip = ip
        self.username = username
        self.password = password
        self.port = 8091
        self.baseUrl = "http://{0}:{1}/".format(self.ip, self.port)
        self.capiBaseUrl = "http://{0}:{1}/".format(self.ip, 8092)
        for iteration in xrange(3):
            http_res, success = self.init_http_request(self.baseUrl + 'nodes/self')
            if not success and type(http_res) == str and\
               http_res.find('Node is unknown to this cluster') > -1:
                log.error("Error 'Node is unknown to this cluster' was gotten,\
                    5 seconds sleep before retry")
                time.sleep(5)
                continue
            else:
                break
        #if node was not initialized we can't get response from 'nodes/self'
        if not http_res or http_res["version"][0:2] == "1.":
            self.capiBaseUrl = self.baseUrl + "/couchBase"
        else:
            try:
                self.capiBaseUrl = http_res["couchApiBase"]
            except Exception as e:
                log.error("unexpected response was gotten: %s " % http_res)
                raise Exception(e)


    def __init__(self, serverInfo):
        #serverInfo can be a json object
        if isinstance(serverInfo, dict):
            self.ip = serverInfo["ip"]
            self.username = serverInfo["username"]
            self.password = serverInfo["password"]
            self.port = serverInfo["port"]
        else:
            self.ip = serverInfo.ip
            self.username = serverInfo.rest_username
            self.password = serverInfo.rest_password
            self.port = serverInfo.port
        self.baseUrl = "http://{0}:{1}/".format(self.ip, self.port)
        self.capiBaseUrl = "http://{0}:{1}/".format(self.ip, 8092)
        #for Node is unknown to this cluster error
        #determine the real couchApiBase for cluster_run
        for iteration in xrange(3):
            http_res, success = self.init_http_request(self.baseUrl + 'nodes/self')
            if not success and type(http_res) == str and\
               http_res.find('Node is unknown to this cluster') > -1:
                log.error("Error 'Node is unknown to this cluster' was gotten,\
                    5 seconds sleep before retry")
                time.sleep(5)
                continue
            else:
                break
        #couchApiBase appeared in version 2.*
        try:
            if not http_res or http_res["version"][0:2] == "1.":
                self.capiBaseUrl = self.baseUrl + "/couchBase"
            else:
                self.capiBaseUrl = http_res["couchApiBase"]
        except Exception as e:
            log.error("unexpected response was gotten: %s " % http_res)
            raise Exception(e)


    def init_http_request(self, api):
        try:
            status, content, header = self._http_request(api, 'GET', headers=self._create_capi_headers_with_auth(self.username, self.password))
            json_parsed = json.loads(content)
            if status in ['200', '201', '202']:
                return json_parsed, True
            else:
                return json_parsed, False
        except ValueError:
            return content, False

    def active_tasks(self):
        api = self.capiBaseUrl + "_active_tasks"

        try:
            status, content, header = self._http_request(api, 'GET', headers=self._create_capi_headers())
            json_parsed = json.loads(content)
        except ValueError:
            return ""

        return json_parsed

    def ns_server_tasks(self):
        api = self.baseUrl + 'pools/default/tasks'

        try:
            status, content, header = self._http_request(api, 'GET', headers=self._create_headers())
            return json.loads(content)
        except ValueError:
            return ""

    # DEPRECATED: use create_ddoc() instead.
    def create_view(self, design_doc_name, bucket_name, views):
        return self.create_ddoc(design_doc_name, bucket_name, views)

    def create_ddoc(self, design_doc_name, bucket, views):
        design_doc = DesignDocument(design_doc_name, views)
        return self.create_design_document(bucket, design_doc)

    def create_design_document(self, bucket, design_doc):
        design_doc_name = design_doc.id
        api = '%s/%s/%s' % (self.capiBaseUrl, bucket, design_doc_name)
        if isinstance(bucket, Bucket):
            api = '%s/%s/%s' % (self.capiBaseUrl, bucket.name, design_doc_name)

        if isinstance(bucket, Bucket) and bucket.authType == "sasl":
            status, content, header = self._http_request(api, 'PUT', str(design_doc),
                                                headers=self._create_capi_headers_with_auth(
                                                username=bucket.name, password=bucket.saslPassword))
        else:
            status, content, header = self._http_request(api, 'PUT', str(design_doc),
                                                 headers=self._create_capi_headers())
        if not status:
            raise DesignDocCreationException(design_doc_name, content)
        return json.loads(content)


    def query_view(self, design_doc_name, view_name, bucket, query, timeout=120, invalid_query=False):
        status, content = self._query(design_doc_name, view_name, bucket, "view", query, timeout)
        if not status and not invalid_query:
            raise QueryViewException(view_name, content)
        return json.loads(content)

    def _query(self, design_doc_name, view_name, bucket, view_type, query, timeout):
        api = self.capiBaseUrl + '%s/_design/%s/_%s/%s?%s' % (bucket,
                                               design_doc_name, view_type,
                                               view_name,
                                               urllib.urlencode(query))
        if isinstance(bucket, Bucket):
            api = self.capiBaseUrl + '%s/_design/%s/_%s/%s?%s' % (bucket.name,
                                                  design_doc_name, view_type,
                                                  view_name,
                                                  urllib.urlencode(query))
        log.info("index query url: {0}".format(api))
        if isinstance(bucket, Bucket) and bucket.authType == "sasl":
            status, content, header = self._http_request(api, headers=self._create_capi_headers_with_auth(
                                                username=bucket.name, password=bucket.saslPassword),
                                                timeout=timeout)
        else:
            status, content, header = self._http_request(api, headers=self._create_capi_headers(),
                                             timeout=timeout)
        return status, content

    def view_results(self, bucket, ddoc_name, params, limit=100, timeout=120,
                     view_name=None):
        status, json = self._index_results(bucket, "view", ddoc_name, params, limit, timeout=timeout, view_name=view_name)
        if not status:
            raise Exception("unable to obtain view results")
        return json


    # DEPRECATED: Incorrectly named function kept for backwards compatibility.
    def get_view(self, bucket, view):
        log.info("DEPRECATED function get_view(" + view + "). use get_ddoc()")
        return self.get_ddoc(bucket, view)


    def get_ddoc(self, bucket, ddoc_name):
        status, json, meta = self._get_design_doc(bucket, ddoc_name)

        if not status:
            raise ReadDocumentException(ddoc_name, json)

        return json, meta

    #the same as Preview a Random Document on UI
    def get_random_key(self, bucket):
        api = self.baseUrl + 'pools/default/buckets/%s/localRandomKey' % (bucket)
        status, content, header = self._http_request(api, headers=self._create_capi_headers())
        json_parsed = json.loads(content)

        if not status:
            raise Exception("unable to get random document/key for bucket %s" % (bucket))
        return json_parsed


    def run_view(self, bucket, view, name):
        api = self.capiBaseUrl + '/%s/_design/%s/_view/%s' % (bucket, view, name)

        status, content, header = self._http_request(api, headers=self._create_capi_headers())

        json_parsed = json.loads(content)

        if not status:
            raise Exception("unable to create view")

        return json_parsed


    def delete_view(self, bucket, view):
        status, json = self._delete_design_doc(bucket, view)

        if not status:
            raise Exception("unable to delete the view")

        return json


    def spatial_results(self, bucket, spatial, params, limit=100):
        status, json = self._index_results(bucket, "spatial", spatial,
                                           params, limit)

        if not status:
            raise Exception("unable to obtain spatial view results")

        return json


    def create_spatial(self, bucket, spatial, function):
        status, json = self._create_design_doc(bucket, spatial, function)

        if status == False:
            raise Exception("unable to create spatial view")

        return json


    def get_spatial(self, bucket, spatial):
        status, json, meta = self._get_design_doc(bucket, spatial)

        if not status:
            raise Exception("unable to get the spatial view definition")

        return json, meta


    def delete_spatial(self, bucket, spatial):
        status, json = self._delete_design_doc(bucket, spatial)

        if not status:
            raise Exception("unable to delete the spatial view")

        return json


    # type_ is "view" or "spatial"
    def _index_results(self, bucket, type_, ddoc_name, params, limit, timeout=120,
                       view_name=None):
        if type_ == 'all_docs':
            api = self.capiBaseUrl + '/{0}/_all_docs'.format(bucket)
        else:
            if view_name is None:
                view_name = ddoc_name
            query = '/{0}/_design/{1}/_{2}/{3}'
            api = self.capiBaseUrl + query.format(bucket, ddoc_name, type_, view_name)

        num_params = 0
        if limit != None:
            num_params = 1
            api += "?limit={0}".format(limit)
        for param in params:
            if num_params > 0:
                api += "&"
            else:
                api += "?"
            num_params += 1

            if param in ["key", "startkey", "endkey"] or isinstance(params[param], bool):
                api += "{0}={1}".format(param, json.dumps(params[param]))
            else:
                api += "{0}={1}".format(param, params[param])

        log.info("index query url: {0}".format(api))
        status, content, header = self._http_request(api, headers=self._create_capi_headers(), timeout=timeout)

        json_parsed = json.loads(content)

        return status, json_parsed

    def all_docs(self, bucket, params={}, limit=None, timeout=120):
        api = self.capiBaseUrl + '/{0}/_all_docs?{1}'.format(bucket, urllib.urlencode(params))
        log.info("query all_docs url: {0}".format(api))

        status, content, header = self._http_request(api, headers=self._create_capi_headers(),
                                             timeout=timeout)

        if not status:
            raise Exception("unable to obtain all docs")

        return  json.loads(content)


    def get_couch_doc(self, doc_id, bucket="default", timeout=120):
        """ use couchBase uri to retrieve document from a bucket """

        api = self.capiBaseUrl + '/%s/%s' % (bucket, doc_id)
        status, content, header = self._http_request(api, headers=self._create_capi_headers(),
                                             timeout=timeout)

        if not status:
            raise ReadDocumentException(doc_id, content)

        return  json.loads(content)

    def _create_design_doc(self, bucket, name, function):
        api = self.capiBaseUrl + '/%s/_design/%s' % (bucket, name)
        status, content, header = self._http_request(
            api, 'PUT', function, headers=self._create_capi_headers())
        json_parsed = json.loads(content)
        return status, json_parsed


    def _get_design_doc(self, bucket, name):
        api = self.capiBaseUrl + '/%s/_design/%s' % (bucket, name)
        if isinstance(bucket, Bucket):
            api = self.capiBaseUrl + '/%s/_design/%s' % (bucket.name, name)

        if isinstance(bucket, Bucket) and bucket.authType == "sasl" and bucket.name != "default":
            status, content, header = self._http_request(api, headers=self._create_capi_headers_with_auth(
                                                username=bucket.name, password=bucket.saslPassword))
        else:
            status, content, header = self._http_request(api, headers=self._create_capi_headers())
        json_parsed = json.loads(content)
        meta_parsed = ""
        if status:
            meta = header['x-couchbase-meta']
            meta_parsed = json.loads(meta)
        return status, json_parsed, meta_parsed

    def _delete_design_doc(self, bucket, name):
        status, design_doc, meta = self._get_design_doc(bucket, name)
        if not status:
            raise Exception("unable to delete design document")

        api = self.capiBaseUrl + '/%s/_design/%s' % (bucket, name)
        if isinstance(bucket, Bucket):
            api = self.capiBaseUrl + '/%s/_design/%s' % (bucket.name, name)

        if isinstance(bucket, Bucket) and bucket.authType == "sasl" and bucket.name != "default":
            status, content, header = self._http_request(api, 'DELETE', headers=self._create_capi_headers_with_auth(
                                                username=bucket.name, password=bucket.saslPassword))
        else:
            status, content, header = self._http_request(api, 'DELETE', headers=self._create_capi_headers())

        json_parsed = json.loads(content)

        return status, json_parsed


    def spatial_compaction(self, bucket, design_name):
        api = self.capiBaseUrl + '/%s/_design/%s/_spatial/_compact' % (bucket, design_name)
        if isinstance(bucket, Bucket):
            api = self.capiBaseUrl + \
            '/%s/_design/%s/_spatial/_compact' % (bucket.name, design_name)

        if isinstance(bucket, Bucket) and bucket.authType == "sasl":
            status, content, header = self._http_request(api, 'POST', headers=self._create_capi_headers_with_auth(
                                                username=bucket.name, password=bucket.saslPassword))
        else:
            status, content, header = self._http_request(api, 'POST', headers=self._create_capi_headers())

        json_parsed = json.loads(content)

        return status, json_parsed

    # Make a _design/_info request
    def set_view_info(self, bucket, design_name):
        """Get view diagnostic info (node specific)"""
        api = self.capiBaseUrl
        if isinstance(bucket, Bucket):
            api += '/_set_view/{0}/_design/{1}/_info'.format(bucket, design_name)
        else:
            api += '_set_view/{0}/_design/{1}/_info'.format(bucket, design_name)

        if isinstance(bucket, Bucket) and bucket.authType == "sasl":
            headers = self._create_capi_headers_with_auth(
                username=bucket.name, password=bucket.saslPassword)
            status, content, header = self._http_request(api, 'POST',
                                                         headers=headers)
        else:
            headers = self._create_capi_headers()
            status, content, header = self._http_request(api, 'GET',
                                                         headers=headers)

        if not status:
            raise SetViewInfoNotFound(design_name, content)
        json_parsed = json.loads(content)
        return status, json_parsed

    # Make a _spatial/_info request
    def spatial_info(self, bucket, design_name):
        api = self.capiBaseUrl + \
            '/%s/_design/%s/_spatial/_info' % (bucket, design_name)

        status, content, header = self._http_request(
            api, 'GET', headers=self._create_capi_headers())

        json_parsed = json.loads(content)

        return status, json_parsed


    def _create_capi_headers(self):
        return {'Content-Type': 'application/json',
                'Accept': '*/*'}

    def _create_capi_headers_with_auth(self, username, password):
        authorization = base64.encodestring('%s:%s' % (username, password))
        return {'Content-Type': 'application/json',
                'Authorization': 'Basic %s' % authorization,
                'Accept': '*/*'}

    #authorization must be a base64 string of username:password
    def _create_headers(self):
        authorization = base64.encodestring('%s:%s' % (self.username, self.password))
        return {'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': 'Basic %s' % authorization,
                'Accept': '*/*'}


    def _http_request(self, api, method='GET', params='', headers=None, timeout=120):
        if not headers:
            headers = self._create_headers()

        end_time = time.time() + timeout
        while True:
            try:
                response, content = httplib2.Http(timeout=timeout).request(api, method, params, headers)
                if response['status'] in ['200', '201', '202']:
                    return True, content, response
                else:
                    try:
                        json_parsed = json.loads(content)
                    except ValueError as e:
                        json_parsed = {}
                        json_parsed["error"] = "status: {0}, content: {1}".format(response['status'], content)
                    reason = "unknown"
                    if "error" in json_parsed:
                        reason = json_parsed["error"]
                    log.error('{0} error {1} reason: {2} {3}'.format(api, response['status'], reason, content.rstrip('\n')))
                    return False, content, response
            except socket.error as e:
                log.error("socket error while connecting to {0} error {1} ".format(api, e))
                if time.time() > end_time:
                    raise ServerUnavailableException(ip=self.ip)
            except httplib2.ServerNotFoundError as e:
                log.error("ServerNotFoundError error while connecting to {0} error {1} ".format(api, e))
                if time.time() > end_time:
                    raise ServerUnavailableException(ip=self.ip)
            time.sleep(1)


    def init_cluster(self, username='Administrator', password='password'):
        api = self.baseUrl + 'settings/web'
        params = urllib.urlencode({'port': "8091",
                                   'username': username,
                                   'password': password})

        log.info('settings/web params : {0}'.format(params))

        status, content, header = self._http_request(api, 'POST', params)
        return status


    def init_cluster_port(self, username='Administrator', password='password'):
        api = self.baseUrl + 'settings/web'
        params = urllib.urlencode({'port': '8091',
                                   'username': username,
                                   'password': password})

        log.info('settings/web params : {0}'.format(params))

        status, content, header = self._http_request(api, 'POST', params)
        return status


    def init_cluster_memoryQuota(self, username='Administrator',
                                 password='password',
                                 memoryQuota=256):
        api = self.baseUrl + 'pools/default'
        params = urllib.urlencode({'memoryQuota': memoryQuota,
                                   'username': username,
                                   'password': password})

        log.info('pools/default params : {0}'.format(params))

        status, content, header = self._http_request(api, 'POST', params)
        return status

    def add_remote_cluster(self, remoteIp, remotePort, username, password, name):
        #example : password:password username:Administrator hostname:127.0.0.1:9002 name:two
        msg = "adding remote cluster hostname:{0}:{1} with username:password {2}:{3} name:{4}"
        log.info(msg.format(remoteIp, remotePort, username, password, name))
        api = self.baseUrl + 'pools/default/remoteClusters'
        params = urllib.urlencode({'hostname': "{0}:{1}".format(remoteIp, remotePort),
                                   'username': username,
                                   'password': password,
                                   'name':name})

        status, content, header = self._http_request(api, 'POST', params)
        #sample response :
        # [{"name":"two","uri":"/pools/default/remoteClusters/two","validateURI":"/pools/default/remoteClusters/two?just_validate=1","hostname":"127.0.0.1:9002","username":"Administrator"}]
        if status:
            json_parsed = json.loads(content)
            remoteCluster = json_parsed
        else:
            log.error("/remoteCluster failed : status:{0},content:{1}".format(status, content))
            raise Exception("remoteCluster API 'add cluster' failed")

        return remoteCluster

    def get_remote_clusters(self):
        remote_clusters = []
        api = self.baseUrl + 'pools/default/remoteClusters/'
        params = urllib.urlencode({})
        status, content, header = self._http_request(api, 'GET', params)
        if status:
            remote_clusters = json.loads(content)
        return remote_clusters


    def remove_all_remote_clusters(self):
        remote_clusters = self.get_remote_clusters()
        for remote_cluster in remote_clusters:
            if remote_cluster["deleted"] == False:
                self.remove_remote_cluster(remote_cluster["name"])



    def remove_remote_cluster(self, name):
        #example : name:two
        msg = "removing remote cluster name:{0}".format(name)
        log.info(msg)
        api = self.baseUrl + 'pools/default/remoteClusters/{0}'.format(name)
        params = urllib.urlencode({})
        status, content, header = self._http_request(api, 'DELETE', params)
        #sample response :
        # [{"name":"two","uri":"/pools/default/remoteClusters/two","validateURI":"/pools/default/remoteClusters/two?just_validate=1","hostname":"127.0.0.1:9002","username":"Administrator"}]
        if status:
            json_parsed = json.loads(content)
        else:
            log.error("failed to remove remote cluster: status:{0},content:{1}".format(status, content))
            raise Exception("remoteCluster API 'remove cluster' failed")

        return json_parsed


    #replicationType:continuous toBucket:default toCluster:two fromBucket:default
    def start_replication(self, replicationType, fromBucket, toCluster, toBucket=None):
        toBucket = toBucket or fromBucket

        msg = "starting replication type:{0} from {1} to {2} in the remote cluster {3}"
        create_replication_response = {}
        log.info(msg.format(replicationType, fromBucket, toBucket, toCluster))
        api = self.baseUrl + 'controller/createReplication'
        params = urllib.urlencode({'replicationType': replicationType,
                                   'toBucket': toBucket,
                                   'fromBucket': fromBucket,
                                   'toCluster':toCluster})

        status, content, header = self._http_request(api, 'POST', params)
        #respone : {"database":"http://127.0.0.1:9500/_replicator",
        # "id": "replication_id"}
        if status:
            json_parsed = json.loads(content)
            return (json_parsed['database'], json_parsed['id'])
        else:
            log.error("/controller/createReplication failed : status:{0},content:{1}".format(status, content))
            raise Exception("create replication failed : status:{0},content:{1}".format(status, content))


    def get_replications(self):
        replications = []
        api = self.capiBaseUrl + '_replicator/_design/_replicator_info/_view/infos?group_level=1'
        params = urllib.urlencode({})
        status, content, header = self._http_request(api, 'GET', params)
        if status:
            replications = json.loads(content)["rows"]
        return replications


    def remove_all_replications(self):
        replications = self.get_replications()
        for replication in replications:
            if replication["value"]["have_replicator_doc"] == True:
                self.stop_replication(self.capiBaseUrl + '_replicator', replication["value"]["replication_fields"]["_id"].replace("/", "%2F"))

    def stop_replication(self, database, rep_id):
        self._http_request(database + "/{0}".format(rep_id), 'DELETE', None, self._create_capi_headers())


    #params serverIp : the server to add to this cluster
    #raises exceptions when
    #unauthorized user
    #server unreachable
    #can't add the node to itself ( TODO )
    #server already added
    #returns otpNode
    def add_node(self, user='', password='', remoteIp='', port='8091'):
        otpNode = None
        log.info('adding remote node : {0} to this cluster @ : {1}'\
        .format(remoteIp, self.ip))
        api = self.baseUrl + 'controller/addNode'
        params = urllib.urlencode({'hostname': "{0}:{1}".format(remoteIp, port),
                                   'user': user,
                                   'password': password})

        status, content, header = self._http_request(api, 'POST', params)

        if status:
            json_parsed = json.loads(content)
            otpNodeId = json_parsed['otpNode']
            otpNode = OtpNode(otpNodeId)
            if otpNode.ip == '127.0.0.1':
                otpNode.ip = self.ip
        else:
            if content.find('Prepare join failed. Node is already part of cluster') >= 0:
                raise ServerAlreadyJoinedException(nodeIp=self.ip,
                                                   remoteIp=remoteIp)
            elif content.find('Prepare join failed. Joining node to itself is not allowed') >= 0:
                raise ServerSelfJoinException(nodeIp=self.ip,
                                          remoteIp=remoteIp)
            else:
                log.error('add_node error : {0}'.format(content))
                raise AddNodeException(nodeIp=self.ip,
                                          remoteIp=remoteIp,
                                          reason=content)
        return otpNode


    def eject_node(self, user='', password='', otpNode=None):
        if not otpNode:
            log.error('otpNode parameter required')
            return False

        api = self.baseUrl + 'controller/ejectNode'
        params = urllib.urlencode({'otpNode': otpNode,
                                   'user': user,
                                   'password': password})

        status, content, header = self._http_request(api, 'POST', params)

        if status:
            log.info('ejectNode successful')
        else:
            if content.find('Prepare join failed. Node is already part of cluster') >= 0:
                raise ServerAlreadyJoinedException(nodeIp=self.ip,
                                                   remoteIp=otpNode)
            else:
                # todo : raise an exception here
                log.error('eject_node error {0}'.format(content))
        return True

    def fail_over(self, otpNode=None):
        if otpNode is None:
            log.error('otpNode parameter required')
            return False

        api = self.baseUrl + 'controller/failOver'
        params = urllib.urlencode({'otpNode': otpNode})

        status, content, header = self._http_request(api, 'POST', params)

        if status:
            log.info('fail_over successful')
        else:
            log.error('fail_over error : {0}'.format(content))
            raise FailoverFailedException(content)

        return status

    def add_back_node(self, otpNode=None):
        if otpNode is None:
            log.error('otpNode parameter required')
            return False

        api = self.baseUrl + 'controller/reAddNode'
        params = urllib.urlencode({'otpNode': otpNode})

        status, content, header = self._http_request(api, 'POST', params)

        if status:
            log.info('add_back_node successful')
        else:
            log.error('add_back_node error : {0}'.format(content))
            raise InvalidArgumentException('controller/reAddNode',
                                           parameters=params)

        return status

    def rebalance(self, otpNodes, ejectedNodes):
        knownNodes = ','.join(otpNodes)
        ejectedNodesString = ','.join(ejectedNodes)

        params = urllib.urlencode({'knownNodes': knownNodes,
                                   'ejectedNodes': ejectedNodesString,
                                   'user': self.username,
                                   'password': self.password})
        log.info('rebalance params : {0}'.format(params))

        api = self.baseUrl + "controller/rebalance"

        status, content, header = self._http_request(api, 'POST', params)

        if status:
            log.info('rebalance operation started')
        else:
            log.error('rebalance operation failed: {0}'.format(content))
            #extract the error
            raise InvalidArgumentException('controller/rebalance',
                                           parameters=params)

        return status

    def diag_eval(self, code):
        api = '{0}{1}'.format(self.baseUrl, 'diag/eval/')
        status, content, header = self._http_request(api, "POST", code)
        log.info("/diag/eval status: {0} content: {1} command: {2}".format(status, content, code))
        return status, content


    def monitorRebalance(self, stop_if_loop=False):
        start = time.time()
        progress = 0
        retry = 0
        same_progress_count = 0
        previous_progress = 0
        while progress != -1 and (progress != 100 or self._rebalance_progress_status() == 'running') and retry < 20:
            #-1 is error , -100 means could not retrieve progress
            progress = self._rebalance_progress()
            if progress == -100:
                log.error("unable to retrieve rebalanceProgress.try again in 1 second")
                retry += 1
            else:
                retry = 0
            if stop_if_loop:
                #reset same_progress_count if get a different result, or progress is still O
                #(it may take a long time until the results are different from 0)
                if previous_progress != progress or progress == 0:
                    previous_progress = progress
                    same_progress_count = 0
                else:
                    same_progress_count += 1
                if same_progress_count > 50:
                    log.error("apparently rebalance progress code in infinite loop: {0}".format(progress))
                    return False
            #sleep for 2 seconds
            time.sleep(2)

        if progress < 0:
            log.error("rebalance progress code : {0}".format(progress))
            return False
        else:
            duration = time.time() - start
            if duration > 10:
                sleep = 10
            else:
                sleep = duration
            log.info('rebalance progress took {0} seconds '.format(duration))
            log.info("sleep for {0} seconds after rebalance...".format(sleep))
            time.sleep(sleep)
            return True

    def _rebalance_progress_status(self):
        api = self.baseUrl + "pools/default/rebalanceProgress"

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)
        if status:
            if "status" in json_parsed:
                return json_parsed['status']
        else:
            return None

    def _rebalance_progress(self):
        avg_percentage = -1
        api = self.baseUrl + "pools/default/rebalanceProgress"

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)
        if status:
            if "status" in json_parsed:
                if "errorMessage" in json_parsed:
                    msg = '{0} - rebalance failed'.format(json_parsed)
                    log.error(msg)
                    raise RebalanceFailedException(msg)
                elif json_parsed["status"] == "running":
                    total_percentage = 0
                    count = 0
                    for key in json_parsed:
                        if key.find('@') >= 0:
                            ns_1_dictionary = json_parsed[key]
                            percentage = ns_1_dictionary['progress'] * 100
                            count += 1
                            total_percentage += percentage
                    if count:
                        avg_percentage = (total_percentage / count)
                    else:
                        avg_percentage = 0
                    log.info('rebalance percentage : {0} %' .format(avg_percentage))
                else:
                    avg_percentage = 100
        else:
            avg_percentage = -100

        return avg_percentage


    #if status is none , is there an errorMessage
    #convoluted logic which figures out if the rebalance failed or suceeded
    def rebalance_statuses(self):
        rebalanced = None
        api = self.baseUrl + 'pools/rebalanceStatuses'

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            rebalanced = json_parsed['balanced']

        return rebalanced


    def log_client_error(self, post):
        api = self.baseUrl + 'logClientError'

        status, content, header = self._http_request(api, 'POST', post)

        if not status:
            log.error('unable to logClientError')


    #returns node data for this host
    def get_nodes_self(self, timeout=120):
        node = None
        api = self.baseUrl + 'nodes/self'

        status, content, header = self._http_request(api, timeout=timeout)

        if status:
            json_parsed = json.loads(content)
            node = RestParser().parse_get_nodes_response(json_parsed)

        return node


    def node_statuses(self, timeout=120):
        nodes = []
        api = self.baseUrl + 'nodeStatuses'

        status, content, header = self._http_request(api, timeout=timeout)

        json_parsed = json.loads(content)

        if status:
            for key in json_parsed:
                #each key contain node info
                value = json_parsed[key]
                #get otp,get status
                node = OtpNode(id=value['otpNode'],
                               status=value['status'])
                if node.ip == '127.0.0.1':
                    node.ip = self.ip
                node.port = int(key[key.rfind(":") + 1:])
                node.replication = value['replication']
                nodes.append(node)

        return nodes


    def cluster_status(self):
        parsed = {}
        api = self.baseUrl + 'pools/default'

        status, content, header = self._http_request(api)

        if status:
            parsed = json.loads(content)
        return parsed


    def get_pools_info(self):
        parsed = {}
        api = self.baseUrl + 'pools'

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            parsed = json_parsed

        return parsed


    def get_pools(self):
        version = None
        api = self.baseUrl + 'pools'

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            version = MembaseServerVersion(json_parsed['implementationVersion'], json_parsed['componentsVersion'])

        return version


    def get_buckets(self):
        #get all the buckets
        buckets = []
        api = '{0}{1}'.format(self.baseUrl, 'pools/default/buckets/')

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            for item in json_parsed:
                bucketInfo = RestParser().parse_get_bucket_json(item)
                buckets.append(bucketInfo)

        return buckets

    def get_bucket_stats_for_node(self, bucket='default', node=None):
        if not node:
            log.error('node_ip not specified')
            return None

        stats = {}
        api = "{0}{1}{2}{3}{4}:{5}{6}".format(self.baseUrl, 'pools/default/buckets/',
                                     bucket, "/nodes/", node.ip, node.port, "/stats")

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            op = json_parsed["op"]
            samples = op["samples"]
            for stat_name in samples:
                stats[stat_name] = samples[stat_name][-1]

        return stats

    def fetch_bucket_stats(self, bucket='default', zoom='minute'):
        """Return deserialized buckets stats.

        Keyword argument:
        bucket -- bucket name
        zoom -- stats zoom level (minute | hour | day | week | month | year)
        """

        api = self.baseUrl + 'pools/default/buckets/{0}/stats?zoom={1}'.format(bucket, zoom)

        status, content, header = self._http_request(api)

        return json.loads(content)

    def fetch_system_stats(self):
        """Return deserialized system stats."""

        api = self.baseUrl + 'pools/default/'

        status, content, header = self._http_request(api)

        return json.loads(content)

    def get_xdc_queue_size(self, bucket):
        """Fetch bucket stats and return the latest value of XDC replication
        queue size"""
        bucket_stats = self.fetch_bucket_stats(bucket)
        return bucket_stats['op']['samples']['replication_changes_left'][-1]

    def get_nodes(self):
        nodes = []
        api = self.baseUrl + 'pools/default'

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            if "nodes" in json_parsed:
                for json_node in json_parsed["nodes"]:
                    node = RestParser().parse_get_nodes_response(json_node)
                    node.rest_username = self.username
                    node.rest_password = self.password
                    if node.ip == "127.0.0.1":
                        node.ip = self.ip
                    # Only add nodes which are active on cluster
                    if node.clusterMembership == 'active':
                        nodes.append(node)
                    else:
                        log.info("Node {0} not part of cluster {1}".format(node.ip, node.clusterMembership))

        return nodes


    def get_bucket_stats(self, bucket='default'):
        stats = {}
        api = "{0}{1}{2}{3}".format(self.baseUrl, 'pools/default/buckets/', bucket, "/stats")

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            op = json_parsed["op"]
            samples = op["samples"]
            for stat_name in samples:
                if samples[stat_name]:
                    last_sample = len(samples[stat_name]) - 1
                    if last_sample:
                        stats[stat_name] = samples[stat_name][last_sample]

        return stats

    def get_bucket_json(self, bucket='default'):
        api = '{0}{1}{2}'.format(self.baseUrl, 'pools/default/buckets/', bucket)

        status, content, header = self._http_request(api)
        if not status:
            raise GetBucketInfoFailed(bucket, content)

        return json.loads(content)

    def get_bucket(self, bucket='default', num_attempt=1, timeout=1):
        bucketInfo = None

        api = '%s%s%s' % (self.baseUrl, 'pools/default/buckets/', bucket)
        if isinstance(bucket, Bucket):
            api = '%s%s%s' % (self.baseUrl, 'pools/default/buckets/', bucket.name)

        status, content, header = self._http_request(api)
        num = 1
        while not status and num_attempt > num:
            log.info("try again after {0} sec".format(timeout))
            time.sleep(timeout)
            status, content, header = self._http_request(api)
            num += 1
        if status:
            bucketInfo = RestParser().parse_get_bucket_response(content)
        return bucketInfo


    def get_vbuckets(self, bucket='default'):
        return self.get_bucket(bucket).vbuckets


    def delete_bucket(self, bucket='default'):
        api = '%s%s%s' % (self.baseUrl, 'pools/default/buckets/', bucket)
        if isinstance(bucket, Bucket):
            api = '%s%s%s' % s (self.baseUrl, 'pools/default/buckets/', bucket.name)

        status, content, header = self._http_request(api, 'DELETE')
        return status


    # figure out the proxy port
    def create_bucket(self, bucket='',
                      ramQuotaMB=1,
                      authType='none',
                      saslPassword='',
                      replicaNumber=1,
                      proxyPort=11211,
                      bucketType='membase',
                      replica_index=1):
        api = '{0}{1}'.format(self.baseUrl, 'pools/default/buckets')
        params = urllib.urlencode({})
        #this only works for default bucket ?
        if bucket == 'default':
            params = urllib.urlencode({'name': bucket,
                                       'authType': 'sasl',
                                       'saslPassword': saslPassword,
                                       'ramQuotaMB': ramQuotaMB,
                                       'replicaNumber': replicaNumber,
                                       'proxyPort': proxyPort,
                                       'bucketType': bucketType,
                                       'replicaIndex': replica_index})

        elif authType == 'none':
            params = urllib.urlencode({'name': bucket,
                                       'ramQuotaMB': ramQuotaMB,
                                       'authType': authType,
                                       'replicaNumber': replicaNumber,
                                       'proxyPort': proxyPort,
                                       'bucketType': bucketType,
                                       'replicaIndex': replica_index})

        elif authType == 'sasl':
            params = urllib.urlencode({'name': bucket,
                                       'ramQuotaMB': ramQuotaMB,
                                       'authType': authType,
                                       'saslPassword': saslPassword,
                                       'replicaNumber': replicaNumber,
                                       'proxyPort': self.get_nodes_self().moxi,
                                       'bucketType': bucketType,
                                       'replicaIndex': replica_index})



        log.info("{0} with param: {1}".format(api, params))

        create_start_time = time.time()
        status, content, header = self._http_request(api, 'POST', params)

        if not status:
            raise BucketCreationException(ip=self.ip, bucket_name=bucket)

        create_time = time.time() - create_start_time
        log.info("{0} seconds to create bucket {1}".format(create_time, bucket))

        return status


    #return AutoFailoverSettings
    def get_autofailover_settings(self):
        settings = None
        api = self.baseUrl + 'settings/autoFailover'

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)

        if status:
            settings = AutoFailoverSettings()
            settings.enabled = json_parsed["enabled"]
            settings.count = json_parsed["count"]
            settings.timeout = json_parsed["timeout"]

        return settings


    def update_autofailover_settings(self, enabled, timeout):
        if enabled:
            params = urllib.urlencode({'enabled': 'true',
                                       'timeout': timeout})
        else:
            params = urllib.urlencode({'enabled': 'false',
                                       'timeout': timeout})
        api = self.baseUrl + 'settings/autoFailover'
        log.info('settings/autoFailover params : {0}'.format(params))

        status, content, header = self._http_request(api, 'POST', params)
        return status


    def reset_autofailover(self):
        api = self.baseUrl + 'settings/autoFailover/resetCount'

        status, content, header = self._http_request(api, 'POST', '')
        return status


    def enable_autofailover_alerts(self, recipients, sender, email_username, email_password, email_host='localhost', email_port=25, email_encrypt='false', alerts='auto_failover_node,auto_failover_maximum_reached'):
        api = self.baseUrl + 'settings/alerts'
        params = urllib.urlencode({'enabled': 'true',
                                   'recipients': recipients,
                                   'sender': sender,
                                   'emailUser': email_username,
                                   'emailPass': email_password,
                                   'emailHost': email_host,
                                   'emailPrt': email_port,
                                   'emailEncrypt': email_encrypt,
                                   'alerts': alerts})
        log.info('settings/alerts params : {0}'.format(params))

        status, content, header = self._http_request(api, 'POST', params)
        return status


    def disable_autofailover_alerts(self):
        api = self.baseUrl + 'settings/alerts'
        params = urllib.urlencode({'enabled': 'false'})
        log.info('settings/alerts params : {0}'.format(params))

        status, content, header = self._http_request(api, 'POST', params)
        return status


    def stop_rebalance(self, wait_timeout=10):
        api = self.baseUrl + '/controller/stopRebalance'

        status, content, header = self._http_request(api, 'POST')

        if status:
            for i in xrange(wait_timeout):
                if self._rebalance_progress_status() == 'running':
                    log.info("rebalance is not stopped yet")
                    time.sleep(1)
                    status = False
                else:
                    log.info("rebalance was stopped")
                    status = True
                    break
        else:
            log.error("Rebalance is not stopped due to {0}".format(content))
        return status


    def set_data_path(self, data_path=None, index_path=None):
        if data_path:
            api = self.baseUrl + '/nodes/self/controller/settings'
            paths = {'path': data_path}
            if index_path:
                paths['index_path'] = index_path
            params = urllib.urlencode(paths)
            log.info('/nodes/self/controller/settings params : {0}'.format(params))

            status, content, header = self._http_request(api, 'POST', params)
            if status:
                log.info("Setting data_path: {0}: status {1}".format(data_path, status))
            else:
                log.error("Unable to set data_path {0} : {1}".format(data_path, content))
            return status

    def get_database_disk_size(self, bucket='default'):
        api = self.baseUrl + "pools/{0}/buckets".format(bucket)
        status, content, header = self._http_request(api)
        json_parsed = json.loads(content)
        # disk_size in MB
        disk_size = (json_parsed[0]["basicStats"]["diskUsed"]) / (1024 * 1024)
        return status, disk_size

    def ddoc_compaction(self, design_doc_id, bucket="default"):
        api = self.baseUrl + "pools/default/buckets/%s/ddocs/%s/controller/compactView" % \
            (bucket, design_doc_id)
        status, content, header = self._http_request(api, 'POST')
        if not status:
            raise CompactViewFailed(design_doc_id, content)
        log.info("compaction for ddoc '%s' was triggered" % design_doc_id)

    def check_compaction_status(self, bucket):
        vbucket = self.get_vbuckets(bucket)
        for i in range(len(vbucket)):
            api = self.capiBaseUrl + "/{0}%2F{1}".format(bucket, i)
            status, content = httplib2.Http().request(api, "GET")
            data = json.loads(content)
            if "compact_running" in data and data["compact_running"]:
                return True, i
        return False, i

    def set_ensure_full_commit(self, value):
        """Dynamic settings changes"""
        # the boolean paramter is used to turn on/off ensure_full_commit(). In XDCR,
        # issuing checkpoint in this function is expensive and not necessary in some
        # test, turning off this function would speed up some test. The default value
        # is ON.
        cmd = 'ns_config:set(ensure_full_commit_enabled, {0}).'.format(value)
        return self.diag_eval(cmd)

    def set_reb_cons_view(self, disable=None):
        #do not change if None
        if disable is None:
            log.info("default consistent_view value will be used on server")
            return
        """Enable/disable consistent view for rebalance tasks"""
        cmd = 'ns_config:set(index_aware_rebalance_disabled, %s).'\
                % str(disable).lower()
        log.info('Enabling/disabling consistent-views during rebalance: {0}'.format(cmd))
        return self.diag_eval(cmd)

    def set_mc_threads(self, mc_threads=4):
        """
        Change number of memcached threads and restart the cluster
        """
        cmd = "[ns_config:update_key({node, N, memcached}, " \
              "fun (PList) -> lists:keystore(verbosity, 1, PList," \
              " {verbosity, \"-t %s\"}) end) " \
              "|| N <- ns_node_disco:nodes_wanted()]." % mc_threads

        return self.diag_eval(cmd)

    def set_auto_compaction(self, parallelDBAndVC="false",
                            dbFragmentThreshold=None,
                            viewFragmntThreshold=None,
                            dbFragmentThresholdPercentage=100,
                            viewFragmntThresholdPercentage=100,
                            allowedTimePeriodFromHour=None,
                            allowedTimePeriodFromMin=None,
                            allowedTimePeriodToHour=None,
                            allowedTimePeriodToMin=None,
                            allowedTimePeriodAbort=None,
                            bucket=None):
        """Reset compaction values to default, try with old fields (dp4 build)
        and then try with newer fields"""
        params = {}
        api = self.baseUrl

        if bucket is None:
            # setting is cluster wide
            api = api + "controller/setAutoCompaction"
        else:
            # overriding per/bucket compaction setting
            api = api + "pools/default/buckets/" + bucket
            params["autoCompactionDefined"] = "true"
            # reuse current ram quota in mb per node
            num_nodes = len(self.node_statuses())
            quota = self.get_bucket_json(bucket)["quota"]["ram"] / (1048576 * num_nodes)
            params["ramQuotaMB"] = quota

        params["parallelDBAndViewCompaction"] = parallelDBAndVC
        # Need to verify None because the value could be = 0
        if dbFragmentThreshold is not None:
            params["databaseFragmentationThreshold[size]"] = dbFragmentThreshold
        if viewFragmntThreshold is not None:
            params["viewFragmentationThreshold[percentage]"] = viewFragmntThreshold
        if dbFragmentThresholdPercentage is not None:
            params["databaseFragmentationThreshold[percentage]"] = dbFragmentThresholdPercentage
        if viewFragmntThresholdPercentage is not None:
            params["viewFragmentationThreshold[percentage]"] = viewFragmntThresholdPercentage
        if allowedTimePeriodFromHour is not None:
            params["allowedTimePeriod[fromHour]"] = allowedTimePeriodFromHour
        if allowedTimePeriodFromMin is not None:
            params["allowedTimePeriod[fromMinute]"] = allowedTimePeriodFromMin
        if allowedTimePeriodToHour is not None:
            params["allowedTimePeriod[toHour]"] = allowedTimePeriodToHour
        if allowedTimePeriodToMin is not None:
            params["allowedTimePeriod[toMinute]"] = allowedTimePeriodToMin
        if allowedTimePeriodAbort is not None:
            params["allowedTimePeriod[abortOutside]"] = allowedTimePeriodAbort

        params = urllib.urlencode(params)
        log.info("'%s' bucket's settings were changed with parameters: %s" % (bucket, params))
        return self._http_request(api, "POST", params)

    def set_global_loglevel(self, loglevel='error'):
        """Set cluster-wide logging level for core components

        Possible loglevel:
            -- debug
            -- info
            -- warn
            -- error
        """

        api = self.baseUrl + 'diag/eval'

        request_body = 'rpc:eval_everywhere(erlang, apply, [fun () -> \
                        [ale:set_loglevel(L, {0}) || L <- \
                        [ns_server, couchdb, user, menelaus, ns_doctor, stats, \
                        rebalance, cluster, views, stderr]] end, []]).'.format(loglevel)

        return self._http_request(api=api, method='POST', params=request_body,
                                  headers=self._create_headers())

    def set_couchdb_option(self, section, option, value):
        """Dynamic settings changes"""

        cmd = 'ns_config:set({{couchdb, {{{0}, {1}}}}}, {2}).'.format(section,
                                                                      option,
                                                                      value)

        return self.diag_eval(cmd)

    def get_alerts(self):
        api = self.baseUrl + "pools/default/"

        status, content, header = self._http_request(api)

        json_parsed = json.loads(content)
        if status:
            if "alerts" in json_parsed:
                return json_parsed['alerts']
        else:
            return None

    def flush_bucket(self, bucket="default"):

        if isinstance(bucket, Bucket):
            bucket_name = bucket.name
        else:
            bucket_name = bucket

        api = self.baseUrl + "pools/default/buckets/%s/controller/doFlush" % (bucket_name)

        status, content, header = self._http_request(api, 'POST')
        if not status:
            raise BucketFlushFailed(self.ip, bucket_name)
        log.info("Flush for bucket '%s' was triggered" % bucket_name)


class MembaseServerVersion:
    def __init__(self, implementationVersion='', componentsVersion=''):
        self.implementationVersion = implementationVersion
        self.componentsVersion = componentsVersion


#this class will also contain more node related info
class OtpNode(object):
    def __init__(self, id='', status=''):
        self.id = id
        self.ip = ''
        self.replication = ''
        self.port = 8091
        #extract ns ip from the otpNode string
        #its normally ns_1@10.20.30.40
        if id.find('@') >= 0:
            self.ip = id[id.index('@') + 1:]
        self.status = status


class NodeInfo(object):
    def __init__(self):
        self.availableStorage = None # list
        self.memoryQuota = None


class NodeDataStorage(object):
    def __init__(self):
        self.type = '' #hdd or ssd
        self.path = ''
        self.index_path = ''
        self.quotaMb = ''
        self.state = '' #ok

    def __str__(self):
        return '{0}'.format({'type': self.type,
                             'path': self.path,
                             'index_path' : self.index_path,
                             'quotaMb': self.quotaMb,
                             'state': self.state})

    def get_data_path(self):
        return self.path

    def get_index_path(self):
        return self.index_path


class NodeDiskStorage(object):
    def __init__(self):
        self.type = 0
        self.path = ''
        self.sizeKBytes = 0
        self.usagePercent = 0


class Bucket(object):
    def __init__(self, bucket_size='', name="", authType="sasl", saslPassword="", num_replicas=0, port=11211, master_id=None):
        self.name = name
        self.port = port
        self.type = ''
        self.nodes = None
        self.stats = None
        self.servers = []
        self.vbuckets = []
        self.forward_map = []
        self.numReplicas = num_replicas
        self.saslPassword = saslPassword
        self.authType = ""
        self.bucket_size = bucket_size
        self.kvs = {1:KVStore()}
        self.authType = authType
        self.master_id = master_id

    def __str__(self):
        return self.name


class Node(object):
    def __init__(self):
        self.uptime = 0
        self.memoryTotal = 0
        self.memoryFree = 0
        self.mcdMemoryReserved = 0
        self.mcdMemoryAllocated = 0
        self.status = ""
        self.hostname = ""
        self.clusterCompatibility = ""
        self.clusterMembership = ""
        self.version = ""
        self.os = ""
        self.ports = []
        self.availableStorage = []
        self.storage = []
        self.memoryQuota = 0
        self.moxi = 11211
        self.memcached = 11210
        self.id = ""
        self.ip = ""
        self.rest_username = ""
        self.rest_password = ""
        self.port = 8091


class AutoFailoverSettings(object):
    def __init__(self):
        self.enabled = True
        self.timeout = 0
        self.count = 0


class NodePort(object):
    def __init__(self):
        self.proxy = 0
        self.direct = 0


class BucketStats(object):
    def __init__(self):
        self.quotaPercentUsed = 0
        self.opsPerSec = 0
        self.diskFetches = 0
        self.itemCount = 0
        self.diskUsed = 0
        self.memUsed = 0
        self.ram = 0


class vBucket(object):
    def __init__(self):
        self.master = ''
        self.replica = []
        self.id = -1

class RestParser(object):
    def parse_get_nodes_response(self, parsed):
        node = Node()
        node.uptime = parsed['uptime']
        node.memoryFree = parsed['memoryFree']
        node.memoryTotal = parsed['memoryTotal']
        node.mcdMemoryAllocated = parsed['mcdMemoryAllocated']
        node.mcdMemoryReserved = parsed['mcdMemoryReserved']
        node.status = parsed['status']
        node.hostname = parsed['hostname']
        node.clusterCompatibility = parsed['clusterCompatibility']
        node.clusterMembership = parsed['clusterMembership']
        node.version = parsed['version']
        node.port = parsed["hostname"][parsed["hostname"].find(":") + 1:]
        node.os = parsed['os']
        if "otpNode" in parsed:
            node.id = parsed["otpNode"]
            if parsed["otpNode"].find('@') >= 0:
                node.ip = node.id[node.id.index('@') + 1:]
        elif "hostname" in parsed:
            node.ip = parsed["hostname"].split(":")[0]

        # memoryQuota
        if 'memoryQuota' in parsed:
            node.memoryQuota = parsed['memoryQuota']
        if 'availableStorage' in parsed:
            availableStorage = parsed['availableStorage']
            for key in availableStorage:
                #let's assume there is only one disk in each noce
                dict_parsed = parsed['availableStorage']
                if 'path' in dict_parsed and 'sizeKBytes' in dict_parsed and 'usagePercent' in dict_parsed:
                    diskStorage = NodeDiskStorage()
                    diskStorage.path = dict_parsed['path']
                    diskStorage.sizeKBytes = dict_parsed['sizeKBytes']
                    diskStorage.type = key
                    diskStorage.usagePercent = dict_parsed['usagePercent']
                    node.availableStorage.append(diskStorage)
                    log.info(diskStorage)

        if 'storage' in parsed:
            storage = parsed['storage']
            for key in storage:
                disk_storage_list = storage[key]
                for dict_parsed in disk_storage_list:
                    if 'path' in dict_parsed and 'state' in dict_parsed and 'quotaMb' in dict_parsed:
                        dataStorage = NodeDataStorage()
                        dataStorage.path = dict_parsed['path']
                        dataStorage.index_path = dict_parsed.get('index_path', '')
                        dataStorage.quotaMb = dict_parsed['quotaMb']
                        dataStorage.state = dict_parsed['state']
                        dataStorage.type = key
                        node.storage.append(dataStorage)

        # ports":{"proxy":11211,"direct":11210}
        if "ports" in parsed:
            ports = parsed["ports"]
            if "proxy" in ports:
                node.moxi = ports["proxy"]
            if "direct" in ports:
                node.memcached = ports["direct"]
        return node


    def parse_get_bucket_response(self, response):
        parsed = json.loads(response)
        return self.parse_get_bucket_json(parsed)


    def parse_get_bucket_json(self, parsed):
        bucket = Bucket()
        bucket.name = parsed['name']
        bucket.type = parsed['bucketType']
        bucket.port = parsed['proxyPort']
        bucket.authType = parsed["authType"]
        bucket.saslPassword = parsed["saslPassword"]
        bucket.nodes = list()
        if 'vBucketServerMap' in parsed:
            vBucketServerMap = parsed['vBucketServerMap']
            serverList = vBucketServerMap['serverList']
            bucket.servers.extend(serverList)
            if "numReplicas" in vBucketServerMap:
                bucket.numReplicas = vBucketServerMap["numReplicas"]
            #vBucketMapForward
            if 'vBucketMapForward' in vBucketServerMap:
                #let's gather the forward map
                vBucketMapForward = vBucketServerMap['vBucketMapForward']
                counter = 0
                for vbucket in vBucketMapForward:
                    #there will be n number of replicas
                    vbucketInfo = vBucket()
                    vbucketInfo.master = serverList[vbucket[0]]
                    if vbucket:
                        for i in range(1, len(vbucket)):
                            if vbucket[i] != -1:
                                vbucketInfo.replica.append(serverList[vbucket[i]])
                    vbucketInfo.id = counter
                    counter += 1
                    bucket.forward_map.append(vbucketInfo)
            vBucketMap = vBucketServerMap['vBucketMap']
            counter = 0
            for vbucket in vBucketMap:
                #there will be n number of replicas
                vbucketInfo = vBucket()
                vbucketInfo.master = serverList[vbucket[0]]
                if vbucket:
                    for i in range(1, len(vbucket)):
                        if vbucket[i] != -1:
                            vbucketInfo.replica.append(serverList[vbucket[i]])
                vbucketInfo.id = counter
                counter += 1
                bucket.vbuckets.append(vbucketInfo)
                #now go through each vbucket and populate the info
            #who is master , who is replica
        # get the 'storageTotals'
        log.debug('read {0} vbuckets'.format(len(bucket.vbuckets)))
        stats = parsed['basicStats']
        #vBucketServerMap
        bucketStats = BucketStats()
        log.debug('stats:{0}'.format(stats))
        bucketStats.quotaPercentUsed = stats['quotaPercentUsed']
        bucketStats.opsPerSec = stats['opsPerSec']
        if 'diskFetches' in stats:
            bucketStats.diskFetches = stats['diskFetches']
        bucketStats.itemCount = stats['itemCount']
        bucketStats.diskUsed = stats['diskUsed']
        bucketStats.memUsed = stats['memUsed']
        quota = parsed['quota']
        bucketStats.ram = quota['ram']
        bucket.stats = bucketStats
        nodes = parsed['nodes']
        for nodeDictionary in nodes:
            node = Node()
            node.uptime = nodeDictionary['uptime']
            node.memoryFree = nodeDictionary['memoryFree']
            node.memoryTotal = nodeDictionary['memoryTotal']
            node.mcdMemoryAllocated = nodeDictionary['mcdMemoryAllocated']
            node.mcdMemoryReserved = nodeDictionary['mcdMemoryReserved']
            node.status = nodeDictionary['status']
            node.hostname = nodeDictionary['hostname']
            if 'clusterCompatibility' in nodeDictionary:
                node.clusterCompatibility = nodeDictionary['clusterCompatibility']
            if 'clusterMembership' in nodeDictionary:
                node.clusterCompatibility = nodeDictionary['clusterMembership']
            node.version = nodeDictionary['version']
            node.os = nodeDictionary['os']
            if "ports" in nodeDictionary:
                ports = nodeDictionary["ports"]
                if "proxy" in ports:
                    node.moxi = ports["proxy"]
                if "direct" in ports:
                    node.memcached = ports["direct"]
            if "hostname" in nodeDictionary:
                value = str(nodeDictionary["hostname"])
                node.ip = value[:value.rfind(":")]
                node.port = int(value[value.rfind(":") + 1:])
            if "otpNode" in nodeDictionary:
                node.id = nodeDictionary["otpNode"]
            bucket.nodes.append(node)
        return bucket
