"""Restore the player startup/handoff that shipped in Atomic 1.10.139.

1.10.139 is the user-confirmed good playback baseline. Later runtime patches
wrapped the core PlayerPage startup methods, changed mpv cache options and
changed torrent resume/start waits. This module removes those later playback
wrappers at the final hook boundary while leaving unrelated UI/Reader/Settings/
Discover/scrolling fixes installed.
"""
from __future__ import annotations

import sys
import time

_INSTALLED = False
_PATCHED = set()


def _find_core(function, module_name, function_name, seen=None):
    """Find the original module function captured underneath wrapper closures."""
    if seen is None:
        seen = set()
    if not callable(function) or id(function) in seen:
        return None
    seen.add(id(function))
    if (getattr(function, "__module__", None) == module_name
            and getattr(function, "__name__", None) == function_name):
        return function
    for cell in getattr(function, "__closure__", ()) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value):
            found = _find_core(value, module_name, function_name, seen)
            if found is not None:
                return found
    original = getattr(function, "_atomic_original", None)
    if callable(original):
        return _find_core(original, module_name, function_name, seen)
    return None


def _restore_video_backend():
    from . import video_backend
    core = _find_core(video_backend.default_options,
                      "helpers.video_backend", "default_options")
    if core is not None:
        video_backend.default_options = core


def _restore_torrent_engine():
    from . import torrent_engine as engine

    # Exact 1.10.139 startup/resume budgets.
    engine.INDEX_WAIT = 3.0
    engine.RESUME_INDEX_WAIT = 6.0
    engine.RESUME_BAND_WAIT = 3.0

    core_add = _find_core(engine.add, "helpers.torrent_engine", "add")
    if core_add is not None:
        engine.add = core_add
    core_arm = _find_core(engine.arm_start_band,
                          "helpers.torrent_engine", "arm_start_band")
    if core_arm is not None:
        engine.arm_start_band = core_arm

    core_apply = _find_core(engine._Torrent._apply_windows,
                            "helpers.torrent_engine", "_apply_windows")
    if core_apply is not None:
        engine._Torrent._apply_windows = core_apply

    # 1.10.145 replaced this outright rather than wrapping it, so restore the
    # small 1.10.139 wait directly. It is resume-only and does not gate a fresh
    # play without a saved seat.
    def await_start_band(info_hash: str, wait: float = None) -> bool:
        torrent = getattr(engine, "_torrents", {}).get(
            str(info_hash or "").lower())
        if torrent is None or getattr(torrent, "start_seconds", None) is None:
            return False
        if wait is None:
            wait = engine.RESUME_BAND_WAIT
        deadline = time.time() + max(0.0, float(wait))
        while time.time() < deadline:
            try:
                offset = getattr(torrent, "start_offset", None)
                if offset is not None and torrent.have(torrent.piece_at(int(offset))):
                    return True
            except Exception:
                return False
            time.sleep(0.05)
        return False

    engine.await_start_band = await_start_band

    # Remove state fields belonging only to the abandoned 1.10.147 lock.
    for torrent in list(getattr(engine, "_torrents", {}).values()):
        try:
            torrent._atomic_startup_lock_147 = False
            if torrent.file_index is not None and not torrent.want_whole:
                torrent.refresh_windows()
        except Exception:
            pass


def _restore_player(module):
    key = ("player139", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Page = module.PlayerPage

    # These are the exact core methods used by 1.10.139. Search through every
    # later wrapper's closure and put the original method back on the class.
    for name in ("_play_stream", "_load_into_mpv", "_on_property",
                 "_seek_absolute", "_startup_snapshot"):
        current = getattr(Page, name, None)
        core = _find_core(current, "windows.player", name)
        if core is not None:
            setattr(Page, name, core)

    # 1.10.151 changed per-instance mpv properties before load. A new page uses
    # the 1.10.139 defaults after _restore_video_backend; clear only its marker
    # on any page object that happens to survive a hot import.
    try:
        Page._atomic_local_torrent_151 = False
    except Exception:
        pass


def _chain_final_player_restore():
    # requested_fixes_patch owns the shared lazy windows.player hook. All newer
    # UI patches chain through it, so append the 1.10.139 playback restore last:
    # their UI changes remain, their startup method wrappers do not.
    from . import requested_fixes_patch as requested
    previous = requested._patch_player

    def chained(module):
        previous(module)
        _restore_player(module)

    requested._patch_player = chained
    loaded = sys.modules.get("windows.player")
    if loaded is not None:
        _restore_player(loaded)


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _restore_video_backend()
    _restore_torrent_engine()
    _chain_final_player_restore()

    # 1.10.139's Windows presentation patch was part of the known-good build.
    try:
        from . import player_native_present_patch
        player_native_present_patch.install()
    except Exception:
        pass
