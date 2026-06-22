"""Read a strategy Google Doc and turn it into an Atrium AI summary (optional + graceful).

Atrium lets an admin attach a Google Doc to a campaign's strategy; the "Generate AI summary from
doc" action reads that doc and writes the client-facing AI summary. Like feedback_ai.py, this is
DELIBERATELY a graceful no-op chain unless explicitly configured -- the portal must deploy and run
with no Docs/AI wired at all.

Two independent, optional capabilities, each fail-closed:
  1. Doc fetch (this module) -- gated on ATRIUM_DOCS_ENABLED=1. Reads the doc text via the Google
     Drive API using the runtime service account's Application Default Credentials. The doc must be
     shared with that SA (we do NOT use domain-wide delegation). `googleapiclient` is imported
     LAZILY, so it is NOT a hard dependency: if the package is absent or the SA can't read the doc,
     fetch returns None.
  2. Summarisation (feedback_ai.summarize_strategy) -- gated on FEEDBACK_AI_ENABLED + ANTHROPIC_API_KEY.

`generate_summary(doc_url)` ties them together and ALWAYS degrades gracefully:
  * Claude available  -> an AI-written summary            (source "ai")
  * doc readable only -> a trimmed excerpt of the doc     (source "excerpt")
  * neither           -> ("", "none")                     (the admin can still type a summary by hand)

NOTE: enabling capability (1) is the documented, opt-in deviation from Atrium's "no new infra" rule:
it requires enabling the Docs/Drive API, adding `google-api-python-client` to requirements.txt, and
sharing the strategy doc with the platform-dash runtime SA. A default deploy is unaffected.
"""

import os
import re

# Drive's read-only scope is all we need to export a doc to plain text.
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# How much of the doc to keep when falling back to a plain excerpt (no AI).
_EXCERPT_CHARS = 600


def _enabled():
    """True iff the private-doc (Drive API) path is switched on. Fail-closed otherwise."""
    return os.environ.get("ATRIUM_DOCS_ENABLED", "") in ("1", "true", "True")


def _no_public_fetch():
    """True iff the keyless public-doc fetch is switched OFF (locked-down deploys / hermetic tests)."""
    return os.environ.get("ATRIUM_DOCS_NO_PUBLIC_FETCH", "") in ("1", "true", "True")


def doc_id_from_url(doc_url):
    """Extract the Google Doc id from a share/edit URL (or accept a bare id). None if not found."""
    if not doc_url:
        return None
    text = doc_url.strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    return None


def _fetch_via_drive(doc_id):
    """Private-doc path: export the doc via the Drive API using the runtime SA's ADC. None on any error.

    The Google API client is imported LAZILY so an unconfigured deploy has no hard dependency on it.
    """
    try:
        import google.auth  # part of google-cloud-* deps already present
        from googleapiclient.discovery import build  # optional; absent on a default deploy

        creds, _project = google.auth.default(scopes=[_DRIVE_SCOPE])
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        data = service.files().export(fileId=doc_id, mimeType="text/plain").execute()
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        return (data or "").strip() or None
    except Exception:
        return None


def _fetch_via_public_export(doc_id):
    """Public-doc path: the keyless export endpoint for a doc shared 'anyone with the link'. None otherwise.

    Needs no API, key, or service account -- just an outbound GET to docs.google.com -- so a shared
    doc summarises even in local preview / a default deploy. A non-public doc redirects to a Google
    sign-in HTML page, so we accept ONLY a text/plain body and reject anything else as unreadable.
    The doc id is validated (doc_id_from_url) and the host is hard-coded, so there is no SSRF surface.
    """
    if _no_public_fetch():
        return None
    try:
        import requests  # already a portal dependency (used by the dashboard proxy)

        url = "https://docs.google.com/document/d/%s/export?format=txt" % doc_id
        resp = requests.get(url, timeout=10, allow_redirects=True,
                            headers={"User-Agent": "AgoraAtrium/1.0"})
        if resp.status_code != 200:
            return None
        if "text/plain" not in resp.headers.get("Content-Type", "").lower():
            return None
        text = (resp.text or "").strip()
        if not text or text.startswith("<"):   # an HTML sign-in/error page is not the doc
            return None
        return text[:100000]
    except Exception:
        return None


def fetch_doc_text(doc_url):
    """Return the plain text of a Google Doc, or None if it cannot be read.

    Two keyless-by-default tiers, each fail-closed (a missing package, an unshared doc, or a network
    error never raises into the request path -- the caller degrades):
      1. PRIVATE docs -- the Drive API using the runtime SA's ADC. Opt-in (ATRIUM_DOCS_ENABLED +
         google-api-python-client + the doc shared with that SA); tried first when enabled.
      2. PUBLIC docs ('anyone with the link can view') -- the keyless export endpoint. No infra, so a
         pasted link works in local preview and a default deploy. Disable with ATRIUM_DOCS_NO_PUBLIC_FETCH=1.
    """
    doc_id = doc_id_from_url(doc_url)
    if not doc_id:
        return None
    if _enabled():
        text = _fetch_via_drive(doc_id)
        if text:
            return text
    return _fetch_via_public_export(doc_id)


def _excerpt(text):
    """A short, clean excerpt of the doc for the no-AI fallback (one trimmed paragraph).

    Collapses ALL whitespace runs (including single newlines) to single spaces so the excerpt is one
    clean line. In the strategy card that renders as a single readable bullet, rather than splitting
    the doc's raw title/heading lines (e.g. '1. Client Context') into separate junk bullets.
    """
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if len(collapsed) <= _EXCERPT_CHARS:
        return collapsed
    cut = collapsed[:_EXCERPT_CHARS]
    # Prefer to break on a sentence/space boundary rather than mid-word.
    for sep in (". ", " "):
        idx = cut.rfind(sep)
        if idx > _EXCERPT_CHARS // 2:
            return cut[: idx + (1 if sep == ". " else 0)].strip() + " …"
    return cut.strip() + " …"


def generate_summary(doc_url):
    """Produce an AI summary from a strategy doc. Returns (summary, source).

    source is one of:
      "ai"      -- Claude wrote the summary from the doc text,
      "excerpt" -- AI is off, so we returned a trimmed excerpt of the doc,
      "none"    -- the doc could not be read (disabled / unshared / no URL).
    Never raises: every failure degrades to ("", "none") so the action stays usable.
    """
    text = fetch_doc_text(doc_url)
    if not text:
        return "", "none"
    try:
        import feedback_ai
        summary = feedback_ai.summarize_strategy(text)
    except Exception:
        summary = None
    if summary:
        return summary, "ai"
    return _excerpt(text), "excerpt"


# --- No-AI document parser: split a brief into Insight vs Action by its own section headings -------
#
# When AI is off (no key), we still fill BOTH columns by reading the document's structure. We break
# the doc into sections (a short heading line + the body until the next heading), classify each
# section as Insight (the WHY -- context, audience, goals) or Action (the WHAT -- strategy, content,
# deliverables) from keywords in its heading, and turn each section into one clean bullet. It is a
# best-effort heuristic, not AI: quality depends on how the doc is structured, but it reliably
# populates Action instead of leaving it blank.

# Heading keyword -> column. Checked (substring, lower-cased) against each section heading.
_INSIGHT_KW = (
    "context", "insight", "audience", "target", "persona", "demographic", "problem", "challenge",
    "pain", "background", "situation", "overview", "research", "data", "market", "opportunity",
    "goal", "objective", "mission", "vision", "brand", "about", "who", "why",
)
_ACTION_KW = (
    "action", "strateg", "plan", "approach", "execution", "tactic", "content", "pillar",
    "deliverable", "asset", "recommend", "solution", "channel", "campaign idea", "creative", "idea",
    "format", "produce", "cadence", "calendar", "schedule", "distribution", "post", "publish",
    "next step", "to do", "do next", "todo", "workflow", "rollout",
)

_MAX_BULLETS = 6          # per column
_MAX_BULLET_CHARS = 240   # trim each bullet to keep the card readable


def _is_heading(line):
    """True if `line` looks like a short section heading (e.g. 'Client Context' or '2. Strategy')."""
    s = (line or "").strip()
    if not s or len(s) > 70:
        return False
    body = re.sub(r"^\d+[.)]\s*", "", s).strip()        # drop a leading '1.' / '2)' number
    if not body or len(body) > 60:
        return False
    if body[-1] in ".!?,;":                              # headings don't end like a sentence
        return False
    # Title-ish: starts with a capital and is only a few words.
    return body[0].isupper() and len(body.split()) <= 8


def _classify_heading(heading):
    """Return 'insight', 'action', or None for a heading, by keyword match (action checked first)."""
    h = (heading or "").lower()
    for kw in _ACTION_KW:
        if kw in h:
            return "action"
    for kw in _INSIGHT_KW:
        if kw in h:
            return "insight"
    return None


# Leading boilerplate labels to strip off a sentence (they stack, e.g. 'Confidence level — High —').
_LABEL_PREFIX = re.compile(
    r"^(insight\s*\d+|confidence level|confidence|high|medium|low|best positioning angle|"
    r"data summary|recommendation|note|summary|goal|objective)\s*[:\-—]\s*",
    re.IGNORECASE,
)
_MIN_SENTENCE_CHARS = 30
_MIN_SENTENCE_WORDS = 5


def _clean_sentence(s):
    """Tidy one candidate sentence: drop fill-in underscores and any leading label prefixes."""
    s = re.sub(r"_{2,}", " ", s or "")                   # kill '______' fill-in blanks
    s = re.sub(r"\s+", " ", s).strip()
    while True:                                          # labels can stack -> strip repeatedly
        stripped = _LABEL_PREFIX.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    return s


def _is_good_sentence(s):
    """True if `s` reads like real content, not a heading, label, value, or fragment."""
    if len(s) < _MIN_SENTENCE_CHARS or len(s.split()) < _MIN_SENTENCE_WORDS:
        return False
    letters = sum(c.isalpha() for c in s)
    return letters >= len(s) * 0.6                       # mostly words, not blanks / symbols / numbers


def _sentences_from(body):
    """Split a section body into clean ONE-sentence bullets (no heading), dropping junk fragments."""
    text = re.sub(r"_{2,}", " ", body or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    text = re.sub(r"\s*(Insight\s*\d+\s*:)", r"\n\1", text, flags=re.IGNORECASE)  # break before inline labels
    raw = []
    for chunk in text.split("\n"):
        raw.extend(re.split(r"(?<=[.!?])\s+", chunk))
    out = []
    for part in raw:
        s = _clean_sentence(part)
        if not _is_good_sentence(s):
            continue
        if len(s) > _MAX_BULLET_CHARS:                  # hard-trim an over-long sentence on a space
            cut = s[:_MAX_BULLET_CHARS]
            idx = cut.rfind(" ")
            s = (cut[:idx] if idx > _MAX_BULLET_CHARS // 2 else cut).strip() + " …"
        out.append(s)
    return out


def _parse_sections(text):
    """Break the doc into [(heading, body), ...] sections by detecting short heading lines."""
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    sections, heading, body = [], None, []
    for ln in lines:
        if not ln.strip():
            continue
        if _is_heading(ln):
            if heading is not None or body:
                sections.append((heading, " ".join(body).strip()))
            heading = re.sub(r"^\d+[.)]\s*", "", ln.strip()).rstrip(":").strip()
            body = []
        else:
            body.append(ln.strip())
    if heading is not None or body:
        sections.append((heading, " ".join(body).strip()))
    # Drop a leading title-only section (heading, no body) -- usually the document title.
    return [(h, b) for (h, b) in sections if (h or b)]


def _dedupe(bullets):
    """Drop duplicate bullets (case-insensitive), preserving first-seen order."""
    seen, out = set(), []
    for b in bullets:
        key = b.lower()
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def split_doc_to_sections(text):
    """Heuristically split a brief into Insight / Action bullet lists. Returns {"what", "why"}.

    No AI: classifies each document section by its heading keywords (context/audience/goals ->
    Insight; strategy/content/deliverables -> Action), then emits each section's body as clean
    ONE-sentence bullets (the heading itself is NOT shown -- it only decides the column). Junk
    fragments (labels, values, fill-in blanks, headings) are dropped. Sections with an unrecognised
    heading go to Insight until the first Action section is seen, then to Action (the natural brief
    flow: context first, then plan). If one column ends up empty, bullets are split in half so both fill.
    """
    sections = _parse_sections(text)
    # Drop leading title-only (no body) sections -- typically the document title line itself.
    while sections and not sections[0][1]:
        sections.pop(0)
    if not sections:
        return {"what": _excerpt(text), "why": ""}

    insight, action, seen_action = [], [], False
    for heading, body in sections:
        bullets = _sentences_from(body)
        if not bullets:
            continue
        kind = _classify_heading(heading)
        if kind == "action":
            seen_action = True
            action.extend(bullets)
        elif kind == "insight":
            insight.extend(bullets)
        else:
            (action if seen_action else insight).extend(bullets)

    insight, action = _dedupe(insight), _dedupe(action)

    # If everything landed in one column, split the bullets in half so both columns populate.
    if insight and not action:
        mid = (len(insight) + 1) // 2
        insight, action = insight[:mid], insight[mid:]
    elif action and not insight:
        mid = (len(action) + 1) // 2
        insight, action = action[:mid], action[mid:]

    return {
        "what": "\n".join(insight[:_MAX_BULLETS]).strip(),
        "why": "\n".join(action[:_MAX_BULLETS]).strip(),
    }


def generate_strategy(doc_url):
    """Produce a campaign's two strategy sections from a strategy doc. Returns (strategy, source).

    strategy is a dict {"what", "why"} of client-facing bullet lists (mapping positionally to the
    Insight / Action columns), or None when the doc could not be read. source is one of:
      "ai"      -- Claude wrote the two sections from the doc text,
      "parsed"  -- AI is off, so we split the doc into Insight/Action by its own section headings,
      "none"    -- the doc could not be read (disabled / unshared / no URL).
    Never raises: every failure degrades so the caller can still create the campaign (with the doc
    link saved) and let the admin fill the sections by hand.
    """
    text = fetch_doc_text(doc_url)
    if not text:
        return None, "none"
    try:
        import feedback_ai
        sections = feedback_ai.summarize_strategy_sections(text)
    except Exception:
        sections = None
    if sections:
        return {"what": sections.get("what", ""), "why": sections.get("why", "")}, "ai"
    # No AI: split the document by its own structure so BOTH columns fill (Action no longer blank).
    return split_doc_to_sections(text), "parsed"
