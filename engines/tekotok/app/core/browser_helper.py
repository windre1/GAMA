import os
from app.core.path_helper import get_exe_dir, resource_path


BUNDLED_BROWSER_CANDIDATES = [
    os.path.join("browser", "chrome-win", "chrome.exe"),
    os.path.join("browser", "chrome.exe"),
    os.path.join("browser", "chrome", "chrome.exe"),
    os.path.join("browser", "msedge.exe"),
    os.path.join("browser", "chrome-win64", "chrome.exe"),
    os.path.join("browser", "chrome-win32", "chrome.exe"),
]

SYSTEM_BROWSER_CANDIDATES = [
    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
]


def _existing_path(candidates):
    seen = set()
    for path in candidates:
        if not path:
            continue
        normalized = os.path.abspath(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.exists(normalized):
            return normalized
    return None


def get_bundled_browser_candidates():
    candidates = []
    exe_dir = get_exe_dir()
    for rel in BUNDLED_BROWSER_CANDIDATES:
        candidates.append(os.path.join(exe_dir, rel))
        candidates.append(resource_path(rel))
    return candidates


def get_browser_exe():
    bundled = _existing_path(get_bundled_browser_candidates())
    if bundled:
        return bundled
    return _existing_path(SYSTEM_BROWSER_CANDIDATES)


def get_browser_label(browser_path=None):
    browser_path = browser_path or get_browser_exe()
    if not browser_path:
        return "browser tidak ditemukan"
    lower = browser_path.lower()
    if "edge" in lower:
        return "Microsoft Edge"
    if "chrome" in lower:
        if "browser" in lower or "chrome-win" in lower:
            return "Bundled Chromium"
        return "Google Chrome"
    return os.path.basename(browser_path)


def get_browser_debug_sources():
    return get_bundled_browser_candidates() + SYSTEM_BROWSER_CANDIDATES
