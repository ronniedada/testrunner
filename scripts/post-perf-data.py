import collections
import sys

sys.path.append('.')
sys.path.append('lib')
from optparse import OptionParser
from couchdbkit import Server
from logger import Logger
import json
from bz2 import BZ2Compressor


log = Logger.get_logger()


def compress(data):
    try:
        bc = BZ2Compressor()
        bc.compress(lg)
        attachment = bc.flush()
        return attachment
    except Exception as ex:
        log.error("unable to bzip data", ex)


def sortedDictValues1(adict):
    items = adict.items()
    items.sort()
    return [value for key, value in items]


def convert(data):
    if isinstance(data, unicode):
        return str(data)
    elif isinstance(data, collections.Mapping):
        return dict(map(convert, data.iteritems()))
    elif isinstance(data, collections.Iterable):
        return type(data)(map(convert, data))
    else:
        return data


def flatten(keys, json):
    result = {}
    for key in keys:
        if key in json:
            print key
            for _k in json[key]:
                print _k
                if not isinstance(json[key][_k], dict):
                    result["{0}.{1}".format(key, _k)] = json[key][_k]
    return result

if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option("-n", "--node", dest="node", default="http://127.0.0.1:5984",
                      help="couchdb ip , defaults to 127.0.0.1:5984")
    parser.add_option("-d", "--database",
                      dest="database", help="db name in couchdb",
                      default="tmp")
    parser.add_option("-i", "--input",
                      dest="input", help="json file to post to couchdb",
                      default="test.json")

    options, args = parser.parse_args()

    try:
        server = Server(options.node)
        db = server.get_or_create_db(options.database)
        input_json = json.loads(open(options.input).read())

        log.info("loaded file : {0} into a json document".format(options.input))

        attachments = {}

        build_info = flatten(["buildinfo"], input_json)
        info = flatten(["info"], input_json)
        info = {"info.test_time": info["info.test_time"]}
        name = {"name": input_json["name"]}
        z = dict(build_info.items() + info.items() + name.items())
        if "buildinfo.thisNode" in z:
            del z["buildinfo.thisNode"]
        if "buildinfo.couchApiBase" in z:
            del z["buildinfo.couchApiBase"]
        if "time" in z:
            del z["time"]

        if "ns_server_data" in input_json:
            attachments["ns_server_data"] = []
            ns_server_data = input_json["ns_server_data"]

            index = 1
            for item in ns_server_data:
                print "flattening ns_server_data"
                if "op" in item:
                    samples = item["op"]["samples"]
                    _time = 0
                    _new_samples = []
                    max_length = 1
                    for sample in samples:
                        if len(samples[sample]) > max_length:
                            max_length = len(samples[sample])
                    print "max_length", max_length
                    for i in range(0, max_length):
                        _new_sample = {}
                        for sample in samples:
                            if len(samples[sample]) > i:
                                _new_sample[sample] = samples[sample][i]
                        _new_sample.update(z)
                        _new_sample.update({"row": index})
#                        _new_sample = sortedDictValues1(_new_sample)
#                        print _new_sample
                        attachments["ns_server_data"].append(_new_sample)
                        index += 1
            del input_json["ns_server_data"]
        if "ns_server_data_system" in input_json:
            print "flattening ns_server_data_system"
            attachments["ns_server_data_system"] = []
            for index, row in enumerate(input_json["ns_server_data_system"],
                                        start=1):
                temp_row = convert(row)
                values = temp_row.get('nodes', {})
                for row in values:
                    row_dict = {}
                    if 'cpu_utilization_rate' in row['systemStats']:
                        row_dict['cpu_util'] = row['systemStats']['cpu_utilization_rate']
                    if 'swap_used' in row['systemStats']:
                        row_dict['swap_used'] = row['systemStats']['swap_used']
                    if not 'vb_replica_curr_items' in row['interestingStats']:
                        continue
                    row_dict['vb_replica_curr_items'] = row['interestingStats']['vb_replica_curr_items']
                    row_dict['curr_items_tot'] = row['interestingStats']['curr_items_tot']
                    row_dict['curr_items'] = row['interestingStats']['curr_items']
                    row_dict['node'] = row['hostname']
                    row_dict.update(z)
                    row_dict.update({"row": index})
                    attachments["ns_server_data_system"].append(row_dict)
            del input_json["ns_server_data_system"]
        if "dispatcher" in input_json:
            print "flattening dispatcher"
            attachments["dispatcher"] = input_json["dispatcher"]
            for index, row in enumerate(attachments["dispatcher"], start=1):
                row.update(z)
                row.update({"row": index})
            del input_json["dispatcher"]
        if "timings" in input_json:
            print "flattening timings"
            attachments["timings"] = input_json["timings"]
            for index, row in enumerate(attachments["timings"], start=1):
                row.update(z)
                row.update({"row": index})
            del input_json["timings"]
        if "ops" in input_json:
            print "flattening ops"
            attachments["ops"] = input_json["ops"]
            for index, row in enumerate(attachments["ops"], start=1):
                row.update(z)
                row.update({"row": index})
            del input_json["ops"]
        if "qps" in input_json:
            print "flattening qps"
            attachments["qps"] = input_json["qps"]
            attachments["qps"].update(z)
            del input_json["qps"]
        if "systemstats" in input_json:
            print "flattening systemstats"
            attachments["systemstats"] = input_json["systemstats"]
            for index, row in enumerate(attachments["systemstats"], start=1):
                row.update(z)
                row.update({"row": index})
                print row["stime"], row["comm"], row["row"]
            del input_json["systemstats"]
        if "iostats" in input_json:
            print "flattening iostats"
            attachments["iostats"] = input_json["iostats"]
            for index, row in enumerate(attachments["iostats"], start=1):
                row.update(z)
                row.update({"row": index})
            del input_json["iostats"]
        if "data-size" in input_json:
            print "flattening data-size"
            attachments["data-size"] = input_json["data-size"]
            for index, row in enumerate(attachments["data-size"], start=1):
                row.update(z)
                row.update({"row": index})
            del input_json["data-size"]
        if "bucket-size" in input_json:
            print "flattening bucket-size"
            values = input_json["bucket-size"]
            bucket_sizes = []
            for index, row in enumerate(values, start=1):
                row_dict = {}
                row_dict['size'] = row
                row_dict.update(z)
                row_dict.update({"row": index})
                bucket_sizes.append(row_dict.copy())
            del input_json["bucket-size"]
        attachments["bucket-size"] = bucket_sizes
        if "membasestats" in input_json:
            print "flattening membasestats"
            attachments["membasestats"] = input_json["membasestats"]
            for index, row in enumerate(attachments["membasestats"], start=1):
                row.update(z)
                row.update({"row": index})
            del input_json["membasestats"]

        if "view_info" in input_json:
            print "flattening view info"
            attachments["view_info"] = input_json["view_info"]
            index = 1
            for index, row in enumerate(attachments["view_info"], start=1):
                row.update(z)
                row.update({"row": index})
            del input_json["view_info"]

        for histogram in input_json.keys():
            if histogram.startswith("latency") and histogram.endswith("histogram"):
                print "flattening {0} snapshot".format(histogram)
                if not isinstance(input_json[histogram], dict):
                    print "Error: cannot flatten {0} snapshot: not a dict or empty".format(histogram)
                    continue
                if not "client_id" in input_json[histogram]:
                    print "Error: cannot flatten {0} snapshot: no client_id".format(histogram)
                    continue

                client_id = input_json[histogram].get("client_id", "")
                del input_json[histogram]["client_id"]
                attachments[histogram] = []
                index = 1
                for key, value in input_json[histogram].iteritems():
                    lr = {"row": index,
                          "time": key,
                          "count": value,
                          "client_id": client_id}
                    lr.update(z)
                    attachments[histogram].append(lr)
                    index += 1
                del input_json[histogram]

        for latency in input_json.keys():
            if latency.startswith('latency'):
                print "flattening {0}".format(latency)
                attachments[latency] = []
                index = 1
                for row in input_json[latency]:
                    if isinstance(row[0], list):
                        lr = {"percentile_90th": row[0][1],
                              "percentile_95th": 0,
                              "percentile_99th": row[1][1],
                              "client_id": "UNKNOWN",
                              "mystery": ""}
                        lr.update(z)
                        lr.update({"row": index})
                        index += 1
                        attachments[latency].append(lr)
                    else:
                    #create a new dict
                        lr = {"percentile_80th": row[0],
                              "percentile_90th": row[1],
                              "percentile_95th": row[2],
                              "percentile_99th": row[3],
                              "percentile_999th": row[4],
                              "client_id": row[5],
                              "time": row[6],
                              "mystery": row[7]}
                        lr.update(z)
                        lr.update({"row": index})
                        index += 1
                        attachments[latency].append(lr)
                del input_json[latency]

        log.info("attachments has {0} objects".format(len(attachments)))
        res = db.save_doc(input_json, force_update=True)
        doc_id = res["id"]
        rev_id = res["rev"]
        msg = "inserted document with id : {0} and rev : {1}in database {2}"
        log.info(msg.format(doc_id, rev_id, options.database))
        for name in attachments:
            log.info("inserting attachment with name : {0}".format(name))
            db.put_attachment(input_json, attachments[name], name, "text/plain")

    except Exception as ex:
        print ex
        log.error("unable to connect to {0}".format(options.node))
