#!/usr/bin/env python3
"""reviewdoc — build self-contained, annotatable HTML review documents.

An HTML fragment in a small fixed vocabulary goes in, checked strictly; one
self-contained HTML file comes out, with a Word-style threaded comment panel.
Notes live inside the HTML (a JSON block), so a rebuild never loses them and
Claude can read and reply to them from the command line. `md` turns a built
document back into markdown, best effort.

Python 3 stdlib only. See SKILL.md for the operational loop.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import NoReturn

# Offered in the note editor's tag picker, in this order.
TAGS = {
    "question": "❓ Question",
    "change": "✏️ Change this",
    "defer": "\U0001f4e5 Defer to backlog",
    "agree": "\U0001f44d Agree",
}

# Filed from the selection bar in one click, so deliberately absent from the
# picker — but still current tags that need a label.
BAR_TAGS = {
    "clarify": "\U0001f50d Clarify this",
    "highlight": "\U0001f58d Highlight",
}

# Tags that can be submitted without a note body (the quote alone is the point).
NO_BODY_TAGS = {"clarify", "agree", "defer"}
# ...and tags that file themselves as settled rather than as an open question.
CLOSING_TAGS = {"agree"}
NOTES_RE = re.compile(
    r'(<script id="rd-notes" type="application/json">)(.*?)(</script>)', re.S
)
SEC_RE = re.compile(
    r'<section class="rd-sec" id="([^"]+)" data-title="([^"]*)">(.*?)<!--/rd-sec--></section>',
    re.S,
)
MD_RE = re.compile(r'<script id="rd-md" type="application/json">(.*?)</script>', re.S)
# identifies one document across rebuilds; the browser keys its drafts on it
DOCID_RE = re.compile(r'<meta name="reviewdoc-id" content="([\w-]+)">')
DOC_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def die(msg) -> NoReturn:
    sys.stderr.write("reviewdoc: %s\n" % msg)
    sys.exit(1)


def now_iso():
    # milliseconds, not seconds: the panel orders threads by their newest reply,
    # and several `reply` calls inside one second must still be distinguishable
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
# source: an HTML fragment in a fixed vocabulary, checked before it is used
# --------------------------------------------------------------------------

# Every tag the source may use. Anything else fails the build, which is the
# point: a literal `<` written into prose (`vector<int>`) parses as an unknown
# tag, so it is reported with its line number instead of vanishing from the page.
INLINE = {"a", "b", "strong", "i", "em", "code", "br", "sub", "sup", "kbd"}
BLOCK = {"h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "table", "thead", "tbody",
         "tr", "th", "td", "pre", "div", "blockquote", "hr", "svg", "meta"}
VOID = {"br", "hr", "meta"}

# class -> the one tag it may sit on
CLASSES = {
    "lede": "p", "sub": "p", "meta": "p", "small": "p",
    "flag": "div", "card": "div", "analogy": "div", "grid": "div",
    "tick": "ul", "cross": "ul", "steps": "ol",
}
# modifiers that only make sense next to one base class
MODIFIERS = {"flag": {"good", "warn", "bad", "info"}, "grid": {"g2", "g3"}}
# attributes other than `class`; everything unlisted is refused, `style` included
ATTRS = {
    "a": {"href"}, "th": {"colspan", "rowspan"}, "td": {"colspan", "rowspan"},
    "div": {"data-pill"}, "meta": {"name", "content"},
}
META_NAMES = {"slug", "hint"}

# what may sit directly inside a tag, when that is narrower than "anything"
CHILDREN = {
    "ul": {"li"}, "ol": {"li"},
    "table": {"thead", "tbody", "tr"}, "thead": {"tr"}, "tbody": {"tr"},
    "tr": {"th", "td"}, "pre": {"code"}, "code": set(),
}
ONLY_INLINE = {"p", "h1", "h2", "h3", "h4", "a", "b", "strong", "i", "em",
               "sub", "sup", "kbd"}
PARENTS = {"li": {"ul", "ol"}, "tr": {"table", "thead", "tbody"},
           "th": {"tr"}, "td": {"tr"}, "thead": {"table"}, "tbody": {"table"}}
TOP_ONLY = {"h1", "h2", "meta"}
NO_TEXT = {"ul": "li", "ol": "li", "table": "td", "thead": "td", "tbody": "td",
           "tr": "td"}
# HTML tags that make a browser abandon an open <svg> mid-diagram
SVG_BREAKOUT = {"b", "big", "blockquote", "br", "center", "code", "dd", "div", "dl",
                "dt", "em", "embed", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
                "img", "li", "listing", "menu", "meta", "nobr", "ol", "p", "pre",
                "ruby", "s", "small", "span", "strike", "strong", "sub", "sup",
                "table", "tt", "u", "ul", "var"}


class Source(HTMLParser):
    """Check a source fragment against the vocabulary above and note where
    each top-level <h2> starts. Collects every error rather than stopping at
    the first, so one build reports everything that needs fixing."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.errors, self.stack, self.svg = [], [], 0
        self.h2 = []            # (line, col) of each top-level <h2>
        self.meta = {}
        self.top = 0            # top-level elements seen so far, <meta> aside

    def err(self, msg):
        self.errors.append("line %d: %s" % (self.getpos()[0], msg))

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, False)
        self._replay()

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, True)
        self._replay()

    def _replay(self):
        """`x<y here.</p>` parses as one bogus tag that swallowed the `</p>`.
        Replay any closing tags it ate, so one slip is reported once instead of
        leaving the paragraph open for the rest of the file."""
        if self.svg:
            return
        for name in re.findall(r"</\s*([A-Za-z][\w-]*)", self.get_starttag_text() or ""):
            self.handle_endtag(name.lower())

    def _open(self, tag, attrs, selfclosed):
        if self.svg:
            if tag in SVG_BREAKOUT:
                self.err("<%s> inside <svg> makes a browser end the diagram early; "
                         "diagram labels go in <text>" % tag)
            elif tag == "svg" and not selfclosed:
                self.svg += 1
            return
        parent = self.stack[-1][0] if self.stack else None
        if tag not in INLINE and tag not in BLOCK:
            # not pushed: a stray `<int>` must not also report "never closed"
            self.err("<%s> is not an allowed tag. A literal < in text must be "
                     "written &lt; (and & as &amp;), e.g. vector&lt;int&gt;" % tag)
            return
        self._placement(tag, parent)
        self._attrs(tag, attrs)
        if tag == "svg":
            if not selfclosed:
                self.svg = 1
            return
        if selfclosed and tag not in VOID:
            self.err("<%s/> does not close itself in HTML; write <%s></%s>" % (tag, tag, tag))
        elif tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def _placement(self, tag, parent):
        if parent is None:
            if tag == "h1" and self.top:
                self.err("<h1> must be the first thing in the source")
            if tag == "h2":
                self.h2.append(self.getpos())
            if tag != "meta":
                self.top += 1
            if tag in INLINE:
                self.err("<%s> at the top level; wrap it in <p>" % tag)
            elif tag in PARENTS:
                self.err("<%s> must sit inside <%s>" % (tag, "> or <".join(sorted(PARENTS[tag]))))
            return
        if tag in TOP_ONLY:
            self.err("<%s> must be at the top level, not inside <%s>" % (tag, parent))
        elif parent in CHILDREN and tag not in CHILDREN[parent]:
            allowed = CHILDREN[parent]
            self.err("<%s> cannot sit directly inside <%s>%s" % (
                tag, parent,
                " (only <%s>)" % "> or <".join(sorted(allowed)) if allowed else
                "; it holds text only"))
        elif parent in ONLY_INLINE and tag not in INLINE:
            self.err("<%s> cannot sit inside <%s>; only inline tags can" % (tag, parent))
        elif tag in PARENTS and parent not in PARENTS[tag]:
            self.err("<%s> must sit inside <%s>, not <%s>"
                     % (tag, "> or <".join(sorted(PARENTS[tag])), parent))
        elif tag == "a" and any(t == "a" for t, _ in self.stack):
            self.err("<a> inside <a>")

    def _attrs(self, tag, attrs):
        if tag == "svg":
            return              # a diagram is passed through untouched, attributes and all
        classes = []
        for k, v in attrs:
            if k == "class":
                classes = (v or "").split()
            elif k == "style":
                self.err("inline style on <%s>; use the classes in SKILL.md" % tag)
            elif k not in ATTRS.get(tag, ()):
                self.err("attribute %s is not allowed on <%s>" % (k, tag))
        if tag == "meta":
            d = dict(attrs)
            if d.get("name") not in META_NAMES or d.get("content") is None:
                self.err('<meta> takes name="slug" or name="hint" and a content')
            else:
                self.meta[d["name"]] = d["content"]
        if tag == "code":
            if len(classes) > 1 or (classes and not classes[0].startswith("language-")):
                self.err('<code> takes only class="language-..."')
            return
        base = [c for c in classes if c in CLASSES]
        mods = [c for c in classes if c not in CLASSES]
        if len(base) > 1:
            self.err("<%s> has two kinds at once: %s" % (tag, " ".join(base)))
        elif base and CLASSES[base[0]] != tag:
            self.err('class "%s" belongs on <%s>, not <%s>' % (base[0], CLASSES[base[0]], tag))
        bad = [c for c in mods if not base or c not in MODIFIERS.get(base[0], ())]
        if bad:
            self.err('unknown class "%s" on <%s>' % (" ".join(bad), tag))
        if dict(attrs).get("data-pill") is not None and base != ["flag"]:
            self.err("data-pill only goes on a flag")

    def handle_endtag(self, tag):
        if self.svg:
            if tag == "svg":
                self.svg -= 1
            return
        if tag not in INLINE and tag not in BLOCK:
            return              # its opening tag has already been reported
        if tag in VOID:
            self.err("</%s>: <%s> has no closing tag" % (tag, tag))
            return
        if not any(t == tag for t, _ in self.stack):
            self.err("</%s> has no matching <%s>" % (tag, tag))
            return
        if self.stack[-1][0] != tag:
            t, line = self.stack[-1]
            self.err("</%s> arrives while <%s> from line %d is still open" % (tag, t, line))
        # pop back to the match, so one mistake is reported once, not at every tag after it
        while self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        if self.svg or not data.strip():
            return
        parent = self.stack[-1][0] if self.stack else None
        snippet = re.sub(r"\s+", " ", data.strip())[:40]
        if parent is None:
            self.err('text outside any element, wrap it in <p>: "%s"' % snippet)
        elif parent in NO_TEXT:
            self.err('text directly inside <%s>, put it in <%s>: "%s"'
                     % (parent, NO_TEXT[parent], snippet))

    def handle_decl(self, decl):
        self.err("<!%s> does not belong in a source fragment" % decl)

    def unknown_decl(self, data):
        if not self.svg:            # CDATA is legal inside a diagram
            self.err("<![%s]> does not belong in a source fragment" % data)

    def handle_pi(self, data):
        self.err("<?%s> does not belong in a source fragment" % data)

    def close(self):
        HTMLParser.close(self)
        for tag, line in self.stack:
            self.errors.append("line %d: <%s> is never closed" % (line, tag))
        if self.svg:
            self.errors.append("end of file: <svg> is never closed")


def plain(html):
    """Visible text of an inline HTML snippet."""
    return unescape(re.sub(r"<[^>]*>", "", html)).strip()


HEAD_RE = re.compile(
    r"\s*<h1\s*>(.*?)</h1\s*>((?:\s*<p\s+class=[\"']?(?:sub|meta)[\"']?\s*>.*?</p\s*>)*)",
    re.S,
)
H2_RE = re.compile(r"<h2\s*>(.*?)</h2\s*>", re.S)


def polish(html):
    """The few rewrites a built section needs on top of the source."""
    # a browser drops a newline straight after <pre>; Python's text walk would
    # not, and the two must agree on every character offset a note uses
    html = re.sub(r"<pre\s*>\n", "<pre>", html)
    html = re.sub(r"<table\b", '<div class="scroll"><table', html)
    return re.sub(r"</table\s*>", "</table></div>", html)


def parse_source(text, path, fallback_title):
    """Check the source, then -> (meta, title, [(id, title, html)]).
    Slot 0 is always the intro (the header plus anything before the first
    <h2>), so the page header has somewhere to live and text up there anchors."""
    chk = Source()
    chk.feed(text)
    chk.close()
    if chk.errors:
        shown = chk.errors[:25]
        more = len(chk.errors) - len(shown)
        die("%s: %d problem(s), nothing written\n  %s%s" % (
            path, len(chk.errors), "\n  ".join(shown),
            "\n  ... and %d more" % more if more else ""))

    # HTMLParser counts lines by "\n" alone, so do the same here
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    cuts = [starts[line - 1] + col for line, col in chk.h2]
    chunks = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
    chunks = [re.sub(r"<meta\b[^>]*>", "", re.sub(r"<!--.*?-->", "", c, flags=re.S))
              for c in chunks]

    intro = chunks[0]
    m = HEAD_RE.match(intro)
    if m:
        title = plain(m.group(1)) or fallback_title
        head = "<header><h1>%s</h1>%s</header>" % (m.group(1), m.group(2))
        intro = intro[m.end():]
    else:
        title = fallback_title
        head = "<header><h1>%s</h1></header>" % esc(title)
    out = [("sec-intro", title, head + polish(intro))]

    seen = {}
    for num, chunk in enumerate(chunks[1:], 1):
        h = H2_RE.match(chunk)
        stitle = plain(h.group(1)) if h else ""
        body = '<h2><span class="num">%d</span>%s</h2>' % (num, h.group(1)) + chunk[h.end():] \
            if h else chunk
        sid = "sec-" + slugify(stitle)
        seen[sid] = seen.get(sid, 0) + 1
        if seen[sid] > 1:
            sid = "%s-%d" % (sid, seen[sid])
        out.append((sid, stitle, polish(body)))
    return chk.meta, title, out


# --------------------------------------------------------------------------
# built HTML -> markdown (best effort, over the vocabulary above)
# --------------------------------------------------------------------------

GEN_MARK = "<!-- generated by reviewdoc.py"
FIG_RE = re.compile(r"<svg\b.*?</svg\s*>", re.S | re.I)
MD_BLOCKS = {"section", "header", "h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "pre",
             "table", "thead", "tbody", "tr", "th", "td", "div", "blockquote", "hr",
             "rdfig"}
ALERT = {"bad": "CAUTION", "warn": "WARNING", "good": "TIP", "info": "NOTE"}


class Node(object):
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.kids = tag, dict(attrs), []

    def cls(self):
        return (self.attrs.get("class") or "").split()


class Tree(HTMLParser):
    """Tolerant tree builder: just enough structure to walk."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def text_of(node):
    return "".join(k if isinstance(k, str) else text_of(k) for k in node.kids)


def md_escape(s, table=False):
    s = re.sub(r"([\\`*\[\]<~])", r"\\\1", s)
    # an underscore between two letters is never emphasis; anywhere else it can be
    s = re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", r"\\_", s)
    s = re.sub(r"&(?=#?\w+;)", r"\\&", s)
    return s.replace("|", "\\|") if table else s


def md_lead(s):
    """Escape whatever would turn the start of a paragraph into markup."""
    if re.fullmatch(r"[-=*_ ]{3,}", s):
        return "\\" + s
    m = re.match(r"(\d+)([.)])(\s|$)", s)
    if m:
        return m.group(1) + "\\" + s[len(m.group(1)):]
    if re.match(r"(#{1,6}(\s|$)|>|[-+=](\s|$))", s):
        return "\\" + s
    return s


def md_code(s, table=False):
    s = re.sub(r"\s+", " ", s)
    runs = [len(r) for r in re.findall(r"`+", s)]
    fence = "`" * (max(runs) + 1 if runs else 1)
    if runs or s.startswith(" ") or s.endswith(" "):
        s = " " + s + " "
    return fence + (s.replace("|", "\\|") if table else s) + fence


def md_emph(s, mark):
    core = s.strip()
    if not core:
        return s
    return (" " if s[:1].isspace() else "") + mark + core + mark + \
        (" " if s[-1:].isspace() else "")


def md_link(text, href):
    if not href:
        return text
    if re.search(r"[\s()<>]", href):
        href = "<%s>" % href.replace("<", "%3C").replace(">", "%3E")
    return "[%s](%s)" % (text.strip() or md_escape(href), href)


class MdCtx(object):
    """Figures seen so far, and whether we are inside a table cell."""

    def __init__(self, stem, files):
        self.stem, self.files, self.table = stem, files, False
        self.figs = []

    def stash(self, m):
        self.figs.append(m.group(0))
        return '<rdfig n="%d"></rdfig>' % len(self.figs)

    def figure(self, node):
        n = int(node.attrs.get("n", "0"))
        svg = self.figs[n - 1] if 0 < n <= len(self.figs) else ""
        lab = re.search(r"\baria-label=\"([^\"]*)\"", svg[:400])
        label = unescape(lab.group(1)).strip() if lab else ""
        if self.files is None:
            return "*Figure %d%s. The diagram is in the HTML version of this document.*" \
                % (n, ": " + md_escape(label) if label else "")
        name = "%s-fig-%d.svg" % (self.stem, n)
        self.files.append((name, standalone_svg(svg)))
        return "![%s](%s)" % (md_escape(label or "Figure %d" % n), name)


def md_inline(node, ctx):
    out = []
    for k in node.kids:
        if isinstance(k, str):
            out.append(md_escape(re.sub(r"\s+", " ", k), ctx.table))
            continue
        t = k.tag
        if t == "span" and "num" in k.cls():
            continue
        if t == "br":
            out.append("<br>")
        elif t == "code":
            out.append(md_code(text_of(k), ctx.table))
        elif t in ("b", "strong"):
            out.append(md_emph(md_inline(k, ctx), "**"))
        elif t in ("i", "em"):
            out.append(md_emph(md_inline(k, ctx), "*"))
        elif t == "a":
            out.append(md_link(md_inline(k, ctx), k.attrs.get("href", "")))
        elif t in ("sub", "sup", "kbd"):
            out.append("<%s>%s</%s>" % (t, md_inline(k, ctx), t))
        else:
            out.append(md_inline(k, ctx))
    return "".join(out)


def md_para(node, ctx):
    return md_lead(re.sub(r" {2,}", " ", md_inline(node, ctx)).strip())


def md_blocks(node, ctx):
    """-> markdown blocks for node's children; loose inline runs become paragraphs."""
    out, run = [], Node("#run")
    for k in node.kids + [None]:
        if k is not None and (isinstance(k, str) or k.tag not in MD_BLOCKS):
            run.kids.append(k)
            continue
        if run.kids:
            p = md_para(run, ctx)
            if p:
                out.append(p)
            run.kids = []
        if k is not None:
            out.extend(md_block(k, ctx))
    return out


def md_quote(blocks):
    return "\n".join(("> " + ln) if ln else ">" for ln in "\n\n".join(blocks).split("\n"))


def md_titled(k, ctx):
    """A card or flag: (its leading <h3> as text, the rest as blocks)."""
    kids = [x for x in k.kids if not (isinstance(x, str) and not x.strip())]
    if kids and not isinstance(kids[0], str) and kids[0].tag == "h3":
        rest = Node("#body")
        rest.kids = kids[1:]
        return md_para(kids[0], ctx), md_blocks(rest, ctx)
    return "", md_blocks(k, ctx)


def md_block(k, ctx):
    t, c = k.tag, k.cls()
    if t == "h1":
        return ["# " + md_para(k, ctx)]
    if t == "h2":
        num = next((text_of(x).strip() for x in k.kids
                    if not isinstance(x, str) and "num" in x.cls()), "")
        return ["## %s%s" % (num + ". " if num else "", md_para(k, ctx))]
    if t in ("h3", "h4"):
        return ["#" * int(t[1]) + " " + md_para(k, ctx)]
    if t == "p":
        p = md_para(k, ctx)
        return [md_emph(p, "*") if "meta" in c else p] if p else []
    if t == "hr":
        return ["---"]
    if t == "pre":
        return [md_fence(k)]
    if t in ("ul", "ol"):
        return [md_list(k, ctx)] if k.kids else []
    if t == "table":
        return [md_table(k, ctx)]
    if t == "rdfig":
        return [ctx.figure(k)]
    if "flag" in c:
        kind = next((x for x in c if x in ALERT), "info")
        title, body = md_titled(k, ctx)
        pill = k.attrs.get("data-pill", "").strip()
        head = " · ".join(x for x in (
            "*%s*" % md_escape(pill) if pill else "",
            "**%s**" % title if title else "") if x)
        return ["> [!%s]\n" % ALERT[kind] + md_quote(([head] if head else []) + body)]
    if "card" in c:
        title, body = md_titled(k, ctx)
        return [md_quote((["**%s**" % title] if title else []) + body)]
    if t == "blockquote" or "analogy" in c:
        return [md_quote(md_blocks(k, ctx))]
    return md_blocks(k, ctx)        # section, header, grid, scroll, strays


def md_fence(pre):
    code = next((x for x in pre.kids if not isinstance(x, str) and x.tag == "code"), None)
    src = text_of(code if code is not None else pre).strip("\n")
    lang = ""
    if code is not None:
        m = re.search(r"\blanguage-([\w+#-]+)", code.attrs.get("class") or "")
        lang = m.group(1) if m else ""
    runs = [len(r) for r in re.findall(r"`{3,}", src)]
    fence = "`" * max([3] + [n + 1 for n in runs])
    return "%s%s\n%s\n%s" % (fence, lang, src, fence)


def md_list(k, ctx):
    ordered = k.tag == "ol"
    mark = "✓ " if "tick" in k.cls() else "✗ " if "cross" in k.cls() else ""
    items, n = [], 0
    for li in k.kids:
        if isinstance(li, str) or li.tag != "li":
            continue
        n += 1
        bullet = "%d. " % n if ordered else "- "
        pad = " " * len(bullet)
        blocks = md_blocks(li, ctx) or [""]
        text = bullet + mark + blocks[0].replace("\n", "\n" + pad)
        for b in blocks[1:]:
            tight = re.match(r"(- |\d+\. )", b)     # a nested list hugs its item
            text += ("\n" if tight else "\n\n") + pad + b.replace("\n", "\n" + pad)
        items.append(text)
    return "\n".join(items)


def md_table(k, ctx):
    rows = []

    def walk(n):
        for x in n.kids:
            if isinstance(x, str):
                continue
            if x.tag == "tr":
                rows.append([cell(c) for c in x.kids
                             if not isinstance(c, str) and c.tag in ("th", "td")])
            elif x.tag in ("thead", "tbody"):
                walk(x)

    def cell(c):
        was, ctx.table = ctx.table, True
        try:
            return "<br>".join(b.replace("\n", " ") for b in md_blocks(c, ctx))
        finally:
            ctx.table = was

    walk(k)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def standalone_svg(svg):
    """An inline diagram as a file of its own. Its colours are CSS variables
    the page defines, so bring the light palette along or strokes vanish."""
    m = re.match(r"<svg\b[^>]*>", svg, re.I)
    if not m:
        return svg
    tag = m.group(0)
    if "xmlns=" not in tag:
        tag = tag[:4] + ' xmlns="http://www.w3.org/2000/svg"' + tag[4:]
    if "xlink:" in svg and "xmlns:xlink" not in tag:
        tag = tag[:4] + ' xmlns:xlink="http://www.w3.org/1999/xlink"' + tag[4:]
    pal = re.search(r"prefers-color-scheme: light\)\{\s*:root\{(.*?)\}", CSS, re.S)
    style = "<style>:root{%s}text{font-family:-apple-system,BlinkMacSystemFont," \
            "'Segoe UI',Roboto,Helvetica,Arial,sans-serif}</style>" \
            % re.sub(r"\s+", "", pal.group(1) if pal else "")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tag + style + svg[m.end():] + "\n"


def to_markdown(sections, source_name, stem, files=None):
    """sections: [(id, title, html)] as built. files: a list that collects
    (filename, svg) for each diagram, or None to leave a line in its place."""
    ctx = MdCtx(stem, files)
    blocks = []
    for _sid, _title, body in sections:
        t = Tree()
        t.feed(FIG_RE.sub(ctx.stash, body))
        t.close()
        blocks.extend(md_blocks(t.root, ctx))
    head = "%s from %s; edits here are not read back -->" % (GEN_MARK, source_name)
    return "\n\n".join([head] + [b for b in blocks if b.strip()]) + "\n"


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
  h4{font-size:16px;margin:22px 0 6px}
  p{margin:0 0 14px}
  a{color:var(--accent)}
  .lede{color:var(--dim);margin:0 0 24px;font-size:17px;max-width:66ch}
  code{font-family:var(--mono);font-size:.86em;background:var(--card2);padding:2px 6px;
    border-radius:5px;border:1px solid var(--line);word-break:break-word}
  pre{background:var(--card2);border:1px solid var(--line);border-radius:11px;padding:15px 16px;
    overflow-x:auto;margin:14px 0;font-family:var(--mono);font-size:13px;line-height:1.55}
  pre code{background:none;border:none;padding:0;font-size:inherit}
  kbd{font-family:var(--mono);font-size:.82em;background:var(--card2);padding:1px 6px;
    border:1px solid var(--line);border-bottom-width:2px;border-radius:5px}
  .wrap blockquote{margin:16px 0;padding-left:14px;border-left:3px solid var(--line);color:var(--dim)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:18px 0}
  .card>h3:first-child{margin-top:0}
  .grid{display:grid;gap:16px}
  @media(min-width:720px){ .g2{grid-template-columns:1fr 1fr} .g3{grid-template-columns:repeat(3,1fr)} }
  .flag{border-left:4px solid var(--accent);background:var(--card);border-radius:0 12px 12px 0;
    padding:18px 22px;margin:20px 0;border-top:1px solid var(--line);
    border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
  .flag.bad{border-left-color:var(--bad)} .flag.warn{border-left-color:var(--warn)}
  .flag.good{border-left-color:var(--good)}
  .flag>h3:first-child{font-size:16.5px;font-weight:650;margin:0 0 6px}
  .flag>:last-child,.card>:last-child,.analogy>:last-child{margin-bottom:0}
  /* the pill is generated from data-pill, so its text is never part of what a
     note anchors against, in the browser or in reviewdoc.py */
  .flag[data-pill]::before{content:attr(data-pill);display:inline-block;vertical-align:2px;
    font-size:12px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;line-height:1.35;
    padding:3px 9px;border-radius:999px;background:var(--card2);color:var(--dim);
    border:1px solid var(--line);margin-right:9px}
  .flag[data-pill]>h3:first-child{display:inline;margin:0}
  .flag[data-pill]>h3:first-child+*,.flag[data-pill]>:first-child:not(h3){margin-top:10px}
  .flag.bad[data-pill]::before{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
  .flag.good[data-pill]::before{color:var(--good);border-color:color-mix(in srgb,var(--good) 40%,transparent)}
  .flag.warn[data-pill]::before{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
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
  /* numbers drawn by CSS, like the pill: not text, so never in an anchor */
  ol.steps{list-style:none;padding-left:0;counter-reset:rd-step}
  ol.steps>li{counter-increment:rd-step;position:relative;padding-left:40px;margin-bottom:16px;
    min-height:28px}
  ol.steps>li::before{content:counter(rd-step);position:absolute;left:0;top:1px;width:26px;
    height:26px;border-radius:50%;background:var(--accent);color:#fff;display:grid;
    place-items:center;font-size:13px;font-weight:700;line-height:1}
  hr{border:none;border-top:1px solid var(--line);margin:48px 0}
  .analogy{color:var(--dim);font-style:italic;border-left:3px solid var(--accent);padding-left:14px;margin:16px 0}
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}

  /* ---------- top toolbar ---------- */
  .rd-tools{display:flex;justify-content:flex-end;gap:8px;padding:16px 0 0;flex-wrap:wrap}
  .rd-tools button{display:inline-flex;align-items:center;gap:7px;font:600 13px/1 inherit;
    background:var(--card);color:var(--dim);border:1px solid var(--line);border-radius:999px;
    padding:8px 14px;cursor:pointer}
  .rd-tools button:hover{color:var(--ink);border-color:var(--accent)}
  /* the how-to is the only cue that the page is annotatable — make it read as one */
  .rd-tools #rd-howto{color:var(--accent);
    border-color:color-mix(in srgb,var(--accent) 45%,transparent)}
  .rd-tools button[hidden]{display:none}
  .rd-tools + section > header{padding-top:26px}
  /* the fullscreen element is the root, which has no background of its own */
  html:fullscreen{background:var(--bg);overflow:auto}
  html:-webkit-full-screen{background:var(--bg);overflow:auto}

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
  .rd-tags button:disabled{cursor:default;opacity:.8}
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
  .rd-live{font-size:12px;line-height:1.45;color:var(--dim);padding:0 15px 10px}
  .rd-live .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;
    vertical-align:1px;background:var(--dim)}
  .rd-live.ok .dot,.rd-live.saving .dot{background:var(--good)}
  .rd-live.down{color:var(--warn)} .rd-live.down .dot{background:var(--warn)}
  #rd-reload{position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:95;display:flex;
    gap:12px;align-items:center;background:var(--card);border:1px solid var(--accent);
    border-radius:11px;padding:10px 12px 10px 16px;font:600 14px/1.3 inherit;
    box-shadow:0 6px 22px rgba(0,0,0,.35)}
  #rd-reload button{font:650 13px/1 inherit;background:var(--accent);color:#fff;border:none;
    border-radius:8px;padding:8px 13px;cursor:pointer}
  #rd-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:90;
    background:var(--good);color:#fff;padding:12px 20px;border-radius:10px;font:650 14px/1 inherit;
    display:none;box-shadow:0 6px 22px rgba(0,0,0,.35);text-align:center;max-width:88vw}
  #rd-help{position:fixed;inset:0;z-index:85;display:none;align-items:center;justify-content:center;
    padding:20px;background:rgba(0,0,0,.5)}
  #rd-help.open{display:flex}
  #rd-help .box{background:var(--card);border:1px solid var(--line);border-radius:15px;
    padding:22px 24px;width:100%;max-width:620px;max-height:84vh;overflow:auto;
    box-shadow:0 12px 40px rgba(0,0,0,.45)}
  #rd-help h3{margin:0 0 12px;font-size:19px}
  #rd-help p,#rd-help li{font-size:14.5px;line-height:1.6}
  #rd-help ul{margin:0 0 14px}
  #rd-help .row{display:flex;justify-content:flex-end;margin-top:6px}
  #rd-help .row button{font:650 14px/1 inherit;padding:11px 20px;border-radius:9px;cursor:pointer;
    background:var(--accent);color:#fff;border:none}
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
    #rd-fab,#rd-panel,#rd-bar,#rd-ed,#rd-toast,#rd-help,#rd-reload,.rd-tools{display:none!important}
    body{padding-right:0}
    mark.rd-hl{background:none;border-bottom:1px solid #999}
  }
"""

JS = r"""
(function(){
  "use strict";
  var DOC = __DOC__;
  var TAGS = __TAGS__;            /* offered in the picker, in this order */
  var LABELS = __LABELS__;        /* display labels, including the bar-only tags */
  var NO_BODY_TAGS = __NO_BODY_TAGS__;
  var CLOSING_TAGS = __CLOSING_TAGS__;
  /* keyed on the document's own id, not its file name: every file:// page may
     share one storage area, and two repos can both hold a plan.html */
  var KEY = 'reviewdoc:' + DOC.id;

  var embedded = [];
  try{ embedded = JSON.parse(document.getElementById('rd-notes').textContent || '[]') || []; }
  catch(e){ embedded = []; }

  var drafts = {notes:[], replies:[], patch:{}};
  try{
    var raw = JSON.parse(localStorage.getItem(KEY) || 'null');
    if(raw && raw.notes) drafts = {notes:raw.notes||[], replies:raw.replies||[], patch:raw.patch||{}};
  }catch(e){}

  pruneDrafts();
  if(window.RD_LIVE) takeHandoff();
  saveDrafts(true);

  /* once a draft shows up in the embedded block it must never resurrect */
  function pruneDrafts(){
    var byId = {}, mine = {};
    embedded.forEach(function(n){ byId[n.id] = n; });
    drafts.notes = drafts.notes.filter(function(n){ return !byId[n.id]; });
    drafts.notes.forEach(function(n){ mine[n.id] = 1; });
    drafts.replies = drafts.replies.filter(function(r){
      var host = byId[r.noteId];
      if(!host) return !!mine[r.noteId];
      return !(host.replies||[]).some(function(x){
        return x.ts === r.ts && x.body === r.body && x.author === r.author;
      });
    });
    /* same for an edit / resolve the embedded block has already caught up with,
       and for a patch whose thread is gone altogether (a delete that landed) */
    Object.keys(drafts.patch).forEach(function(id){
      var host = byId[id], p = drafts.patch[id];
      if(!host){ if(!mine[id]) delete drafts.patch[id]; return; }
      var pending = Object.keys(p).some(function(k){
        return (host[k] || (k === 'status' ? 'open' : '')) !== p[k];
      });
      if(!pending) delete drafts.patch[id];
    });
  }

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

  function saveDrafts(quiet){
    try{
      if(!drafts.notes.length && !drafts.replies.length && !Object.keys(drafts.patch).length)
        localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, JSON.stringify(
        {notes:drafts.notes, replies:drafts.replies, patch:drafts.patch}));
    }catch(e){}
    if(!quiet) scheduleSync();
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
  function label(k){ return LABELS[k] || TAGS[k] || k; }
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
      /* newest thing that happened in this thread, whoever did it */
      t._last = String(t.ts || '');
      t.replies.forEach(function(r){
        if(String(r.ts) > t._last) t._last = String(r.ts);
      });
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
      /* a real link (a backlog issue, say) should navigate, not jump the card */
      if(el.closest && el.closest('a[href]')){ e.stopPropagation(); return; }
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
  /* In a message body: §N and [label](#sec-id) become jumps into the document, so
     a reply can point at prose instead of repeating it; URLs (a backlog issue, say)
     become ordinary links. One alternation, so a link never nests inside a link. */
  var JUMP_RE = /\[([^\]\n]{1,80})\]\((#[A-Za-z0-9_-]+|https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<]+[^\s<.,;:!?)])|§\s?(\d+)/g;
  function linkify(s){
    return s.replace(JUMP_RE, function(m, txt, target, bare, num){
      if(target) return target.charAt(0) === '#'
        ? '<a class="rd-jump" data-sec="' + target.slice(1) + '">' + txt + '</a>'
        : '<a href="' + target + '" target="_blank" rel="noopener">' + txt + '</a>';
      if(bare) return '<a href="' + bare + '" target="_blank" rel="noopener">' + bare + '</a>';
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
    /* highlights read as a map of the document, so they stay in document order.
       notes are a work queue: newest activity on top, no section grouping. */
    if(tab === 'hl') return fill(hls, hlCard,
      'No highlights yet.<br><br>Select any text and press <b>🖍 Highlight</b> to mark a ' +
      'passage without writing anything. Highlights stay in this tab even if the wording ' +
      'later changes, so nothing you flagged gets lost.',
      'Not in the text any more');
    notes = notes.filter(function(t){ return !openOnly || t.status !== 'resolved'; });
    notes.sort(function(a,b){ return a._last < b._last ? 1 : (a._last > b._last ? -1 : 0); });
    fill(notes, card,
      'No notes yet.<br><br>Select any text in the document and a <b>💬 Note</b> button ' +
      'appears — ask a question, ask for a change, defer it to the backlog, or just agree.',
      'Unanchored — the quoted text has changed');

    function count(k, n){
      panel.querySelector('.rd-tabs [data-tab="' + k + '"] .k').textContent = n;
    }
    function fill(rows, make, empty, looseTitle){
      if(!rows.length){ list.innerHTML = '<p class="small">' + empty + '</p>'; return; }
      rows.filter(function(t){ return t._anchored || !t.quote; }).forEach(function(t){
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
    var blk = el && el.closest ? el.closest('p,li,td,th,h1,h2,h3,h4,pre,blockquote') : null;
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
  /* one-click filings: no modal, no typing — the quoted text is the whole point */
  function quick(kind, tag, done){
    var p = capture();
    if(!p) return;
    var n = { id:uid(), kind:kind, tag:tag, quote:p.quote,
              sectionId:p.sectionId, sectionTitle:p.sectionTitle, context:p.context,
              start:p.start, body:'', author:'user', ts:new Date().toISOString(),
              replies:[], status:'open', anchored:true };
    drafts.notes.push(n); saveDrafts();
    anchor(n);
    clearSel(); render(); toast(done);
  }
  bar.querySelector('[data-hl]').addEventListener('click', function(){
    quick('highlight', 'highlight', 'Highlighted — export when you are done');
  });
  bar.querySelector('[data-clarify]').addEventListener('click', function(){
    quick('note', 'clarify', 'Claude will clarify this — export when you are done');
  });

  var PLACEHOLDER = edText.getAttribute('placeholder') || '';
  var NO_BODY_HINT = {
    clarify: 'Optional — Claude clarifies the quoted text as it stands.',
    agree: 'Optional — saving files this as agreed and already closed.',
    defer: 'Optional — say what should happen; Claude opens a backlog issue for it.'
  };
  Object.keys(TAGS).forEach(function(k){
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = TAGS[k]; b.dataset.k = k;
    b.addEventListener('click', function(){ selTag(k); });
    edTags.appendChild(b);
  });
  function selTag(k){
    pendTag = k;
    var extra = edTags.querySelector('[data-extra]');
    if(extra) edTags.removeChild(extra);
    if(!TAGS[k]){
      /* filed from the selection bar, so not in the picker: show what it is
         rather than leaving nothing lit, but do not offer it as a choice */
      extra = document.createElement('button');
      extra.type = 'button'; extra.disabled = true;
      extra.dataset.extra = '1'; extra.dataset.k = k;
      extra.textContent = label(k);
      edTags.insertBefore(extra, edTags.firstChild);
    }
    [].forEach.call(edTags.querySelectorAll('button'), function(x){
      x.classList.toggle('sel', x.dataset.k === k);
    });
    edText.setAttribute('placeholder', NO_BODY_HINT[k] || PLACEHOLDER);
  }
  function openEd(quote, note){
    editing = note || null;
    var conv = !!note && note.kind === 'highlight';   /* highlight -> note */
    /* keep whatever tag the note already has, even one the picker does not offer,
       so editing the body of a Clarify note never silently re-tags it */
    selTag(conv || !note || !note.tag ? 'question' : note.tag);
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
      /* re-tagging to agree closes the thread, exactly as filing it that way
         does; re-tagging an agree to anything else opens it again */
      if(CLOSING_TAGS.indexOf(pendTag) >= 0) p.status = 'resolved';
      else if(CLOSING_TAGS.indexOf(editing.tag) >= 0) p.status = 'open';
      if(was === 'highlight') p.kind = 'note';
      closeEd();
      patch(id, p);
      if(was === 'highlight'){ setTab('notes'); render(); focusThread(id); }
      toast(was === 'highlight' ? 'Now a note — export when you are done'
                                : 'Note updated — export when you are done');
      return;
    }
    var closes = CLOSING_TAGS.indexOf(pendTag) >= 0;
    var n = { id:uid(), kind:'note', tag:pendTag, quote:pending ? pending.quote : '',
              sectionId:pending ? pending.sectionId : null,
              sectionTitle:pending ? pending.sectionTitle : 'General',
              context:pending ? pending.context : '',
              start:pending ? pending.start : null, body:body, author:'user',
              ts:new Date().toISOString(), replies:[],
              status: closes ? 'resolved' : 'open', anchored:true };
    drafts.notes.push(n); saveDrafts();
    anchor(n);
    closeEd(); setTab('notes'); render();
    toast(closes ? 'Filed as agreed and closed — export when you are done'
                 : 'Note saved — export when you are done');
  });
  edText.addEventListener('keydown', function(e){
    if((e.metaKey || e.ctrlKey) && e.key === 'Enter') edSave.click();
  });

  document.addEventListener('click', function(e){
    var m = e.target.closest && e.target.closest('mark.rd-hl');
    if(m) focusThread(m.dataset.noteId);
  });
  /* ---------- top toolbar: how-to, markdown, full screen ---------- */
  /* build renders the markdown once, in Python, and embeds it: one converter,
     not a second copy in here that drifts from `reviewdoc.py md` */
  var mdBtn = document.getElementById('rd-mdx');
  if(mdBtn) mdBtn.addEventListener('click', function(){
    var md = '';
    try{ md = JSON.parse(document.getElementById('rd-md').textContent || '""'); }catch(e){}
    if(!md){ toast('This page has no markdown copy — rebuild it'); return; }
    saveFile(md, DOC.slug + '.md', MD_FILE).then(function(how){
      if(how !== 'cancel') toast(how === 'picked' ? 'Markdown saved' : 'Markdown saved to Downloads');
    });
  });

  var help = document.getElementById('rd-help'),
      howBtn = document.getElementById('rd-howto'),
      fsBtn = document.getElementById('rd-fs'),
      root = document.documentElement,
      reqFs = root.requestFullscreen || root.webkitRequestFullscreen;

  function showHelp(on){ if(help) help.classList.toggle('open', !!on); }
  if(howBtn) howBtn.addEventListener('click', function(){ showHelp(true); });
  if(help) help.addEventListener('click', function(e){
    /* the backdrop is the overlay itself; anything inside .box keeps it open */
    if(e.target === help || (e.target.closest && e.target.closest('[data-help-close]')))
      showHelp(false);
  });

  /* Safari still only has the webkit-prefixed half of the Fullscreen API */
  function fsOn(){ return !!(document.fullscreenElement || document.webkitFullscreenElement); }
  function syncFs(){ if(fsBtn) fsBtn.textContent = fsOn() ? '⤡ Exit full screen'
                                                          : '⤢ Full screen'; }
  if(fsBtn && !reqFs) fsBtn.hidden = true;
  else if(fsBtn){
    fsBtn.addEventListener('click', function(){
      var call;
      try{
        if(fsOn()){
          var exit = document.exitFullscreen || document.webkitExitFullscreen;
          call = exit ? exit.call(document) : null;
        } else call = reqFs.call(root);
      }catch(err){ toast('Full screen is not available here'); return; }
      Promise.resolve(call).catch(function(){ toast('The browser refused full screen'); });
    });
    document.addEventListener('fullscreenchange', syncFs);
    document.addEventListener('webkitfullscreenchange', syncFs);
    syncFs();
  }

  /* ---------- panel collapse (desktop) + view state ---------- */
  var UIKEY = KEY + ':ui', ui = {};
  try{ ui = JSON.parse(localStorage.getItem(UIKEY) || '{}') || {}; }
  catch(e){ ui = {}; }
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
    if(help && help.classList.contains('open')) showHelp(false);
    else if(ed.classList.contains('open')) closeEd();
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
  // Prefer a native save dialog (File System Access API) so a file can land in
  // the project folder Claude is reading, not just ~/Downloads. Chrome remembers
  // the last-used directory per `kind.id`. Falls back to a plain download when
  // the API is missing or refuses (e.g. some file:// origins). Resolves to one of
  // 'picked' | 'download' | 'cancel'.
  var NOTES_FILE = {id:'reviewdoc-notes', what:'Notes JSON', mime:'application/json', ext:'.json'},
      MD_FILE = {id:'reviewdoc-md', what:'Markdown', mime:'text/markdown', ext:'.md'};
  function saveFile(text, name, kind){
    if(window.showSaveFilePicker){
      return (async function(){
        var handle, accept = {};
        accept[kind.mime] = [kind.ext];
        try{
          handle = await window.showSaveFilePicker({
            suggestedName: name, id: kind.id, types: [{description:kind.what, accept:accept}]
          });
        }catch(err){
          if(err && err.name === 'AbortError') return 'cancel';
          download(text, name, kind.mime); return 'download';
        }
        try{
          var w = await handle.createWritable();
          await w.write(text); await w.close();
          return 'picked';
        }catch(err){
          download(text, name, kind.mime); return 'download';
        }
      })();
    }
    download(text, name, kind.mime);
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
    /* the doc id rides along so `import` can refuse notes meant for another page */
    var out = {doc:DOC.id, file:DOC.file, notes:ts};
    Promise.resolve(saveFile(JSON.stringify(out, null, 1), DOC.slug + '-notes.json', NOTES_FILE))
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

  /* ---------- live mode: the page as served by `reviewdoc.py serve` ----------
     The server injects RD_LIVE into the page it serves. Every change goes into
     the file on disk within a second, Claude's replies arrive without a
     reload, and Send to Claude wakes whichever session is waiting on this
     page. When the server goes away the page carries on: drafts stay in this
     browser (the origin is a stable localhost port) and go up as soon as it
     answers again. Opened as a plain file, the page looks for a server and
     offers the live copy, carrying any unsent drafts across in the URL. */
  var LIVE = window.RD_LIVE || null, liveUp = false, waiting = 0,
      REV = LIVE ? LIVE.rev : null, CONTENT = LIVE ? LIVE.content : null,
      syncTimer = null, inflight = null, again = false,
      statusEl = panel.querySelector('[data-live]'),
      sendBtn = panel.querySelector('[data-send-claude]'),
      exportBtn = panel.querySelector('[data-export]');

  function hasDrafts(){
    return !!(drafts.notes.length || drafts.replies.length || Object.keys(drafts.patch).length);
  }
  function api(method, path, body){
    return fetch(LIVE.api + path, {method:method, cache:'no-store',
      headers:{'X-Reviewdoc-Token':LIVE.token, 'Content-Type':'application/json'},
      body: body ? JSON.stringify(body) : undefined
    }).then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); });
  }
  function scheduleSync(){
    if(!LIVE) return;
    clearTimeout(syncTimer); syncTimer = setTimeout(sync, 400);
  }
  /* resolves true once everything local is in the file */
  function sync(){
    if(!LIVE) return Promise.resolve(false);
    if(inflight){ again = true; return inflight; }
    if(!hasDrafts()) return Promise.resolve(liveUp);
    setStatus('saving');
    inflight = api('POST', '/notes', {doc:DOC.id, notes:payload()}).then(function(st){
      inflight = null; applyState(st);
      if(again){ again = false; return sync(); }
      return true;
    }, function(){ inflight = null; liveDown(); return false; });
    return inflight;
  }
  function poll(){
    api('GET', '/state').then(function(st){
      applyState(st);
      if(hasDrafts()) scheduleSync();
    }, liveDown).then(function(){ setTimeout(poll, liveUp ? 2000 : 5000); });
  }
  function liveDown(){ liveUp = false; setStatus('down'); }
  /* typing a reply in the panel: a re-render now would throw the text away */
  function busy(){
    var a = document.activeElement;
    return !!(a && a.tagName === 'TEXTAREA' && a.closest && a.closest('#rd-panel') && a.value);
  }
  function claudeCount(){
    var k = 0;
    embedded.forEach(function(n){
      (n.replies || []).forEach(function(r){ if(r.author === 'claude') k++; });
    });
    return k;
  }
  function applyState(st){
    liveUp = true;
    waiting = st.waiting || 0;
    if(st.content && CONTENT && st.content !== CONTENT) showReload();
    if(st.rev !== REV && !busy()){       /* busy: REV stays stale, next poll retries */
      var before = claudeCount();
      REV = st.rev;
      adopt(st.notes || []);
      if(claudeCount() > before) toast('Claude replied');
    }
    setStatus(inflight ? 'saving' : 'ok');
  }
  /* the file's notes are the truth; redraw marks and panel against them */
  function adopt(notes){
    embedded = notes;
    pruneDrafts(); saveDrafts(true);
    [].forEach.call(document.querySelectorAll('mark.rd-hl'), function(m){
      unmark(m.dataset.noteId);
    });
    threads().forEach(function(t){ if(t.status !== 'deleted') anchor(t); });
    render();
  }
  function setStatus(kind){
    if(!statusEl) return;
    var on = '<span class="dot"></span>', t;
    if(kind === 'offline') t = on + 'Offline copy. Notes stay in this browser until you export them.';
    else if(kind === 'down') t = on + '<b>Claude Code is not running.</b> Keep going: your notes ' +
      'stay in this browser and are saved to the file as soon as it is back. Or export them.';
    else if(kind === 'saving') t = on + 'Live · saving…';
    else t = on + 'Live · saved to the file' + (waiting ? ' · <b>Claude is waiting</b>' : '');
    statusEl.innerHTML = t;
    statusEl.className = 'rd-live ' + kind;
  }
  function showReload(){
    if(document.getElementById('rd-reload')) return;
    var b = document.createElement('div');
    b.id = 'rd-reload';
    b.innerHTML = '<span>Claude rebuilt this page.</span><button type="button">Reload</button>';
    b.querySelector('button').addEventListener('click', function(){
      sync().then(function(){ location.reload(); });
    });
    document.body.appendChild(b);
  }
  if(sendBtn) sendBtn.addEventListener('click', function(){
    sync().then(function(ok){
      if(!ok){ toast('Claude Code is not running. Your notes are safe in this browser'); return; }
      return api('POST', '/send', {doc:DOC.id}).then(function(r){
        waiting = r.waiting || 0; setStatus('ok');
        toast(r.waiting ? 'Sent. Claude is reading your notes'
                        : 'Saved. No Claude session is waiting on this page: tell Claude "notes sent"');
      });
    }).catch(function(){ liveDown(); toast('Could not reach the server. Your notes are kept in this browser'); });
  });

  /* unsent drafts ride from the file:// copy to the live one in the URL
     fragment: a navigation, so no cross-origin request is involved */
  function b64(s){
    return btoa(unescape(encodeURIComponent(s))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function unb64(s){
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    while(s.length % 4) s += '=';
    return decodeURIComponent(escape(atob(s)));
  }
  function takeHandoff(){
    var m = /^#rd-drafts=([A-Za-z0-9_-]+)$/.exec(location.hash);
    if(!m) return;
    try{
      var d = JSON.parse(unb64(m[1]));
      (d.notes || []).forEach(function(n){
        if(!drafts.notes.some(function(x){ return x.id === n.id; })) drafts.notes.push(n);
      });
      (d.replies || []).forEach(function(r){
        if(!drafts.replies.some(function(x){ return x.noteId === r.noteId && x.ts === r.ts; }))
          drafts.replies.push(r);
      });
      Object.keys(d.patch || {}).forEach(function(id){
        var p = drafts.patch[id] || (drafts.patch[id] = {});
        for(var k in d.patch[id]) p[k] = d.patch[id][k];
      });
    }catch(e){}
    history.replaceState(null, '', location.pathname + location.search);
    pruneDrafts();
  }
  /* an <img> probe: a plain GET the server answers only for a document it
     has registered, and one a file:// page is allowed to make */
  function probe(){
    var ports = DOC.ports || [], i = 0;
    (function next(){
      if(i >= ports.length){ setTimeout(probe, 20000); return; }
      var p = ports[i++], img = new Image(), settled = false,
          timer = setTimeout(function(){ done(false); }, 1200);
      function done(ok){
        if(settled) return;
        settled = true; clearTimeout(timer);
        if(ok) offerLive(p); else next();
      }
      img.onload = function(){ done(true); };
      img.onerror = function(){ done(false); };
      img.src = 'http://127.0.0.1:' + p + '/probe/' + DOC.id + '.gif?' + Date.now();
    })();
  }
  function offerLive(p){
    var url = 'http://127.0.0.1:' + p + '/d/' + DOC.id + '/';
    statusEl.innerHTML = '<span class="dot"></span>A live session is running for this page. ' +
      '<a href="' + url + '" data-golive>Open it live</a>' +
      (hasDrafts() ? ', your unsent notes come along.' : '.');
    statusEl.className = 'rd-live ok';
    statusEl.querySelector('[data-golive]').addEventListener('click', function(e){
      e.preventDefault();
      location.href = url + (hasDrafts() ? '#rd-drafts=' + b64(JSON.stringify(drafts)) : '');
    });
  }

  /* one pass over the merged view, so a locally-deleted mark never comes back
     and highlights get the marker-pen class rather than the note one */
  threads().forEach(function(t){ if(t.status !== 'deleted') anchor(t); });
  syncTabs();
  render();
  if(LIVE){
    sendBtn.hidden = false;
    exportBtn.className = 'rd-lite';
    exportBtn.innerHTML = 'Export notes as JSON';
    setStatus('ok');
    poll();
  } else {
    setStatus('offline');
    if(location.protocol === 'file:' && (DOC.ports || []).length) probe();
  }
})();
"""

PANEL = """
<div id="rd-bar">
  <button type="button" data-hl>&#128396; Highlight</button>
  <button type="button" data-clarify>&#128269; Clarify</button>
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
  <div class="rd-live" data-live></div>
  <label class="rd-only"><input type="checkbox" data-openonly> Show open only</label>
  <div class="rd-list"></div>
  <div class="rd-foot">
    <button class="rd-big" type="button" data-send-claude hidden>&#10148; Send to Claude</button>
    <button class="rd-big" type="button" data-export>&#10003; Export notes for Claude</button>
    <button class="rd-lite" type="button" data-copy>Copy notes as markdown</button>
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

HELP = """
<div id="rd-help" role="dialog" aria-modal="true" aria-label="How to use this page">
  <div class="box">
    <h3>This page is for you to mark up</h3>
    <p>Select any sentence in the document and a small bar appears with three buttons:</p>
    <ul>
      <li><b>&#128396; Highlight</b> &mdash; marks the passage in amber. Nothing to type.</li>
      <li><b>&#128269; Clarify</b> &mdash; asks Claude what a word or phrase means. One click,
          the quoted text is the whole question.</li>
      <li><b>&#128172; Note</b> &mdash; attaches a question, a request to change something, a
          <b>defer to backlog</b> (Claude opens an issue for it), or a plain <b>agree</b>, which
          files itself closed and needs no typing.</li>
    </ul>
    <p>The review panel keeps notes and highlights in separate tabs, newest first. You can edit or
    delete your own, and mark a thread resolved &mdash; closing a thread is yours to decide, never
    Claude's.</p>
    <p><b>Live</b> (the panel says so): every note is saved to the file as you write it, Claude's
    replies appear without a reload, and <b>&#10148; Send to Claude</b> tells the waiting session
    you are done. If Claude Code stops, keep going: notes wait in this browser and are saved once it
    is back.</p>
    <p><b>Offline copy</b> (the file opened on its own): everything stays in this browser until you
    press <b>&#10003; Export notes for Claude</b>, which saves a JSON file. Tell Claude when you're
    done and it will read your notes, reply in the panel, and rebuild the page.</p>
    <p class="small">&#8677; in the panel header hides the panel and centres the text.
    <b>&#8595; Markdown</b> at the top of the page saves the document itself as a
    markdown file. <b>&#10530; Full screen</b> hides the browser chrome.</p>
    <div class="row"><button type="button" data-help-close>Got it</button></div>
  </div>
</div>
"""


def toolbar(with_help):
    """The row of page-level buttons above the title."""
    bits = []
    if with_help:
        bits.append('<button type="button" id="rd-howto">&#9432; How to use this page</button>')
    bits.append('<button type="button" id="rd-mdx" title="Save this document as a markdown '
                'file">&#8595; Markdown</button>')
    bits.append('<button type="button" id="rd-fs">&#10530; Full screen</button>')
    return '<div class="rd-tools">%s</div>' % "".join(bits)


def fill(template, values):
    """One pass over the template only, so a document that happens to contain
    `__JS__` in its prose is never mistaken for a placeholder."""
    return re.sub(r"__([A-Z_]+)__", lambda m: values.get(m.group(1), m.group(0)), template)


def js_json(obj):
    """JSON that is safe to drop inside a <script> element."""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def render_html(src_text, src_path, out_name, old_html=None):
    stem = os.path.splitext(out_name)[0]
    meta, title, sections = parse_source(src_text, src_path, stem)
    slug = slugify(meta.get("slug") or stem)
    docid = doc_id(old_html) or uuid.uuid4().hex[:16]

    # the toolbar sits inside .wrap but outside every section, so its button
    # labels never land in the text a note is anchored against
    with_help = meta.get("hint", "").lower() not in ("false", "no", "off")
    body_html = toolbar(with_help) + "".join(
        '<section class="rd-sec" id="%s" data-title="%s">%s<!--/rd-sec--></section>'
        % (sid, esc(stitle), body)
        for sid, stitle, body in sections
    )

    old_notes = read_notes(old_html, out_name) if old_html else []
    notes = [normalise(n, i) for i, n in enumerate(old_notes)]
    stats = reanchor(notes, [(s[0], s[1], section_text(s[2])) for s in sections])

    js = fill(JS, {
        "DOC": js_json({"id": docid, "slug": slug, "file": out_name, "title": title,
                        "ports": live_ports()}),
        "TAGS": js_json(TAGS),
        "LABELS": js_json(dict(BAR_TAGS, **TAGS)),
        "NO_BODY_TAGS": js_json(sorted(NO_BODY_TAGS)),
        "CLOSING_TAGS": js_json(sorted(CLOSING_TAGS)),
    })
    payload = json.dumps(notes, ensure_ascii=False, indent=1)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    md = js_json(to_markdown(sections, out_name, slug))

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="reviewdoc-id" content="__ID__">
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
<script id="rd-md" type="application/json">__MD__</script>
<script>__JS__</script>
</body>
</html>
"""
    return fill(html, {
        "ID": docid,
        "TITLE": esc(title),
        "CSS": CSS,
        "BODY": body_html,
        "PANEL": PANEL + (HELP if with_help else ""),
        "NOTES": payload,
        "MD": md,
        "JS": js,
    }), stats


# --------------------------------------------------------------------------
# shared state: where the CLI and the live server (live.py) meet
# --------------------------------------------------------------------------

# The live server listens on the first free port of this range. A stable port
# matters: it is the page's origin, so drafts kept in the browser while the
# server is down are found again when it comes back.
DEFAULT_PORT = 47700
PORT_SPAN = 10


def state_dir(*parts):
    """~/.cache/reviewdoc/<parts>, created on first use. One per user, shared
    by every Claude Code session on the machine."""
    root = os.environ.get("REVIEWDOC_STATE") or os.path.join(
        os.path.expanduser("~"), ".cache", "reviewdoc")
    path = os.path.join(root, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def live_ports():
    try:
        base = int(os.environ.get("REVIEWDOC_PORT") or DEFAULT_PORT)
    except ValueError:
        base = DEFAULT_PORT
    return list(range(base, base + PORT_SPAN))


@contextlib.contextmanager
def doc_lock(path):
    """Serialise read-modify-write of one document between the CLI (replies,
    imports, rebuilds) and the live server (the reader's autosave), which may
    run in any number of processes at once."""
    key = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:20]
    try:
        f = open(os.path.join(state_dir("locks"), key + ".lock"), "w")
    except OSError:
        yield                   # no state dir: unlocked, as it always was
        return
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def doc_id(html):
    m = DOCID_RE.search(html or "")
    return m.group(1) if m else None


def register_doc(path, docid, title):
    """Tell the live server this document exists, so it will serve it. Only
    registered documents are ever served."""
    if not docid:
        return
    try:
        rec = {"path": os.path.abspath(path), "title": title, "ts": now_iso()}
        write(os.path.join(state_dir("docs"), docid + ".json"), json.dumps(rec))
    except OSError:
        pass


def merge_notes(notes, incoming):
    """Fold the reader's notes into the document's. The reader owns every
    thread's question and status; replies from either side are unioned, so
    nothing Claude wrote in the meantime is lost. -> counts."""
    by_id = dict((n["id"], n) for n in notes if "id" in n)
    c = {"added": 0, "replies": 0, "edited": 0, "removed": 0}
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
                c["removed"] += 1
            continue
        if cur is None:
            notes.append(n)
            by_id[n["id"]] = n
            c["added"] += 1
            continue
        have = set((r.get("author"), r.get("ts"), r.get("body")) for r in cur["replies"])
        for r in n["replies"]:
            key = (r.get("author"), r.get("ts"), r.get("body"))
            if key not in have:
                cur["replies"].append(r)
                have.add(key)
                c["replies"] += 1
        for k in ("kind", "tag", "body", "status", "context"):
            if k in raw and n[k] != cur.get(k):
                cur[k] = n[k]
                c["edited"] += 1
    return c


def unwrap_export(incoming, html, source):
    """An export is {doc, file, notes} (or a bare list from older pages).
    -> the notes list, after refusing one meant for another document."""
    if isinstance(incoming, dict):
        mine = doc_id(html)
        if mine and incoming.get("doc") and incoming["doc"] != mine:
            raise ValueError("%s was exported from another document (%s)"
                             % (source, incoming.get("file") or incoming["doc"]))
        incoming = incoming.get("notes")
    if not isinstance(incoming, list):
        raise ValueError("%s must contain a JSON array of notes" % source)
    if not all(isinstance(x, dict) and "id" in x for x in incoming):
        raise ValueError("%s: every note must be an object with an \"id\"" % source)
    return incoming


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

SRC_EXT = ".src.html"

STARTER = """<h1>__TITLE__</h1>
<p class="sub">One sentence saying what this is and why it matters to the reader.</p>
<p class="meta">__META__</p>

<p class="lede">Open with the whole answer in plain language, before any detail.</p>

<h2>What is going on</h2>

<div class="analogy">
  <p>A concrete everyday comparison, for a mechanism that is abstract to this reader.</p>
</div>

<div class="flag warn" data-pill="heads up">
  <h3>Something worth pausing on</h3>
  <p>Explain the risk in one short paragraph.</p>
</div>

<h2>What I recommend</h2>

<ol class="steps">
  <li>First thing to do.</li>
  <li>Second thing.</li>
  <li>Third thing.</li>
</ol>

<h2>What I need from you</h2>

<table>
  <tr><th>Decision</th><th>My recommendation</th></tr>
  <tr><td>Something you must choose</td><td><code>yes</code>, and why</td></tr>
</table>
"""


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except IOError as e:
        die("cannot read %s (%s)" % (path, e.strerror))


def write(path, text):
    """Write via a temp file and rename, so the live server never serves a
    half-written document."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except IOError as e:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        die("cannot write %s (%s)" % (path, e.strerror))


def cmd_new(a):
    base = re.sub(r"(\.src)?\.html$", "", a.name)
    path = base + SRC_EXT
    if os.path.exists(path):
        die("%s already exists" % path)
    title = a.title or os.path.basename(base).replace("-", " ").replace("_", " ").capitalize()
    write(path, STARTER.replace("__TITLE__", esc(title)).replace(
        "__META__", datetime.now().strftime("%Y-%m-%d")))
    print("wrote %s" % path)


def cmd_build(a):
    if a.src.endswith(".md"):
        die("%s: sources are HTML fragments now (<name>%s), markdown is no longer "
            "read. See \"Source format\" in SKILL.md." % (a.src, SRC_EXT))
    if not os.path.exists(a.src):
        die("no such source file: %s" % a.src)
    if a.out:
        out = a.out
    elif a.src.endswith(SRC_EXT):
        out = a.src[: -len(SRC_EXT)] + ".html"
    else:
        die("name the source <name>%s (it builds <name>.html), or pass -o" % SRC_EXT)
    if os.path.abspath(out) == os.path.abspath(a.src):
        die("the output would overwrite the source")
    src = read(a.src)
    with doc_lock(out):
        old = read(out) if os.path.exists(out) else None
        html, stats = render_html(src, a.src, os.path.basename(out), old)
        write(out, html)
    register_doc(out, doc_id(html), plain(DOC_TITLE_RE.search(html).group(1)))
    msg = "built %s (%d bytes)" % (out, len(html.encode("utf-8")))
    if old:
        msg += " — notes: %d kept, %d re-anchored, %d unanchored" % (
            stats["ok"],
            stats["moved"],
            stats["lost"],
        )
    print(msg)
    if a.open:
        open_doc(out, html)


def open_doc(path, html):
    """Open the live page when the server is up, the file itself otherwise."""
    target = os.path.abspath(path)
    try:
        import live
        live.ensure_live()
        url = live.url_for(doc_id(html))
    except ImportError:
        url = None
    if url and os.environ.get("BROWSER"):
        # VS Code sets $BROWSER in remote terminals to a helper that opens the
        # URL on the reader's own machine and forwards the port on the way;
        # the webbrowser module honours it
        import webbrowser
        print("live: %s (opening through $BROWSER)" % url)
        webbrowser.open(url)
        return
    if url:
        target = url
        print("live: %s" % url)
    else:
        print("offline: the live server is not running; opening the file itself: %s" % target)
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        sys.stderr.write("reviewdoc: could not run %s; open it by hand\n" % opener)


def cmd_md(a):
    html = read(a.doc)
    read_notes(html, a.doc)             # proves it is a reviewdoc build
    out = a.out or os.path.splitext(a.doc)[0] + ".md"
    if os.path.exists(out) and not a.force:
        with open(out, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        if not first.startswith(GEN_MARK):
            die("%s exists and was not written by `md`; pass --force to replace it" % out)
    stem = os.path.splitext(os.path.basename(out))[0]
    figs = []
    md = to_markdown(SEC_RE.findall(html), os.path.basename(a.doc), stem, figs)
    write(out, md)
    for name, svg in figs:
        write(os.path.join(os.path.dirname(out), name), svg)
    print("wrote %s%s" % (out, " + %d figure file(s)" % len(figs) if figs else ""))


def cmd_import(a):
    try:
        raw = json.loads(read(a.notes))
    except ValueError as e:
        die("%s is not valid JSON (%s)" % (a.notes, e))
    with doc_lock(a.doc):
        html = read(a.doc)
        notes = read_notes(html, a.doc)
        try:
            incoming = unwrap_export(raw, html, a.notes)
        except ValueError as e:
            die(str(e))
        c = merge_notes(notes, incoming)
        reanchor(notes, doc_sections(html))
        write(a.doc, write_notes(html, notes))
    loose = sum(1 for n in notes if not n.get("anchored"))
    hl = sum(1 for n in notes if n.get("kind") == "highlight")
    print(
        "imported into %s: %d new, %d new repl(ies), %d edit(s), %d removed — "
        "%d thread(s) + %d highlight(s), %d unanchored"
        % (a.doc, c["added"], c["replies"], c["edited"], c["removed"],
           len(notes) - hl, hl, loose)
    )


def is_hl(n):
    return n.get("kind") == "highlight"


def is_defer(n):
    return not is_hl(n) and n.get("tag") == "defer"


def cmd_list(a):
    notes = read_notes(read(a.doc), a.doc)
    hl = [n for n in notes if is_hl(n)]
    dfr = [n for n in notes if is_defer(n)]
    if a.highlights:
        notes = hl
    elif a.deferred:
        notes = dfr
    else:
        notes = [n for n in notes if not is_hl(n)]
        if a.open_only:
            notes = [n for n in notes if n.get("status") != "resolved"]
    if a.json:
        print(json.dumps(notes, ensure_ascii=False, indent=2))
        return
    if not notes:
        print("no highlights" if a.highlights
              else "nothing deferred" if a.deferred else "no notes")
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
    if not (a.highlights or a.deferred):
        if dfr:
            print("+ %d deferred — each needs a backlog issue: reviewdoc.py list %s --deferred"
                  % (len(dfr), a.doc))
        if hl:
            print("+ %d highlight(s) — no reply needed: reviewdoc.py list %s --highlights"
                  % (len(hl), a.doc))


def _save_notes(path, html, notes):
    write(path, write_notes(html, notes))


READER_CLOSES = (
    "closing a thread is the reader's call, not yours. They resolve it from the "
    "\"Mark resolved\" button in the review panel and it arrives on the next import. "
    "Reply and leave the thread open."
)


def cmd_reply(a):
    if a.resolve:
        die(READER_CLOSES)
    with doc_lock(a.doc):
        _reply(a)


def _reply(a):
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


def doc_arg(arg):
    """A command's <doc.html>: a path, or else the bare file name of a
    registered document ("plan.html" or "plan"), so it works from any folder.
    A name two registered documents share is refused, never guessed."""
    if os.path.exists(arg) or "/" in arg:
        return arg
    try:
        import live
    except ImportError:
        return arg
    hits = live.by_name(arg)
    if len(hits) > 1:
        die("%s names %d registered documents; pass the path:\n  %s"
            % (arg, len(hits), "\n  ".join(h[1] for h in hits)))
    if hits:
        sys.stderr.write("reviewdoc: %s is %s\n" % (arg, hits[0][1]))
        return hits[0][1]
    return arg


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `reply` takes a free-text positional, so argparse cannot tell where the
    # flags go. Pull them out first and the flag may sit anywhere on the line.
    flags = set()
    if argv[:1] == ["reply"]:
        argv = [x for x in argv if not (x in ("--resolve", "--all-open") and not flags.add(x))]

    p = argparse.ArgumentParser(prog="reviewdoc.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd")

    q = sub.add_parser("new", help="scaffold a starter <name>.src.html")
    q.add_argument("name")
    q.add_argument("--title")
    q.set_defaults(fn=cmd_new)

    q = sub.add_parser("build", help="check <name>.src.html, write <name>.html, keeping notes")
    q.add_argument("src")
    q.add_argument("-o", "--out")
    q.add_argument("--open", action="store_true")
    q.set_defaults(fn=cmd_build)

    q = sub.add_parser("md", help="write a built document out as markdown, best effort")
    q.add_argument("doc")
    q.add_argument("-o", "--out")
    q.add_argument("--force", action="store_true",
                   help="replace an existing .md that `md` did not write")
    q.set_defaults(fn=cmd_md)

    def live_cmd(name):
        def run(a):
            import live
            return getattr(live, "cmd_" + name)(a)
        return run

    q = sub.add_parser("open", help="open a document: the live page if the server runs, else the file")
    q.add_argument("doc")
    q.set_defaults(fn=live_cmd("open"))

    q = sub.add_parser("wait", help="block until the reader sends or exports notes (run in background)")
    q.add_argument("doc")
    q.add_argument("--timeout", type=int, default=0, help="seconds; 0 waits as long as the session lives")
    q.set_defaults(fn=live_cmd("wait"))

    q = sub.add_parser("serve", help="run the live server in the foreground")
    q.add_argument("--port", type=int)
    q.add_argument("--forever", action="store_true",
                   help="do not stop when the last Claude Code session ends")
    q.add_argument("--spawned", action="store_true", help=argparse.SUPPRESS)
    q.add_argument("--launchd", action="store_true", help=argparse.SUPPRESS)
    q.set_defaults(fn=live_cmd("serve"))

    q = sub.add_parser("install", help="macOS: LaunchAgent that starts the live server for "
                                       "sandboxed Claude Code (run once, in Terminal)")
    q.set_defaults(fn=live_cmd("install"))

    q = sub.add_parser("uninstall", help="remove the LaunchAgent and stop the server")
    q.set_defaults(fn=live_cmd("uninstall"))

    q = sub.add_parser("status", help="show the live server, sessions and documents")
    q.set_defaults(fn=live_cmd("status"))

    q = sub.add_parser("stop", help="stop the live server")
    q.set_defaults(fn=live_cmd("stop"))

    q = sub.add_parser("import", help="merge exported notes JSON into a built doc")
    q.add_argument("doc")
    q.add_argument("notes")
    q.set_defaults(fn=cmd_import)

    q = sub.add_parser("list", help="print threads, one line each")
    q.add_argument("doc")
    q.add_argument("--open-only", action="store_true")
    q.add_argument("--highlights", action="store_true",
                   help="list the reader's highlights instead of the note threads")
    q.add_argument("--deferred", action="store_true",
                   help="list only threads tagged defer (each wants a backlog issue)")
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
    if getattr(a, "doc", None):
        a.doc = doc_arg(a.doc)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
