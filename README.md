# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, seventh build |
| SHA-256 | `186de6e68273cc4d25d8b7226680be4403306187758e0f84c804ae14048659fe` |

### Fixed since the sixth build

**A pack no longer plays the wrong episode because an addon said so.**
The owner: *"the same source in Silo series ep 1 s1 sometimes plays a
diff ep"* - *sometimes*, because which file plays came down to the
addon's own `fileIdx`, and two addons offering the same release can
disagree about it.

The app already overrode a fileIdx when a file's name stated a
different `SxxExx`, or when the season folders placed the episode
elsewhere. What it did not do was override one on a pack whose files
carry bare numbers - so a pack named `Silo - 01 .. - 10` with a fileIdx
of 6 served episode 7 for a request for episode 1.

Now a loose reading wins too, but only against a file that contradicts
itself: some other file must read as the episode asked for **and** the
file the index points at must state a number that is not it. Both
halves are positive evidence, so this cannot fire on a pack whose names
say nothing - which is the case the fileIdx was measured right for, six
of six, and still owns.

Checked against thirteen pack shapes (single file, season pack,
two-season pack, season folders with loose numbers, absolute numbering
across seasons, flat loose numbering, and an episode the pack does not
hold) with the fileIdx right, wrong and absent: 15 of 15. Then against
two real Silo packs pulled live - S01E01 and S01E05 both resolve to the
correctly named file.
