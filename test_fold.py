"""Self-test for the fold state machine with canned upstream rounds.

Run: uv run python test_fold.py
"""
import asyncio
import json

from codexcomp.fold import DONE, RoundOpenError, fold, STEP


def reasoning_round(rid: str, reasoning_toks: int, text: str | None, enc: bool = True):
    """Canned upstream events for one round."""
    item = {"id": rid, "type": "reasoning", "summary": []}
    done_item = dict(item)
    if enc:
        done_item["encrypted_content"] = "ENC_" + rid
    evs = [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.in_progress", "sequence_number": 1, "response": {"id": "resp_1"}},
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
        "id": "resp_1", "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": reasoning_toks + (20 if text else 0),
                  "total_tokens": 120 + reasoning_toks,
                  "output_tokens_details": {"reasoning_tokens": reasoning_toks}},
    }})
    # NB: real upstream sends [DONE] after the terminal event; the fold stops at
    # the terminal, so DONE never reaches it — stream close is the terminator.
    return evs


def stall_round(rid: str, reasoning_toks: int, text: str):
    """Canned events for a continuation round that emits NO reasoning item —
    only a buffered message. reasoning_toks is what upstream usage reports
    (0 = a stalled nudge)."""
    msg = {"id": "msg_" + rid, "type": "message", "role": "assistant"}
    return [
        {"type": "response.created", "sequence_number": 0,
         "response": {"id": "resp_1", "created_at": 111, "status": "in_progress"}},
        {"type": "response.in_progress", "sequence_number": 1, "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": msg},
        {"type": "response.output_text.delta", "output_index": 0,
         "item_id": msg["id"], "content_index": 0, "delta": text},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {**msg, "content": [{"type": "output_text", "text": text}]}},
        {"type": "response.completed", "response": {
            "id": "resp_1", "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": reasoning_toks + 20,
                      "total_tokens": 120 + reasoning_toks,
                      "output_tokens_details": {"reasoning_tokens": reasoning_toks}}}},
    ]


async def test_happy_fold():
    opened_bodies = []
    rounds = [
        reasoning_round("rs_1", STEP - 2, "TRUNCATED ANSWER"),   # 516 -> continue
        reasoning_round("rs_2", 2 * STEP - 2, "STILL TRUNCATED"),  # 1034 -> continue
        reasoning_round("rs_3", 404, "REAL ANSWER"),             # clean
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
                          "stream": True}, opener):
        out.append(ev)

    # --- assertions -----------------------------------------------------------
    assert len(opened_bodies) == 3, f"expected 3 rounds, got {len(opened_bodies)}"

    # continuation bodies replay reasoning + commentary nudge, drop prev id
    b2 = opened_bodies[1]
    types2 = [i.get("type") for i in b2["input"]]
    assert types2 == ["message", "reasoning", "message"], types2
    assert b2["input"][-1]["phase"] == "commentary"
    assert "reasoning.encrypted_content" in b2["include"]
    b3 = opened_bodies[2]
    assert [i.get("type") for i in b3["input"]] == [
        "message", "reasoning", "message", "reasoning", "message"]

    dict_events = [e for e in out if isinstance(e, dict)]
    # exactly one terminal, and it is the LAST dict event
    terminals = [e for e in dict_events if e["type"].startswith("response.")
                 and e["type"] in ("response.completed", "response.failed", "response.incomplete")]
    assert len(terminals) == 1 and dict_events[-1] is terminals[0]
    term = terminals[0]["response"]

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

    print("terminal usage:", json.dumps(u))


async def test_compaction_passthrough():
    """A remote-compaction request (input ends with compaction_trigger) is never
    folded, even when its reasoning usage lands exactly on a 518n-2 boundary:
    Codex needs the trigger to stay last and exactly one compaction item back."""
    calls = []
    r_item = {"id": "rs_c", "type": "reasoning", "summary": []}
    c_item = {"id": "cmp_1", "type": "compaction", "encrypted_content": "CMP_ENC"}
    evs = [
        {"type": "response.created", "response": {"id": "resp_c", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": r_item},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {**r_item, "encrypted_content": "ENC_c"}},
        {"type": "response.output_item.added", "output_index": 1, "item": c_item},
        {"type": "response.output_item.done", "output_index": 1, "item": c_item},
        {"type": "response.completed", "response": {
            "id": "resp_c", "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": STEP - 2,
                      "total_tokens": 98 + STEP,
                      "output_tokens_details": {"reasoning_tokens": STEP - 2}}}},
    ]

    async def opener(body):
        calls.append(body)

        async def gen():
            for ev in evs:
                yield ev
        return gen()

    out = [ev async for ev in fold(
        {"model": "gpt-5.5", "stream": True,
         "input": [{"type": "message", "role": "user"}, {"type": "compaction_trigger"}]},
        opener)]
    assert len(calls) == 1, f"compaction request must stay single-round, opened {len(calls)}"
    assert calls[0]["input"][-1] == {"type": "compaction_trigger"}
    term = out[-1]
    assert term["type"] == "response.completed", term
    resp = term["response"]
    assert [i["type"] for i in resp["output"]] == ["reasoning", "compaction"], resp["output"]
    assert resp["metadata"]["proxy_stopped_reason"] == "compaction_request"


async def test_round1_rejected():
    """Upstream rejects round 1: fold itself yields the response.failed terminal."""
    async def opener(body):
        raise RoundOpenError(429, "quota exceeded")

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(out) == 1, out
    ev = out[0]
    assert ev["type"] == "response.failed", ev
    assert ev["response"]["status"] == "failed"
    assert ev["response"]["error"]["code"] == 429
    assert "quota exceeded" in ev["response"]["error"]["message"]
    assert ev["sequence_number"] == 0


async def test_continuation_open_fails():
    """Round 2 fails to open: incomplete terminal, round 1 reasoning kept,
    truncated message never flushed."""
    calls = []

    async def opener(body):
        calls.append(body)
        if len(calls) > 1:
            raise RoundOpenError(500, "boom")

        async def gen():
            for ev in reasoning_round("rs_1", STEP - 2, "TRUNCATED"):
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    assert len(calls) == 2, len(calls)
    term = out[-1]
    assert term["type"] == "response.incomplete", term
    resp = term["response"]
    assert resp["incomplete_details"]["reason"] == "upstream_error"
    assert resp["metadata"]["proxy_stopped_reason"] == "upstream_error"
    assert [i["type"] for i in resp["output"]] == ["reasoning"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == [], deltas


async def test_upstream_eof():
    """Stream ends without a terminal event: incomplete terminal, tentative
    output is not an answer."""
    evs = reasoning_round("rs_1", 404, "TENTATIVE")[:-1]  # strip the terminal

    async def opener(body):
        async def gen():
            for ev in evs:
                yield ev
        return gen()

    out = [ev async for ev in fold({"input": [], "stream": True}, opener)]
    term = out[-1]
    assert term["type"] == "response.incomplete", term
    assert term["response"]["incomplete_details"]["reason"] == "upstream_eof"
    assert [i["type"] for i in term["response"]["output"]] == ["reasoning"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == [], deltas


async def test_zero_stall_recovers():
    """A continuation round returning reasoning_tokens=0 is a stalled nudge, not
    a clean answer: it is re-nudged, and the eventual real answer is flushed."""
    opened_bodies = []
    rounds = [
        reasoning_round("rs_1", STEP - 2, "TRUNCATED ANSWER"),  # 516 -> continue
        stall_round("st_2", 0, "STALL ANSWER"),                 # 0 -> re-nudge
        reasoning_round("rs_3", 4321, "REAL ANSWER"),           # clean
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
                          "stream": True}, opener):
        out.append(ev)

    assert len(opened_bodies) == 3, f"expected 3 rounds, got {len(opened_bodies)}"

    # round 3 input carries TWO commentary nudges: one after round 1, one after
    # the stalled round 2 (round 2 contributed no reasoning item to the replay).
    b3 = opened_bodies[2]
    nudges = [i for i in b3["input"]
              if isinstance(i, dict) and i.get("phase") == "commentary"]
    assert len(nudges) == 2, [i.get("type") for i in b3["input"]]
    assert [i.get("type") for i in b3["input"]] == [
        "message", "reasoning", "message", "message"], [i.get("type") for i in b3["input"]]

    dict_events = [e for e in out if isinstance(e, dict)]
    term = dict_events[-1]
    assert term["type"] == "response.completed", term
    resp = term["response"]

    # stalled round's message discarded; only the clean round's text flushed
    deltas = [e["delta"] for e in dict_events if e["type"] == "response.output_text.delta"]
    assert deltas == ["REAL ANSWER"], deltas

    u = resp["usage"]
    assert u["output_tokens_details"]["reasoning_tokens"] == (STEP - 2) + 0 + 4321, u
    pr = resp["metadata"]["proxy_rounds"]
    assert len(pr) == 3, pr
    assert pr[1]["reasoning_tokens"] == 0, pr
    assert "proxy_stopped_reason" not in resp["metadata"], resp["metadata"]


async def test_zero_stall_exhausts():
    """Repeated zero-reasoning stalls exhaust MAX_CONTINUE and stop; the last
    round's answer is still flushed (never drop an answer)."""
    opened_bodies = []
    rounds = [
        reasoning_round("rs_1", STEP - 2, "TRUNCATED"),  # 516 -> continue
        stall_round("st_2", 0, "STALL 2"),               # 0 -> re-nudge (round 2)
        stall_round("st_3", 0, "STALL 3"),               # 0 -> re-nudge (round 3)
        stall_round("st_4", 0, "STALL 4"),               # round_no 4 > MAX_CONTINUE -> stop
    ]

    async def opener(body):
        opened_bodies.append(body)
        idx = len(opened_bodies) - 1

        async def gen():
            for ev in rounds[idx]:
                yield ev
        return gen()

    out = [ev async for ev in fold(
        {"model": "gpt-5.5", "input": [{"type": "message", "role": "user"}],
         "stream": True}, opener)]

    assert len(opened_bodies) == 4, f"expected 4 rounds, got {len(opened_bodies)}"
    term = out[-1]
    assert term["type"] == "response.completed", term
    resp = term["response"]
    assert resp["metadata"]["proxy_stopped_reason"] == "max_continue", resp["metadata"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == ["STALL 4"], deltas


async def test_round1_zero_reasoning():
    """Regression guard: round 1 with reasoning_tokens=0 is a valid complete
    answer — clean pass-through, one round, no stall retry, message flushed."""
    async def opener(body):
        async def gen():
            for ev in stall_round("rs_1", 0, "ZERO ANSWER"):
                yield ev
        return gen()

    calls = []

    async def counting_opener(body):
        calls.append(body)
        return await opener(body)

    out = [ev async for ev in fold(
        {"model": "gpt-5.5", "input": [{"type": "message", "role": "user"}],
         "stream": True}, counting_opener)]

    assert len(calls) == 1, f"round-1 zero must not retry, opened {len(calls)}"
    term = out[-1]
    assert term["type"] == "response.completed", term
    assert "proxy_stopped_reason" not in term["response"]["metadata"], term["response"]["metadata"]
    deltas = [e["delta"] for e in out
              if isinstance(e, dict) and e["type"] == "response.output_text.delta"]
    assert deltas == ["ZERO ANSWER"], deltas


async def main():
    await test_happy_fold()
    await test_compaction_passthrough()
    await test_round1_rejected()
    await test_continuation_open_fails()
    await test_upstream_eof()
    await test_zero_stall_recovers()
    await test_zero_stall_exhausts()
    await test_round1_zero_reasoning()
    print("fold self-test: ALL PASS")


asyncio.run(main())
