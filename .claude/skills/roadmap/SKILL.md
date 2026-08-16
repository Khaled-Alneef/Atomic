---
name: roadmap
description: The shape of docs/ROADMAP.md and how to keep it true - writing it (architect) and marking an item done (whoever implemented it). Use when creating the roadmap, picking work off it, or updating it after an item lands.
---

# Roadmap

`docs/ROADMAP.md` is the one place that says what happens next. The
`architect` writes it; every other agent reads it and updates the item
it finished. Planning rules and the standing list of known defects:
`.claude/rules/planning.md`.

## Picking work off it

Take the top unstarted item whose owner matches you. Everything needed
to start is in the item - if it isn't, that's a defect in the plan: say
so rather than guessing.

## The shape

An index table first, so an agent can find its work without reading the
whole file:

```markdown
| # | Item | Owner | Size | Status |
|---|---|---|---|---|
| 1 | Bound manga_sites' regex and reads | integrations-engineer | contained | todo |
| 2 | Survive an exception in a Qt slot | ui-engineer | spans modules | todo |
```

`Status` is one of `todo` / `doing` / `done` / `dropped`.
`Size` is `contained`, `spans modules`, or `shape unknown - investigate
first`. Never hours.

Then one block per item, in the same order, each exactly these six
fields:

```markdown
### 1. Bound manga_sites' regex and reads

**What** - `manga_sites.py` can freeze the whole app on a malformed
search result, the same way `anime_sites.py` could before 1.3.
**Why now** - Python's regex engine holds the GIL, so this stops every
thread including the UI. Measured at 32.5s on a 0.7MB page; Windows
offers to kill a window unresponsive for ~30s. Reachable from the
Add/Edit form.
**Owner** - integrations-engineer
**Where** - `src/helpers/manga_sites.py`: `_AJAX_SEARCH_CARD_RE` (~252),
the `.*?` at ~102, `resp.read()` at ~184/194. Copy the fixed pattern
from `anime_sites._read_body` (read1 + size cap + wall-clock deadline).
**Done when** - a malformed 2MB page parses in under 0.1s, a slow-drip
host returns inside the timeout, and the existing search results for the
four known site shapes are byte-identical to before.
**Risk** - the parity check is the point: these engines are the only
thing standing between a reading site and no suggestions at all.
```

**What** is behaviour the user would notice, or a defect that exists -
not an implementation. **Why now** is the cost of leaving it alone; an
item whose cost can't be stated doesn't belong. **Where** points at real
paths and line numbers already known, so the implementer doesn't spend
their cold start rediscovering them. **Done when** is observable -
"faster" is not, "under 8s instead of 24s, measured" is. **Risk** names
what could break, preferring what already broke here before.

## Keeping it true

When an item lands, the agent that did it edits that item in the same
commit as the change: set `Status` to `done`, and if the work turned out
different from the plan, correct the item to say what was actually done.
A plan nobody updates stops being one, and the next agent to read a
stale roadmap wastes a whole context on work already finished.

Dropping an item is a normal outcome - set `dropped` and add one line
saying why. Don't delete it: the reasoning is why it won't be proposed
again next month.
