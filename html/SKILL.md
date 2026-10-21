---
name: html
description: Generate a self-contained, annotatable HTML review document with a Word-style threaded comment panel, then read the user's comments and reply to them in place. Use when you are about to present a dense or complicated plan, diagnosis, design doc, architecture proposal, security review, migration strategy or post-mortem — anything with enough moving parts, branching decisions or cross-cutting mechanism that a wall of terminal markdown would be hard to review, and especially when you need decisions back on specific points rather than a yes/no. Also use whenever the user asks to "review", "annotate", "comment on", or "mark up" something, and to continue an existing review round trip after they say they have exported their notes. Skip it for short answers, single-decision questions, and anything they can act on by reading one screen.
---

# html review documents

`reviewdoc.py` (Python 3 stdlib only, no install) builds and edits the
document; `live.py` beside it is the optional real-time server. The source is
an HTML fragment, `<name>.src.html`, in a small fixed vocabulary; the output is
`<name>.html`, one file that works over `file://` with no network. Notes live
inside the output in a `<script id="rd-notes">` JSON block, so rebuilding never
loses them and you can read and answer them from the CLI.

The document works two ways, and you drive both with the same commands:

- **Live**, when the server can run: the page is served from `127.0.0.1`,
  every note is written into `<name>.html` as the reader types, your replies
  appear in the open page within seconds, and **Send to Claude** wakes you.
- **Offline**, always: the file opened on its own. Notes wait in the browser
  until the reader exports them, exactly as before. A live page whose server
  goes away drops to offline by itself and catches up when it is back.

Invoke as `python3 ~/.claude/skills/html/reviewdoc.py`. Put the source and the
output **in the current session's working directory** (the repo you were
invoked in), or a `docs/` subfolder of it — never `/tmp`, and avoid
`~/.claude/plans` or `~/Downloads`. The reason is the export round-trip: on
macOS you frequently cannot read `~/Downloads` back, so the notes file must
land somewhere inside a working directory. The "Export notes for Claude" button
opens a native save dialog (Chrome remembers the last folder), so the user can
point it at the doc's own folder and you read it straight from there.

## The loop

```
reviewdoc.py new <name> --title "T"          # optional scaffold: <name>.src.html
reviewdoc.py build <name>.src.html --open    # prints "live: <url>" or "offline: ..."
reviewdoc.py wait <name>.html                # as a BACKGROUND task, then STOP and wait
   ... the reader annotates, then presses Send to Claude (live) or saves an Export ...
   ... `wait` exits, and that wakes you. Its last line says which: ...
   "sent: ..."          the notes are already in <name>.html: go straight to list
   "exported: <path>"   reviewdoc.py import <name>.html <path>   first
reviewdoc.py list <name>.html                # note threads: ids, tags, quotes, reply counts
reviewdoc.py list <name>.html --json         # exact bodies, read this before answering
reviewdoc.py list <name>.html --deferred     # threads that each want a backlog issue
reviewdoc.py list <name>.html --highlights   # passages they marked without asking anything
reviewdoc.py reply <name>.html <id> "answer" # threads stay open; only the reader closes them
reviewdoc.py build <name>.src.html           # only if you changed the prose; notes survive
reviewdoc.py wait <name>.html                # background again, for the next round
```

Live, replies reach the open page on their own; a rebuild shows the reader a
**Reload** banner. Offline, tell them to reload. Either way, say in chat what
you answered and what you changed.

Rules for the loop:

- After `build --open`, start `wait` with the Bash tool's `run_in_background:
  true` and **stop**. Never run it in the foreground: it blocks until the reader
  acts, which can take hours. Do not start editing project files while the
  user is still annotating.
- `wait` ends when the reader presses Send to Claude or saves an Export beside
  the document or in `~/Downloads`. If it ends any other way (a timeout you
  gave it, a restart), start it again rather than assuming anything.
- The user may also just say "done" or "notes exported" in chat, with or
  without a `wait` running. Then `import` whatever they exported (look beside
  the document, then `~/Downloads`), or, live, go straight to `list`.
- When the notes are in: `list --json` and actually read every body, then
  `reply` to each thread, then `build` if the prose changed, then start `wait`
  again. Only after that should you touch project files — their notes may
  change the plan.
- If `open` or `build --open` prints that live review is off because of the
  sandbox, tell the user once that it takes one command in a normal Terminal:
  `python3 ~/.claude/skills/html/reviewdoc.py install`. Do not try it
  yourself: Claude Code's sandbox refuses it by design, and offline mode
  works fine meanwhile.
- Reply in the document, not only in chat. The panel is where they are reading.
- **Never resolve a thread.** Closing a comment is the reader's judgement, not
  yours — they have a "Mark resolved" button in the panel and it arrives on the
  next import. `--resolve` and the `resolve` command both refuse and exit
  non-zero. Answer, leave it open, let them close it.
- Answer every open thread. `reply <doc> --all-open "text"` exists for a blanket
  acknowledgement but is rarely the right thing. `agree` threads arrive already
  resolved and are not yours to answer; `defer` threads want a backlog issue —
  see "Tags the user can apply".
- The reader can also edit their own notes and delete their own replies in the
  panel. `import` takes their `tag`, `body` and `status` as authoritative, so a
  question may come back reworded — answer the version you just imported, and do
  not argue with an edit.
- The export carries the document's id. `import` refuses a notes file exported
  from a different document, so two docs that share a name cannot cross over.
- Rebuild is safe: each note is re-found by its quoted text (moving between
  sections is fine). A note whose quote no longer exists is kept and shown under
  **Unanchored**, never dropped. `build` prints `N kept, N re-anchored, N unanchored`.

### Highlights are not questions

The reader has two gestures on a selection: **🖍 Highlight** marks a passage,
**💬 Note** attaches a comment. They live in separate tabs of the panel and
`kind` tells them apart in the JSON.

- `list` shows note threads only and prints a highlight count at the end.
  `list --highlights` shows the highlights.
- **Never reply to a highlight.** `reply` on one exits non-zero, and
  `--all-open` skips them. Nobody asked you anything — do not treat a highlight
  as a request to explain the passage, expand it, or change it.
- **Do read them.** They are a free signal about what the reader cared about. A
  cluster of highlights in one section means that section is load-bearing for
  them; three highlights and no notes on a decision table means they are weighing
  it. Let that shape what you emphasise next, silently.
- A highlight that no longer matches any text is kept in the panel with its
  original section and the opening of the paragraph it came from, so nothing
  the reader marked is ever lost to a rewrite.
- The reader can promote a highlight to a note ("Add a note" on the card). It
  arrives as a normal thread with `kind` flipped to `note` — answer it then.
- Highlights are in the export and live in the HTML alongside the notes. **The
  only thing you ever change about one is where it points**, and only as a side
  effect of `build` re-anchoring it — which is what keeps it on the right words
  when you edit the prose. Never touch a highlight's `quote`, `body`, `tag` or
  `status`, and never hand-edit the `<script id="rd-notes">` block; go through
  the CLI so the anchoring stays consistent.

### Where the answer goes — panel or corpus, not both

Every answer lives in exactly one place. Duplicating it makes the page longer
without making it clearer, and leaves two copies to drift apart.

Decide per thread:

- **Panel only** — the default for anything local: "what is this?", "what does
  that flag do?", a definition, a number, a yes/no, a `clarify` on one phrase.
  Reply in the thread and leave the source alone. Do not bolt a new paragraph
  onto the document because one reader asked one question; the answer is
  already attached to the exact sentence that prompted it.
- **Corpus only, with a pointer in the panel** — when the answer changes the
  plan, corrects something wrong, or is something the *next* reader of the
  document also needs. Edit the `.src.html`, rebuild, and make the reply a
  one-liner that points at the change: `Wrong as written — rewrote §4, it now
  says the patch runs before startup.` Do not paste the new prose into the reply
  as well.
- Reply bodies linkify `§N` (section number), `[label](#sec-id)` and bare URLs.
  The first two scroll the document to that section and flash it — use `§N`, it
  is checked against the rendered section numbers at click time.

A `change` tag always edits the corpus; so does any reply where the reader has
shown the prose itself failed. Rewrite it in the source and let the reply just
say where.

Keep replies short. A reply longer than the passage it is about usually means
the answer belonged in the document.

## Commands

| Command | Notes |
|---|---|
| `new <name> [--title T]` | writes `<name>.src.html` starter; refuses to overwrite |
| `build <name>.src.html [-o out.html] [--open]` | checks the source, writes `<name>.html`; preserves notes and the doc id in an existing output. Writes nothing if the check fails |
| `md <doc.html> [-o out.md] [--force]` | the built document as markdown, best effort — see "Markdown out" |
| `open <doc.html>` | opens the live page when the server can run, the file otherwise; prints the page's address; records this session |
| `wait <doc.html> [--timeout S]` | run in the background; exits when the reader sends or exports. Records this session, which keeps the server alive |
| `status` | the server, the sessions keeping it alive, registered documents with their addresses, whether the LaunchAgent is installed |
| `install` / `uninstall` | macOS LaunchAgent that starts the server for sandboxed Claude Code. The **user** runs it, once, in a normal Terminal |
| `serve [--forever]` / `stop` | run the server in the foreground (debugging, or outside Claude Code); stop it |
| `import <doc.html> <notes.json>` | merges by id; re-running is a no-op. The reader's `kind`, `tag`, `body`, `context` and `status` overwrite what is in the doc, and `status: deleted` removes the thread. Refuses notes exported from another document |
| `list <doc.html> [--open-only] [--json]` | note threads only; `!` marks unanchored; deferred and highlight counts printed at the end |
| `list <doc.html> --deferred [--json]` | only `defer` threads — the backlog queue |
| `list <doc.html> --highlights [--json]` | the reader's highlights instead |
| `reply <doc.html> <id> "text"` | appends a Claude reply; the thread stays open |
| `reply <doc.html> --all-open "text"` | every non-resolved thread; skips highlights |
| `resolve <doc.html> <id>` | **refuses** — only the reader closes a thread |

`reply --resolve` refuses for the same reason, and `reply` on a highlight refuses
because there is no question in it. Every command exits non-zero with a one-line
error on bad input.

### The live page's address

The server answers for a document at two addresses. Both serve the same page,
with the same id, the same notes and the same live features:

| Address | Example |
|---|---|
| by path | `http://127.0.0.1:47700/p/Users/me/repo/docs/plan.html` (or `/p/~/repo/docs/plan.html`) |
| by id | `http://127.0.0.1:47700/d/<id>/` |

- `open` and `build --open` print the path address. Give the user the address
  they print, not one you built yourself.
- The path address works for any reviewdoc build, registered or not, so the
  user can open one by typing its path. Anything else is a plain 404.
- Spaces in a path are `%20`. The browser does that when the user types a space.

## Source format

The source is a fragment of plain HTML — no `<html>`, `<head>` or `<body>` —
using the tags and classes below and nothing else. `build` checks it strictly
and, if anything is off, writes nothing and lists every problem with its line
number. Fix them and build again.

**Escaping is two characters.** Write a literal `<` as `&lt;` and `&` as
`&amp;`, inside `<pre>` and `<code>` too: `<code>vector&lt;int&gt;</code>`.
Nothing else needs escaping — `*ptr`, `**kwargs`, `a|b`, backticks and
underscores are all literal. A `<` you forget to escape (`x<y`, `vector<int>`)
fails the check as an unknown tag instead of silently vanishing from the page.

The top of the file is the header:

```html
<meta name="slug" content="lab-build">     <!-- optional: names the exported files -->
<meta name="hint" content="false">         <!-- optional: drops the how-to button -->
<h1>Why the build breaks on the lab Mac</h1>
<p class="sub">One sentence of context, written for this reader.</p>
<p class="meta">fiorano-workspace · branch main_B0 · 2026-08-20</p>
```

All of it is optional. `<h1>` has to be the first element if it is there;
without it the title is the file name.

**Tags.** `<h2>` starts an auto-numbered section and only goes at the top level.
Then `h3`, `h4`, `p`, `ul`/`ol`/`li` (nest freely), `table`/`thead`/`tbody`/
`tr`/`th`/`td` (a first row of `<th>` is the header), `pre` holding one `code`,
`blockquote`, `hr`, and inline `b`, `strong`, `i`, `em`, `code`, `a href`, `br`,
`sub`, `sup`, `kbd`. `<code class="language-python">` tags a code block's
language for the markdown export. No `style`, `id`, `span`, `img` or `script`,
and no class outside this table:

| Markup | Renders as | Use it for |
|---|---|---|
| `<p class="lede">` | grey intro paragraph | the whole answer, before any detail |
| `<div class="flag good\|warn\|bad\|info" data-pill="words">` | coloured side-bar | verdicts and warnings |
| `<div class="card">` | boxed panel | a self-contained aside |
| `<div class="analogy">` | italic block | a concrete comparison |
| `<ol class="steps">` | numbered circles | anything the user follows by hand |
| `<ul class="tick">` / `<ul class="cross">` | ✓ / ✗ lists | what holds and what does not |
| `<div class="grid g2">` / `g3` | side by side on wide screens | two or three cards to compare |
| `<p class="small">` | dim small print | footnotes |
| `<svg>` | inline diagram, untouched | anything with arrows |

A flag or a card takes an optional `<h3>` as its first child, which becomes its
bold title. On a flag, `data-pill` becomes the uppercase pill before the title;
both parts are optional:

```html
<div class="flag bad" data-pill="the crux">
  <h3>The patch runs after startup</h3>
  <p>Body, as ordinary blocks.</p>
</div>
```

Pills and step numbers are drawn by CSS, so they are not text and cannot be
selected or annotated. Neither can anything inside `<svg>`: put what you want
commented on in real prose.

Inside `<svg>`, use the CSS variables so diagrams follow dark/light mode:
`var(--ink)`, `var(--dim)`, `var(--line)`, `var(--card)`, `var(--card2)`,
`var(--accent)`, `var(--good)`, `var(--warn)`, `var(--bad)`. Never hardcode
`#000`/`#fff` for text. Give the `<svg>` an `aria-label`; the markdown export
uses it as the figure's caption.

See `example.src.html` in this folder for a complete working source that uses
every class.

## Markdown out

The source stays the only thing you edit; markdown is an output, for pasting
into an MR, a wiki or a chat, or committing next to the code.

- `reviewdoc.py md <name>.html` writes `<name>.md` in GitLab-flavoured
  markdown. Flags become alerts (`bad` → `[!CAUTION]`, `warn` → `[!WARNING]`,
  `good` → `[!TIP]`, `info` → `[!NOTE]`), cards and analogies become
  blockquotes, steps an ordered list. Each diagram is written as
  `<name>-fig-N.svg` beside it, with the light palette built in so its colours
  survive outside the page. It refuses to overwrite a `.md` it did not write
  itself (`--force` overrides), so it cannot clobber a hand-written file.
- The **↓ Markdown** button at the top of the page gives the reader the same
  markdown, generated by `build`, with a caption line where each diagram was.
- Best effort: `colspan` flattens, the pill turns into italic text before the
  title, and the notes are not included (`list --json` has those).

## Writing it

The format only earns its keep if the writing does.

**Pitch it at this reader, per topic.** Do not default to explaining everything from
scratch, and do not default to assuming expertise either. Check the user's CLAUDE.md
for their background and calibrate against it:

- Topics they know well get no primer, no analogy for something they use daily, and
  no definition of their own vocabulary. Explaining the basics of their own field
  reads as condescension and wastes the page.
- Topics they have flagged as weak, or that sit outside their stated background, get
  built up from fundamentals with an `analogy` block doing real work.
- A document that spans both varies register section by section. It is normal for one
  page to assume fluency in the reader's core domain while carefully unpacking an
  adjacent one.

When in doubt on a specific topic, ask rather than guess — a misjudged level is the
most common reason a review document annoys the reader.

The rest of the guidance holds at any level:

- **Answer first.** Open with `<p class="lede">` giving the whole conclusion in
  four sentences. No suspense, no chronology of your investigation.
- **One idea per `<h2>`.** Short sections, scannable headings that state a claim
  ("The error message was pointing at the wrong country"), not a label
  ("Analysis").
- **Analogies are load-bearing**, not decoration. Any mechanism that is abstract
  *to this reader* gets a concrete everyday comparison in an `analogy` block.
  Something they work with daily does not.
- **Diagram instead of listing facts.** When you are about to write a paragraph
  describing how three things connect, draw it with an inline `<svg>`.
- **Colour-code severity** with flags: `bad` for the crux/blocker, `warn` for a
  trade-off or caveat, `good` for something verified or safe, `info` for
  neutral framing. Put the pill to work (`verified`, `the crux`, `assumption`).
- **Say what you are unsure about**, in its own flag or section. It is the part
  the user can most usefully correct, and the panel exists for exactly that.
- **End with what you need from them** — a small table of decisions with your
  recommendation on each, so annotating has an obvious target.
- Tables for comparisons, `tick`/`cross` lists for holds/does-not, `steps` for
  anything the user will follow by hand.

Selections work best inside a single paragraph or list item; a selection
spanning two blocks cannot be highlighted and will land in Unanchored. Do not
warn the user about this — just do not rely on it.

## Tags the user can apply

Five, and each wants a different response. `clarify` is filed from the **🔍
Clarify** button on the selection bar in one click; the other four come from the
note editor's picker, which no longer offers `clarify` at all:

| Tag | What it means | What you do |
|---|---|---|
| `question` ❓ | a real question | answer it — panel or corpus, per the rule above |
| `clarify` 🔍 | "I don't know this term"; one click from the bar, almost always with no body at all | define it in the panel — see below |
| `change` ✏️ | they want the plan or the prose different | make the change, then say where in one line |
| `defer` 📥 | valid, but not now | open an issue on the repo's tracker, reply with its link (see below) |
| `agree` 👍 | acknowledgement; body optional | **nothing** — it arrives already resolved |

`agree` files itself as `status: resolved`, so `--all-open` skips it and it does
not appear in your work queue. Do not reply to one to say "thanks" or "noted";
that is noise in a thread the reader already closed.

### `clarify` — the reader hit a word they don't know

This is the tag they reach for most, and it has its own button: they select the
word, press **🔍 Clarify**, done — no editor, no typing. So it arrives with an
empty body nearly every time, and the quoted text *is* the whole question. Treat
an empty body as normal, never as a mistake or an empty note.

- **Answer in the panel, not the document.** A definition of one term does not
  belong bolted into the plan; it belongs attached to the word that prompted it.
  The exception is when the term is load-bearing for the whole document — then
  define it once in the prose and point at it with `§N`.
- **Pitch it at their actual level for that specific domain.** Check the user's
  CLAUDE.md. A term from a field they know well gets one plain sentence and no
  primer. A term from a field they have flagged as weak gets built up properly:
  what it is, why it exists, and a concrete comparison if the mechanism is
  abstract to them. Do not give a one-line gloss for something that genuinely
  needs three sentences, and do not lecture about something they use daily.
- **Define, don't defend.** They are not challenging the passage. Do not
  re-argue the point or rewrite the section; just say what the word means and how
  it is being used here.
- If the same term drew a `clarify` and it appears throughout the document, that
  is a signal your writing assumed too much. Consider defining it on first use
  next time — but say so in the reply rather than silently restructuring.

### `defer` — open a backlog issue

A `defer` thread means the point stands but is out of scope for now. It wants a
tracked issue, not an argument.

1. **Find the tracker.** The plan document lives inside a git repo — the one you
   were invoked in. `git remote -v` in that repo tells you the host and project.
2. **Use whatever you actually have** to create the issue there. That might be a
   configured MCP server for that host, a CLI already installed and authenticated
   (`glab` for GitLab, `gh` for GitHub), or something else in this environment. No
   tool is mandated — check what is available for *that* host and use it. Do not
   reach for a different project because it is easier to reach.
3. **Confirm the target project with the user once per document**, before filing
   the first issue. An issue is outward-facing and notifies people; getting the
   project wrong is not something you can quietly undo. One confirmation covers
   every `defer` in that round.
4. File one issue per deferred thread. Title from the quoted passage or their
   note; the body should stand on its own for someone who has not read the plan —
   what was proposed, why it was deferred, and a path or link to the document.
5. **Reply in the thread with the issue link.** Just the link and a couple of
   words: `Deferred — <url>`. Panel replies linkify bare URLs, so it is
   clickable. Leave the thread open; the reader closes it once satisfied it is
   tracked. A deferred thread with no link in it is an unfinished job.
6. Report every issue you filed, with its URL, in your chat message too.

**If you cannot create the issue** — no tool for that host, not authenticated,
host unreachable, or the repo has no tracker — then do not invent an issue
number, a URL, or an ID. Tell the user plainly what is missing, reply in the
thread that the deferral is noted but *not* tracked yet, and let them decide
whether to file it themselves or point you at another route.

`reviewdoc.py list <doc.html> --deferred` is the queue. Plain `list` prints a
count and points at it.
