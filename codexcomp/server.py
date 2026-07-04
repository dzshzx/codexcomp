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

Upstream is always the SSE POST endpoint; the fold state machine (fold.py) is
transport-agnostic.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import zlib
from typing import Any, AsyncIterator

import httpx
import zstandard
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .fold import DONE, RoundOpenError, fold

log = logging.getLogger("codexcomp.server")

UPSTREAM_BASE = os.environ.get(
    "CODEXCOMP_UPSTREAM_BASE", "https://chatgpt.com/backend-api/codex"
).rstrip("/")
RESPONSES_URL = UPSTREAM_BASE + "/responses"

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
    out["accept"] = "text/event-stream"
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
    previous round's response before opening the next."""

    def __init__(self, client: httpx.AsyncClient, headers: dict[str, str]):
        self.client = client
        self.headers = headers
        self._resp: httpx.Response | None = None

    async def open(self, body: dict[str, Any]) -> AsyncIterator[dict | object]:
        await self.aclose()
        req = self.client.build_request(
            "POST", RESPONSES_URL,
            content=json.dumps(body, ensure_ascii=False).encode(),
            headers={**self.headers, "content-type": "application/json"},
            timeout=httpx.Timeout(connect=30, read=600, write=60, pool=30),
        )
        resp = await self.client.send(req, stream=True)
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode(errors="replace")
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


# --- downstream endpoints -----------------------------------------------------


def sse_bytes(ev: dict | object) -> bytes:
    if ev is DONE:
        return b"data: [DONE]\n\n"
    etype = ev.get("type", "message")  # type: ignore[union-attr]
    return f"event: {etype}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()


async def responses_post(request: Request) -> Response:
    raw = await request.body()
    try:
        raw = decompress_body(raw, request.headers.get("content-encoding"))
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": f"bad request body: {exc}"}, status_code=400)

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
    headers = upstream_headers(ws.headers.raw)
    rounds = UpstreamRounds(ws.app.state.client, headers)
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
                async for ev in fold(body, rounds.open):
                    if ev is DONE:
                        continue
                    await ws.send_text(json.dumps(ev, ensure_ascii=False))
            except RoundOpenError as exc:
                await ws.send_text(json.dumps({
                    "type": "response.failed",
                    "response": {"status": "failed",
                                 "error": {"message": str(exc), "code": exc.status}},
                }))
    except WebSocketDisconnect:
        pass
    finally:
        await rounds.aclose()


async def passthrough(request: Request) -> Response:
    """Transparent proxy for every other /v1/* call (e.g. GET /v1/models)."""
    suffix = request.path_params["path"]
    url = f"{UPSTREAM_BASE}/{suffix}"
    if request.url.query:
        url += "?" + request.url.query
    content = await request.body()
    if content:
        content = decompress_body(content, request.headers.get("content-encoding"))
    headers = upstream_headers(request.headers.raw)
    headers.pop("accept", None)
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
    return app


app = build_app()
