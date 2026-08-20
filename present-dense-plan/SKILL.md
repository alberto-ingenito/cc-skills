---
name: present-dense-plan
description: Generate a self-contained, annotatable HTML review document with a Word-style threaded comment panel, then read the user's comments and reply to them in place. Use when you are about to present a dense or complicated plan, diagnosis, design doc, architecture proposal, security review, migration strategy or post-mortem — anything with enough moving parts, branching decisions or cross-cutting mechanism that a wall of terminal markdown would be hard to review, and especially when you need decisions back on specific points rather than a yes/no. Also use whenever the user asks to "review", "annotate", "comment on", or "mark up" something, and to continue an existing review round trip after they say they have exported their notes. Skip it for short answers, single-decision questions, and anything they can act on by reading one screen.
---

# present-dense-plan

One script: `reviewdoc.py` (Python 3 stdlib only, no install). Source is a
markdown-ish `.md`; output is one HTML file that works over `file://` with no
network. Notes live inside the HTML in a `<script id="rd-notes">` JSON block, so
rebuilding never loses them and you can read and answer them from the CLI.

Invoke as `python3 ~/.claude/skills/present-dense-plan/reviewdoc.py`. Put the `.md` and
`.html` somewhere durable and project-adjacent (e.g. `~/.claude/plans/<name>.md`
or a `docs/` folder), never in `/tmp`.

## The loop

```
reviewdoc.py new <name> --title "T"          # optional scaffold
reviewdoc.py build <src.md> --open           # write it, open it, then STOP and wait
   ... user selects text, adds notes, presses "Export notes for Claude" ...
   ... user says "done" / "notes exported" ...
reviewdoc.py import <doc.html> ~/Downloads/<slug>-notes.json
reviewdoc.py list <doc.html>                 # ids, tags, quotes, reply counts
reviewdoc.py list <doc.html> --json          # exact bodies, read this before answering
reviewdoc.py reply <doc.html> <id> "answer"  # add --resolve when it is settled
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
- `--resolve` when your reply closes the question; leave it open if you changed
  something and want them to confirm.
- Answer every open thread. `reply <doc> --all-open "text"` exists for a blanket
  acknowledgement but is rarely the right thing.
- Rebuild is safe: each note is re-found by its quoted text (moving between
  sections is fine). A note whose quote no longer exists is kept and shown under
  **Unanchored**, never dropped. `build` prints `N kept, N re-anchored, N unanchored`.

## Commands

| Command | Notes |
|---|---|
| `new <name> [--title T]` | writes `<name>.md` starter; refuses to overwrite |
| `build <src.md> [-o out.html] [--open]` | default output is `<src>.html`; preserves notes in an existing output |
| `import <doc.html> <notes.json>` | merges by id; re-running is a no-op (replies deduped on author+ts+body) |
| `list <doc.html> [--open-only] [--json]` | one line per thread; `!` marks unanchored |
| `reply <doc.html> <id> "text" [--resolve]` | `--resolve` may go anywhere on the line |
| `reply <doc.html> --all-open "text"` | every non-resolved thread |
| `resolve <doc.html> <id>` | status only, no reply |

Every command exits non-zero with a one-line error on bad input.

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

`question` ❓, `change` ✏️, `concern` ⚠️, `unclear` 🤷, `agree` 👍. Treat
`unclear` as a defect in your writing, not in their understanding: rewrite that
passage in the source before rebuilding, and say so in the reply.
