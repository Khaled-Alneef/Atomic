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
   between them refuses - release by snapshot (`docs/RELEASING.md`).
3. **Implement, then stop.** Finish, rebuild locally, leave it for the
   user to test - don't commit or push on your own initiative, however
   small the change. Testing happens before code lands, not after. Once
   the user says **approved**, commit and push per rule 4.
4. **Every approved change bumps the third part of `APP_VERSION`**, in
   the same commit as the source change. `Atomic.exe` is gitignored on
   `development` and tracked only on `main`, at a release.
   - **"Approved"** (tested, not released): commit and push to
     `development` - 1.0.1 → 1.0.2.
   - **"Approved, release it"**: skip the `development` push; follow
     `docs/RELEASING.md` instead - goes out on `main`, bumping the
     *second* part (1.0 → 1.1), not the third.
5. **Never test against real user data** in `%APPDATA%\Atomic`. Copy it
   to a temp directory and point `storage.DATA_DIR` at the copy before
   importing anything.
6. **Close any running Atomic before a build or checkout touches the
   binary**, automatically, without asking - it may destroy in-progress
   test state, but Windows won't let the build replace a running binary
   either way.

Full procedure, version numbering and VDD rules: `docs/RELEASING.md`.

## How work gets done here

The session the user talks to is the **Liaison**: carries requests down
and results back, does no work itself. Everything else is an agent in
`.claude/agents/`:

| Role | Agent | Owns |
|---|---|---|
| Project Manager | `project-manager` | Plans the request, routes each part, sequences, reports back |
| UI Engineer | `ui-engineer` | Anything visible - pages, cards, dialogs, sidebar, theme, layout, animation, DPI |
| Integrations Engineer | `integrations-engineer` | AniList, TVMaze, MangaDex, Stremio, reading sites, background threading |
| Test Engineer | `test-engineer` | Proving a change works - harnesses, measurement, reading code out of the exe |
| Release Engineer | `release-engineer` | Builds, version numbering, branches, tags, releases, VDDs |
| Repo Engineer | `repo-engineer` | Anything touching `origin` |

**Every task goes to its owner, however small** - a one-line colour
change goes to the UI Engineer exactly as a new page does; a single
`git push` goes to the Repo Engineer. No overlap, no exceptions.

**Only the Liaison and Project Manager talk.** The five specialists work
silently - no narration - and return only a terse, factual result for
the Project Manager to relay. Handoffs in either direction carry only
the essential instruction, not full background.

Run agents in the background; never invent what a running agent will
report - say it's still running if asked. Questions with no owner (what
the repo contains, what an agent is for) are answered directly by the
Liaison.

**Agents start cold - spend accordingly.** A fresh `Agent` call has no
memory of anything; minimise how often that cost gets paid rather than
fighting it. Resume an existing agent (message its id) instead of
spawning a new one when it already has live/recent context on the same
topic. Skip the Project Manager hop and dispatch straight to the owning
specialist for a single-owner task. Brief with facts already known
(paths, line numbers, findings), not just the ask, so the agent spends
tokens acting instead of re-discovering. Doc/meta edits to this file or
`.claude/agents/` the Liaison makes directly - no agent needed.

**Keep tool-call counts as low as the task allows.** Batch independent
reads/checks into one turn instead of trickling them out one at a time;
run one broad search instead of several narrow follow-ups; don't
re-confirm what a prior call in the same task already established. This
means cutting calls that don't change the outcome, not the verification
itself - measuring a claim (see "Measure rather than assert" below) is
the point of a task like `test-engineer`'s or a feasibility check, not
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
reported every frame broken in both the broken and fixed case. Check
what can be checked; say plainly what you couldn't.
