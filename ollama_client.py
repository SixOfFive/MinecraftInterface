"""Minimal async Ollama client (standard library only).

Uses the native /api/chat endpoint with a JSON-Schema `format` so the local
model's output is grammar-constrained to a valid action object. Blocking urllib
calls run in a daemon thread so (a) they don't stall the event loop and (b) an
in-flight request can't block interpreter shutdown (daemon threads are abandoned
at exit, unlike the default thread-pool executor asyncio.run() joins).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Optional


# Code-side fallbacks, used only when nothing is set. The single place YOU edit to
# change the model is OLLAMA_MODEL in the project-root .env file (see .env.example).
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"


class OllamaError(Exception):
    pass


async def _in_daemon_thread(func: Callable[[], Any]) -> Any:
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def worker() -> None:
        try:
            result = func()
        except Exception as e:  # noqa: BLE001 - marshal any error back to the awaiter
            loop.call_soon_threadsafe(fut.set_exception, e)
        else:
            loop.call_soon_threadsafe(fut.set_result, result)

    threading.Thread(target=worker, daemon=True, name="ollama-http").start()
    return await fut


class OllamaClient:
    def __init__(
        self,
        url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.3,
        keep_alive: str = "30m",
        num_predict: int = 512,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.keep_alive = keep_alive
        self.num_predict = num_predict

    async def chat(
        self,
        messages: list[dict],
        schema: Optional[dict] = None,
        timeout: float = 180.0,  # generous: the first call may cold-load the model
    ) -> dict:
        """Return the assistant message content parsed as a dict.

        With a schema the content is grammar-constrained JSON; we still guard the
        parse in case of truncation.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_predict": self.num_predict},
        }
        if schema is not None:
            body["format"] = schema
        data = await _in_daemon_thread(lambda: self._post("/api/chat", body, timeout))
        content = (data.get("message") or {}).get("content", "")
        return _parse_json_object(content)

    async def warmup(self, timeout: float = 180.0) -> bool:
        """Load the model into VRAM so the first real decision is fast. Non-fatal."""
        body = {
            "model": self.model, "stream": False, "keep_alive": self.keep_alive,
            "messages": [{"role": "user", "content": "ok"}],
            "options": {"num_predict": 1},
        }
        try:
            await _in_daemon_thread(lambda: self._post("/api/chat", body, timeout))
            return True
        except OllamaError:
            return False

    async def list_models(self, timeout: float = 10.0) -> list[str]:
        data = await _in_daemon_thread(lambda: self._get("/api/tags", timeout))
        return [m.get("name", "") for m in data.get("models", [])]

    async def version(self, timeout: float = 6.0) -> str:
        try:
            data = await _in_daemon_thread(lambda: self._get("/api/version", timeout))
            return data.get("version", "")
        except OllamaError:
            return ""

    # -- transport --------------------------------------------------------
    def _post(self, path: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise OllamaError(f"Ollama {path} HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OllamaError(f"Ollama {path} failed at {self.url}: {e}") from None

    def _get(self, path: str, timeout: float) -> dict:
        req = urllib.request.Request(self.url + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OllamaError(f"Ollama {path} failed at {self.url}: {e}") from None


def _parse_json_object(text: str) -> dict:
    """Best-effort parse of a JSON object from model output.

    Grammar-constrained output is already clean JSON, but we strip stray fences
    and fall back to slicing the outermost braces if the model wandered.
    """
    text = (text or "").strip()
    # Reasoning models (e.g. qwen3) may prefix a <think>...</think> trace — drop it.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise OllamaError(f"model did not return valid JSON: {text[:200]!r}")
