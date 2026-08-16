"""Why isn't Crunchyroll progress showing up?

Run:  python packaging/diagnose_crunchyroll.py

Uses the token already saved in Settings - no password, nothing typed.
Reads your real data directory but writes nothing to it.

It answers, in order, the three things that can be wrong:
  1. is the token still valid (they expire in under an hour)
  2. does the history actually come back, and in the shape expected
  3. is each tracked entry pointed at Crunchyroll, which is what makes
     Atomic ask Crunchyroll for it at all
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helpers import anime_sites, app_settings, crunchyroll, storage, title_match

print(f"data directory: {storage.DATA_DIR}\n")

token = app_settings.get_crunchyroll_token()
if not token:
    print("No Crunchyroll token saved. Settings > Crunchyroll Account.")
    raise SystemExit(1)
print(f"1. token saved, {len(token)} characters, starts {token[:8]}...")

try:
    session = crunchyroll.session_from_token(token)
    print(f"   still valid - account {session['account_id']}")
except Exception as exc:
    print(f"   REJECTED: {exc}")
    print("   -> paste a fresh token; they expire in under an hour.")
    raise SystemExit(1)

print("\n2. watch history")
try:
    crunchyroll.forget_cached_history()
    history = crunchyroll.fetch_history(session)
except Exception as exc:
    print(f"   FAILED: {type(exc).__name__}: {exc}")
    print("   -> the history endpoint or its response shape has changed.")
    raise SystemExit(1)

if not history:
    print("   came back EMPTY - nothing parsed out of the response.")
    print("   -> either the account has no history, or the response shape")
    print("      differs from what fetch_history expects.")
else:
    print(f"   {len(history)} episodes:")
    for item in history[:10]:
        print(f"     S{item['season']:02d}E{item['episode']:02d}  {item['title']}")

print("\n3. tracked Anime entries, and whether Atomic will ask Crunchyroll")
providers = anime_sites.streaming_provider_map()
entries = [e for e in storage.load("tracker.json", [])
           if e.get("type") in ("Anime", "Series")]
if not entries:
    print("   no Anime/Series entries tracked.")
for entry in entries:
    provider = providers.get(entry.get("site_id"))
    site = anime_sites.get_site(entry.get("site_id")) if entry.get("site_id") else None
    site_name = site["name"] if site else "(no Video Website set)"
    asks = "YES" if provider == "crunchyroll" else "no"
    best = None
    for item in history:
        score = title_match.similarity(entry.get("title", ""), item["title"])
        if best is None or score > best[0]:
            best = (score, item)
    print(f"   {entry.get('title', '?')[:32]:34} site={site_name[:20]:22} "
          f"asks Crunchyroll: {asks}")
    if best:
        score, item = best
        verdict = "matches" if score >= 0.8 else "TOO DIFFERENT to match"
        print(f"      closest in history: {item['title'][:34]:36} "
              f"score {score:.2f} - {verdict}")

print("\nIf 'asks Crunchyroll' is 'no', that entry's Video Website isn't a\n"
      "Crunchyroll site - set it in Edit, and Atomic will start asking.")
