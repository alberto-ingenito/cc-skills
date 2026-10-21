# html review documents

Turns an HTML source fragment into a **single self-contained HTML review document** with
a Word-style threaded comment panel on the right. You select a sentence and either
highlight it or tag a note (question / clarify this / change this / defer to backlog /
agree); Claude's answers come back stacked underneath your comment in the same thread.

Python 3 stdlib only: no npm, no build step on your side. The generated HTML has zero
external references — you can email it, AirDrop it, or open it from a USB stick and it
still works, including dark mode. An optional local server makes it live while Claude
Code runs (see "Live and offline").

## Using it

```bash
R=~/.claude/skills/html/reviewdoc.py

python3 $R new myplan                       # starter myplan.src.html
python3 $R build myplan.src.html --open     # check it, write myplan.html, open it
python3 $R wait myplan.html                 # Claude runs this in the background
# ... annotate, then Send to Claude (live) or Export and save next to the doc ...
python3 $R import myplan.html myplan-notes.json   # offline only: live notes are already in
python3 $R list myplan.html                 # the threads needing an answer
python3 $R list myplan.html --deferred
python3 $R list myplan.html --highlights
python3 $R reply myplan.html u1 "answer"
python3 $R build myplan.src.html            # rebuild; reload the page
python3 $R md myplan.html                   # myplan.md (+ myplan-fig-N.svg)
```

Normally you don't type any of this — you ask Claude for a reviewable document and
it drives the CLI. Your only jobs are annotating and pressing Send to Claude (live) or
Export (offline).

## Why the source is HTML

The source used to be a markdown dialect. Its hand-rolled parser had no escapes, so
`*ptr` became italics, `**kwargs` became bold and a `|` in a table cell split the
cell — all silently. In HTML only `<` and `&` are special, and `build` checks the
source against a fixed list of tags and classes. A slip such as `vector<int>` fails the
build with its line number rather than vanishing from the page.

Markdown is now an output. `md` writes GitLab-flavoured markdown from a built document,
and the page's **↓ Markdown** button hands the reader the same text.

## Live and offline

The same file works two ways.

**Live.** A small server (`live.py`, stdlib only) serves the page on
`http://127.0.0.1:<port>/`, by path or by id (see "Addresses"). Every note goes into
the HTML file on disk as you write it, so there is no Export. Claude's replies appear
in the open page within seconds, so there is no reload. **Send to Claude** wakes the
Claude session that is waiting on the page. The panel's status line says which mode
you are in.

**Offline.** The file opened on its own, from disk or an email. Notes stay in the
browser until you press **Export notes for Claude**, which saves a JSON file; Claude
reads it from beside the document or from `~/Downloads`. If a live server is running,
the offline copy notices and offers **Open it live**, carrying your unsent notes
across in the link.

A live page whose server stops (Claude Code quit) says so and keeps working: notes
wait in the browser and are written to the file as soon as the server is back. The
port is stable, so the browser finds those notes again.

### Addresses

Each document has two addresses on the server. Both serve the same page, with the
same id, the same notes and the same live features. Drafts are kept per id, so they
follow the document whichever address you use.

| | |
|---|---|
| `/p/Users/me/repo/docs/plan.html` | by absolute path. `/p/~/repo/docs/plan.html` works too |
| `/d/<doc id>/` | by id, as before |

- A path finds any reviewdoc build, registered or not. A build the server has not
  seen yet is registered on the spot, as `open` would do.
- Type spaces as they are; the browser sends them as `%20`.
- `open`, `build --open` and `wait` print the path address. `status` lists each
  document's address.
- There is no address by file name alone. Two folders often hold files with the
  same name, and a name that pointed at one document today could point at another
  tomorrow. A path names exactly one file.
- A copy of a built document carries the same id. The server keeps one file per id,
  so asking for the copy by path shows a page that points at the original.
- A server started before addresses existed knows only `/d/`. The CLI reads the
  running server's version and prints `/d/` addresses to it until it restarts.

### When the server runs

One server per user, shared by every Claude Code session on the machine. It starts
when a session first opens or waits on a review, and stops 90 seconds after the last
such session has exited. Sessions record their Claude process under
`~/.cache/reviewdoc/sessions/`, and the server checks those processes are alive.

Claude never talks to the server over the network. They meet in files: the document
itself, under a per-document lock, plus a few small records in `~/.cache/reviewdoc`.
That is deliberate. Apple Claude Code's sandbox lets nothing Claude runs open or reach
a local port. Files also cope with any number of parallel sessions.

The same sandbox means Claude cannot start the server, so on Apple Claude Code there
is a one-time step, run in a normal Terminal:

```bash
python3 ~/.claude/skills/html/reviewdoc.py install     # uninstall to undo
```

It installs a LaunchAgent that watches `~/.cache/reviewdoc/sessions/`. When a session
writes there, launchd starts the server outside the sandbox, and launchd never runs
two copies. Without a sandbox (plain Claude Code), the CLI starts the server itself
and no install is needed. `reviewdoc.py status` shows what is running.

### On a remote machine (VS Code Remote-SSH)

Claude runs on the remote, so the server does too, with its own state in the remote's
`~/.cache/reviewdoc`. VS Code forwards the port to your machine. Its remote terminals
set `$BROWSER` to a helper that opens a URL on your machine, and `build --open` uses it,
so the live page opens in your local browser.

- The forwarded port may differ, for example `localhost:47701` when your own machine's
  server already holds 47700. That is fine: the server checks the host name, not the
  port.
- Inside VS Code, **Simple Browser: Show** with the same `localhost` address shows the
  live page in an editor tab.
- If nothing opens, forward port 47700 from the **Ports** panel and open it from there.
- A remote Mac running Apple Claude Code needs `install` once, from a plain terminal on
  that Mac. A Linux remote needs nothing, as long as Claude Code's sandbox lets the
  server listen; if it does not, there is no LaunchAgent equivalent yet.
- The offline copy is a file on the remote, so reviewing it offline means copying it to
  your machine first.

### Security

- It listens on `127.0.0.1` only, on the first free port from 47700 (`REVIEWDOC_PORT`
  moves the range).
- It serves only reviewdoc builds. By id, that means documents a `build` or `open`
  registered. By path, it means a file that carries a build's markers: the doctype
  first, the document id in the head, and the notes block. The file must end in
  `.html` and belong to you. Symlinks and `..` are resolved before any check.
- Everything else under `/p/` gets the same plain 404, whatever the reason. So the
  server cannot be used to learn whether a file exists.
- Requests whose `Host` is not `127.0.0.1`/`localhost` are refused (DNS rebinding).
- A page the server serves carries the API token, so only builds you trust should be
  served. That is why another website cannot register one for you. It may link to
  a document that is already registered, as it always could with `/d/`. It cannot
  embed one (`<img>`, `<script>`, `fetch`). The browser's `Sec-Fetch-Site` header
  tells the server who asked. A build the server has not seen opens only from an
  address you typed or pasted, or from a link on one of its own pages.
- Every API call needs a per-user token, kept `0600` in the state directory. Only the
  pages the server serves carry it, and it rides in a custom header, which another
  site's script cannot send without a CORS preflight the server never grants.
- Any other local user can reach the server too. They could open any build of yours
  by its path. On a single-user Mac this does not matter.

## Sharing it with others

Today the skill is a folder: copy `~/.claude/skills/html/` to someone else's
`~/.claude/skills/`. It needs only the system Python 3.

The better route is a **Claude Code plugin** in a marketplace, so people install and
update it with `/plugin`:

```
html-review/
  .claude-plugin/plugin.json      name, version, description
  skills/html/                    this folder, unchanged
```

At Apple, the `ai-for-devs-community` marketplace
(`github.pie.apple.com/AI-for-Devs-Community/claude-code-marketplace`) is the natural
home. A marketplace of your own on GitLab works the same way. Two things to know:

- **Apple Claude Code users still run `install` once.** A plugin cannot start a server
  from inside the sandbox either. The LaunchAgent records the script's path, and plugin
  updates may move it; `status` and `open` notice and ask for a reinstall.
- **The clean long-term fix is on the ACC side.** ACC already runs its own server per
  session, outside the sandbox. A supported way for a plugin to declare a local service
  for ACC to host would remove the install step for everyone. That is worth raising
  with the ACC team.

## Where things go

| | |
|---|---|
| The skill | `~/.claude/skills/html/` — `reviewdoc.py`, `live.py`, `SKILL.md`, `example.src.html`, `test_routes.py` |
| Your source | `<name>.src.html` in the repo you are working in, or its `docs/` |
| The document | next to the source, `<name>.html`, unless `-o` says otherwise |
| Exported notes | wherever you save them; the doc's own folder is best |
| Markdown | `<name>.md` and `<name>-fig-N.svg` next to the document |
| Unexported drafts | browser `localStorage`, key `reviewdoc:<doc id>` (live: only while the server is down) |
| Live server state | `~/.cache/reviewdoc/` (`REVIEWDOC_STATE` moves it) |

Notes are stored **inside the HTML** in a `<script id="rd-notes">` JSON block, which
is the single source of truth. `localStorage` is only a scratch buffer for notes you
have written but not yet exported; once a note appears in the file, its draft copy is
discarded so it cannot duplicate itself.

Each document gets a random id on its first build, kept across rebuilds. Drafts are
keyed on it rather than on the file name, because every `file://` page may share one
storage area and two repos can both hold a `plan.html`. The export carries the id too,
and `import` refuses notes that were exported from a different document.

Rebuilding after the prose changes re-finds each note by its quoted text, so notes
follow their sentence even across sections. If the sentence is gone entirely the note
is kept and listed under **Unanchored** rather than dropped.

## Reading it

Three buttons sit at the top of the page, above the title. **ⓘ How to use this page**
opens the instructions as a dialog. **↓ Markdown** saves the document itself as a
markdown file. **⤢ Full screen** puts the document into browser fullscreen (standard
API in Chrome, `webkit`-prefixed in Safari); that button is hidden if neither exists.

Panel is docked on the right on a desktop and collapses to a drawer with a floating
button under 900px, so it is usable on a phone. The **⇥** button in the panel header
hides it on a desktop too, dropping the right gutter so the document centres on the
full window; the floating **Review** button brings it back, and the choice is
remembered per document in `localStorage`. Clicking a thread jumps to its mark;
clicking a mark opens its thread. Resolved threads dim and fold. Printing hides the
whole comment layer.

Selecting text offers three buttons. **🖍 Highlight** just marks the passage — amber
wash, no wording needed. **🔍 Clarify** files a one-click "what does this mean?" on the
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
| **🔍 Clarify this** | you don't know the term — the word is the question. Filed straight from the **🔍 Clarify** button, so it is not in the editor's picker |
| **✏️ Change this** | you want the plan or the wording different |
| **📥 Defer to backlog** | valid but not now — Claude opens an issue on whatever tracker the repo uses and replies with the link, or tells you if it can't |
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

`example.src.html` is a complete source exercising every class, including an inline
SVG diagram — copy it as a starting point.
