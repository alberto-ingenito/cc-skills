---
title: How the review loop works
subtitle: A worked example that also happens to exercise every directive this skill supports. Copy it, delete the prose, keep the shapes.
meta: present-dense-plan example · stdlib only · 2026-08-20
---

:::lede
Everything you need is in this one file. It is written the way a review document
should be written: the answer first, in plain language, before any of the detail.
:::

## The shape of the thing

A review document is a single HTML file. There is no server, no build step and no
network. You open it by double-clicking it, you select sentences you want to argue
with, and your notes travel back to Claude as one small JSON file.

:::analogy
Think of it as a printed draft with a red pen and a pile of sticky notes. The
difference is that the sticky notes know exactly which sentence they were stuck to,
so they still line up after the draft is retyped.
:::

:::flag good verified | Notes are never lost on rebuild
When the document is regenerated, every existing note is searched for by its quoted
text. If the sentence moved, the note follows it. If the sentence is gone entirely,
the note is kept and clearly labelled **Unanchored** rather than silently dropped.
:::

:::flag warn one manual step | You have to click Export yourself
A page opened over `file://` cannot write to your disk on its own, and it cannot
call out to a local process. So the loop has exactly one human step: press
**Export notes for Claude**, which saves a JSON file to Downloads.
:::

## The loop, end to end

:::steps
1. Claude writes the source `.md` and runs `reviewdoc.py build`.
2. You open the HTML, select text, and add notes. They are held in this browser.
3. You press **Export notes for Claude**. A JSON file lands in Downloads.
4. Claude runs `import`, then `list`, then `reply` on each thread.
5. Claude runs `build` again. You reload the page and read the answers under your comments.
:::

The panel on the right is the whole interface. Each of your notes becomes a thread,
and Claude's answers stack underneath it in order, the way a comment thread works in
a word processor.

:::html
<svg viewBox="0 0 700 200" role="img" aria-label="Diagram of the review loop">
  <defs>
    <marker id="a1" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
      <path d="M0,0 L0,6 L7,3 z" fill="var(--dim)"/>
    </marker>
  </defs>
  <rect x="10" y="60" width="130" height="56" rx="11" fill="var(--card)" stroke="var(--line)"/>
  <text x="75" y="84" text-anchor="middle" fill="var(--ink)" font-size="13" font-weight="650">build</text>
  <text x="75" y="101" text-anchor="middle" fill="var(--dim)" font-size="11">Claude writes it</text>

  <rect x="185" y="60" width="150" height="56" rx="11" fill="var(--card)" stroke="var(--accent)"/>
  <text x="260" y="84" text-anchor="middle" fill="var(--accent)" font-size="13" font-weight="650">you annotate</text>
  <text x="260" y="101" text-anchor="middle" fill="var(--dim)" font-size="11">in the browser</text>

  <rect x="380" y="60" width="140" height="56" rx="11" fill="var(--card)" stroke="var(--warn)"/>
  <text x="450" y="84" text-anchor="middle" fill="var(--warn)" font-size="13" font-weight="650">export JSON</text>
  <text x="450" y="101" text-anchor="middle" fill="var(--dim)" font-size="11">the one manual step</text>

  <rect x="565" y="60" width="125" height="56" rx="11" fill="var(--card)" stroke="var(--good)"/>
  <text x="627" y="84" text-anchor="middle" fill="var(--good)" font-size="13" font-weight="650">reply</text>
  <text x="627" y="101" text-anchor="middle" fill="var(--dim)" font-size="11">Claude answers</text>

  <line x1="140" y1="88" x2="181" y2="88" stroke="var(--dim)" stroke-width="1.6" marker-end="url(#a1)"/>
  <line x1="335" y1="88" x2="376" y2="88" stroke="var(--dim)" stroke-width="1.6" marker-end="url(#a1)"/>
  <line x1="520" y1="88" x2="561" y2="88" stroke="var(--dim)" stroke-width="1.6" marker-end="url(#a1)"/>
  <path d="M627,116 L627,165 L75,165 L75,120" fill="none" stroke="var(--dim)"
        stroke-width="1.4" stroke-dasharray="5 4" marker-end="url(#a1)"/>
  <text x="351" y="182" text-anchor="middle" fill="var(--dim)" font-size="11">rebuild — your notes and the answers both survive</text>
</svg>
:::

## What the source file looks like

Front matter sets the header. Everything after it is ordinary markdown plus a small
set of `:::` blocks.

```
---
title: The headline
subtitle: One sentence of context.
meta: small byline line
---

## A section

Ordinary **bold**, *italic* and `code` all work.
```

:::card What each directive is for
Use `:::lede` for the opening summary, `:::analogy` when a concept needs a
real-world hook, `:::flag` for anything the reader must not miss, `:::steps` for
an ordered procedure, `:::card` to box up a digression like this one, and
`:::html` when you want to drop an inline SVG diagram in untouched.
:::

| Directive | Renders as | Use it for |
|---|---|---|
| `:::lede` | grey intro paragraph | the answer, up front |
| `:::analogy` | italic quote block | making an abstraction concrete |
| `:::flag good\|warn\|bad\|info` | coloured side-bar | verdicts and warnings |
| `:::card` | boxed panel | a self-contained aside |
| `:::steps` | numbered circles | a procedure |
| `:::tick` / `:::cross` | ✓ / ✗ lists | what holds and what does not |
| `:::html` | raw passthrough | inline SVG |

### Things that work well

:::tick
- Short sections with one idea each.
- A diagram wherever a list of facts would otherwise pile up.
- Naming your own uncertainty, so the reader knows where to push back.
:::

### Things that do not

:::cross
- Walls of prose with no headings.
- Jargon that assumes the reader already knows the answer.
- Burying the recommendation at the bottom.
:::

### Ordinary lists nest

1. A numbered step, with *emphasis* where it helps.
2. Another one.
   - A nested detail.
   - And a second nested detail.
3. Back out to the top level.

---

## What I need from you

:::flag info decisions | Two things to choose
Selecting text inside this box works exactly like anywhere else — try it.
:::

| Decision | Recommendation |
|---|---|
| Keep the export step manual | `yes` — no server means nothing to install or trust |
| Store notes inside the HTML | `yes` — one file to move around, and rebuilds keep them |

Mark this page up and export the notes when you are done.
