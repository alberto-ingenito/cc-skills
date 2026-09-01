# present-dense-plan

Turns a markdown file into a **single self-contained HTML review document** with a
Word-style threaded comment panel on the right. You select a sentence, tag a note
(question / change this / concern / don't understand / agree), and Claude's answers
come back stacked underneath your comment in the same thread.

One Python file, stdlib only. No install, no npm, no build step, no server, no
network. The generated HTML has zero external references — you can email it,
AirDrop it, or open it from a USB stick and it still works, including dark mode.

## Using it

```bash
R=~/.claude/skills/present-dense-plan/reviewdoc.py

python3 $R build myplan.md --open      # render and open
# ... annotate, then press "Export notes for Claude" ...
python3 $R import myplan.html ~/Downloads/myplan-notes.json
python3 $R list myplan.html            # see the threads
python3 $R reply myplan.html u1 "answer" --resolve
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
button under 900px, so it is usable on a phone. Clicking a thread jumps to its
highlight; clicking a highlight opens its thread. Resolved threads dim and fold.
Printing hides the whole comment layer.

`example.md` is a complete source document exercising every directive, including an
inline SVG diagram — copy it as a starting point.
