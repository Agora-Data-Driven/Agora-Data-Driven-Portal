"""Optional AI enrichment of portal feedback (transcription + summarisation).

This module is DELIBERATELY a graceful no-op unless explicitly configured. The portal must deploy
and run with no AI wired at all: feedback is always stored verbatim by feedback.py, and these
functions simply return None when the enrichment is not enabled. That keeps the LLM strictly
optional -- there is NO hard dependency on the `anthropic` package, so an unconfigured deploy
cannot break.

Enable enrichment by setting BOTH:
  * FEEDBACK_AI_ENABLED=1            -- the on/off guard
  * ANTHROPIC_API_KEY=<key>         -- the Anthropic API key (read by the SDK from the env)
When wired, the LLM calls use the Anthropic Claude API with model id "claude-opus-4-8".

Each function returns the enrichment string on success, or None when disabled / on any error
(so a failed enrichment never blocks storing the raw feedback).
"""

import os

# Model id for any Claude call made here (only used when enrichment is enabled + configured).
CLAUDE_MODEL = "claude-opus-4-8"


def _enabled():
    """True iff enrichment is switched on AND an API key is present. Fail-closed otherwise."""
    if os.environ.get("FEEDBACK_AI_ENABLED", "") not in ("1", "true", "True"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))


def _client():
    """Return an Anthropic client, or None if the SDK is unavailable / not configured.

    Imported lazily so the portal has NO hard dependency on `anthropic`: if the package is not
    installed (it is intentionally not in requirements.txt), this returns None and the caller
    falls back to storing the raw feedback unenriched.
    """
    if not _enabled():
        return None
    try:
        import anthropic  # optional dependency; absent on a default deploy
        return anthropic.Anthropic()
    except Exception:
        return None


def summarize_text(message):
    """Summarise/interpret a text feedback message. Returns a short summary string, or None.

    No-op (returns None) unless enrichment is enabled, configured, and the SDK is installed.
    """
    client = _client()
    if client is None or not (message or "").strip():
        return None
    try:
        # TODO: tune the prompt/length to the CRM's needs once feedback volume is understood.
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=(
                "You triage product feedback for a marketing-analytics portal. In 2-3 sentences, "
                "summarise the user's point and note any concrete feature request or bug."
            ),
            messages=[{"role": "user", "content": message}],
        )
        parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        return "".join(parts).strip() or None
    except Exception:
        # Enrichment is best-effort; never raise into the feedback path.
        return None


def summarize_strategy(doc_text):
    """Write a short, client-facing strategy summary from a strategy doc's text. Returns it, or None.

    Used by Agora Atrium's "Generate AI summary from doc" action (atrium_docs.generate_summary).
    No-op (returns None) unless enrichment is enabled, configured, and the SDK is installed -- the
    caller then falls back to a plain excerpt of the doc.
    """
    client = _client()
    if client is None or not (doc_text or "").strip():
        return None
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=(
                "You write the AGORA marketing team's client-facing campaign summaries. Given an "
                "internal strategy document, write 2-3 warm, plain-English sentences a client would "
                "read in their workspace: what we're doing and why it helps them. No jargon, no "
                "headings, no preamble -- just the summary text."
            ),
            messages=[{"role": "user", "content": doc_text[:12000]}],
        )
        parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        return "".join(parts).strip() or None
    except Exception:
        return None


def _parse_sections_json(raw):
    """Best-effort parse of a model reply into a dict. Tolerates ```json fences and surrounding prose."""
    import json
    import re
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):                      # strip a ```json ... ``` fence if the model added one
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)        # last resort: grab the first {...} block
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def summarize_strategy_sections(doc_text):
    """Turn a strategy doc into a campaign's two client-facing sections. Returns a dict, or None.

    The dict has keys "what" and "why" -- bullet lists (newline-separated, one bullet per line) where
    each bullet is a precise, complete point carrying the document's concrete supporting detail --
    that populate a campaign's "Insight / Action" strategy card (the keys keep their original names
    for backward compatibility, but map positionally to Insight / Action). Used by Agora Atrium's
    "generate strategy from doc" action (atrium_docs.generate_strategy). No-op (returns None) unless
    enrichment is enabled, configured, and the SDK is installed -- the caller then falls back to a
    plain excerpt of the doc.
    """
    client = _client()
    if client is None or not (doc_text or "").strip():
        return None
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1800,
            system=(
                "You write the AGORA marketing team's client-facing campaign strategy from an "
                "internal strategy document. Read the WHOLE document, then produce exactly two "
                "sections, each a bullet list (4 to 6 bullets, ONE bullet per line). Each bullet must "
                "be PRECISE and COMPLETE -- a fully detailed point, not a terse headline. Rules for "
                "every bullet:\n"
                "  - Be precise and concrete: name the actual audience, goal, channel, format, or "
                "angle from the doc, and carry over the SPECIFIC supporting detail that makes the "
                "point land -- the real numbers, percentages, budgets, dates, platforms, KPIs, or "
                "named tactics from the document. Never a vague generality.\n"
                "  - Be complete: write a full, self-standing point (one or two sentences) that a "
                "client can understand on its own without the doc. Do not clip or summarise away the "
                "detail -- include the 'how' and the 'how much', not just the headline.\n"
                "  - Be self-contained: do NOT carry over the document's title, section numbering, or "
                "headings (e.g. '1. Client Context'); rewrite the substance in plain words.\n"
                "  - No jargon, no preamble, no markdown bullet characters -- just the point text.\n"
                "The two sections:\n"
                "  what -- the Insight: WHY this campaign matters -- the audience need, market gap, or "
                "data point that makes it worth doing, with the concrete evidence behind it.\n"
                "  why  -- the Action: WHAT we are doing about it -- the concrete approach, content "
                "angles, formats, channels, and cadence we will run to act on that insight, each with "
                "its specifics.\n"
                "Cover the document completely; do not stop early or leave a section thin. Respond "
                "with ONLY a JSON object of the form "
                '{\"what\": \"...\", \"why\": \"...\"} where each value is its bullets separated by '
                "newline (\\n) characters -- no preamble, no code fences."
            ),
            messages=[{"role": "user", "content": doc_text[:12000]}],
        )
        parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        data = _parse_sections_json("".join(parts).strip())
        if not isinstance(data, dict):
            return None
        out = {k: str(data.get(k, "") or "").strip() for k in ("what", "why")}
        return out if any(out.values()) else None
    except Exception:
        return None


def transcribe_voice(audio_bytes, content_type="audio/webm"):
    """Transcribe a voice feedback note to text. Returns the transcript string, or None.

    No-op (returns None) unless enrichment is enabled and configured. The Claude Messages API does
    not itself accept raw audio, so a real implementation would route the bytes through a
    speech-to-text step first; that integration is intentionally left as a marked TODO so the
    portal deploys without it.
    """
    if not _enabled() or not audio_bytes:
        return None
    try:
        # TODO: wire a speech-to-text step here (the audio bytes -> transcript), then optionally
        # pass the transcript to Claude (model "claude-opus-4-8") for cleanup/summary. Until that
        # is configured this returns None and feedback.py stores the raw audio unenriched.
        return None
    except Exception:
        return None
