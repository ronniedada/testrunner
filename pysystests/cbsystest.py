import argparse
import json
from rabbit_helper import RabbitHelper


parser = argparse.ArgumentParser(description='CB System Test Tool')
subparser = parser.add_subparsers(dest="subparsers")

def add_modifier_args(parser):
    parser.add_argument("--cc_queues",    nargs='+', help="queues to copy created keys into")
    parser.add_argument("--consume_queue",help="queue with keys to get/update/delete")
    parser.add_argument("--precondition", help="required stat or cluster state required before running workload")
    parser.add_argument("--postcondition",help="required stat or cluster state required to complete workload")
    parser.add_argument("--wait",  nargs=3,  help="time to wait before starting workload: <hour> <min> <sec>", metavar = ('HOUR','MIN','SEC'), type=int)
    parser.add_argument("--expires",nargs=3,  help="time to wait before terminating workload: <hour> <min> <sec>", metavar = ('HOUR','MIN','SEC'), type=int)

def add_broker_arg(parser):
    parser.add_argument("--broker", required = True, help="ip address of broker used to consume options")

def add_template_parser(parent):
    parser = parent.add_parser("template")

    add_broker_arg(parser)
    parser.add_argument("--name",     help="template name", required = True)
    parser.add_argument("--ttl",      default=0, help="document expires time")
    parser.add_argument("--flags",    default=0, help="document create flags")
    parser.add_argument("--cc_queues",nargs='+', help="queues to copy created keys into")
    parser.add_argument("--kvpairs",   nargs='+', help="list of kv items i.e=> state:ca,age:28,company:cb")
    parser.add_argument("--type",    help="json/non-json default is json", default="json")
    parser.add_argument("--size",    help="size of documents. padding is used if necessary")

#TODO    parser.add_argument("--blobs",   nargs='+', help="data strings for non-json docs")
    parser.set_defaults(handler=import_template)


def add_workload_parser(parent):
    parser = parent.add_parser("workload")

    add_broker_arg(parser)
    parser.add_argument("--name",    help="predefind workload", default="default")
    parser.add_argument("--bucket",  help="bucket", default="default")
    parser.add_argument("--ops",     help="ops per sec", default=0, type=int)
    parser.add_argument("--create",  help="percentage of creates 0-100", default=0, type=int)
    parser.add_argument("--update",  help="percentage of updates 0-100", default=0, type=int)
    parser.add_argument("--get",     help="percentage of gets 0-100", default=0, type=int)
    parser.add_argument("--delete",  help="percentage of deletes 0-100", default=0, type=int)
    parser.add_argument("--template",help="predefined template to use", default="default")
    add_modifier_args(parser)

    parser.set_defaults(handler=run_workload)

def add_admin_parser(parent):
    parser = parent.add_parser("admin")

    add_broker_arg(parser)
    parser.add_argument("--rebalance_in", help="rebalance_in", default='', type=str)
    parser.add_argument("--rebalance_out", help="rebalance_out", default='', type=str)
    parser.add_argument("--failover", help="failover", default='', type=str)
    parser.add_argument("--only_failover", help="only_failover", default=False, action='store_true')
    parser.add_argument("--soft_restart", help="soft_restart", default='', type=str)
    parser.add_argument("--hard_restart", help="hard_restart", default='', type=str)

    parser.set_defaults(handler=perform_admin_tasks)

def add_xdcr_parser(parent):
    parser = parent.add_parser("xdcr")
    parser.add_argument("--dest_cluster_ip", help="Dest. cluster ip", default='', type=str)
    parser.add_argument("--dest_cluster_username", help="Dest. cluster rest username", default='Administrator', type=str)
    parser.add_argument("--dest_cluster_pwd", help="Dest. cluster rest pwd", default='password', type=str)
    parser.add_argument("--dest_cluster_name", help="Dest. cluster name", default='', type=str)
    parser.add_argument("--replication_type", help="unidirection or bidirection", default='unidirection', type=str)
    parser.set_defaults(handler=perform_xdcr_tasks)

def add_test_parser(parent):
    parser = parent.add_parser("test")
    parser.add_argument("workloads", help="rebalance")

def setup_run_parser():
    run_parser = subparser.add_parser('run')
    subparser_ = run_parser.add_subparsers()
    add_workload_parser(subparser_)
    add_admin_parser(subparser_)
    add_xdcr_parser(subparser_)
    add_test_parser(subparser_)

def setup_import_parser():
    import_parser = subparser.add_parser('import')
    subparser_ = import_parser.add_subparsers()
    add_template_parser(subparser_)


def setup_list_parser():
    list_parser = subparser.add_parser('list')
    list_parser.add_argument('workloads', help='list pre-defined workloads')
    list_parser.add_argument('templates', help='list pre-defined document templates')
    list_parser.add_argument('tests', help='list pre-defined tests')

def conv_to_secs(list_):
    return list_[0]*60*60 + list_[1]*60 + list_[2]

def run_workload(args):
 
    workload = {}

    if args.name != None:
        # TODO: read in workload params from saved store
        # workload.update(cached_workload)
        pass

    if args.wait is not None:
        args.wait = conv_to_secs(args.wait) 

    if args.expires is not None:
        args.expires = conv_to_secs(args.expires)

    workload = { "bucket"      : args.bucket,
                 "ops_per_sec" : args.ops,
                 "create_perc" : args.create, 
                 "update_perc" : args.update, 
                 "get_perc"    : args.get, 
                 "del_perc"    : args.delete, 
                 "cc_queues"   : args.cc_queues,
                 "consume_queue" : args.consume_queue,
                 "postconditions" : args.postcondition,
                 "preconditions" : args.precondition,
                 "wait"  : args.wait,
                 "expires"  : args.expires,
                 "template"  : args.template}

    rabbitHelper = RabbitHelper(args.broker)
    rabbitHelper.putMsg("workload", json.dumps(workload))


def import_template(args):

    val = None

    if args.type == "json":
        json_val = {}
        for kv in args.kvpairs:
            pair = '{%s}' % kv 
            try:
                pair = json.loads(pair)
                json_val.update(pair)
            except ValueError as ex:
                print "ERROR: Unable to encode as valid json: %s " % kv
                print "make sure strings surrounded by double quotes"
                return
        val = json_val

    #TODO binary blobs

    template = { "name" : args.name,
                 "ttl" : args.ttl,
                 "flags" : args.flags,
                 "cc_queues" : args.cc_queues,
                 "size" : args.size,
                 "kv" : val}

    rabbitHelper = RabbitHelper(args.broker)
    rabbitHelper.putMsg("workload_template", json.dumps(template))

def perform_admin_tasks(args):

    actions = {'rebalance_in': args.rebalance_in,
               'rebalance_out': args.rebalance_out,
               'failover': args.failover,
               'soft_restart': args.soft_restart,
               'hard_restart': args.hard_restart,
               'only_failover': args.only_failover
              }

    #TODO: Validate the user inputs, before passing to rabbit
    print actions
    rabbitHelper = RabbitHelper(args.broker)
    rabbitHelper.putMsg("admin_tasks", json.dumps(actions))

def perform_xdcr_tasks(args):

    xdcrMsg = {'dest_cluster_ip': args.dest_cluster_ip,
               'dest_cluster_rest_username': args.dest_cluster_username,
               'dest_cluster_rest_pwd':  args.dest_cluster_pwd,
               'dest_cluster_name': args.dest_cluster_name,
               'replication_type': args.replication_type,
    }

    #TODO: Validate the user inputs, before passing to rabbit
    print xdcrMsg
    rabbitHelper.putMsg("xdcr_tasks", json.dumps(xdcrMsg))


### setup main arg parsers
setup_run_parser()
setup_list_parser()
setup_import_parser()

## PARSE ARGS ##
args = parser.parse_args()

# setup parser callbacks
if args.subparsers == "run" or\
    args.subparsers == "import":
    args.handler(args)
    
