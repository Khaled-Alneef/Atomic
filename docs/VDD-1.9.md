# Atomic — Version Description Document

**Version 1.9** · 18 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.9 |
| Release date | 2026-08-18 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.exe` — a single self-contained executable |

---

## 2. System overview

Unchanged from VDD-1.2 §2.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.exe` | The application. 47,786,140 bytes. SHA-256 `1b734b7b4307fa3a4508066bb82e1937eb49769eede6d939a29703d3e2470a77` |
| `src/` | Full Python source, 16,191 lines across 38 modules (16,122 across 38 at 1.8) |
| `packaging/` | `build.py`, `Atomic.spec` and `check_release_notes.py` |
| `docs/VDD-1.9.md` | This document |

Build environment unchanged from VDD-1.1 §3.

One commit stands between 1.8 and this release, taken from `development`
as a single snapshot. No development builds sit in between: the fix was
tested from a local build and released directly, so 1.8.1 was never
numbered.

---

## 4. Inventory of software contents

| Module changed | Lines | What changed |
|---|---|---|
| `windows/tracker.py` | 3,086 | `_video_sites_for`; the Video Website dropdown and the suggestion fan-out filtered through it |
| `helpers/anime_sites.py` | 1,116 | `_ANIME_ONLY_HOSTS`, `_is_anime_only`, `anime_only_site_ids` |
| `helpers/whats_new.py` | 346 | 1.9's note |
| `helpers/updater.py` | 272 | `APP_VERSION` |

Nothing else in `src/` differs from 1.8.

---

## 5. What this version provides

### Crunchyroll is no longer offered for films and series

Typing a film's name on the Movies & Series page listed a Crunchyroll
suggestion for it — measured on *Interstellar*, which produced
"Interstellar — Movie (Crunchyroll)" alongside the Stremio and Netflix
rows. Picking it was worse than useless: the entry saved with
Crunchyroll as its Video Website, a page-url resolution ran against a
service that has never carried the title, found nothing, and the card
opened Crunchyroll's search results for a film that is not in its
catalogue. Crunchyroll carries anime and anime films only.

The Video Website dropdown offered it there for the same reason — the
list of video websites is shared by the Anime page and the Movies &
Series page, and nothing in it recorded what a given service actually
holds.

Now a site can be anime-only, and the two places that offer a video
website leave those sites out for a Series or a Movie. Netflix is
deliberately not anime-only: it carries films and series, which is why
it was added alongside Crunchyroll at 1.3 in the first place. The Anime
page is unchanged in every respect.

Two deliberate limits on the rule:

* **A site the user added is never filtered.** Only the services this
  app resolves itself can be classified; nothing here can know what
  someone else's site holds, so an unknown site stays offered
  everywhere.
* **An entry already saved on Crunchyroll keeps it.** Reopening such an
  entry still lists and selects Crunchyroll, rather than the dropdown
  silently reassigning it to Stremio behind the user's back. Only new
  choices are constrained.

---

## 6. Design notes

**Why the filter is applied per search result, not per dialog.** A
Movies & Series search asks both Cinemeta catalogs (series and movie),
and each hit is already tagged with the type its catalog answered for —
that is what lets a film picked while the Type box says Series set the
type to Movie. Filtering on the Type box instead would classify a hit by
whatever the dropdown happened to say when the results landed, which is
not necessarily what that hit is. Both branches happen to give the same
answer today, because no page offers Anime alongside Series; taking the
hit's own type costs nothing and does not depend on that staying true.

**Why the classification lives in `anime_sites` and not in the tracker.**
It is a fact about a service, not about a page — the same fact
`streaming_provider` already publishes for the "this entry cannot sync"
hint. Reaching into `_streaming_site_for` from a page would be reaching
into a private, and a second host table in `tracker.py` would be a copy
to keep in step.

**Why a host list rather than a row on `_STREAMING_SITES`.** Anime-only
is not a property of *resolving through a third party* — the two tables
would agree only by coincidence. A user-added anime-only site could join
`_ANIME_ONLY_HOSTS` without implying anything about how its pages are
found, and Netflix sits in `_STREAMING_SITES` while carrying everything.

---

## 7. Configuration and user data

Unchanged from VDD-1.8 §7. No stored field changed: an entry's
`site_id` is still whatever site it was saved with, including a
Crunchyroll one saved before this version.

---

## 8. External interfaces

Unchanged from VDD-1.8 §8. No new service is contacted; this release
removes requests rather than adding them — a film no longer sends a
Crunchyroll page-url resolution (a Wikidata query and, failing that, an
AniList query) that could not succeed.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

In addition to VDD-1.8 §10, including the antivirus note (the durable
remedy remains a code-signing certificate, and nothing has been bought):

- **Only Crunchyroll is classified.** Any other anime-only service — a
  site the owner adds by hand — is still offered for films and series,
  because nothing in the app can tell what it carries. Adding one is a
  row in `_ANIME_ONLY_HOSTS`, not new code.
- **An anime film tracked on the Movies & Series page as a Movie will
  not be offered Crunchyroll**, even though Crunchyroll may well carry
  it. The rule is by entry type, not by whether the title is animated;
  anime films belong on the Anime page, where nothing is filtered.
- **Entries saved on Crunchyroll before this version are untouched.**
  A film pinned to Crunchyroll under 1.8 keeps opening that site's
  search results until it is repointed by hand.

---

## 11. Verification performed

In addition to VDD-1.0 §11 through VDD-1.8 §11:

- **Measured offscreen against a copy of the owner's real data**, with
  `storage.DATA_DIR` redirected to a temp copy (never the live
  `%APPDATA%\Atomic`), driving the real `EntryForm` rather than the
  filter function alone. With the owner's two configured video websites
  (Crunchyroll, Netflix): the Series dropdown and the Movie dropdown
  both read *Stremio, Netflix*; an *Interstellar* search fanned out to
  *Interstellar — Movie (Stremio)* and *Interstellar — Movie (Netflix)*
  only; the Anime dropdown and an anime search were unchanged at
  *Stremio, Crunchyroll, Netflix*; and an existing Series entry saved on
  Crunchyroll still listed and selected it. `anime_only_site_ids()`
  returned exactly the Crunchyroll id, with Netflix excluded.
- **The shipped code was read back out of the frozen executable**, not
  out of the source tree. `helpers.anime_sites` in the bundled PYZ
  carries `_ANIME_ONLY_HOSTS`, `_is_anime_only` and
  `anime_only_site_ids`; `windows.tracker` carries `_video_sites_for`,
  whose names include `anime_only_site_ids`, and `_video_site_options`
  calls it. A build log is not evidence here — PyInstaller caches, and a
  no-op rebuild re-copies the previous binary.
- **The bundle gate passed**: every file `Atomic.spec` promises in
  `datas` present in the archive and byte-identical to the one on disk,
  174 entries.
- **`check_release_notes.py` passed** for 1.9, with one note written.
- **The tag and its executable were confirmed on `origin`**:
  `refs/tags/v1.9` present remotely, the blob at `v1.9:Atomic.exe`
  matching the object built and hashed here.
- **The updater resolved this release from the live repository.** Run
  with `APP_VERSION` lowered to 1.8, `check_for_update()` returned
  `v1.9`; run as 1.9 it returned `None`, meaning already current.

Not verified: no Defender scan was performed on this build.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
