# Remote Tests

**Nothing here is a release**: no tag, no version bump, and the in-app
updater ignores this branch.

| file | what it is |
|---|---|
| `Atomic.exe` | the current build (seventh of 25 August 2026) |
| `Atomic.zip` | the same exe, zipped - usually gets past a browser's "not commonly downloaded" block |
| `Atomic-build1.exe` | **the first build of the day**, from before the flagging started |

## The virus warning: what was checked, and the one test left

Measured 25 August 2026, on the seven builds pushed here today,
extracted from this branch's own history:

* **Same 193 bundled entries** in every build - no DLL, no data file
  and no dependency was added at any point.
* **Same 347 Python modules**, none added. The last three builds differ
  from the last un-flagged one by nine module updates in total:
  build 5 (cover_fetch, lookup_pool, widgets, details, player, reader,
  tracker), build 6 (discover), build 7 (torrent_engine).
* **The bootloader is byte-identical.** The first byte that differs
  between two builds is offset 0x108 - the PE TimeDateStamp - and 4093
  of the first 4096 bytes match.
* **Every one of the seven scans clean** under Defender with cloud
  protection on (MAPSReporting 2, block-at-first-sight enabled) and with
  the Mark-of-the-Web set to this branch's raw URL.
* `upx=True` in Atomic.spec **has never applied** - upx.exe is not
  installed, so PyInstaller silently skips it. The usual first suspect
  for Wacatac!ml on PyInstaller builds was never in play.

So nothing found so far can be pinned on the code. **`Atomic-build1.exe`
is the experiment that settles it**: download it now.

* build 1 blocked too -> it is reputation or the machine's policy, not
  the code, and no code change will clear it.
* build 1 clean, current one blocked -> it *is* one of those nine
  modules, and there is enough here to bisect them.
