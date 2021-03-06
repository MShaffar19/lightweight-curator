# coding: utf8

from datetime import date, timedelta
from elasticsearch import Elasticsearch, exceptions
import json, os, subprocess, sys, logging, argparse, time

# read environment variables
elasticsearch_host = os.getenv("ELASTICSEARCH_HOST", "elasticsearch:9200")
percentage_threshold = int(os.getenv("PERCENTAGE_THRESHOLD", "80"))
index_name_prefixes = os.getenv("INDEX_NAME_PREFIXES", "infra-,app-,audit-")

def argument_parser(args):
    """
    Add debug, verbose and dry_run command-line options.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d", "--debug",
        help="Print debugging information in addition to normal processing.",
        action="store_const", dest="loglevel", const=logging.DEBUG,
    )
    parser.add_argument(
        "-v", "--verbose",
        help="Shows details about the result of running lightweight_curator.py",
        action="store_const", dest="loglevel", const=logging.INFO,
        default=logging.WARNING,
    )
    parser.add_argument(
        "-n", "--dry_run",
        help="Print the list of indices which would be passed onto deletion process, but do not execute.",
        action="store_const", dest="dry", const=True,
    )

    return parser.parse_args(args)

def output_log_config(loglevel):
    """
    Configure output logs with provided or default loglevel.
    """
    stdout_handler = logging.StreamHandler(sys.stdout)

    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s [%(filename)s:%(module)s:%(funcName)s:%(lineno)d] %(message)s",
        level=loglevel,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=stdout_handler
    )

def env_validation(index_name_prefixes, elasticsearch_host):
    """
    Initial validation of environment variables.
    """
    if index_name_prefixes == "":
        logger.error("Index name prefix is empty (INDEX_NAME_PREFIXES="")")
        sys.exit(1)

    if elasticsearch_host == "":
        logger.error("Elasticsearch host is empty (ELASTICSEARCH_HOST="")")
        sys.exit(1)

    return

def es_connect(host):
    """
    Returns Elasticsearch instance which will be used for api calls.
    """
    counter = 0
    max_attempts = 2
    while True:
        try:
            es = Elasticsearch(
                [host],
                # enable SSL
                use_ssl=True,
                # verify SSL certificates to authenticare
                verify_certs=True,
                # path to ca
                ca_certs="/home/data/ca",
                # path to key
                client_key="/home/data/key",
                # path to cert
                client_cert="/home/data/cert"
            )
            es.cluster.health()
            break

        except exceptions.ConnectionError:
            logger.warning("Still trying to connect to Elasticsearch...")
            counter += 1
            if counter == max_attempts:
                logger.critical("Error of connecting to Elasticsearch. Script will not proceeded. Please investigate if Elasticsearch is up and running.")
                sys.exit(1)

        logger.info("Sleeping 10 seconds...")
        time.sleep(10)

    return es

def get_max_allowed_size(es, percentage_threshold):
    """
    Returns a integer which is calculated as maximal allowed size. We think of <percentage_value_input> as 100% of our total available storage limit.
    """
    i = 0
    data = es.cluster.client.cat.allocation(h="disk.total", bytes="b")
    for node in data.splitlines():
        i = i + int(node)

    max_allowed_size = int( (percentage_threshold * i) / 100.0 )

    return max_allowed_size

def get_first_item(a_dict={}):
    values_view = a_dict.values()
    value_iterator = iter(values_view)
    first_value = next(value_iterator)
    return first_value

def indices_smaller_then_max_allowed_size(index, limit, indices_size_counter, indices_to_delete):
    """
    Returns list of indices which are above threshold limit.
    """
    expected_size = index.size + indices_size_counter

    if indices_size_counter < limit and expected_size < limit:
        indices_size_counter += index.size
        logger.warning(f"Do not add into actionable list: {index.name}, summed disk usage is {indices_size_counter} B and disk limit is {limit} B")
    else:
        logger.warning(f"Add into actionable list: {index.name}, summed disk usage is {indices_size_counter} B and disk limit is {limit} B")
        indices_to_delete.append(index.name)

    return indices_to_delete, indices_size_counter

def get_actionable_indices(es, max_allowed_size, index_name_prefixes):
    """
    This function returns a list of indices which will be used in deletion process.
    """
    class index_struct:
      def __init__(self, name, size, creation_date):
        self.name = name
        self.size = size
        self.creation_date = creation_date

    """
    Appends index into the list of indices with their name, size and creation_date.
    """
    indices = []
    for index_name_prefix in index_name_prefixes:
        for name in es.indices.get_alias(index=index_name_prefix + "*").keys():
            size = int(get_first_item(es.indices.stats(index=name)["indices"][name]["total"]["store"]))
            creation_date = int(es.indices.get(index=name)[name]["settings"]["index"]["creation_date"])
            indices.append(index_struct(name, size, creation_date))

    """
    Iterates through sorted indices (using reverse=True meaning oldest indices will be deleted first)
    and calculates if index is smaller or bigger then max allowed size.
    """
    indices_to_delete = []
    indices_size_counter = 0
    for index in sorted(indices, key=lambda x: x.creation_date, reverse=True):
        indices_to_delete, indices_size_counter = indices_smaller_then_max_allowed_size(index, max_allowed_size, indices_size_counter, indices_to_delete)

    return indices_to_delete

def delete_indices(es, indices_to_delete):
    """
    Delete actionable indices pasted from get_actionable_indices() function.
    """
    for index in indices_to_delete:
        try:
            es.indices.delete(index=index)
            try:
                logger.warning(f"Deleted index {index}")
            except:
                pass
        except ValueError as e:
            try:
                logger.exception(f"Error deleting index {index}", extra={
                    "exception": e
                })
            except:
                pass
    return

def main():
    global index_name_prefixes
    global elasticsearch_host
    global percentage_threshold
    global logger

    # Add debug, verbose and dry_run command-line options.
    # sys.argv[1:] removes the script name.
    parser = argument_parser(sys.argv[1:])

    # Configure logging with provided or default loglevel.
    logger = logging.getLogger("lightweightCurator")
    output_log_config(parser.loglevel)

    # Initial validation of environment variables.
    env_validation(index_name_prefixes, elasticsearch_host)

    # Index name prefixes from comma-separated string.
    index_name_prefixes = index_name_prefixes.split(",")

    # Connect to the Elasticsearch.
    es = es_connect(elasticsearch_host)

    logger.warning(f"""Searching through indices sorted by the age to find and remove first oldest index which exceeds total storage threshold,
    Value for total storage threshold is set to {percentage_threshold}%,
    Host name is {elasticsearch_host}""")

    # Get list of actionable indices.
    indices_to_delete = get_actionable_indices(es, get_max_allowed_size(es, percentage_threshold), index_name_prefixes)

    # For development purpose.
    if parser.dry:
        print(indices_to_delete)
        sys.exit(1)

    # Delete actionable indices.
    delete_indices(es, indices_to_delete)

if __name__ == "__main__":
    main()
