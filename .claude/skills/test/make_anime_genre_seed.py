"""Regenerate src/helpers/anime_genre_seed.py from AniList. Regenerate,
never edit the output by hand.

    py -3.13 .claude/skills/test/make_anime_genre_seed.py [PAGES=40]
    py -3.13 .claude/skills/test/make_anime_genre_seed.py --from a.json b.json

Fetches PAGES x 50 titles by popularity, then every work AniList flags
explicit (RESTRICTED_PAGES, stopping at a short page), then FLAGGED_PAGES
of its fan-service genre - paced at one request per 2.2s, since AniList
answered 30/min when this was first written (21 September 2026) and its
limit is the whole network's. The fetched media are kept in the temp
directory; `--from` builds from such a file without asking again.

What goes in, and why (helpers/anime_genres has the measurements):
TV-shaped works only, plus explicit films (they are removed, and
Cinemeta's movie catalogue carries them as easily as its series one);
English and romaji titles always; a synonym only when it is 8+
characters normalised and belongs to one work; every AniList genre and
the five tags anime_genres.work_genres maps; and for the works this app
does not show, their start years (RESTRICTED_YEARS), since by title
alone "First Love" and "Teacher's Pet" caught a Netflix drama and a
Disney cartoon. The module is one encoded block (seed_blob)."""
import json, pathlib, sys, tempfile, time, urllib.request

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from helpers.anime_genres import norm, work_genres              # noqa: E402
from helpers.content_words import ANILIST_FLAG, RESTRICTED_GENRE, SUGGESTIVE_GENRE  # noqa: E402
from seed_blob import module_text                                # noqa: E402

FORMATS = ("TV", "TV_SHORT", "ONA", "OVA", "SPECIAL")
RESTRICTED_PAGES = 80
FLAGGED_PAGES = 10
FIELDS = (f"title{{ english romaji }} synonyms genres format {ANILIST_FLAG} "
          "startDate{ year } tags{ name rank isMediaSpoiler }")
QUERY = ("query($p:Int){ Page(page:$p, perPage:50){ media(type:ANIME, "
         "sort:POPULARITY_DESC){ " + FIELDS + " } } }")
RESTRICTED_QUERY = ("query($p:Int){ Page(page:$p, perPage:50){ media(type:ANIME, "
                    f"{ANILIST_FLAG}:true, sort:POPULARITY_DESC){{ " + FIELDS + " } } }")
FLAGGED_QUERY = ("query($p:Int){ Page(page:$p, perPage:50){ media(type:ANIME, "
                 f'genre:"{SUGGESTIVE_GENRE}", sort:POPULARITY_DESC){{ ' + FIELDS + " } } }")


def fetch(pages, query=QUERY):
    media = []
    for page in range(1, pages + 1):
        body = json.dumps({"query": query, "variables": {"p": page}}).encode()
        req = urllib.request.Request("https://graphql.anilist.co", data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            got = json.loads(resp.read())["data"]["Page"]["media"]
        media += got
        print(f"page {page}: {len(media)} titles", flush=True)
        time.sleep(2.2)
        if len(got) < 50:
            break
    return media


def build(media):
    titles, synonyms, owners, years = {}, {}, {}, {}
    for number, item in enumerate(media):
        genres = work_genres(item)
        restricted = bool(item.get(ANILIST_FLAG)) or RESTRICTED_GENRE in genres
        if item.get("format") not in FORMATS and not (restricted and item.get("format") == "MOVIE"):
            continue
        genres = [g for g in genres if g not in (RESTRICTED_GENRE, SUGGESTIVE_GENRE)]
        if restricted:
            genres.append(RESTRICTED_GENRE)
        if not genres:
            continue
        year = (item.get("startDate") or {}).get("year") if restricted else None
        for name in ((item.get("title") or {}).get("english"),
                     (item.get("title") or {}).get("romaji")):
            key = norm(name)
            if key:
                titles.setdefault(key, set()).update(genres)
                if year:
                    years.setdefault(key, set()).add(int(year))
        for name in item.get("synonyms") or ():
            key = norm(name)
            if len(key) >= 8:
                synonyms.setdefault(key, set()).update(genres)
                owners.setdefault(key, set()).add(number)
                if year:
                    years.setdefault("~" + key, set()).add(int(year))
    for key, genres in synonyms.items():
        if key not in titles and len(owners[key]) == 1:
            titles[key] = genres
            if "~" + key in years:
                years.setdefault(key, set()).update(years["~" + key])
    restricted_years = {k: v for k, v in years.items()
                        if not k.startswith("~") and RESTRICTED_GENRE in titles.get(k, ())}
    return titles, restricted_years


def write(built, count):
    table, restricted_years = built
    vocab = sorted({g for genres in table.values() for g in genres})
    data = {"genres": vocab,
            "titles": {k: ",".join(str(vocab.index(g)) for g in sorted(table[k]))
                       for k in sorted(table)},
            "restricted_years": {k: ",".join(str(y) for y in sorted(v))
                                 for k, v in sorted(restricted_years.items())}}
    doc = ("AniList genres for anime titles, keyed by anime_genres.norm - generated\n"
           f"by .claude/skills/test/make_anime_genre_seed.py from {count} AniList titles\n"
           f'on {time.strftime("%d %B %Y")}. Regenerate, never edit.\n\n'
           "GENRES is the vocabulary, TITLES maps a title to indexes into it, and\n"
           "RESTRICTED_YEARS holds the start years of the works this app does not\n"
           "show (helpers/anime_genres.is_restricted).")
    tail = ('GENRES = tuple(_DATA["genres"])\nTITLES = _DATA["titles"]\n'
            'RESTRICTED_YEARS = _DATA["restricted_years"]')
    out = REPO / "src" / "helpers" / "anime_genre_seed.py"
    out.write_text(module_text(doc, data, tail), encoding="utf-8", newline="\n")
    print(f"{out}: {len(table)} titles, {len(restricted_years)} restricted, "
          f"{out.stat().st_size // 1024}KB")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--from":
        media = []
        for path in sys.argv[2:]:
            media += json.load(open(path, encoding="utf-8"))
    else:
        media = fetch(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
        media += fetch(RESTRICTED_PAGES, RESTRICTED_QUERY)
        media += fetch(FLAGGED_PAGES, FLAGGED_QUERY)
        # Kept outside the repo so a rebuild can use `--from` without
        # asking AniList again.
        dump = pathlib.Path(tempfile.gettempdir()) / "anime_genre_media.json"
        dump.write_text(json.dumps(media), encoding="utf-8")
        print(f"media kept in {dump}")
    write(build(media), len(media))
