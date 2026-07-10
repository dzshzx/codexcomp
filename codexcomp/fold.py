"""518n-2 truncation detection + round folding for the Codex Responses event stream.

gpt-5.5 reasoning gets cut at reasoning_tokens == 518*n - 2 (openai/codex#30364).
When a round ends on that fingerprint we replay the conversation plus the round's
reasoning items and a phase:"commentary" nudge, then fold every round into ONE
downstream response: reasoning streams live, each round's tentative final output
(message / tool calls) is buffered and only the clean round's output is flushed.
A continuation round that returns zero reasoning tokens is treated as a stalled
nudge and re-nudged within the same MAX_CONTINUE budget.

Transport-agnostic: `fold()` consumes upstream events as dicts and yields
downstream events as dicts; serialization (SSE / WebSocket) lives in server.py.

Mechanism credit: neteroster/CodexCont (MIT). Implementation is original.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger("codexcomp.fold")

STEP = 518
MIN_N = 1          # continue only when truncation tier n >= MIN_N
MAX_N = 0          # highest tier to force-continue; 0 = no cap. Cuts up to n=21
                   # observed in the wild, and a natural stop landing exactly on
                   # 518n-2 is ~1/518 at any tier, so a cap buys almost no
                   # false-positive safety while releasing real truncations.
MAX_CONTINUE = 3   # continuation rounds after round 1 (runaway guard)
MARKER_TEXT = "Continue thinking..."
ENC_INCLUDE = "reasoning.encrypted_content"

TERMINAL_TYPES = ("response.completed", "response.failed", "response.incomplete")

# An opener returns the upstream event iterator for one round's body.
RoundOpener = Callable[[dict[str, Any]], Awaitable[AsyncIterator[dict[str, Any]]]]


class RoundOpenError(Exception):
    """A round could not be opened (upstream HTTP >= 400 or transport error). Raised by the
    opener; always handled inside fold(), never escapes to the transport."""

    def __init__(self, status: int, detail: str, *, code: str | None = None):
        super().__init__(f"upstream {status}: {detail[:200]}")
        self.status = status
        self.code = code or f"upstream_{status}"


DONE = object()  # sentinel an opener may yield to signal upstream sent [DONE]


# --- fingerprint -------------------------------------------------------------


def reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    val = ((usage or {}).get("output_tokens_details") or {}).get("reasoning_tokens")
    return int(val) if val is not None else None


def tier_n(tokens: int | None) -> int | None:
    """n for reasoning_tokens == STEP*n - 2 (516, 1034, ...), else None."""
    if tokens is None or tokens < STEP - 2 or (tokens + 2) % STEP != 0:
        return None
    return (tokens + 2) // STEP


def in_continue_window(n: int | None) -> bool:
    return n is not None and n >= MIN_N and (MAX_N == 0 or n <= MAX_N)


# --- continuation payload ----------------------------------------------------


def commentary_nudge() -> dict[str, Any]:
    """phase:"commentary" assistant message that provokes the model to resume
    reasoning when replayed together with the encrypted reasoning items."""
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": MARKER_TEXT}],
        "phase": "commentary",
    }


def next_round_body(base_body: dict[str, Any], input_items: list[Any]) -> dict[str, Any]:
    """The agent's request re-shaped for a continuation round: explicit input,
    always streamed, encrypted reasoning included, no previous_response_id
    (state is carried in the replayed items)."""
    body = dict(base_body)
    body["stream"] = True
    body["input"] = input_items
    include = [str(x) for x in (base_body.get("include") or [])]
    if ENC_INCLUDE not in include:
        include.append(ENC_INCLUDE)
    body["include"] = include
    body.pop("previous_response_id", None)
    return body


# --- usage accounting --------------------------------------------------------


def _sum_usage(acc: dict[str, Any], usage: dict[str, Any] | None) -> None:
    if not usage:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if usage.get(key) is not None:
            acc[key] = acc.get(key, 0) + int(usage[key])
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        acc.setdefault("input_tokens_details", {})
        acc["input_tokens_details"]["cached_tokens"] = (
            acc["input_tokens_details"].get("cached_tokens", 0) + int(cached)
        )
    rt = reasoning_tokens(usage)
    if rt is not None:
        acc.setdefault("output_tokens_details", {})
        acc["output_tokens_details"]["reasoning_tokens"] = (
            acc["output_tokens_details"].get("reasoning_tokens", 0) + rt
        )


def agent_usage(
    first: dict[str, Any] | None,
    summed: dict[str, Any],
    final_round: dict[str, Any] | None,
    flushed_final: bool,
) -> dict[str, Any]:
    """Usage as if the fold were one response. input/cached come from round 1
    (summing hidden rounds would fake a blown context window); reasoning is
    summed because every round's reasoning reached the agent; output adds only
    the flushed final round's non-reasoning part."""
    first = first or {}
    in_tok = first.get("input_tokens") or 0
    cached = (first.get("input_tokens_details") or {}).get("cached_tokens")
    reason = (summed.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
    final_part = 0
    if flushed_final and final_round:
        out = final_round.get("output_tokens") or 0
        final_part = max(0, out - (reasoning_tokens(final_round) or 0))
    usage: dict[str, Any] = {
        "input_tokens": in_tok,
        "output_tokens": reason + final_part,
        "total_tokens": in_tok + reason + final_part,
        "output_tokens_details": {"reasoning_tokens": reason},
    }
    if cached is not None:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    return usage


def _fmt(usage: dict[str, Any] | None) -> str:
    u = usage or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens")
    cached_part = f"cached={cached} " if cached is not None else ""
    return (
        f"in={u.get('input_tokens')} {cached_part}out={u.get('output_tokens')} "
        f"reason={reasoning_tokens(u)} total={u.get('total_tokens')}"
    )


# --- terminal reconstruction ---------------------------------------------------


def _terminal_event(
    upstream_terminal: dict[str, Any] | None,
    base_response: dict[str, Any] | None,
    output: list[dict[str, Any]],
    usage: dict[str, Any],
    rounds: list[dict[str, Any]],
    billed: dict[str, Any],
    stopped_reason: str | None,
    *,
    incomplete_reason: str | None = None,
) -> dict[str, Any]:
    """Downstream terminal: round-1 response identity, upstream status (or a
    synthetic incomplete), our reconstructed output + single-response usage,
    true billed cost and per-round breakdown in metadata."""
    tresp = (upstream_terminal or {}).get("response") or {}
    resp = dict(base_response or tresp)
    resp["output"] = output
    resp["usage"] = usage
    metadata = dict(resp.get("metadata") or {})
    metadata["proxy_rounds"] = rounds
    metadata["proxy_billed_usage"] = billed
    if stopped_reason:
        metadata["proxy_stopped_reason"] = stopped_reason
    resp["metadata"] = metadata
    if incomplete_reason is not None:
        resp["status"] = "incomplete"
        resp["incomplete_details"] = {"reason": incomplete_reason}
        return {"type": "response.incomplete", "response": resp}
    resp["status"] = tresp.get("status", "completed")
    if "incomplete_details" in tresp:
        resp["incomplete_details"] = tresp["incomplete_details"]
    return {"type": (upstream_terminal or {}).get("type", "response.completed"), "response": resp}


def _failed_event(exc: RoundOpenError) -> dict[str, Any]:
    """Downstream terminal for a request upstream rejected outright (round 1)."""
    return {
        "type": "response.failed",
        "response": {"status": "failed",
                     "error": {"message": str(exc), "code": exc.code}},
    }


# --- the fold ----------------------------------------------------------------


async def fold(
    base_body: dict[str, Any],
    open_round: RoundOpener,
) -> AsyncIterator[dict[str, Any] | object]:
    """Yield downstream events (dicts, plus the DONE sentinel when upstream sent
    one). Every yielded event gets a proxy-owned sequence_number; output_index
    is renumbered into one downstream space across rounds.

    Sole owner of downstream terminal shapes: upstream failures surface as
    terminal events (response.failed for a rejected round 1, response.incomplete
    otherwise) — RoundOpenError never escapes to the transport.

    A fold torn down from downstream (client disconnect / task cancel) delivers
    no terminal; it is logged as `fold aborted downstream` so it never vanishes
    from the journal without a trace."""
    stats: dict[str, Any] = {"rounds": 0, "usage": {}}
    inner = _fold_inner(base_body, open_round, stats)
    try:
        async for ev in inner:
            yield ev
    except (GeneratorExit, asyncio.CancelledError):
        log.warning("fold aborted downstream after %d round(s) | %s",
                    stats["rounds"], _fmt(stats["usage"]))
        raise
    finally:
        await inner.aclose()


async def _fold_inner(
    base_body: dict[str, Any],
    open_round: RoundOpener,
    stats: dict[str, Any],
) -> AsyncIterator[dict[str, Any] | object]:
    """The fold state machine; `stats` mirrors round count / summed usage for
    the abort log in fold()."""
    orig_input = list(base_body.get("input") or [])
    # A remote-compaction request (input ends with a compaction_trigger item) is
    # a positional control frame: Codex requires the trigger to stay last and
    # exactly one compaction output item back. Replaying it with appended
    # reasoning would break both, so it always passes through as one round.
    is_compaction = bool(
        orig_input
        and isinstance(orig_input[-1], dict)
        and orig_input[-1].get("type") == "compaction_trigger"
    )
    seq = 0
    ds_oi = 0
    base_response: dict[str, Any] | None = None
    saw_done = False
    final_output: list[dict[str, Any]] = []
    replay_tail: list[Any] = []
    summed_usage: dict[str, Any] = {}
    first_usage: dict[str, Any] | None = None
    rounds_info: list[dict[str, Any]] = []

    def stamp(ev: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        ev["sequence_number"] = seq
        seq += 1
        return ev

    def incomplete(reason: str) -> dict[str, Any]:
        """Synthesized degraded-stop terminal — never fabricates a completed answer."""
        return stamp(_terminal_event(
            None, base_response, final_output,
            agent_usage(first_usage, summed_usage, usage, flushed_final=False),
            rounds_info, summed_usage, reason,
            incomplete_reason=reason))

    round_no = 0
    usage: dict[str, Any] | None = None
    try:
        events = await open_round(next_round_body(base_body, orig_input))
    except RoundOpenError as exc:
        log.warning("round 1 rejected by upstream: %s", exc)
        yield stamp(_failed_event(exc))
        return

    while True:
        round_no += 1
        oi_to_ds: dict[Any, int] = {}
        kind: dict[Any, str] = {}
        buffered: list[dict[str, Any]] = []  # {oi, item, events}
        round_reasoning: list[dict[str, Any]] = []
        terminal: dict[str, Any] | None = None
        usage = None

        while True:
            # Only the upstream pull is guarded: an exception raised by the
            # event-processing below is a proxy bug and must surface loudly,
            # not be disguised as an upstream_error incomplete.
            try:
                ev = await events.__anext__()
            except StopAsyncIteration:
                break
            except Exception as exc:  # upstream died mid-stream
                log.warning("round %d: upstream error mid-stream: %r", round_no, exc)
                _sum_usage(summed_usage, usage)
                yield incomplete("upstream_error")
                return

            if ev is DONE:
                saw_done = True
                continue
            etype = ev.get("type", "")

            if etype in ("response.created", "response.in_progress"):
                if round_no == 1:
                    if etype == "response.created":
                        base_response = ev.get("response") or {}
                    yield stamp(ev)
                continue
            if etype in TERMINAL_TYPES:
                terminal = ev
                usage = (ev.get("response") or {}).get("usage")
                break

            oi = ev.get("output_index")
            if etype == "response.output_item.added":
                item = ev.get("item") or {}
                if item.get("type") == "reasoning":
                    kind[oi] = "reasoning"
                    oi_to_ds[oi] = ds_oi
                    ev["output_index"] = ds_oi
                    ds_oi += 1
                    yield stamp(ev)
                else:
                    kind[oi] = "buffered"
                    buffered.append({"oi": oi, "item": item, "events": [ev]})
                continue

            k = kind.get(oi)
            if k == "reasoning":
                if oi in oi_to_ds:
                    ev["output_index"] = oi_to_ds[oi]
                if etype == "response.output_item.done":
                    item = ev.get("item") or {}
                    round_reasoning.append(item)
                    final_output.append(item)
                yield stamp(ev)
            elif k == "buffered":
                entry = next(e for e in buffered if e["oi"] == oi)
                entry["events"].append(ev)
                if etype == "response.output_item.done":
                    entry["item"] = ev.get("item") or entry["item"]
            else:
                yield stamp(ev)  # unknown scope: forward best-effort

        # ---- round ended: decide continue / stop ----------------------------
        _sum_usage(summed_usage, usage)
        if round_no == 1:
            first_usage = usage
        rt = reasoning_tokens(usage)
        n = tier_n(rt)
        rounds_info.append({"round": round_no, "reasoning_tokens": rt, "n": n})
        stats["rounds"], stats["usage"] = round_no, summed_usage
        has_enc = bool(round_reasoning and round_reasoning[-1].get("encrypted_content"))
        # A continuation round is one we explicitly nudged after a confirmed
        # truncation; zero reasoning there means the nudge failed and the
        # collapse continues, so re-nudge. Round 1's zero reasoning is a valid
        # complete answer and is NOT in scope (strict ==0; rt is None — missing
        # usage or a failed terminal — must not enter the retry loop).
        zero_stall = round_no >= 2 and rt == 0

        do_continue = (
            terminal is not None
            and not is_compaction
            and round_no <= MAX_CONTINUE
            and ((in_continue_window(n) and has_enc) or zero_stall)
        )
        stopped_reason = None
        if not do_continue and (n is not None or zero_stall):
            stopped_reason = (
                "compaction_request" if is_compaction
                else "no_encrypted_content" if n is not None and not has_enc
                else "max_continue" if round_no > MAX_CONTINUE
                else "tier_out_of_window"
            )

        log.info(
            "round %d: %s | n=%s buffered=%s -> %s",
            round_no, _fmt(usage), n,
            [e["item"].get("type") for e in buffered],
            "continue" if do_continue else
            "upstream_eof" if terminal is None else stopped_reason or "clean",
        )

        if do_continue:
            replay_tail.extend([*round_reasoning, commentary_nudge()])
            try:
                events = await open_round(next_round_body(base_body, orig_input + replay_tail))
            except RoundOpenError as exc:
                log.warning("continuation round %d failed to open: %s", round_no + 1, exc)
                yield incomplete("upstream_error")
                return
            continue

        if terminal is None:  # EOF with no terminal: tentative output is NOT an answer
            log.warning("round %d: upstream EOF with no terminal event", round_no)
            yield incomplete("upstream_eof")
            return

        # Clean stop: flush this round's buffered output as the real answer.
        for entry in buffered:
            for ev in entry["events"]:
                if "output_index" in ev:
                    ev["output_index"] = ds_oi
                yield stamp(ev)
            ds_oi += 1
            final_output.append(entry["item"])

        status = (terminal.get("response") or {}).get("status", "completed")
        log.info("done: %d round(s) | %s | status=%s stop=%s",
                 round_no, _fmt(summed_usage), status, stopped_reason or "natural")
        yield stamp(_terminal_event(
            terminal, base_response, final_output,
            agent_usage(first_usage, summed_usage, usage, flushed_final=True),
            rounds_info, summed_usage, stopped_reason))
        if saw_done:
            yield DONE
        return
