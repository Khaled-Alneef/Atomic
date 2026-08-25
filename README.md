# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, sixth build |
| SHA-256 | `3f5dd40b7a1788e77ee7f0903ce92ff6f5d616ad67e4e0da3573a0b3a632bc8b` |

### Fixed since the fifth build

**Discover's reading search is ranked by the title now.** The rows were
never missing - they were ordered by whose turn it was. The interleave
exists so a *browse* shows every site rather than thirty rows of
whichever answered first, and it was being applied to searches too:

    "One Piece"   led with One Piece Special: Boichi Crossover,
                  then One Piece Strong World 0 - the manga was fifth
    "Kingdom"     led with Eternal Kingdom and How I destroyed my
                  kingdom! - Kingdom and Kingdom (WAN) were ninth

Now every one of them leads with the right title from 3asq: Kingdom,
then Kingdom (WAN); One Piece ahead of One Piece (French); Hunter X
Hunter for both "Hunter x Hunter" and "HxH". Ranked, not filtered -
nothing a site answered is thrown away, and the interleave still
decides ties, so no site loses its place.
