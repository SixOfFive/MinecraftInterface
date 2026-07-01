"""Async client for the Node Mineflayer bot's newline-delimited-JSON TCP bridge.

Pure standard library. Correlates request/response by an incrementing id and
delivers unsolicited events (chat, spawn, death, ...) to registered handlers.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Awaitable, Callable, Optional

EventHandler = Callable[[str, dict], Optional[Awaitable[None]]]


class BotError(Exception):
    """Raised when the bot reports a command failure or a command times out."""


class BotBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 25585) -> None:
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: list[EventHandler] = []
        self._read_task: Optional[asyncio.Task] = None
        self.ready = False  # True once the bot has emitted 'spawn'

    # -- connection -------------------------------------------------------
    async def connect(self, retries: int = 120, delay: float = 0.5) -> None:
        last: Optional[Exception] = None
        for _ in range(retries):
            try:
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
                self._read_task = asyncio.create_task(self._read_loop())
                return
            except OSError as e:  # bot not listening yet
                last = e
                await asyncio.sleep(delay)
        raise BotError(f"could not reach bot bridge at {self.host}:{self.port}: {last}")

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    # -- io ---------------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # bridge closed (EOF)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except OSError:
            # Abrupt socket drop (e.g. bot.js died) — on Windows this raises
            # ConnectionResetError rather than returning EOF. Fall through to the
            # finally so callers fail fast instead of hanging on timeouts.
            pass
        finally:
            self.ready = False
            self._writer = None
            self._reader = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BotError("bridge connection closed"))
            self._pending.clear()

    async def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid is not None:
            fut = self._pending.pop(mid, None)
            if fut is not None:
                if not fut.done():
                    if msg.get("ok"):
                        fut.set_result(msg.get("result"))
                    else:
                        fut.set_exception(BotError(msg.get("error", "unknown error")))
                return
            # id we don't recognize (already timed out) — drop it.
            return
        event = msg.get("event")
        if event:
            data = msg.get("data") or {}
            if event == "spawn":
                self.ready = True
            elif event in ("end", "kicked"):
                self.ready = False
            elif event == "bridge_connected":
                # We may have connected AFTER the bot already spawned (e.g. an
                # external/reconnected bot); trust its reported readiness.
                self.ready = bool(data.get("ready"))
            for handler in self._handlers:
                try:
                    res = handler(event, data)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # a bad handler must not kill the read loop
                    pass

    async def send(self, cmd: str, timeout: float = 60.0, **args: Any) -> Any:
        if self._writer is None:
            raise BotError("bridge not connected")
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            payload = json.dumps({"id": mid, "cmd": cmd, "args": args}) + "\n"
            self._writer.write(payload.encode("utf-8"))
            await self._writer.drain()
        except Exception as e:  # write/drain failed — don't leak the pending future
            self._pending.pop(mid, None)
            raise BotError(f"failed to send '{cmd}': {e}") from None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise BotError(f"command '{cmd}' timed out after {timeout:g}s") from None
        finally:
            self._pending.pop(mid, None)

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task  # let the finally run (fails pending futures)
            except (asyncio.CancelledError, Exception):
                pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
