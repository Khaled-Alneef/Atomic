---
name: build-release
description: Building Atomic.exe, version numbering, the development/main branch split, tagging and shipping a release, and the VDD that accompanies one. Use for "rebuild the exe", "bump the version", "make a release", or any git operation on this repo's branches.
---

You own the path from source to a released executable. This repo ships a
48MB `Atomic.exe` committed at its root, and a running copy of the app
updates itself from GitHub tags, so mistakes here reach users.

## The two branches

| Branch | Holds |
|---|---|
| `development` | all work, full history - this is where you commit |
| `main` | one commit per released version, each tagged, nothing else |

`main` was restarted at 1.0 as a single squashed commit, so **the two
branches share no ancestry and `git merge` between them refuses**. A
release is taken as a snapshot, never merged.

## Version numbers

A release has two parts (`1.0`, `1.1`). A development build has three,
counting up from the release it sits on (`1.0.1`, `1.0.2`), and those go
out as `1.1`. The third part counts from the *last* release, not the one
it is becoming: versions compare as number tuples, so `1.0.2 < 1.1` and a
dev build correctly accepts its own release, where `1.1.1` would sort
*above* `1.1` and never accept it. The `1.1.0-dev.2` spelling fails the
same way - `parse_version` reads only the digits.

`1.0.0` is deliberately unused: it would mean "1.0 with no development on
top", which is the release. **Never tag a release `v1.0.0`** either -
`updater.RELEASE_TAG_RE` accepts only two-part tags, so a three-part tag
is invisible to updates. That guard exists because GitHub's tag list is
per repository, not per branch: without it, tagging a dev build would
offer that build to everyone running Atomic.

## Never release unless asked

The user's standing instruction: keep accumulating `1.0.1`, `1.0.2`, ...
"until I say make a release". Do not put anything on `main`, tag
anything, or bump to a two-part version on your own initiative.

## An ordinary change

1. Bump the third part of `APP_VERSION` in `src/helpers/updater.py`, in
   the same commit as the source change.
2. Commit the `src/` change.
3. **Check no `Atomic` process is running** - Windows will not let git or
   the build replace a running binary, and a checkout that hits it leaves
   the tree half-updated. Ask before stopping one; the user is often
   mid-test.
4. `python packaging/build.py`
5. Verify it is a real rebuild, not a cached re-copy: hash the exe, and
   read the new code back out of the frozen archive (see `verify-change`).
6. Separate commit: `Rebuild Atomic.exe with ...`
7. Push `development`.

## Releasing, when actually asked

Set `APP_VERSION` to the two-part number and rebuild **first** - the
snapshot copies whatever `development` holds, executable included, so a
stale dev-numbered exe would ship as the release.

```
git checkout main
git read-tree -u --reset development    # main's tree becomes development's
git commit -m "Atomic 1.1"
git tag -a v1.1 -m "Atomic 1.1"
git push origin main && git push origin v1.1
git checkout development
```

Then write `docs/VDD-<version>.md` from the previous one: version, date,
exe size and SHA-256, module line counts, a "Changes since" table, and
any design note worth recording. Afterwards, confirm the updater still
resolves the release by calling `updater.check_for_update()` against the
live repo with `APP_VERSION` temporarily lowered.

## Commit style

Sentence-case summary line, then a body explaining what was wrong and
what was measured - not a list of files. End with:

    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
