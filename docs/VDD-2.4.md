# Atomic — Version Description Document

**Version 2.4** · 12 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.4 |
| Release date | 2026-09-12 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — a **GitHub release asset**, holding one self-contained `Atomic.exe` |
| Also at the tag | `Atomic.exe` / `Atomic.zip` — the ~11 MB **bridge installer**, for installs predating 2.0 (§3, §8) |

---

## 2. System overview

Unchanged from 2.0 — see `docs/VDD-2.0.md` §2. A single-window Windows
desktop application over one person's anime, reading, films, series,
games, applications and websites, with a native video player (libmpv)
over its own torrent engine (libtorrent) and debrid support, a chapter
reader, a download queue, and a locally served web UI (WebView2).

**2.4 is one visible change**: a cast member's page is now headed by
that person, not only by their name.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,535,961 bytes**. SHA-256 `fef46a3369ace0e433af83db838067bc1681a5e27ee912eb88aba89c43997da1` |
| `Atomic.exe` (inside that zip) | **126,313,498 bytes**. SHA-256 `fd26277904da0af251761a3f1634e9cc938fa21d26164a41ea46516cc56f5dc9` |
| `Atomic.exe` (committed at `v2.4`) | The **bridge installer**, 11,101,866 bytes. SHA-256 `d0d5f156e4ea896e0d5973a2fff7c8b0fa3282bf9bece76c26c28565ef7d9668` |
| `Atomic.zip` (committed at `v2.4`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **127,156 Python lines** across 126 modules, plus 5,994 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `docs/VDD-2.4.md` | This document |

One commit stands between `released/2.3` and this release.

The bridge is unchanged in purpose and rebuilt from the same source: it
resolves the newest release carrying an `Atomic.zip` asset **at run
time**, walking `/releases`, so the copy committed at `v2.4` will find
2.5 and everything after it without being rebuilt.

---

## 4. What this version provides

**A cast member's page opens with their own picture.** Pressing a name
under CAST on a title's page — or a face on Discover, a search or the
search suggestions — used to open a grid headed by the name alone, in
the same small dim type the genre page uses. The page now opens with
that person's portrait, round and centred above the name, with the
name as the page's heading and the number of titles under it; the
Anime / Series / Movies tabs centre with them. A name the picture
source has nothing for keeps exactly the old heading rather than
showing an empty circle, and the genre page — which shares this
header — is untouched.

---

## 5. Design notes

**The picture costs no request.** `helpers/people._person_id` already
fetches TMDB's `search/person` to find the person behind a name, and
that answer carries `profile_path`; the portrait is taken from the row
the id came from and remembered under the name as it was clicked
(`_PORTRAITS`, `PORTRAIT_SIZE` = `w342`). Every other path that parses
a person row fills the same map for free — `_face`, so a face card
Discover or a search has drawn this session answers without asking
anything.

So the only name that pays a request of its own is one whose
filmography was written to `people_cache.json` by a build that had no
notion of portraits. Measured on this machine: **421 ms**, once, and
the answer is then written into that name's own cache entry beside its
rows. `people.prefetch`, which the details page already calls as it
draws its chips, now warms a portrait for a name whose list is cached
too — so that one request is paid behind the details page rather than
inside the cast route's answer. Of the 88 names in the owner's cache, 6
were written by this build and 82 will each pay it once.

**w342, not the `w185` a card uses.** The header draws the picture at
132 CSS px, which is 165 device pixels on his 125 % panel, and a round
crop spends the whole width; `w185` would have been the one upscaled
picture on a page of correctly sized ones. Measured on the frozen
build's screenshot: **165 device pixels**, centred on the page to
within a pixel.

**The circle is reserved before the picture arrives.** The header is
drawn with the portrait's box at its final size (`width`/`height` on
the element as well as in the stylesheet), so nothing below it moves
when the picture decodes — photographed at 0.6 s with the circle empty
and the name, tabs, count and every card title already in place, and
again at 2.6 s with the picture in. This is CLAUDE.md rule 7's answer:
draw what there is, fill the rest in.

---

## 6. Configuration and user data

Unchanged. The portrait is kept in `people_cache.json`, which already
held each name's filmography, as one extra field per entry; an entry
written by an older build simply lacks it and is filled in on first
use. No new file, no migration.

---

## 7. External interfaces

Unchanged from 2.3. No host is added: the portrait comes from the same
TMDB `search/person` answer the cast page already depended on, and is
served to the page through the app's own image proxy like every other
remote picture.

---

## 8. Installation and removal

Unchanged — `docs/VDD-2.0.md` §9. An install running 2.0 or later
updates in place from the release asset; an install running 1.10 or
older is handed the bridge committed at the tag, which fetches the
asset.

---

## 9. Known limitations

Carried forward from 2.0 §10 and 2.3 §9. Two that belong to this
version:

- **A name TMDB has no portrait for gets no picture**, and the page
  reads as it did before. There is no second source: the app asks TMDB
  for people because it already carries a read token for artwork, and
  no keyless alternative was measured for this release.
- **The 82 cached names written by earlier builds** each pay one
  `search/person` the first time their page is opened, unless the
  details page's prefetch got there first — which it does whenever the
  name was reached by pressing its chip.
- Code signing (roadmap #8) is still open; the release is unsigned, and
  shipping the zip rather than the bare exe is what makes the download
  come through.

---

## 10. Verification performed

Method: `.claude/rules/testing.md` — measure, fix, read the archive
back, drive the frozen exe from outside, photograph it.

**In process**, against an empty temporary data directory so nothing
touched his files, with a real TMDB token:

| | |
|---|---|
| `/api/cast` cold (two TMDB requests, as before this change) | 446–812 ms |
| the same route again | 2–3 ms |
| portrait for a legacy cache entry (one `search/person`) | 421 ms, once, then 0 ms |
| portrait for a name a face card had drawn | 0 ms, no request |
| a name TMDB has no picture of (`Kyley Statham`) | `face=""` — the old heading |
| the proxied portrait | 342×512 JPEG, 30,025 bytes, decodes |

**On the frozen build** (2.4's own binary, a copy of his real
`%APPDATA%\Atomic` taken with `copy_real_data.py` from outside the
desktop app's package, his own maximised 2578×1398 geometry):

| Cast page | Route | Portrait |
|---|---|---|
| Johnny Yong Bosch, 38 titles | `ms=12` | in, round, centred |
| Alan Ritchson, 55 titles | `ms=14` | in, round, centred |
| Taito Ban, 74 titles — a legacy cache entry | `ms=14` | fetched by the details page's prefetch, written back to disk |

No `web route slow` line for `/api/cast`, and no errors in the log.
**Photographed**: each page at 0.6 s (circle reserved, everything else
drawn) and at 2.6 s (picture in); the portrait measured at 165 device
pixels; and a **genre page as the control**, whose header is identical
to 2.3's.

**Read back out of the frozen archive**: `helpers.people` carries
`portrait`, `_PORTRAITS`, `_portrait_url` and `PORTRAIT_SIZE`;
`web.server` carries the `face` key and the `portrait` lookup;
`static/app.js` and `static/app.css` carry `pface` and `withface`;
`helpers.whats_new` carries the 2.4 note; `APP_VERSION` reads **2.4**
in both `helpers.updater` and `helpers.development_version_patch`.

**Not exercised**: a name with no portrait on the frozen build — only
in process, since one cannot be arranged on demand from the app's own
surfaces; and his laptop, the second machine these reports usually come
from.

**Defender**: `MpCmdRun -Scan -ScanType 3` over the release exe twice
and over the zip once, clean every time (`WinDefend` running,
`-DisableRemediation`).

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
