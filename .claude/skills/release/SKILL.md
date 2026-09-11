---
name: release
description: Ship an Atomic release to main - version bump, branch
snapshot, tag, push, and the VDD. Use only when the user has explicitly
asked for a release ("approved, release it"), never on your own
initiative.
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

Set `APP_VERSION` to the two-part number **in both places** (see below)
and rebuild first (see the `build` skill), so what is uploaded is the
tree being tagged. `docs/ROADMAP.md` is development-only and never
ships: drop it from every snapshot.

**The artifact is `Atomic.zip`, not the bare exe** (CLAUDE.md rule 8):
a downloaded .exe is refused as `Trojan:Win32/Wacatac.B!ml` while the
same bytes inside a zip come through. That still holds and now names
the *asset*.

**The build itself is a release asset now, not a committed file**
(2.0, 8 September 2026). It is 126MB and GitHub refuses any file over
100MiB on push, so `git add -f Atomic.zip` cannot work and never will
again. What is committed at the tag is the **bridge installer**, which
is the only thing an install running 1.10 or older can be handed - see
`docs/RELEASING.md` and `packaging/bridge/`.

**Bump the version in two places**: `updater.APP_VERSION` *and*
`development_version_patch.install()`, which overwrites it at startup
and is installed last.

```
py -3.13 packaging/check_release_notes.py   # fail if this version has no notes
py -3.13 packaging/build.py --zip           # the real build, ~126MB, for the asset
py -3.13 packaging/bridge/build_bridge.py   # the ~10MB Atomic.exe for the tag
# scan the release exe with Defender (below), then:
git add -A && git commit -m "Atomic 2.0"    # development
git checkout main
git read-tree -u --reset development
cp packaging/bridge/dist/Atomic.exe Atomic.exe
python -c "import
zipfile;zipfile.ZipFile('Atomic.zip','w',zipfile.ZIP_DEFLATED).write('Atomic.exe','Atomic.exe')"
git add -f Atomic.exe Atomic.zip            # the bridge, both shapes - never the app
git rm --cached docs/ROADMAP.md docs/NEXT-SESSION.md && rm -f docs/ROADMAP.md docs/NEXT-SESSION.md
git commit -m "Atomic 2.0"
git tag -a v2.0 -m "Atomic 2.0"
git push origin main && git push origin v2.0
gh release create v2.0 <dir>/Atomic.zip --title "Atomic 2.0" --notes-file <notes>
git checkout development
```

**The asset has to be *named* `Atomic.zip`, and `gh`'s `#` does not do
that.** Both readers look for that exact name (`updater.ZIP_NAME`,
`atomic_setup.ZIP_NAME`), so an asset called anything else is invisible
to every install - the release exists and nobody is ever offered it. `gh
release create v2.1 /tmp/Atomic-2.1-release.zip#Atomic.zip` reads as a
fix and is not: `file#label` sets a *display label* and the asset keeps
the file's own name. Measured at 2.1, which uploaded as
`Atomic-2.1-release.zip` and had to be deleted and re-uploaded. **Copy
the build to a directory as `Atomic.zip` and upload that path**, then
read the name back before believing it:

```
gh release view v2.1 --json assets --jq '[.assets[]|{name,size,state}]'
```

**`docs/NEXT-SESSION.md` is development-only too**, like the roadmap -
it has never been in a snapshot, and `read-tree` brings it in silently
(caught at 2.1 by reading `git status` before committing, not by the
procedure). Anything on `development` that main has never carried is
worth the same look.

The release is not published until the asset is attached: the tag alone
offers 1.10 installs a bridge with nothing to fetch.

`main` and `development` share no ancestry (`main` was restarted at 1.0
as a single squashed commit) - never merge, always snapshot.

**Prove the exe belongs to the tree you are tagging, and notes are
written, before pushing.**
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
