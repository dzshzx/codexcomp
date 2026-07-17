"""Self-test for the WebSocket transport's stateful-protocol handling.

Replays the frame sequence Codex 0.142.x actually sends over
responses_websockets (prewarm `generate:false`, then `previous_response_id` +
incremental input) against a mock SSE upstream, and asserts the upstream only
ever sees stateless full-input bodies.

Run: uv run python test_ws.py
"""
import json

import httpx
from starlette.testclient import TestClient

from codexcomp.server import build_app

USER1 = {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]}
FCO = {"type": "function_call_output", "call_id": "call_1", "output": "ok"}

upstream_calls: list[dict] = []
upstream_request_headers: list[httpx.Headers] = []


def canned_sse(rid: str) -> bytes:
    msg = {"id": f"msg_{rid}", "type": "message", "role": "assistant",
           "content": [{"type": "output_text", "text": "ANSWER " + rid}]}
    events = [
        {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": msg},
        {"type": "response.output_item.done", "output_index": 0, "item": msg},
        {"type": "response.completed", "response": {
            "id": rid, "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "output_tokens_details": {"reasoning_tokens": 3}}}},
    ]
    out = b"".join(f"data: {json.dumps(ev)}\n\n".encode() for ev in events)
    return out + b"data: [DONE]\n\n"


def truncated_sse(rid: str) -> bytes:
    """One round cut exactly at the 518n-2 fingerprint, reasoning only."""
    r_item = {"id": f"rs_{rid}", "type": "reasoning", "summary": []}
    events = [
        {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": r_item},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {**r_item, "encrypted_content": "ENC_" + rid}},
        {"type": "response.completed", "response": {
            "id": rid, "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 516, "total_tokens": 526,
                      "output_tokens_details": {"reasoning_tokens": 516}}}},
    ]
    out = b"".join(f"data: {json.dumps(ev)}\n\n".encode() for ev in events)
    return out + b"data: [DONE]\n\n"


def mock_upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    upstream_calls.append(body)
    upstream_request_headers.append(request.headers)
    if body.get("model") == "connect-fail":
        raise httpx.ConnectError("proxy TLS failed", request=request)
    if body.get("model") == "fail-me":
        return httpx.Response(400, json={"detail": "boom"})
    rid = f"resp_up_{len(upstream_calls)}"
    headers = {"content-type": "text/event-stream",
               "x-reasoning-included": "true",
               "x-codex-turn-state": f"TS_{len(upstream_calls)}",
               "x-request-id": f"req_{len(upstream_calls)}"}
    content = canned_sse(rid)
    if body.get("model") == "truncate-once":
        is_continuation = any(isinstance(i, dict) and i.get("type") == "reasoning"
                              for i in body.get("input") or [])
        if not is_continuation:
            content = truncated_sse(rid)
    return httpx.Response(200, content=content, headers=headers)


def make_client() -> TestClient:
    app = build_app("http://upstream.test/v1")
    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_upstream))
    return TestClient(app)


def recv_until_terminal(ws) -> list[dict]:
    frames = []
    while True:
        frames.append(json.loads(ws.receive_text()))
        if frames[-1]["type"] in ("response.completed", "response.failed",
                                  "response.incomplete"):
            return frames


def test_prewarm_then_incremental():
    """Prewarm is acked locally; empty-delta and tool-loop frames are rebuilt
    to full input; upstream never sees ws-only fields."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        # 1. prewarm: generate=false, full input — must NOT reach upstream
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [USER1],
                                 "generate": False}))
        ack = json.loads(ws.receive_text())
        assert ack["type"] == "response.completed", ack
        prewarm_id = ack["response"]["id"]
        assert prewarm_id.startswith("resp_codexcomp_prewarm_"), prewarm_id
        assert ack["response"]["usage"]["total_tokens"] == 0
        assert upstream_calls == [], "prewarm must not reach upstream"

        # 2. real request: previous_response_id = prewarm id, EMPTY delta
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [],
                                 "previous_response_id": prewarm_id}))
        frames = recv_until_terminal(ws)
        term = frames[-1]
        assert term["type"] == "response.completed"
        up1 = upstream_calls[0]
        assert up1["input"] == [USER1], up1["input"]
        assert "previous_response_id" not in up1 and "generate" not in up1
        seqs = [f["sequence_number"] for f in frames]
        assert seqs == list(range(len(seqs))), seqs
        answer_item = term["response"]["output"][-1]

        # 3. tool loop: previous_response_id = upstream response id, delta=[FCO]
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [FCO],
                                 "previous_response_id": term["response"]["id"]}))
        frames = recv_until_terminal(ws)
        assert frames[-1]["type"] == "response.completed"
        up2 = upstream_calls[1]
        assert up2["input"] == [USER1, answer_item, FCO], \
            [i.get("type") for i in up2["input"]]
        assert "previous_response_id" not in up2


def test_unknown_previous_response():
    """An id this session never issued fails loud and drops the connection."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [],
                                 "previous_response_id": "resp_bogus"}))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "response.failed", frame
        assert frame["response"]["error"]["code"] == "unknown_previous_response_id"


def test_failed_turn_invalidates_state():
    """After a failed request, its would-be id must not be reusable."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [USER1],
                                 "generate": False}))
        prewarm_id = json.loads(ws.receive_text())["response"]["id"]
        ws.send_text(json.dumps({"type": "response.create", "model": "fail-me",
                                 "stream": True, "input": [],
                                 "previous_response_id": prewarm_id}))
        frames = recv_until_terminal(ws)
        assert frames[-1]["type"] == "response.failed"
        # the prewarm id was consumed by note_request; reusing it must fail loud
        ws.send_text(json.dumps({"type": "response.create", "model": "gpt-5.5",
                                 "stream": True, "input": [],
                                 "previous_response_id": prewarm_id}))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "response.failed"
        assert frame["response"]["error"]["code"] == "unknown_previous_response_id"


def test_ws_connect_error_yields_failed_terminal():
    """A transport-level upstream open failure must not escape into ASGI."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        ws.send_text(json.dumps({"type": "response.create", "model": "connect-fail",
                                 "stream": True, "input": [USER1]}))
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "response.failed", frame
        assert frame["response"]["status"] == "failed"
        err = frame["response"]["error"]
        assert err["code"] == "upstream_connection_error"
        assert "ConnectError" in err["message"]
        assert "proxy TLS failed" in err["message"]
        assert len(upstream_calls) == 1  # no retry: a re-send could double-generate


def test_post_sse_unchanged():
    """The HTTP fallback path still passes full-input bodies straight through."""
    client = make_client()
    n_before = len(upstream_calls)
    resp = client.post("/v1/responses",
                       json={"model": "gpt-5.5", "stream": True, "input": [USER1]})
    assert resp.status_code == 200
    assert "response.completed" in resp.text
    assert upstream_calls[n_before]["input"] == [USER1]


def test_post_header_propagation():
    """Upstream round-1 response headers are mirrored onto the downstream POST
    response — Codex reads x-reasoning-included (token-accounting mode),
    x-codex-turn-state (sticky routing) and x-request-id from them."""
    client = make_client()
    resp = client.post("/v1/responses",
                       json={"model": "gpt-5.5", "stream": True, "input": [USER1]})
    assert resp.status_code == 200
    assert resp.headers["x-reasoning-included"] == "true"
    assert resp.headers["x-codex-turn-state"] == "TS_1"
    assert resp.headers["x-request-id"] == "req_1"
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_turn_state_replayed_on_continuation():
    """The sticky-routing token minted by round 1's response is sent back as a
    request header on the fold's continuation round; downstream still sees
    round 1's headers."""
    client = make_client()
    resp = client.post("/v1/responses",
                       json={"model": "truncate-once", "stream": True, "input": [USER1]})
    assert resp.status_code == 200
    assert "response.completed" in resp.text
    assert len(upstream_calls) == 2, [b.get("model") for b in upstream_calls]
    h1, h2 = upstream_request_headers
    assert "x-codex-turn-state" not in h1
    assert h2["x-codex-turn-state"] == "TS_1"
    assert resp.headers["x-codex-turn-state"] == "TS_1"


def test_ws_handshake_stays_bare():
    """Codex reads server_reasoning_included ONLY from the 101 upgrade response
    (presence-based). The real backend's handshake does not carry it
    (live-probed 2026-07-07), so ours must not either — declaring a flag the
    upstream doesn't would skew Codex's context accounting vs a direct
    connection."""
    client = make_client()
    with client.websocket_connect("/v1/responses") as ws:
        names = {k.lower() for k, _ in (ws.extra_headers or [])}
        assert b"x-reasoning-included" not in names, ws.extra_headers


def test_passthrough_connect_error_returns_502():
    """GET /v1/models is outside fold(); transport failures still need a response."""
    def failing_upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("proxy TLS failed", request=request)

    app = build_app("http://upstream.test/v1")
    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(failing_upstream))
    client = TestClient(app)
    resp = client.get("/v1/models?client_version=0.143.0")
    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    assert err["code"] == "upstream_connection_error"
    assert "ConnectError" in err["message"]
    assert "proxy TLS failed" in err["message"]


def test_pool_timeout_rotates_client_once():
    """A saturated/wedged shared pool must self-heal without a process restart."""
    def exhausted(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("no connection available", request=request)

    app = build_app("http://upstream.test/v1")
    failed = httpx.AsyncClient(transport=httpx.MockTransport(exhausted))
    app.state.client = failed

    def recovered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    app.state.client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(recovered))
    client = TestClient(app)
    with client:
        resp = client.get("/v1/models")
        assert resp.status_code == 502, resp.text
        assert app.state.client_generation == 2
        assert app.state.client is not failed
        assert app.state.upstream_active == 0
        resp = client.get("/v1/models")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"models": []}
        assert app.state.client_generation == 2


def clear_state():
    upstream_calls.clear()
    upstream_request_headers.clear()


def main():
    for test in (test_prewarm_then_incremental,
                 test_unknown_previous_response,
                 test_failed_turn_invalidates_state,
                 test_ws_connect_error_yields_failed_terminal,
                 test_post_sse_unchanged,
                 test_post_header_propagation,
                 test_turn_state_replayed_on_continuation,
                 test_ws_handshake_stays_bare,
                 test_passthrough_connect_error_returns_502,
                 test_pool_timeout_rotates_client_once):
        clear_state()
        test()
    print("ws transport self-test: ALL PASS")


main()
