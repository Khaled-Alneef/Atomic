"""Translating a subtitle track into Arabic with an LLM.

This exists because of a measurement, not a preference. Arabic subtitle
*files* for seasonal anime do not exist in any source reachable from
here - OpenSubtitles, SubtitleCat, AnimeTosho and seven dead sites all
return nothing, and the best multi-subtitle releases carry English,
Spanish, Portuguese, French, German and Italian tracks with no Arabic
among them. Waiting for somebody to publish Arabic for one specific
episode is waiting for something that does not happen.

Translating the English track does not depend on that. Every release has
English, so every episode can have Arabic.

How it is done matters as much as that it is done:

  * **In batches with surrounding context, never line by line.** A
    subtitle line is a fragment - half a sentence, a name, a reply to
    something said four lines earlier. Translated alone it comes back
    grammatically fine and meaningless. Each request carries a run of
    lines together so the model can see the conversation.
  * **Numbered in, numbered out.** The model is asked for one line per
    input index, and the reply is matched back by index. If a batch
    comes back the wrong length it is retried once and then failed
    rather than silently sliding every subsequent line out of sync -
    an off-by-one here would put the wrong words on every remaining
    frame of the episode.
  * **On demand and cached.** Nothing is translated until asked for, and
    a finished translation is written to disk keyed by the source
    subtitle's own hash, so a re-watch costs nothing and switching away
    and back does not pay twice.

Keys come from the user in Settings (`app_settings.API_KEYS`). No key,
no translator - the option simply is not offered.
"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import app_settings, net, storage

# Provider -> (endpoint, model, how the request is shaped). Kept as data
# so adding one is a row, not a new code path.
PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "style": "openai",
        "label": "OpenAI",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "style": "openai",          # DeepSeek speaks the OpenAI shape
        "label": "DeepSeek",
    },
    "gemini": {
        # gemini-3.6-flash, not gemini-2.0-flash. The old id is retired:
        # measured 21 August 2026 against the owner's own key, it answers
        # 404 "This model models/gemini-2.0-flash is no longer available.
        # Please update your code to use models/gemini-3.6-flash", and
        # gemini-2.5-flash is closed to new users with the same message.
        # gemini-3.6-flash answered in 3.25s and parsed cleanly.
        "url": ("https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.6-flash:generateContent"),
        "model": "gemini-3.6-flash",
        "style": "gemini",
        "label": "Google Gemini",
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        # claude-3-5-haiku-20241022 is retired. claude-haiku-4-5 is the
        # current cheap, fast model, which is what this job wants - it is
        # a translation, not a reasoning task. Not verified live: the
        # owner's Anthropic key answers 400 "credit balance is too low"
        # before a model id is ever looked at.
        "model": "claude-haiku-4-5",
        "style": "anthropic",
        "label": "Anthropic",
    },
}

# Lines per request. Large enough that the model sees a conversation,
# small enough that one failure costs a few seconds and that the reply
# stays inside a sane token budget.
BATCH_SIZE = 40
REQUEST_TIMEOUT = 90
MAX_RETRIES = 1

CACHE_FILE = "ai_subtitle_cache.json"

_SYSTEM = (
    "You are translating subtitles for an anime episode into Modern "
    "Standard Arabic. Rules: translate each numbered line and return the "
    "same numbers with the same count. Keep each line short enough to "
    "read on screen. Preserve names, honorifics and terminology "
    "consistently across lines. Do not add commentary, notes, "
    "romanisation or explanations. If a line is a sound effect or is "
    "already Arabic, return it unchanged. Reply as JSON: "
    '{"lines": [{"i": <number>, "t": "<arabic>"}]}'
)


class TranslationFailed(Exception):
    """Why the translation did not happen, in words a person can act on.

    Modelled on `anilist.RateLimited` and for the same reason: this used
    to return None for every possible failure, and the player said
    "Translation Failed - Loading the Original" with nothing else. The
    owner had four keys pasted, saw that message, and had no way to learn
    that three accounts were out of credit and the fourth named a model
    Google had retired. A silent failure that looks like a bug in the
    feature is worse than the feature saying what is wrong."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _reason_for(provider, error) -> str:
    """One HTTP failure, said in the user's terms.

    The bodies quoted here were all measured on 21 August 2026 against
    the owner's own keys - every provider failed, each differently, and
    none of it reached the screen."""
    name = label(provider)
    if isinstance(error, urllib.error.HTTPError):
        body = ""
        try:
            body = error.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        lowered = body.lower()
        if error.code in (401, 403) or "invalid_api_key" in lowered:
            return f"{name} rejected the key"
        if (error.code == 402 or "insufficient_quota" in lowered
                or "insufficient balance" in lowered
                or "credit balance is too low" in lowered):
            return f"{name} has no credit left"
        if error.code == 429:
            return f"{name} is rate limiting or out of quota"
        if error.code == 404 and "model" in lowered:
            # Google names the replacement in the body; carrying it
            # through means the next report says what to change to.
            return f"{name} has retired this model"
        if error.code >= 500:
            return f"{name} is down ({error.code})"
        return f"{name} refused the request ({error.code})"
    if isinstance(error, urllib.error.URLError):
        return f"{name} could not be reached"
    return f"{name} failed ({type(error).__name__})"


def providers_available() -> list:
    """Which translators the user has actually supplied a key for."""
    return [name for name in PROVIDERS if app_settings.get_api_key(name)]


def available() -> bool:
    return bool(providers_available())


def default_provider() -> str:
    """Whichever configured provider comes first in PROVIDERS order.

    Order is deliberate rather than alphabetical: the first two are the
    cheapest per token for this job, so a user with several keys is not
    billed for the dearest by accident."""
    found = providers_available()
    return found[0] if found else ""


def label(provider: str) -> str:
    return (PROVIDERS.get(provider) or {}).get("label", provider)


# ------------------------------------------------------------- cache

def _cache_key(cues, provider) -> str:
    """Identity of *this* subtitle translated by *this* provider.

    Hashed from the source text rather than the episode id: two releases
    of the same episode have different subtitles, and a cache keyed by
    episode would hand the wrong timings to the second one."""
    digest = hashlib.sha256()
    digest.update(provider.encode())
    for cue in cues:
        digest.update(f"{cue['start']:.3f}|{cue['text']}\n".encode("utf-8"))
    return digest.hexdigest()[:32]


def cached(cues, provider):
    store = storage.load(CACHE_FILE, {})
    if not isinstance(store, dict):
        return None
    row = store.get(_cache_key(cues, provider))
    return row.get("cues") if isinstance(row, dict) else None


def _store(cues, provider, translated):
    store = storage.load(CACHE_FILE, {})
    if not isinstance(store, dict):
        store = {}
    store[_cache_key(cues, provider)] = {"at": time.time(), "cues": translated}
    # Bounded: a handful of episodes is useful, a year of them is not.
    if len(store) > 40:
        for stale in sorted(store, key=lambda k: store[k].get("at", 0))[:len(store) - 40]:
            store.pop(stale, None)
    storage.save(CACHE_FILE, store)


# ------------------------------------------------------------ requests

def _post(url, payload, headers, timeout):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    deadline = net.deadline_in(timeout)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(net.read_text(response, deadline, max_bytes=4_000_000))


def _ask(provider, key, numbered, timeout):
    """One batch to one provider; returns the raw reply text."""
    spec = PROVIDERS[provider]
    prompt = ("Translate these subtitle lines to Arabic.\n\n"
              + "\n".join(f"{i}. {text}" for i, text in numbered))
    style = spec["style"]

    if style == "openai":
        body = _post(spec["url"], {
            "model": spec["model"],
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }, {"Authorization": f"Bearer {key}"}, timeout)
        return body["choices"][0]["message"]["content"]

    if style == "anthropic":
        body = _post(spec["url"], {
            "model": spec["model"],
            "max_tokens": 8000,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        return body["content"][0]["text"]

    # gemini
    body = _post(f"{spec['url']}?key={urllib.parse.quote(key)}", {
        "systemInstruction": {"parts": [{"text": _SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2,
                             "responseMimeType": "application/json"},
    }, {}, timeout)
    return body["candidates"][0]["content"]["parts"][0]["text"]


def _parse(reply, expected_indexes):
    """Reply -> {index: arabic}. Tolerant of a model wrapping its JSON in
    prose or a code fence, which they all do occasionally."""
    text = (reply or "").strip()
    if "```" in text:
        parts = text.split("```")
        text = max(parts, key=len).lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except Exception:
        return {}
    out = {}
    for row in (data.get("lines") or []):
        try:
            index = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        value = str(row.get("t") or "").strip()
        if index in expected_indexes and value:
            out[index] = value
    return out


def translate(cues, *, provider=None, progress=None, cancelled=None):
    """Arabic versions of `cues`.

    Raises `TranslationFailed` with a reason rather than returning None
    on failure; returns None only when there is nothing to do or the work
    was cancelled. Every configured provider is tried in turn, starting
    with the one asked for - the owner has four keys pasted and three
    accounts out of credit, and giving up on the first is how a working
    fourth key went unused.

    `progress(done, total)` is called as batches complete so the player
    can say how far along it is - a 24 minute episode is 300-400 lines
    and takes tens of seconds, which is far too long to show nothing.
    `cancelled()` is polled between batches so leaving the episode stops
    the work rather than paying for the rest of it."""
    if not cues:
        return None
    order = []
    for name in ([provider] if provider else []) + providers_available():
        if name and name not in order and app_settings.get_api_key(name):
            order.append(name)
    if not order:
        return None

    reasons = []
    for name in order:
        try:
            return _translate_with(name, cues, progress, cancelled)
        except _Cancelled:
            return None
        except TranslationFailed as failure:
            reasons.append(failure.reason)
    raise TranslationFailed("; ".join(reasons))


class _Cancelled(Exception):
    """The episode was left. Not a failure - nothing to report."""


def _translate_with(provider, cues, progress, cancelled):
    key = app_settings.get_api_key(provider)
    ready = cached(cues, provider)
    if ready:
        if progress:
            progress(len(cues), len(cues))
        return ready

    translated = {}
    total = len(cues)
    for start in range(0, total, BATCH_SIZE):
        if cancelled and cancelled():
            raise _Cancelled()
        batch = [(i, cues[i]["text"]) for i in range(start, min(start + BATCH_SIZE, total))]
        wanted = {i for i, _ in batch}
        got = {}
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                got = _parse(_ask(provider, key, batch, REQUEST_TIMEOUT), wanted)
            except Exception as error:
                last_error, got = error, {}
            # Every line back, or try once more. A short batch means the
            # model dropped lines, and accepting it would leave gaps.
            if len(got) == len(wanted):
                break
        if not got:
            raise TranslationFailed(
                _reason_for(provider, last_error) if last_error is not None
                else f"{label(provider)} returned nothing usable")
        translated.update(got)
        if progress:
            progress(min(start + BATCH_SIZE, total), total)

    out = []
    for index, cue in enumerate(cues):
        text = translated.get(index)
        # A line the model dropped keeps its original rather than
        # vanishing - a missing subtitle reads as a bug, where one
        # untranslated line reads as what it is.
        out.append({"start": cue["start"], "end": cue["end"],
                    "text": text or cue["text"]})
    _store(cues, provider, out)
    return out


def to_srt(cues) -> str:
    """Cues back out as an .srt the player can hand to mpv."""
    def stamp(seconds):
        seconds = max(0.0, float(seconds))
        hours, rest = divmod(int(seconds), 3600)
        minutes, secs = divmod(rest, 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    blocks = []
    for number, cue in enumerate(cues or [], 1):
        blocks.append(f"{number}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n"
                      f"{cue['text']}\n")
    return "\n".join(blocks)

