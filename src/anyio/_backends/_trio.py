import socket
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from types import TracebackType
from typing import (
    Any, Awaitable, Callable, Coroutine, Dict, Generic, List, NoReturn, Optional, Tuple, Type,
    TypeVar, Union)

import trio.from_thread
from outcome import Error, Value
from trio.to_thread import run_sync

from .. import TaskInfo, abc
from .._core._eventloop import claim_worker_thread
from .._core._exceptions import (
    BrokenResourceError, BusyResourceError, ClosedResourceError, EndOfStream)
from .._core._exceptions import ExceptionGroup as BaseExceptionGroup
from .._core._sockets import convert_ipv6_sockaddr
from .._core._synchronization import ResourceGuard
from ..abc import IPSockAddrType, UDPPacketType

try:
    from trio import lowlevel as trio_lowlevel
except ImportError:
    from trio import hazmat as trio_lowlevel
    from trio.hazmat import wait_readable, wait_writable
else:
    from trio.lowlevel import wait_readable, wait_writable

T_Retval = TypeVar('T_Retval')
T_SockAddr = TypeVar('T_SockAddr', str, IPSockAddrType)


#
# Event loop
#

run = trio.run


#
# Miscellaneous
#

sleep = trio.sleep


#
# Timeouts and cancellation
#

abc.CancelScope.register(trio.CancelScope)

CancelledError = trio.Cancelled
checkpoint = trio.lowlevel.checkpoint
CancelScope = trio.CancelScope
current_effective_deadline = trio.current_effective_deadline
current_time = trio.current_time


#
# Task groups
#

class ExceptionGroup(BaseExceptionGroup, trio.MultiError):
    pass


class TaskGroup(abc.TaskGroup):
    def __init__(self):
        self._active = False
        self._nursery_manager = trio.open_nursery()
        self.cancel_scope = None

    async def __aenter__(self):
        self._active = True
        self._nursery = await self._nursery_manager.__aenter__()
        self.cancel_scope = self._nursery.cancel_scope
        return self

    async def __aexit__(self, exc_type: Optional[Type[BaseException]],
                        exc_val: Optional[BaseException],
                        exc_tb: Optional[TracebackType]) -> Optional[bool]:
        try:
            return await self._nursery_manager.__aexit__(exc_type, exc_val, exc_tb)
        except trio.MultiError as exc:
            raise ExceptionGroup(exc.exceptions) from None
        finally:
            self._active = False

    def spawn(self, func: Callable, *args, name=None) -> None:
        if not self._active:
            raise RuntimeError('This task group is not active; no new tasks can be spawned.')

        self._nursery.start_soon(func, *args, name=name)

    async def start(self, func: Callable[..., Coroutine], *args, name=None):
        if not self._active:
            raise RuntimeError('This task group is not active; no new tasks can be spawned.')

        return await self._nursery.start(func, *args, name=name)

#
# Threads
#


async def run_sync_in_worker_thread(
        func: Callable[..., T_Retval], *args, cancellable: bool = False,
        limiter: Optional[trio.CapacityLimiter] = None) -> T_Retval:
    def wrapper():
        with claim_worker_thread('trio'):
            return func(*args)

    return await run_sync(wrapper, cancellable=cancellable, limiter=limiter)

run_async_from_thread = trio.from_thread.run
run_sync_from_thread = trio.from_thread.run_sync


class BlockingPortal(abc.BlockingPortal):
    def __init__(self):
        super().__init__()
        self._token = trio.lowlevel.current_trio_token()

    def _spawn_task_from_thread(self, func: Callable, args: tuple, kwargs: Dict[str, Any],
                                name, future: Future) -> None:
        return trio.from_thread.run_sync(
            partial(self._task_group.spawn, name=name), self._call_func, func, args, kwargs,
            future, trio_token=self._token)


#
# Subprocesses
#

@dataclass
class ReceiveStreamWrapper(abc.ByteReceiveStream):
    _stream: trio.abc.ReceiveStream

    async def receive(self, max_bytes: Optional[int] = None) -> bytes:
        data = await self._stream.receive_some(max_bytes)
        if data:
            return data
        else:
            raise EndOfStream

    async def aclose(self) -> None:
        await self._stream.aclose()


@dataclass
class SendStreamWrapper(abc.ByteSendStream):
    _stream: trio.abc.SendStream

    async def send(self, item: bytes) -> None:
        await self._stream.send_all(item)

    async def aclose(self) -> None:
        await self._stream.aclose()


@dataclass
class Process(abc.Process):
    _process: trio.Process
    _stdin: Optional[abc.ByteSendStream]
    _stdout: Optional[abc.ByteReceiveStream]
    _stderr: Optional[abc.ByteReceiveStream]

    async def aclose(self) -> None:
        if self._stdin:
            await self._stdin.aclose()
        if self._stdout:
            await self._stdout.aclose()
        if self._stderr:
            await self._stderr.aclose()

        await self.wait()

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def send_signal(self, signal: int) -> None:
        self._process.send_signal(signal)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode

    @property
    def stdin(self) -> Optional[abc.ByteSendStream]:
        return self._stdin

    @property
    def stdout(self) -> Optional[abc.ByteReceiveStream]:
        return self._stdout

    @property
    def stderr(self) -> Optional[abc.ByteReceiveStream]:
        return self._stderr


async def open_process(command, *, shell: bool, stdin: int, stdout: int, stderr: int):
    process = await trio.open_process(command, stdin=stdin, stdout=stdout, stderr=stderr,
                                      shell=shell)
    stdin_stream = SendStreamWrapper(process.stdin) if process.stdin else None
    stdout_stream = ReceiveStreamWrapper(process.stdout) if process.stdout else None
    stderr_stream = ReceiveStreamWrapper(process.stderr) if process.stderr else None
    return Process(process, stdin_stream, stdout_stream, stderr_stream)


#
# Sockets and networking
#

class _TrioSocketMixin(Generic[T_SockAddr]):
    def __init__(self, trio_socket):
        self._trio_socket = trio_socket
        self._closed = False

    def _check_closed(self) -> None:
        if self._closed:
            raise ClosedResourceError
        if self._trio_socket.fileno() < 0:
            raise BrokenResourceError

    @property
    def _raw_socket(self) -> socket.socket:
        return self._trio_socket._sock

    async def aclose(self) -> None:
        if self._trio_socket.fileno() >= 0:
            self._closed = True
            self._trio_socket.close()

    def _convert_socket_error(self, exc: BaseException) -> 'NoReturn':
        if isinstance(exc, trio.ClosedResourceError):
            raise ClosedResourceError from exc
        elif self._trio_socket.fileno() < 0 and self._closed:
            raise ClosedResourceError from None
        elif isinstance(exc, OSError):
            raise BrokenResourceError from exc
        else:
            raise exc


class SocketStream(_TrioSocketMixin, abc.SocketStream):
    def __init__(self, trio_socket):
        super().__init__(trio_socket)
        self._receive_guard = ResourceGuard('reading from')
        self._send_guard = ResourceGuard('writing to')

    async def receive(self, max_bytes: int = 65536) -> bytes:
        with self._receive_guard:
            try:
                data = await self._trio_socket.recv(max_bytes)
            except BaseException as exc:
                self._convert_socket_error(exc)

            if data:
                return data
            else:
                raise EndOfStream

    async def send(self, item: bytes) -> None:
        with self._send_guard:
            view = memoryview(item)
            while view:
                try:
                    bytes_sent = await self._trio_socket.send(view)
                except BaseException as exc:
                    self._convert_socket_error(exc)

                view = view[bytes_sent:]

    async def send_eof(self) -> None:
        self._trio_socket.shutdown(socket.SHUT_WR)


class SocketListener(_TrioSocketMixin, abc.SocketListener):
    def __init__(self, raw_socket: socket.SocketType):
        super().__init__(trio.socket.from_stdlib_socket(raw_socket))
        self._accept_guard = ResourceGuard('accepting connections from')

    async def accept(self) -> SocketStream:
        with self._accept_guard:
            try:
                trio_socket, _addr = await self._trio_socket.accept()
            except BaseException as exc:
                self._convert_socket_error(exc)

        if trio_socket.family in (socket.AF_INET, socket.AF_INET6):
            trio_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        return SocketStream(trio_socket)


class UDPSocket(_TrioSocketMixin[IPSockAddrType], abc.UDPSocket):
    def __init__(self, trio_socket):
        super().__init__(trio_socket)
        self._receive_guard = ResourceGuard('reading from')
        self._send_guard = ResourceGuard('writing to')

    async def receive(self) -> Tuple[bytes, IPSockAddrType]:
        with self._receive_guard:
            try:
                data, addr = await self._trio_socket.recvfrom(65536)
                return data, convert_ipv6_sockaddr(addr)
            except BaseException as exc:
                self._convert_socket_error(exc)

    async def send(self, item: UDPPacketType) -> None:
        with self._send_guard:
            try:
                await self._trio_socket.sendto(*item)
            except BaseException as exc:
                self._convert_socket_error(exc)


class ConnectedUDPSocket(_TrioSocketMixin[IPSockAddrType], abc.ConnectedUDPSocket):
    def __init__(self, trio_socket):
        super().__init__(trio_socket)
        self._receive_guard = ResourceGuard('reading from')
        self._send_guard = ResourceGuard('writing to')

    async def receive(self) -> bytes:
        with self._receive_guard:
            try:
                return await self._trio_socket.recv(65536)
            except BaseException as exc:
                self._convert_socket_error(exc)

    async def send(self, item: bytes) -> None:
        with self._send_guard:
            try:
                await self._trio_socket.send(item)
            except BaseException as exc:
                self._convert_socket_error(exc)


async def connect_tcp(host: str, port: int,
                      local_address: Optional[IPSockAddrType] = None) -> SocketStream:
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    trio_socket = trio.socket.socket(family)
    trio_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if local_address:
        await trio_socket.bind(local_address)

    try:
        await trio_socket.connect((host, port))
    except BaseException:
        trio_socket.close()
        raise

    return SocketStream(trio_socket)


async def connect_unix(path: str) -> SocketStream:
    trio_socket = trio.socket.socket(socket.AF_UNIX)
    try:
        await trio_socket.connect(path)
    except BaseException:
        trio_socket.close()
        raise

    return SocketStream(trio_socket)


async def create_udp_socket(
    family: socket.AddressFamily,
    local_address: Optional[IPSockAddrType],
    remote_address: Optional[IPSockAddrType],
    reuse_port: bool
) -> Union[UDPSocket, ConnectedUDPSocket]:
    trio_socket = trio.socket.socket(family=family, type=socket.SOCK_DGRAM)

    if reuse_port:
        trio_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

    if local_address:
        await trio_socket.bind(local_address)

    if remote_address:
        await trio_socket.connect(remote_address)
        return ConnectedUDPSocket(trio_socket)
    else:
        return UDPSocket(trio_socket)


getaddrinfo = trio.socket.getaddrinfo
getnameinfo = trio.socket.getnameinfo


async def wait_socket_readable(sock):
    try:
        await wait_readable(sock)
    except trio.ClosedResourceError as exc:
        raise ClosedResourceError().with_traceback(exc.__traceback__) from None
    except trio.BusyResourceError:
        raise BusyResourceError('reading from') from None


async def wait_socket_writable(sock):
    try:
        await wait_writable(sock)
    except trio.ClosedResourceError as exc:
        raise ClosedResourceError().with_traceback(exc.__traceback__) from None
    except trio.BusyResourceError:
        raise BusyResourceError('writing to') from None


#
# Synchronization
#

abc.Event.register(trio.Event)
abc.CapacityLimiter.register(trio.CapacityLimiter)

Event = trio.Event
CapacityLimiter = trio.CapacityLimiter


def current_default_thread_limiter():
    return trio.to_thread.current_default_thread_limiter()


#
# Signal handling
#

open_signal_receiver = trio.open_signal_receiver


#
# Testing and debugging
#

def get_current_task() -> TaskInfo:
    task = trio_lowlevel.current_task()

    parent_id = None
    if task.parent_nursery and task.parent_nursery.parent_task:
        parent_id = id(task.parent_nursery.parent_task)

    return TaskInfo(id(task), parent_id, task.name, task.coro)


def get_running_tasks() -> List[TaskInfo]:
    root_task = trio_lowlevel.current_root_task()
    task_infos = [TaskInfo(id(root_task), None, root_task.name, root_task.coro)]
    nurseries = root_task.child_nurseries
    while nurseries:
        new_nurseries: List[trio.Nursery] = []
        for nursery in nurseries:
            for task in nursery.child_tasks:
                task_infos.append(
                    TaskInfo(id(task), id(nursery.parent_task), task.name, task.coro))
                new_nurseries.extend(task.child_nurseries)

        nurseries = new_nurseries

    return task_infos


def wait_all_tasks_blocked():
    import trio.testing
    return trio.testing.wait_all_tasks_blocked()


class TestRunner(abc.TestRunner):
    def __init__(self, **options):
        from collections import deque
        from queue import Queue

        self._call_queue = Queue()
        self._result_queue = deque()
        self._stop_event: Optional[trio.Event] = None
        self._nursery: Optional[trio.Nursery] = None
        self._options = options

    async def _trio_main(self) -> None:
        self._stop_event = trio.Event()
        async with trio.open_nursery() as self._nursery:
            await self._stop_event.wait()

    async def _call_func(self, func, args, kwargs):
        try:
            retval = await func(*args, **kwargs)
        except BaseException as exc:
            self._result_queue.append(Error(exc))
        else:
            self._result_queue.append(Value(retval))

    def _main_task_finished(self, outcome) -> None:
        self._nursery = None

    def close(self) -> None:
        if self._stop_event:
            self._stop_event.set()
            while self._nursery is not None:
                self._call_queue.get()()

    def call(self, func: Callable[..., Awaitable], *args, **kwargs):
        if self._nursery is None:
            trio.lowlevel.start_guest_run(
                self._trio_main, run_sync_soon_threadsafe=self._call_queue.put,
                done_callback=self._main_task_finished, **self._options)
            while self._nursery is None:
                self._call_queue.get()()

        self._nursery.start_soon(self._call_func, func, args, kwargs)
        while not self._result_queue:
            self._call_queue.get()()

        outcome = self._result_queue.pop()
        return outcome.unwrap()
