import os
import asyncio
from typing import Any, Callable, Coroutine, TypeVar, Optional, Dict, Generic, Tuple
import socket
import random

DEBUG = int(os.getenv("DEBUG", default="0"))
DEBUG_DISCOVERY = int(os.getenv("DEBUG_DISCOVERY", default="0"))
VERSION = "0.0.1"

exo_text = r"""
  _____  _____  
 / _ \ \/ / _ \ 
|  __/>  < (_) |
 \___/_/\_\___/ 
    """

def find_available_port(host: str = '', min_port: int = 49152, max_port: int = 65535) -> int:
    used_ports_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.exo_used_ports')

    def read_used_ports():
        if os.path.exists(used_ports_file):
            with open(used_ports_file, 'r') as f:
                return [int(line.strip()) for line in f if line.strip().isdigit()]
        return []

    def write_used_port(port, used_ports):
        with open(used_ports_file, 'w') as f:
            print(used_ports[-19:])
            for p in used_ports[-19:] + [port]:
                f.write(f"{p}\n")

    used_ports = read_used_ports()
    available_ports = set(range(min_port, max_port + 1)) - set(used_ports)

    while available_ports:
        port = random.choice(list(available_ports))
        if DEBUG >= 2: print(f"Trying to find available port {port=}")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
            write_used_port(port, used_ports)
            return port
        except socket.error:
            available_ports.remove(port)

    raise RuntimeError("No available ports in the specified range")

def print_exo():
    print(exo_text)

def print_yellow_exo():
    yellow = "\033[93m"  # ANSI escape code for yellow
    reset = "\033[0m"    # ANSI escape code to reset color
    print(f"{yellow}{exo_text}{reset}")

def terminal_link(uri, label=None):
    if label is None: 
        label = uri
    parameters = ''

    # OSC 8 ; params ; URI ST <name> OSC 8 ;; ST 
    escape_mask = '\033]8;{};{}\033\\{}\033]8;;\033\\'

    return escape_mask.format(parameters, uri, label)

T = TypeVar('T')
K = TypeVar('K')

class AsyncCallback(Generic[T]):
    def __init__(self) -> None:
        self.condition: asyncio.Condition = asyncio.Condition()
        self.result: Optional[Tuple[T, ...]] = None
        self.observers: list[Callable[..., None]] = []

    async def wait(self,
                   check_condition: Callable[..., bool],
                   timeout: Optional[float] = None) -> Tuple[T, ...]:
        async with self.condition:
            await asyncio.wait_for(self.condition.wait_for(lambda: self.result is not None and check_condition(*self.result)), timeout)
            assert self.result is not None  # for type checking
            return self.result

    def on_next(self, callback: Callable[..., None]) -> None:
        self.observers.append(callback)

    def set(self, *args: T) -> None:
        self.result = args
        for observer in self.observers:
            observer(*args)
        asyncio.create_task(self.notify())

    async def notify(self) -> None:
        async with self.condition:
            self.condition.notify_all()

class AsyncCallbackSystem(Generic[K, T]):
    def __init__(self) -> None:
        self.callbacks: Dict[K, AsyncCallback[T]] = {}

    def register(self, name: K) -> AsyncCallback[T]:
        if name not in self.callbacks:
            self.callbacks[name] = AsyncCallback[T]()
        return self.callbacks[name]

    def deregister(self, name: K) -> None:
        if name in self.callbacks:
            del self.callbacks[name]

    def trigger(self, name: K, *args: T) -> None:
        if name in self.callbacks:
            self.callbacks[name].set(*args)

    def trigger_all(self, *args: T) -> None:
        for callback in self.callbacks.values():
            callback.set(*args)