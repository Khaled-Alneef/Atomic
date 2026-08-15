# Atomic

A PyQt6 desktop dashboard for one person's anime, reading, series, games,
apps and websites. Ships as a single `Atomic.exe` committed at the repo
root, which updates itself from this repository's GitHub tags.

    src/main.py     window, sidebar, navigation, full screen
    src/windows/    the pages - home, tracker (Anime/Reading/Series), games, link_grid
    src/helpers/    theme, widgets, storage, external sources, updater
    packaging/      build.py and Atomic.spec
    docs/           RELEASING.md, and one VDD per released version

## Rules that outrank any plan

1. **Never release unless explicitly asked.** Work accumulates on
   `development` as 1.0.1, 1.0.2, … Do not touch `main`, tag, or bump to
   a two-part version on your own initiative.
2. **Work happens on `development`.** `main` holds released versions
   only, as squashed snapshots with no shared ancestry, so a merge
   between them refuses - release by snapshot (`release` skill).
   **Agents read and operate on `development` only. Never read, diff
   against, or check out `main` unless the task is explicitly release-
   related** (the `release-engineer` running the `release` skill) - not
   even to look something up.
3. **Implement, then stop.** Finish, rebuild locally (`build` skill),
   leave it for the user to test - don't commit or push on your own
   initiative, however small the change. Testing happens before code
   lands, not after. Once the user says **approved**, commit and push
   per rule 4.
4. **Every approved change bumps the third part of `APP_VERSION`**, in
   the same commit as the source change. `Atomic.exe` is gitignored on
   `development` and tracked only on `main`, at a release.
   - **"Approved"** (tested, not released): commit and push to
     `development` - 1.0.1 → 1.0.2.
   - **"Approved, release it"**: skip the `development` push; use the
     `release` skill instead - goes out on `main`, bumping the
     *second* part (1.0 → 1.1), not the third.
   - **Always report a push as previous → current** (e.g. "1.1.2 →
     1.1.3"), never the new number alone.
5. **Never test against real user data** in `%APPDATA%\Atomic`. Copy it
   to a temp directory and point `storage.DATA_DIR` at the copy before
   importing anything - see the `test` skill.
6. **Close any running Atomic before a build or checkout touches the
   binary**, automatically, without asking - it may destroy in-progress
   test state, but Windows won't let the build replace a running binary
   either way.

## How work gets done here

The session the user talks to is the **Manager**: plans the request,
routes each part to its owner, sequences multi-step work, and reports
back - one answer, not a transcript. Everything else is an agent in
`.claude/agents/`:

| Role | Agent | Owns |
|---|---|---|
| Architect | `architect` | What gets built next and in what order - writes `docs/ROADMAP.md`, implements nothing |
| UI Engineer | `ui-engineer` | Anything visible - pages, cards, dialogs, sidebar, theme, layout, animation, DPI |
| Integrations Engineer | `integrations-engineer` | AniList, TVMaze, MangaDex, Stremio, reading/anime sites, background threading |
| Test Engineer | `test-engineer` | Proving a change works - harnesses, measurement, reading code out of the exe |
| Release Engineer | `release-engineer` | Builds, version numbering, branches, tags, releases, VDDs, and anything touching `origin`/GitHub |

Each agent's own file carries only what's specific to it; domain
conventions and hard-won traps live in `.claude/rules/` (`ui.md`,
`integrations.md`, `testing.md`, `planning.md`) and get read on demand,
not carried in every context. Step-by-step procedures (build, test,
release, roadmap) live as Skills in `.claude/skills/`, loaded only when
actually invoked.

`docs/ROADMAP.md` is what the Architect maintains and what the rest
work from: it names the owner, the files, and the "done when" for each
item, so picking up a task doesn't start with rediscovering it. Whoever
lands an item marks it done in the same commit - a plan nobody updates
stops being one. **The Architect is dispatched only when the user asks
for planning**; otherwise the roadmap is read, not rewritten, and a
diagnosed bug goes straight to its owner rather than through a plan.

**The Manager does the work itself by default.** Every task given -
implementation, investigation, fixes, pushes - is the Manager's to
carry out directly, however large. Dispatch to an agent only when the
user asks for one ("make an agent do it", "stay", or anything similar),
or when the work genuinely can't be done from this session (it needs
its own long-running context, or several genuinely independent pieces
must run at once). Reaching for an agent by default is what this rule
exists to stop: each dispatch starts cold, re-reads what the Manager
already knows, and has repeatedly cost more than doing the work here.

**When an agent is used: one task, one narrow scope** - a UI dispatch
covers one page/dialog/area, an integrations dispatch covers one
source, not several bundled together; a mistake then stays contained
and a report stays reviewable.

**Agents work silently** - no narration - and report back only a
short, structured result: files changed, result, what was verified,
any open issue. Skip whatever doesn't apply; never pad it back out to
prose. Cut padding, not substance - verified numbers and any finding
that contradicts the plan still have to be there. Handoffs into an
agent stay just as terse: the essential ask plus facts already known
(paths, line numbers, findings), not a context dump.

Run agents in the background; never invent what a running agent will
report - say it's still running if asked. Questions with no owner (what
the repo contains, what an agent is for) are answered directly by the
Manager.

**Agents start cold - spend accordingly.** A fresh `Agent` call has no
memory of anything. Resume an existing agent (message its id) instead
of spawning a new one when it already has live/recent context on the
same topic. Doc/meta edits to this file, `.claude/agents/`,
`.claude/rules/`, or `.claude/skills/` the Manager makes directly - no
agent needed.

**Keep tool-call counts as low as the task allows.** Batch independent
reads/checks into one turn; run one broad search instead of several
narrow follow-ups; don't re-confirm what a prior call already
established. This means cutting calls that don't change the outcome,
not the verification itself - measuring a claim (see "Measure rather
than assert" below) is the point of a task like `test-engineer`'s, not
overhead to trim.

Three things cannot run concurrently - wrong answers, not just slower:
screen-measuring work (pixel probes/screenshots own the display), builds
(fixed paths, shared PyInstaller cache), and version bumps or anything
touching `main`. Verification always comes after the change it checks.

## Style

Comments explain *why*, often recording what failed before and what was
measured; match that. Commit messages: sentence-case summary line, then
a body explaining what was wrong and what was measured - not a file
list. One commit per change on `development`; the executable is only
committed on `main`, at a release.

Measure rather than assert. Things here look correct and aren't - a
toast positioned with `mapToGlobal` on mixed-DPI displays, a build
PyInstaller silently re-copied from cache, a pixel classifier that
reported every frame broken in both the broken and fixed case, a
Crunchyroll resolver that was correct in isolation while the real save
dialog raced it. Check what can be checked; say plainly what you
couldn't.
