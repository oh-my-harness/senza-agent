"""Confidence metric — per-token logprob entropy signal.

Principle: the flatter the model's distribution over the next token (the more
"uncertain" it is), the lower the logprob of the sampled token.  By aggregating
the mean negative log-likelihood (perplexity) and the fraction of low-confidence
tokens over a span of free-form output, we obtain an objective confidence signal
that cannot be gamed by prompting.

Senza SDK note: the SDK does not yet expose per-token logprobs from providers.
Until it does, :func:`compute_confidence` returns ``None``.  The full
implementation (thought-span alignment, UTF-8 byte-level matching, perplexity)
is preserved below so it activates automatically once logprobs become available
— callers already guard with ``if metrics is not None``.
"""
from __future__ import annotations

import math
import re
from typing import Optional

# A token with probability below 0.5 is "low confidence" (logprob < ln 0.5).
LOW_CONF_LOGPROB = math.log(0.5)

# If fewer than this many tokens align to the thought span, fall back to
# full-token statistics — the sample is too small to be meaningful.
MIN_SPAN_TOKENS = 5


def _find_thought_span(raw: str) -> Optional[tuple[int, int]]:
    """Locate the character interval ``[start, end)`` of the ``"thought"`` field's
    string value inside *raw*.

    Hand-rolled scan rather than ``json.loads``: *raw* is frequently truncated or
    malformed JSON, and precisely those rounds carry the most interesting
    confidence signal — we must not drop them on parse failure.
    """
    m = re.search(r'"thought"\s*:\s*"', raw)
    if not m:
        return None
    start = m.end()
    i = start
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return (start, i)
        i += 1
    # Unclosed (output truncated): take to end.
    return (start, n)


def compute_confidence(raw, token_logprobs: Optional[list] = None) -> Optional[dict]:
    """Compute confidence metrics from an LLM response.

    Parameters
    ----------
    raw:
        Either the raw LLM output string, or a Senza response/harness object.
        If it is not a string, we attempt to extract ``last_response()`` or
        ``text``; if none is available, *raw* is treated as empty.
    token_logprobs:
        List of ``(token_bytes_or_str, logprob)`` pairs from the provider.
        If ``None`` (the default when Senza does not expose logprobs), the
        function returns ``None``.

    Returns
    -------
    dict or None
        ``{mean_lp, perplexity, low_conf_ratio, n_tok, span}`` or ``None`` if
        logprobs are unavailable.
    """
    # ── Senza SDK does not yet expose logprobs ─────────────────────────────
    # When token_logprobs is None there is nothing to compute; return None so
    # callers (loop / advisor) can skip confidence-based gating.
    if token_logprobs is None:
        return None

    # Coerce *raw* to a string if a Senza object was passed.
    if not isinstance(raw, str):
        if raw is None:
            return None
        # Try common Senza accessors.
        for attr in ("last_response", "text", "content"):
            val = getattr(raw, attr, None)
            if callable(val):
                try:
                    val = val()
                except Exception:
                    continue
            if isinstance(val, str):
                raw = val
                break
        if not isinstance(raw, str):
            return None

    if not token_logprobs or not isinstance(raw, str):
        return None

    pairs: list[tuple[bytes, float]] = []
    for item in token_logprobs:
        try:
            tok, lp = item[0], float(item[1])
        except Exception:
            continue
        if isinstance(tok, str):
            tok = tok.encode("utf-8", errors="replace")
        elif not isinstance(tok, (bytes, bytearray)):
            continue
        pairs.append((bytes(tok), lp))
    if not pairs:
        return None

    selected: Optional[list[float]] = None
    span_kind = "all"

    char_span = _find_thought_span(raw)
    if char_span is not None:
        raw_b = raw.encode("utf-8")
        b_start = len(raw[: char_span[0]].encode("utf-8"))
        b_end = len(raw[: char_span[1]].encode("utf-8"))
        pos = 0
        picked: list[float] = []
        aligned = True
        for tok, lp in pairs:
            t_start, t_end = pos, pos + len(tok)
            if raw_b[t_start:t_end] != tok:
                aligned = False
                break
            if t_end > b_start and t_start < b_end:
                picked.append(lp)
            pos = t_end
            if pos >= b_end:
                break
        if aligned and len(picked) >= MIN_SPAN_TOKENS:
            selected = picked
            span_kind = "thought"

    if selected is None:
        selected = [lp for _, lp in pairs]

    n = len(selected)
    mean_lp = sum(selected) / n
    low = sum(1 for lp in selected if lp < LOW_CONF_LOGPROB) / n
    try:
        ppl = math.exp(-mean_lp)
    except OverflowError:
        ppl = float("inf")

    return {
        "mean_lp": round(mean_lp, 4),
        "perplexity": round(ppl, 3),
        "low_conf_ratio": round(low, 4),
        "n_tok": n,
        "span": span_kind,
    }
