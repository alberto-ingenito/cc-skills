---
name: present-dense-plan
description: Generate a self-contained, annotatable HTML review document with a Word-style threaded comment panel, then read the user's comments and reply to them in place. Use when you are about to present a dense or complicated plan, diagnosis, design doc, architecture proposal, security review, migration strategy or post-mortem — anything with enough moving parts, branching decisions or cross-cutting mechanism that a wall of terminal markdown would be hard to review, and especially when you need decisions back on specific points rather than a yes/no. Also use whenever the user asks to "review", "annotate", "comment on", or "mark up" something, and to continue an existing review round trip after they say they have exported their notes. Skip it for short answers, single-decision questions, and anything they can act on by reading one screen.
---

# present-dense-plan

One script: `reviewdoc.py` (Python 3 stdlib only, no install). Source is a
markdown-ish `.md`; output is one HTML file that works over `file://` with no
network. Notes live inside the HTML in a `<script id="rd-notes">` JSON block, so
rebuilding never loses them and you can read and answer them from the CLI.

Invoke as `python3 ~/.claude/skills/html/reviewdoc.py`. Put the `.md` and
`.html` **in the current session's working directory** (the repo you were
invoked in), or a `docs/` subfolder of it — never `/tmp`, and avoid
`~/.claude/plans` or `~/Downloads`. The reason is the export round-trip: on
macOS you frequently cannot read `~/Downloads` back, so the notes file must
land somewhere inside a working directory. The "Export notes for Claude" button
opens a native save dialog (Chrome remembers the last folder), so the user can
point it at the doc's own folder and you read it straight from there.

## The loop

```
reviewdoc.py new <name> --title "T"          # optional scaffold
reviewdoc.py build <src.md> --open           # write it, open it, then STOP and wait
   ... user selects text, adds notes, presses "Export notes for Claude" ...
   ... a native save dialog opens — user saves <slug>-notes.json into the doc's folder ...
   ... user says "done" / "notes exported" ...
reviewdoc.py import <doc.html> <doc-folder>/<slug>-notes.json
reviewdoc.py list <doc.html>                 # note threads: ids, tags, quotes, reply counts
reviewdoc.py list <doc.html> --json          # exact bodies, read this before answering
reviewdoc.py list <doc.html> --deferred      # threads that each want a backlog issue
reviewdoc.py list <doc.html> --highlights    # passages they marked without asking anything
reviewdoc.py reply <doc.html> <id> "answer"  # threads stay open; only the reader closes them
reviewdoc.py build <src.md>                  # re-render; notes + replies survive
   ... tell the user to reload; answers appear under their comments ...
```

Rules for the loop:

- After `build --open`, **wait**. Do not start editing project files while the
  user is still annotating.
- When the user says they are done: `import`, then `list --json` and actually
  read every body, then `reply` to each thread, then `build` again, then tell
  them to reload. Only after that should you touch project files — their notes
  may change the plan.
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
  Reply in the thread and leave the source `.md` alone. Do not bolt a new
  paragraph onto the document because one reader asked one question; the answer
  is already attached to the exact sentence that prompted it.
- **Corpus only, with a pointer in the panel** — when the answer changes the
  plan, corrects something wrong, or is something the *next* reader of the
  document also needs. Edit the `.md`, rebuild, and make the reply a one-liner
  that points at the change: `Wrong as written — rewrote §4, it now says the
  patch runs before startup.` Do not paste the new prose into the reply as well.
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
| `new <name> [--title T]` | writes `<name>.md` starter; refuses to overwrite |
| `build <src.md> [-o out.html] [--open]` | default output is `<src>.html`; preserves notes in an existing output |
| `import <doc.html> <notes.json>` | merges by id; re-running is a no-op. The reader's `kind`, `tag`, `body`, `context` and `status` overwrite what is in the doc, and `status: deleted` removes the thread |
| `list <doc.html> [--open-only] [--json]` | note threads only; `!` marks unanchored; deferred and highlight counts printed at the end |
| `list <doc.html> --deferred [--json]` | only `defer` threads — the backlog queue |
| `list <doc.html> --highlights [--json]` | the reader's highlights instead |
| `reply <doc.html> <id> "text"` | appends a Claude reply; the thread stays open |
| `reply <doc.html> --all-open "text"` | every non-resolved thread; skips highlights |
| `resolve <doc.html> <id>` | **refuses** — only the reader closes a thread |

`reply --resolve` refuses for the same reason, and `reply` on a highlight refuses
because there is no question in it. Every command exits non-zero with a one-line
error on bad input.

## Source format

Front matter (all optional): `title`, `subtitle`, `meta` (small mono byline),
`slug` (controls the export filename, defaults to the output stem),
`hint: false` (suppresses the "this page is for you to mark up" box).

```
---
title: Why the build breaks on his Mac
subtitle: One sentence of context, written for someone with no background.
meta: fiorano-workspace · branch main_B0 · 2026-08-20
---
```

Markdown supported: `##` (auto-numbered sections), `###`, paragraphs,
`**bold**`, `*italic*`, `` `code` ``, fenced blocks, `-`/`*`/`1.` lists with
2-space nesting, pipe tables (`|---|` separator row makes a header), `---`
rules, `[text](url)`. Everything is HTML-escaped except `:::html`. `##` inside a
fence or a `:::` block is not treated as a heading.

### Directives

Each opens with `:::name` and closes with a bare `:::`. Bodies are parsed as
markdown (except `:::html`).

```
:::lede
The whole answer in plain language, before any detail.
:::

:::flag good verified | Not a guess this time
Body. Kind is good|warn|bad|info; the words after it become the uppercase pill;
anything after | is the bold title. All three parts are optional.
:::

:::card What each knob does
Boxed aside. The argument becomes an h3.
:::

:::analogy
Think of it as a soundproof booth with one phone line to the mailroom.
:::

:::steps
1. First thing.
2. Second thing.
:::

:::tick
- What holds.
:::

:::cross
- What does not.
:::

:::html
<svg viewBox="0 0 700 200" role="img" aria-label="...">...</svg>
:::
```

`:::html` is raw passthrough — not escaped, not markdown-processed. It is the
only way to get a diagram in. An unknown directive name warns on stderr and
renders as a card.

Inside `:::html`, use the CSS variables so diagrams follow dark/light mode:
`var(--ink)`, `var(--dim)`, `var(--line)`, `var(--card)`, `var(--card2)`,
`var(--accent)`, `var(--good)`, `var(--warn)`, `var(--bad)`. Never hardcode
`#000`/`#fff` for text. Text nodes inside `<svg>` are skipped by the anchoring
walker, so diagram labels cannot be annotated — put anything you want commented
on in real prose.

See `example.md` in this folder for a complete working source exercising every
directive.

## Writing it

The format only earns its keep if the writing does.

**Pitch it at this reader, per topic.** Do not default to explaining everything from
scratch, and do not default to assuming expertise either. Check the user's CLAUDE.md
for their background and calibrate against it:

- Topics they know well get no primer, no analogy for something they use daily, and
  no definition of their own vocabulary. Explaining the basics of their own field
  reads as condescension and wastes the page.
- Topics they have flagged as weak, or that sit outside their stated background, get
  built up from fundamentals with `:::analogy` doing real work.
- A document that spans both varies register section by section. It is normal for one
  page to assume fluency in the reader's core domain while carefully unpacking an
  adjacent one.

When in doubt on a specific topic, ask rather than guess — a misjudged level is the
most common reason a review document annoys the reader.

The rest of the guidance holds at any level:

- **Answer first.** Open with `:::lede` giving the whole conclusion in four
  sentences. No suspense, no chronology of your investigation.
- **One idea per `##`.** Short sections, scannable headings that state a claim
  ("The error message was pointing at the wrong country"), not a label
  ("Analysis").
- **Analogies are load-bearing**, not decoration. Any mechanism that is abstract
  *to this reader* gets a concrete everyday comparison in `:::analogy`. Something
  they work with daily does not.
- **Diagram instead of listing facts.** When you are about to write a paragraph
  describing how three things connect, draw it with inline SVG in `:::html`.
- **Colour-code severity** with `:::flag`: `bad` for the crux/blocker, `warn`
  for a trade-off or caveat, `good` for something verified or safe, `info` for
  neutral framing. Put the pill to work (`verified`, `the crux`, `assumption`).
- **Say what you are unsure about**, in its own flag or section. It is the part
  the user can most usefully correct, and the panel exists for exactly that.
- **End with what you need from them** — a small table of decisions with your
  recommendation on each, so annotating has an obvious target.
- Tables for comparisons, `:::tick`/`:::cross` for holds/does-not, `:::steps`
  for anything the user will follow by hand.

Selections work best inside a single paragraph or list item; a selection
spanning two blocks cannot be highlighted and will land in Unanchored. Do not
warn the user about this — just do not rely on it.

## Tags the user can apply

Five, and each wants a different response. `clarify` is filed from the **🔍
Explain** button on the selection bar in one click; the other four come from the
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

Older documents may still carry `concern` ⚠️ or `unclear` 🤷 from before those
tags were retired. They still render with their labels; treat `concern` as a
`question` and `unclear` as a defect in your writing — rewrite that passage in
the source and say so in the reply.

### `clarify` — the reader hit a word they don't know

This is the tag they reach for most, and it has its own button: they select the
word, press **🔍 Explain**, done — no editor, no typing. So it arrives with an
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
