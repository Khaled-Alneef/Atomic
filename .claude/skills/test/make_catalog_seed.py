"""Generate src/helpers/catalog_seed.py: the first PAGES catalogue pages of
each video kind, with genres, so a fresh install's first genre tick answers
from the index. Fetched live from Cinemeta on a temp DATA_DIR, and written
as one encoded block (seed_blob) - the module still exposes FIELDS and
ROWS. `--convert` rewrites the current module in that form without a
fetch.

    py -3.13 .claude/skills/test/make_catalog_seed.py [--convert]"""
import re, shutil, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_blob import module_text      # noqa: E402

PAGES = 10
FIELDS = ("imdb_id", "title", "type", "year", "poster", "imdbRating", "genres")


def write(seed, when):
    doc = ("Catalogue rows a fresh install's genre index starts with - see\n"
           f"catalog_index._load. Generated {when} from Cinemeta's first {PAGES} pages of\n"
           "each kind (anime, series, movie): (imdb_id, title, type, year, poster,\n"
           "imdbRating, genres) per row. Data, not code; regenerate with the\n"
           "test skill's make_catalog_seed.py rather than edit.")
    tail = ("FIELDS = " + repr(FIELDS) + "\n"
            'ROWS = {kind: [tuple(row) for row in rows] for kind, rows in _DATA["rows"].items()}')
    out = REPO / "src" / "helpers" / "catalog_seed.py"
    out.write_text(module_text(doc, {"rows": seed}, tail), encoding="utf-8", newline="\n")
    print("wrote", out, out.stat().st_size, "bytes;", {k: len(v) for k, v in seed.items()})


def fetch():
    work = Path(tempfile.mkdtemp(prefix="atomic-seed-"))
    from helpers import storage
    storage.DATA_DIR = work
    from helpers import discover, catalog_index
    storage.DATA_DIR = work
    try:
        for kind in ("anime", "series", "movie"):
            t = time.monotonic(); got = 0
            for n in range(PAGES):
                rows = discover.discover_video(kind, limit=50, skip=n * 50)
                got += len(rows or [])
                if not rows:
                    break
            print(f"{kind}: {got} rows fetched in {time.monotonic()-t:.1f}s, "
                  f"index holds {catalog_index.size(kind)}")
        with catalog_index._lock:
            index = catalog_index._load()
            return {kind: [[r.get("imdb_id"), r.get("title"), r.get("type"), r.get("year"),
                            r.get("poster"), r.get("imdbRating"), list(r.get("genres") or [])]
                           for r in index[kind].values()]
                    for kind in catalog_index.KINDS}
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--convert":
        from helpers import catalog_seed as current
        when = re.search(r"Generated (.+?) from", current.__doc__ or "")
        write({k: [list(r) for r in v] for k, v in current.ROWS.items()},
              when.group(1) if when else "earlier")
    else:
        write(fetch(), time.strftime("%d %B %Y"))
