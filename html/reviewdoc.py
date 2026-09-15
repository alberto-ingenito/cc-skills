#!/usr/bin/env python3
"""reviewdoc — build self-contained, annotatable HTML review documents.

Markdown-ish source in, one HTML file out, with a Word-style threaded comment
panel. Notes live inside the HTML (a JSON block), so a rebuild never loses them
and Claude can read and reply to them from the command line.

Python 3 stdlib only. See SKILL.md for the operational loop.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser

TAGS = {
    "question": "❓ Question",
    "clarify": "\U0001f50d Clarify",
    "change": "✏️ Change this",
    "concern": "⚠️ Concern",
    "unclear": "\U0001f937 Don't understand",
    "agree": "\U0001f44d Agree",
}

# Tags that can be submitted without a note body (the quote alone is the ask).
NO_BODY_TAGS = {"clarify"}
NOTES_RE = re.compile(
    r'(<script id="rd-notes" type="application/json">)(.*?)(</script>)', re.S
)
SEC_RE = re.compile(
    r'<section class="rd-sec" id="([^"]+)" data-title="([^"]*)">(.*?)<!--/rd-sec--></section>',
    re.S,
)


def die(msg):
    sys.stderr.write("reviewdoc: %s\n" % msg)
    sys.exit(1)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def esc(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s or "x"


def u16len(s):
    """Length in UTF-16 code units — the offset space JS strings use."""
    return len(s.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------
# markdown subset -> html
# --------------------------------------------------------------------------

CODE_RE = re.compile(r"`([^`]+)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
ITAL_RE = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)")


def inline(text):
    """Escape, then apply the small inline grammar. Code spans are protected."""
    spans = []

    def stash(m):
        spans.append(esc(m.group(1)))
        return "\x00%d\x00" % (len(spans) - 1)

    text = CODE_RE.sub(stash, text)
    text = esc(text)
    text = BOLD_RE.sub(lambda m: "<b>%s</b>" % m.group(1), text)
    text = ITAL_RE.sub(lambda m: "<i>%s</i>" % m.group(1), text)
    text = LINK_RE.sub(
        lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), text
    )
    text = re.sub(
        r"\x00(\d+)\x00", lambda m: "<code>%s</code>" % spans[int(m.group(1))], text
    )
    return text


def front_matter(lines):
    meta = {}
    if not lines or lines[0].strip() != "---":
        return meta, lines
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return meta, lines[i + 1 :]
        m = re.match(r"\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$", lines[i])
        if m:
            meta[m.group(1)] = m.group(2).strip().strip("\"'")
    return meta, lines[1:]


def is_blank(s):
    return not s.strip()


def take_directive(lines, i):
    """Return (name, args, body_lines, next_index) for a ::: block at i."""
    m = re.match(r":::(\w+)[ \t]*(.*)$", lines[i].strip())
    name, args = m.group(1), m.group(2).strip()
    body, j, depth = [], i + 1, 1
    while j < len(lines):
        s = lines[j].strip()
        if s == ":::":
            depth -= 1
            if depth == 0:
                return name, args, body, j + 1
        elif re.match(r":::\w+", s):
            depth += 1
        body.append(lines[j])
        j += 1
    return name, args, body, j  # unterminated: swallow to EOF


def list_items(lines):
    """Flat-ish list parse. Returns [(depth, text, ordered)] items."""
    out = []
    for ln in lines:
        m = re.match(r"^(\s*)(?:[-*]|(\d+)[.)])\s+(.*)$", ln)
        if m:
            out.append(
                (len(m.group(1)) // 2, m.group(3).rstrip(), m.group(2) is not None)
            )
        elif out and ln.strip():
            d, t, o = out[-1]
            out[-1] = (d, (t + " " + ln.strip()).strip(), o)
    return out


def render_list(items, cls=""):
    if not items:
        return ""
    attr = ' class="%s"' % cls if cls else ""
    out, stack = [], []
    for d, text, ordered in items:
        tag = "ol" if ordered else "ul"
        d = min(d, len(stack))
        while len(stack) > d + 1:
            out.append("</li></%s>" % stack.pop())
        if len(stack) == d + 1:
            out.append("</li>")
        else:
            out.append("<%s%s>" % (tag, attr if not stack else ""))
            stack.append(tag)
        out.append("<li>%s" % inline(text))
    while stack:
        out.append("</li></%s>" % stack.pop())
    return "".join(out)


def render_table(rows):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head = len(cells) > 1 and all(
        re.fullmatch(r":?-{2,}:?", c) for c in cells[1] if c
    )
    out = ['<div class="scroll"><table>']
    body = cells[2:] if head else cells
    if head:
        out.append(
            "<tr>%s</tr>" % "".join("<th>%s</th>" % inline(c) for c in cells[0])
        )
    for row in body:
        out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in row))
    out.append("</table></div>")
    return "".join(out)


ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
HR_RE = re.compile(r"\s*(---+|\*\*\*+)\s*$")
STOP = (":::", "```", "|", "###")


def scan_list(lines, i):
    """Consume one list, allowing blank lines between items. -> end index."""
    n, j = len(lines), i
    while j < n:
        if is_blank(lines[j]):
            k = j
            while k < n and is_blank(lines[k]):
                k += 1
            if k < n and (ITEM_RE.match(lines[k]) or lines[k].startswith(("  ", "\t"))):
                j = k
                continue
            return j
        if lines[j].strip().startswith(STOP) or HR_RE.fullmatch(lines[j]):
            return j
        j += 1
    return j


def render_blocks(lines):
    """Everything except `##` headings (the section splitter owns those)."""
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if is_blank(ln):
            i += 1
            continue

        if ln.strip().startswith(":::"):
            name, args, body, i = take_directive(lines, i)
            out.append(directive(name, args, body))
            continue

        if ln.lstrip().startswith("```"):
            j, buf = i + 1, []
            while j < n and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append("<pre><code>%s</code></pre>" % esc("\n".join(buf)))
            i = j + 1
            continue

        if re.match(r"^###\s+", ln):
            out.append("<h3>%s</h3>" % inline(ln.split(None, 1)[1].strip()))
            i += 1
            continue

        if HR_RE.fullmatch(ln):
            out.append("<hr>")
            i += 1
            continue

        if ln.strip().startswith("|"):
            j = i
            while j < n and lines[j].strip().startswith("|"):
                j += 1
            out.append(render_table(lines[i:j]))
            i = j
            continue

        if ITEM_RE.match(ln):
            j = scan_list(lines, i)
            out.append(render_list(list_items(lines[i:j])))
            i = j
            continue

        j = i
        while (
            j < n
            and not is_blank(lines[j])
            and not lines[j].strip().startswith(STOP)
            and not HR_RE.fullmatch(lines[j])
            and not ITEM_RE.match(lines[j])
        ):
            j += 1
        if j == i:
            j += 1
        out.append("<p>%s</p>" % inline(" ".join(x.strip() for x in lines[i:j])))
        i = j
    return "".join(out)


def paragraphs(lines):
    """Split on blank lines, return list of joined single-line paragraphs."""
    out, buf = [], []
    for ln in lines + [""]:
        if is_blank(ln):
            if buf:
                out.append(" ".join(x.strip() for x in buf))
                buf = []
        else:
            buf.append(ln)
    return out


def directive(name, args, body):
    if name == "html":
        return "\n".join(body)

    if name == "flag":
        parts = args.split("|", 1)
        title = parts[1].strip() if len(parts) > 1 else ""
        words = parts[0].split()
        if words and words[0] in ("good", "warn", "bad", "info"):
            kind, pill = words[0], " ".join(words[1:])
        else:
            kind, pill = "info", " ".join(words)
        cls = "" if kind == "info" else " " + kind
        head = ""
        if pill or title:
            bits = ""
            if pill:
                bits += '<span class="pill%s">%s</span>' % (cls, inline(pill))
            if title:
                bits += inline(title)
            head = '<div class="t">%s</div>' % bits
        return '<div class="flag%s">%s%s</div>' % (cls, head, render_blocks(body))

    if name == "card":
        head = "<h3>%s</h3>" % inline(args) if args else ""
        return '<div class="card">%s%s</div>' % (head, render_blocks(body))

    if name == "analogy":
        return '<div class="an">%s</div>' % "<br><br>".join(
            inline(p) for p in paragraphs(body)
        )

    if name == "lede":
        return "".join('<p class="lede">%s</p>' % inline(p) for p in paragraphs(body))

    if name == "steps":
        out = []
        for k, (_, text, _) in enumerate(list_items(body), 1):
            out.append(
                '<div class="step"><div class="stepn">%d</div><div>%s</div></div>'
                % (k, inline(text))
            )
        return "".join(out)

    if name in ("tick", "cross"):
        return render_list(list_items(body), cls=name)

    sys.stderr.write("reviewdoc: warning: unknown directive :::%s (rendered as card)\n" % name)
    return '<div class="card">%s</div>' % render_blocks(body)


def split_sections(lines, intro_title):
    """-> [(id, title, html)]. Slot 0 is always the intro (content before the
    first `##`), so the page header has somewhere to live and text selected up
    there still anchors. `##` inside a fence or a ::: block is not a heading."""
    chunks, cur, num = [], [intro_title, [], None], 0
    fence, depth = False, 0
    for ln in lines:
        s = ln.strip()
        if s.startswith("```"):
            fence = not fence
        elif not fence:
            if s == ":::":
                depth = max(0, depth - 1)
            elif re.match(r":::\w+", s):
                depth += 1
        m = None if (fence or depth) else re.match(r"^##\s+(.*)$", ln)
        if m:
            chunks.append(cur)
            num += 1
            cur = [m.group(1).strip(), [], num]
        else:
            cur[1].append(ln)
    chunks.append(cur)

    out, seen = [], {}
    for k, (title, body, num) in enumerate(chunks):
        if k and not any(x.strip() for x in body) and not title:
            continue
        head = ""
        if num is not None:
            head = '<h2><span class="num">%d</span>%s</h2>' % (num, inline(title))
        if k == 0:
            sid = "sec-intro"
        else:
            sid = "sec-" + slugify(title)
            seen[sid] = seen.get(sid, 0) + 1
            if seen[sid] > 1:
                sid = "%s-%d" % (sid, seen[sid])
        out.append((sid, title, head + render_blocks(body)))
    return out


# --------------------------------------------------------------------------
# text extraction — must mirror the browser's text-node view exactly
# --------------------------------------------------------------------------


class TextNodes(HTMLParser):
    """Collect non-blank text nodes, skipping svg/script/style, like the JS
    TreeWalker filter does. A text node is a maximal run of character data
    between markup, so any tag/comment flushes the buffer."""

    SKIP = ("svg", "script", "style")

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self._buf = []
        self._skip = 0

    def _flush(self):
        if self._buf:
            s = "".join(self._buf)
            self._buf = []
            if s.strip():
                self.parts.append(s)

    def handle_starttag(self, tag, attrs):
        self._flush()
        if tag in self.SKIP:
            self._skip += 1

    def handle_startendtag(self, tag, attrs):
        self._flush()

    def handle_endtag(self, tag):
        self._flush()
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def handle_comment(self, data):
        self._flush()

    def handle_decl(self, decl):
        self._flush()

    def handle_pi(self, data):
        self._flush()

    def close(self):
        HTMLParser.close(self)
        self._flush()


def section_text(html):
    p = TextNodes()
    p.feed(html)
    p.close()
    return "".join(p.parts)


# --------------------------------------------------------------------------
# notes: read / re-anchor / embed
# --------------------------------------------------------------------------


def read_notes(html, path="document"):
    m = NOTES_RE.search(html)
    if not m:
        die("%s has no <script id=\"rd-notes\"> block — was it built by reviewdoc?" % path)
    raw = m.group(2).strip() or "[]"
    try:
        notes = json.loads(raw)
    except ValueError as e:
        die("%s: embedded notes are not valid JSON (%s)" % (path, e))
    if not isinstance(notes, list):
        die("%s: embedded notes block is not a JSON array" % path)
    return notes


def write_notes(html, notes):
    payload = json.dumps(notes, ensure_ascii=False, indent=1)
    # keep the browser's HTML tokenizer out of the JSON
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    return NOTES_RE.sub(
        lambda m: m.group(1) + "\n" + payload + "\n" + m.group(3), html, count=1
    )


def doc_sections(html):
    """-> [(id, title, text)] from an already-built document."""
    return [(sid, title, section_text(body)) for sid, title, body in SEC_RE.findall(html)]


def reanchor(notes, sections):
    """Re-point every note at the new prose. Never drops a note."""
    texts = dict((sid, text) for sid, _t, text in sections)
    titles = dict((sid, t) for sid, t, _x in sections)
    order = [sid for sid, _t, _x in sections]
    stats = {"ok": 0, "moved": 0, "lost": 0}

    for n in notes:
        quote = n.get("quote") or ""
        if not quote:
            n["anchored"] = True  # nothing to anchor: a general note, not a broken one
            n.setdefault("start", None)
            stats["ok"] += 1
            continue
        want = n.get("start")
        want = want if isinstance(want, int) else 0
        hit = None
        for sid in [n.get("sectionId")] + order:
            if sid not in texts or (hit and hit[0] == sid):
                continue
            found = []
            pos = texts[sid].find(quote)
            while pos != -1:
                found.append(u16len(texts[sid][:pos]))
                pos = texts[sid].find(quote, pos + 1)
            if found:
                hit = (sid, min(found, key=lambda p: abs(p - want)))
                break
        if hit is None:
            n["anchored"] = False
            stats["lost"] += 1
            continue
        if hit[0] != n.get("sectionId") or hit[1] != n.get("start"):
            stats["moved"] += 1
        else:
            stats["ok"] += 1
        n["sectionId"], n["start"] = hit[0], hit[1]
        n["sectionTitle"] = titles.get(hit[0], n.get("sectionTitle") or "")
        n["anchored"] = True
    return stats


def normalise(n, i=0):
    n.setdefault("id", "n%d" % (i + 1))
    n.setdefault("kind", "note")
    n.setdefault("tag", "highlight" if n["kind"] == "highlight" else "question")
    n.setdefault("quote", "")
    n.setdefault("sectionId", None)
    n.setdefault("sectionTitle", "")
    n.setdefault("context", "")
    n.setdefault("start", None)
    n.setdefault("body", "")
    n.setdefault("author", "user")
    n.setdefault("ts", now_iso())
    n.setdefault("status", "open")
    n.setdefault("anchored", True)
    if not isinstance(n.get("replies"), list):
        n["replies"] = []
    for r in n["replies"]:
        r.setdefault("author", "claude")
        r.setdefault("body", "")
        r.setdefault("ts", now_iso())
    return n


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

CSS = r"""
  :root{
    --bg:#0f1115; --card:#171a21; --card2:#1e222b; --ink:#e8eaed; --dim:#9aa3b2;
    --line:#2a2f3a; --accent:#5b9dff; --good:#3ecf8e; --warn:#f0b429; --bad:#ff5c5c;
    --you:#c77dff; --panw:392px;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f6f7f9; --card:#fff; --card2:#f0f2f5; --ink:#14171c; --dim:#5a6472;
           --line:#e1e5ea; --accent:#1f6feb; --good:#12874f; --warn:#a86a00; --bad:#c92a2a;
           --you:#7d2fd0; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;padding:0 0 80px}
  .wrap{max-width:920px;margin:0 auto;padding:0 20px}
  header{padding:56px 0 32px;border-bottom:1px solid var(--line);margin-bottom:36px}
  h1{font-size:clamp(27px,5vw,40px);line-height:1.15;margin:0 0 12px;letter-spacing:-.02em}
  .sub{color:var(--dim);font-size:18px;max-width:62ch;margin:0}
  .meta{color:var(--dim);font-size:13px;margin-top:16px;font-family:var(--mono)}
  h2{font-size:clamp(21px,3.4vw,28px);margin:56px 0 8px;letter-spacing:-.01em}
  h2 .num{color:var(--accent);font-variant-numeric:tabular-nums;margin-right:10px;font-weight:600}
  h3{font-size:18px;margin:28px 0 8px}
  p{margin:0 0 14px}
  a{color:var(--accent)}
  .lede{color:var(--dim);margin:0 0 24px;font-size:17px;max-width:66ch}
  code{font-family:var(--mono);font-size:.86em;background:var(--card2);padding:2px 6px;
    border-radius:5px;border:1px solid var(--line);word-break:break-word}
  pre{background:var(--card2);border:1px solid var(--line);border-radius:11px;padding:15px 16px;
    overflow-x:auto;margin:14px 0;font-family:var(--mono);font-size:13px;line-height:1.55}
  pre code{background:none;border:none;padding:0;font-size:inherit}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:18px 0}
  .card>h3:first-child{margin-top:0}
  .grid{display:grid;gap:16px}
  @media(min-width:720px){ .g2{grid-template-columns:1fr 1fr} .g3{grid-template-columns:repeat(3,1fr)} }
  .flag{border-left:4px solid var(--accent);background:var(--card);border-radius:0 12px 12px 0;
    padding:18px 22px;margin:20px 0;border-top:1px solid var(--line);
    border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
  .flag.bad{border-left-color:var(--bad)} .flag.warn{border-left-color:var(--warn)}
  .flag.good{border-left-color:var(--good)}
  .flag .t{font-weight:650;margin-bottom:6px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
  .flag>p:last-child,.flag>ul:last-child,.flag>ol:last-child,.flag>div:last-child{margin-bottom:0}
  .pill{display:inline-block;font-size:12px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;
    padding:3px 9px;border-radius:999px;background:var(--card2);color:var(--dim);border:1px solid var(--line)}
  .pill.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
  .pill.good{color:var(--good);border-color:color-mix(in srgb,var(--good) 40%,transparent)}
  .pill.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
  svg{display:block;max-width:100%;height:auto;margin:10px auto}
  table{width:100%;border-collapse:collapse;margin:16px 0;font-size:15px}
  th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}
  th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);font-weight:650}
  tr:last-child td{border-bottom:none}
  ul,ol{margin:0 0 14px;padding-left:22px} li{margin-bottom:7px}
  .tick{list-style:none;padding-left:0} .tick li{padding-left:28px;position:relative}
  .tick li::before{content:"\2713";position:absolute;left:4px;color:var(--good);font-weight:700}
  .cross{list-style:none;padding-left:0} .cross li{padding-left:28px;position:relative}
  .cross li::before{content:"\2715";position:absolute;left:4px;color:var(--bad);font-weight:700}
  .small{font-size:14px;color:var(--dim)}
  .step{display:flex;gap:14px;margin-bottom:16px}
  .stepn{flex:0 0 26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;
    display:grid;place-items:center;font-size:13px;font-weight:700;margin-top:3px}
  hr{border:none;border-top:1px solid var(--line);margin:48px 0}
  .an{color:var(--dim);font-style:italic;border-left:3px solid var(--accent);padding-left:14px;margin:16px 0}
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .rd-hint{background:color-mix(in srgb,var(--accent) 12%,transparent);
    border:1px dashed color-mix(in srgb,var(--accent) 50%,transparent);
    border-radius:12px;padding:16px 20px;margin:0 0 30px;font-size:15px}

  /* ---------- review layer ---------- */
  /* a commented passage reads as a link (accent underline); a plain highlight
     is the marker-pen wash (amber, no rule) */
  mark.rd-hl{background:color-mix(in srgb,var(--accent) 15%,transparent);color:inherit;
    border-bottom:2px solid var(--accent);border-radius:2px;cursor:pointer;padding:0 1px}
  mark.rd-hl:hover{background:color-mix(in srgb,var(--accent) 30%,transparent)}
  mark.rd-hl.on{background:color-mix(in srgb,var(--accent) 46%,transparent)}
  mark.rd-hl.done{background:none;border-bottom:2px dotted var(--dim)}
  mark.rd-hl.mk{background:color-mix(in srgb,var(--warn) 34%,transparent);border-bottom:none}
  mark.rd-hl.mk:hover{background:color-mix(in srgb,var(--warn) 50%,transparent)}
  mark.rd-hl.mk.on{background:color-mix(in srgb,var(--warn) 68%,transparent)}
  /* inline code and fenced blocks carry an opaque background of their own, which
     paints over the wash — drop it inside a mark so a highlight spanning
     `a_variable` is not a line with gaps punched in it */
  mark.rd-hl code,mark.rd-hl pre{background:none}
  /* a panel reply can point at a section (§N) — flash it on arrival */
  section.rd-sec{border-radius:14px;transition:background-color .35s ease,box-shadow .35s ease}
  section.rd-sec.rd-flash{background-color:color-mix(in srgb,var(--accent) 11%,transparent);
    box-shadow:0 0 0 12px color-mix(in srgb,var(--accent) 11%,transparent)}
  #rd-bar{position:absolute;z-index:60;display:none;transform:translate(-50%,-100%);
    gap:1px;background:var(--line);border-radius:9px;overflow:hidden;
    box-shadow:0 4px 16px rgba(0,0,0,.35)}
  #rd-bar button{font:600 13px/1 inherit;background:var(--accent);color:#fff;border:none;
    padding:9px 13px;cursor:pointer}
  #rd-bar button:hover{filter:brightness(1.12)}
  #rd-fab{position:fixed;right:18px;bottom:18px;z-index:55;display:none;align-items:center;gap:8px;
    background:var(--accent);color:#fff;border:none;border-radius:999px;padding:13px 19px;cursor:pointer;
    font:650 14px/1 inherit;box-shadow:0 6px 22px rgba(0,0,0,.35)}
  #rd-fab .n{background:rgba(255,255,255,.28);border-radius:999px;padding:2px 8px;font-size:12px}
  #rd-panel{position:fixed;top:0;right:0;bottom:0;width:var(--panw);z-index:70;
    background:var(--card);border-left:1px solid var(--line);display:flex;flex-direction:column;
    transition:transform .22s ease}
  #rd-panel header{padding:14px 16px;margin:0;border:none;border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:10px}
  #rd-panel header b{font-size:15px;flex:1}
  #rd-panel .rd-list{flex:1;overflow:auto;padding:12px 14px}
  #rd-panel .rd-foot{padding:12px 14px;border-top:1px solid var(--line);display:grid;gap:7px}
  .rd-tabs{display:flex;gap:6px;padding:0 14px 8px}
  .rd-tabs button{flex:1;font:600 12.5px/1 inherit;background:var(--card2);color:var(--dim);
    border:1px solid var(--line);border-radius:8px;padding:8px 6px;cursor:pointer;
    display:flex;align-items:center;justify-content:center;gap:6px}
  .rd-tabs button.sel{background:var(--accent);color:#fff;border-color:var(--accent)}
  .rd-tabs .k{font-variant-numeric:tabular-nums;opacity:.75;font-size:11.5px}
  .rd-only{display:flex;align-items:center;gap:7px;padding:0 15px 10px;
    font-size:12.5px;color:var(--dim);cursor:pointer;user-select:none}
  .rd-only input{accent-color:var(--accent);margin:0;cursor:pointer}
  .rd-hlc{border:1px solid var(--line);border-left:3px solid var(--warn);border-radius:11px;
    padding:10px 12px;margin-bottom:9px;background:var(--card2);cursor:pointer}
  .rd-hlc.sel{border-color:var(--accent)}
  .rd-hlc.gone{opacity:.6;border-left-style:dashed}
  .rd-hlc .sec{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;
    margin-bottom:6px;display:flex;justify-content:space-between;gap:8px}
  .rd-hlc blockquote{margin:0;padding:0;border:none;font-size:13.5px;line-height:1.5;
    max-height:120px;overflow:hidden}
  .rd-hlc .ctx{font-size:11.5px;color:var(--dim);font-style:italic;margin-top:7px}
  .rd-x{background:none;border:1px solid var(--line);color:var(--dim);border-radius:8px;
    width:30px;height:30px;cursor:pointer;font-size:16px;line-height:1;flex:0 0 auto;display:none}
  .rd-hide{background:none;border:1px solid var(--line);color:var(--dim);border-radius:8px;
    width:30px;height:30px;cursor:pointer;font-size:15px;line-height:1;flex:0 0 auto}
  .rd-hide:hover{color:var(--ink);border-color:var(--accent)}
  .rd-grp{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);
    font-weight:700;margin:14px 0 8px;padding-bottom:5px;border-bottom:1px solid var(--line)}
  .rd-grp:first-child{margin-top:0}
  .rd-th{border:1px solid var(--line);border-radius:11px;padding:11px 12px;margin-bottom:10px;
    background:var(--card2);cursor:pointer}
  .rd-th.sel{border-color:var(--accent)}
  .rd-th .sec{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;
    margin-bottom:6px;display:flex;justify-content:space-between;gap:8px}
  .rd-th .tg{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:7px}
  .rd-chip{font-size:12px;font-weight:650;padding:2px 8px;border-radius:999px;
    background:var(--card);border:1px solid var(--line)}
  .rd-th blockquote{margin:0 0 9px;padding-left:10px;border-left:3px solid var(--accent);
    color:var(--dim);font-size:13px;font-style:italic;max-height:96px;overflow:hidden}
  .rd-msg{border-left:3px solid var(--you);padding:0 0 0 10px;margin:0 0 9px}
  .rd-msg.claude{border-left-color:var(--accent)}
  .rd-msg .who{font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
    color:var(--you);margin-bottom:2px;display:flex;align-items:center;gap:8px}
  .rd-msg.claude .who{color:var(--accent)}
  .rd-msg .who .sp{flex:1}
  .rd-msg .who button{background:none;border:none;padding:0;cursor:pointer;color:var(--dim);
    font:700 10px/1 inherit;letter-spacing:.05em;text-transform:uppercase;text-decoration:underline}
  .rd-msg .who button:hover{color:var(--ink)}
  .rd-msg .bd{font-size:14px;white-space:pre-wrap;line-height:1.55}
  .rd-msg .bd a{cursor:pointer}
  .rd-draft{font-size:10px;font-weight:700;color:var(--warn);letter-spacing:.04em}
  .rd-fold-btn{display:inline-flex;align-items:center;gap:5px;font:650 11.5px/1 inherit;
    color:var(--accent);background:var(--card);border:1px solid var(--accent);
    border-radius:999px;padding:4px 10px;cursor:pointer}
  .rd-fold-btn:hover{background:var(--accent);color:#fff}
  .rd-acts{display:flex;gap:8px;margin-top:8px}
  .rd-acts button{background:none;border:none;color:var(--dim);cursor:pointer;font:600 12px/1 inherit;
    padding:2px 0;text-decoration:underline}
  .rd-rep{margin-top:8px;display:none}
  .rd-rep.open{display:block}
  .rd-rep textarea{width:100%;min-height:64px;background:var(--bg);color:var(--ink);
    border:1px solid var(--line);border-radius:8px;padding:8px;font:14px/1.5 inherit;resize:vertical}
  .rd-th.resolved{opacity:.55}
  .rd-th.resolved.fold blockquote,.rd-th.resolved.fold .rd-msg,.rd-th.resolved.fold .rd-acts,
  .rd-th.resolved.fold .rd-rep{display:none}
  .rd-st{font-size:10.5px;font-weight:700;color:var(--good);letter-spacing:.05em;
    background:none;border:none;padding:0;cursor:pointer;font-family:inherit}
  .rd-st:hover{text-decoration:underline}
  .rd-tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
  .rd-tags button{font:600 12.5px/1 inherit;background:var(--card2);color:var(--ink);
    border:1px solid var(--line);border-radius:999px;padding:7px 12px;cursor:pointer}
  .rd-tags button.sel{background:var(--accent);color:#fff;border-color:var(--accent)}
  #rd-ed{position:fixed;inset:auto 12px 12px 12px;max-width:520px;margin:0 auto;z-index:80;
    background:var(--card);border:1px solid var(--line);border-radius:15px;padding:16px;display:none;
    box-shadow:0 12px 40px rgba(0,0,0,.45)}
  #rd-ed.open{display:block}
  #rd-ed blockquote{margin:0 0 11px;padding-left:11px;border-left:3px solid var(--accent);
    color:var(--dim);font-size:13.5px;font-style:italic;max-height:78px;overflow:auto}
  #rd-ed textarea{width:100%;min-height:88px;background:var(--bg);color:var(--ink);
    border:1px solid var(--line);border-radius:9px;padding:11px;font:15px/1.5 inherit;resize:vertical}
  #rd-ed .row{display:flex;gap:8px;margin-top:11px}
  #rd-ed .row button{flex:1;font:650 14px/1 inherit;padding:12px;border-radius:9px;cursor:pointer;
    border:1px solid var(--line)}
  #rd-ed .row .ok{background:var(--accent);color:#fff;border-color:var(--accent)}
  #rd-ed .row .no{background:var(--card2);color:var(--ink)}
  .rd-big{font:650 14px/1 inherit;padding:13px;border-radius:10px;cursor:pointer;
    background:var(--good);color:#fff;border:none;width:100%}
  .rd-lite{font:600 13px/1 inherit;padding:11px;border-radius:10px;cursor:pointer;
    background:var(--card2);color:var(--ink);border:1px solid var(--line);width:100%}
  #rd-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:90;
    background:var(--good);color:#fff;padding:12px 20px;border-radius:10px;font:650 14px/1 inherit;
    display:none;box-shadow:0 6px 22px rgba(0,0,0,.35);text-align:center;max-width:88vw}
  @media(min-width:901px){
    body{padding-right:var(--panw);transition:padding-right .22s ease}
    /* panel collapsed: drop the gutter so .wrap centres on the full viewport */
    body.rd-nopanel{padding-right:0}
    body.rd-nopanel #rd-panel{transform:translateX(101%)}
    body.rd-nopanel #rd-fab{display:flex}
  }
  @media(max-width:900px){
    #rd-panel{width:min(400px,100%);transform:translateX(101%);
      box-shadow:-8px 0 30px rgba(0,0,0,.3)}
    #rd-panel.open{transform:none}
    #rd-fab{display:flex}
    .rd-x{display:block}
    .rd-hide{display:none}
  }
  @media print{
    #rd-fab,#rd-panel,#rd-bar,#rd-ed,#rd-toast,.rd-hint{display:none!important}
    body{padding-right:0}
    mark.rd-hl{background:none;border-bottom:1px solid #999}
  }
"""

JS = r"""
(function(){
  "use strict";
  var DOC = __DOC__;
  var TAGS = __TAGS__;
  var NO_BODY_TAGS = __NO_BODY_TAGS__;
  var KEY = 'reviewdoc:' + DOC.slug;

  var embedded = [];
  try{ embedded = JSON.parse(document.getElementById('rd-notes').textContent || '[]') || []; }
  catch(e){ embedded = []; }

  var drafts = {notes:[], replies:[], patch:{}};
  try{
    var raw = JSON.parse(localStorage.getItem(KEY) || 'null');
    if(raw && raw.notes) drafts = {notes:raw.notes||[], replies:raw.replies||[], patch:raw.patch||{}};
  }catch(e){}

  /* once a draft shows up in the embedded block it must never resurrect */
  var byId = {};
  embedded.forEach(function(n){ byId[n.id] = n; });
  drafts.notes = drafts.notes.filter(function(n){ return !byId[n.id]; });
  drafts.replies = drafts.replies.filter(function(r){
    var host = byId[r.noteId];
    if(!host) return true;
    return !(host.replies||[]).some(function(x){
      return x.ts === r.ts && x.body === r.body && x.author === r.author;
    });
  });
  /* same for an edit / resolve the embedded block has already caught up with */
  Object.keys(drafts.patch).forEach(function(id){
    var host = byId[id], p = drafts.patch[id];
    if(!host) return;
    var live = Object.keys(p).some(function(k){
      return (host[k] || (k === 'status' ? 'open' : '')) !== p[k];
    });
    if(!live) delete drafts.patch[id];
  });
  saveDrafts();

  var wrap = document.querySelector('.wrap'),
      bar = document.getElementById('rd-bar'),
      fab = document.getElementById('rd-fab'),
      panel = document.getElementById('rd-panel'),
      list = panel.querySelector('.rd-list'),
      onlyBox = panel.querySelector('[data-openonly]'),
      ed = document.getElementById('rd-ed'),
      edTags = ed.querySelector('.rd-tags'),
      edQuote = ed.querySelector('blockquote'),
      edText = ed.querySelector('textarea'),
      edSave = ed.querySelector('[data-save]'),
      toastEl = document.getElementById('rd-toast');

  var pending = null, editing = null, pendTag = 'question', tab = 'notes', openOnly = false;

  function saveDrafts(){
    try{
      if(!drafts.notes.length && !drafts.replies.length && !Object.keys(drafts.patch).length)
        localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, JSON.stringify(
        {v:1, notes:drafts.notes, replies:drafts.replies, patch:drafts.patch}));
    }catch(e){}
  }
  /* an edit, a resolve or a delete on a thread that is already in the document
     is held here as an override until the user exports and Claude imports it */
  function patch(id, obj){
    var p = drafts.patch[id] || (drafts.patch[id] = {});
    for(var k in obj) p[k] = obj[k];
    saveDrafts(); reMark(id); render();
  }
  /* redraw one mark after its kind / status / deleted-ness changed */
  function reMark(id){
    var t = null, all = threads();
    for(var i=0;i<all.length;i++) if(all[i].id === id) t = all[i];
    unmark(id);
    if(t && t.status !== 'deleted') anchor(t);
  }
  function uid(){ return 'u' + Date.now().toString(36) + Math.random().toString(36).slice(2,6); }
  function label(k){ return TAGS[k] || k; }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function toast(m){ toastEl.textContent = m; toastEl.style.display = 'block';
    clearTimeout(toast._t); toast._t = setTimeout(function(){ toastEl.style.display='none'; }, 2800); }

  /* ---------- anchoring (char offsets over a section's text nodes) ---------- */
  var sections = [].slice.call(document.querySelectorAll('section.rd-sec'));
  var secIndex = {};
  sections.forEach(function(s,i){ secIndex[s.id] = i; });

  function secNodes(sec){
    var out = [], w = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n){
        if(!n.data || !n.data.trim()) return 2;
        var p = n.parentElement;
        if(!p) return 2;
        if(p.closest('#rd-panel,#rd-ed,#rd-bar,#rd-toast,script,style')) return 2;
        if(p.namespaceURI === 'http://www.w3.org/2000/svg') return 2;
        return 1;
      }
    }), n;
    while((n = w.nextNode())) out.push(n);
    return out;
  }
  /* Character offset of a boundary point within a section's filtered text.
     A selection that begins right at a tag edge — which is what you get when the
     line starts with a `code` span — reports its boundary as the *element* plus a
     child index rather than as a text node, so identity matching alone finds
     nothing and the note ends up with no offset at all. Fall back to document
     order in that case. */
  function offsetOf(nodes, container, off){
    var acc = 0, i;
    if(container.nodeType === 3){
      for(i=0;i<nodes.length;i++){
        if(nodes[i] === container) return acc + off;
        acc += nodes[i].data.length;
      }
    }
    var probe = document.createRange();
    try{ probe.setStart(container, off); probe.collapse(true); }
    catch(e){ return -1; }
    acc = 0;
    for(i=0;i<nodes.length;i++){
      var cmp;
      try{ cmp = probe.comparePoint(nodes[i], nodes[i].data.length); }
      catch(e){ return -1; }
      if(cmp >= 0) return acc;      /* the point is at or before this node's end */
      acc += nodes[i].data.length;
    }
    return acc;
  }
  /* Wrap [start, start+len) of a section in <mark>s. One surroundContents() over
     the whole range throws whenever the range begins or ends inside an inline
     element it does not fully contain — again, a selection starting on a `code`
     span — so wrap each text node it touches separately, all sharing the note id. */
  function mark(nodes, start, len, id, resolved, isHl){
    var acc = 0, stop = start + len, hits = [], i;
    for(i=0;i<nodes.length && acc < stop;i++){
      var L = nodes[i].data.length,
          s = Math.max(start - acc, 0), e = Math.min(stop - acc, L);
      if(s < e) hits.push([nodes[i], s, e]);
      acc += L;
    }
    if(!hits.length) return false;
    var cls = 'rd-hl' + (isHl ? ' mk' : '') + (resolved ? ' done' : ''), ok = false;
    for(i = hits.length - 1; i >= 0; i--){
      var r = document.createRange(), m = document.createElement('mark');
      m.className = cls; m.dataset.noteId = id;
      try{
        r.setStart(hits[i][0], hits[i][1]); r.setEnd(hits[i][0], hits[i][2]);
        r.surroundContents(m);
        ok = true;
      }catch(err){}
    }
    return ok;
  }
  function anchor(n){
    if(!n.quote || !n.sectionId) return false;
    var sec = document.getElementById(n.sectionId);
    if(!sec || typeof n.start !== 'number') return false;
    var nodes = secNodes(sec), text = '';
    for(var i=0;i<nodes.length;i++) text += nodes[i].data;
    if(text.slice(n.start, n.start + n.quote.length) !== n.quote) return false;
    return mark(nodes, n.start, n.quote.length, n.id,
                n.status === 'resolved', n.kind === 'highlight');
  }
  /* a note can own several marks now (one per text node it spans) */
  function unmark(id){
    var ms = document.querySelectorAll('mark.rd-hl[data-note-id="' + id + '"]');
    for(var i=0;i<ms.length;i++){
      var m = ms[i], p = m.parentNode;
      while(m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m); p.normalize();
    }
  }

  /* ---------- merged view ---------- */
  function threads(){
    var out = embedded.map(function(n){
      var t = {};
      for(var k in n) t[k] = n[k];
      t.replies = (n.replies || []).slice();
      t._draft = false;
      return t;
    });
    drafts.notes.forEach(function(n){
      var t = {};
      for(var k in n) t[k] = n[k];
      t.replies = (n.replies || []).slice();
      t._draft = true;
      out.push(t);
    });
    var idx = {};
    out.forEach(function(t){ idx[t.id] = t; });
    drafts.replies.forEach(function(r){
      var host = idx[r.noteId];
      if(host) host.replies.push({author:r.author, body:r.body, ts:r.ts, _draft:true});
    });
    out.forEach(function(t){
      var p = drafts.patch[t.id];
      if(p){ for(var k in p) t[k] = p[k]; t._patched = true; }
      t.replies.sort(function(a,b){ return String(a.ts) < String(b.ts) ? -1 : 1; });
      t._anchored = !!document.querySelector('mark.rd-hl[data-note-id="' + t.id + '"]');
    });
    out.sort(function(a,b){
      var ai = a.sectionId in secIndex ? secIndex[a.sectionId] : 999,
          bi = b.sectionId in secIndex ? secIndex[b.sectionId] : 999;
      if(ai !== bi) return ai - bi;
      return (a.start == null ? 1e9 : a.start) - (b.start == null ? 1e9 : b.start);
    });
    return out;
  }

  function card(t){
    var d = document.createElement('article');
    d.className = 'rd-th' + (t.status === 'resolved' ? ' resolved fold' : '');
    d.dataset.id = t.id;
    var h = '<div class="sec"><span>' + esc(t.sectionTitle || 'General') + '</span>' +
            (t.status === 'resolved' ? '<button type="button" class="rd-st" data-status ' +
               'title="Reopen this thread">✓ resolved</button>' : '') + '</div>';
    h += '<div class="tg"><span class="rd-chip">' + esc(label(t.tag)) + '</span>' +
         (t._draft || t._patched ? '<span class="rd-draft">not exported</span>' : '') +
         (t.status === 'resolved' ? '<button type="button" class="rd-fold-btn" data-fold>' +
            foldTxt(true, t.replies.length) + '</button>' : '') + '</div>';
    if(t.quote) h += '<blockquote>' + esc(t.quote) + '</blockquote>';
    if(t.body) h += msg('user', t.body, t._draft, 'note', '');
    t.replies.forEach(function(r){
      h += msg(r.author === 'claude' ? 'claude' : 'user', r.body, r._draft,
               r._draft ? 'reply' : '', r.ts);
    });
    h += '<div class="rd-acts"><button type="button" data-reply>Reply</button>' +
         '<button type="button" data-status>' +
           (t.status === 'resolved' ? 'Reopen' : 'Mark resolved') + '</button>' +
         (t._draft ? '<button type="button" data-del>Delete</button>' : '') + '</div>';
    h += '<div class="rd-rep"><textarea placeholder="Reply to this thread…"></textarea>' +
         '<button class="rd-lite" type="button" data-send style="margin-top:6px">Save reply</button></div>';
    d.innerHTML = h;

    d.addEventListener('click', function(e){
      var el = e.target;
      if(el.closest('.rd-rep')) return;
      var foldBtn = el.closest && el.closest('[data-fold]');
      if(foldBtn){
        e.stopPropagation();
        d.classList.toggle('fold');
        foldBtn.textContent = foldTxt(d.classList.contains('fold'), t.replies.length);
        return;
      }
      var link = el.closest && el.closest('.rd-jump');
      if(link){ e.stopPropagation(); e.preventDefault(); gotoSec(link); return; }
      var edBtn = el.closest && el.closest('[data-edit]');
      if(edBtn){
        e.stopPropagation();
        if(edBtn.dataset.edit === 'note') openEd(t.quote, t);
        else editReply(d, t, edBtn.dataset.ts);
        return;
      }
      var dropBtn = el.closest && el.closest('[data-drop]');
      if(dropBtn){ e.stopPropagation(); dropReply(t.id, dropBtn.dataset.ts); return; }
      if(el.hasAttribute && el.hasAttribute('data-status')){
        e.stopPropagation();
        var to = t.status === 'resolved' ? 'open' : 'resolved';
        patch(t.id, {status:to});
        toast(to === 'resolved' ? 'Marked resolved — export when you are done' : 'Reopened');
        return;
      }
      if(el.hasAttribute && el.hasAttribute('data-reply')){
        e.stopPropagation();
        var box = d.querySelector('.rd-rep');
        box.classList.toggle('open');
        if(box.classList.contains('open')) box.querySelector('textarea').focus();
        return;
      }
      if(el.hasAttribute && el.hasAttribute('data-del')){
        e.stopPropagation(); removeDraft(t.id); return;
      }
      jump(t.id);
    });
    d.querySelector('[data-send]').addEventListener('click', function(){
      var box = d.querySelector('.rd-rep'), ta = box.querySelector('textarea'),
          body = ta.value.trim();
      if(!body){ ta.focus(); return; }
      var was = box.dataset.editTs, i = was ? draftReply(t.id, was) : -1;
      if(i >= 0) drafts.replies[i].body = body;
      else drafts.replies.push({noteId:t.id, author:'user', body:body, ts:new Date().toISOString()});
      saveDrafts(); render();
      toast(i >= 0 ? 'Reply updated' : 'Reply saved as draft — export when done');
    });
    return d;
  }
  function msg(who, body, draft, ref, ts){
    var acts = '';
    if(ref) acts = '<span class="sp"></span><button type="button" data-edit="' + ref +
      '" data-ts="' + esc(ts || '') + '">edit</button>' +
      (ref === 'reply' ? '<button type="button" data-drop data-ts="' + esc(ts || '') +
         '">delete</button>' : '');
    return '<div class="rd-msg ' + who + '"><div class="who"><span>' +
      (who === 'claude' ? 'Claude' : 'You') + (draft ? ' · draft' : '') + '</span>' + acts +
      '</div><div class="bd">' + linkify(esc(body)) + '</div></div>';
  }
  /* §N and [label](#sec-id) in a message body become jumps into the document,
     so a reply can point at prose instead of repeating it */
  var JUMP_RE = /\[([^\]\n]{1,80})\]\(#([A-Za-z0-9_-]+)\)|§\s?(\d+)/g;
  function linkify(s){
    return s.replace(JUMP_RE, function(m, txt, id, num){
      if(id) return '<a class="rd-jump" data-sec="' + id + '">' + txt + '</a>';
      return '<a class="rd-jump" data-num="' + num + '">§' + num + '</a>';
    });
  }
  function secByNum(n){
    var els = document.querySelectorAll('section.rd-sec h2 .num');
    for(var i=0;i<els.length;i++)
      if(els[i].textContent.trim() === String(n)) return els[i].closest('section.rd-sec');
    return null;
  }
  function gotoSec(a){
    var sec = a.dataset.sec ? document.getElementById(a.dataset.sec) : secByNum(a.dataset.num);
    if(!sec){ toast('That section is not in this document'); return; }
    if(window.matchMedia('(max-width:900px)').matches) panel.classList.remove('open');
    sec.scrollIntoView({behavior:'smooth', block:'start'});
    sec.classList.add('rd-flash');
    setTimeout(function(){ sec.classList.remove('rd-flash'); }, 1500);
  }
  function draftReply(noteId, ts){
    for(var i=0;i<drafts.replies.length;i++)
      if(drafts.replies[i].noteId === noteId && drafts.replies[i].ts === ts) return i;
    return -1;
  }
  function editReply(d, t, ts){
    var i = draftReply(t.id, ts);
    if(i < 0) return;
    var box = d.querySelector('.rd-rep'), ta = box.querySelector('textarea');
    box.classList.add('open');
    box.dataset.editTs = ts;
    ta.value = drafts.replies[i].body;
    d.querySelector('[data-send]').textContent = 'Update reply';
    ta.focus();
  }
  function dropReply(noteId, ts){
    drafts.replies = drafts.replies.filter(function(r){
      return !(r.noteId === noteId && r.ts === ts);
    });
    saveDrafts(); render();
  }
  function foldTxt(folded, nrep){
    if(!folded) return '▴ hide';
    return '▾ show ' + (nrep === 1 ? '1 reply' : nrep + ' replies');
  }

  /* ---------- the highlights tab ---------- */
  function hlCard(t){
    var d = document.createElement('article');
    d.className = 'rd-hlc' + (t._anchored ? '' : ' gone');
    d.dataset.id = t.id;
    var h = '<div class="sec"><span>' + esc(t.sectionTitle || 'General') + '</span>' +
            (t._draft || t._patched ? '<span class="rd-draft">not exported</span>' : '') + '</div>';
    h += '<blockquote>' + esc(t.quote) + '</blockquote>';
    if(!t._anchored)
      h += '<div class="ctx">not in the text any more' +
           (t.context ? ' &middot; was in &ldquo;' + esc(t.context) + '&rdquo;' : '') + '</div>';
    h += '<div class="rd-acts"><button type="button" data-note>Add a note</button>' +
         '<button type="button" data-del>Delete</button></div>';
    d.innerHTML = h;
    d.addEventListener('click', function(e){
      var el = e.target;
      if(el.hasAttribute && el.hasAttribute('data-note')){
        e.stopPropagation(); openEd(t.quote, t); return;
      }
      if(el.hasAttribute && el.hasAttribute('data-del')){
        e.stopPropagation(); drop(t); return;
      }
      jump(t.id);
    });
    return d;
  }
  /* a draft is simply forgotten; anything already in the document has to travel
     back to Claude as a tombstone, so import can take it out */
  function drop(t){
    if(t._draft) removeDraft(t.id);
    else { patch(t.id, {status:'deleted'}); toast('Removed — export when you are done'); }
  }

  function setTab(t){ tab = t; syncTabs(); }
  function syncTabs(){
    [].forEach.call(panel.querySelectorAll('.rd-tabs button'), function(b){
      b.classList.toggle('sel', b.dataset.tab === tab);
    });
    onlyBox.parentNode.style.display = tab === 'notes' ? 'flex' : 'none';
  }

  function render(){
    var all = threads().filter(function(t){ return t.status !== 'deleted'; }),
        notes = all.filter(function(t){ return t.kind !== 'highlight'; }),
        hls = all.filter(function(t){ return t.kind === 'highlight'; });
    fab.querySelector('.n').textContent =
      notes.filter(function(t){ return t.status !== 'resolved'; }).length;
    count('notes', notes.length); count('hl', hls.length);
    list.innerHTML = '';
    if(tab === 'hl') return fill(hls, hlCard,
      'No highlights yet.<br><br>Select any text and press <b>🖍 Highlight</b> to mark a ' +
      'passage without writing anything. Highlights stay in this tab even if the wording ' +
      'later changes, so nothing you flagged gets lost.',
      'Not in the text any more');
    fill(notes.filter(function(t){ return !openOnly || t.status !== 'resolved'; }), card,
      'No notes yet.<br><br>Select any text in the document and a <b>💬 Note</b> button ' +
      'appears. Tag it as a question, a change, a concern, or “I don’t understand this” — ' +
      'that last one is the most useful of all.',
      'Unanchored — the quoted text has changed');

    function count(k, n){
      panel.querySelector('.rd-tabs [data-tab="' + k + '"] .k').textContent = n;
    }
    function fill(rows, make, empty, looseTitle){
      if(!rows.length){ list.innerHTML = '<p class="small">' + empty + '</p>'; return; }
      var seen = null;
      rows.filter(function(t){ return t._anchored || !t.quote; }).forEach(function(t){
        var title = t.sectionTitle || 'General';
        if(title !== seen){ seen = title; add('rd-grp', title); }
        list.appendChild(make(t));
      });
      var loose = rows.filter(function(t){ return !t._anchored && t.quote; });
      if(loose.length){
        add('rd-grp', looseTitle);
        loose.forEach(function(t){ list.appendChild(make(t)); });
      }
    }
    function add(cls, text){
      var e = document.createElement('div'); e.className = cls; e.textContent = text;
      list.appendChild(e);
    }
  }

  function removeDraft(id){
    drafts.notes = drafts.notes.filter(function(n){ return n.id !== id; });
    drafts.replies = drafts.replies.filter(function(r){ return r.noteId !== id; });
    delete drafts.patch[id];
    saveDrafts(); unmark(id); render();
  }
  function jump(id){
    var ms = document.querySelectorAll('mark.rd-hl[data-note-id="' + id + '"]');
    if(!ms.length){ toast('This note is no longer anchored to any text'); return; }
    if(window.matchMedia('(max-width:900px)').matches) panel.classList.remove('open');
    ms[0].scrollIntoView({behavior:'smooth', block:'center'});
    for(var i=0;i<ms.length;i++) ms[i].classList.add('on');
    setTimeout(function(){
      for(var j=0;j<ms.length;j++) ms[j].classList.remove('on');
    }, 1900);
  }
  function focusThread(id){
    if(collapsed()) collapse(false);
    var sel = '[data-id="' + id + '"]', c = list.querySelector(sel);
    if(!c){
      var t = null, all = threads();
      for(var i=0;i<all.length;i++) if(all[i].id === id) t = all[i];
      if(!t) return;
      setTab(t.kind === 'highlight' ? 'hl' : 'notes');
      if(openOnly){ openOnly = false; onlyBox.checked = false; }
      render();
      c = list.querySelector(sel);
    }
    if(!c) return;
    panel.classList.add('open');
    c.scrollIntoView({block:'center'});
    c.classList.add('sel');
    setTimeout(function(){ c.classList.remove('sel'); }, 1900);
  }

  /* ---------- selection -> highlight or note ---------- */
  function hideBar(){ bar.style.display = 'none'; }
  function onSel(){
    if(ed.classList.contains('open')) return;
    var s = window.getSelection();
    if(!s || s.isCollapsed || !s.rangeCount) return hideBar();
    var r = s.getRangeAt(0), txt = r.toString().trim();
    if(txt.length < 2) return hideBar();
    if(!wrap.contains(r.commonAncestorContainer)) return hideBar();
    var b = r.getBoundingClientRect();
    if(!b.width && !b.height) return hideBar();
    bar.style.display = 'flex';
    bar.style.left = (b.left + b.width/2 + window.scrollX) + 'px';
    bar.style.top = (b.top + window.scrollY - 9) + 'px';
  }
  document.addEventListener('mouseup', function(){ setTimeout(onSel, 10); });
  document.addEventListener('touchend', function(){ setTimeout(onSel, 10); });
  document.addEventListener('scroll', hideBar, {passive:true});

  /* everything both buttons need: the text, where it sits, and enough context to
     still make sense in the panel if the prose it came from is rewritten */
  function capture(){
    var s = window.getSelection();
    if(!s || !s.rangeCount) return null;
    var r = s.getRangeAt(0), quote = r.toString().trim();
    if(quote.length < 2) return null;
    var sc = r.startContainer, el = sc.nodeType === 1 ? sc : sc.parentElement;
    var host = el && el.closest ? el.closest('section.rd-sec') : null;
    var blk = el && el.closest ? el.closest('p,li,td,th,h2,h3,pre,blockquote,.an,.t') : null;
    var nodes = host ? secNodes(host) : [];
    var start = host ? offsetOf(nodes, r.startContainer, r.startOffset) : -1;
    var lead = r.toString().indexOf(quote);
    if(start >= 0 && lead > 0) start += lead;
    return { quote:quote, sectionId:host ? host.id : null,
             sectionTitle:host ? (host.dataset.title || '') : 'General',
             context: blk ? blk.textContent.replace(/\s+/g,' ').trim().slice(0, 90) : '',
             start: start >= 0 ? start : null };
  }
  function clearSel(){
    var s = window.getSelection();
    if(s) s.removeAllRanges();
    hideBar();
  }
  bar.querySelector('[data-note]').addEventListener('click', function(){
    pending = capture();
    if(!pending) return;
    openEd(pending.quote);
    clearSel();
  });
  bar.querySelector('[data-hl]').addEventListener('click', function(){
    var p = capture();
    if(!p) return;
    var n = { id:uid(), kind:'highlight', tag:'highlight', quote:p.quote,
              sectionId:p.sectionId, sectionTitle:p.sectionTitle, context:p.context,
              start:p.start, body:'', author:'user', ts:new Date().toISOString(),
              replies:[], status:'open', anchored:true };
    drafts.notes.push(n); saveDrafts();
    anchor(n);
    clearSel(); render(); toast('Highlighted — export when you are done');
  });

  Object.keys(TAGS).forEach(function(k){
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = TAGS[k]; b.dataset.k = k;
    b.addEventListener('click', function(){ selTag(k); });
    edTags.appendChild(b);
  });
  function selTag(k){
    pendTag = k;
    [].forEach.call(edTags.querySelectorAll('button'), function(x){
      x.classList.toggle('sel', x.dataset.k === k);
    });
  }
  function openEd(quote, note){
    editing = note || null;
    var conv = !!note && note.kind === 'highlight';   /* highlight -> note */
    selTag(note && TAGS[note.tag] ? note.tag : 'question');
    if(quote){ edQuote.textContent = '“' + quote + '”'; edQuote.style.display = 'block'; }
    else edQuote.style.display = 'none';
    edText.value = note ? (note.body || '') : '';
    edSave.textContent = note && !conv ? 'Update note' : 'Save note';
    ed.classList.add('open');
    setTimeout(function(){ edText.focus(); }, 50);
  }
  function closeEd(){ ed.classList.remove('open'); pending = null; editing = null; }
  ed.querySelector('[data-cancel]').addEventListener('click', closeEd);
  edSave.addEventListener('click', function(){
    var body = edText.value.trim();
    if(!body && NO_BODY_TAGS.indexOf(pendTag) < 0){ edText.focus(); return; }
    if(editing){
      var id = editing.id, was = editing.kind, p = {tag:pendTag, body:body};
      if(was === 'highlight') p.kind = 'note';
      closeEd();
      patch(id, p);
      if(was === 'highlight'){ setTab('notes'); render(); focusThread(id); }
      toast(was === 'highlight' ? 'Now a note — export when you are done'
                                : 'Note updated — export when you are done');
      return;
    }
    var n = { id:uid(), kind:'note', tag:pendTag, quote:pending ? pending.quote : '',
              sectionId:pending ? pending.sectionId : null,
              sectionTitle:pending ? pending.sectionTitle : 'General',
              context:pending ? pending.context : '',
              start:pending ? pending.start : null, body:body, author:'user',
              ts:new Date().toISOString(), replies:[], status:'open', anchored:true };
    drafts.notes.push(n); saveDrafts();
    anchor(n);
    closeEd(); setTab('notes'); render(); toast('Note saved — export when you are done');
  });
  edText.addEventListener('keydown', function(e){
    if((e.metaKey || e.ctrlKey) && e.key === 'Enter') edSave.click();
  });

  document.addEventListener('click', function(e){
    var m = e.target.closest && e.target.closest('mark.rd-hl');
    if(m) focusThread(m.dataset.noteId);
  });
  /* ---------- panel collapse (desktop) + view state ---------- */
  var UIKEY = KEY + ':ui', ui = {};
  try{
    var rawUi = localStorage.getItem(UIKEY);
    if(rawUi === 'hidden') ui = {c:1};               /* pre-tabs format */
    else if(rawUi) ui = JSON.parse(rawUi) || {};
  }catch(e){ ui = {}; }
  function saveUi(){ try{ localStorage.setItem(UIKEY, JSON.stringify(ui)); }catch(e){} }
  function wide(){ return window.matchMedia('(min-width:901px)').matches; }
  function collapsed(){ return document.body.classList.contains('rd-nopanel'); }
  function collapse(on){
    document.body.classList.toggle('rd-nopanel', !!on);
    ui.c = on ? 1 : 0; saveUi();
  }
  if(ui.c) document.body.classList.add('rd-nopanel');
  openOnly = !!ui.o;                                  /* the tab itself never persists:
                                                         always land on Notes after a rebuild */
  panel.querySelector('[data-collapse]').addEventListener('click', function(){ collapse(true); });

  fab.addEventListener('click', function(){
    if(wide() && collapsed()){ collapse(false); return; }
    panel.classList.toggle('open');
  });
  panel.querySelector('[data-close]').addEventListener('click', function(){
    panel.classList.remove('open');
  });
  panel.querySelector('[data-general]').addEventListener('click', function(){
    pending = null; openEd('');
  });
  document.addEventListener('keydown', function(e){
    if(e.key !== 'Escape') return;
    if(ed.classList.contains('open')) closeEd();
    else panel.classList.remove('open');
  });
  [].forEach.call(panel.querySelectorAll('.rd-tabs button'), function(b){
    b.addEventListener('click', function(){ setTab(b.dataset.tab); render(); });
  });
  onlyBox.checked = openOnly;
  onlyBox.addEventListener('change', function(){
    openOnly = onlyBox.checked; ui.o = openOnly ? 1 : 0; saveUi(); render();
  });

  /* ---------- export ---------- */
  function payload(){
    return threads().map(function(t){
      return { id:t.id, kind:t.kind || 'note', tag:t.tag, quote:t.quote,
               sectionId:t.sectionId, sectionTitle:t.sectionTitle,
               context:t.context || '', start:t.start, body:t.body,
               author:t.author || 'user', ts:t.ts, status:t.status || 'open',
               anchored: t._anchored,
               replies: t.replies.map(function(r){
                 return {author:r.author, body:r.body, ts:r.ts};
               }) };
    });
  }
  function download(text, name, type){
    var b = new Blob([text], {type:type}), u = URL.createObjectURL(b),
        a = document.createElement('a');
    a.href = u; a.download = name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(u); }, 1500);
  }
  // Prefer a native save dialog (File System Access API) so notes can land in
  // the project folder Claude is reading, not just ~/Downloads. Chrome remembers
  // the last-used directory via the `id`. Falls back to a plain download when the
  // API is missing or refuses (e.g. some file:// origins). Resolves to one of
  // 'picked' | 'download' | 'cancel'.
  function saveNotes(text, name){
    if(window.showSaveFilePicker){
      return (async function(){
        var handle;
        try{
          handle = await window.showSaveFilePicker({
            suggestedName: name, id: 'reviewdoc-notes',
            types: [{description:'Notes JSON', accept:{'application/json':['.json']}}]
          });
        }catch(err){
          if(err && err.name === 'AbortError') return 'cancel';
          download(text, name, 'application/json'); return 'download';
        }
        try{
          var w = await handle.createWritable();
          await w.write(text); await w.close();
          return 'picked';
        }catch(err){
          download(text, name, 'application/json'); return 'download';
        }
      })();
    }
    download(text, name, 'application/json');
    return Promise.resolve('download');
  }
  function toMd(){
    var all = payload().filter(function(t){ return t.status !== 'deleted'; }),
        ns = all.filter(function(t){ return t.kind !== 'highlight'; }),
        hs = all.filter(function(t){ return t.kind === 'highlight'; });
    var L = ['# Review notes — ' + DOC.title, '',
      'Source: `' + DOC.file + '`',
      'Threads: **' + ns.length + '** · highlights: **' + hs.length + '**', '', '---', ''];
    var seen = null;
    ns.forEach(function(t){
      if(t.sectionTitle !== seen){ seen = t.sectionTitle; L.push('## ' + (seen || 'General'), ''); }
      L.push('**' + label(t.tag) + '**  `' + t.id + '`' +
             (t.status === 'resolved' ? ' _(resolved)_' : ''), '');
      if(t.quote) L.push('> ' + t.quote.replace(/\s*\n\s*/g, ' '), '');
      L.push(t.body, '');
      t.replies.forEach(function(r){
        L.push('- **' + (r.author === 'claude' ? 'Claude' : 'You') + ':** ' +
               r.body.replace(/\s*\n\s*/g, ' '), '');
      });
    });
    if(hs.length){
      L.push('---', '', '## Highlights (no comment attached)', '');
      seen = null;
      hs.forEach(function(t){
        if(t.sectionTitle !== seen){ seen = t.sectionTitle; L.push('**' + (seen || 'General') + '**', ''); }
        L.push('- ' + t.quote.replace(/\s*\n\s*/g, ' ') +
               (t.anchored ? '' : '  _(no longer in the text)_'));
      });
      L.push('');
    }
    return L.join('\n');
  }
  panel.querySelector('[data-export]').addEventListener('click', function(){
    var ts = payload();
    if(!ts.length){ toast('Nothing to export yet'); return; }
    Promise.resolve(saveNotes(JSON.stringify(ts, null, 1), DOC.slug + '-notes.json'))
      .then(function(how){
        if(how === 'cancel') return;
        toast((how === 'picked' ? 'Saved' : 'Saved to Downloads') +
              ' — now tell Claude "notes exported"');
      });
  });
  panel.querySelector('[data-copy]').addEventListener('click', function(){
    if(!payload().length){ toast('Nothing to copy yet'); return; }
    var md = toMd();
    function fallback(){
      var t = document.createElement('textarea'); t.value = md;
      t.style.position = 'fixed'; t.style.opacity = '0';
      document.body.appendChild(t); t.select();
      try{ document.execCommand('copy'); }catch(e){}
      document.body.removeChild(t); toast('Copied — paste it to Claude');
    }
    if(navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(md).then(function(){ toast('Copied — paste it to Claude'); }, fallback);
    else fallback();
  });

  /* one pass over the merged view, so a locally-deleted mark never comes back
     and highlights get the marker-pen class rather than the note one */
  threads().forEach(function(t){ if(t.status !== 'deleted') anchor(t); });
  syncTabs();
  render();
})();
"""

PANEL = """
<div id="rd-bar">
  <button type="button" data-hl>&#128396; Highlight</button>
  <button type="button" data-note>&#128172; Note</button>
</div>

<button id="rd-fab" type="button">&#128221; Review <span class="n">0</span></button>

<aside id="rd-panel" aria-label="Review comments">
  <header><b>Review</b>
    <button class="rd-hide" type="button" data-collapse title="Hide panel &mdash; centre the document">&#8677;</button>
    <button class="rd-x" type="button" data-close>&#10005;</button></header>
  <div class="rd-tabs">
    <button type="button" data-tab="notes">Notes <span class="k">0</span></button>
    <button type="button" data-tab="hl">Highlights <span class="k">0</span></button>
  </div>
  <label class="rd-only"><input type="checkbox" data-openonly> Show open only</label>
  <div class="rd-list"></div>
  <div class="rd-foot">
    <button class="rd-big" type="button" data-export>&#10003; Export notes for Claude</button>
    <button class="rd-lite" type="button" data-copy>Copy as markdown</button>
    <button class="rd-lite" type="button" data-general>+ Note not tied to any text</button>
  </div>
</aside>

<div id="rd-ed" role="dialog" aria-label="Add a note">
  <div class="rd-tags"></div>
  <blockquote></blockquote>
  <textarea placeholder="What's on your mind? Questions and half-formed doubts are exactly what's useful here."></textarea>
  <div class="row"><button class="no" type="button" data-cancel>Cancel</button><button class="ok" type="button" data-save>Save note</button></div>
</div>

<div id="rd-toast"></div>
"""

HINT = """<div class="rd-hint"><b>This page is for you to mark up.</b> Select any sentence and two
buttons appear: <b>&#128396; Highlight</b> just marks the passage, <b>&#128172; Note</b> attaches a
question, a change, a concern or &ldquo;I don't understand this&rdquo;. The panel keeps them in
separate tabs; you can edit or delete your own, and mark a thread resolved &mdash; closing a thread
is yours to decide, never Claude's. Everything is kept in this browser until you hit
<b>Export notes for Claude</b>, which saves a JSON file. Tell Claude when you're done and it will
read them, reply in the panel, and rebuild the page. Use <b>&#8677;</b> in the panel header to hide
it and centre the text.</div>"""


def render_html(src_text, out_name, old_notes):
    meta, lines = front_matter(src_text.splitlines())
    title = meta.get("title") or out_name
    slug = slugify(meta.get("slug") or os.path.splitext(out_name)[0])
    sections = split_sections(lines, title)

    head = ["<header>", "<h1>%s</h1>" % inline(title)]
    if meta.get("subtitle"):
        head.append('<p class="sub">%s</p>' % inline(meta["subtitle"]))
    if meta.get("meta"):
        head.append('<p class="meta">%s</p>' % inline(meta["meta"]))
    head.append("</header>")
    if meta.get("hint", "").lower() not in ("false", "no", "off"):
        head.append(HINT)

    sid, stitle, body = sections[0]
    sections[0] = (sid, stitle, "".join(head) + body)

    body_html = "".join(
        '<section class="rd-sec" id="%s" data-title="%s">%s<!--/rd-sec--></section>'
        % (sid, esc(stitle), body)
        for sid, stitle, body in sections
    )

    notes = [normalise(n, i) for i, n in enumerate(old_notes or [])]
    stats = reanchor(notes, [(s[0], s[1], section_text(s[2])) for s in sections])

    js = (
        JS.replace(
            "__DOC__",
            json.dumps({"slug": slug, "file": out_name, "title": title}, ensure_ascii=False),
        )
        .replace("__TAGS__", json.dumps(TAGS, ensure_ascii=False))
        .replace("__NO_BODY_TAGS__", json.dumps(sorted(NO_BODY_TAGS)))
    )
    payload = json.dumps(notes, ensure_ascii=False, indent=1)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
__BODY__
</div>
__PANEL__
<script id="rd-notes" type="application/json">
__NOTES__
</script>
<script>__JS__</script>
</body>
</html>
"""
    for k, v in (
        ("__TITLE__", esc(re.sub(r"<[^>]+>", "", inline(title)))),
        ("__CSS__", CSS),
        ("__BODY__", body_html),
        ("__PANEL__", PANEL),
        ("__NOTES__", payload),
        ("__JS__", js),
    ):
        html = html.replace(k, v, 1)
    return html, stats


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

STARTER = """---
title: __TITLE__
subtitle: One sentence saying what this is and why it matters to the reader.
meta: __META__
---

:::lede
Open with the whole answer in plain language, before any detail. Assume the reader
has no background in this topic.
:::

## What is going on

:::analogy
Reach for a concrete everyday comparison here. Analogies are the point of this format.
:::

:::flag warn heads up | Something worth pausing on
Explain the risk in one short paragraph.
:::

## What I recommend

:::steps
1. First thing to do.
2. Second thing.
3. Third thing.
:::

## What I need from you

| Decision | My recommendation |
|---|---|
| Something you must choose | `yes` and why |
"""


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except IOError as e:
        die("cannot read %s (%s)" % (path, e.strerror))


def cmd_new(a):
    path = a.name if a.name.endswith(".md") else a.name + ".md"
    if os.path.exists(path):
        die("%s already exists" % path)
    title = a.title or os.path.basename(path)[:-3].replace("-", " ").replace("_", " ").capitalize()
    text = STARTER.replace("__TITLE__", title).replace(
        "__META__", datetime.now().strftime("%Y-%m-%d")
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("wrote %s" % path)


def cmd_build(a):
    if not os.path.exists(a.src):
        die("no such source file: %s" % a.src)
    out = a.out or os.path.splitext(a.src)[0] + ".html"
    old = []
    if os.path.exists(out):
        old = read_notes(read(out), out)
    html, stats = render_html(read(a.src), os.path.basename(out), old)
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
    except IOError as e:
        die("cannot write %s (%s)" % (out, e.strerror))
    msg = "built %s (%d bytes)" % (out, len(html.encode("utf-8")))
    if old:
        msg += " — notes: %d kept, %d re-anchored, %d unanchored" % (
            stats["ok"],
            stats["moved"],
            stats["lost"],
        )
    print(msg)
    if a.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, out], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            sys.stderr.write("reviewdoc: could not run %s\n" % opener)


def cmd_import(a):
    html = read(a.doc)
    notes = read_notes(html, a.doc)
    try:
        incoming = json.loads(read(a.notes))
    except ValueError as e:
        die("%s is not valid JSON (%s)" % (a.notes, e))
    if isinstance(incoming, dict) and isinstance(incoming.get("notes"), list):
        incoming = incoming["notes"]      # tolerate a {"notes":[...]} wrapper
    if not isinstance(incoming, list):
        die("%s must contain a JSON array of notes" % a.notes)
    if not all(isinstance(x, dict) and "id" in x for x in incoming):
        die("%s: every note must be an object with an \"id\"" % a.notes)

    by_id = dict((n["id"], n) for n in notes if "id" in n)
    added = merged = edited = dropped = 0
    for raw in incoming:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        n = normalise(dict(raw))
        cur = by_id.get(n["id"])
        # the reader deleted it in the panel: take it out and never re-add it
        if n["status"] == "deleted":
            if cur is not None:
                notes.remove(cur)
                del by_id[n["id"]]
                dropped += 1
            continue
        if cur is None:
            notes.append(n)
            by_id[n["id"]] = n
            added += 1
            continue
        have = set((r.get("author"), r.get("ts"), r.get("body")) for r in cur["replies"])
        for r in n["replies"]:
            key = (r.get("author"), r.get("ts"), r.get("body"))
            if key not in have:
                cur["replies"].append(r)
                have.add(key)
                merged += 1
        # the reader owns the question and its status: whatever came back wins
        for k in ("kind", "tag", "body", "status", "context"):
            if k in raw and n[k] != cur.get(k):
                cur[k] = n[k]
                edited += 1

    reanchor(notes, doc_sections(html))
    with open(a.doc, "w", encoding="utf-8") as f:
        f.write(write_notes(html, notes))
    loose = sum(1 for n in notes if not n.get("anchored"))
    hl = sum(1 for n in notes if n.get("kind") == "highlight")
    print(
        "imported into %s: %d new, %d new repl(ies), %d edit(s), %d removed — "
        "%d thread(s) + %d highlight(s), %d unanchored"
        % (a.doc, added, merged, edited, dropped, len(notes) - hl, hl, loose)
    )


def is_hl(n):
    return n.get("kind") == "highlight"


def cmd_list(a):
    notes = read_notes(read(a.doc), a.doc)
    hl = [n for n in notes if is_hl(n)]
    if a.highlights:
        notes = hl
    else:
        notes = [n for n in notes if not is_hl(n)]
        if a.open_only:
            notes = [n for n in notes if n.get("status") != "resolved"]
    if a.json:
        print(json.dumps(notes, ensure_ascii=False, indent=2))
        return
    if not notes:
        print("no highlights" if a.highlights else "no notes")
    for n in notes:
        q = re.sub(r"\s+", " ", n.get("quote") or "").strip()
        if len(q) > 52:
            q = q[:51] + "…"
        flag = "" if n.get("anchored", True) else "!"
        if a.highlights:
            print("%-10s %-1s %-26s %s" % (n.get("id", "?"), flag,
                  (n.get("sectionTitle") or "General")[:26], '"%s"' % q))
            continue
        print(
            "%-10s %-9s %-8s %-1s %-26s %-54s replies:%d"
            % (
                n.get("id", "?"),
                n.get("tag", "?"),
                n.get("status", "open"),
                flag,
                (n.get("sectionTitle") or "General")[:26],
                '"%s"' % q if q else "(general)",
                len(n.get("replies") or []),
            )
        )
    loose = [n["id"] for n in notes if not n.get("anchored", True)]
    if loose:
        print("! unanchored: %s" % " ".join(loose))
    if hl and not a.highlights:
        print("+ %d highlight(s) — no reply needed: reviewdoc.py list %s --highlights"
              % (len(hl), a.doc))


def _save_notes(path, html, notes):
    with open(path, "w", encoding="utf-8") as f:
        f.write(write_notes(html, notes))


READER_CLOSES = (
    "closing a thread is the reader's call, not yours. They resolve it from the "
    "\"Mark resolved\" button in the review panel and it arrives on the next import. "
    "Reply and leave the thread open."
)


def cmd_reply(a):
    if a.resolve:
        die(READER_CLOSES)
    html = read(a.doc)
    notes = read_notes(html, a.doc)
    if a.all_open:
        if len(a.rest) != 1:
            die('reply --all-open takes exactly one text argument')
        targets = [n for n in notes
                   if n.get("status") != "resolved" and not is_hl(n)]
        if not targets:
            die("no open threads to reply to")
        text = a.rest[0]
    else:
        if len(a.rest) != 2:
            die('usage: reviewdoc.py reply <doc.html> <note-id> <text>  [or --all-open <text>]')
        nid, text = a.rest
        targets = [n for n in notes if n.get("id") == nid]
        if not targets:
            die("no thread with id %s (try: reviewdoc.py list %s)" % (nid, a.doc))
        if is_hl(targets[0]):
            die("%s is a highlight, not a question — the reader marked that passage "
                "without asking anything, so there is nothing to answer." % nid)
    if not text.strip():
        die("reply text is empty")
    ts = now_iso()
    for n in targets:
        n.setdefault("replies", []).append(
            {"author": "claude", "body": text, "ts": ts}
        )
    _save_notes(a.doc, html, notes)
    print(
        "replied to %d thread(s): %s"
        % (len(targets), " ".join(n.get("id", "?") for n in targets))
    )


def cmd_resolve(a):
    die(READER_CLOSES)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `reply` takes a free-text positional, so argparse cannot tell where the
    # flags go. Pull them out first and the flag may sit anywhere on the line.
    flags = set()
    if argv[:1] == ["reply"]:
        argv = [x for x in argv if not (x in ("--resolve", "--all-open") and not flags.add(x))]

    p = argparse.ArgumentParser(prog="reviewdoc.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd")

    q = sub.add_parser("new", help="scaffold a starter .md")
    q.add_argument("name")
    q.add_argument("--title")
    q.set_defaults(fn=cmd_new)

    q = sub.add_parser("build", help="render .md -> self-contained .html, keeping notes")
    q.add_argument("src")
    q.add_argument("-o", "--out")
    q.add_argument("--open", action="store_true")
    q.set_defaults(fn=cmd_build)

    q = sub.add_parser("import", help="merge exported notes JSON into a built doc")
    q.add_argument("doc")
    q.add_argument("notes")
    q.set_defaults(fn=cmd_import)

    q = sub.add_parser("list", help="print threads, one line each")
    q.add_argument("doc")
    q.add_argument("--open-only", action="store_true")
    q.add_argument("--highlights", action="store_true",
                   help="list the reader's highlights instead of the note threads")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_list)

    q = sub.add_parser("reply", help="append a Claude reply to a thread")
    q.add_argument("doc")
    q.add_argument("rest", nargs="*")
    q.add_argument("--all-open", action="store_true")
    q.add_argument("--resolve", action="store_true",
                   help="(refused — only the reader closes a thread)")
    q.set_defaults(fn=cmd_reply)

    q = sub.add_parser("resolve", help="(refused — only the reader closes a thread)")
    q.add_argument("doc")
    q.add_argument("note_id")
    q.set_defaults(fn=cmd_resolve)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 2
    if a.cmd == "reply":
        a.resolve = "--resolve" in flags
        a.all_open = "--all-open" in flags
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
