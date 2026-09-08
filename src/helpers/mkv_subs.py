"""Read a subtitle track out of a Matroska file, without any other app.

The owner, 4 September 2026: *"if there is En embedded then add it as an
option so that AI can translate from, not just Opensubtitles!"* - a
release that already carries an English track should be able to feed the
Arabic translator, instead of the player going out to OpenSubtitles for
text the file is holding.

**Why this is written rather than shelled out to.** Every other way of
getting those lines needs a second program installed - ffmpeg,
mkvextract - and `.claude/rules/integrations.md` is explicit that Atomic
has to work on a clean machine with nothing beside it. libmpv is bundled
and plays the track happily but publishes only the line currently on
screen (`sub-text`), which is no use for translating a whole episode. So
the container is read here: a subtitle track is text in a handful of
elements, and the parts of Matroska needed to find them are small.

Deliberately narrow. It reads only what a subtitle needs - the track
list, the timestamp scale, and the blocks of one track - and it walks
clusters top to bottom rather than using the Cues index, because a
subtitle track's blocks are spread through the file either way. Anything
it does not understand is skipped by its own declared size, which is what
makes an unknown element harmless.
"""

import io
import os
import time

# Only the elements this needs. Everything else is skipped by its size.
_SEGMENT = 0x18538067
_INFO = 0x1549A966
_TIMESTAMP_SCALE = 0x2AD7B1
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CODEC_ID = 0x86
_CODEC_PRIVATE = 0x63A2
_LANGUAGE = 0x22B59C
_LANGUAGE_IETF = 0x22B59D
_NAME = 0x536E
_CLUSTER = 0x1F43B675
_CLUSTER_TIME = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_BLOCK_DURATION = 0x9B

# The containers this walks into rather than skipping over.

_SUBTITLE_TRACK = 0x11

# The text codecs a subtitle track can be. Anything else - a bitmap
# track, PGS or VobSub - carries pictures, not lines, and is not offered.
_TEXT_CODECS = ("S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA", "S_TEXT/WEBVTT")

# How long a whole-file walk may take. A subtitle track's blocks run the
# length of the file, so this reads all of it - 1.4 GB of local disk is a
# few seconds, and a file that is still downloading is not worth waiting
# on (see usable).
DEFAULT_BUDGET_S = 25.0


def _read_vint(stream, keep_marker=False):
    """One EBML variable-length integer, or None at end of stream."""
    first = stream.read(1)
    if not first:
        return None
    value = first[0]
    if value == 0:
        return None                 # not a valid length descriptor
    length = 1
    mask = 0x80
    while not value & mask:
        mask >>= 1
        length += 1
    number = value if keep_marker else value & (mask - 1)
    rest = stream.read(length - 1)
    if len(rest) != length - 1:
        return None
    for byte in rest:
        number = (number << 8) | byte
    return number


def _read_uint(data):
    number = 0
    for byte in data:
        number = (number << 8) | byte
    return number


def _walk(stream, end, deadline):
    """Yield `(element_id, size, data_offset)` for one level.

    `data` is not read here: a cluster is megabytes and only the blocks
    of one track are ever wanted."""
    while stream.tell() < end:
        if deadline and time.monotonic() > deadline:
            return
        element = _read_vint(stream, keep_marker=True)
        if element is None:
            return
        size = _read_vint(stream)
        if size is None:
            return
        start = stream.tell()
        # An unknown-size element (all size bits set) runs to the end of
        # its parent - live-muxed files do this for the Segment.
        if size >= (1 << 56) - 1:
            size = end - start
        yield element, size, start
        stream.seek(start + size)


def _tracks_from(stream, end, deadline):
    found = []
    for element, size, start in _walk(stream, end, deadline):
        if element != _TRACK_ENTRY:
            continue
        entry = {"number": 0, "type": 0, "codec": "", "lang": "",
                 "title": "", "private": b""}
        for child, child_size, child_start in _walk(
                stream, start + size, deadline):
            data = stream.read(child_size) if child_size < 1 << 20 else b""
            if child == _TRACK_NUMBER:
                entry["number"] = _read_uint(data)
            elif child == _TRACK_TYPE:
                entry["type"] = _read_uint(data)
            elif child == _CODEC_ID:
                entry["codec"] = data.decode("ascii", "replace").strip("\0")
            elif child == _CODEC_PRIVATE:
                entry["private"] = data
            elif child in (_LANGUAGE, _LANGUAGE_IETF):
                entry["lang"] = (entry["lang"]
                                 or data.decode("ascii", "replace").strip("\0"))
            elif child == _NAME:
                entry["title"] = data.decode("utf-8", "replace").strip("\0")
            stream.seek(child_start + child_size)
        if entry["number"]:
            found.append(entry)
    return found


def _open(path):
    stream = open(path, "rb")
    size = os.path.getsize(path)
    header_end = size
    # Step over the EBML header to reach the Segment.
    for element, element_size, start in _walk(stream, size, None):
        if element == _SEGMENT:
            return stream, start, start + element_size
    stream.close()
    return None, 0, header_end


def list_tracks(path):
    """Every *text* subtitle track in this file.

    `[{number, lang, title, codec}]`, in the file's own order. Empty for
    anything that is not a readable Matroska - a partly downloaded file,
    an mp4, a URL - which is the honest answer and the one the caller
    treats as "nothing to offer"."""
    stream = None
    try:
        stream, start, end = _open(path)
        if stream is None:
            return []
        deadline = time.monotonic() + 8.0      # the track list is near the top
        for element, size, offset in _walk(stream, end, deadline):
            if element != _TRACKS:
                continue
            rows = _tracks_from(stream, offset + size, deadline)
            return [{"number": t["number"], "lang": t["lang"],
                     "title": t["title"], "codec": t["codec"]}
                    for t in rows
                    if t["type"] == _SUBTITLE_TRACK
                    and t["codec"].upper() in _TEXT_CODECS]
        return []
    except Exception:
        return []
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _srt_time(seconds):
    if seconds < 0:
        seconds = 0.0
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    if ms == 1000:
        whole, ms = whole + 1, 0
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d},{ms:03d}"


def _ass_text(payload):
    """The visible text of an ASS block.

    A Matroska ASS block is the Dialogue line **without** its
    "Dialogue:" prefix and without the start/end fields - those are the
    block's own timestamps - so it reads
    `ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text`.
    Nine fields, and only the last may contain commas."""
    parts = payload.split(",", 8)
    text = parts[8] if len(parts) == 9 else payload
    # Drawing and override tags: {\an8}, {\pos(..)} and the like say
    # where and how, never what.
    out, depth = [], 0
    for char in text:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out).replace("\\N", "\n").replace("\\n", "\n").strip()


def extract_srt(path, number, budget_s=DEFAULT_BUDGET_S):
    """One track's lines as SRT text, or "".

    SRT because helpers/subtitles.parse already reads it, so the
    translator, the .ass writer and the player's own loader all take this
    without learning a new shape."""
    stream = None
    try:
        stream, start, end = _open(path)
        if stream is None:
            return ""
        deadline = time.monotonic() + float(budget_s or DEFAULT_BUDGET_S)
        scale = 1_000_000                    # Matroska's default: nanoseconds
        codec = ""
        for track in list_tracks(path):
            if track["number"] == number:
                codec = track["codec"].upper()
                break
        cues = []
        for element, size, offset in _walk(stream, end, deadline):
            if element == _INFO:
                for child, child_size, child_start in _walk(
                        stream, offset + size, deadline):
                    if child == _TIMESTAMP_SCALE:
                        scale = _read_uint(stream.read(child_size)) or scale
                    stream.seek(child_start + child_size)
            elif element == _CLUSTER:
                cluster_time = 0
                for child, child_size, child_start in _walk(
                        stream, offset + size, deadline):
                    if child == _CLUSTER_TIME:
                        cluster_time = _read_uint(stream.read(child_size))
                    elif child in (_SIMPLE_BLOCK, _BLOCK):
                        cue = _block(stream, child_size, cluster_time,
                                     scale, number, codec)
                        if cue:
                            cues.append(cue)
                    elif child == _BLOCK_GROUP:
                        duration = 0
                        pending = None
                        for kid, kid_size, kid_start in _walk(
                                stream, child_start + child_size, deadline):
                            if kid == _BLOCK:
                                pending = _block(stream, kid_size, cluster_time,
                                                 scale, number, codec)
                            elif kid == _BLOCK_DURATION:
                                duration = _read_uint(stream.read(kid_size))
                            stream.seek(kid_start + kid_size)
                        if pending:
                            if duration:
                                pending["end"] = (pending["start"]
                                                  + duration * scale / 1e9)
                            cues.append(pending)
                    stream.seek(child_start + child_size)
        if time.monotonic() > deadline:
            return ""            # a partial episode is worse than none
        return _as_srt(cues)
    except Exception:
        return ""
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _block(stream, size, cluster_time, scale, wanted, codec):
    """One SimpleBlock/Block, as a cue, or None when it is another
    track's."""
    here = stream.tell()
    track = _read_vint(stream)
    if track != wanted:
        stream.seek(here)
        return None
    header = stream.read(3)              # int16 relative time + flags
    if len(header) != 3:
        return None
    relative = int.from_bytes(header[:2], "big", signed=True)
    payload = stream.read(size - (stream.tell() - here))
    text = payload.decode("utf-8", "replace").strip("\0").strip()
    if codec in ("S_TEXT/ASS", "S_TEXT/SSA"):
        text = _ass_text(text)
    if not text:
        return None
    start = (cluster_time + relative) * scale / 1e9
    # SimpleBlocks carry no duration; a readable default beats dropping
    # the line, and the translator only ever reads the text anyway.
    return {"start": start, "end": start + 2.0, "text": text}


def _as_srt(cues):
    cues = sorted(cues, key=lambda c: c["start"])
    if not cues:
        return ""
    out = []
    for index, cue in enumerate(cues, start=1):
        out.append(f"{index}\n{_srt_time(cue['start'])} --> "
                   f"{_srt_time(cue['end'])}\n{cue['text']}\n")
    return "\n".join(out)


def usable(path) -> bool:
    """Whether this source is a local file worth opening.

    A stream URL is not: the blocks of a subtitle track run the length of
    the file, so reading one out of a torrent that is still filling in
    would mean fetching the whole episode to translate it."""
    text = str(path or "")
    if not text or "://" in text:
        return False
    try:
        return os.path.isfile(text) and os.path.getsize(text) > 1024
    except OSError:
        return False
