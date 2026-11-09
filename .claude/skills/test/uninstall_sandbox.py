"""Press the real Settings > Uninstall on a disposable copy of the local build.

usage: python uninstall_sandbox.py [--check-only]

His ask, 9 November 2026: "I want to test it without deleting my local files
and app!". The copy runs from %USERPROFILE%\\AtomicUninstallTest with APPDATA and
TEMP pointed inside that folder, so what Uninstall removes is the copy, its fake
data and its fake temp. Three things keep his own install out of its reach:

  - the copy is not in %LOCALAPPDATA%\\Programs\\Atomic, so is_installed_copy() is
    False and no install folder or shortcut is named;
  - the startup task names the installed exe, so names_this_copy() is False;
  - %APPDATA% and %TEMP% are the sandbox's, and those are what the app reads.

His folders are counted before and after by a `dir` started outside the Claude
desktop app's package (.claude/rules/testing.md, "%APPDATA% is not his").
--check-only builds the sandbox and runs every check except the launch.
"""
import json, os, pathlib, shutil, subprocess, sys, tempfile, time

REPO = pathlib.Path(__file__).resolve().parents[3]
BUILD = REPO / "app"
ROOT = pathlib.Path(os.environ["USERPROFILE"]) / "AtomicUninstallTest"
PROGRAM = ROOT / "Program"
FAKE_APPDATA = ROOT / "AppData"
FAKE_TEMP = ROOT / "Temp"
FAKE_DATA = FAKE_APPDATA / "Atomic"
EXE = PROGRAM / "Atomic.exe"
REAL_DATA = pathlib.Path(os.environ["USERPROFILE"]) / "AppData" / "Roaming" / "Atomic"
REAL_INSTALL = pathlib.Path(os.environ["USERPROFILE"]) / "AppData" / "Local" / "Programs" / "Atomic"

sys.path.insert(0, str(REPO / "src"))


def real_state():
    """What is on his disk, read from outside the package."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "dir.txt"
        line = f'cmd /c dir /s /b /a-d "{REAL_DATA}" "{REAL_INSTALL}" > "{out}" 2>nul'
        ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              "-Arguments @{CommandLine='" + line + "'}; $r.ReturnValue")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        size, stable = -1, 0
        for _ in range(150):
            time.sleep(0.2)
            now = out.stat().st_size if out.exists() else -1
            stable = stable + 1 if now == size and now > 0 else 0
            size = now
            if stable >= 3:
                break
        names = out.read_text(encoding="mbcs", errors="replace").lower().splitlines()
    data = str(REAL_DATA).lower() + "\\"
    install = str(REAL_INSTALL).lower() + "\\"
    from helpers import startup
    from helpers import uninstall
    return {
        "data files": sum(n.startswith(data) for n in names),
        "settings.json": any(n == data + "settings.json" for n in names),
        "history.json": any(n == data + "history.json" for n in names),
        "installed Atomic.exe": any(n == install + "atomic.exe" for n in names),
        "shortcuts": sorted(str(p) for p in uninstall._shortcuts() if p.exists()),
        "startup task": str(REAL_INSTALL / "Atomic.exe").lower() in startup._registered_task_xml().lower(),
    }


def build_sandbox():
    if ROOT.name != "AtomicUninstallTest":
        sys.exit("refused: not the sandbox folder")
    shutil.rmtree(ROOT, ignore_errors=True)
    shutil.copytree(BUILD, PROGRAM)
    FAKE_DATA.mkdir(parents=True)
    (FAKE_DATA / "settings.json").write_text(json.dumps({
        "setup_completed_at": "2026-01-01T00:00:00+00:00",
        "setup_shown_for": "2.0"}), encoding="utf-8")
    (FAKE_TEMP / "atomic-stream-cache").mkdir(parents=True)
    (FAKE_TEMP / "atomic-stream-cache" / "piece").write_text("x")
    (FAKE_TEMP / "unrelated.txt").write_text("this one must survive")


def say(ok, text):
    print(("  PASS  " if ok else "  FAIL  ") + text)
    return ok


def main():
    check_only = "--check-only" in sys.argv
    if not (BUILD / "Atomic.exe").exists():
        sys.exit(f"no build at {BUILD}")
    print("Reading your real folders first (read only)...")
    before = real_state()
    print("  ", {k: v for k, v in before.items() if k != "shortcuts"}, f"shortcuts: {len(before['shortcuts'])}")
    print("Copying the build into the sandbox...")
    build_sandbox()
    result = True
    try:
        if not check_only:
            env = dict(os.environ, APPDATA=str(FAKE_APPDATA), TEMP=str(FAKE_TEMP), TMP=str(FAKE_TEMP))
            app = subprocess.Popen([str(EXE)], cwd=str(PROGRAM), env=env)
            print("\nThe TEST copy is open, with an empty library.")
            print("If Windows Firewall asks about it, press Cancel.")
            print("In it: Settings > Uninstall > Uninstall > Yes. Waiting for it to close...\n")
            app.wait()
            print("It closed. Waiting for the removal to finish...")
            start = time.time()
            while (FAKE_DATA.exists() or EXE.exists()) and time.time() - start < 45:
                time.sleep(0.2)
            time.sleep(1.5)
            print("\nThe test copy:")
            result &= say(not FAKE_DATA.exists(), "its data folder is gone")
            result &= say(not EXE.exists(), "its Atomic.exe is gone")
            result &= say(not (PROGRAM / "_internal").exists(), "its _internal folder is gone")
            result &= say(not (FAKE_TEMP / "atomic-stream-cache").exists(), "its stream cache is gone")
            result &= say((FAKE_TEMP / "unrelated.txt").exists(), "an unrelated temp file was left alone")
        print("\nYour real install:")
        after = real_state()
        for key in ("settings.json", "history.json", "installed Atomic.exe", "startup task"):
            result &= say(after[key] == before[key] and after[key], key + " is still there")
        result &= say(after["shortcuts"] == before["shortcuts"], f"{len(after['shortcuts'])} shortcut(s) still there")
        result &= say(after["data files"] >= before["data files"] - 25,
                      f"data folder: {before['data files']} files before, {after['data files']} after")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    print("\nRESULT: " + ("everything passed." if result else "something failed, see above."))
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
