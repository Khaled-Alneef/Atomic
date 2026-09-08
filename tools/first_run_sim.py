"""Open Atomic exactly as a brand-new install sees it.

    py -3.13 tools/first_run_sim.py

The real app, the real first-run wizard, the real window - against a
throwaway data directory, so nothing it writes can reach the owner's own
library. That redirection is the whole point of the script and it is not
optional: `.claude/rules/testing.md` forbids testing against
`%APPDATA%\\Atomic`, and a first-run simulation is the single most
destructive thing to get wrong, because "fresh install" is exactly the
state where a wrong path would overwrite everything.

**`storage.DATA_DIR` is redirected before any page is imported.** Every
module reads it at import time, so the order below is load-bearing:
import storage, repoint it, and only then let `main` (and through it the
pages, the wizard and the settings file) be imported.

What a genuinely fresh install means here, per
`setup_wizard._install_is_fresh`: no setting ever saved *and* no entry
file with anything in it. An empty directory satisfies both, so the
wizard offers itself about a second after the window paints.

Flags:
    --keep      leave the temp directory behind and print its path,
                for inspecting what the wizard actually wrote
    --seed      pre-fill it with a couple of entries instead, to check
                the opposite case: an install that already holds data
                must *not* be offered the wizard
"""

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

KEEP = "--keep" in sys.argv
SEED = "--seed" in sys.argv

# --- the redirection, before anything else touches storage -----------
from helpers import storage                                    # noqa: E402

sandbox = Path(tempfile.mkdtemp(prefix="atomic-first-run-"))
storage.DATA_DIR = sandbox
storage.DATA_DIR.mkdir(parents=True, exist_ok=True)

real_data = Path(storage._default_data_dir())
if sandbox == real_data or str(sandbox).startswith(str(real_data)):
    raise SystemExit("Refusing to run: the sandbox resolved inside the "
                     "real data directory.")

print("Simulating a first run of Atomic")
print(f"  data directory : {sandbox}")
print(f"  real data      : {real_data}  (untouched)")

if SEED:
    # The negative case: an install that already holds entries is not
    # fresh, so the wizard must stay away and the app open straight to
    # Home. Useful for proving the offer is conditional rather than
    # unconditional.
    storage.save("series.json", [
        {"id": "seed-1", "type": "Anime", "title": "An Existing Show",
         "status": "Watching", "progress": "S01E03",
         "progress_verified": True},
    ])
    print("  seeded         : one existing entry (wizard should NOT appear)")
else:
    print("  seeded         : nothing (wizard SHOULD appear)")

print()
print("Close the window to end the simulation.")
print()

import main                                                    # noqa: E402

try:
    main.main()
finally:
    if KEEP:
        print(f"\nLeft the sandbox in place: {sandbox}")
        for path in sorted(sandbox.rglob("*")):
            if path.is_file():
                print(f"   {path.relative_to(sandbox)}  "
                      f"{path.stat().st_size} bytes")
    else:
        shutil.rmtree(sandbox, ignore_errors=True)
        print("\nSandbox removed. Nothing outside it was written.")
