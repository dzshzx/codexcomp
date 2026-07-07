"""codexcomp transport layer.

Downstream (Codex, wired via top-level `openai_base_url`):
  * WebSocket /v1/responses  — Codex's preferred transport (openai-beta
    responses_websockets): client sends {"type":"response.create", ...body...}
    frames, we answer with response.* event frames; the connection is reused
    for sequential requests (prewarm + turns).
  * POST /v1/responses       — SSE fallback; request body may be zstd/gzip
    compressed (built-in provider sends zstd when request compression is on).
  * anything else under /v1/ — transparent passthrough to the upstream base
    (Codex refreshes its model catalog via GET /v1/models).

Upstream uses the matching transport: downstream WebSocket turns go to the
Codex Responses WebSocket endpoint, while downstream POST uses SSE POST. The
fold state machine (fold.py) is transport-agnostic.
"""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from dataclasses import dataclass
import gzip
import json
import logging
import os
import re
import time
import zlib
from typing import Any, AsyncIterator

import httpx
import websockets
from websockets import ConnectionClosed
import zstandard
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .fold import DONE, TERMINAL_TYPES, RoundOpenError, fold

log = logging.getLogger("codexcomp.server")

UPSTREAM_BASE = os.environ.get(
    "CODEXCOMP_UPSTREAM_BASE", "https://chatgpt.com/backend-api/codex"
).rstrip("/")
RESPONSES_URL = UPSTREAM_BASE + "/responses"
RESPONSES_WS_URL = (
    UPSTREAM_BASE.replace("https://", "wss://", 1)
    if UPSTREAM_BASE.startswith("https://")
    else UPSTREAM_BASE.replace("http://", "ws://", 1)
) + "/responses"
RESPONSES_WS_BETA = "responses_websockets=2026-02-06"
RESPONSES_WS_READ_TIMEOUT = float(os.environ.get("CODEXCOMP_WS_READ_TIMEOUT", "600"))
RESPONSES_WS_CANCEL_DRAIN_TIMEOUT = float(
    os.environ.get("CODEXCOMP_WS_CANCEL_DRAIN_TIMEOUT", "10")
)
RESPONSES_WS_POOL_TTL = float(os.environ.get("CODEXCOMP_WS_POOL_TTL", "3600"))
RESPONSES_WS_POOL_MAX = int(os.environ.get("CODEXCOMP_WS_POOL_MAX", "128"))

# hop-by-hop / transport-specific headers never forwarded upstream
_DROP_HEADERS = {
    "host", "connection", "upgrade", "keep-alive", "te", "trailer",
    "transfer-encoding", "proxy-authorization", "proxy-connection",
    "content-length", "content-encoding", "accept-encoding",
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol",
    "openai-beta",  # downstream transport advertisement; upstream sets its own
}
X_CODEX_TURN_STATE = "x-codex-turn-state"
_ID_RE = re.compile(r"\b(resp|rs|msg|fc|call)_[A-Za-z0-9_-]{8,}\b")


def redact_ids(text: object) -> str:
    """Redact full upstream ids before writing to the journal."""
    return _ID_RE.sub(lambda m: f"{m.group(1)}_{m.group(0).split('_', 1)[1][:8]}…", str(text))


def passthrough_headers(raw: Any) -> dict[str, str]:
    out = {}
    for key, value in raw:
        k = key.decode() if isinstance(key, bytes) else key
        if k.lower() in _DROP_HEADERS:
            continue
        out[k] = value.decode() if isinstance(value, bytes) else value
    return out


def upstream_headers(raw: Any) -> dict[str, str]:
    out = passthrough_headers(raw)
    out["accept"] = "text/event-stream"
    return out


def upstream_ws_headers(raw: Any) -> dict[str, str]:
    out = passthrough_headers(raw)
    out["OpenAI-Beta"] = RESPONSES_WS_BETA
    return out


def decompress_body(data: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").lower().strip()
    if not enc or enc == "identity":
        return data
    if enc == "zstd":
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    if enc == "gzip":
        return gzip.decompress(data)
    if enc == "deflate":
        return zlib.decompress(data)
    raise ValueError(f"unsupported content-encoding: {enc}")


# --- upstream SSE rounds ------------------------------------------------------


def request_shape(headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    prev = body.get("previous_response_id")
    input_items = body.get("input") or []
    client_metadata = body.get("client_metadata")
    if not isinstance(client_metadata, dict):
        client_metadata = {}
    return {
        "model": body.get("model"),
        "previous_response_id": "yes" if prev else "no",
        "previous_response_id_prefix": str(prev)[:12] if prev else None,
        "input_items": len(input_items),
        "input_types": [
            item.get("type") if isinstance(item, dict) else type(item).__name__
            for item in input_items[:8]
        ],
        "keys": sorted(body.keys()),
        "has_tools": bool(body.get("tools")),
        "tool_choice": body.get("tool_choice"),
        "parallel_tool_calls": body.get("parallel_tool_calls"),
        "store": body.get("store"),
        "include": body.get("include"),
        "client_metadata_has_turn_state": X_CODEX_TURN_STATE in client_metadata,
        "header_presence": {
            k: k in {h.lower() for h in headers}
            for k in ("authorization", "cookie", "session-id", "thread-id",
                      "x-client-request-id", X_CODEX_TURN_STATE)
        },
    }


def parse_sse(text_chunks: AsyncIterator[str]) -> AsyncIterator[dict | object]:
    """Incremental SSE parser: yields event dicts (from data: lines) and the
    DONE sentinel for `data: [DONE]`. Handles LF/CRLF and flushes a final event
    even when the stream lacks a trailing blank line."""

    async def gen():
        buf = ""

        async def emit_block(block: str):
            data_lines = [
                line[5:].lstrip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                return None
            data = "\n".join(data_lines)
            if data == "[DONE]":
                return DONE
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                log.warning("unparseable SSE data (len=%d), dropped", len(data))
                return None

        async for chunk in text_chunks:
            buf += chunk.replace("\r\n", "\n").replace("\r", "\n")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                ev = await emit_block(block)
                if ev is not None:
                    yield ev
        if buf.strip():
            ev = await emit_block(buf)
            if ev is not None:
                yield ev

    return gen()


class UpstreamRounds:
    """RoundOpener bound to one downstream request's headers; closes the
    previous round's response before opening the next."""

    def __init__(self, client: httpx.AsyncClient, headers: dict[str, str]):
        self.client = client
        self.headers = headers
        self._resp: httpx.Response | None = None

    def _body_shape(self, body: dict[str, Any]) -> dict[str, Any]:
        return request_shape(self.headers, body)

    @staticmethod
    def _error_summary(detail: str) -> dict[str, Any]:
        try:
            data = json.loads(detail)
        except json.JSONDecodeError:
            return {"raw": redact_ids(detail)[:500]}
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return {
                "type": err.get("type") or err.get("error_type"),
                "code": err.get("code"),
                "message": redact_ids(err.get("message", ""))[:500],
            }
        if isinstance(data, dict):
            summary = {
                "type": data.get("type") or data.get("error_type"),
                "code": data.get("code"),
                "message": redact_ids(data.get("message", ""))[:500],
                "keys": sorted(data.keys()),
            }
            if not any(summary.get(k) for k in ("type", "code", "message")):
                summary["raw"] = redact_ids(detail)[:500]
            return summary
        return {"raw": redact_ids(detail)[:500]}

    async def open(self, body: dict[str, Any]) -> AsyncIterator[dict | object]:
        await self.aclose()
        if body.get("previous_response_id"):
            # The ChatGPT Codex backend POST endpoint rejects previous_response_id
            # even though Codex's downstream WebSocket transport may send it for
            # tiny incremental turns. Fail fast so Codex retries with explicit
            # context instead of silently dropping context or wasting an upstream
            # 400 round trip.
            log.info(
                "reject previous_response_id request for upstream POST: request_shape=%s",
                json.dumps(self._body_shape(body), ensure_ascii=False),
            )
            raise RoundOpenError(
                400, json.dumps({"detail": "Unsupported parameter: previous_response_id"})
            )
        req = self.client.build_request(
            "POST", RESPONSES_URL,
            content=json.dumps(body, ensure_ascii=False).encode(),
            headers={**self.headers, "content-type": "application/json"},
            timeout=httpx.Timeout(connect=30, read=600, write=60, pool=30),
        )
        resp = await self.client.send(req, stream=True)
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode(errors="replace")
            log.warning(
                "upstream round open failed: status=%s request_shape=%s error=%s",
                resp.status_code,
                json.dumps(self._body_shape(body), ensure_ascii=False),
                json.dumps(self._error_summary(detail), ensure_ascii=False),
            )
            await resp.aclose()
            raise RoundOpenError(resp.status_code, detail)
        self._resp = resp
        return parse_sse(resp.aiter_text())

    async def aclose(self) -> None:
        if self._resp is not None:
            try:
                await self._resp.aclose()
            except Exception:
                pass
            self._resp = None


class UpstreamWsRounds:
    """RoundOpener that speaks the Codex upstream Responses WebSocket protocol."""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers
        self._ws: Any | None = None
        self._lock = asyncio.Lock()
        self._owner_task: asyncio.Task[Any] | None = None
        self.last_used = time.monotonic()

    @staticmethod
    def _ws_status(ws: Any) -> Any:
        response = getattr(ws, "response", None)
        return (
            getattr(response, "status_code", None)
            or getattr(response, "status", None)
            or "unknown"
        )

    @staticmethod
    def _ws_error_summary(ev: dict[str, Any]) -> dict[str, Any]:
        err = ev.get("error")
        if isinstance(err, dict):
            return {
                "status": ev.get("status"),
                "type": err.get("type") or err.get("error_type"),
                "code": err.get("code"),
                "message": redact_ids(err.get("message", ""))[:500],
            }
        return {
            "status": ev.get("status"),
            "type": ev.get("type"),
            "code": ev.get("code"),
            "message": redact_ids(ev.get("message", ""))[:500],
            "keys": sorted(ev.keys()),
        }

    @staticmethod
    def _response_headers(ws: Any) -> Any:
        response = getattr(ws, "response", None)
        return getattr(response, "headers", None)

    @staticmethod
    def _header_present(headers: Any, name: str) -> bool:
        if headers is None:
            return False
        try:
            return headers.get(name) is not None
        except Exception:
            return False

    @staticmethod
    def _is_closed(ws: Any) -> bool:
        if getattr(ws, "closed", False):
            return True
        if getattr(ws, "close_code", None) is not None:
            return True
        state = getattr(ws, "state", None)
        state_name = getattr(state, "name", "")
        return str(state_name).upper() in {"CLOSING", "CLOSED"}

    def is_busy(self) -> bool:
        return self._lock.locked()

    def is_closed(self) -> bool:
        return self._ws is None or self._is_closed(self._ws)

    async def _connect(self) -> Any:
        if self._ws is None or self._is_closed(self._ws):
            log.info("connecting upstream websocket: %s", RESPONSES_WS_URL)
            self._ws = await websockets.connect(
                RESPONSES_WS_URL,
                additional_headers=self.headers,
                user_agent_header=None,
                open_timeout=30,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_size=None,
            )
            response_headers = self._response_headers(self._ws)
            log.info(
                "upstream websocket connected: status=%s turn_state_header=%s",
                self._ws_status(self._ws),
                "yes" if self._header_present(response_headers, X_CODEX_TURN_STATE) else "no",
            )
        self.last_used = time.monotonic()
        return self._ws

    async def open(self, body: dict[str, Any]) -> AsyncIterator[dict | object]:
        await self._lock.acquire()
        self._owner_task = asyncio.current_task()
        try:
            ws = await self._connect()
        except BaseException:
            self._owner_task = None
            self._lock.release()
            raise
        payload = dict(body)
        payload["type"] = "response.create"
        shape = request_shape(self.headers, body)
        log.info(
            "open upstream websocket round: request_shape=%s",
            json.dumps(shape, ensure_ascii=False),
        )
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except BaseException:
            self._owner_task = None
            self._lock.release()
            raise

        async def gen():
            first = True
            emitted = False
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=RESPONSES_WS_READ_TIMEOUT
                        )
                    except asyncio.TimeoutError as exc:
                        await self.aclose()
                        log.warning(
                            "upstream websocket timed out before terminal: request_shape=%s",
                            json.dumps(shape, ensure_ascii=False),
                        )
                        raise TimeoutError("upstream websocket read timeout") from exc
                    except ConnectionClosed as exc:
                        self._ws = None
                        log.info(
                            "upstream websocket closed before terminal: code=%s reason=%s request_shape=%s",
                            getattr(exc, "code", None),
                            str(getattr(exc, "reason", ""))[:200],
                            json.dumps(shape, ensure_ascii=False),
                        )
                        raise ConnectionError(
                            f"upstream websocket closed: {getattr(exc, 'code', None)}"
                        ) from exc
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("unparseable upstream WS event (len=%d), dropped", len(raw))
                        continue
                    etype = ev.get("type")
                    if first:
                        log.info(
                            "upstream websocket first event: type=%s request_shape=%s",
                            etype,
                            json.dumps(shape, ensure_ascii=False),
                        )
                        first = False
                    if etype == "response.metadata":
                        headers = ev.get("headers")
                        if isinstance(headers, dict) and X_CODEX_TURN_STATE in headers:
                            log.info("upstream websocket response.metadata turn_state=yes")
                    if ev.get("type") == "error":
                        log.warning(
                            "upstream websocket error frame: request_shape=%s error=%s",
                            json.dumps(shape, ensure_ascii=False),
                            json.dumps(self._ws_error_summary(ev), ensure_ascii=False),
                        )
                        if not emitted:
                            await self.aclose()
                            raise RoundOpenError(
                                int(ev.get("status") or 502),
                                redact_ids(json.dumps(ev, ensure_ascii=False)),
                            )
                        raise ConnectionError(redact_ids(json.dumps(ev, ensure_ascii=False)))
                    if etype in TERMINAL_TYPES:
                        log.info(
                            "upstream websocket terminal: type=%s status=%s request_shape=%s",
                            etype,
                            (ev.get("response") or {}).get("status"),
                            json.dumps(shape, ensure_ascii=False),
                        )
                        emitted = True
                        self.last_used = time.monotonic()
                        yield ev
                        return
                    emitted = True
                    yield ev
            finally:
                if self._owner_task is asyncio.current_task():
                    self._owner_task = None
                self.last_used = time.monotonic()
                self._lock.release()

        return gen()

    async def cancel_for_task(self, task: asyncio.Task[Any]) -> bool:
        if self._owner_task is not task or self._ws is None or self._is_closed(self._ws):
            return False
        try:
            await self._ws.send(json.dumps({"type": "response.cancel"}))
            return True
        except Exception:
            await self.aclose()
            return False

    async def aclose(self) -> None:
        if self._ws is not None:
            ws = self._ws
            self._ws = None
            self._owner_task = None
            try:
                await ws.close()
            except Exception:
                pass
        self.last_used = time.monotonic()


def _pool_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def upstream_ws_pool_key(headers: dict[str, str]) -> tuple[Any, ...] | None:
    lower = {k.lower(): v for k, v in headers.items()}
    auth_scope = _pool_hash(lower.get("authorization"))
    cookie_scope = _pool_hash(lower.get("cookie"))
    session_id = lower.get("session-id")
    thread_id = lower.get("thread-id")
    request_id = lower.get("x-client-request-id")
    if session_id or thread_id:
        return (auth_scope, cookie_scope, session_id, thread_id)
    if request_id:
        return (auth_scope, cookie_scope, None, None, request_id)
    return None


class UpstreamWsPool:
    """Keeps upstream Responses WebSockets sticky for a Codex session/thread."""

    def __init__(self) -> None:
        self._items: dict[tuple[Any, ...], UpstreamWsRounds] = {}
        self._lock = asyncio.Lock()

    async def _close_many(self, rounds: list[UpstreamWsRounds]) -> None:
        for item in rounds:
            await item.aclose()

    def _collect_stale_locked(self) -> list[UpstreamWsRounds]:
        now = time.monotonic()
        stale_keys = [
            key for key, rounds in self._items.items()
            if (
                not getattr(rounds, "is_busy", lambda: False)()
                and (
                    getattr(rounds, "is_closed", lambda: False)()
                    or now - getattr(rounds, "last_used", now) > RESPONSES_WS_POOL_TTL
                )
            )
        ]
        stale: list[UpstreamWsRounds] = []
        for key in stale_keys:
            stale.append(self._items.pop(key))

        if len(self._items) >= RESPONSES_WS_POOL_MAX:
            candidates = sorted(
                (
                    (getattr(rounds, "last_used", now), key)
                    for key, rounds in self._items.items()
                    if not getattr(rounds, "is_busy", lambda: False)()
                )
            )
            while len(self._items) >= RESPONSES_WS_POOL_MAX and candidates:
                _, key = candidates.pop(0)
                stale.append(self._items.pop(key))
        return stale

    async def get(self, headers: dict[str, str]) -> tuple[UpstreamWsRounds, bool]:
        key = upstream_ws_pool_key(headers)
        if key is None:
            return UpstreamWsRounds(headers), False
        stale: list[UpstreamWsRounds]
        async with self._lock:
            stale = self._collect_stale_locked()
            rounds = self._items.get(key)
            if rounds is None:
                rounds = UpstreamWsRounds(headers)
                self._items[key] = rounds
            rounds.last_used = time.monotonic()
        await self._close_many(stale)
        return rounds, True


# --- downstream endpoints -----------------------------------------------------


def sse_bytes(ev: dict | object) -> bytes:
    if ev is DONE:
        return b"data: [DONE]\n\n"
    etype = ev.get("type", "message")  # type: ignore[union-attr]
    return f"event: {etype}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()


@dataclass
class ActiveTurn:
    task: asyncio.Task[None]
    rounds: Any
    cancel_event: asyncio.Event
    pooled: bool


async def responses_post(request: Request) -> Response:
    raw = await request.body()
    try:
        raw = decompress_body(raw, request.headers.get("content-encoding"))
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": f"bad request body: {exc}"}, status_code=400)

    log.info(
        "post response.create: model=%s previous_response_id=%s input_items=%d",
        body.get("model"),
        "yes" if body.get("previous_response_id") else "no",
        len(body.get("input") or []),
    )
    rounds = UpstreamRounds(request.app.state.client, upstream_headers(request.headers.raw))

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for ev in fold(body, rounds.open):
                yield sse_bytes(ev)
        except RoundOpenError as exc:  # round 1 rejected: surface upstream error
            yield sse_bytes({
                "type": "response.failed",
                "response": {"status": "failed",
                             "error": {"message": str(exc), "code": exc.status}},
            })
        finally:
            await rounds.aclose()

    return StreamingResponse(stream(), media_type="text/event-stream")


async def responses_ws(ws: WebSocket) -> None:
    await ws.accept()
    headers = upstream_ws_headers(ws.headers.raw)
    pool = getattr(ws.app.state, "upstream_ws_pool", None)
    if pool is not None:
        shared_rounds, pooled_rounds = await pool.get(headers)
    else:
        shared_rounds, pooled_rounds = UpstreamWsRounds(headers), False
    send_q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=100)
    active: ActiveTurn | None = None

    async def writer() -> None:
        while True:
            msg = await send_q.get()
            if msg is None:
                return
            await ws.send_text(msg)

    async def send_event(ev: dict[str, Any]) -> None:
        await send_q.put(json.dumps(ev, ensure_ascii=False))

    async def run_turn(
        body: dict[str, Any],
        rounds: Any,
        cancel_event: asyncio.Event,
    ) -> None:
        try:
            async for ev in fold(body, rounds.open):
                if ev is DONE:
                    continue
                if cancel_event.is_set():
                    continue
                await send_q.put(json.dumps(ev, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except RoundOpenError as exc:
            if not cancel_event.is_set():
                await send_event({
                    "type": "response.failed",
                    "response": {"status": "failed",
                                 "error": {"message": str(exc), "code": exc.status}},
                })
            await rounds.aclose()
        except Exception as exc:
            log.exception("ws turn failed")
            if not cancel_event.is_set():
                await send_event({
                    "type": "response.failed",
                    "response": {"status": "failed",
                                 "error": {"message": str(exc), "code": "proxy_error"}},
                })
            await rounds.aclose()

    def clear_active_when_done(task: asyncio.Task[None], turn: ActiveTurn) -> None:
        nonlocal active
        if active is turn:
            active = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("ws turn task crashed")

    async def cancel_active(reason: str) -> None:
        nonlocal active
        turn = active
        if turn is None:
            return
        log.info("ws: cancelling active response (%s)", reason)
        turn.cancel_event.set()
        if turn.pooled:
            cancel_sent = False
            cancel_for_task = getattr(turn.rounds, "cancel_for_task", None)
            if cancel_for_task is not None:
                cancel_sent = await cancel_for_task(turn.task)
            if cancel_sent:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(turn.task),
                        timeout=RESPONSES_WS_CANCEL_DRAIN_TIMEOUT,
                    )
                except TimeoutError:
                    log.warning("ws: pooled upstream cancel did not drain before timeout")
                    await turn.rounds.aclose()
                    turn.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await turn.task
            else:
                turn.task.cancel()
                with suppress(asyncio.CancelledError):
                    await turn.task
        else:
            await turn.rounds.aclose()
            turn.task.cancel()
            with suppress(asyncio.CancelledError):
                await turn.task
        if active is turn:
            active = None

    writer_task = asyncio.create_task(writer())

    async def receive_text() -> str:
        recv_task = asyncio.create_task(ws.receive_text())
        done, _ = await asyncio.wait(
            {recv_task, writer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if writer_task in done:
            recv_task.cancel()
            with suppress(asyncio.CancelledError):
                await recv_task
            try:
                writer_task.result()
            except Exception:
                log.exception("ws writer failed")
            raise WebSocketDisconnect()
        return recv_task.result()

    try:
        while True:
            try:
                envelope = json.loads(await receive_text())
            except (WebSocketDisconnect, json.JSONDecodeError):
                return

            frame_type = envelope.get("type")
            if frame_type == "response.cancel":
                await cancel_active("client response.cancel")
                continue

            if frame_type != "response.create":
                log.info("ws: ignoring frame type %s", frame_type)
                continue

            if active is not None and active.task.done():
                with suppress(Exception):
                    active.task.result()
                active = None
            if active is not None:
                log.warning("ws: rejecting response.create while a turn is active")
                await send_event({
                    "type": "response.failed",
                    "response": {"status": "failed",
                                 "error": {"message": "response already active",
                                           "code": "response_active"}},
                })
                continue

            body = {k: v for k, v in envelope.items() if k != "type"}
            log.info(
                "ws response.create: model=%s previous_response_id=%s input_items=%d",
                body.get("model"),
                "yes" if body.get("previous_response_id") else "no",
                len(body.get("input") or []),
            )
            cancel_event = asyncio.Event()
            task = asyncio.create_task(run_turn(body, shared_rounds, cancel_event))
            turn = ActiveTurn(
                task=task,
                rounds=shared_rounds,
                cancel_event=cancel_event,
                pooled=pooled_rounds,
            )
            active = turn
            task.add_done_callback(lambda t, turn=turn: clear_active_when_done(t, turn))
    except WebSocketDisconnect:
        pass
    finally:
        if active is not None and active.task.done():
            with suppress(Exception):
                active.task.result()
            active = None
        await cancel_active("websocket disconnect")
        if not pooled_rounds:
            await shared_rounds.aclose()
        writer_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await writer_task


async def passthrough(request: Request) -> Response:
    """Transparent proxy for every other /v1/* call (e.g. GET /v1/models)."""
    suffix = request.path_params["path"]
    url = f"{UPSTREAM_BASE}/{suffix}"
    if request.url.query:
        url += "?" + request.url.query
    content = await request.body()
    if content:
        content = decompress_body(content, request.headers.get("content-encoding"))
    headers = passthrough_headers(request.headers.raw)
    headers["accept-encoding"] = "identity"
    upstream = await request.app.state.client.request(
        request.method, url, content=content or None, headers=headers,
        timeout=httpx.Timeout(60),
    )
    drop = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    return Response(
        upstream.content, status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() not in drop},
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "upstream": UPSTREAM_BASE})


def build_app() -> Starlette:
    app = Starlette(routes=[
        Route("/healthz", health),
        Route("/v1/responses", responses_post, methods=["POST"]),
        WebSocketRoute("/v1/responses", responses_ws),
        Route("/v1/{path:path}", passthrough,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]),
    ])
    app.state.client = httpx.AsyncClient(trust_env=True, http2=False)
    app.state.upstream_ws_pool = UpstreamWsPool()
    return app


app = build_app()
