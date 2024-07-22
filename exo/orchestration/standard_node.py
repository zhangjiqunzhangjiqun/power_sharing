import numpy as np
import json
import asyncio
import uuid
import time
from typing import List, Dict, Optional, Tuple, Union
from exo.networking import Discovery, PeerHandle, Server
from exo.inference.inference_engine import InferenceEngine, Shard
from .node import Node
from exo.topology.topology import Topology
from exo.topology.device_capabilities import device_capabilities
from exo.topology.partitioning_strategy import Partition, PartitioningStrategy, map_partitions_to_shards
from exo import DEBUG
from exo.helpers import AsyncCallbackSystem
from exo.viz.topology_viz import TopologyViz

class StandardNode(Node):
    def __init__(self, id: str, server: Server, inference_engine: InferenceEngine, discovery: Discovery, partitioning_strategy: PartitioningStrategy = None, max_generate_tokens: int = 256, chatgpt_api_endpoint: Optional[str] = None, web_chat_url: Optional[str] = None, disable_tui: Optional[bool] = False):
        self.id = id
        self.inference_engine = inference_engine
        self.server = server
        self.discovery = discovery
        self.partitioning_strategy = partitioning_strategy
        self.peers: List[PeerHandle] = {}
        self.topology: Topology = Topology()
        self.device_capabilities = device_capabilities()
        self.buffered_token_output: Dict[str, Tuple[List[int], bool]] = {}
        self.topology_viz = TopologyViz(chatgpt_api_endpoint=chatgpt_api_endpoint, web_chat_url=web_chat_url) if not disable_tui else None
        self.max_generate_tokens = max_generate_tokens
        self._on_token = AsyncCallbackSystem[str, Tuple[str, List[int], bool]]()
        self._on_opaque_status = AsyncCallbackSystem[str, str]()
        self._on_opaque_status.register("node_status").on_next(self.on_node_status)

    def on_node_status(self, request_id, opaque_status):
        try:
            status_data = json.loads(opaque_status)
            if status_data.get("type", "") == "node_status":
                if status_data.get("status", "").startswith("start_"):
                    self.current_topology.active_node_id = status_data.get("node_id")
                elif status_data.get("status", "").startswith("end_"):
                    if status_data.get("node_id") == self.current_topology.active_node_id:
                        self.current_topology.active_node_id = None
            if self.topology_viz: self.topology_viz.update_visualization(self.current_topology, self.partitioning_strategy.partition(self.current_topology))
        except json.JSONDecodeError:
            pass

    async def start(self, wait_for_peers: int = 0) -> None:
        await self.server.start()
        await self.discovery.start()
        await self.update_peers(wait_for_peers)
        await self.collect_topology()
        if DEBUG >= 2: print(f"Collected topology: {self.topology}")
        asyncio.create_task(self.periodic_topology_collection(5))

    async def stop(self) -> None:
        await self.discovery.stop()
        await self.server.stop()

    async def process_prompt(self, base_shard: Shard, prompt: str, request_id: Optional[str] = None, inference_state: Optional[str] = None) -> Optional[np.ndarray]:
        shard = self.get_current_shard(base_shard)
        asyncio.create_task(self.broadcast_opaque_status(request_id, json.dumps({"type": "node_status", "node_id": self.id, "status": "start_process_prompt", "base_shard": base_shard.to_dict(), "shard": shard.to_dict(), "prompt": prompt, "inference_state": inference_state, "request_id": request_id})))
        start_time = time.perf_counter_ns()
        resp = await self._process_prompt(base_shard, prompt, request_id, inference_state)
        end_time = time.perf_counter_ns()
        elapsed_time_ns = end_time - start_time
        asyncio.create_task(self.broadcast_opaque_status(request_id, json.dumps({"type": "node_status", "node_id": self.id, "status": "end_process_prompt", "base_shard": base_shard.to_dict(), "shard": shard.to_dict(), "prompt": prompt, "inference_state": inference_state, "request_id": request_id, "elapsed_time_ns": elapsed_time_ns, "result_size": resp.size if resp is not None else 0})))
        return resp

    async def _process_prompt(self, base_shard: Shard, prompt: str, request_id: Optional[str] = None, inference_state: Optional[str] = None) -> Optional[np.ndarray]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        if request_id not in self.buffered_token_output:
            self.buffered_token_output[request_id] = ([], False)
        shard = self.get_current_shard(base_shard)

        if DEBUG >= 2: print(f"[{request_id}] process prompt: {base_shard=} {shard=} {prompt=}")
        if shard.start_layer != 0:
            if DEBUG >= 2: print(f"[{request_id}] forwarding to next shard: {base_shard=} {shard=} {prompt=}")
            await self.forward_to_next_shard(shard, prompt, request_id)
            return

        result, inference_state, is_finished = await self.inference_engine.infer_prompt(shard, prompt, inference_state=inference_state)
        is_finished = is_finished or len(self.buffered_token_output[request_id][0]) >= self.max_generate_tokens
        if is_finished:
            self.buffered_token_output[request_id] = (self.buffered_token_output[request_id][0], True)
        asyncio.create_task(self.broadcast_result(request_id, self.buffered_token_output[request_id][0], is_finished)) # TODO: this is n^2 communication complexity

        if result.size == 1:
            self.buffered_token_output[request_id][0].append(result.item())
            self.trigger_on_token_callbacks(request_id, self.buffered_token_output[request_id][0], is_finished)

        if DEBUG >= 2: print(f"[{request_id}] result size: {result.size}, is finished: {is_finished}, buffered tokens: {len(self.buffered_token_output[request_id][0])}")

        if not is_finished:
            asyncio.create_task(self.forward_to_next_shard(shard, result, request_id, inference_state=inference_state))

        return np.array(self.buffered_token_output[request_id][0]) if len(self.buffered_token_output[request_id][0]) > 0 else None

    async def process_tensor(self, base_shard: Shard, tensor: np.ndarray, request_id: Optional[str] = None, inference_state: Optional[str] = None) -> Optional[np.ndarray]:
        shard = self.get_current_shard(base_shard)
        asyncio.create_task(self.broadcast_opaque_status(request_id, json.dumps({"type": "node_status", "node_id": self.id, "status": "start_process_tensor", "base_shard": base_shard.to_dict(), "shard": shard.to_dict(), "tensor_size": tensor.size, "tensor_shape": tensor.shape, "request_id": request_id, "inference_state": inference_state})))
        start_time = time.perf_counter_ns()
        resp = await self._process_tensor(shard, tensor, request_id, inference_state)
        end_time = time.perf_counter_ns()
        elapsed_time_ns = end_time - start_time
        asyncio.create_task(self.broadcast_opaque_status(request_id, json.dumps({"type": "node_status", "node_id": self.id, "status": "end_process_tensor", "base_shard": base_shard.to_dict(), "shard": shard.to_dict(), "request_id": request_id, "elapsed_time_ns": elapsed_time_ns, "result_size": resp.size if resp is not None else 0})))
        return resp

    async def _process_tensor(self, base_shard: Shard, tensor: np.ndarray, request_id: Optional[str] = None, inference_state: Optional[str] = None) -> Optional[np.ndarray]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        if request_id not in self.buffered_token_output:
            self.buffered_token_output[request_id] = ([], False)
        shard = self.get_current_shard(base_shard)

        try:
            if DEBUG >= 1: print(f"[{request_id}] process_tensor: {tensor.size=} {tensor.shape=}")
            result, inference_state, is_finished = await self.inference_engine.infer_tensor(shard, tensor, inference_state=inference_state)
            is_finished = is_finished or len(self.buffered_token_output[request_id][0]) >= self.max_generate_tokens
            if is_finished:
                self.buffered_token_output[request_id] = (self.buffered_token_output[request_id][0], True)
            asyncio.create_task(self.broadcast_result(request_id, self.buffered_token_output[request_id][0], is_finished)) # TODO: this is n^2 communication complexity

            if result.size == 1:  # we got a new token out
                self.buffered_token_output[request_id][0].append(result.item())
                self.trigger_on_token_callbacks(request_id, self.buffered_token_output[request_id][0], is_finished)
            if DEBUG >= 2: print(f"[{request_id}] result size: {result.size}, is finished: {is_finished}, buffered tokens: {len(self.buffered_token_output[request_id][0])}")

            if not is_finished:
                asyncio.create_task(self.forward_to_next_shard(shard, result, request_id, inference_state=inference_state))

            return np.array(self.buffered_token_output[request_id][0]) if len(self.buffered_token_output[request_id][0]) > 0 else None
        except Exception as e:
            print(f"Error processing tensor for shard {shard}: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def forward_to_next_shard(self, base_shard: Shard, tensor_or_prompt: Union[np.ndarray, str], request_id: str, inference_state: Optional[str] = None) -> None:
        if not self.partitioning_strategy:
            if DEBUG >= 1: print("No partitioning strategy found. Skipping forward.")
            return
        shard = self.get_current_shard(base_shard)

        partitions = self.partitioning_strategy.partition(self.topology)
        shards = map_partitions_to_shards(self.partitioning_strategy.partition(self.topology), base_shard.n_layers, base_shard.model_id)
        current_partition_index = next((i for i, p in enumerate(partitions) if p.node_id == self.id), None)
        if DEBUG >= 1: print(f"Current partition index: {current_partition_index}")
        if current_partition_index is not None:
            next_partition_index = (current_partition_index + 1) % len(partitions)
            next_partition: Partition = partitions[next_partition_index]
            next_shard = shards[next_partition_index]
            if DEBUG >= 2: print(f"Computed next from: {shard}, {self.topology}. Next partition: {next_partition}")

            if next_partition.node_id == self.id:
                if isinstance(tensor_or_prompt, np.ndarray):
                    await self.process_tensor(shard, tensor_or_prompt, request_id, inference_state=inference_state)
                else:
                    await self.process_prompt(shard, tensor_or_prompt, request_id, inference_state=inference_state)
                return

            target_peer = next((p for p in self.peers if p.id() == next_partition.node_id), None)
            if not target_peer:
                raise ValueError(f"Peer for {next_partition} not found")

            if DEBUG >= 1: print(f"Sending tensor_or_prompt to {target_peer.id()}: {tensor_or_prompt}")

            if isinstance(tensor_or_prompt, np.ndarray):
                await target_peer.send_tensor(next_shard, tensor_or_prompt, request_id=request_id, inference_state=inference_state)
            else:
                await target_peer.send_prompt(next_shard, tensor_or_prompt, request_id=request_id, inference_state=inference_state)

    def get_current_shard(self, base_shard: Shard) -> Shard:
        partitions = self.partitioning_strategy.partition(self.topology)
        shards = map_partitions_to_shards(partitions, base_shard.n_layers, base_shard.model_id)
        current_partition_index = next((i for i, p in enumerate(partitions) if p.node_id == self.id), None)
        if current_partition_index is None:
            raise ValueError(f"No current partition found for node: {self.id}")
        return shards[current_partition_index]

    async def reset_shard(self, base_shard: Shard) -> None:
        # Implement shard reset logic
        if DEBUG >= 2: print(f"Resetting shard: {base_shard}")
        self.buffered_token_output = {}
        await self.inference_engine.reset_shard(self.get_current_shard(base_shard))

    async def update_peers(self, wait_for_peers: int = 0) -> None:
        self.peers = await self.discovery.discover_peers(wait_for_peers)
        if DEBUG >= 2: print(f"Starting with the following peers: {self.peers}")
        if DEBUG >= 2: print("Connecting to new peers...")
        for peer in self.peers:
            is_connected = await peer.is_connected()
            if DEBUG >= 2 and is_connected: print(f"Already connected to {peer.id()}: {is_connected}")
            if not is_connected:
                await peer.connect()
                if DEBUG >= 0: print(f"Connected to peer {peer.device_capabilities()} ({peer.id()=})")

    async def periodic_topology_collection(self, interval: int):
        while True:
            await asyncio.sleep(interval)
            try:
                await self.update_peers()
                await self.collect_topology()
            except Exception as e:
                print(f"Error collecting topology: {e}")

            if DEBUG >= 2: print("Topology collection task executed.")
            if DEBUG >= 2: print(f"Current topology: {self.topology}")

    async def get_inference_result(self, request_id: str) -> Tuple[Optional[np.ndarray], bool]:
        if request_id not in self.buffered_token_output:
            return None, False
        return np.array(self.buffered_token_output[request_id][0]), self.buffered_token_output[request_id][1]

    async def collect_topology(self, visited: set[str] = set(), max_depth: int = 4) -> Topology:
        next_topology = Topology()
        next_topology.update_node(self.id, self.device_capabilities)

        if DEBUG >= 2: print(f"Collecting topology {max_depth=} {visited=}")

        prev_visited = visited.copy()
        visited.update(p.id() for p in self.peers)

        for peer in self.peers:
            next_topology.update_node(peer.id(), peer.device_capabilities())
            next_topology.add_edge(self.id, peer.id())

            if peer.id() in prev_visited:
                if DEBUG >= 2: print(f"Already visited {peer.id()}. Skipping...")
                continue

            if max_depth <= 0:
                if DEBUG >= 2: print(f"Max depth reached. Skipping...")
                continue

            try:
                other_topology = await peer.collect_topology(visited, max_depth = max_depth - 1)
                if DEBUG >= 2: print(f"Collected topology from: {peer.id()}: {other_topology}")
                self.topology.merge(other_topology)
            except Exception as e:
                print(f"Error collecting topology from {peer.id()}: {e}")

        next_topology.active_node_id = self.topology.active_node_id # this is not so clean.
        self.topology = next_topology
        if self.topology_viz: self.topology_viz.update_visualization(self.current_topology, self.partitioning_strategy.partition(self.current_topology))
        return next_topology

    # TODO: unify this and collect_topology as global actions
    async def global_reset(self, base_shard: Shard, visited: set[str] = set(), max_depth: int = 2) -> None:
        shard = self.get_current_shard(base_shard)
        await self.reset_shard(shard)

        if DEBUG >= 2: print(f"Global reset {base_shard=} {max_depth=} {visited=}")

        prev_visited = visited.copy()
        visited.update(p.id() for p in self.peers)

        for peer in self.peers:
            if peer.id() in prev_visited:
                if DEBUG >= 2: print(f"Already visited {peer.id()}. Skipping...")
                continue

            if max_depth <= 0:
                if DEBUG >= 2: print(f"Max depth reached. Skipping...")
                continue

            try:
                print(f"Forwarding global reset to peer {peer.id()}")
                await peer.global_reset(base_shard, visited, max_depth = max_depth - 1)
            except Exception as e:
                print(f"Error collecting topology from {peer.id()}: {e}")

    @property
    def on_token(self) -> AsyncCallbackSystem[str, Tuple[str, List[int], bool]]:
        return self._on_token

    @property
    def on_opaque_status(self) -> AsyncCallbackSystem[str, str]:
        return self._on_opaque_status

    def trigger_on_token_callbacks(self, request_id: str, tokens: List[int], is_finished: bool) -> None:
        if DEBUG >= 2: print(f"Triggering all on_token callbacks with {request_id=} num_tokens={len(tokens)} {is_finished=}")
        self.on_token.trigger_all(request_id, tokens, is_finished)

    async def broadcast_result(self, request_id: str, result: List[int], is_finished: bool) -> None:
        async def send_result_to_peer(peer):
            try:
                await asyncio.wait_for(peer.send_result(request_id, result, is_finished), timeout=15.0)
            except asyncio.TimeoutError:
                print(f"Timeout broadcasting result to {peer.id()}")
            except Exception as e:
                print(f"Error broadcasting result to {peer.id()}: {e}")
                import traceback
                traceback.print_exc()

        await asyncio.gather(*[send_result_to_peer(peer) for peer in self.peers], return_exceptions=True)

    async def broadcast_opaque_status(self, request_id: str, status: str) -> None:
        for peer in self.peers:
            await peer.send_opaque_status(request_id, status)

    @property
    def current_topology(self) -> Topology:
        return self.topology