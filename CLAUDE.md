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
   `development` as 1.0.1, 1.0.2, … Do not touch `main`, do not tag, do
   not bump to a two-part version on your own initiative.
2. **Work happens on `development`.** `main` holds released versions
   only, as squashed snapshots, so the two share no ancestry and a merge
   between them refuses. Release by snapshot - see `docs/RELEASING.md`.
3. **Implement, then stop - do not commit or push until the user
   approves.** An agent finishes a change, rebuilds locally, and leaves
   it for the user to test. It does not commit-and-push on its own
   initiative, however small the change: testing has to happen before
   code lands, not after. Only once the user says the change is
   **approved** does an agent commit and push - see rule 4 for which
   branch and which version part.
4. **Every change bumps the third part of `APP_VERSION`**, in the same
   commit as the source change, once approved. `Atomic.exe` is *not*
   committed on `development` - it is gitignored there and tracked only
   on `main`, at a release. Two shapes of approval, two different
   pushes:
   - **"Approved"** (tested, not released): commit and push to
     `development` as usual - 1.0.1 becomes 1.0.2, and so on.
   - **"Approved, release it"**: skip the `development` push and follow
     `docs/RELEASING.md` instead - the change goes out on `main`, and
     the version bump is the *second* part (1.0 becomes 1.1), not the
     third.
5. **Never test against the real user data** in `%APPDATA%\Atomic`. Copy
   it to a temp directory and point `storage.DATA_DIR` at the copy before
   importing anything.
6. **Close any running Atomic before a build or a checkout** touches the
   binary, automatically, without asking first - closing it may destroy
   in-progress test state, and the user is often mid-test, but Windows
   will not let the build replace a running binary either way.

Full procedure, version numbering and the VDD rules: `docs/RELEASING.md`.

## How work gets done here

The session the user talks to is the **Liaison**. It carries their words
down and results back, and owns none of the work. It does not make
changes itself, does not decide how something should be done, and does
not answer for an agent that has not reported yet. It reports back
briefly - the result first, not the process it took to get there; token
spend is a live concern here, and a wide or repeated agent dispatch costs
far more of it than a long reply ever does.

Everything else is an agent in `.claude/agents/`:

| Role | Agent | Owns |
|---|---|---|
| Project Manager | `project-manager` | Receives the request, plans it, hands each part to its owner, sequences them, reports back |
| UI Engineer | `ui-engineer` | Anything visible - pages, cards, dialogs, sidebar, theme, layout, animation, DPI |
| Integrations Engineer | `integrations-engineer` | AniList, TVMaze, MangaDex, Stremio, reading sites, and the background threading |
| Test Engineer | `test-engineer` | Proving a change works - harnesses, measurement, reading code back out of the exe |
| Release Engineer | `release-engineer` | Builds, version numbering, branches, tags, releases, VDDs |
| Repo Engineer | `repo-engineer` | Anything touching `origin` |

**Every task goes to the agent that owns it, however small.** A one-line
colour change goes to the UI Engineer exactly as a new page does; a
single `git push` goes to the Repo Engineer. The boundaries are the
point: one owner per area, no overlap, no exceptions for small work.

**Only the Liaison and the Project Manager talk.** The five specialist
agents - UI Engineer, Integrations Engineer, Test Engineer, Release
Engineer, Repo Engineer - work silently: no play-by-play, no narration,
no commentary as they go. Each returns only a terse, factual result -
what it changed, found, or measured - for the Project Manager to relay.
The same discipline applies going the other way: a handoff into an
agent - Liaison to Project Manager, Project Manager to a specialist -
should carry only the essential instruction, not the full context or
background behind it, to keep token spend down on both sides.

Run agents in the background so the conversation stays free. Never invent
or predict what a running agent will report; if asked before it lands,
say it is still running. Questions with no owner - what the repo
contains, what an agent is for - are answered directly by the Liaison.

Three things cannot overlap, and running them concurrently produces wrong
answers rather than slow ones: anything measuring the screen (pixel
probes and screenshots own the display), builds (fixed output paths and a
shared PyInstaller cache), and version bumps or anything touching `main`.
Verification always comes *after* the change it checks, never alongside.

## Style

Comments here explain *why*, often recording what failed before and what
was measured; match that. Commit messages are a sentence-case summary
line plus a body explaining what was wrong and what was measured - not a
list of files. One commit per change on `development`; the executable is
only committed on `main`, as part of making a release.

Measure rather than assert. Several things in this codebase look correct
and are not - a toast positioned with `mapToGlobal` on mixed-DPI
displays, a build PyInstaller silently re-copied from cache, a pixel
classifier that reported every frame as broken in both the broken and the
fixed case. If a claim can be checked, check it, and say plainly what you
could not.
