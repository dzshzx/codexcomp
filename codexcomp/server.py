"""codexcomp transport layer.

Downstream (Codex, wired via top-level `openai_base_url`):
  * WebSocket /v1/responses  — Codex's preferred transport (openai-beta
    responses_websockets): client sends {"type":"response.create", ...body...}
    frames, we answer with response.* event frames; the connection is reused
    for sequential requests (prewarm + turns). The protocol is STATEFUL:
    Codex sends `generate:false` prewarm frames (connection setup, must not
    generate) and compresses follow-up requests to `previous_response_id` +
    incremental input. WsSession implements that contract locally so the
    upstream request is always stateless full input.
  * POST /v1/responses       — SSE fallback; request body may be zstd/gzip
    compressed (built-in provider sends zstd when request compression is on).
  * anything else under /v1/ — transparent passthrough to the upstream base
    (Codex refreshes its model catalog via GET /v1/models).

Upstream is always the SSE POST endpoint; the fold state machine (fold.py) is
transport-agnostic.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import gzip
from itertools import count
import json
import logging
import os
import time
import zlib
from typing import Any, AsyncIterator

import httpx
import zstandard
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import DEFAULT_UPSTREAM
from .fold import DONE, RoundOpenError, fold

log = logging.getLogger("codexcomp.server")

_REQUEST_IDS = count(1)
_POOL_MAX_CONNECTIONS = 100


def _new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=True,
        http2=False,
        limits=httpx.Limits(
            max_connections=_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=20,
            keepalive_expiry=5,
        ),
    )


def _proxy_summary() -> str:
    """Describe proxy routing without ever logging credentials."""
    from urllib.parse import urlsplit

    configured = []
    for key in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                "all_proxy", "https_proxy", "http_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        parsed = urlsplit(raw)
        configured.append(
            f"{key}={parsed.scheme}://{parsed.hostname}:{parsed.port}")
    return ",".join(configured) or "direct"


def _pool_snapshot(client: httpx.AsyncClient) -> str:
    """Best-effort httpcore snapshot used only for failure diagnostics."""
    transports = [getattr(client, "_transport", None)]
    transports.extend(getattr(client, "_mounts", {}).values())
    seen: set[int] = set()
    pools = []
    for transport in transports:
        if transport is None or id(transport) in seen:
            continue
        seen.add(id(transport))
        pool = getattr(transport, "_pool", None)
        connections = list(getattr(pool, "connections", ()) or ())
        if pool is None:
            continue
        try:
            idle = sum(conn.is_idle() for conn in connections)
            available = sum(conn.is_available() for conn in connections)
        except (AttributeError, TypeError):
            pools.append(f"total={len(connections)}")
            continue
        active = len(connections) - idle
        pools.append(
            f"total={len(connections)} active={active} idle={idle} "
            f"available={available}")
    return ";".join(pools) or "unavailable"


async def _recover_exhausted_pool(state: Any, failed_client: httpx.AsyncClient,
                                  context: str) -> None:
    """Rotate a wedged pool once; later retries use a clean client immediately."""
    async with state.client_reset_lock:
        if state.client is not failed_client:
            return
        old_snapshot = _pool_snapshot(failed_client)
        state.client = state.client_factory()
        state.client_generation += 1
        log.error(
            "upstream pool exhausted; rotated client generation=%d context=%s "
            "active_requests=%d old_pool=[%s] proxy=%s",
            state.client_generation, context, state.upstream_active,
            old_snapshot, _proxy_summary(),
        )
        # PoolTimeout means an interactive request already waited 30 seconds for
        # a slot. Abort the wedged generation so its sockets cannot remain stuck.
        await failed_client.aclose()

# hop-by-hop / transport-specific headers never forwarded upstream
_DROP_HEADERS = {
    "host", "connection", "upgrade", "keep-alive", "te", "trailer",
    "transfer-encoding", "proxy-authorization", "proxy-connection",
    "content-length", "content-encoding", "accept-encoding",
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol",
    "openai-beta",  # advertises the ws protocol; upstream round is plain SSE
}


def upstream_headers(raw: Any) -> dict[str, str]:
    out = {}
    for key, value in raw:
        k = key.decode() if isinstance(key, bytes) else key
        if k.lower() in _DROP_HEADERS:
            continue
        out[k] = value.decode() if isinstance(value, bytes) else value
    return out


def _http_error_detail(exc: httpx.HTTPError) -> str:
    return str(exc) or repr(exc.__cause__) or repr(exc)


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


def parse_sse(text_chunks: AsyncIterator[str]) -> AsyncIterator[dict | object]:
    """Incremental SSE parser: yields event dicts (from data: lines) and the
    DONE sentinel for `data: [DONE]`."""

    async def gen():
        buf = ""
        async for chunk in text_chunks:
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                data_lines = [
                    line[5:].lstrip()
                    for line in block.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                data = "\n".join(data_lines)
                if data == "[DONE]":
                    yield DONE
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    log.warning("unparseable SSE data (len=%d), dropped", len(data))

    return gen()


class UpstreamRounds:
    """RoundOpener bound to one downstream request's headers; closes the
    previous round's response before opening the next.

    Round 1's response headers are kept for the downstream transport (Codex
    reads x-reasoning-included / x-codex-turn-state / rate-limit snapshots from
    them), and the x-codex-turn-state sticky-routing token is captured once and
    replayed on every continuation round — mirroring Codex's own per-turn
    OnceLock — unless the downstream request already pinned one."""

    def __init__(self, state: Any, client: httpx.AsyncClient, responses_url: str,
                 headers: dict[str, str]):
        self.state = state
        self.client = client
        self.responses_url = responses_url
        self.headers = headers
        self.first_response_headers: httpx.Headers | None = None
        self._turn_state: str | None = None
        self._client_pinned_turn_state = any(
            k.lower() == "x-codex-turn-state" for k in headers)
        self._resp: httpx.Response | None = None
        self._active_request_id: int | None = None
        self._opened_at: float | None = None

    async def open(self, body: dict[str, Any]) -> AsyncIterator[dict | object]:
        await self.aclose()
        headers = {**self.headers, "content-type": "application/json",
                   "accept": "text/event-stream"}
        if self._turn_state is not None and not self._client_pinned_turn_state:
            headers["x-codex-turn-state"] = self._turn_state
        req = self.client.build_request(
            "POST", self.responses_url,
            content=json.dumps(body, ensure_ascii=False).encode(),
            headers=headers,
            timeout=httpx.Timeout(connect=30, read=600, write=60, pool=30),
        )
        request_id = next(_REQUEST_IDS)
        self._active_request_id = request_id
        self._opened_at = time.monotonic()
        self.state.upstream_active += 1
        log.debug("upstream request start id=%d kind=round active=%d generation=%d",
                  request_id, self.state.upstream_active,
                  self.state.client_generation)
        try:
            resp = await self.client.send(req, stream=True)
        except httpx.HTTPError as exc:
            # No retry: send() may fail after the request reached the upstream
            # (e.g. ReadTimeout), and a re-send would double-generate the turn.
            detail = _http_error_detail(exc)
            elapsed = time.monotonic() - self._opened_at
            self.state.upstream_active -= 1
            self._active_request_id = None
            self._opened_at = None
            log.warning(
                "round open upstream transport error id=%d elapsed=%.3fs "
                "active=%d generation=%d pool=[%s] proxy=%s: %s: %s",
                request_id, elapsed, self.state.upstream_active,
                self.state.client_generation, _pool_snapshot(self.client),
                _proxy_summary(), type(exc).__name__, detail,
            )
            if isinstance(exc, httpx.PoolTimeout):
                await _recover_exhausted_pool(self.state, self.client, "round")
            raise RoundOpenError(
                502,
                f"{type(exc).__name__}: {detail}",
                code="upstream_connection_error",
            ) from exc
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode(errors="replace")
            await resp.aclose()
            raise RoundOpenError(resp.status_code, detail)
        if self.first_response_headers is None:
            self.first_response_headers = resp.headers
        if self._turn_state is None:
            self._turn_state = resp.headers.get("x-codex-turn-state")
        log.info("round open: reasoning_included=%s turn_state=%s request_id=%s",
                 resp.headers.get("x-reasoning-included"),
                 "present" if resp.headers.get("x-codex-turn-state") else "absent",
                 resp.headers.get("x-request-id") or resp.headers.get("x-oai-request-id"))
        self._resp = resp
        return parse_sse(resp.aiter_text())

    async def aclose(self) -> None:
        if self._resp is not None:
            try:
                await self._resp.aclose()
            except Exception:
                pass
            self._resp = None
        if self._active_request_id is not None:
            elapsed = time.monotonic() - (self._opened_at or time.monotonic())
            self.state.upstream_active -= 1
            log.debug("upstream request end id=%d kind=round elapsed=%.3fs active=%d",
                      self._active_request_id, elapsed, self.state.upstream_active)
            self._active_request_id = None
            self._opened_at = None


# --- downstream websocket session state ----------------------------------------


class UnknownPreviousResponse(Exception):
    """An incremental frame referenced a response id this session never issued
    (proxy restarted, or the previous turn did not complete)."""

    def __init__(self, prev_id: Any):
        super().__init__(f"unknown previous_response_id: {prev_id!r}")


class WsSession:
    """The stateful half of Codex's responses_websockets contract, scoped to one
    downstream connection — exactly the scope Codex reuses response ids in.

    Codex compares `previous_request.input + items_added` against its next full
    input to build an incremental frame; both halves passed through us (the
    envelope input and the output_item.done items we streamed), so the full
    input can be reconstructed exactly. State is only valid after a completed
    response — any failed/incomplete/aborted request invalidates it, matching
    Codex, which records a reusable LastResponse only on Completed."""

    def __init__(self) -> None:
        self.last_id: str | None = None
        self.last_input: list[Any] = []
        self.last_output: list[Any] = []
        self._prewarms = 0

    def expand(self, body: dict[str, Any]) -> dict[str, Any]:
        """Resolve an envelope against session state: reconstruct full input
        from an incremental frame (empty delta included) and strip the ws-only
        `previous_response_id`. Raises UnknownPreviousResponse on mismatch."""
        body = dict(body)
        prev_id = body.pop("previous_response_id", None)
        if prev_id is not None:
            if self.last_id is None or prev_id != self.last_id:
                raise UnknownPreviousResponse(prev_id)
            delta = list(body.get("input") or [])
            body["input"] = [*self.last_input, *self.last_output, *delta]
            log.info("ws: rebuilt incremental frame: %d delta -> %d full input items",
                     len(delta), len(body["input"]))
        return body

    def prewarm_ack(self, body: dict[str, Any]) -> dict[str, Any]:
        """Consume a `generate:false` prewarm frame locally: remember its input
        as the conversation prefix and mint the completed frame Codex waits for.
        Never forwarded — the upstream SSE endpoint rejects `generate`."""
        self._prewarms += 1
        self.last_id = f"resp_codexcomp_prewarm_{self._prewarms}"
        self.last_input = list(body.get("input") or [])
        self.last_output = []
        log.info("ws: prewarm acked locally as %s (%d input items)",
                 self.last_id, len(self.last_input))
        return {
            "type": "response.completed",
            "sequence_number": 0,
            "response": {
                "id": self.last_id, "object": "response", "status": "completed",
                "output": [],
                "usage": {"input_tokens": 0,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens": 0,
                          "output_tokens_details": {"reasoning_tokens": 0},
                          "total_tokens": 0},
            },
        }

    def note_request(self, body: dict[str, Any]) -> None:
        """A generating request starts: remember its full input, invalidate the
        reusable id until a completed terminal arrives."""
        self.last_id = None
        self.last_input = list(body.get("input") or [])
        self.last_output = []

    def note_event(self, ev: dict[str, Any]) -> None:
        if ev.get("type") != "response.completed":
            return
        resp = ev.get("response") or {}
        self.last_id = resp.get("id") or None
        self.last_output = list(resp.get("output") or [])


def unknown_previous_response_frame(exc: UnknownPreviousResponse) -> dict[str, Any]:
    return {
        "type": "response.failed",
        "sequence_number": 0,
        "response": {"status": "failed",
                     "error": {"message": f"codexcomp: {exc}; reconnect and resend full input",
                               "code": "unknown_previous_response_id"}},
    }


# --- downstream endpoints -----------------------------------------------------


def make_rounds(state: Any, headers: dict[str, str]) -> UpstreamRounds:
    return UpstreamRounds(state, state.client, state.upstream_base + "/responses", headers)


async def drive_fold(rounds: UpstreamRounds,
                     body: dict[str, Any]) -> AsyncIterator[dict | object]:
    """One folded request: owns the UpstreamRounds lifecycle and yields
    downstream events. Transports only serialize what comes out of here (and
    may read rounds.first_response_headers once events start flowing)."""
    gen = fold(body, rounds.open)
    try:
        async for ev in gen:
            yield ev
    finally:
        # Close the fold generator FIRST and explicitly: `async for` does not
        # close its iterator on abnormal exit, and an orphaned fold would log
        # its abort (and release the upstream stream) only whenever the event
        # loop's async-gen finalizer / GC got around to it.
        try:
            await gen.aclose()
        finally:
            await rounds.aclose()


def sse_bytes(ev: dict | object) -> bytes:
    if ev is DONE:
        return b"data: [DONE]\n\n"
    etype = ev.get("type", "message")  # type: ignore[union-attr]
    return f"event: {etype}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()


# upstream response headers never mirrored downstream: hop-by-hop / envelope
# headers the downstream response owns itself. Everything else passes through —
# Codex reads x-reasoning-included (token-accounting mode), x-codex-turn-state
# (sticky routing), x-request-id, openai-model and the rate-limit snapshot
# headers from the POST response.
_DOWNSTREAM_DROP = {
    "content-type", "content-length", "content-encoding", "transfer-encoding",
    "connection", "date", "server",
}


def downstream_headers(upstream: httpx.Headers | None) -> dict[str, str]:
    if upstream is None:
        return {}
    return {k: v for k, v in upstream.items() if k.lower() not in _DOWNSTREAM_DROP}


async def responses_post(request: Request) -> Response:
    raw = await request.body()
    try:
        raw = decompress_body(raw, request.headers.get("content-encoding"))
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": f"bad request body: {exc}"}, status_code=400)

    rounds = make_rounds(request.app.state, upstream_headers(request.headers.raw))
    events = drive_fold(rounds, body)
    # Pull the first event before answering: it forces round 1 open, so the
    # upstream response headers exist and can be mirrored onto our response.
    try:
        first = await events.__anext__()
    except BaseException:
        await events.aclose()
        raise

    async def stream() -> AsyncIterator[bytes]:
        sent_done = False
        try:
            yield sse_bytes(first)
            async for ev in events:
                if ev is DONE:
                    sent_done = True
                yield sse_bytes(ev)
        finally:
            # Deterministic teardown on downstream disconnect: without this the
            # fold generator (and its upstream connection) lingers until the
            # event loop's async-gen finalizer runs.
            await events.aclose()
        if not sent_done:
            # fold() stops at the terminal event and normally never sees
            # upstream's trailing [DONE]; SSE clients that wait for it would
            # hang until connection close.
            yield b"data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers=downstream_headers(rounds.first_response_headers))


async def responses_ws(ws: WebSocket) -> None:
    # Codex reads connection-wide flags (x-reasoning-included token-accounting
    # mode, x-models-etag) from the 101 upgrade response, presence-based. The
    # real backend's handshake carries NEITHER for this endpoint (live-probed
    # 2026-07-07: only x-models-etag, no x-reasoning-included), so the accept
    # stays bare — adding flags the upstream doesn't declare would skew Codex's
    # context accounting relative to a direct connection.
    await ws.accept()
    ws_id = next(_REQUEST_IDS)
    ws.app.state.websocket_active += 1
    log.info("ws session open id=%d active=%d", ws_id,
             ws.app.state.websocket_active)
    headers = upstream_headers(ws.headers.raw)
    sess = WsSession()
    try:
        while True:
            try:
                envelope = json.loads(await ws.receive_text())
            except (WebSocketDisconnect, json.JSONDecodeError):
                return
            if envelope.get("type") != "response.create":
                log.info("ws: ignoring frame type %s", envelope.get("type"))
                continue
            body = {k: v for k, v in envelope.items() if k != "type"}
            try:
                body = sess.expand(body)
            except UnknownPreviousResponse as exc:
                # Fail loud and drop the connection: Codex reconnects and
                # resends full input — never silently answer without context.
                log.warning("ws: %s — closing so the client resends full input", exc)
                await ws.send_text(json.dumps(unknown_previous_response_frame(exc),
                                              ensure_ascii=False))
                await ws.close(code=1011)
                return
            if body.pop("generate", None) is False:  # prewarm: never generate
                await ws.send_text(json.dumps(sess.prewarm_ack(body), ensure_ascii=False))
                continue
            sess.note_request(body)
            events = drive_fold(make_rounds(ws.app.state, headers), body)
            try:
                async for ev in events:
                    if ev is DONE:
                        continue
                    sess.note_event(ev)
                    await ws.send_text(json.dumps(ev, ensure_ascii=False))
            finally:
                # a failed send_text must not leave the fold generator (and its
                # upstream connection) dangling until GC finalization
                await events.aclose()
    except WebSocketDisconnect as exc:
        log.info("ws session disconnected id=%d type=%s", ws_id,
                 type(exc).__name__)
    finally:
        ws.app.state.websocket_active -= 1
        log.info("ws session close id=%d active=%d", ws_id,
                 ws.app.state.websocket_active)


async def passthrough(request: Request) -> Response:
    """Transparent proxy for every other /v1/* call (e.g. GET /v1/models)."""
    suffix = request.path_params["path"]
    url = f"{request.app.state.upstream_base}/{suffix}"
    if request.url.query:
        url += "?" + request.url.query
    content = await request.body()
    if content:
        content = decompress_body(content, request.headers.get("content-encoding"))
    headers = upstream_headers(request.headers.raw)
    state = request.app.state
    client = state.client
    request_id = next(_REQUEST_IDS)
    started = time.monotonic()
    state.upstream_active += 1
    try:
        upstream = await client.request(
            request.method, url, content=content or None, headers=headers,
            timeout=httpx.Timeout(60),
        )
    except httpx.HTTPError as exc:
        detail = _http_error_detail(exc)
        log.warning(
            "passthrough upstream error id=%d elapsed=%.3fs active=%d "
            "generation=%d pool=[%s] proxy=%s for %s /v1/%s: %s: %s",
            request_id, time.monotonic() - started, state.upstream_active - 1,
            state.client_generation, _pool_snapshot(client), _proxy_summary(),
            request.method, suffix, type(exc).__name__, detail,
        )
        if isinstance(exc, httpx.PoolTimeout):
            await _recover_exhausted_pool(state, client, "passthrough")
        return JSONResponse(
            {"error": {"message": f"upstream connection error: {type(exc).__name__}: {detail}",
                       "code": "upstream_connection_error"}},
            status_code=502,
        )
    finally:
        state.upstream_active -= 1
    drop = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    return Response(
        upstream.content, status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() not in drop},
    )


async def health(request: Request) -> JSONResponse:
    state = request.app.state
    return JSONResponse({
        "ok": True,
        "upstream": state.upstream_base,
        "client_generation": state.client_generation,
        "active_upstream_requests": state.upstream_active,
        "active_websockets": state.websocket_active,
    })


def build_app(upstream_base: str | None = None) -> Starlette:
    """Assemble the proxy app. `upstream_base` falls back to the
    CODEXCOMP_UPSTREAM_BASE env var, then the official Codex backend."""
    base = upstream_base or os.environ.get("CODEXCOMP_UPSTREAM_BASE") or DEFAULT_UPSTREAM

    @asynccontextmanager
    async def lifespan(app: Starlette):
        log.info(
            "startup upstream=%s proxy=%s pool_max=%d client_generation=%d",
            app.state.upstream_base, _proxy_summary(), _POOL_MAX_CONNECTIONS,
            app.state.client_generation,
        )
        try:
            yield
        finally:
            log.info("shutdown active_requests=%d websocket_active=%d pool=[%s]",
                     app.state.upstream_active, app.state.websocket_active,
                     _pool_snapshot(app.state.client))
            await app.state.client.aclose()

    app = Starlette(routes=[
        Route("/healthz", health),
        Route("/v1/responses", responses_post, methods=["POST"]),
        WebSocketRoute("/v1/responses", responses_ws),
        Route("/v1/{path:path}", passthrough,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]),
    ], lifespan=lifespan)
    app.state.client_factory = _new_http_client
    app.state.client = app.state.client_factory()
    app.state.client_reset_lock = asyncio.Lock()
    app.state.client_generation = 1
    app.state.upstream_active = 0
    app.state.websocket_active = 0
    app.state.upstream_base = base.rstrip("/")
    return app
