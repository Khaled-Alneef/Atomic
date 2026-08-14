# Releasing Atomic

How this repository turns work into a released build. Standing procedure,
not a description of any one version — for that, see the VDD of the
version in question.

## Two branches

| Branch | Holds |
|---|---|
| `development` | All work, commit by commit, including every intermediate `Rebuild Atomic.exe` step |
| `main` | One commit per released version, each tagged — nothing else ever lands here |

`main` is what the in-app updater effectively reads, so keeping
unreleased work off it is what makes "is this released?" answerable.

`main` was restarted at 1.0 as a single squashed commit, so **the two
branches share no ancestry and `git merge` between them refuses.** A
release is taken as a snapshot instead.

## Version numbers

A release has two parts. A build still in development has three, counting
up from the release it sits on top of:

| Version | Where | Tagged |
|---|---|---|
| 1.0 | `main` | `v1.0` |
| 1.0.1, 1.0.2, … | `development` | no |
| 1.1 | `main` | `v1.1` |
| 1.1.1, 1.1.2, … | `development` | no |

The three-part form deliberately sorts *below* the release it leads to —
1.0.2 < 1.1 — so a development build correctly recognises its own release
as newer when it lands. Numbering development builds with the version
they are *becoming* would invert that: 1.1.1 sorts above 1.1, and such a
build would never accept the release. The conventional `1.1.0-dev.2`
spelling fails the same way, because `updater.parse_version` reads only
the digits and lands on (1, 1, 0, 2).

`1.0.0` is unused on purpose: it would mean "1.0 with no development on
top of it", which is the release itself.

**Never tag a release `v1.0.0`.** `updater.RELEASE_TAG_RE` accepts only
two-part tags. That guard exists because GitHub's tag list is per
*repository*, not per branch — without it, tagging a development build
would offer that build to everyone running Atomic.

## An ordinary change

1. Bump the third part of `APP_VERSION` in `src/helpers/updater.py`, in
   the same commit as the source change.
2. Commit the `src/` change.
3. Close any running Atomic — Windows will not let git or the build
   replace a running binary.
4. `python packaging/build.py`
5. Confirm it is a real rebuild rather than a cached re-copy: hash the
   executable, and read the new code back out of the frozen archive.
6. Commit the executable separately: `Rebuild Atomic.exe with …`
7. Push `development`.

Nothing goes on `main` and nothing is tagged during ordinary work.

## Releasing

Only when the release is actually asked for.

Set `APP_VERSION` to the two-part number and rebuild **first** — the
snapshot copies whatever `development` holds, executable included, so a
stale development-numbered build would otherwise ship as the release.

```
git checkout main
git read-tree -u --reset development    # main's tree becomes development's
git commit -m "Atomic 1.1"
git tag -a v1.1 -m "Atomic 1.1"
git push origin main && git push origin v1.1
git checkout development                # next round opens at 1.1.1
```

Afterwards, confirm the updater resolves the new release: call
`updater.check_for_update()` against the live repository with
`APP_VERSION` temporarily lowered, and check the reported blob hash
matches the committed executable.

## Version Description Documents

**One VDD per released version, and only for released versions.** A VDD
describes a delivered build; a development build (1.0.1, 1.0.2, …) is not
delivered to anyone and gets no document.

Write it as part of making the release, not before:

- Name it `docs/VDD-<version>.md`, starting from the previous release's
  document.
- Take the executable's size and SHA-256 from the released build.
- Build the "Changes since" section from the commit log since the
  previous release tag — `git log v1.0..development` — which is why
  commit messages here carry a real explanation rather than a list of
  files.
- Record any design decision whose reasoning is not obvious from the
  code, especially where it cost investigation to arrive at.

**Never edit a released version's VDD.** It records what was actually
delivered under that version; folding later work into it would make it
describe an executable nobody has.
