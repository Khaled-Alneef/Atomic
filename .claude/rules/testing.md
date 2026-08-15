# Testing rules

Read this before non-trivial verification work (`test-engineer`, or
anyone verifying their own change). Procedures/code snippets: see the
`test` skill.

## The rule that matters most

**Never touch real user data at `%APPDATA%\Atomic`.** Always copy it to
a temp directory and redirect `storage.DATA_DIR` before importing any
page - order is not negotiable, `DATA_DIR` is read at import time.
Delete temp directories when done.

## How to test this app

- **Logic and flows:** offscreen, fabricated entries, stub the network.
  Real lookups make a test slow, flaky, and dependent on what's
  actually airing.
- **Visual, DPI-related or timed:** a real window, not offscreen -
  position bugs and animation artifacts don't reproduce offscreen.
- **Background work finishing:** poll the page's own state rather than
  sleeping a fixed time.
- **End-to-end over isolation for anything with timing/UI in the loop.**
  A function can be provably correct and the feature still shipped
  broken - the Crunchyroll resolver was right in isolation, but the
  real save dialog raced a background lookup and stored an empty
  result. Drive the real widget/dialog when the bug (or the risk of
  one) involves user timing, not just the function it calls.

## Measuring, not eyeballing

For animation/window-geometry questions, read the screen directly -
sampling pixels is fast enough, grabbing a bitmap isn't. Take a
baseline of the settled state first, compare against it.

**A classifier you haven't validated is not a measurement.** This app's
surfaces are near-black, close enough to a dark desktop that "is the
window covering this point" can't be answered from a raw pixel - the
first attempt reported every frame bad in *both* the broken and fixed
case, which was the test failing, not the app. When colours are
ambiguous, instrument: monkeypatch the paint event to fill one flat
unmistakable colour for the run.

## Verifying a frozen build

PyInstaller caches aggressively and a no-op rebuild silently re-copies
the previous binary - a "succeeded" build proves nothing when it
matters that the build is real (any release, any claim about exe
contents). Read the code back out of the frozen archive instead of
trusting the build log - see the `test` skill for the extraction
snippet. Check the function actually calls what you expect - a string
can match because it's in a docstring.

## How deep to dig

Small, obvious fix → quick targeted check. Reserve pixel-probe suites,
exe archaeology and multi-pass runs for cases actually in doubt: a
critical bug, an effect that can't be eyeballed, or a result that
contradicts expectation.
