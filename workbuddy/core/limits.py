"""聊天请求的共享准入闸门：并发上限 + 每分钟请求预算。"""

from __future__ import annotations

import asyncio
import collections
import time
from contextlib import asynccontextmanager

_WINDOW_SECONDS = 60.0


class LimitExceeded(Exception):
    """请求被准入闸门拒绝。"""

    def __init__(self, reason, retry_after=None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


class ConcurrencyGate:
    """并发与 RPM 的上限控制，整个账号池共用一个实例。"""

    def __init__(self, *, concurrency=1, queue_size=0, queue_timeout=None, rpm=None, clock=None):
        self._concurrency = max(1, int(concurrency))
        self._queue_size = max(0, int(queue_size))
        self._queue_timeout = queue_timeout
        self._rpm = int(rpm) if rpm else None
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._active = 0
        self._waiters = collections.deque()
        self._rpm_events = collections.deque()
        self._closed = False

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def _trim_rpm_locked(self, now) -> None:
        while self._rpm_events and now - self._rpm_events[0] >= _WINDOW_SECONDS:
            self._rpm_events.popleft()

    async def _wait_rpm(self, max_wait: float | None) -> None:
        """等到 RPM 窗口腾出位置再计数；超出等待上限才拒绝。

        Host 会把聊天、记忆回写、表达检查等任务并发打进来，瞬时超过每分钟预算很常见。
        直接抛 ``LimitExceeded`` 会让这些请求直接失败，所以这里改为排队等待。
        """
        if self._rpm is None:
            return
        limit = max_wait if max_wait is not None else 30.0
        waited = 0.0
        while True:
            async with self._lock:
                if self._closed:
                    raise LimitExceeded("stopped", retry_after=1.0)
                now = self._clock()
                self._trim_rpm_locked(now)
                if len(self._rpm_events) < self._rpm:
                    self._rpm_events.append(now)
                    return
                wait = max(0.05, _WINDOW_SECONDS - (now - self._rpm_events[0]) + 0.05)
            if waited + wait > limit:
                raise LimitExceeded("rpm", retry_after=wait)
            await asyncio.sleep(wait)
            waited += wait

    async def acquire(self, *, count_rpm=True) -> None:
        if self._closed:
            raise LimitExceeded("stopped", retry_after=1.0)
        if count_rpm:
            await self._wait_rpm(self._queue_timeout)
        async with self._lock:
            if self._closed:
                raise LimitExceeded("stopped", retry_after=1.0)
            if self._active < self._concurrency:
                self._active += 1
                return
            if len(self._waiters) >= self._queue_size:
                raise LimitExceeded("queue_full", retry_after=1.0)
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)

        timeout = self._queue_timeout
        try:
            if timeout is not None:
                await asyncio.wait_for(fut, timeout)
            else:
                await fut
        except asyncio.CancelledError:
            await self._discard_waiter(fut)
            raise
        except (asyncio.TimeoutError, TimeoutError):
            await self._discard_waiter(fut)
            raise LimitExceeded("queue_timeout", retry_after=1.0) from None

    async def _discard_waiter(self, fut) -> None:
        async with self._lock:
            try:
                self._waiters.remove(fut)
            except ValueError:
                if fut.done() and not fut.cancelled():
                    self._handoff_locked()

    def _handoff_locked(self) -> None:
        while self._waiters:
            nxt = self._waiters.popleft()
            if not nxt.done():
                nxt.set_result(None)
                return
        self._active = max(0, self._active - 1)

    async def release(self) -> None:
        async with self._lock:
            self._handoff_locked()

    async def consume_rpm(self) -> None:
        """记一次请求预算；预算用完时排队等待，而不是直接失败。"""
        await self._wait_rpm(self._queue_timeout)

    @asynccontextmanager
    async def slot(self, *, count_rpm=True):
        await self.acquire(count_rpm=count_rpm)
        try:
            yield
        finally:
            await self.release()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            waiters = list(self._waiters)
            self._waiters.clear()
        for fut in waiters:
            if not fut.done():
                fut.set_exception(LimitExceeded("stopped", retry_after=1.0))
