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

```
git checkout main
git read-tree -u --reset development    # main's tree becomes development's
git commit -m "Atomic 1.1"
git tag -a v1.1 -m "Atomic 1.1"
git push origin main && git push origin v1.1
git checkout development
```

`main` and `development` share no ancestry (`main` was restarted at 1.0
as a single squashed commit) - never merge, always snapshot.

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
