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
  acknowledgement but is rarely the right thing.
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
- Reply bodies linkify `§N` (section number) and `[label](#sec-id)` into
  clickable jumps that scroll the document to that section and flash it. Use
  `§N` — it is checked against the rendered section numbers at click time.

An `unclear` tag is the one case that always edits the corpus: the passage
failed, so rewrite it in the source and let the reply just say where.

Keep replies short. A reply longer than the passage it is about usually means
the answer belonged in the document.

## Commands

| Command | Notes |
|---|---|
| `new <name> [--title T]` | writes `<name>.md` starter; refuses to overwrite |
| `build <src.md> [-o out.html] [--open]` | default output is `<src>.html`; preserves notes in an existing output |
| `import <doc.html> <notes.json>` | merges by id; re-running is a no-op. The reader's `kind`, `tag`, `body`, `context` and `status` overwrite what is in the doc, and `status: deleted` removes the thread |
| `list <doc.html> [--open-only] [--json]` | note threads only; `!` marks unanchored; highlight count printed at the end |
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

`question` ❓, `clarify` 🔍, `change` ✏️, `concern` ⚠️, `unclear` 🤷, `agree` 👍. Treat
`unclear` as a defect in your writing, not in their understanding: rewrite that
passage in the source before rebuilding, and say so in the reply. `clarify` is a
lightweight "expand on this" — the user can apply it to a highlight without
typing a note body, so the quoted text alone is the ask; answer it in the panel
like any other thread, and only touch the source if the gap is one every reader
would hit.
