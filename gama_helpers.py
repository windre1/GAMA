"""
gama_helpers.py — Fungsi helper ringan yang dipakai worker bridge subprocess.

Dipisah dari main.py supaya bridge (video_generator_bridge, voice_over_bridge,
dll) tidak perlu import seluruh Flask app saat dijalankan sebagai frozen .exe.
Di mode frozen (PyInstaller), import main dari worker exe berarti dekompresi +
load seluruh module graph Flask — bisa lambat 15-30 detik. File ini hanya
import stdlib sehingga startup worker tetap cepat.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

# ── Path dasar ───────────────────────────────────────────────────────────────
BASE_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, 'frozen', False)
    else os.path.dirname(os.path.abspath(__file__))
)
DATA_DIR  = os.path.join(BASE_DIR, 'data')
META_FILE = os.path.join(DATA_DIR, 'profiles_meta.json')

CHROME_PATHS = [
    os.path.join(BASE_DIR, 'assets', 'chrome', 'chrome.exe'),
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    os.path.join(os.environ.get('LOCALAPPDATA', ''), r'Google\Chrome\Application\chrome.exe'),
]

# ── Metadata ─────────────────────────────────────────────────────────────────
_meta_lock       = threading.Lock()
_meta_cache: dict | None = None
_meta_cache_time: float  = 0.0
_META_CACHE_TTL  = 2.0   # detik


def load_meta() -> dict:
    """Load per-profile metadata. Di-cache selama 2 detik."""
    global _meta_cache, _meta_cache_time
    now = time.monotonic()
    if _meta_cache is not None and (now - _meta_cache_time) < _META_CACHE_TTL:
        return _meta_cache
    with _meta_lock:
        try:
            with open(META_FILE, 'r', encoding='utf-8') as f:
                _meta_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _meta_cache = {}
        _meta_cache_time = time.monotonic()
        return _meta_cache


# ── Chrome path & User-Agent ──────────────────────────────────────────────────
_chrome_major_cache: int | None = None


def get_chrome_path() -> str | None:
    env = os.environ.get('GAMA_BROWSER')
    if env and os.path.isfile(env):
        return env
    for p in CHROME_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def _installed_chrome_major() -> int:
    global _chrome_major_cache
    if _chrome_major_cache is not None:
        return _chrome_major_cache
    major = 130
    chrome_path = get_chrome_path()
    if chrome_path:
        try:
            app_dir = os.path.dirname(chrome_path)
            for entry in os.listdir(app_dir):
                m = re.match(r'^(\d+)\.\d+\.\d+\.\d+$', entry)
                if m and os.path.isdir(os.path.join(app_dir, entry)):
                    major = int(m.group(1))
                    break
        except Exception:
            pass
    _chrome_major_cache = major
    return major


def generate_user_agent() -> str:
    """UA Chrome asli yang mengikuti versi Chrome yang terpasang di mesin ini."""
    major = _installed_chrome_major()
    return (
        f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        f'AppleWebKit/537.36 (KHTML, like Gecko) '
        f'Chrome/{major}.0.0.0 Safari/537.36'
    )
