"""Did MAL-Sync's update reach Atomic, and if not, where did it stop?

Run:  python packaging/diagnose_anilist.py

No login, no password - only your public AniList username, read from
Settings. Reads your data directory, writes nothing to it.

The chain has four links and any one of them breaks it silently:
  1. a username is set in Atomic
  2. AniList answers for that username at all (a private list does not)
  3. the title exists on your list
  4. its progress is actually the episode you watched (MAL-Sync only
     writes after ~85% of an episode, not when you open it)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helpers import anilist, app_settings, storage

print(f"data directory: {storage.DATA_DIR}\n")

username = app_settings.get_anilist_username()
if not username:
    print("1. No AniList username set in Settings - nothing can arrive.")
    raise SystemExit(1)
print(f"1. username: {username}")

# Is the list readable at all? A private profile answers nothing, which
# is indistinguishable from "you have not watched anything" unless it is
# asked separately - which is what this does.
print("\n2. is the list public?")
probe_titles = [e.get("title") for e in storage.load("tracker.json", [])
                if e.get("type") in ("Anime", "Series") and e.get("title")]
if not probe_titles:
    print("   no Anime/Series entries tracked - nothing to look up.")
    raise SystemExit(0)

any_answer = False
print("\n3. what AniList says for each tracked title")
for title in probe_titles:
    try:
        found = anilist.fetch_watch_progress(title, username)
    except anilist.RateLimited:
        print("   AniList is rate-limiting this connection (403). Try again")
        print("   in an hour - this says nothing about your list.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"   {title[:34]:36} lookup failed: {type(exc).__name__}")
        continue
    if found is None:
        print(f"   {title[:34]:36} not on your list (or list not public)")
    else:
        any_answer = True
        season, episode = found
        print(f"   {title[:34]:36} episode {episode}")

print()
if any_answer:
    print("AniList is readable and has progress. If Atomic still shows")
    print("something else, press the refresh button on the Anime page -")
    print("and check the entry's Video Website, since a Crunchyroll entry")
    print("asks Crunchyroll first when a token is saved.")
else:
    print("Nothing came back for any title. Either MAL-Sync has not written")
    print("anything yet (it syncs after ~85% of an episode, not on open), or")
    print("your AniList list is not public - Settings > Privacy on AniList,")
    print("'Anime & Manga list' must be visible to everyone for a username")
    print("lookup to see it.")
