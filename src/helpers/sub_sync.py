"""Line an outside subtitle up with the video it is laid over.

**His report, 17 November 2026: "the episode I am in now seems to be not
loading the subtitles correctly ... I mean the Arabic".** Silicon Valley
S04E08, the MAX WEB-DL (TURG) release, with the Arabic OpenSubtitles row
he picked: the text was clean UTF-8 and the right episode, and every
line came up **50.2 seconds late**. Measured against the English SDH
track muxed into the same file: 139 of 537 lines start within 0.35s of
an English line as loaded, 490 of 537 do at a shift of -50.2s, and the
first and second halves of the episode each give the same -50.2s. The
other two Arabic files for that episode measured -46.2s (MOROSE WEBRip)
and no single shift at all (OSN WEBRip: 141 of 448 at its best - a
different cut). Nothing in the app compared a subtitle with the file it
was going to cover, so nothing could notice.

The reference is the file's own text tracks: every track in one file is
timed to that file, whatever its language, so their line starts are the
video's clock. mkv_subs.cue_starts reads them off the Cues index in
milliseconds (21ms, eight tracks, on the file above) while the episode
is still downloading.

What this does *not* do, so nobody assumes it: a subtitle cut against a
different frame rate (a steady drift, not a shift) and a different edit
(the OSN case) are both rejected rather than half-fixed, and a source
with no text track of its own - most .mp4s, any debrid link, which has
no file on this disk - has nothing to measure against.
"""

import bisect
import collections
import os

from helpers import logs, mkv_subs

# How far apart the two clocks may be. A recap or a distributor's logo
# runs 5-90s; 300s leaves room for a cold open as well.
SEARCH_S = 300.0
# Histogram bins, and how close a line must start to a reference line to
# count as the same line. 0.35s: the three files above measured a median
# residual under 0.1s at their best shift, and a 0.35s window catches a
# re-timed line without letting a neighbour in (lines here are 2-4s
# apart).
BIN_S = 0.1
TOLERANCE_S = 0.35
# What a shift needs before it is applied. The good file above: 490 of
# 537 (91%). The different cut: 31% at its best shift. Half sits well
# clear of both, and 20 lines is enough that a coincidence cannot reach
# it (with lines ~3s apart a random shift lines up roughly one in four).
MIN_MATCHES = 20
MIN_SHARE = 0.5
# The best shift must stand out: the runner-up more than 2s away may
# score at most this share of it.
RUNNER_UP_SHARE = 0.6
# A shift this small is the subtitle being right already.
NEGLIGIBLE_S = 0.15


def _nearest(sorted_values, value):
    index = bisect.bisect_left(sorted_values, value)
    best = None
    for candidate in sorted_values[max(0, index - 1):index + 1]:
        if best is None or abs(candidate - value) < abs(best - value):
            best = candidate
    return best


def best_shift(starts, reference):
    """The shift to add to `starts` so they land on `reference`.

    `{shift, matched, candidates, share, runner_up}`, or None when
    either list is empty. Every number is reported so the caller can
    log why a shift was or was not taken."""
    starts = sorted(float(s) for s in starts or ())
    reference = sorted(float(r) for r in reference or ())
    if not starts or not reference:
        return None
    hist = collections.Counter()
    for start in starts:
        low = bisect.bisect_left(reference, start - SEARCH_S)
        high = bisect.bisect_right(reference, start + SEARCH_S)
        for ref in reference[low:high]:
            hist[round((ref - start) / BIN_S)] += 1
    if not hist:
        return None
    reach = max(1, round(TOLERANCE_S / BIN_S))
    scores = {}
    for bin_index in hist:
        for near in range(bin_index - reach, bin_index + reach + 1):
            if near not in scores:
                scores[near] = sum(hist.get(k, 0) for k in range(near - reach,
                                                                  near + reach + 1))
    best_bin = max(scores, key=lambda b: (scores[b], -abs(b)))
    far = round(2.0 / BIN_S)
    runner = max((score for b, score in scores.items()
                  if abs(b - best_bin) > far), default=0)
    shift = best_bin * BIN_S

    def count(at):
        residuals, candidates = [], 0
        first, last = reference[0] - 1.0, reference[-1] + 1.0
        for start in starts:
            moved = start + at
            if not first <= moved <= last:
                continue        # the reference does not reach this far
            candidates += 1
            near = _nearest(reference, moved)
            if near is not None and abs(near - moved) <= TOLERANCE_S:
                residuals.append(near - moved)
        return residuals, candidates

    residuals, candidates = count(shift)
    if residuals:
        # The bin is a tenth of a second wide; the median residual is
        # where inside it the lines actually sit.
        residuals.sort()
        shift += residuals[len(residuals) // 2]
        residuals, candidates = count(shift)
    matched = len(residuals)
    return {"shift": round(shift, 2), "matched": matched,
            "candidates": candidates,
            "share": (matched / candidates) if candidates else 0.0,
            "runner_up": (runner / scores[best_bin]) if scores[best_bin] else 1.0}


def reference_starts(media_path, finished=False):
    """{track number: [line starts]} from the file's own text tracks.

    The Cues index first (mkv_subs.cue_starts). Only for a file that is
    wholly on disk, and only when the index names no subtitle line, are
    the blocks walked instead - a top-to-bottom walk of a file still
    filling in would stop at its first hole and report the opening
    minutes as the whole episode."""
    path = str(media_path or "")
    if not mkv_subs.usable(path):
        return {}
    tracks = mkv_subs.list_tracks(path)
    if not tracks:
        return {}
    found = mkv_subs.cue_starts(path, [t["number"] for t in tracks])
    found = {k: v for k, v in found.items() if len(v) >= MIN_MATCHES}
    if found or not finished:
        return found
    from helpers import subtitles
    for track in tracks[:2]:
        text = mkv_subs.extract_srt(path, track["number"], budget_s=10.0)
        starts = [cue["start"] for cue in subtitles.parse(text, "srt")]
        if len(starts) >= MIN_MATCHES:
            found[track["number"]] = starts
    return found


def measure(subtitle_starts, media_path, finished=False):
    """What to set the delay to for this subtitle on this file.

    `{verdict, shift, matched, candidates, share, track, reference}`:

    - `synced`     - apply `shift` as the delay;
    - `other-cut`  - the reference is good and nothing lines up: the
                     subtitle was made for another version;
    - `no-reference` - this file carries no text track to measure by,
                     or its index is not on disk yet (the caller may
                     ask again as the file arrives).

    Every track is tried and the best kept: they share one clock, and a
    forced-only track (a dozen lines) must not stand in for the SDH one
    beside it. Never raises."""
    try:
        starts = sorted(float(s) for s in subtitle_starts or ())
        if len(starts) < MIN_MATCHES:
            return {"verdict": "no-reference", "reason": "too few lines"}
        refs = reference_starts(media_path, finished=finished)
        if not refs:
            return {"verdict": "no-reference", "reason": "no text track indexed"}
        best = None
        for track, reference in refs.items():
            found = best_shift(starts, reference)
            if found is None:
                continue
            found["track"] = track
            found["reference"] = len(reference)
            if best is None or found["matched"] > best["matched"]:
                best = found
        if best is None:
            return {"verdict": "no-reference", "reason": "nothing to compare"}
        good = (best["matched"] >= MIN_MATCHES
                and best["share"] >= MIN_SHARE
                and best["runner_up"] <= RUNNER_UP_SHARE)
        if good and abs(best["shift"]) < NEGLIGIBLE_S:
            best["shift"] = 0.0
        best["verdict"] = "synced" if good else "other-cut"
        return best
    except Exception:
        logs.exception("subtitle sync: the measurement failed")
        return {"verdict": "no-reference", "reason": "error"}


def describe(result) -> str:
    """One log line's worth of a measurement."""
    if not result:
        return "nothing measured"
    if result.get("verdict") == "no-reference":
        return f"no reference ({result.get('reason')})"
    return (f"{result['verdict']}: shift {result['shift']:+.2f}s, "
            f"{result['matched']} of {result['candidates']} lines "
            f"({result['share']:.0%}) against track {result.get('track')} "
            f"({result.get('reference')} lines), runner-up "
            f"{result['runner_up']:.0%}")


def file_name(path) -> str:
    return os.path.basename(str(path or ""))
