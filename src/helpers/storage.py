"""Tiny JSON persistence helper shared by the feature windows."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _default_data_dir() -> Path:
    """When PyInstaller freezes this into an EXE, __file__ resolves inside
    a temp extraction folder that's wiped after every run - saving there
    would silently lose all user data on every launch. Use a real,
    persistent per-user folder instead. Running from source (dev) keeps
    using the local data/ folder next to main.py, for convenience."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent / "data"

    base = Path(os.getenv("APPDATA") or Path.home())
    new_dir = base / "Atomic"
    old_dir = base / "PC App"
    if not new_dir.exists() and old_dir.exists():
        # One-time migration: the app used to be called "PC App" - carry
        # existing user data over to the new folder instead of the
        # frozen build silently starting empty after the rename.
        try:
            old_dir.rename(new_dir)
        except OSError:
            pass
    return new_dir


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(filename: str, default):
    path = DATA_DIR / filename
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save(filename: str, data):
    path = DATA_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
