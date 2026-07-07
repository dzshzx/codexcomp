"""518n-2 truncation detection + round folding for the Codex Responses event stream.

gpt-5.5 reasoning gets cut at reasoning_tokens == 518*n - 2 (openai/codex#30364).
When a round ends on that fingerprint we replay the conversation plus the round's
reasoning items and a phase:"commentary" nudge, then fold every round into ONE
downstream response: reasoning streams live, each round's tentative final output
(message / tool calls) is buffered and only the clean round's output is flushed.

Transport-agnostic: `fold()` consumes upstream events as dicts and yields
downstream events as dicts; serialization (SSE / WebSocket) lives in server.py.

Mechanism credit: neteroster/CodexCont (MIT). Implementation is original.
"""
from __future__ import annotations

import logging
import re
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

log = logging.getLogger("codexcomp.fold")

STEP = 518
MIN_N = 1          # continue only when truncation tier n >= MIN_N
MAX_N = 6          # stop forcing once n > MAX_N (0 = no cap)
MAX_CONTINUE = 3   # continuation rounds after round 1 (runaway guard)
MARKER_TEXT = "Continue thinking..."
ENC_INCLUDE = "reasoning.encrypted_content"

TERMINAL_TYPES = ("response.completed", "response.failed", "response.incomplete")

# An opener returns the upstream event iterator for one round's body.
RoundOpener = Callable[[dict[str, Any]], Awaitable[AsyncIterator[dict[str, Any]]]]


_ID_RE = re.compile(r"\b(resp|rs|msg|fc|call)_[A-Za-z0-9_-]{8,}\b")


def redact_ids(text: object) -> str:
    """Redact full upstream ids before logging/presenting proxy errors."""
    return _ID_RE.sub(lambda m: f"{m.group(1)}_{m.group(0).split('_', 1)[1][:8]}…", str(text))


class RoundOpenError(Exception):
    """Continuation round could not be opened (upstream HTTP >= 400)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"upstream {status}: {redact_ids(detail)[:200]}")
        self.status = status
        self.detail = detail


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


def next_round_body(
    base_body: dict[str, Any],
    input_items: list[Any],
) -> dict[str, Any]:
    """Shape the agent's request for one upstream round.

    Preserve previous_response_id for the client-visible round whenever Codex
    supplied it. Hidden continuation rounds may override/drop it after shaping
    the body, depending on the continuation strategy.
    """
    body = dict(base_body)
    body["stream"] = True
    body["input"] = input_items
    include = [str(x) for x in (base_body.get("include") or [])]
    if ENC_INCLUDE not in include:
        include.append(ENC_INCLUDE)
    body["include"] = include
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
    return (
        f"in={u.get('input_tokens')} out={u.get('output_tokens')} "
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
    """Downstream terminal: final upstream response identity, upstream status
    (or a synthetic incomplete), our reconstructed output + single-response
    usage, true billed cost and per-round breakdown in metadata.

    The completed response id must be the final upstream id so Codex chains the
    next turn to the clean response, not to an earlier truncated round.
    """
    tresp = (upstream_terminal or {}).get("response") or {}
    # Some upstream terminal frames omit fields that were present on
    # response.created. Start with the current round's created response, then
    # overlay terminal fields so final folded identity can still chain to the
    # correct just-produced response id.
    resp = dict(base_response or {})
    resp.update(tresp)
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


# --- the fold ----------------------------------------------------------------


async def fold(
    base_body: dict[str, Any],
    open_round: RoundOpener,
) -> AsyncIterator[dict[str, Any] | object]:
    """Yield downstream events (dicts, plus the DONE sentinel when upstream sent
    one). Every yielded event gets a proxy-owned sequence_number; output_index
    is renumbered into one downstream space across rounds."""
    orig_input = list(base_body.get("input") or [])
    seq = 0
    ds_oi = 0
    base_response: dict[str, Any] | None = None
    saw_done = False
    final_output: list[dict[str, Any]] = []
    reasoning_replay: list[Any] = []
    summed_usage: dict[str, Any] = {}
    first_usage: dict[str, Any] | None = None
    rounds_info: list[dict[str, Any]] = []
    continuation_fallback_used = False

    def stamp(ev: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        ev["sequence_number"] = seq
        seq += 1
        return ev

    round_no = 0
    round1_body = next_round_body(base_body, orig_input)
    log.info(
        "open round 1: previous_response_id=%s input_items=%d",
        "yes" if round1_body.get("previous_response_id") else "no",
        len(round1_body.get("input") or []),
    )
    events = await open_round(round1_body)

    while True:
        round_no += 1
        oi_to_ds: dict[Any, int] = {}
        kind: dict[Any, str] = {}
        buffered: list[dict[str, Any]] = []  # {oi, item, events}
        round_reasoning: list[dict[str, Any]] = []
        terminal: dict[str, Any] | None = None
        usage: dict[str, Any] | None = None
        round_response: dict[str, Any] | None = None

        try:
            async for ev in events:
                if ev is DONE:
                    saw_done = True
                    continue
                etype = ev.get("type", "")

                if etype in ("response.created", "response.in_progress"):
                    resp = ev.get("response") or {}
                    if resp.get("id"):
                        round_response = resp
                    if round_no == 1:
                        if etype == "response.created":
                            base_response = resp
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
        except RoundOpenError:
            raise  # only raised before any event; handled by caller for round 1
        except (httpx.HTTPError, ConnectionError, TimeoutError, OSError) as exc:
            log.warning("round %d: upstream error mid-stream: %s", round_no, redact_ids(repr(exc)))
            # If the primary hidden continuation failed because the truncated
            # response id was not available upstream, retry once with a full
            # no-previous-response replay only when the client request itself was
            # already full-context. For incremental Codex turns, silently dropping
            # previous_response_id would lose context; return incomplete and let
            # Codex perform its own full replay.
            if (
                round_no > 1
                and not continuation_fallback_used
                and not base_body.get("previous_response_id")
                and "previous_response_not_found" in str(exc)
            ):
                continuation_fallback_used = True
                try:
                    fallback_body = next_round_body(
                        base_body,
                        orig_input + reasoning_replay + [commentary_nudge()],
                    )
                    fallback_body.pop("previous_response_id", None)
                    log.info(
                        "open continuation fallback round %d: previous_response_id=no input_items=%d replay_items=%d",
                        round_no + 1,
                        len(fallback_body.get("input") or []),
                        len(reasoning_replay),
                    )
                    events = await open_round(fallback_body)
                    continue
                except RoundOpenError as fallback_exc:
                    log.warning("continuation fallback round %d failed to open: %s", round_no + 1, fallback_exc)
                except Exception as fallback_exc:
                    log.warning(
                        "continuation fallback round %d failed to open: %s",
                        round_no + 1,
                        redact_ids(repr(fallback_exc)),
                    )
            _sum_usage(summed_usage, usage)
            yield stamp(_terminal_event(
                None, base_response, final_output,
                agent_usage(first_usage, summed_usage, usage, flushed_final=False),
                rounds_info, summed_usage, "upstream_error",
                incomplete_reason="upstream_error"))
            return
        except Exception:
            log.exception("round %d: fold state-machine bug", round_no)
            raise

        # ---- round ended: decide continue / stop ----------------------------
        # We intentionally stop reading a round as soon as its terminal event is
        # seen.  For async generators (notably the sticky upstream WebSocket
        # opener), breaking out of `async for` does not necessarily advance the
        # generator to its `finally` block immediately.  Close it explicitly so
        # per-round resources/locks are released before a hidden continuation
        # opens the next round on the same sticky upstream WebSocket.
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                log.warning("round %d: failed to close upstream event iterator", round_no, exc_info=True)

        _sum_usage(summed_usage, usage)
        if round_no == 1:
            first_usage = usage
        rt = reasoning_tokens(usage)
        n = tier_n(rt)
        rounds_info.append({"round": round_no, "reasoning_tokens": rt, "n": n})
        has_enc = bool(round_reasoning and round_reasoning[-1].get("encrypted_content"))

        do_continue = (
            terminal is not None
            and terminal.get("type") == "response.completed"
            and ((terminal.get("response") or {}).get("status") in (None, "completed"))
            and in_continue_window(n)
            and has_enc
            and round_no <= MAX_CONTINUE
        )
        stopped_reason = None
        if not do_continue and n is not None:
            stopped_reason = (
                "no_encrypted_content" if not has_enc
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
            # Replayed reasoning reconstructs hidden continuation state. Keep a
            # single trailing nudge rather than accumulating previous nudges.
            reasoning_replay.extend(round_reasoning)
            try:
                terminal_response_id = (terminal.get("response") or {}).get("id")
                created_response_id = (round_response or {}).get("id")
                truncated_response_id = terminal_response_id or created_response_id
                # Do not reuse Codex's original previous_response_id for a hidden
                # continuation. When the just-truncated upstream response id is
                # available, follow Codex's delta rule: previous_response_id points
                # at the prior response and input contains only the new delta. The
                # truncated response already contains the original input and its
                # reasoning output server-side, so resending orig_input/reasoning
                # can duplicate a function_call_output or user message.
                if truncated_response_id:
                    cont_body = next_round_body(base_body, [commentary_nudge()])
                    cont_body["previous_response_id"] = truncated_response_id
                else:
                    if base_body.get("previous_response_id"):
                        log.warning(
                            "round %d: no current response id for safe hidden continuation; "
                            "returning incomplete rather than reusing original previous_response_id",
                            round_no,
                        )
                        yield stamp(_terminal_event(
                            None, round_response or base_response, final_output,
                            agent_usage(first_usage, summed_usage, usage, flushed_final=False),
                            rounds_info, summed_usage, "upstream_error",
                            incomplete_reason="upstream_error"))
                        return
                    cont_body = next_round_body(
                        base_body,
                        orig_input + reasoning_replay + [commentary_nudge()],
                    )
                    cont_body.pop("previous_response_id", None)
                log.info(
                    "open continuation round %d: previous_response_id=%s id_source=%s id_prefix=%s input_items=%d replay_items=%d",
                    round_no + 1,
                    "yes" if cont_body.get("previous_response_id") else "no",
                    "terminal" if terminal_response_id else "created" if created_response_id else "none",
                    redact_ids(cont_body.get("previous_response_id")) if cont_body.get("previous_response_id") else None,
                    len(cont_body.get("input") or []),
                    len(reasoning_replay),
                )
                events = await open_round(cont_body)
            except RoundOpenError as exc:
                log.warning("continuation round %d failed to open: %s", round_no + 1, exc)
                yield stamp(_terminal_event(
                    None, base_response, final_output,
                    agent_usage(first_usage, summed_usage, usage, flushed_final=False),
                    rounds_info, summed_usage, "upstream_error",
                    incomplete_reason="upstream_error"))
                return
            continue

        if terminal is None:  # EOF with no terminal: tentative output is NOT an answer
            log.warning("round %d: upstream EOF with no terminal event", round_no)
            yield stamp(_terminal_event(
                None, base_response, final_output,
                agent_usage(first_usage, summed_usage, usage, flushed_final=False),
                rounds_info, summed_usage, "upstream_eof",
                incomplete_reason="upstream_eof"))
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
            terminal, round_response or base_response, final_output,
            agent_usage(first_usage, summed_usage, usage, flushed_final=True),
            rounds_info, summed_usage, stopped_reason))
        if saw_done:
            yield DONE
        return
