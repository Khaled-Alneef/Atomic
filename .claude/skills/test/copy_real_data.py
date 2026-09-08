"""Copy the owner's REAL %APPDATA%\\Atomic to <dest>\\Atomic - from outside
the Claude desktop app's package.

    py -3.13 .claude/skills/test/copy_real_data.py <dest-root> [--check]

Why this exists (8 September 2026): the Claude desktop app is an MSIX
package (Claude_pzs8sxrjxfjjc), and every process it spawns - this
shell, Python, a rig-launched Atomic.exe - runs inside that package with
AppData virtualization. A write to %APPDATA%\\Atomic from in here lands
in LocalCache\\Roaming\\Atomic instead, and from then on every read of
that path from in here answers the shadow, however old, while his real
file goes on changing. Measured that morning: the shadow said
history.json was 28,531 bytes from 4 September and reading_meta.json
held 936 verdicts; the real files were 21,386 bytes from that morning
and 1,828 verdicts. A plain shutil.copytree from in here copies the
shadow.

So the copy is made by a robocopy spawned through WMI (Win32_Process
Create), which starts it outside the package. `--check` only compares
the inside view with the outside view for the files the shadow is known
to hold and exits non-zero when they differ - run it before trusting
anything read from %APPDATA% in here."""
import os
import subprocess
import sys
import time
from pathlib import Path

FILES = ("history.json", "series.json", "player_state.json", "settings.json", "tracker.json",
         "downloads.json", "discover_cache.json", "reading_meta.json", "schedule_cache.json",
         "apps.json", "games.json", "reader_cache.json")
REAL = Path(os.environ["APPDATA"]) / "Atomic"


def run_outside(cmd_text, out_file, timeout=180):
    """Run a cmd line outside the package; waits for `=== DONE` in out_file."""
    if out_file.exists():
        out_file.unlink()
    line = f'cmd /c ({cmd_text}) > "{out_file}" 2>&1 & echo === DONE >> "{out_file}"'
    # A PowerShell single-quoted literal: only the quote itself needs
    # doubling, and the cmd line's own double quotes pass through.
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = '"
          + line.replace("'", "''") + "' }; $r.ReturnValue")
    rc = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if rc.stdout.strip() != "0":
        raise SystemExit(f"could not spawn outside the package: {rc.stdout} {rc.stderr}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if out_file.exists() and "=== DONE" in out_file.read_text(encoding="utf-8", errors="replace"):
            return out_file.read_text(encoding="utf-8", errors="replace")
        time.sleep(1)
    raise SystemExit("the outside command did not finish")


def outside_sizes():
    out = Path(os.environ["TEMP"]) / "atomic_real_view.txt"
    cmd = " & ".join(f'for %a in ("{REAL / n}") do @echo {n} %~za %~ta' for n in FILES)
    text = run_outside(cmd, out)
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] in FILES and len(parts) >= 2 and parts[1].isdigit():
            found[parts[0]] = int(parts[1])
    return found


def check():
    real = outside_sizes()
    bad = []
    for name, size in real.items():
        here = REAL / name
        mine = here.stat().st_size if here.exists() else None
        if mine != size:
            bad.append(f"{name}: inside {mine} vs real {size}")
    if bad:
        print("SHADOWED - the inside view of %APPDATA%\\Atomic is stale:\n  " + "\n  ".join(bad))
        return 1
    print(f"inside view matches the real directory for {len(real)} files")
    return 0


def copy(dest_root):
    dest = Path(dest_root) / "Atomic"
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = Path(os.environ["TEMP"]) / "atomic_real_copy.txt"
    text = run_outside(f'robocopy "{REAL}" "{dest}" /E /XD WebView2 /NFL /NDL /NJH /NP', out, timeout=600)
    tail = [l for l in text.splitlines() if l.strip().startswith(("Files", "Bytes", "Dirs"))]
    print("\n".join(tail))
    print("copied to", dest)


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check())
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    copy(sys.argv[1])
