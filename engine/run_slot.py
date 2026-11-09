"""Exclusive run slot shared by interactive and server-owned batch runs."""

import asyncio
import time
from collections.abc import Callable


class SlotGate:
    def __init__(self, lingering: Callable[[], bool], capacity: int = 1) -> None:
        self.lingering = lingering
        self.capacity = capacity
        self.holders: set[str] = set()

    @property
    def holder(self) -> str | None:
        return next(iter(self.holders), None)

    def try_claim(self, run_id: str) -> bool:
        if run_id in self.holders or len(self.holders) >= self.capacity or self.lingering():
            return False
        self.holders.add(run_id)
        return True

    def release(self, run_id: str) -> None:
        self.holders.discard(run_id)

    async def wait_claim(self, run_id: str, cancelled: Callable[[], bool], timeout_s: float = 2700,
                         on_wait: Callable[[], None] | None = None) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if cancelled():
                raise asyncio.CancelledError()
            if time.monotonic() >= deadline:
                return False
            if self.try_claim(run_id):
                return True
            if on_wait:
                on_wait()
            await asyncio.sleep(1)
