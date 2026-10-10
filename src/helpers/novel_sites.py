"""Novels: four text sites, two Arabic and two English, behind one shape.

The owner, 10 October 2026: *"I want to add a new section on the
sidebar, named Novels, I want to make it source from different sites,
some are arabic and others are English ... find 2 sites for Arabic and
other 2 for English, and add them normally to the app like the manga and
others!"*

**Chosen by measurement from his connection, not by reputation.** Every
candidate was fetched through helpers/net.py - the client the app
actually uses - on 10 October 2026:

    refused (403, `cf-mitigated: challenge` - Cloudflare's bot check,
    which no keyless HTTP client passes): royalroad.com, scribblehub.com,
    webnovel.com, freewebnovel.com, novelupdates.com, allnovel.org,
    novelhall.com, novelfull.net, lightnovelpub.com, novellive.app
    dead or parked: kolnovel.site and riwayat-kings.com (no DNS),
    lightnovelworld.co (521), novelarab.com (525), novelbin.com (TLS
    EOF), lightnovelpub.vip and novgo.co (parked domains)
    answered, and looked at further: kolnovel.com, rewayat.club,
    sunovels.com, riwyat.com, readnovelfull.com, novelfire.net,
    novelcool.com, novelbuddy.me

and of those, the four kept are the ones whose search, chapter list and
chapter text all answer to a plain request:

    site            search      chapter list               one chapter
    kolnovel.com    2.0s        557 in the series page,    1.0s
                                1.0s (The Beginning After
                                the End)
    rewayat.club    0.9s JSON   1,441 in one call, 0.8s    2.7s JSON
                                (`/all/`, Lord of the
                                Mysteries)
    readnovelfull   0.75s       3,210 in one call, 1.2s    0.7-3.2s
                                (Shadow Slave)
    novelfire.net   3.0-3.9s    100 a page, 0.6s a page    0.6-2.7s

Turned down: sunovels.com searches in the browser (the server's
`/search?q=` answers no rows) and salts its chapters with hex-and-name
watermark paragraphs; riwyat.com's `?s=` search returns its home page
(byte-identical, 207,047 bytes); novelcool.com's Shadow Slave page is
2.1MB, took 6.0s and lists 2,503 chapters against the 3,210 the other
two hold; novelbuddy.me renders its search in the browser.

Everything here fails soft: a site that does not answer is a section
with fewer rows, never an exception at the caller.
"""

import concurrent.futures
import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from . import logs, net, storage

TYPE = "Novel"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
DEFAULT_TIMEOUT = 10

# Order is the order a mixed list interleaves in: one Arabic site, one
# English, so neither language is pushed below the other's first page.
SITES = (
    {"id": "kolnovel", "name": "Kolnovel", "lang": "ar",
     "base": "https://kolnovel.com"},
    {"id": "readnovelfull", "name": "ReadNovelFull", "lang": "en",
     "base": "https://readnovelfull.com"},
    {"id": "rewayat", "name": "Rewayat Club", "lang": "ar",
     "base": "https://rewayat.club"},
    {"id": "novelfire", "name": "Novel Fire", "lang": "en",
     "base": "https://novelfire.net"},
)
_BY_ID = {site["id"]: site for site in SITES}

_REWAYAT_API = "https://api.rewayat.club/api"

# rewayat.club's chapter list carries its translators' announcement posts
# as chapters with made-up numbers - measured on Lord of the Mysteries:
# 123321213 ("for translators and authors"), 8888888 and 4141441, beside
# real chapters 1-1418; and on Legend Of Swordsman 999888, beside 1-4100.
# Nothing real is numbered this high.
_REWAYAT_NOT_A_CHAPTER = 100_000


# ---------------------------------------------------------------- basics

def is_novel(entry) -> bool:
    return str((entry or {}).get("type") or "").strip() == TYPE


def _host(url) -> str:
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_for(entry):
    """The site an entry's chapters come from: its site_id, else its
    url's host. None for anything that is not one of SITES."""
    entry = entry or {}
    found = _BY_ID.get(str(entry.get("site_id") or ""))
    if found:
        return found
    mine = _host(entry.get("url"))
    for site in SITES:
        if mine and mine == _host(site["base"]):
            return site
    return None


def site_name(entry) -> str:
    return str((site_for(entry) or {}).get("name") or "")


def direction(entry) -> str:
    """"rtl" for the Arabic sites, "ltr" for the English ones."""
    site = site_for(entry)
    return "rtl" if site and site["lang"] == "ar" else "ltr"


def _headers(referer=None, accept=None) -> dict:
    headers = {"User-Agent": _UA,
               "Accept": accept or "text/html,application/xhtml+xml,*/*",
               "Accept-Language": "ar,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    return headers


# A series page is fetched twice on a details open - once for the facts
# (details) and once for the chapter list - so the body is kept briefly
# and the second ask is a dictionary lookup. Bounded, and short-lived on
# purpose: a refresh press must reach the site.
_PAGE_TTL_S = 120
_PAGE_CACHE_MAX = 48
_pages = {}
_pages_lock = threading.Lock()


def _get(url, deadline, referer=None, accept=None, keep=False) -> str:
    """The body of `url`, or raises. `keep` serves a page read in the
    last _PAGE_TTL_S from memory."""
    if keep:
        with _pages_lock:
            hit = _pages.get(url)
        if hit and time.monotonic() - hit[0] < _PAGE_TTL_S:
            return hit[1]
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        raise TimeoutError("no time left for another request")
    request = urllib.request.Request(net.ascii_url(url),
                                     headers=_headers(referer, accept))
    with net.urlopen(request, timeout=timeout) as response:
        body = net.read_text(response, min(deadline, net.deadline_in(timeout + 5)))
    if keep:
        with _pages_lock:
            _pages[url] = (time.monotonic(), body)
            while len(_pages) > _PAGE_CACHE_MAX:
                _pages.pop(next(iter(_pages)), None)
    return body

# **Arabic genre names in English** - the owner, 10 October 2026: "can u
# make the Ara novels genres in Eng like others?". Every genre kolnovel
# files under, read off its /series/ page that day (154 names, several
# spellings of one genre among them); rewayat's API names each genre in
# English itself (_rewayat_rows). Only the label changes - a chip still
# opens the site's own page for that genre. A name not here is shown as
# the site wrote it.
_GENRE_EN = {
    "أبطال خارقين": "Superheroes", "أساطير": "Mythology", "أسرار": "Mystery",
    "أشباح": "Ghosts", "أعراق": "Races", "أكشن": "Action", "اكشن": "Action",
    "أكشن مغامرة": "Action Adventure", "اكشن مغامرات": "Action Adventure",
    "ألعاب": "Games", "ألغاز": "Mystery", "حل الألغاز": "Puzzles",
    "إثارة": "Thriller", "الإثارة": "Thriller", "إثارة وتشويق": "Thriller",
    "تشويق": "Suspense", "إدارة أعمال": "Business", "إسلامي": "Islamic",
    "إنتقال الى عالم أخر": "Isekai", "انتقال إلى عالم آخر": "Isekai",
    "إيسيكاي": "Isekai", "ايسيكاي": "Isekai", "إنتقام": "Revenge",
    "انتقام": "Revenge", "استراتيجي": "Strategy", "اكاديمي": "Academy",
    "الخيال العلمي": "Sci-Fi", "خيال علمي": "Sci-Fi", "الدراما": "Drama",
    "دراما": "Drama", "العصر الحديث": "Modern", "بطل ذكي": "Smart Protagonist",
    "بطل رمادي": "Grey Protagonist", "بطل شرير": "Villain Protagonist",
    "بطل محظوظ": "Lucky Protagonist", "بطل مضاد": "Anti-Hero",
    "مضاد البطل": "Anti-Hero", "بطل ناضج": "Mature Protagonist",
    "بطل وقح": "Shameless Protagonist", "بقاء": "Survival", "نجاة": "Survival",
    "بناء إمبراطورية": "Empire Building", "بناء ممالك": "Kingdom Building",
    "بناء مملكة": "Kingdom Building", "بوليسي": "Detective",
    "تاريخي": "Historical", "تحقيقات": "Investigation", "تدريب": "Training",
    "تطور شخصيات": "Character Growth", "تطوير العاب": "Game Development",
    "تقمص شخصيات": "RPG", "تلاعب": "Manipulation", "تناسخ": "Reincarnation",
    "ثراء مفاجئ": "Sudden Wealth", "جريمة": "Crime", "جوسي": "Josei",
    "حرب": "War", "حروب": "War", "حرب بين النجوم": "Space War",
    "حريم": "Harem", "حياة مدرسية": "School Life", "مدرسي": "School Life",
    "مدرسية": "School Life", "خارق للطبيعة": "Supernatural",
    "ظواهر خارقة للطبيعة": "Supernatural", "ما وراء الطبيعة": "Supernatural",
    "خوارق": "Supernatural", "خيال": "Fantasy", "فانتازي": "Fantasy",
    "فانتازيا": "Fantasy", "فنتازيا": "Fantasy", "خيال شرقي": "Eastern Fantasy",
    "خيال مظلم": "Dark Fantasy", "داخل لعبة": "Game World", "دموي": "Gore",
    "دهاليز": "Dungeons", "زنزانات": "Dungeons", "رعب": "Horror",
    "رعب كوني": "Cosmic Horror", "رعب نفسي": "Psychological Horror",
    "رواية ويب": "Web Novel", "رومانسي": "Romance", "رومانسية": "Romance",
    "رياضة": "Sports", "زراعة": "Cultivation", "زيانشيا": "Xianxia",
    "شيانشيا": "Xianxia", "ستيم بانك": "Steampunk", "سحر": "Magic",
    "سفر بالزمن": "Time Travel", "سفر عبر الزمن": "Time Travel",
    "سلالة نادرة": "Rare Bloodline", "سنين": "Seinen", "سينن": "Seinen",
    "سينين": "Seinen", "سوداوي": "Dark", "مظلمة": "Dark", "سياسة": "Politics",
    "سياسية": "Politics", "صراعات سياسية": "Political Intrigue",
    "شريحة من الحياة": "Slice of Life", "شعر": "Poetry",
    "شوانهوان": "Xuanhuan", "شوجو": "Shoujo", "شونين": "Shounen",
    "شياطين": "Demons", "صينية": "Chinese", "ضعيف-إلى-قوي": "Weak to Strong",
    "طبي": "Medical", "عائلي": "Family", "عالم واسع": "Vast World",
    "عسكري": "Military", "عسكرية": "Military", "عشائر": "Clans",
    "عموض": "Mystery", "غموض": "Mystery", "غموض تدريجي": "Slow-Burn Mystery",
    "غموض نفسي": "Psychological Mystery", "غير بشري": "Non-Human",
    "فان فيكشن": "Fan Fiction", "فانفيك": "Fan Fiction", "فضاء": "Space",
    "فلسفي": "Philosophical", "فناء": "Apocalypse", "نهاية العالم": "Apocalypse",
    "ما بعد الكارثة": "Post-Apocalyptic", "ما بعد نهاية العالم": "Post-Apocalyptic",
    "فنون القتال": "Martial Arts", "فنون قتال": "Martial Arts",
    "فنون قتالية": "Martial Arts", "مهارات القتال": "Martial Arts",
    "قصة قصيرة": "Short Story", "ون شوت": "One Shot",
    "قوة خارقة": "Superpowers", "قوى خارقة": "Superpowers",
    "كوميدي": "Comedy", "كوميديا": "Comedy", "كوميدية": "Comedy",
    "لاموتى": "Immortality", "مأسأة": "Tragedy", "مأساة": "Tragedy",
    "مأساوي": "Tragedy", "ماساة": "Tragedy", "مؤامرة": "Conspiracy",
    "مغامرات": "Adventure", "مغامرة": "Adventure",
    "مكر و خداع": "Cunning & Deceit", "ملحمي": "Epic", "ميكا": "Mecha",
    "ناضج": "Mature", "نظام": "System", "نفسي": "Psychological",
    "هوية مخفية": "Hidden Identity", "ووشيا": "Wuxia", "ووكسيا": "Wuxia",
}


def genre_in_english(name) -> str:
    text = str(name or "").strip().lstrip("#").strip()
    return _GENRE_EN.get(text, text)


def _get_json(url, deadline):
    return json.loads(_get(url, deadline, accept="application/json, */*"))


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v ]+")


def _text(fragment) -> str:
    """Tags out, entities decoded, runs of space folded."""
    plain = html.unescape(_TAG_RE.sub("", str(fragment or "")))
    return _SPACE_RE.sub(" ", plain).strip()


def _absolute(url, base) -> str:
    url = html.unescape(str(url or "").strip())
    if not url or url.startswith("data:"):
        return ""
    return urllib.parse.urljoin(base + "/", url)


def _row(site, title, url, cover="", **extra) -> dict:
    row = {"title": _text(title), "url": url, "cover_url": cover,
           "type": TYPE, "site_id": site["id"], "site_name": site["name"],
           "lang": site["lang"], "imdb_id": "", "year": ""}
    row.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return row


def _meta(body, name) -> str:
    found = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"'
                      % re.escape(name), body, re.I)
    return html.unescape(found.group(1)).strip() if found else ""


def _paragraphs(segment) -> list:
    """`segment`'s paragraphs, split on every `<p` - several of these
    sites leave a `<p>` unclosed and open the next one inside it, so a
    `<p>...</p>` match would swallow two paragraphs as one."""
    out = []
    for piece in re.split(r"<p\b[^>]*>|<br\s*/?>\s*<br\s*/?>", segment, flags=re.I):
        line = _text(piece)
        if line:
            out.append(line)
    return out


def _until_div(body, marker) -> str:
    """From `marker` to the first `</div>` after it. Measured on all
    three HTML sites (10 October 2026): the chapter's text container
    holds no nested div, and what follows its close is the site's own
    furniture - a reading-settings panel on kolnovel, ad slots on Novel
    Fire, the chapter navigation on ReadNovelFull."""
    start = body.find(marker)
    if start < 0:
        return ""
    start = body.find(">", start) + 1
    end = body.find("</div>", start)
    return body[start:end if end > 0 else len(body)]


# ---------------------------------------------------------------- kolnovel
#
# A WordPress "LightNovel" (themesia) site. Search and the series archive
# are <article> cards; the series page carries every chapter in its
# `.eplister` list, newest first (557 for The Beginning After the End, in
# one 623KB page).

def _kol_cards(site, body) -> list:
    rows = []
    for block in re.findall(r"<article\b.*?</article>", body, re.S | re.I):
        link = re.search(r'href="(https?://[^"]+/series/[^"]+)"', block)
        if not link:
            continue
        title = (re.search(r'<h2[^>]*>.*?<a[^>]*>(.*?)</a>', block, re.S)
                 or re.search(r'title="([^"]+)"', block))
        cover = re.search(r'<img[^>]+(?:data-src|src)="([^"]+)"', block)
        if title:
            rows.append(_row(site, title.group(1), link.group(1),
                             cover.group(1) if cover else ""))
    return rows


def _kol_search(site, query, deadline):
    url = f"{site['base']}/?s={urllib.parse.quote(query)}"
    return _kol_cards(site, _get(url, deadline))


def _kol_browse(site, page, deadline):
    url = f"{site['base']}/series/?order=popular&page={int(page)}"
    return _kol_cards(site, _get(url, deadline))


def _kol_details(site, entry, deadline):
    body = _get(entry["url"], deadline, keep=True)
    links = [{"name": genre_in_english(_text(name)), "url": href}
             for href, name in re.findall(
                 r'<a[^>]+href="([^"]*/genre/[^"]*)"[^>]*rel="tag"[^>]*>(.*?)</a>',
                 body, re.S)]
    genres = [link["name"] for link in links]
    summary = ""
    found = re.search(r'class="[^"]*\bsersys\b[^"]*"[^>]*>(.*?)</div>', body, re.S)
    if found:
        summary = "\n\n".join(_paragraphs(found.group(1)))
    cover = re.search(r'class="sertothumb".*?<img[^>]+src="([^"]+)"', body, re.S)
    return {"summary": summary or _meta(body, "og:description"),
            "genres": genres, "genre_links": links,
            "cover_url": cover.group(1) if cover else _meta(body, "og:image")}


def _kol_chapters(site, entry, deadline):
    body = _get(entry["url"], deadline, keep=True)
    lister = body[body.find("eplister"):]
    rows = []
    for item in re.findall(r"<li\b[^>]*data-ID=\"\d+\"[^>]*>(.*?)</li>", lister, re.S | re.I):
        link = re.search(r'href="([^"]+)"', item)
        if not link:
            continue
        num = re.search(r'class="epl-num"[^>]*>(.*?)</div>', item, re.S)
        name = re.search(r'class="epl-title"[^>]*>(.*?)</div>\s*</div>', item, re.S)
        rows.append((link.group(1), _text(num.group(1)) if num else "",
                     _text(name.group(1)) if name else ""))
    # Newest first on the page; numbered from the oldest.
    return [(url, " - ".join(p for p in (num, name) if p)) for url, num, name in rows][::-1]


# Kolnovel salts every chapter with decoy paragraphs: copies of other
# lines, under random class names that a <style> block in the same page
# hides (`height:0.1px; overflow:hidden; position:fixed; opacity:0;
# text-indent:-99999px; bottom:-999px`). Measured on The Beginning After
# the End, ch. 532 extra 1: 9 of the page's 18 paragraph classes are
# hidden that way, and a plain scrape repeats whole sentences mid-scene.
# So the hidden classes are read out of the page's own CSS, never listed
# here - they change from page to page.
_HIDING = re.compile(r"opacity\s*:\s*0(?![.\d])|text-indent\s*:\s*-\d{3,}"
                     r"|height\s*:\s*0?\.\d+px|display\s*:\s*none", re.I)


def _hidden_classes(body) -> set:
    hidden = set()
    for css in re.findall(r"<style[^>]*>(.*?)</style>", body, re.S | re.I):
        for selectors, rules in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if _HIDING.search(rules):
                hidden.update(re.findall(r"\.([A-Za-z_][\w-]*)", selectors))
    return hidden


def _kol_text(site, chapter, deadline):
    body = _get(chapter["url"], deadline)
    segment = _until_div(body, 'id="kol_content"')
    hidden = _hidden_classes(body)
    lines = []
    for piece in re.split(r"(?=<p\b)", segment, flags=re.I):
        cls = re.match(r"<p\b[^>]*class=['\"]([^'\"]+)['\"]", piece, re.I)
        if cls and set(cls.group(1).split()) & hidden:
            continue
        lines.extend(_paragraphs(piece))
    return lines


# ---------------------------------------------------------------- rewayat
#
# rewayat.club is a Nuxt front end over a public Django REST API at
# api.rewayat.club - keyless, JSON, and the whole chapter list in one
# call at `/chapters/<slug>/all/` (the paged list is 24 a page, 61 pages
# for Lord of the Mysteries; `page_size=2000` works too and took 10.2s
# against 0.8s for `/all/`). `ordering=` is ignored by `/novels/`, so its
# default order - which leads with the home page's popular titles - is
# the browse.

def _rewayat_rows(site, results) -> list:
    rows = []
    for item in results or []:
        slug = str(item.get("slug") or "")
        if not slug:
            continue
        title = item.get("arabic") or item.get("english") or slug
        rows.append(_row(site, title, f"{site['base']}/novel/{slug}",
                         str(item.get("poster_url") or ""),
                         alt_titles=[t for t in (item.get("english"),) if t],
                         summary=_text(item.get("about")),
                         genres=[g.get("english") or genre_in_english(g.get("arabic"))
                                 for g in item.get("genre") or [] if isinstance(g, dict)],
                         genre_links=[{"name": g.get("english") or genre_in_english(g.get("arabic")),
                                       "url": f"{_REWAYAT_API}/novels/?genre={g.get('id')}"}
                                      for g in item.get("genre") or []
                                      if isinstance(g, dict) and g.get("id") is not None]))
    return rows


def _rewayat_slug(entry) -> str:
    parts = urllib.parse.urlsplit(str(entry.get("url") or "")).path.strip("/").split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "novel" else ""


def _rewayat_search(site, query, deadline):
    data = _get_json(f"{_REWAYAT_API}/novels/?search={urllib.parse.quote(query)}",
                     deadline)
    return _rewayat_rows(site, data.get("results"))


def _rewayat_browse(site, page, deadline):
    data = _get_json(f"{_REWAYAT_API}/novels/?page={int(page)}", deadline)
    return _rewayat_rows(site, data.get("results"))


def _rewayat_details(site, entry, deadline):
    slug = _rewayat_slug(entry)
    if not slug:
        return {}
    item = _get_json(f"{_REWAYAT_API}/novels/{slug}/", deadline)
    row = (_rewayat_rows(site, [item]) or [{}])[0]
    return {"summary": row.get("summary", ""), "genres": row.get("genres", []),
            "genre_links": row.get("genre_links", []),
            "cover_url": row.get("cover_url", "")}


def _rewayat_chapters(site, entry, deadline):
    slug = _rewayat_slug(entry)
    if not slug:
        return []
    listed = _get_json(f"{_REWAYAT_API}/chapters/{slug}/all/", deadline)
    rows = []
    for item in listed if isinstance(listed, list) else []:
        try:
            number = int(item.get("number"))
        except (TypeError, ValueError):
            continue
        if number >= _REWAYAT_NOT_A_CHAPTER:
            continue
        name = _text(item.get("title"))
        rows.append((number, f"{site['base']}/novel/{slug}/{number}",
                     f"الفصل {number}" + (f" - {name}" if name else "")))
    rows.sort()
    return [(url, label) for _n, url, label in rows]


def _rewayat_text(site, chapter, deadline):
    path = urllib.parse.urlsplit(chapter["url"]).path.strip("/").split("/")
    if len(path) < 3:
        return []
    data = _get_json(f"{_REWAYAT_API}/chapters/{path[1]}/{path[2]}/", deadline)
    lines = []

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, str):
            lines.extend(_paragraphs(node))

    walk(data.get("content"))
    # Its translators separate scenes with runs of single-"." paragraphs
    # (ten in a row at the head of LotM 1418) - one break is the meaning.
    out = []
    for line in lines:
        if not re.search(r"\w", line):
            if out and out[-1] != "* * *":
                out.append("* * *")
            continue
        out.append(line)
    while out and out[0] == "* * *":
        out.pop(0)
    return out


# ---------------------------------------------------------- readnovelfull
#
# The "novelfull" engine. A list row's picture is a 200x89 crop
# (`/thumb/t-200x89/`) - unusable on a 160x216 card - and the same file
# is served at `/thumb/t-300x439/`, the size the novel page's own
# og:image names: six of six titles answered, cold.

def _rnf_cover(url) -> str:
    return str(url or "").replace("/thumb/t-200x89/", "/thumb/t-300x439/")


def _rnf_cards(site, body) -> list:
    rows = []
    for block in body.split('<div class="row">')[1:]:
        # `\s+`, not a space: the most-popular list breaks the tag
        # across lines before `title=` on page 1 and not on page 2.
        link = re.search(r'<h3 class="novel-title"><a\s+href="([^"]+)"\s+title="([^"]*)"', block)
        if not link:
            continue
        cover = re.search(r'<img[^>]+src="([^"]+)"[^>]*class="cover"', block)
        rows.append(_row(site, link.group(2), _absolute(link.group(1), site["base"]),
                         _rnf_cover(cover.group(1)) if cover else ""))
    return rows


def _rnf_search(site, query, deadline):
    url = f"{site['base']}/novel-list/search?keyword={urllib.parse.quote_plus(query)}"
    return _rnf_cards(site, _get(url, deadline))


def _rnf_browse(site, page, deadline):
    url = f"{site['base']}/novel-list/most-popular-novel?page={int(page)}"
    return _rnf_cards(site, _get(url, deadline))


def _rnf_details(site, entry, deadline):
    body = _get(entry["url"], deadline, keep=True)
    desc = re.search(r'class="desc-text"[^>]*>(.*?)</div>', body, re.S)
    # The heading, not the bare word: "Genre:" first appears inside the
    # page's meta description, whose list ends at the first </li> of the
    # navigation and named no genre links at all.
    genres_at = body.find("<h3>Genre:</h3>")
    links = []
    if genres_at > 0:
        links = [{"name": _text(name), "url": _absolute(href, site["base"])}
                 for href, name in re.findall(
                     r'<a[^>]+href="(/genres/[^"]*)"[^>]*>(.*?)</a>',
                     body[genres_at:body.find("</li>", genres_at)])]
    return {"summary": "\n\n".join(_paragraphs(desc.group(1))) if desc
            else _meta(body, "og:description"),
            "genres": [link["name"] for link in links], "genre_links": links,
            "cover_url": _meta(body, "og:image")}


def _rnf_chapters(site, entry, deadline):
    body = _get(entry["url"], deadline, keep=True)
    found = re.search(r'data-novel-id="(\d+)"', body)
    if not found:
        return []
    listing = _get(f"{site['base']}/ajax/chapter-archive?novelId={found.group(1)}",
                   deadline, referer=entry["url"])
    rows, seen = [], set()
    for href, title in re.findall(r'<a\s+href="([^"]+)"\s+title="([^"]*)"', listing):
        url = _absolute(href, site["base"])
        if url in seen:
            continue
        seen.add(url)
        rows.append((url, _text(title)))
    return rows                                   # already oldest first


def _rnf_text(site, chapter, deadline):
    segment = _until_div(_get(chapter["url"], deadline), 'id="chr-content"')
    # The chapter's own heading sits in an <h3> ahead of the text; the
    # reader's bar already names the chapter.
    segment = re.sub(r"<h3\b.*?</h3>", "", segment, flags=re.S | re.I)
    return _paragraphs(segment)


# --------------------------------------------------------------- novelfire
#
# The successor of LightNovelPub / LightNovelWorld. Its chapter list is
# 100 a page (`/book/<slug>/chapters?page=N`, 33 pages for Shadow Slave)
# and its chapter addresses are `/book/<slug>/chapter-<N>`, so the list
# page says how many there are ("A total of 3210 chapters") and every
# number up to that has an address whether or not its page of titles
# was read.

def _nf_cards(site, body) -> list:
    rows = []
    for block in re.findall(r'<li class="novel-item">(.*?)</li>', body, re.S):
        link = re.search(r'<a[^>]+title="([^"]*)"[^>]+href="([^"]*/book/[^"]+)"', block)
        if not link:
            continue
        cover = (re.search(r'<img[^>]+data-src="([^"]+)"', block)
                 or re.search(r'<img[^>]+src="(?!data:)([^"]+)"', block))
        rows.append(_row(site, link.group(1), _absolute(link.group(2), site["base"]),
                         _absolute(cover.group(1), site["base"]) if cover else ""))
    return rows


def _nf_search(site, query, deadline):
    url = f"{site['base']}/search?keyword={urllib.parse.quote_plus(query)}"
    return _nf_cards(site, _get(url, deadline))


def _nf_browse(site, page, deadline):
    url = f"{site['base']}/genre-all/sort-popular/status-all/all-novel?page={int(page)}"
    return _nf_cards(site, _get(url, deadline))


def _nf_details(site, entry, deadline):
    body = _get(entry["url"], deadline, keep=True)
    summary = re.search(r'class="summary".*?class="content[^"]*"[^>]*>(.*?)</div>', body, re.S)
    links = [{"name": _text(name),
              "url": _absolute(href.replace("/sort-new/", "/sort-popular/"), site["base"])}
             for href, name in re.findall(
                 r'<a[^>]+href="([^"]*/genre-[^"/]+/[^"]*)"[^>]*class="property-item"[^>]*>(.*?)</a>',
                 body, re.S)]
    genres = [link["name"] for link in links]
    lines = _paragraphs(summary.group(1)) if summary else []
    # Its own expander rides along at the end - a row of dots and "Show
    # More" (photographed on Kill the Sun's details page).
    lines = [line for line in lines if not re.fullmatch(r"[.\s…]*Show More", line)]
    return {"summary": "\n\n".join(lines) if lines
            else _meta(body, "og:description"),
            "genres": genres, "genre_links": links,
            "cover_url": _absolute(_meta(body, "og:image"), site["base"])}


_NF_PAGES_AT_ONCE = 6


def _nf_chapters(site, entry, deadline, partial=None):
    """`partial(rows)` is handed the whole numbered list as soon as the
    first page says how long it is - measured on Shadow Slave, the full
    list of titles is 33 pages and 9.8s, the first page 0.6s - and the
    titles fill in when the rest of the pages have answered."""
    base_url = entry["url"].rstrip("/")
    first = _get(base_url + "/chapters", deadline)
    total = re.search(r"A total of (\d+) chapters", first)
    pages = [int(n) for n in re.findall(r"chapters\?page=(\d+)", first)]
    titles = {}

    def harvest(body):
        for number, title in re.findall(
                r'/book/[^/"]+/chapter-(\d+)"[^>]*title="([^"]*)"', body):
            titles.setdefault(int(number), _text(title))

    def listed():
        count = int(total.group(1)) if total else max(titles or [0])
        # Every number up to the stated total, a missed page's included -
        # a hole in the middle of a list is worse than a row named only
        # by its number (the chapter_source note on olympustaff's
        # paginator).
        return [(f"{base_url}/chapter-{n}", titles.get(n) or f"Chapter {n}")
                for n in range(1, count + 1)]

    harvest(first)
    more = range(2, (max(pages) if pages else 1) + 1)
    if more and partial is not None:
        try:
            partial(listed())
        except Exception:
            pass
    if more:
        with concurrent.futures.ThreadPoolExecutor(_NF_PAGES_AT_ONCE) as pool:
            futures = [pool.submit(_get, f"{base_url}/chapters?page={n}", deadline)
                       for n in more]
            for future in futures:
                try:
                    harvest(future.result())
                except Exception:
                    pass        # its numbers are still filled in below
    return listed()


def _nf_text(site, chapter, deadline):
    return _paragraphs(_until_div(_get(chapter["url"], deadline), 'id="content"'))


# ------------------------------------------------------------ the shape

_OPS = {
    "kolnovel": (_kol_search, _kol_browse, _kol_details, _kol_chapters, _kol_text),
    "rewayat": (_rewayat_search, _rewayat_browse, _rewayat_details,
                _rewayat_chapters, _rewayat_text),
    "readnovelfull": (_rnf_search, _rnf_browse, _rnf_details, _rnf_chapters, _rnf_text),
    "novelfire": (_nf_search, _nf_browse, _nf_details, _nf_chapters, _nf_text),
}


def _interleave(lists) -> list:
    """One from each site in turn, so the first screenful holds both
    languages rather than one site's whole page and then the next."""
    out, seen = [], set()
    longest = max((len(rows) for rows in lists), default=0)
    for i in range(longest):
        for rows in lists:
            if i < len(rows):
                key = (rows[i]["site_id"], rows[i]["url"])
                if key not in seen:
                    seen.add(key)
                    out.append(rows[i])
    return out


def _each_site(op_index, args, budget, what):
    """op `op_index` on every site at once, under one deadline. Returns
    one list per site, in SITES order; a site that fails or runs out of
    time is an empty list and a log line."""
    deadline = net.deadline_in(budget)
    results = {}

    def one(site):
        started = time.perf_counter()
        try:
            rows = _OPS[site["id"]][op_index](site, *args, deadline) or []
        except Exception as exc:
            rows = []
            logs.info(f"novels: {what} on {site['id']} failed: {net.why(exc)}")
        else:
            logs.info(f"novels: {what} on {site['id']} "
                      f"ms={int((time.perf_counter() - started) * 1000)} rows={len(rows)}")
        return rows

    with concurrent.futures.ThreadPoolExecutor(len(SITES), thread_name_prefix="novels") as pool:
        futures = {site["id"]: pool.submit(one, site) for site in SITES}
        for site_id, future in futures.items():
            try:
                results[site_id] = future.result(
                    timeout=max(0.1, deadline - time.monotonic() + 1.0))
            except Exception:
                results[site_id] = []
    return [results.get(site["id"]) or [] for site in SITES]


SEARCH_BUDGET_S = 8.0
BROWSE_BUDGET_S = 10.0
SEARCH_LIMIT = 40


def search_all(query, budget=SEARCH_BUDGET_S) -> list:
    query = str(query or "").strip()
    if not query:
        return []
    return _interleave(_each_site(0, (query,), budget, "search"))[:SEARCH_LIMIT]


CACHE_FILE = "novels_cache.json"


def cached_rows() -> list:
    """The last first page of the catalogue, from disk. No network."""
    try:
        block = storage.load(CACHE_FILE, {})
    except Exception:
        return []
    rows = block.get("rows") if isinstance(block, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("title")]


def browse(page=1, budget=BROWSE_BUDGET_S) -> list:
    """Page `page` of every site's popular list, interleaved. Page 1 is
    written to CACHE_FILE so the next visit draws it at once (rule 7) -
    merged over what was there, so a site that failed this time does
    not empty its share of the grid."""
    rows = _interleave(_each_site(1, (int(page),), budget, f"browse p{page}"))
    if int(page) == 1 and rows:
        try:
            fresh = {(r["site_id"], r["url"]) for r in rows}
            kept = [r for r in cached_rows() if (r.get("site_id"), r.get("url")) not in fresh]
            storage.save(CACHE_FILE, {"at": time.time(), "rows": rows + kept})
        except Exception:
            logs.exception("novels: could not write the catalogue cache")
    return rows


def details(entry, budget=12.0) -> dict:
    """{"summary", "genres", "cover_url"} for one novel, or {}."""
    site = site_for(entry)
    if site is None or not (entry or {}).get("url"):
        return {}
    try:
        return _OPS[site["id"]][2](site, dict(entry), net.deadline_in(budget)) or {}
    except Exception as exc:
        logs.info(f"novels: details on {site['id']} failed: {net.why(exc)}")
        return {}


def _shaped(site, listed) -> list:
    out = [{"number": index, "label": label or f"Chapter {index}",
            "title": label, "url": url, "source": "novel", "site_id": site["id"]}
           for index, (url, label) in enumerate(listed, start=1)]
    return out[::-1]


def chapters(entry, deadline=None, on_partial=None) -> list:
    """Chapters newest first, in chapter_source's shape.

    **Numbered by position from the first chapter, not by the site's own
    numbers.** Those are not an identity on any of the four: kolnovel's
    are volume-relative ("Volume 12.5 extra ... chapter 532" sits 25
    places after chapter 532), rewayat mixes announcement posts in with
    eight-digit numbers, and ReadNovelFull/Novel Fire titles are free
    text. A position is unique, sorts, and is stable while a site only
    appends - which is what all four do. The site's own words are the
    row's `label`, so the list reads as the site does."""
    site = site_for(entry)
    if site is None or not (entry or {}).get("url"):
        return []
    if deadline is None:
        deadline = net.deadline_in(30)
    started = time.perf_counter()
    op = _OPS[site["id"]][3]
    try:
        if site["id"] == "novelfire" and on_partial is not None:
            listed = op(site, dict(entry), deadline,
                        partial=lambda rows: on_partial(_shaped(site, rows))) or []
        else:
            listed = op(site, dict(entry), deadline) or []
    except Exception as exc:
        logs.info(f"novels: chapters on {site['id']} failed: {net.why(exc)}")
        return []
    out = _shaped(site, listed)
    logs.info(f"novels: chapters on {site['id']} "
              f"ms={int((time.perf_counter() - started) * 1000)} found={len(out)}")
    return out


# Chapter texts read lately, by url - see prefetch.
_TEXTS_MAX = 24
_texts = {}
_texts_lock = threading.Lock()
_fetching = set()


def prefetch(chapter):
    """Read `chapter`'s text into memory behind the one on screen, so
    Next Chapter answers from memory. Measured on the frozen build, 10
    October 2026: one Novel Fire chapter took 5.6s to arrive (0.6-3.7s in
    the earlier runs), every press of Next paying it again. One daemon
    thread per chapter, never two for the same one; never raises."""
    url = str((chapter or {}).get("url") or "")
    with _texts_lock:
        if not url or url in _texts or url in _fetching:
            return
        _fetching.add(url)

    def run():
        try:
            chapter_text(chapter)
        except Exception:
            pass
        finally:
            with _texts_lock:
                _fetching.discard(url)

    threading.Thread(target=run, name="novels-prefetch", daemon=True).start()


def chapter_text(chapter, entry=None, deadline=None) -> dict:
    """{"text": [paragraphs], "dir": "rtl"|"ltr", "reason": ...}."""
    chapter = chapter or {}
    site = _BY_ID.get(str(chapter.get("site_id") or "")) or site_for({"url": chapter.get("url")})
    if site is None or not str(chapter.get("url") or "").startswith("http"):
        return {"text": [], "dir": "ltr", "reason": "empty"}
    with _texts_lock:
        kept = _texts.get(chapter["url"])
    if kept:
        return dict(kept)
    if deadline is None:
        deadline = net.deadline_in(20)
    started = time.perf_counter()
    try:
        lines = _OPS[site["id"]][4](site, chapter, deadline) or []
    except Exception as exc:
        logs.info(f"novels: chapter text on {site['id']} failed: {net.why(exc)}")
        return {"text": [], "dir": "rtl" if site["lang"] == "ar" else "ltr",
                "reason": "unreachable"}
    logs.info(f"novels: chapter text on {site['id']} "
              f"ms={int((time.perf_counter() - started) * 1000)} paragraphs={len(lines)}")
    answer = {"text": lines, "dir": "rtl" if site["lang"] == "ar" else "ltr",
              "reason": "" if lines else "empty"}
    if lines:
        with _texts_lock:
            _texts[chapter["url"]] = answer
            while len(_texts) > _TEXTS_MAX:
                _texts.pop(next(iter(_texts)), None)
    return dict(answer)


WARM_MAX_AGE_S = 6 * 3600


def warm_async(max_age_s=WARM_MAX_AGE_S):
    """Fill CACHE_FILE behind the launch when it is missing or old, so
    the Novels page draws a grid on its first visit rather than waiting
    on four sites (measured 4.2-6.4s for the first page). One daemon
    thread, nothing waits on it, never raises."""
    try:
        block = storage.load(CACHE_FILE, {})
        at = float(block.get("at") or 0) if isinstance(block, dict) else 0.0
    except Exception:
        at = 0.0
    if cached_rows() and time.time() - at < max_age_s:
        return

    def run():
        try:
            browse(1)
        except Exception:
            logs.exception("novels: the launch warm failed")

    threading.Thread(target=run, name="novels-warm", daemon=True).start()


def image_headers(url) -> dict:
    """What a novel's cover must be fetched with. Measured 10 October
    2026 on the frozen build: every Novel Fire card drew blank, its log
    reading `image fetch failed for novelfire.net: HTTPError`, because
    the proxy asked with urllib's own User-Agent - the same file answers
    35,382 bytes to a browser one (MangaDex's uploads host does the same,
    integrations.md). The site as Referer as well, for the hotlink
    guards these hosts tend to grow."""
    site = site_for({"url": url}) or {}
    head = {"User-Agent": _UA}
    if site.get("base"):
        head["Referer"] = site["base"] + "/"
    return head


def _genre_page_url(site_id, url, page) -> str:
    """Page `page` of one site's genre listing - each site pages its own
    way (measured 10 October 2026: kolnovel `/genre/<slug>/page/N/`,
    ReadNovelFull and Novel Fire `?page=N`, rewayat's API `&page=N`)."""
    page = max(1, int(page))
    if page == 1:
        return url
    if site_id == "kolnovel":
        return url.rstrip("/") + f"/page/{page}/"
    join = "&" if "?" in url else "?"
    return f"{url}{join}page={page}"


def genre_page(site_id, url, page=1, budget=BROWSE_BUDGET_S) -> list:
    """One page of a site's genre listing, as catalogue rows. The genre
    chips on a novel's details page open this - a genre is the site's own
    word for it, so it is browsed on that site, not across all four."""
    site = _BY_ID.get(str(site_id or ""))
    if site is None or not str(url or "").startswith("http"):
        return []
    deadline = net.deadline_in(budget)
    target = _genre_page_url(site["id"], url, page)
    try:
        if site["id"] == "rewayat":
            return _rewayat_rows(site, _get_json(target, deadline).get("results"))
        parse = {"kolnovel": _kol_cards, "readnovelfull": _rnf_cards,
                 "novelfire": _nf_cards}[site["id"]]
        return parse(site, _get(target, deadline))
    except Exception as exc:
        logs.info(f"novels: genre page on {site['id']} failed: {net.why(exc)}")
        return []
