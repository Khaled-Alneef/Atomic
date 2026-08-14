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
3. **Every change that produces a new executable bumps the third part of
   `APP_VERSION`**, in the same commit as the source change; the rebuilt
   exe is then its own separate commit.
4. **Never test against the real user data** in `%APPDATA%\Atomic`. Copy
   it to a temp directory and point `storage.DATA_DIR` at the copy before
   importing anything.
5. **Close any running Atomic before a build or a checkout** touches the
   binary - ask first, the user is often mid-test.

Full procedure, version numbering and the VDD rules: `docs/RELEASING.md`.

## How work gets done here

Specialists live in `.claude/agents/`: `qt-ui` for anything visible,
`integrations` for the external sources, `verify-change` for proving a
change works, `build-release` for the exe and version discipline,
`github` for anything touching `origin`, and `atomic-lead` as the front
door for a request that spans several of them.

**Hand substantial work to a specialist and let it run in the background,
so the conversation stays free.** Do not spawn one for something small
you can do correctly yourself - a cold start costs more than the work.
Never invent or predict what a running agent will report; if asked before
it lands, say it is still running.

Three things cannot overlap, and running them concurrently produces wrong
answers rather than slow ones: anything measuring the screen (pixel
probes and screenshots own the display), builds (fixed output paths and a
shared PyInstaller cache), and version bumps or anything touching `main`.
Verification always comes *after* the change it checks, never alongside.

## Style

Comments here explain *why*, often recording what failed before and what
was measured; match that. Commit messages are a sentence-case summary
line plus a body explaining what was wrong and what was measured - not a
list of files. Two commits per change: the `src/` change, then a separate
`Rebuild Atomic.exe with …`.

Measure rather than assert. Several things in this codebase look correct
and are not - a toast positioned with `mapToGlobal` on mixed-DPI
displays, a build PyInstaller silently re-copied from cache, a pixel
classifier that reported every frame as broken in both the broken and the
fixed case. If a claim can be checked, check it, and say plainly what you
could not.
