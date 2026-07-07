"""Self-test for the fold state machine with canned upstream rounds.

Run: uv run python test_fold.py
"""
import asyncio
import json
from types import SimpleNamespace

import codexcomp.server as server
from codexcomp.fold import DONE, RoundOpenError, fold, STEP
from codexcomp.server import parse_sse
from starlette.websockets import WebSocketDisconnect
from websockets.protocol import State


def reasoning_round(
    rid: str,
    reasoning_toks: int,
    text: str | None,
    enc: bool = True,
    resp_id: str = "resp_1",
):
    """Canned upstream events for one round."""
    item = {"id": rid, "type": "reasoning", "summary": []}
    done_item = dict(item)
    if enc:
        done_item["encrypted_content"] = "ENC_" + rid
    evs = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": resp_id, "created_at": 111, "status": "in_progress"}},
        {"type": "response.in_progress", "sequence_number": 1, "response": {"id": resp_id}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": done_item},
    ]
    if text is not None:
        msg = {"id": "msg_" + rid, "type": "message", "role": "assistant"}
        evs += [
            {"type": "response.output_item.added", "output_index": 1, "item": msg},
            {"type": "response.output_text.delta", "output_index": 1,
             "item_id": msg["id"], "content_index": 0, "delta": text},
            {"type": "response.output_item.done", "output_index": 1,
             "item": {**msg, "content": [{"type": "output_text", "text": text}]}},
        ]
    evs.append({"type": "response.completed", "response": {
        "id": resp_id, "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": reasoning_toks + (20 if text else 0),
                  "total_tokens": 120 + reasoning_toks,
                  "output_tokens_details": {"reasoning_tokens": reasoning_toks}},
    }})
    # NB: real upstream sends [DONE] after the terminal event; the fold stops at
    # the terminal, so DONE never reaches it — stream close is the terminator.
    return evs


async def main():
    opened_bodies = []
    rounds = [
        reasoning_round("rs_1", STEP - 2, "TRUNCATED ANSWER", resp_id="resp_1"),
        reasoning_round("rs_2", 2 * STEP - 2, "STILL TRUNCATED", resp_id="resp_2"),
        reasoning_round("rs_3", 404, "REAL ANSWER", resp_id="resp_3"),
    ]

    async def opener(body):
        opened_bodies.append(body)
        idx = len(opened_bodies) - 1

        async def gen():
            for ev in rounds[idx]:
                yield ev
        return gen()

    out = []
    async for ev in fold({"model": "gpt-5.5", "input": [{"type": "message", "role": "user"}],
                          "stream": True, "previous_response_id": "resp_previous"}, opener):
        out.append(ev)

    # --- assertions -----------------------------------------------------------
    assert len(opened_bodies) == 3, f"expected 3 rounds, got {len(opened_bodies)}"

    # Round 1 preserves server-side conversation context when Codex supplied
    # previous_response_id. Hidden continuation rounds must not reuse that
    # original id; they chain from the just-truncated upstream response id and
    # follow Codex's delta rule by sending only the new nudge input.
    assert opened_bodies[0]["previous_response_id"] == "resp_previous"
    b2 = opened_bodies[1]
    assert b2["previous_response_id"] == "resp_1"
    types2 = [i.get("type") for i in b2["input"]]
    assert types2 == ["message"], types2
    assert b2["input"][-1]["phase"] == "commentary"
    assert "reasoning.encrypted_content" in b2["include"]
    b3 = opened_bodies[2]
    assert b3["previous_response_id"] == "resp_2"
    assert [i.get("type") for i in b3["input"]] == ["message"]

    dict_events = [e for e in out if isinstance(e, dict)]
    # exactly one terminal, and it is the LAST dict event
    terminals = [e for e in dict_events if e["type"].startswith("response.")
                 and e["type"] in ("response.completed", "response.failed", "response.incomplete")]
    assert len(terminals) == 1 and dict_events[-1] is terminals[0]
    term = terminals[0]["response"]
    # Terminal id follows the final clean upstream response so the next Codex
    # turn can chain to it, even though early lifecycle events came from round 1.
    assert term["id"] == "resp_3", term["id"]

    # truncated messages are discarded; only the clean round's text is flushed
    deltas = [e["delta"] for e in dict_events if e["type"] == "response.output_text.delta"]
    assert deltas == ["REAL ANSWER"], deltas

    # sequence numbers proxy-owned and monotonic; output_index renumbered 0..3
    seqs = [e["sequence_number"] for e in dict_events]
    assert seqs == list(range(len(seqs))), "sequence not monotonic"
    ois = sorted({e.get("output_index") for e in dict_events if "output_index" in e})
    assert ois == [0, 1, 2, 3], ois  # 3 reasoning items + 1 flushed message

    # usage: reasoning summed, input from round 1, billed usage in metadata
    u = term["usage"]
    expect_reason = (STEP - 2) + (2 * STEP - 2) + 404
    assert u["output_tokens_details"]["reasoning_tokens"] == expect_reason
    assert u["input_tokens"] == 100
    assert term["metadata"]["proxy_billed_usage"]["input_tokens"] == 300
    assert [r["n"] for r in term["metadata"]["proxy_rounds"]] == [1, 2, None]

    # output preserved in order: rs_1, rs_2, rs_3 reasoning + final message
    otypes = [(i["type"], i.get("id")) for i in term["output"]]
    assert otypes == [("reasoning", "rs_1"), ("reasoning", "rs_2"),
                      ("reasoning", "rs_3"), ("message", "msg_rs_3")], otypes

    # Regression: real terminal frames may omit response.id even though
    # response.created had it. Continuation must still chain to the current
    # round response id, not Codex's original previous_response_id.
    missing_terminal_id_opened = []
    missing_terminal_id_rounds = [
        reasoning_round("rs_missing_id_1", STEP - 2, "TRUNCATED", resp_id="resp_created_1"),
        reasoning_round("rs_missing_id_2", 123, "CLEAN", resp_id="resp_created_2"),
    ]
    missing_terminal_id_rounds[0][-1]["response"].pop("id", None)

    async def missing_terminal_id_opener(body):
        missing_terminal_id_opened.append(body)
        idx = len(missing_terminal_id_opened) - 1

        async def gen():
            for ev in missing_terminal_id_rounds[idx]:
                yield ev
        return gen()

    missing_terminal_id_out = []
    async for ev in fold({
        "model": "gpt-5.5",
        "input": [{"type": "message", "role": "user"}],
        "previous_response_id": "resp_original_prev",
    }, missing_terminal_id_opener):
        missing_terminal_id_out.append(ev)
    assert len(missing_terminal_id_opened) == 2, missing_terminal_id_opened
    assert missing_terminal_id_opened[1]["previous_response_id"] == "resp_created_1"
    missing_terminal_id_terms = [
        e for e in missing_terminal_id_out
        if isinstance(e, dict) and e.get("type") == "response.completed"
    ]
    assert missing_terminal_id_terms[-1]["response"]["id"] == "resp_created_2"

    # Regression: a continuation must explicitly close the previous round's
    # async iterator before opening the next round. The real sticky WebSocket
    # opener holds a per-upstream lock until its generator is closed; without an
    # explicit aclose(), round 2 can deadlock behind round 1 after a 516 terminal.
    lock = asyncio.Lock()
    close_count = 0
    locking_opened = []
    locking_rounds = [
        reasoning_round("rs_lock_1", STEP - 2, "LOCKED TRUNCATED", resp_id="resp_lock_1"),
        reasoning_round("rs_lock_2", 123, "LOCK RELEASED", resp_id="resp_lock_2"),
    ]

    class LockingIterator:
        def __init__(self, events):
            self._events = iter(events)
            self._closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration:
                await self.aclose()
                raise StopAsyncIteration

        async def aclose(self):
            nonlocal close_count
            if not self._closed:
                self._closed = True
                close_count += 1
                lock.release()

    async def locking_opener(body):
        await asyncio.wait_for(lock.acquire(), timeout=0.5)
        idx = len(locking_opened)
        locking_opened.append(body)
        return LockingIterator(locking_rounds[idx])

    locking_out = []
    async for ev in fold({"model": "gpt-5.5", "input": []}, locking_opener):
        locking_out.append(ev)
    assert len(locking_opened) == 2, locking_opened
    assert close_count == 2, close_count
    locking_deltas = [
        e["delta"] for e in locking_out
        if isinstance(e, dict) and e.get("type") == "response.output_text.delta"
    ]
    assert locking_deltas == ["LOCK RELEASED"], locking_deltas

    # If a truncated-id hidden continuation is rejected with
    # previous_response_not_found, retry once as no-previous-response full replay
    # only when the original request was already full-context.
    fallback_opened = []
    fallback_rounds = [
        reasoning_round("rs_fb_1", STEP - 2, "FB TRUNCATED", resp_id="resp_fb_1"),
        reasoning_round("rs_fb_3", 101, "FALLBACK ANSWER", resp_id="resp_fb_3"),
    ]

    async def fallback_opener(body):
        fallback_opened.append(body)
        idx = len(fallback_opened) - 1

        async def gen():
            if idx == 1:
                yield {"type": "response.created", "response": {"id": "resp_fb_bad", "status": "in_progress"}}
                raise ConnectionError("previous_response_not_found: resp_fb_1")
            for ev in fallback_rounds[0 if idx == 0 else 1]:
                yield ev
        return gen()

    fallback_out = []
    async for ev in fold({"model": "gpt-5.5", "input": [{"type": "message", "role": "user"}]}, fallback_opener):
        fallback_out.append(ev)
    assert len(fallback_opened) == 3, fallback_opened
    assert fallback_opened[1]["previous_response_id"] == "resp_fb_1"
    assert [i.get("type") for i in fallback_opened[1]["input"]] == ["message"]
    assert "previous_response_id" not in fallback_opened[2]
    assert [i.get("type") for i in fallback_opened[2]["input"]] == ["message", "reasoning", "message"]
    fallback_deltas = [
        e["delta"] for e in fallback_out
        if isinstance(e, dict) and e.get("type") == "response.output_text.delta"
    ]
    assert fallback_deltas == ["FALLBACK ANSWER"], fallback_deltas

    # The same previous_response_not_found must not silently drop context when
    # the original client request was incremental and relied on previous_response_id.
    incremental_opened = []

    async def incremental_opener(body):
        incremental_opened.append(body)
        idx = len(incremental_opened) - 1

        async def gen():
            if idx == 1:
                yield {"type": "response.created", "response": {"id": "resp_inc_bad", "status": "in_progress"}}
                raise ConnectionError("previous_response_not_found: resp_inc_1")
            for ev in fallback_rounds[0]:
                yield ev
        return gen()

    incremental_out = []
    async for ev in fold({
        "model": "gpt-5.5",
        "previous_response_id": "resp_before",
        "input": [{"type": "function_call_output"}],
    }, incremental_opener):
        incremental_out.append(ev)
    assert len(incremental_opened) == 2, incremental_opened
    inc_terms = [e for e in incremental_out if isinstance(e, dict) and e.get("type") == "response.incomplete"]
    assert inc_terms and inc_terms[-1]["response"]["incomplete_details"]["reason"] == "upstream_error"

    failed_round = reasoning_round("rs_fail", STEP - 2, "FAILED", resp_id="resp_fail")
    failed_round[-1]["type"] = "response.failed"
    failed_round[-1]["response"]["status"] = "failed"
    failed_opened = []

    async def failed_opener(body):
        failed_opened.append(body)

        async def gen():
            for ev in failed_round:
                yield ev
        return gen()

    failed_out = []
    async for ev in fold({"model": "gpt-5.5", "input": []}, failed_opener):
        failed_out.append(ev)
    failed_terms = [
        e for e in failed_out
        if isinstance(e, dict) and e.get("type") in ("response.completed", "response.failed")
    ]
    assert len(failed_opened) == 1, "response.failed must not open a continuation"
    assert failed_terms and failed_terms[-1]["type"] == "response.failed", failed_terms

    async def chunks():
        yield 'event: response.completed\r\n'
        yield 'data: {"type":"response.completed","response":{"id":"tail"}}'

    parsed = []
    async for ev in parse_sse(chunks()):
        parsed.append(ev)
    assert parsed == [{"type": "response.completed", "response": {"id": "tail"}}], parsed
    assert server.passthrough_headers(
        [(b"accept", b"application/json"), (b"host", b"localhost")]
    ).get("accept") == "application/json"

    class NoCallClient:
        def build_request(self, *args, **kwargs):
            raise AssertionError("previous_response_id should be rejected before HTTP")

    try:
        await server.UpstreamRounds(NoCallClient(), {}).open({
            "model": "gpt-5.5",
            "previous_response_id": "resp_prev",
            "input": [{"type": "function_call_output"}],
        })
    except RoundOpenError as exc:
        assert exc.status == 400
    else:
        raise AssertionError("previous_response_id request should fail fast")

    assert server.upstream_ws_pool_key({"authorization": "Bearer same"}) is None
    assert server.upstream_ws_pool_key({
        "authorization": "Bearer same",
        "x-client-request-id": "req_1",
    }) is not None
    assert server.upstream_ws_pool_key({
        "authorization": "Bearer same",
        "session-id": "sess_1",
        "thread-id": "thread_1",
        "x-client-request-id": "req_1",
    }) == server.upstream_ws_pool_key({
        "authorization": "Bearer same",
        "session-id": "sess_1",
        "thread-id": "thread_1",
        "x-client-request-id": "req_2",
    })
    assert server.UpstreamWsRounds._is_closed(SimpleNamespace(state=State.CLOSED))

    # WebSocket response.cancel must be read while a turn is still streaming.
    started = asyncio.Event()
    never = asyncio.Event()
    fake_rounds = []

    class FakeRounds:
        def __init__(self, headers):
            self.closed = False
            fake_rounds.append(self)

        async def open(self, body):
            raise AssertionError("fake fold does not call open")

        async def aclose(self):
            self.closed = True

    class FakeWebSocket:
        def __init__(self):
            self.headers = SimpleNamespace(raw=[])
            self.app = SimpleNamespace(state=SimpleNamespace(client=object()))
            self.accepted = False
            self.receive_calls = 0
            self.sent = []

        async def accept(self):
            self.accepted = True

        async def receive_text(self):
            self.receive_calls += 1
            if self.receive_calls == 1:
                return json.dumps({"type": "response.create", "model": "gpt-5.5", "input": []})
            if self.receive_calls == 2:
                await asyncio.wait_for(started.wait(), timeout=1)
                return json.dumps({"type": "response.cancel"})
            raise WebSocketDisconnect()

        async def send_text(self, msg):
            self.sent.append(json.loads(msg))

    async def fake_fold(body, opener):
        started.set()
        await never.wait()
        yield {}  # pragma: no cover - makes this an async generator

    old_fold = server.fold
    old_rounds = server.UpstreamWsRounds
    ws = FakeWebSocket()
    server.fold = fake_fold
    server.UpstreamWsRounds = FakeRounds
    try:
        await asyncio.wait_for(server.responses_ws(ws), timeout=1)
    finally:
        server.fold = old_fold
        server.UpstreamWsRounds = old_rounds

    assert ws.accepted
    assert ws.receive_calls >= 2, "cancel frame was not read while fold was active"
    assert fake_rounds and fake_rounds[0].closed
    assert not any(e.get("type") == "response.completed" for e in ws.sent)

    # If the websocket writer dies while the producer is blocked on a full queue,
    # disconnect cleanup must not hang trying to enqueue a sentinel nobody reads.
    sent_once = asyncio.Event()

    class FailingSendWebSocket:
        def __init__(self):
            self.headers = SimpleNamespace(raw=[])
            self.app = SimpleNamespace(state=SimpleNamespace(client=object()))
            self.receive_calls = 0

        async def accept(self):
            pass

        async def receive_text(self):
            self.receive_calls += 1
            if self.receive_calls == 1:
                return json.dumps({"type": "response.create", "model": "gpt-5.5", "input": []})
            await asyncio.wait_for(sent_once.wait(), timeout=1)
            await never.wait()

        async def send_text(self, msg):
            sent_once.set()
            raise RuntimeError("client send failed")

    async def spam_fold(body, opener):
        for i in range(1000):
            yield {"type": "response.output_text.delta", "output_index": 0, "delta": str(i)}

    server.fold = spam_fold
    server.UpstreamWsRounds = FakeRounds
    try:
        await asyncio.wait_for(server.responses_ws(FailingSendWebSocket()), timeout=1)
    finally:
        server.fold = old_fold
        server.UpstreamWsRounds = old_rounds
    assert fake_rounds[-1].closed

    # Upstream WebSocket state is sticky: reconnecting downstream WebSockets for
    # the same Codex session/thread must reuse the same upstream connection so
    # previous_response_id remains resolvable upstream.
    pooled_created = []

    class PooledFakeRounds:
        def __init__(self, headers):
            self.headers = headers
            self.opened = []
            self.closed = 0
            pooled_created.append(self)

        async def open(self, body):
            self.opened.append(body)
            resp_id = f"resp_pool_{len(self.opened)}"

            async def gen():
                yield {"type": "response.created",
                       "response": {"id": resp_id, "status": "in_progress"}}
                yield {"type": "response.completed",
                       "response": {
                           "id": resp_id,
                           "status": "completed",
                           "usage": {"input_tokens": 1, "output_tokens": 1,
                                     "total_tokens": 2,
                                     "output_tokens_details": {"reasoning_tokens": 0}},
                       }}
            return gen()

        async def aclose(self):
            self.closed += 1

    class OneTurnWebSocket:
        def __init__(self, envelope, pool):
            self.headers = SimpleNamespace(raw=[
                (b"authorization", b"Bearer same"),
                (b"session-id", b"sess_1"),
                (b"thread-id", b"thread_1"),
                (b"x-client-request-id", b"thread_1"),
            ])
            self.app = SimpleNamespace(state=SimpleNamespace(upstream_ws_pool=pool))
            self.envelope = envelope
            self.receive_calls = 0
            self.completed = asyncio.Event()

        async def accept(self):
            pass

        async def receive_text(self):
            self.receive_calls += 1
            if self.receive_calls == 1:
                return json.dumps(self.envelope)
            await asyncio.wait_for(self.completed.wait(), timeout=1)
            await asyncio.sleep(0)
            raise WebSocketDisconnect()

        async def send_text(self, msg):
            ev = json.loads(msg)
            if ev.get("type") == "response.completed":
                self.completed.set()

    old_rounds = server.UpstreamWsRounds
    server.UpstreamWsRounds = PooledFakeRounds
    pool = server.UpstreamWsPool()
    try:
        await server.responses_ws(OneTurnWebSocket({
            "type": "response.create", "model": "gpt-5.5", "input": []
        }, pool))
        await server.responses_ws(OneTurnWebSocket({
            "type": "response.create", "model": "gpt-5.5",
            "previous_response_id": "resp_pool_1",
            "input": [{"type": "function_call_output"}],
        }, pool))
    finally:
        server.UpstreamWsRounds = old_rounds
    assert len(pooled_created) == 1, "same session/thread should reuse upstream WS rounds"
    assert [bool(b.get("previous_response_id")) for b in pooled_created[0].opened] == [
        False, True,
    ]
    assert pooled_created[0].closed == 0, "idle pooled WS should survive downstream disconnects"

    # A second downstream connection that is waiting for the same pooled upstream
    # lock must not close/cancel the first connection's active upstream round.
    class ContendedRounds:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.owner_task = None
            self.closed = 0
            self.open_calls = 0
            self.first_acquired = asyncio.Event()
            self.second_waiting = asyncio.Event()
            self.release_first = asyncio.Event()

        async def open(self, body):
            self.open_calls += 1
            call_no = self.open_calls
            if call_no == 2:
                self.second_waiting.set()
            await self.lock.acquire()
            self.owner_task = asyncio.current_task()

            async def gen():
                try:
                    if call_no == 1:
                        self.first_acquired.set()
                        await self.release_first.wait()
                    yield {"type": "response.created",
                           "response": {"id": f"resp_contended_{call_no}"}}
                    yield {"type": "response.completed",
                           "response": {"id": f"resp_contended_{call_no}",
                                        "status": "completed"}}
                finally:
                    self.owner_task = None
                    self.lock.release()
            return gen()

        async def cancel_for_task(self, task):
            return self.owner_task is task

        async def aclose(self):
            self.closed += 1
            self.release_first.set()

    class FixedPool:
        def __init__(self, rounds):
            self.rounds = rounds

        async def get(self, headers):
            return self.rounds, True

    class ContendedWebSocket:
        def __init__(self, pool, name):
            self.headers = SimpleNamespace(raw=[
                (b"authorization", b"Bearer same"),
                (b"session-id", b"sess_contended"),
            ])
            self.app = SimpleNamespace(state=SimpleNamespace(upstream_ws_pool=pool))
            self.name = name
            self.receive_calls = 0
            self.completed = asyncio.Event()

        async def accept(self):
            pass

        async def receive_text(self):
            self.receive_calls += 1
            if self.receive_calls == 1:
                return json.dumps({"type": "response.create", "model": "gpt-5.5", "input": []})
            if self.name == "second" and self.receive_calls == 2:
                await asyncio.wait_for(contended.second_waiting.wait(), timeout=1)
                return json.dumps({"type": "response.cancel"})
            if self.name == "second":
                raise WebSocketDisconnect()
            await asyncio.wait_for(self.completed.wait(), timeout=1)
            raise WebSocketDisconnect()

        async def send_text(self, msg):
            if json.loads(msg).get("type") == "response.completed":
                self.completed.set()

    contended = ContendedRounds()
    contended_pool = FixedPool(contended)
    ws1 = ContendedWebSocket(contended_pool, "first")
    ws2 = ContendedWebSocket(contended_pool, "second")
    t1 = asyncio.create_task(server.responses_ws(ws1))
    await asyncio.wait_for(contended.first_acquired.wait(), timeout=1)
    await server.responses_ws(ws2)
    assert contended.closed == 0, "waiting pooled cancel must not close active pooled WS"
    contended.release_first.set()
    await asyncio.wait_for(t1, timeout=1)

    # Downstream WebSocket turns should be forwarded to upstream WebSocket, where
    # previous_response_id is supported.
    class FakeUpstreamWs:
        def __init__(self):
            self.sent = []
            self.closed = False
            self.events = [
                {"type": "response.created", "response": {"id": "resp_ws"}},
                {"type": "response.completed", "response": {"id": "resp_ws", "status": "completed"}},
            ]

        async def send(self, msg):
            self.sent.append(json.loads(msg))

        async def recv(self):
            return json.dumps(self.events.pop(0))

        async def close(self):
            self.closed = True

    fake_upstream = FakeUpstreamWs()
    connect_calls = []

    async def fake_connect(url, **kwargs):
        connect_calls.append((url, kwargs))
        return fake_upstream

    old_connect = server.websockets.connect
    server.websockets.connect = fake_connect
    try:
        ws_rounds = server.UpstreamWsRounds(
            server.upstream_ws_headers([(b"authorization", b"Bearer x")])
        )
        ws_out = []
        async for ev in await ws_rounds.open({
            "model": "gpt-5.5",
            "previous_response_id": "resp_prev",
            "input": [{"type": "function_call_output"}],
        }):
            ws_out.append(ev)
        await ws_rounds.aclose()
    finally:
        server.websockets.connect = old_connect
    assert fake_upstream.sent[0]["type"] == "response.create"
    assert fake_upstream.sent[0]["previous_response_id"] == "resp_prev"
    assert connect_calls[0][1]["additional_headers"]["OpenAI-Beta"] == server.RESPONSES_WS_BETA
    assert fake_upstream.closed
    assert ws_out[-1]["type"] == "response.completed"

    # Mid-stream upstream WS error frames should be folded as an upstream error,
    # not escape as RoundOpenError after partial response events were emitted.
    class ErrorAfterCreatedWs:
        def __init__(self):
            self.sent = []
            self.events = [
                {"type": "response.created", "response": {"id": "resp_error"}},
                {"type": "error", "status": 502, "message": "upstream broke"},
            ]

        async def send(self, msg):
            self.sent.append(json.loads(msg))

        async def recv(self):
            return json.dumps(self.events.pop(0))

        async def close(self):
            pass

    async def fake_error_connect(url, **kwargs):
        return ErrorAfterCreatedWs()

    old_connect = server.websockets.connect
    server.websockets.connect = fake_error_connect
    try:
        error_rounds = server.UpstreamWsRounds(server.upstream_ws_headers([]))
        error_out = []
        async for ev in fold({"model": "gpt-5.5", "input": []}, error_rounds.open):
            error_out.append(ev)
    finally:
        server.websockets.connect = old_connect
    assert error_out[-1]["response"]["status"] == "incomplete"
    assert error_out[-1]["response"]["incomplete_details"]["reason"] == "upstream_error"

    # A stalled upstream WS round must time out and release the round lock.
    class HangingWs:
        async def send(self, msg):
            pass

        async def recv(self):
            await never.wait()

        async def close(self):
            pass

    async def fake_hanging_connect(url, **kwargs):
        return HangingWs()

    old_connect = server.websockets.connect
    old_timeout = server.RESPONSES_WS_READ_TIMEOUT
    server.websockets.connect = fake_hanging_connect
    server.RESPONSES_WS_READ_TIMEOUT = 0.01
    try:
        hanging_rounds = server.UpstreamWsRounds(server.upstream_ws_headers([]))
        hanging_out = []
        async for ev in fold({"model": "gpt-5.5", "input": []}, hanging_rounds.open):
            hanging_out.append(ev)
    finally:
        server.websockets.connect = old_connect
        server.RESPONSES_WS_READ_TIMEOUT = old_timeout
    assert hanging_out[-1]["response"]["status"] == "incomplete"
    assert not hanging_rounds.is_busy()

    # Pool cleanup should evict idle stale entries.
    class StalePoolItem:
        def __init__(self):
            self.last_used = 0.0
            self.closed = 0

        def is_busy(self):
            return False

        def is_closed(self):
            return False

        async def aclose(self):
            self.closed += 1

    cleanup_pool = server.UpstreamWsPool()
    stale_item = StalePoolItem()
    cleanup_pool._items[("old",)] = stale_item
    await cleanup_pool.get({"session-id": "new_session"})
    assert stale_item.closed == 1
    assert ("old",) not in cleanup_pool._items

    print("fold self-test: ALL PASS")
    print("terminal usage:", json.dumps(u))


asyncio.run(main())
