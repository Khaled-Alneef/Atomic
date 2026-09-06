# Atomic

A PyQt6 desktop dashboard for one person's anime, reading, series, games,
apps and websites. Ships as `Atomic.zip` - one `Atomic.exe` inside it -
committed at the repo root, which updates itself from this repository's
GitHub tags.

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
   initiative, however small the change.

   **`remote-tests` is pushed only when the user says to push to it.**
   His rule, 25 August 2026, stated as its own line because "rebuild it"
   had started to read as "and put it where I can install it". It is the
   branch he installs from on another machine, so a push to it lands a
   build on a device he may be in the middle of testing on. Rebuild
   locally and say it is ready; wait to be told. Testing happens before code
   lands, not after. Once the user says **approved**, commit and push
   per rule 4.
4. **Every approved change bumps the third part of `APP_VERSION`**, in
   the same commit as the source change. `Atomic.exe` and `Atomic.zip`
   are gitignored on `development`; the **zip** is tracked on `main`, at
   a release (rule 8).

   **The first zipped release carries both**, and only that one: every
   install already out there runs an updater that looks for
   `Atomic.exe` and nothing else, so a release without it leaves them
   unable to update in place. Once a zip-aware build is what people are
   running, later releases drop the exe.
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
7. **One second.** The owner's standing rule, 21 August 2026: *"make all
   transitions in the app take at most 1 sec"*. Anything the user
   started and is now watching - a page opening, a search answering,
   sources listing, the reader or player opening - is finished, or is
   *showing what it has so far*, inside a second. A scroll frame has
   16.7ms, not a second: 60Hz is the budget there.

   This is a rule about what is on screen, not about when the network
   replies. Where an answer cannot arrive in a second, show the part
   that has (`on_partial`, as `streams.find_streams` and
   `subtitles.search` already do) and fill the rest in - never an empty
   surface waiting on the slowest source.

   **Measure it before claiming it**, and against the frozen build, not
   only the source tree. Three things that were slow and what they
   actually were, so the same ground is not re-dug:
   - every HTTP request opened a new connection (six GETs: **40.3s**,
     against **0.75s** over one kept-alive connection) - fixed in
     `helpers/net.py`, which everything must now go through;
   - a scroll body was transparent, so Qt repainted every widget every
     frame instead of blitting (Home: **29.4ms** per frame, *every*
     frame over budget → **4.6ms**, none) - `widgets.scroll_area`'s
     `ground`;
   - a search's results depended on which sites won a race inside the
     budget, so the same query returned 22, then 10, then 16 rows.

   None of it was Python, Qt, or the owner's connection, and no rewrite
   in another language would have touched any of it.
8. **Ship the zip, never the bare exe.** The owner's rule, 25 August
   2026, after a build he could not download: `Atomic.zip` is what goes
   on `main` at a release and on `remote-tests`, and the exe is not
   committed beside it.

   It is a measurement, not a preference. The bare exe was refused on
   download as `Trojan:Win32/Wacatac.B!ml` - Microsoft's *machine
   learning* classifier, no signature match - while **the identical
   bytes inside a zip downloaded cleanly**. Before concluding that, all
   seven builds of that day were compared out of the `remote-tests`
   history: same 193 bundled entries (nothing ever added), same 347
   Python modules (none added), byte-identical bootloader (the first
   differing byte is the PE TimeDateStamp at 0x108), and every one of
   them scanning clean under Defender with cloud protection on and the
   Mark-of-the-Web set. `upx=True` in `Atomic.spec` has never applied -
   upx.exe is not installed - so the usual first suspect was never in
   play. Nothing in the code was ever shown to cause it; the container
   was.

   `python packaging/build.py --zip` writes it. `helpers/updater.py`
   prefers `Atomic.zip` at a tag and falls back to `Atomic.exe`, so
   releases already published still install - **do not remove that
   fallback**, and see rule 4 for what the first zipped release has to
   carry.

9. **Find the cause before writing the fix - by measurement, not by
   reading.** The owner's ask, 21 August 2026, after the pass above
   landed every item on his list: *"make the method you used (accurate
   calculations, finding new solutions) a rule for you and the other
   agents"*. It outranks the instinct to start editing, and it is not
   the same as "test afterwards" - the measuring comes **first**, and
   often changes what gets built.

   The loop, in order:

   1. **Reproduce and put a number on it** before forming a theory. A
      harness that drives the real thing (`test` skill), not a reading
      of the code. "The scroll stutters" became "29.4ms per frame, 100%
      of frames over a 16.7ms budget, 278 paints per frame" - and the
      last of those three numbers is what named the cause.
   2. **Split the number until one part dominates.** DNS / TCP / TLS /
      first byte, not "the request is slow". Per site, not "search is
      slow". Frame time *and* paint count, not "it feels heavy".
   3. **Test the theory in isolation before building on it.** One
      attribute at a time, re-measured each time: it took four variants
      to learn that a single `WA_OpaquePaintEvent` bought the whole 4x
      and the palette changes bought nothing.
   4. **Run a control.** Two runs of *unchanged* code, to find out what
      moves on its own. That is the only reason a 37% screenshot diff
      was correctly read as live network content rather than a bug -
      the control diffed 37% too.
   5. **Prove the fix on the same measurement**, then prove it changed
      nothing else (pixel diff, correctness check, the frozen exe).
   6. **Write the number into the code** at the place it explains, with
      the date. Every table in `net.py` and `manga_sites.py` is there so
      the next person inherits the measurement instead of the guess.

   Two failure modes this exists to catch, both of which happened here:
   a fix that measured as doing **nothing** (Qt's polish silently
   cleared the attribute - caught only because the number was taken
   again afterwards), and a fix that was **fast and wrong** (a ground
   colour that was 12 levels off across 65% of the frame, caught only
   by diffing screenshots). A change nobody measured is a guess, however
   good the reasoning behind it reads.

   Corollary: **say what was not measured.** "I did not exercise the
   browser launch live" is a finding. Silence about it is a claim.

10. **A visible change is proven on a screenshot of the running app.**
    His rule, 2 September 2026: *"make it a rule to test any UI
    modifications on screenshots from the user POV!"* - after three
    changes verified in a browser pane shipped broken in the window.
    Photograph the real window (`QScreen.grabWindow(0)`, cropped to its
    geometry - a WebView2 page is a native child and `widget.grab()`
    leaves a hole where it is), look at the picture, and say which
    screenshots were taken. `.claude/rules/ui.md` has the procedure.

11. **A root-cause fix retires what it replaced - with his say-so.**
    His rule, 31 August 2026: *"if we find a root changing fix, then
    remove the old code if I approve"*. Two weeks of scroll work left
    four superseded models layered on top of each other, and every later
    measurement had to reason past all of them.

    So when a fix changes the root rather than a symptom, say plainly
    what it supersedes and ask. On approval, remove the old code **only
    where nothing still uses it** - check by scanning the whole tree, and
    re-check after each deletion, because removing one dead function
    usually kills the constants it was the last caller of. Anything still
    in use stays, and is said out loud: on the day this rule was written,
    the painted scroll model was superseded on one page and still ran
    every other, so only four members were genuinely dead.

    Keep the measurement that justified the change in the code that
    replaced it. The point is to remove dead machinery, not the evidence.

12. **A change is proven by the loop, not by any one step of it.** His
    rule, 3 September 2026, after the pass that closed his eighteen-item
    list: *"the testing methods you used for testing make them rules!!!
    it is perfect!"*. The method, in order, and every step is written
    out in `.claude/rules/testing.md`:

    1. **Reproduce on the build he tested**, against a copy of his
       data, and photograph the exact state he described. Two of his
       reports did not reproduce that day (the shelves drew in under a
       second) and one reproduced differently (two blank tiles, not
       every tile) - the fix that followed was for what was actually
       on screen.
    2. Fix, with the number written into the code (rule 9).
    3. **Run the source tree against a copy as a preview**, never as
       the proof - it is where a wrong click path is cheapest to find
       (the manga "Kingdom" opening the anime's page was found there).
    4. **Build, then read the archive back**: the symbols the change
       added must be in the frozen code and the ones it deleted must
       be gone, before a single screenshot is taken of it.
    5. **Drive the frozen exe from a separate process** (the rig in the
       `test` skill): the screenshots of rule 10, playback watched from
       the screen and confirmed against the app's own state file, and
       the log lines that prove the claim - the child stayed alive, the
       restart took the pre-started one, nothing fell back.
    6. **Every regression the verification finds goes back through 2
       to 5.** Three builds that day, and the second and third existed
       only because step 5 of the one before found something.

    Three habits ride with it. **Let the log find the next bug**: a
    silent `except` becomes a log line, and the line it produces is
    read - the three oversized covers and a broken image proxy were both
    found by lines that had not existed an hour earlier. **Rule out the
    environment before the app**: a dead pointer was another program's
    cursor clip and a stray window's title, not Atomic. **Verify the
    verification**: a review finding is refuted by two independent
    readers before it is fixed, and a fix the Manager applied gets its
    own checker - one of nine checks that day found a wrapper that
    silently bypassed the fix.

## Plan in named phases, always

**The owner's ask, 23 August 2026**, after watching a run go past: *"the
plan and phases method you used, I do not know what is it, but make it a
rule to use it always it is good!"*

What he was looking at is the **Workflow tool's phased plan** - a script
that declares `meta.phases` up front (`[{title, detail}, ...]`) and then
groups the work under `phase('Name')` calls, so the progress display
reads as *Map → Fix → Verify* rather than as an undifferentiated stream
of tool calls. He can watch it, and he can tell which part is which.

So, for any request with more than one moving part:

1. **Name the phases before starting**, in the order they will run, and
   say what each one is for. Investigation is its own phase and comes
   first - it is where rule 8's measuring lives.
2. **Say which phase each piece of work belongs to** as it happens, so a
   report can be read against the plan.
3. **Verification is always its own final phase**, never folded into the
   phase that made the change - see rule 8 step 5.
4. Where the work genuinely fans out (several independent files to map,
   several sources to measure), run it as an actual `Workflow` with
   those phases; where it does not, the phases are still named in the
   answer. **The phases are the deliverable, not the tool.**

A phase that turns out to be unnecessary is dropped out loud. A phase
that fails is reported as failed - "Verify" exists precisely so that a
fix nobody proved has somewhere to be marked unproven.

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
stops being one. It lives on `development` only and is deleted from
every release snapshot - a shipped tree describes what the app *is*,
not what is queued for it. **The Architect is dispatched only when the user asks
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

**Never edit a source file through PowerShell's `Get-Content`/
`Set-Content`.** It reads in the system codepage and writes UTF-8, so
every non-ASCII character in the file is silently mangled - `—` became
`â€”` and `⟳` became `âŸ³` across two files, and it reached the user's
screen. Use the Edit/Write tools, or Python with an explicit
`encoding="utf-8"`.

Measure rather than assert. Things here look correct and aren't - a
toast positioned with `mapToGlobal` on mixed-DPI displays, a build
PyInstaller silently re-copied from cache, a pixel classifier that
reported every frame broken in both the broken and fixed case, a
Crunchyroll resolver that was correct in isolation while the real save
dialog raced it. Check what can be checked; say plainly what you
couldn't.

13. **A change the user makes shows everywhere at once.** His rule, 6
    September 2026: *"make any changes the user make like un-saving the
    watch or read from the main page, make the effect appear
    immediately in the main page no need to switch pages to refresh!!!!
    make this a rule and implement it in the whole app!"*

    The mechanism is `helpers/changes.py`: every write the user makes
    (a save, a removal, a status, a mark, a history edit) calls
    `changes.bump()` **at the write itself**, never at the surface that
    asked for it, so every surface is covered by construction. Each web
    page's 150ms tick reads `changes.version()`: a list page (Home,
    Discover, Saved, History, Schedule) redraws, a catalogue grid
    patches its numbers and saved marks in place, and a page behind an
    overlay does it the moment the overlay closes. The tracker's
    background lookups never bump it - that is why it is a counter and
    not a file mtime, and why the file watch could not do this job.

    So a new write the user can make gets a `bump()` beside it, and a
    new page reads the counter in its tick. Photographed on the frozen
    build: Reacher removed on its details page, and Home behind it
    without the card two seconds later, no page switch.
