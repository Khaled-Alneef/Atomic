# Atomic — Version Description Document

**Version 2.12** · 27 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.12 |
| Release date | 2026-09-27 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` (`Atomic/Atomic.exe` + `Atomic/_internal/`) |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 137,537,204 bytes |
| Asset SHA-256 | `bc513573a511cab9974691f0fd64d39234ae31c4bca5384e75b4381e9dee789e` |
| Preceded by | 2.11 — `docs/VDD-2.11.md` |

---

## 2. System overview

Unchanged in purpose and on-disk shape from 2.7 — see `docs/VDD-2.7.md`
§2. 2.12 carries one subtitle fix.

---

## 3. Inventory of materials released

Unchanged from 2.7 §3. One asset, `Atomic.zip`, named exactly that
because `updater.ZIP_NAME` and `atomic_setup.ZIP_NAME` both look for it.

**Changes since the last release** (`git log released/2.11..development`):

| Commit | Summary |
|---|---|
| `660923c` | A picked subtitle file may be up to 128MB - a typeset .ass was refused |

---

## 4. What this version provides

**Large subtitle files load from the player's file picker.** The owner,
27 September 2026: *"why could not I load the subtitles in my Downloads
???? it is .ass"*.

---

## 5. Design notes

The file was `[ESPADAS-3ASQ] Bleach Sennen Kessen-hen - 48 [Time-24.32].ass`,
**36,361,125 bytes**: 110,273 Dialogue lines of drawn typesetting (31.1M
characters of `[Events]`) and 22 embedded fonts (4.9M of `[Fonts]`).
`subtitles.read_file` refused anything over `MAX_SUBTITLE_BYTES` (8MB) —
a cap written for downloads, where it stops a zip bomb — and his log
carried "the file could not be read" for every one of about twenty tries.
mpv itself lists and selects the file in 0.45s (measured on the app's
libmpv), so the cap was the whole failure.

A file picked off this disk now has its own cap,
`MAX_LOCAL_SUBTITLE_BYTES` (128MB), passed through `_unpack` as `limit`
so a zipped copy is read the same way; downloads keep the 8MB cap. A file
over the new cap is logged with its size instead of the bare message.

---

## 6. Configuration and user data

Unchanged from 2.7 §6.

## 7. External interfaces

Unchanged from 2.7 §7.

## 8. Installation and removal

Unchanged from 2.7 §8.

---

## 9. Known limitations

Carried forward from 2.11 §9.

---

## 10. Verification performed

**Source tree.** `read_file` on his file: 36,135,733 characters in
0.05s, format `ass`; a zipped copy read to identical text; `_unpack` with
no limit still refuses the zip at the 8MB cap.

**mpv.** The file handed to `sub-add` on the app's libmpv against a
lavfi source: listed and selected at +0.45s, text present at 10:00.

**The owner** tested the local build and reported it working.

**The frozen build, read back out of its own archive:** `read_file` in
`helpers.subtitles` names `MAX_LOCAL_SUBTITLE_BYTES` and `logs`, the
constant 128,000,000 is present; "2.12" in `updater`,
`development_version_patch` and `whats_new`.

**Defender**, `MpCmdRun -Scan -ScanType 3 -DisableRemediation`, service
Running: exit 0 on the bridge `Atomic.exe`, the app `Atomic.exe` and
`Atomic.zip`; both executables re-scanned, exit 0 again.

**The release-notes gate:** one note written for 2.12.

**Not measured by me.** The picker driven on the frozen player with
screenshots — the owner's own test stands in for it.

---

## 11. Glossary

Unchanged from 2.7 §11.
