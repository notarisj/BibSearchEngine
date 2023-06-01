import argparse
import heapq
import json
import socket
import sys
import threading
from collections import defaultdict

from hashlib import sha256

from indexer.index_creator import IndexCreator
from Ranker.Search import SearchEngine
from api_requester import APIRequester
from configurations.ini_config import IniConfig
from configurations.json_config import JsonConfig
from distributed.config_manager import ConfigManager
from distributed.fast_json_loader import FastJsonLoader
from distributed.utils import send_request, receive_message, send_message, execute_action


# TODO: Check all blocking parts (see example execute_action with new thread)


def process_data(data):
    return f"Processed data: {data}"


def split_ids(ids, n):
    result = {i: [] for i in range(1, n + 1)}
    for i, _id in enumerate(ids):
        if _id % n == 0:
            result[n].append(_id)
        else:
            result[_id % n].append(_id)
    return result


class DistributedNode:
    def __init__(self, _node_id, _node_host, _node_port, _json_file_path, _load_folder_path, _max_results, db_name,
                 index_collection, metadata_collection, _passphrase, cert_path, key_path, api_url, api_username,
                 api_password):
        self.node_id = _node_id
        self.node_host = _node_host
        self.node_port = _node_port
        self.passphrase = _passphrase
        self.api_url = api_url
        self.api_username = api_username
        self.api_password = api_password
        self.cert_path = cert_path
        self.key_path = key_path
        self.folder_path = _load_folder_path
        self.documents_count = {}
        self.doc_id_sequence = {}
        self.max_results = _max_results
        self.config_manager = ConfigManager(_json_file_path)
        config = self.config_manager.load_config()
        self.neighbour_nodes = config['nodes']
        self.neighbour_nodes[_node_id - 1]['alive'] = True
        self.current_leader = None
        self.db = FastJsonLoader(folder_path=self.folder_path,
                                 documents_per_file=1000,
                                 node_id=self.node_id,
                                 node_count=len(self.neighbour_nodes))
        self.indexer = IndexCreator(self.db,
                                    db_name=db_name,
                                    index_collection=index_collection,
                                    metadata_collection=metadata_collection)
        self.engine = SearchEngine(self.indexer, max_results=10000)
        self.db.doc_id = self.node_id
        self.db.node_count = len(config['nodes'])

    def handle_request(self, request):
        data = json.loads(request)
        action = data.get('action', '')

        # with ThreadPoolExecutor(max_workers=1) as executor:
        # executor.submit(self.handle_insert, data) --> change to that
        if action == 'heartbeat':
            return json.dumps({'status': 'OK'})
        elif action == 'load_documents':
            self.db.load_documents()
            return json.dumps({'status': 'OK'})
        elif action == 'load_index':
            self.indexer.create_load_index()
            self.engine.bm25f.update_total_docs(self.indexer.index_metadata.total_docs)
            return json.dumps({'status': 'OK'})
        elif action == 'get_data':
            results = self.handle_get_data(data)
            return json.dumps({'status': 'OK', 'results': results})
        elif action == 'update_alive':
            results = data.get('attr1', '')
            key = next(iter(results.keys()))
            self.neighbour_nodes[int(key) - 1]['alive'] = results[key]
            self.config_manager.save_config(self.neighbour_nodes)
            return json.dumps({'status': 'OK'})
        elif action == 'search_ids':
            results = self.search_ids(data)
            return json.dumps({'status': 'OK', 'results': results})
        elif action == 'set_starting_doc_id':
            return json.dumps({'doc_id': data.get('doc_id', ''), 'status': 'OK'})
        elif action == 'insert_docs':
            self.db.insert_documents(data.get('new_docs', ''))
            return json.dumps({'status': 'OK'})
        elif action == 'get_config':
            return json.dumps({'status': 'OK', "config": self.neighbour_nodes})
        elif action == 'set_leader':
            self.current_leader = data['leader']
            print(f"Node {self.node_id}: Leader updated to {self.current_leader['id']}")
            return json.dumps({'status': 'OK'})
        else:
            return json.dumps({'error': 'Unknown request'})

    def get_leader(self):
        sorted_nodes = sorted(self.neighbour_nodes, key=lambda x: x['id'])
        return sorted_nodes[0]

    def update_alive_nodes(self):
        alive_nodes = []

        for _node in self.neighbour_nodes:
            if _node['id'] == self.node_id:
                alive_nodes.append(_node)
                continue

            try:
                response = send_request((_node['host'], _node['port']), {
                    'action': 'heartbeat'
                })

                if response and response.get('status') == 'OK':
                    alive_nodes.append(_node)
            except Exception as e:
                print(f"Failed to send heartbeat to node {_node['id']}: {e}")

        return alive_nodes

    def notify_nodes_of_leader(self, leader):
        for _node in self.neighbour_nodes:
            if _node['id'] != self.node_id:
                try:
                    send_request((_node['host'], _node['port']), {
                        'action': 'set_leader',
                        'leader': leader
                    })
                    print(f"Node {self.node_id}: Notified node {_node['id']} of the new leader {leader['id']}")
                except Exception as e:
                    print(
                        f"Node {self.node_id}: Failed to notify node {_node['id']} of the new leader {leader['id']}."
                        f" {e}")

    # run without SSL encryption
    def run(self):
        if self.neighbour_nodes[self.node_id - 1]['leader']:
            heartbeat_thread = threading.Thread(target=self.check_heartbeats)
            heartbeat_thread.start()
        elif not self.neighbour_nodes[self.node_id - 1]['first_boot']:

            # get config from other nodes
            for _node in self.neighbour_nodes:
                if self.node_id != _node['id']:
                    response = send_request((_node['host'], _node['port']), {
                        'action': 'get_config'
                    })
                    if response.get('status') == 'OK':
                        self.neighbour_nodes = response['config']
                        break
            # Start db and index
            self.db.load_documents()
            self.indexer.create_load_index()
            self.engine.bm25f.update_total_docs(self.indexer.index_metadata.total_docs)
            # inform other nodes for recovery
            self.neighbour_nodes[self.node_id - 1]['alive'] = True
            self.config_manager.save_config(self.neighbour_nodes)
            execute_action('update_alive', self.neighbour_nodes, self.node_id, {self.node_id: True})

        # Update first_boot to false. If server crashes when restart
        # it will perform additional actions
        self.neighbour_nodes[self.node_id - 1]['first_boot'] = False
        self.config_manager.save_config(self.neighbour_nodes)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0) as sock:
            sock.bind((self.node_host, self.node_port))
            sock.listen(5)
            print(f"Node {self.node_id} listening on port {self.node_port}")

            while True:
                conn, addr = sock.accept()
                print(f"Node {self.node_id}: Connection accepted from {addr}")
                threading.Thread(target=self.handle_request_wrapper, args=(conn,)).start()

    def handle_request_wrapper(self, conn):
        request = receive_message(conn)
        print(f"Node {self.node_id}: Received request: {request[:100]}")
        response = self.handle_request(request)
        print(f"Node {self.node_id}: Sending response: {response[:100]}")
        send_message(response, conn)

    def check_heartbeats(self):
        execute_action('heartbeat', self.neighbour_nodes, self.node_id)
        self.send_load_documents()

    # TODO: all leader
    def send_load_documents(self):
        # load documents for leader
        load_documents_thread = threading.Thread(target=self.db.load_documents)
        load_documents_thread.start()
        # send load_documents commands to all other nodes
        execute_action('load_documents', self.neighbour_nodes, self.node_id)
        self.send_create_index()

    def send_create_index(self):
        # load index for leader
        create_load_index_thread = threading.Thread(target=self.indexer.create_load_index)
        create_load_index_thread.start()
        execute_action('load_index', self.neighbour_nodes, self.node_id)
        create_load_index_thread.join()
        # change total docs in bm25f for leader
        self.engine.bm25f.update_total_docs(self.indexer.index_metadata.total_docs)

    def get_shard(self, key):
        num_shards = len(self.neighbour_nodes)
        shard_id = int(sha256(key.encode()).hexdigest(), 16) % num_shards
        return self.neighbour_nodes[shard_id]

    def forward_request(self, _node, data, to_leader=False):
        node_addr = (_node['host'], _node['port'])

        try:
            response = send_request(node_addr, data)
            return json.dumps(response)
        except Exception as e:
            if to_leader:
                print(f"Node {self.node_id}: Failed to forward request to leader {_node['id']}. {e}")
                self.neighbour_nodes = self.update_alive_nodes()
                leader = self.get_leader()
                self.current_leader = leader
                self.notify_nodes_of_leader(leader)

                if leader['id'] == self.node_id:
                    print(f"Node {self.node_id} is now the leader")
                    return json.dumps({
                        'worker_id': self.node_id,
                        'result': process_data(data['data'])
                    })
                else:
                    print(f"Node {self.node_id}: Forwarding request to new leader {leader['id']}")
                    leader_addr = (leader['host'], leader['port'])
                    response = send_request(leader_addr, data)
                    return json.dumps(response)
            else:
                print(f"Node {self.node_id}: Failed to forward request to node {_node['id']}. {e}")
                return json.dumps({'error': f"Failed to forward request to node {_node['id']}"})

    def update_config(self, _config_file):
        with open(_config_file, 'w') as f:
            config = json.load(f)
            config['nodes'] = self.neighbour_nodes
            json.dump(config, f, indent=4)

    # TODO: use forward_request function instead of sending plain request
    def handle_get_data(self, data):
        ids = data.get('ids', '')
        if not data.get('forwarded'):
            results = []
            ids_per_node = split_ids(ids, self.db.node_count)
            for _node, value in ids_per_node.items():
                # TODO: THIS IS STUPID. MUST CHANGE!
                if isinstance(_node, dict):
                    _node = _node['id']
                if not self.neighbour_nodes[_node - 1]['alive']:
                    continue

                if _node == self.node_id:
                    results.extend(self.db.get_data(value))
                else:
                    data['forwarded'] = True
                    data['ids'] = value
                    try:
                        response = send_request((self.neighbour_nodes[_node - 1]['host'],
                                                 self.neighbour_nodes[_node - 1]['port']), {
                                                    'action': 'get_data', 'forwarded': True, 'ids': value
                                                })
                        # response = self.forward_request(self.neighbor_nodes[node - 1], data)
                        # TODO: This is overhead. Needs optimization. It returns a string and
                        #  is needed to convert to JSON.
                        results.extend(json.loads(response.get('results'))['results'])
                    except socket.error:
                        self.neighbour_nodes[self.neighbour_nodes[_node - 1]]['alive'] = False
                        self.config_manager.save_config(self.neighbour_nodes)
                        execute_action('update_alive', self.neighbour_nodes, self.node_id, {_node['id']: False})
                        api_requester = APIRequester(self.api_url, self.api_username, self.api_password)
                        api_response = api_requester.post_update_config_endpoint(self.neighbour_nodes)
                        print('Update api config response:', api_response)

            return results
        else:
            return json.dumps({'results': self.db.get_data(ids)})

    def search_ids(self, data):
        query = data.get('query', '')
        if not data.get('forwarded'):
            results = defaultdict(float)
            for _node in self.neighbour_nodes:
                if not _node['alive']:
                    continue

                if _node['id'] == self.node_id:
                    results.update(self.engine.search(query))
                else:
                    data['forwarded'] = True
                    try:
                        response = send_request(
                            (_node['host'], _node['port']), {
                                'action': 'search_ids', 'forwarded': True, 'query': query
                            })

                        # response = self.forward_request(self.neighbor_nodes[node - 1], data)
                        # TODO: This is overhead. Needs optimization. It returns a string and
                        #  is needed to convert to JSON.
                        results.update(json.loads(response.get('results'))['results'])
                    except socket.error:
                        self.neighbour_nodes[_node['id'] - 1]['alive'] = False
                        self.config_manager.save_config(self.neighbour_nodes)
                        execute_action('update_alive', self.neighbour_nodes, self.node_id, {_node['id']: False})
                        api_requester = APIRequester(self.api_url, self.api_username, self.api_password)
                        api_response = api_requester.post_update_config_endpoint(self.neighbour_nodes)
                        print('Update api config response:', api_response)
            return list(map(int, heapq.nlargest(self.max_results, results.keys(), key=results.get)))
        else:
            return json.dumps({'results': self.engine.search(query)})


if __name__ == '__main__':
    sys.path.insert(0, '../indexer/')
    parser = argparse.ArgumentParser()
    parser.add_argument("--json_config", help="Path to the config.json file", required=True)
    parser.add_argument("--ini_config", help="Path to the config.ini file", required=True)
    parser.add_argument("--node_id", type=int, help="ID of the node", required=True)
    args = parser.parse_args()

    ini_config = IniConfig(args.ini_config)
    json_config = JsonConfig(args.json_config)
    nodes = json_config.get_property('nodes')[args.node_id - 1]

    d_node = DistributedNode(_node_id=args.node_id,
                             _node_host=nodes['host'],
                             _node_port=nodes['port'],
                             _json_file_path=args.json_config,
                             _load_folder_path=ini_config.get_property('NodeSettings', 'load_folder_path'),
                             _max_results=int(ini_config.get_property('FastJsonLoader', 'max_results')),
                             db_name=ini_config.get_property('Database', 'db_name'),
                             index_collection=ini_config.get_property('Database', 'index_collection'),
                             metadata_collection=ini_config.get_property('Database', 'metadata_collection'),
                             cert_path=ini_config.get_property('SSL', 'cert_path'),
                             key_path=ini_config.get_property('SSL', 'key_path'),
                             _passphrase=ini_config.get_property('SSL', 'passphrase'),
                             api_url=ini_config.get_property('API', 'url'),
                             api_username=ini_config.get_property('API', 'username'),
                             api_password=ini_config.get_property('API', 'password'))
    d_node.run()
