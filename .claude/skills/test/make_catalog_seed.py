"""Generate src/helpers/catalog_seed.py: the first PAGES catalogue pages of
each video kind, with genres, so a fresh install's first genre tick answers
from the index. Fetched live from Cinemeta on a temp DATA_DIR."""
import sys, time, tempfile, shutil, pprint
from pathlib import Path
sys.path.insert(0, r"C:\Users\pc\Code\VS_Code\Python\Atomic\src")
from helpers import storage
work = Path(tempfile.mkdtemp(prefix="atomic-seed-"))
storage.DATA_DIR = work
from helpers import discover, catalog_index
storage.DATA_DIR = work
PAGES = 10
try:
    for kind in ("anime", "series", "movie"):
        t = time.monotonic(); got = 0
        for n in range(PAGES):
            rows = discover.discover_video(kind, limit=50, skip=n * 50)
            got += len(rows or [])
            if not rows:
                break
        print(f"{kind}: {got} rows fetched in {time.monotonic()-t:.1f}s, index holds {catalog_index.size(kind)}")
    with catalog_index._lock:
        index = catalog_index._load()
        seed = {}
        for kind in catalog_index.KINDS:
            seed[kind] = [(r.get("imdb_id"), r.get("title"), r.get("type"), r.get("year"),
                           r.get("poster"), r.get("imdbRating"), list(r.get("genres") or []))
                          for r in index[kind].values()]
    out = Path(r"C:\Users\pc\Code\VS_Code\Python\Atomic\src\helpers\catalog_seed.py")
    body = ['"""Catalogue rows a fresh install\'s genre index starts with - see',
            'catalog_index._load. Generated %s from Cinemeta\'s first %d pages of' % (time.strftime("%d %B %Y"), PAGES),
            'each kind (anime, series, movie): (imdb_id, title, type, year, poster,',
            'imdbRating, genres) per row. Data, not code; regenerate with the',
            'test skill\'s make_catalog_seed.py rather than edit."""', '',
            'FIELDS = ("imdb_id", "title", "type", "year", "poster", "imdbRating", "genres")', '',
            'ROWS = ' + pprint.pformat(seed, width=110, sort_dicts=False), '']
    out.write_text("\n".join(body), encoding="utf-8", newline="\n")
    print("wrote", out, out.stat().st_size, "bytes;", {k: len(v) for k, v in seed.items()})
finally:
    shutil.rmtree(work, ignore_errors=True)
