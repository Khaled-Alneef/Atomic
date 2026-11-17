"""A record of the crashes atomic.log cannot see.

**Why this exists.** His report, 7 November 2026: "the app crashed when I
changed the subtitles to embedded ara". Windows' own record of it was an
access violation inside python313.dll at offset 0x6955b - in
_PyEval_EvalFrameDefault, the bytecode loop - the same address as a crash
on 4 November, beside a run of heap-corruption crashes (ntdll
0xc0000374) since the 6th. An access violation ends the process before
Python can run a line, so atomic.log simply stops: its last line was five
seconds before the crash and said nothing. The same live sequence was
then replayed about eighty times on a copy of his data, on the build he
was running, without one crash. It is rare, and a reproduction is not
the road to it; the next one has to name itself.

So every run arms faulthandler (the raising thread) into a file of its own,
`crash_<pid>.log` in the data directory:

- **A clean exit deletes it** (atexit). Nothing accumulates.
- **The next launch reads what is left.** A leftover holding a fatal
  error - "Windows fatal exception" (an access violation and its kind)
  or "Fatal Python error" (qFatal's abort among them) - is kept as
  `crash-<when>.log` (the newest KEEP of them) and its first lines, the
  crashing thread's stack, go into atomic.log as `crash: ...`, so the log
  he sends carries the cause. A leftover without one is a run that was
  ended from outside (Task Manager, an updater restart through
  os._exit) and is removed.
- **Per process, not one shared file.** Nothing stops two copies of the
  app running, and the video child (mpv_proxy, `--mpv-host`) is a second
  process of this same program: a shared name would be renamed under a
  live writer. A file still held open by a running process cannot be
  renamed on Windows, which is how a live sibling's file is skipped.

Faulthandler on Windows ignores the CLR's own exceptions (0xE0434352)
and C++ ones (0xE06D7363), so the .NET side of WebView2 does not write
here. Every *other* first-chance exception is written, whoever handles
it - measured 14 November 2026: a ctypes call's access violation, caught
and turned into OSError, wrote "Windows fatal exception: access
violation" and the process carried on.

**And writing it was not harmless, which this note used to say.** It is
written from inside the exception, on the thread that raised it, while
every other thread goes on running - and with all_threads=True the dump
walks those threads' frames as they change. The video child raises
constantly: mpv's Lua scripts unwind every error with LuaJIT's own SEH
exception, 0xe24c4a02, which LuaJIT always catches. His records from 12
to 14 November hold block after block of it, two threads' dumps
interleaved line by line, and Windows' own record of all four video
deaths in that time puts the fault inside python313's traceback dumping
code (offsets 0x2b6d90 three times, 0x2b77c3 once - the functions after
PyTraceBack_Print), with none there before the 9 November build that
armed this. The last of them was "the video process stopped mid-episode"
two seconds after a source change. So only the raising thread is
written now: its own stack is the one a crash needs, and it is the one
stack nothing else is changing. And 0xe24c4a02 is never read as a death.
Never raises.
"""

import atexit
import faulthandler
import os
import re
import time
from pathlib import Path

from . import logs

KEEP = 5                    # kept crash records, newest first
_LIVE_PREFIX = "crash_"     # crash_<pid>[_<role>].log while a run is alive
_KEPT_PREFIX = "crash-"     # crash-<YYYYmmdd-HHMMSS>[_<role>].log once kept
_FATAL = ("Windows fatal exception", "Fatal Python error")
# Exceptions faulthandler writes that are part of something working: LuaJIT
# (mpv's Lua scripts) raises 0xe24c4a02 for every Lua error and catches it
# itself. A leftover holding only these is a run ended from outside.
_HANDLED_CODES = ("code 0xe24c4a02",)
# A fatal line as faulthandler writes it, whole: a named kind ("access
# violation", "stack overflow") or a complete code. **Two threads raising
# at once interleave their lines** - measured on the frozen build, 14
# November 2026: "Windows fatal exception: code 0xWindows fatal
# exception: e24c4a02" - and a torn line names no code at all, so a
# substring test for the LuaJIT code missed it and called a healthy run a
# death. Only threads raising together tear a line, and they are LuaJIT's.
_FATAL_LINE = re.compile(r"^(?:Windows fatal exception: (?:code 0x[0-9a-fA-F]{8}|[a-z][a-z ]*)"
                         r"|Fatal Python error: .*)$")
# How much of a kept record atomic.log is given: the fatal line and the
# crashing thread's frames are at the top of a block, and twenty lines
# are enough for both without pasting every idle thread's stack.
SUMMARY_LINES = 20

_file = None                # the open record; faulthandler holds its fd
_path = None


def _data_dir() -> Path:
    from . import storage
    return Path(storage.DATA_DIR)


def _last_fatal_block(text: str) -> list:
    """The lines of the last fatal block in a record, fatal line first.
    Last, because a run can write a handled first-chance exception
    earlier and then die of something else."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        line = line.strip()
        if (_FATAL_LINE.match(line)
                and not any(code in line for code in _HANDLED_CODES)):
            start = index
    if start is None:
        return []
    block = lines[start:]
    # The crashing thread is the one marked "Current thread" (or "most
    # recent call first" right under the fatal line); put it first.
    for index, line in enumerate(block):
        if line.startswith("Current thread"):
            current = block[index:]
            end = next((i for i, l in enumerate(current[1:], 1)
                        if not l.strip()), len(current))
            return block[:2] + current[:end]
    return block


def _collect(data: Path, version: str) -> None:
    """Keep or remove what earlier runs left behind."""
    try:
        leftovers = sorted(data.glob(_LIVE_PREFIX + "*.log"))
    except OSError:
        return
    for path in leftovers:
        if path == _path:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue        # held open by a live sibling - not ours to judge
        block = _last_fatal_block(text)
        role = path.stem.split("_", 2)[2] if path.stem.count("_") >= 2 else ""
        try:
            if not block:
                path.unlink()
                continue
            when = time.strftime("%Y%m%d-%H%M%S", time.localtime(path.stat().st_mtime))
            kept = data / f"{_KEPT_PREFIX}{when}{'_' + role if role else ''}.log"
            os.replace(path, kept)
        except OSError:
            continue        # still open: a sibling that is running
        logs.warning(f"crash: the last run{' (' + role + ')' if role else ''} "
                     f"died - {kept.name} has the crashing thread's stack:")
        for line in block[:SUMMARY_LINES]:
            logs.warning("crash:   " + line.rstrip())
    try:
        kept_files = sorted(data.glob(_KEPT_PREFIX + "*.log"), reverse=True)
        for old in kept_files[KEEP:]:
            old.unlink()
    except OSError:
        pass


def _clean_exit() -> None:
    global _file
    try:
        faulthandler.disable()
    except Exception:
        pass
    try:
        if _file is not None:
            _file.close()
        _file = None
        if _path is not None:
            _path.unlink()
    except OSError:
        pass


def clean_exit() -> None:
    """The clean-exit removal, for an exit that ends the process without
    running atexit (main._exit_without_teardown)."""
    _clean_exit()


def arm(role: str = "", version: str = "") -> None:
    """Start recording this process's crashes. Call once, as early as
    the data directory is known. `role` names a helper process's file
    ("video" for the mpv child) so the two records cannot be confused."""
    global _file, _path
    if _file is not None:
        return
    try:
        data = _data_dir()
        data.mkdir(parents=True, exist_ok=True)
        _path = data / f"{_LIVE_PREFIX}{os.getpid()}{'_' + role if role else ''}.log"
        _file = open(_path, "w", encoding="utf-8", buffering=1)
        _file.write(f"=== Atomic {version} pid {os.getpid()} "
                    f"{role or 'app'} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        _file.flush()
        # The raising thread only - see the module note: a dump of every
        # thread, taken while they run, is what killed the video child.
        faulthandler.enable(file=_file, all_threads=False)
        atexit.register(_clean_exit)
    except Exception:
        _file = None
        logs.exception("Could not arm the crash record")
        return
    # Only the app looks back: the video child starts with every play,
    # and judging leftovers is one process's job, not two racing.
    if not role:
        try:
            _collect(data, version)
        except Exception:
            logs.exception("Could not read the last run's crash record")


def path():
    """This run's record, for a harness to read. None when unarmed."""
    return _path

