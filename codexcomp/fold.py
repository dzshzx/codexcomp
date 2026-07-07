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

import hashlib
import logging
import os
import re
import secrets
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

log = logging.getLogger("codexcomp.fold")

STEP = 518
MIN_N = 1          # continue only when truncation tier n >= MIN_N
MAX_N = 6          # stop forcing once n > MAX_N (0 = no cap)
MAX_CONTINUE = 3   # continuation rounds after round 1 (runaway guard)
MARKER_TEXT = "Continue thinking..."
ENC_INCLUDE = "reasoning.encrypted_content"
ZERO_RETRY_MODELS: set[str] = set()
ZERO_RETRY_EFFORTS: set[str] = set()
X_CODEX_TURN_STATE = "x-codex-turn-state"

TERMINAL_TYPES = ("response.completed", "response.failed", "response.incomplete")

# An opener returns the upstream event iterator for one round's body.
RoundOpener = Callable[[dict[str, Any]], Awaitable[AsyncIterator[dict[str, Any]]]]


_ID_RE = re.compile(r"\b(resp|rs|msg|fc|call)_[A-Za-z0-9_-]{8,}\b")
_LOG_HASH_KEY = secrets.token_bytes(16)


def redact_ids(text: object) -> str:
    """Redact full upstream ids before logging/presenting proxy errors."""
    return _ID_RE.sub(lambda m: f"{m.group(1)}_{m.group(0).split('_', 1)[1][:8]}…", str(text))


def _content_sig(text: str) -> str | None:
    """Process-local digest for comparing hidden outputs without logging text.

    The key is generated on process start, so the digest is useful for comparing
    nearby rounds in one service run but is not stable across restarts/log files.
    """
    if not text:
        return None
    h = hashlib.blake2s(key=_LOG_HASH_KEY, digest_size=8)
    h.update(text.encode("utf-8", errors="replace"))
    return h.hexdigest()


def _message_text_for_log(item: dict[str, Any]) -> str:
    fragments: list[str] = []
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("output_text", "input_text") and isinstance(part.get("text"), str):
                fragments.append(part["text"])
    elif isinstance(content, str):
        fragments.append(content)
    return "".join(fragments)


def _debug_text_enabled() -> bool:
    return str(os.getenv("CODEXCOMP_DEBUG_TEXT") or "").strip().lower() in {
        "1", "true", "yes", "on", "full",
    }


def _buffered_log_summary(buffered: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Summarize buffered output.

    By default this avoids prompt/answer text and logs only type, character
    counts, and process-local hashes. If CODEXCOMP_DEBUG_TEXT=1 is explicitly
    set, include full buffered assistant message text for diagnosing hidden
    continuation rounds. Request headers/auth are still never logged here.
    """
    summary: list[dict[str, Any]] = []
    sig_parts: list[str] = []
    for entry in buffered:
        item = entry.get("item") or {}
        typ = item.get("type")
        row: dict[str, Any] = {"type": typ}
        if typ == "message":
            text = _message_text_for_log(item)
            sig = _content_sig(text)
            row["chars"] = len(text)
            row["sig"] = sig
            if _debug_text_enabled():
                row["text"] = redact_ids(text)
            if sig is not None:
                sig_parts.append(sig)
        summary.append(row)
    return summary, "+".join(sig_parts) or None


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


def requested_effort(body: dict[str, Any]) -> str:
    """Best-effort extraction of the requested reasoning effort.

    Codex has used several nearby shapes over time; normalize common spelling
    variants so this can stay narrowly gated without depending on one exact
    transport version.
    """

    def normalize(value: Any) -> str:
        return str(value).strip().lower().replace("-", "_").replace(" ", "_")

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort:
            return normalize(effort)
    for key in ("model_reasoning_effort", "reasoning_effort", "effort"):
        effort = body.get(key)
        if effort:
            return normalize(effort)
    return ""


def should_retry_zero_reasoning(body: dict[str, Any], tokens: int | None) -> bool:
    """Zero-reasoning retry is intentionally disabled.

    Live Codex CLI testing showed high-effort gpt-5.5 can return
    reasoning_tokens == 0 with a good, complete direct answer. Retrying those
    rounds hides the good answer and often degrades subsequent attempts. Keep
    continuation automatic only for the stronger 518n-2 truncation fingerprint.
    """
    return False


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
    last_turn_state: str | None = None
    last_hidden_output_sig: str | None = None
    summed_usage: dict[str, Any] = {}
    first_usage: dict[str, Any] | None = None
    rounds_info: list[dict[str, Any]] = []
    continuation_fallback_used = False

    def stamp(ev: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        ev["sequence_number"] = seq
        seq += 1
        return ev

    def add_turn_state(body: dict[str, Any]) -> None:
        if not last_turn_state:
            return
        client_metadata = body.get("client_metadata")
        if not isinstance(client_metadata, dict):
            client_metadata = {}
        else:
            client_metadata = dict(client_metadata)
        client_metadata.setdefault(X_CODEX_TURN_STATE, last_turn_state)
        body["client_metadata"] = client_metadata

    def can_try_continuation_fallback(failed_round_no: int, exc: BaseException) -> bool:
        detail = getattr(exc, "detail", "")
        text = f"{detail} {exc}"
        low = text.lower()
        return (
            failed_round_no > 1
            and not continuation_fallback_used
            and not base_body.get("previous_response_id")
            and (
                "previous_response_not_found" in low
                or ("previous_response_id" in low and "unsupported" in low)
            )
        )

    async def open_continuation_fallback(
        failed_round_no: int,
        fallback_round_no: int,
        exc: BaseException,
    ) -> AsyncIterator[dict[str, Any]] | None:
        nonlocal continuation_fallback_used
        # If the primary hidden continuation failed because the truncated
        # response id was not available/supported upstream, retry once with a
        # full no-previous-response replay only when the client request itself
        # was already full-context. For incremental Codex turns, silently
        # dropping previous_response_id would lose context; return incomplete and
        # let Codex perform its own full replay.
        if not can_try_continuation_fallback(failed_round_no, exc):
            return None
        continuation_fallback_used = True
        try:
            fallback_body = next_round_body(
                base_body,
                orig_input + reasoning_replay + [commentary_nudge()],
            )
            fallback_body.pop("previous_response_id", None)
            add_turn_state(fallback_body)
            log.info(
                "open continuation fallback round %d: previous_response_id=no input_items=%d replay_items=%d",
                fallback_round_no,
                len(fallback_body.get("input") or []),
                len(reasoning_replay),
            )
            return await open_round(fallback_body)
        except RoundOpenError as fallback_exc:
            log.warning(
                "continuation fallback round %d failed to open: %s",
                fallback_round_no,
                fallback_exc,
            )
        except Exception as fallback_exc:
            log.warning(
                "continuation fallback round %d failed to open: %s",
                fallback_round_no,
                redact_ids(repr(fallback_exc)),
            )
        return None

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

                if etype == "response.metadata":
                    headers = ev.get("headers")
                    turn_state = headers.get(X_CODEX_TURN_STATE) if isinstance(headers, dict) else None
                    if isinstance(turn_state, str) and turn_state:
                        last_turn_state = turn_state

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
        except RoundOpenError as exc:
            fallback_events = await open_continuation_fallback(round_no, round_no + 1, exc)
            if fallback_events is not None:
                events = fallback_events
                continue
            if round_no == 1:
                raise  # round 1 rejected: handled by transport caller
            log.warning("round %d: upstream error before response events: %s", round_no, exc)
            _sum_usage(summed_usage, usage)
            yield stamp(_terminal_event(
                None, round_response or base_response, final_output,
                agent_usage(first_usage, summed_usage, usage, flushed_final=False),
                rounds_info, summed_usage, "upstream_error",
                incomplete_reason="upstream_error"))
            return
        except (httpx.HTTPError, ConnectionError, TimeoutError, OSError) as exc:
            log.warning("round %d: upstream error mid-stream: %s", round_no, redact_ids(repr(exc)))
            fallback_events = await open_continuation_fallback(round_no, round_no + 1, exc)
            if fallback_events is not None:
                events = fallback_events
                continue
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
        buffered_types = [e["item"].get("type") for e in buffered]
        buffered_log, buffered_sig = _buffered_log_summary(buffered)
        same_hidden_output = bool(buffered_sig and buffered_sig == last_hidden_output_sig)
        zero_retry = (
            should_retry_zero_reasoning(base_body, rt)
            # Do not retry Codex prewarm/generate probes or empty-output rounds.
            and bool(orig_input)
            and bool(buffered_types)
            # Retrying a zero-reasoning round that emitted a tool/function call
            # can create an unresolved call with no matching tool output. Keep
            # the zero-retry path message-only; the 518n-2 path remains governed
            # by encrypted reasoning instead.
            and all(t == "message" for t in buffered_types)
        )
        rounds_info.append({
            "round": round_no,
            "reasoning_tokens": rt,
            "n": n,
            "zero_reasoning_retry": zero_retry,
        })
        has_enc = bool(round_reasoning and round_reasoning[-1].get("encrypted_content"))

        terminal_completed = (
            terminal is not None
            and terminal.get("type") == "response.completed"
            and ((terminal.get("response") or {}).get("status") in (None, "completed"))
        )
        do_continue = (
            terminal_completed
            and ((in_continue_window(n) and has_enc) or zero_retry)
            and round_no <= MAX_CONTINUE
        )
        stopped_reason = None
        if terminal_completed and not do_continue and zero_retry:
            stopped_reason = "zero_reasoning_max_continue"
        elif not do_continue and n is not None:
            stopped_reason = (
                "no_encrypted_content" if not has_enc
                else "max_continue" if round_no > MAX_CONTINUE
                else "tier_out_of_window"
            )

        log.info(
            "round %d: %s | n=%s zero_retry=%s buffered=%s buffered_log=%s same_hidden_output=%s -> %s",
            round_no, _fmt(usage), n, zero_retry,
            buffered_types,
            buffered_log,
            same_hidden_output,
            "continue" if do_continue else
            "upstream_eof" if terminal is None else stopped_reason or "clean",
        )

        if do_continue:
            last_hidden_output_sig = buffered_sig
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
                add_turn_state(cont_body)
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
                fallback_events = await open_continuation_fallback(round_no + 1, round_no + 1, exc)
                if fallback_events is not None:
                    events = fallback_events
                    continue
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
