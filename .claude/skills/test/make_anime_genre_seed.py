"""Regenerate src/helpers/anime_genre_seed.py from AniList's most popular
anime. Regenerate, never edit the output by hand.

    py -3.13 .claude/skills/test/make_anime_genre_seed.py [PAGES=40]
    py -3.13 .claude/skills/test/make_anime_genre_seed.py --from a.json b.json

Fetches PAGES x 50 titles by popularity, paced at one request per 2.2s -
AniList answered 30/min when this was written (21 September 2026) and
its limit is the whole network's, so never faster. `--from` builds from
media lists already fetched ({title{english romaji} synonyms genres
format}), which is how the first seed was made without asking twice.

What goes in, and why (helpers/anime_genres has the measurement):
TV-shaped works only; English and romaji titles always; a synonym only
when it is 8+ characters normalised and belongs to one work; genres
mapped onto server.WATCH_GENRES and anything else dropped."""
import json, pathlib, sys, time, urllib.request

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from helpers.anime_genres import norm   # noqa: E402

FORMATS = ("TV", "TV_SHORT", "ONA", "OVA", "SPECIAL")
VOCAB = ("Action", "Adventure", "Comedy", "Drama", "Fantasy", "Horror", "Music",
         "Mystery", "Romance", "Sci-Fi", "Sport", "Thriller")
RENAME = {"Sports": "Sport"}
QUERY = ("query($p:Int){ Page(page:$p, perPage:50){ media(type:ANIME, "
         "sort:POPULARITY_DESC){ title{ english romaji } synonyms genres format } } }")


def fetch(pages):
    media = []
    for page in range(1, pages + 1):
        body = json.dumps({"query": QUERY, "variables": {"p": page}}).encode()
        req = urllib.request.Request("https://graphql.anilist.co", data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            media += json.loads(resp.read())["data"]["Page"]["media"]
        print(f"page {page}: {len(media)} titles", flush=True)
        time.sleep(2.2)
    return media


def build(media):
    titles, synonyms, owners = {}, {}, {}
    for number, item in enumerate(media):
        if item.get("format") not in FORMATS:
            continue
        genres = [RENAME.get(g, g) for g in item.get("genres") or ()]
        genres = [g for g in genres if g in VOCAB]
        if not genres:
            continue
        for name in ((item.get("title") or {}).get("english"),
                     (item.get("title") or {}).get("romaji")):
            key = norm(name)
            if key:
                titles.setdefault(key, set()).update(genres)
        for name in item.get("synonyms") or ():
            key = norm(name)
            if len(key) >= 8:
                synonyms.setdefault(key, set()).update(genres)
                owners.setdefault(key, set()).add(number)
    for key, genres in synonyms.items():
        if key not in titles and len(owners[key]) == 1:
            titles[key] = genres
    return titles


def write(table, count):
    lines = ['"""AniList genres for anime titles, keyed by anime_genres.norm - generated',
             f"by .claude/skills/test/make_anime_genre_seed.py from {count} AniList titles",
             f'on {time.strftime("%d %B %Y")}. Regenerate, never edit."""', "",
             f"GENRES = {VOCAB!r}", "", "TITLES = {"]
    for key in sorted(table):
        value = ",".join(str(VOCAB.index(g)) for g in VOCAB if g in table[key])
        lines.append(f"    {key!r}: {value!r},")
    lines.append("}")
    out = REPO / "src" / "helpers" / "anime_genre_seed.py"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}: {len(table)} titles, {out.stat().st_size // 1024}KB")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--from":
        media = []
        for path in sys.argv[2:]:
            media += json.load(open(path, encoding="utf-8"))
    else:
        media = fetch(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
    write(build(media), len(media))
