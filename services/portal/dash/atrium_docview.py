"""Render an uploaded Office document to a small, scrollable, self-contained HTML preview page.

Atrium attachments that a browser cannot display natively (Word / Excel / PowerPoint / CSV / text)
are rendered here to plain styled HTML so the content card and the doc lightbox can show "what's
inside" inside an <iframe>. PDFs are NOT handled here -- the browser previews those natively via the
inline creative serve route; this module covers only the formats a browser can't render itself.

Stdlib ONLY (zipfile + ElementTree) -- NO external deps, NO new infra (per the Atrium "self-contained,
no CDN" rule). Every path degrades gracefully: an unreadable or unsupported file yields a friendly
"download to view" page rather than raising, so the route never 500s on a malformed upload.
"""

import csv
import html
import io
import zipfile
import xml.etree.ElementTree as ET

# Safety caps so a huge document can't blow up the preview render / response size.
MAX_BLOCKS = 4000      # docx paragraphs
MAX_ROWS = 400         # spreadsheet rows
MAX_COLS = 40          # spreadsheet columns
MAX_SLIDES = 200       # pptx slides
MAX_TEXT_CHARS = 400000  # plain-text / csv decode ceiling


def _local(tag):
    """Local-name of a namespaced XML tag, e.g. '{ns}p' -> 'p'."""
    return tag.rsplit("}", 1)[-1]


def _kind(mime, name):
    """Classify an attachment into the preview family this module renders, by extension first
    (most reliable for our uploads) then mime. Returns one of: docx, xlsx, pptx, csv, txt, ''."""
    n = (name or "").lower()
    m = (mime or "").lower()
    if n.endswith(".docx") or "wordprocessingml" in m:
        return "docx"
    if n.endswith(".xlsx") or "spreadsheetml" in m:
        return "xlsx"
    if n.endswith(".pptx") or "presentationml" in m:
        return "pptx"
    if n.endswith(".csv") or m == "text/csv":
        return "csv"
    if n.endswith(".txt") or m == "text/plain":
        return "txt"
    return ""


# ---- per-format extraction ---------------------------------------------------------------------

def _docx_html(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open("word/document.xml") as fh:
            root = ET.parse(fh).getroot()
    out = []
    for el in root.iter():
        if _local(el.tag) != "p":
            continue
        parts = []
        for node in el.iter():
            ln = _local(node.tag)
            if ln == "t" and node.text:
                parts.append(node.text)
            elif ln == "tab":
                parts.append("\t")
            elif ln in ("br", "cr"):
                parts.append("\n")
        text = "".join(parts)
        if text.strip():
            out.append("<p>%s</p>" % html.escape(text).replace("\n", "<br>"))
        if len(out) >= MAX_BLOCKS:
            break
    if not out:
        return None
    return '<div class="doc-body docx">%s</div>' % "".join(out)


def _col_index(ref):
    """'B12' -> 1 (zero-based column). Returns None if no column letters present."""
    letters = "".join(ch for ch in (ref or "") if ch.isalpha())
    if not letters:
        return None
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _xlsx_html(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            with z.open("xl/sharedStrings.xml") as fh:
                sroot = ET.parse(fh).getroot()
            for si in sroot:
                shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            return None
        with z.open(sheets[0]) as fh:
            wroot = ET.parse(fh).getroot()
    sheet_data = None
    for el in wroot:
        if _local(el.tag) == "sheetData":
            sheet_data = el
            break
    if sheet_data is None:
        return None
    rows = []
    max_col = 0
    for row in sheet_data:
        if _local(row.tag) != "row":
            continue
        cells = {}
        auto = 0
        for c in row:
            if _local(c.tag) != "c":
                continue
            t = c.get("t")
            v = None
            for child in c:
                lc = _local(child.tag)
                if lc == "v":
                    v = child.text
                elif lc == "is":
                    v = "".join(x.text or "" for x in child.iter() if _local(x.tag) == "t")
            if t == "s" and v is not None:
                try:
                    idx = int(v)
                    v = shared[idx] if 0 <= idx < len(shared) else ""
                except ValueError:
                    pass
            col = _col_index(c.get("r"))
            if col is None:
                col = auto
            auto = col + 1
            cells[col] = "" if v is None else v
            if col > max_col:
                max_col = col
        rows.append(cells)
        if len(rows) >= MAX_ROWS:
            break
    if not rows:
        return None
    ncols = min(max_col + 1, MAX_COLS)
    trs = []
    for cells in rows:
        tds = []
        for ci in range(ncols):
            tds.append("<td>%s</td>" % html.escape(str(cells.get(ci, ""))))
        trs.append("<tr>%s</tr>" % "".join(tds))
    return '<div class="doc-body sheet"><table>%s</table></div>' % "".join(trs)


def _pptx_html(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted(
            n for n in z.namelist()
            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        )
        out = []
        for i, sname in enumerate(slides[:MAX_SLIDES], 1):
            with z.open(sname) as fh:
                sroot = ET.parse(fh).getroot()
            texts = [t.text for t in sroot.iter() if _local(t.tag) == "t" and t.text]
            body = "".join("<p>%s</p>" % html.escape(t) for t in texts) or "<p class=\"muted\">(no text)</p>"
            out.append('<section class="slide"><h3>Slide %d</h3>%s</section>' % (i, body))
    if not out:
        return None
    return '<div class="doc-body deck">%s</div>' % "".join(out)


def _decode_text(data):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data[:MAX_TEXT_CHARS].decode(enc)
        except UnicodeDecodeError:
            continue
    return data[:MAX_TEXT_CHARS].decode("latin-1", "replace")


def _csv_html(data):
    text = _decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    trs = []
    for r in reader:
        tds = "".join("<td>%s</td>" % html.escape(cell) for cell in r[:MAX_COLS])
        trs.append("<tr>%s</tr>" % tds)
        if len(trs) >= MAX_ROWS:
            break
    if not trs:
        return None
    return '<div class="doc-body sheet"><table>%s</table></div>' % "".join(trs)


def _txt_html(data):
    text = _decode_text(data)
    return '<div class="doc-body text"><pre>%s</pre></div>' % html.escape(text)


# ---- public entry point ------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
  :root { --green:#4FAB4A; --violet:#5C4BD0; --ink:#0E0F12; --sub:#6B7280; --line:#E9EAEE; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%%; }
  body { font-family: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: var(--ink); background: #f4f5f7; line-height: 1.6; -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 900px; margin: 0 auto; padding: 28px 22px 60px; }
  .sheetwrap { max-width: none; }
  .doc-body { background: #fff; border: 1px solid var(--line); border-radius: 10px;
              box-shadow: 0 1px 2px rgba(16,24,40,.04), 0 10px 30px rgba(16,24,40,.06); padding: 40px 46px; }
  .doc-body.docx p { margin: 0 0 11px; font-size: 15px; color: #23272f; }
  .doc-body.text pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 13px;
                       font-family: ui-monospace, Menlo, Consolas, monospace; color: #23272f; }
  .doc-body.sheet { padding: 0; overflow: auto; }
  table { border-collapse: collapse; width: 100%%; font-size: 13px; }
  td { border: 1px solid var(--line); padding: 5px 9px; white-space: nowrap; vertical-align: top; color: #23272f; }
  tr:first-child td { background: #f7f8fa; font-weight: 700; position: sticky; top: 0; }
  tr:nth-child(even) td { background: #fbfbfc; }
  .deck .slide { background: #fff; border: 1px solid var(--line); border-radius: 10px; padding: 22px 26px; margin: 0 0 16px; }
  .deck h3 { margin: 0 0 10px; font-size: 13px; letter-spacing: .4px; text-transform: uppercase; color: var(--violet); }
  .deck p { margin: 0 0 7px; font-size: 15px; color: #23272f; }
  .muted { color: var(--sub); }
  .fallback { display: grid; place-items: center; min-height: 70vh; text-align: center; padding: 30px; }
  .fallback .ic { font-size: 40px; }
  .fallback h2 { margin: 14px 0 6px; font-size: 17px; }
  .fallback p { margin: 0; color: var(--sub); font-size: 14px; max-width: 420px; }
</style></head>
<body><div class="wrap%(wrapcls)s">%(body)s</div></body></html>"""


def _fallback(name):
    safe = html.escape(name or "this file")
    body = (
        '<div class="fallback"><div class="ic">📄</div>'
        "<h2>Preview not available</h2>"
        "<p>%s can&rsquo;t be previewed in the browser. Use the download button to open it.</p></div>"
        % safe
    )
    return _PAGE % {"title": safe, "wrapcls": "", "body": body}


def render_doc_html(data, mime, name):
    """Render `data` (the raw uploaded bytes of `name`, content-type `mime`) to a full, self-contained
    HTML preview page. Always returns a complete HTML string -- never raises -- falling back to a
    friendly "download to view" page for unsupported or corrupt files."""
    kind = _kind(mime, name)
    body = None
    is_sheet = kind in ("xlsx", "csv")
    try:
        if kind == "docx":
            body = _docx_html(data)
        elif kind == "xlsx":
            body = _xlsx_html(data)
        elif kind == "pptx":
            body = _pptx_html(data)
        elif kind == "csv":
            body = _csv_html(data)
        elif kind == "txt":
            body = _txt_html(data)
    except Exception:
        body = None
    if not body:
        return _fallback(name)
    return _PAGE % {
        "title": html.escape(name or "Document"),
        "wrapcls": " sheetwrap" if is_sheet else "",
        "body": body,
    }
