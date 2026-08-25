---
name: release
description: Ship an Atomic release to main - version bump, branch snapshot, tag, push, and the VDD. Use only when the user has explicitly asked for a release ("approved, release it"), never on your own initiative.
---

# Release

Never run this unprompted - see the standing rule in CLAUDE.md. Full
version-number reasoning: `.claude/rules` has none dedicated to this;
the reasoning lives inline below since it's release-specific and only
read at release time.

## Version numbers

A release has two parts (`1.0`, `1.1`). A development build has three,
counting up from the release it sits on (`1.0.1`, `1.0.2`), released as
the next two-part number. The third part counts from the *last*
release, not the one it's becoming: versions compare as number tuples,
so `1.0.2 < 1.1` and a dev build correctly accepts its own release -
`1.1.1` would sort *above* `1.1` and never accept it. `1.0.0` is
deliberately unused (it would mean "no development on top", i.e. the
release itself). Never tag `v1.0.0` either - `updater.RELEASE_TAG_RE`
accepts only two-part tags, so a three-part tag is invisible to
updates and would otherwise offer a dev build to every user (GitHub's
tag list is per repository, not per branch).

## Procedure

Set `APP_VERSION` to the two-part number and rebuild **first** (see the
`build` skill) - the snapshot below copies whatever `development`
holds, executable included, so a stale dev-numbered exe would ship as
the release.

The exe is gitignored on `development`, so `read-tree` carries no
executable and the checkout deletes any untracked one sitting in the
tree - build the release exe *on `main`*, from the source the release
actually contains (`docs/RELEASING.md` is the long form).
`docs/ROADMAP.md` is development-only and never ships: drop it from
every snapshot.

**The artifact is `Atomic.zip`, not the bare exe** (CLAUDE.md rule 8):
a downloaded .exe is refused as `Trojan:Win32/Wacatac.B!ml` while the
same bytes inside a zip come through. The **first** zipped release
commits both, because every install already out there runs an updater
that looks for `Atomic.exe` alone; after that one, drop the exe.

```
rm -f Atomic.exe Atomic.zip             # untracked here; checkout refuses otherwise
git checkout main
git read-tree -u --reset development    # main's tree becomes development's
python packaging/build.py --zip         # build the release exe and zip it
python packaging/check_release_notes.py # fail if this version has no notes
git add -f Atomic.zip                   # gitignored, so -f is required
git add -f Atomic.exe                   # first zipped release only - see above
git rm --cached docs/ROADMAP.md && rm -f docs/ROADMAP.md
git commit -m "Atomic 1.1"
git tag -a v1.1 -m "Atomic 1.1"
git push origin main && git push origin v1.1
git checkout development
```

`main` and `development` share no ancestry (`main` was restarted at 1.0
as a single squashed commit) - never merge, always snapshot.

**Prove the exe belongs to the tree you are tagging, and notes are written, before pushing.**
1.4 shipped an executable built before the last two commits - it was
missing `src/filter_icon.png` entirely. This is now caught automatically:
`packaging/build.py` verifies the produced exe contains every file
listed in `Atomic.spec`'s `datas` and fails loudly if the build is stale
or incomplete. A "succeeded" build log proves nothing — PyInstaller
caches aggressively — but the build script now enforces verification.

**Scan the release exe with Defender before tagging it.** 1.10's first
cut was flagged `Trojan:Win32/Wacatac.B!ml` and deleted as the owner
downloaded it, while 1.9 was clean - and there is no source fix, because
the verdict is a cloud ML guess on the binary's shape. PyInstaller does
not build byte-identical output twice, so *rebuilding the same tree*
produces a different file, and the next one is often clean. Build, scan,
repeat until clean, and tag that binary:

```
& "C:\Program Files\Windows Defender\MpCmdRun.exe" -Scan -ScanType 3 -File <exe> -DisableRemediation
```

Exit 0 is clean, 2 is flagged; `-DisableRemediation` reports without
quarantining. **Check `Get-Service WinDefend` is Running first** - a
stopped service fails the scan with `0x800106ba`, which looks like
"nothing found" to anyone reading only for a threat name. Re-run a
verdict once to confirm: it is stable per file, and only varies between
builds. This is a lottery ticket, not a fix - roadmap #8 (code signing)
is still the durable answer.

`packaging/check_release_notes.py` is the other gate: no `NOTES` entry
for the version about to ship, no tag. 1.4 shipped without one and
recorded itself as seen anyway, so its notes could never be shown to
anyone who had already updated. It works out the version itself -
two-part `APP_VERSION` means that release is the one being made,
three-part means a development build heading for the next one, and
reading the two-part case as "the one after" would demand notes for 1.6
while shipping 1.5.

## The VDD

Written for a release, and only for a release - a development build
(1.0.1, 1.0.2...) gets no document. Start from the previous release's
VDD: size and SHA-256 from the released build, "Changes since" from
`git log released/<previous>..development` (note `released/1.0`, not
`v1.0` - `main`'s tags point at squashed snapshots with no shared
ancestry with `development`, so a `v1.0..development` range excludes
nothing). Tag the development commit taken as `released/<version>` -
that's what makes "since the last release" answerable at all. Never
edit a released version's VDD after the fact.

## Afterward

Confirm the updater actually resolves the release: call
`updater.check_for_update()` against the live repo with `APP_VERSION`
temporarily lowered.
