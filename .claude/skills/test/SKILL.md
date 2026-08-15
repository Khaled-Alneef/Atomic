---
name: test
description: Test harness patterns for Atomic - redirecting storage away from real user data, offscreen vs real-window testing, measuring instead of eyeballing, and reading code back out of a frozen build. Use when verifying a change actually works.
---

# Test

Principles and traps: see `.claude/rules/testing.md`. This is the
mechanics.

## Redirect storage before anything imports

```python
import shutil, sys, tempfile, os
from pathlib import Path
sys.path.insert(0, r"<repo>/src")
from helpers import storage
work = Path(tempfile.mkdtemp(prefix="atomic-test-"))
shutil.copytree(Path(os.environ["APPDATA"]) / "Atomic", work, dirs_exist_ok=True)
storage.DATA_DIR = work          # must happen before `import main` or any page
```

## Offscreen for logic/flows

`QT_QPA_PLATFORM=offscreen`, fabricated entries, stub network calls
(`release_schedule.fetch`, `stremio.fetch_watch_progress`,
`stremio.fetch_latest_episode`, `anilist.*`, `launchers.scan_all`) with
lambdas. Use a real window instead for anything visual, DPI-related, or
timed - those don't reproduce offscreen.

## Poll, don't sleep

Wait on the page's own state (`page._refresh_toast is None`,
`page._scan_toast is None`) rather than a fixed delay. Wrap
`widgets.Toast.set_text` to record what was shown.

## Reading a frozen build back out

```python
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
arch = CArchiveReader("Atomic.exe")            # extract "PYZ.pyz" to a temp file
z = ZlibArchiveReader(str(tmp_pyz))
code = z.extract("helpers.widgets")            # then inspect co_names / co_consts
```

For icons/version resources, read PE resources with
`FindResourceW`/`LoadResource` and compare bytes against
`src/app_icon.ico`.

## Pixel measurement

`GetPixel` on the screen DC samples every ~10ms fine; grabbing a full
bitmap doesn't. Take a baseline of the settled state first. If colours
are ambiguous (this app's surfaces are near-black), monkeypatch the
paint event to fill one flat unmistakable colour for the run rather
than guessing from raw pixel values.
