# Atomic — Version Description Document

**Version 1.7** · 17 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.7 |
| Release date | 2026-08-17 |
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
| `Atomic.exe` | The application. 47,764,478 bytes. SHA-256 `0c9630e72a98c22ea72ea8cf0dbb8a31f9f1f2bb0bb17a9955d0448f6d8cc0bd` |
| `src/` | Full Python source, 15,257 lines across 37 modules (15,253 at 1.6) |
| `packaging/` | `build.py`, `Atomic.spec` and `check_release_notes.py` |
| `docs/VDD-1.7.md` | This document |

Build environment unchanged from VDD-1.1 §3.

One change stands between 1.6 and this release, taken straight off
`development` as a single commit. No development builds sit in between,
and this release was cut once.

---

## 4. Inventory of software contents

| Module changed | Lines | What changed |
|---|---|---|
| `windows/tracker.py` | 3,050 | Both Title-label texts collapsed to one |
| `helpers/whats_new.py` | 318 | 1.7's note |
| `helpers/updater.py` | 272 | `APP_VERSION` |

Nothing else in `src/` differs from 1.6.

---

## 5. What this version provides

### The Title field no longer names what it will search

The Add/Edit form's Title label named the source behind the search:
"Title (type to search Stremio)" on Anime and Movies & Series, "Title
(type to search your reading websites)" on Reading. Both now read
**"Title (type to search)"**.

Which service answers is not what the label is for at the moment of
typing — it is a field caption, and the parenthetical had grown longer
than the caption itself. The reading branch was the worse of the two, at
five words of hint for one word of label.

Only the label changed. `_provider()` still picks Stremio or the
configured reading sites by entry type, the Stremio-link caption below
it still names Stremio (that field really is Stremio-specific, and
opens it), and no search behaviour is different.

Everything else in this release is 1.6. See VDD-1.6 §5.

---

## 6. Design notes

**Why both branches were flattened rather than one shortened.** The
branch on `_update_labels` exists for the *url* caption as well, which
still differs by provider; the title caption is now identical on both
sides and is left in place rather than hoisted, so the method keeps one
shape and a future per-provider title hint has somewhere to go.

---

## 7. Configuration and user data

Unchanged from VDD-1.6 §7. No stored value changes.

---

## 8. External interfaces

Unchanged from VDD-1.6 §8.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

Unchanged from VDD-1.6 §10, including the antivirus note: the durable
remedy remains a code-signing certificate, and nothing has been bought.

---

## 11. Verification performed

In addition to VDD-1.0 §11 through VDD-1.6 §11:

- **The shipped label was read back out of the frozen executable**, not
  out of the source tree: `windows.tracker` in the bundled PYZ carries
  exactly one matching string constant, `"Title (type to search)"`, and
  neither old text survives anywhere in it. `helpers.updater` carries
  `"1.7"` and `helpers.whats_new` the release's one note. A build log is
  not evidence here — PyInstaller caches, and a no-op rebuild re-copies
  the previous binary.
- **The bundle gate passed**: every file `Atomic.spec` promises in
  `datas` present in the archive and byte-identical to the one on disk,
  174 entries.
- **`check_release_notes.py` passed** for 1.7, with one note written.
- **The tag and its executable were confirmed on `origin`**:
  `refs/tags/v1.7` is present remotely, and the blob at
  `v1.7:Atomic.exe` is 47,764,478 bytes — the same object that was
  built and hashed here.

Not verified: **the updater's own `check_for_update()` could not be run
against the live repository.** `api.github.com` answered HTTP 504 on
every attempt from this machine, sandboxed and not, while `git push`
over HTTPS to the same host succeeded — an outage or a filtered API
host, not something about this release. The contract it exercises is
unchanged from 1.6, and the tag and executable were verified directly
through git instead. The release executable has also not been exercised
by its owner against real data before publication, and no Defender scan
was performed.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
