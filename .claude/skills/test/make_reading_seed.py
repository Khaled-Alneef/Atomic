"""Regenerate src/helpers/reading_seed.py from a reading_meta.json.

    py -3.13 .claude/skills/test/make_reading_seed.py <path-to-reading_meta.json>

The file must be a copy of the owner's REAL reading_meta.json - taken with
copy_real_data.py, not with a plain copytree from inside the Claude
desktop app, whose processes see a virtualized %APPDATA% (see
.claude/rules/testing.md, "The desktop app's %APPDATA% is not his").
The 6 September seed was generated from that shadow and carried 936
verdicts; his real file that morning held 1828."""
import json
import pprint
import sys
import time
from pathlib import Path

src = Path(sys.argv[1])
saved = json.loads(src.read_text(encoding="utf-8-sig"))
medium = {str(k): str(v) for k, v in (saved.get("medium") or {}).items() if k and v}
genres = {str(k): [str(g) for g in v] for k, v in (saved.get("genres") or {}).items()
          if k and isinstance(v, list) and k in medium}
out = Path(__file__).resolve().parents[3] / "src" / "helpers" / "reading_seed.py"
body = [
    '"""Reading verdicts a fresh install starts with - see',
    'discover._load_reading_meta. Generated %s from one' % time.strftime("%d %B %Y"),
    "machine's reading_meta.json: lowercased title -> the medium MangaDex",
    'files it under, and the genre tags of a title that matched at 0.85.',
    'Data, not code; regenerate with the test skill\'s make_reading_seed.py',
    'rather than edit."""', '',
    'MEDIUM = ' + pprint.pformat(dict(sorted(medium.items())), width=100), '',
    'GENRES = ' + pprint.pformat(dict(sorted(genres.items())), width=100), '',
]
out.write_text("\n".join(body), encoding="utf-8", newline="\n")
print(f"wrote {out}: {len(medium)} mediums, {len(genres)} genre lists, {out.stat().st_size} bytes")
