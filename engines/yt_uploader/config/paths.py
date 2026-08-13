import os
import sys


APP_NAME = "YT Uploader"


def get_project_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


ROOT_DIR = get_project_root()
ASSETS_DIR = os.path.join(getattr(sys, "_MEIPASS", ROOT_DIR), "assets")
APPDATA_DIR = os.path.join(ROOT_DIR, "appdata")
LOGS_DIR = os.path.join(APPDATA_DIR, "logs")
DB_PATH = os.path.join(APPDATA_DIR, "yt_uploader.db")
PROFILES_DIR = os.path.join(APPDATA_DIR, "chrome_profiles")
COOKIES_DIR = os.path.join(APPDATA_DIR, "yt_cookies")


def ensure_directories():
    for path in (APPDATA_DIR, LOGS_DIR, PROFILES_DIR, COOKIES_DIR):
        os.makedirs(path, exist_ok=True)


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", ROOT_DIR)
    return os.path.join(base_path, relative_path)


def validate_environment():
    ensure_directories()
    return True
