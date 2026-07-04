"""Self-test for the fold state machine with canned upstream rounds.

Run: uv run python test_fold.py
"""
import asyncio
import json

from codexcomp.fold import DONE, fold, STEP


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


async def main():
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

    print("fold self-test: ALL PASS")
    print("terminal usage:", json.dumps(u))


asyncio.run(main())
