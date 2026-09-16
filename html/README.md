# present-dense-plan

Turns a markdown file into a **single self-contained HTML review document** with a
Word-style threaded comment panel on the right. You select a sentence and either
highlight it or tag a note (question / explain this / change this / defer to backlog /
agree); Claude's answers come back stacked underneath your comment in the same thread.

One Python file, stdlib only. No install, no npm, no build step, no server, no
network. The generated HTML has zero external references — you can email it,
AirDrop it, or open it from a USB stick and it still works, including dark mode.

## Using it

```bash
R=~/.claude/skills/present-dense-plan/reviewdoc.py

python3 $R build myplan.md --open      # render and open
# ... annotate, then press "Export notes for Claude" ...
python3 $R import myplan.html ~/Downloads/myplan-notes.json
python3 $R list myplan.html            # the threads needing an answer
python3 $R list myplan.html --deferred
python3 $R list myplan.html --highlights
python3 $R reply myplan.html u1 "answer"
python3 $R build myplan.md             # rebuild; reload the page
```

Normally you don't type any of this — you ask Claude for a reviewable document and
it drives the CLI. Your only jobs are annotating and pressing Export.

## The two manual steps, and why

**1. You press "Export notes for Claude".** A page opened over `file://` cannot
write to your disk by itself. The browser's only sanctioned route is a user-initiated
download, so the button drops `<slug>-notes.json` in `~/Downloads` and Claude reads
it from there.

**2. You reload after Claude replies.** Claude edits the HTML file on disk; an
already-open tab has no way to know. `fetch()` back to a local process is blocked
by CORS on `file://` origins, and long-polling a file is not possible either.

Both steps exist because there is **deliberately no local server**. A server would
remove the clicks but add a process to start, a port to trust, and something to
leave running. For a document you read once and mark up, two clicks is the cheaper
trade.

<!-- Possible future fast lane: drive the open tab over the Chromium MCP
     (evaluate_javascript) to pull notes and inject replies without the export
     or the reload. Not implemented, not approved. -->

## Where things go

| | |
|---|---|
| The skill | `~/.claude/skills/present-dense-plan/` — `reviewdoc.py`, `SKILL.md`, `example.md` |
| Your source | wherever you keep it; `~/.claude/plans/<name>.md` is the usual spot |
| The document | next to the source, `<name>.html`, unless `-o` says otherwise |
| Exported notes | `~/Downloads/<slug>-notes.json` |
| Unexported drafts | browser `localStorage`, key `reviewdoc:<slug>` |

Notes are stored **inside the HTML** in a `<script id="rd-notes">` JSON block, which
is the single source of truth. `localStorage` is only a scratch buffer for notes you
have written but not yet exported; once a note appears in the file, its draft copy is
discarded so it cannot duplicate itself.

Rebuilding after the prose changes re-finds each note by its quoted text, so notes
follow their sentence even across sections. If the sentence is gone entirely the note
is kept and listed under **Unanchored** rather than dropped.

## Reading it

Panel is docked on the right on a desktop and collapses to a drawer with a floating
button under 900px, so it is usable on a phone. The **⇥** button in the panel header
hides it on a desktop too, dropping the right gutter so the document centres on the
full window; the floating **Review** button brings it back, and the choice is
remembered per document in `localStorage`. Clicking a thread jumps to its mark;
clicking a mark opens its thread. Resolved threads dim and fold. Printing hides the
whole comment layer.

Selecting text offers three buttons. **🖍 Highlight** just marks the passage — amber
wash, no wording needed. **🔍 Explain** files a one-click "what does this mean?" on the
selected word, nothing to type. **💬 Note** opens the tagged comment editor; a commented
passage gets the blue underline instead. The panel has a tab for each, with a
**Show open only** checkbox on the Notes tab. A highlight can be promoted to a note
later from its card.

Notes are listed **newest activity first** — Claude's freshest answers and anything
you just wrote sit at the top — rather than grouped by section. Each card still names
its section, and notes whose text has since changed collect at the bottom under
**Unanchored**. Highlights stay in document order instead, since they read as a map
of the page.

Five note tags:

| | |
|---|---|
| **❓ Question** | a question to answer |
| **🔍 Explain this** | you don't know the term — needs no text, the word is the question. Also the **🔍 Explain** button on the selection bar |
| **✏️ Change this** | you want the plan or the wording different |
| **📥 Defer to backlog** | valid but not now — Claude opens a GitLab issue and replies with the link |
| **👍 Agree** | needs no text, and files itself **already closed** so it never lands in Claude's queue |

Highlights survive a rewrite. If the wording they were attached to is gone, the
highlight stays in its tab, marked *not in the text any more*, with the section it
was in and the opening of its original paragraph — so nothing you marked is lost
when Claude edits the prose. Same for notes, under **Unanchored**.

Highlights go out with the export and live in the HTML next to the notes. That is
deliberate: it is what lets a rebuild re-anchor them, so a highlight follows its
sentence even when Claude moves it to another section, and it means your marks
travel with the file rather than being stranded in one browser. Claude never reads
them and cannot reply to one — the only field it ever rewrites is the anchor.

The reader owns their own marks: **edit** on any note body or draft reply, **delete**
on a draft reply or any highlight, and **Mark resolved** / **Reopen** on any thread.
All of it rides back out through the export and `import` treats it as authoritative
(a deleted highlight travels as a `status: deleted` tombstone so it cannot come back).
Claude cannot close a thread — `reply --resolve` and `resolve` both refuse — and
cannot reply to a highlight at all, since no question was asked.

Claude's replies linkify `§N` and `[label](#sec-id)` into jumps that scroll the
document to that section and flash it, so an answer that lives in the prose needs
only a pointer in the panel rather than a second copy. Bare URLs become ordinary
links, which is how a deferred thread comes back carrying its issue.

`example.md` is a complete source document exercising every directive, including an
inline SVG diagram — copy it as a starting point.
