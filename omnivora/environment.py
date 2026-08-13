import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


HELPER_PACKAGES = [
    "io.appium.settings",
    "io.appium.uiautomator2.server",
    "io.appium.uiautomator2.server.test",
]

from omnivora import regions as _regions

SHOPEE_PACKAGES = {code: _regions.package_for(code) for code in _regions.REGION_ORDER}

DEFAULT_CONFIG = {
    "adb_path": "",
    "android_sdk_root": "",
    "appium_command": "",
    "appium_url": "http://localhost:4723",
    "ai_provider": "Gemini",
    "gemini_model": "gemini-2.0-flash",
    "gemini_api_key": "",
    "gemini_api_keys": [],
    "groq_model": "llama-3.3-70b-versatile",
    "groq_api_keys": [],
    "shopee_cookie_id": "",
    "shopee_cookie_sg": "",
    "shopee_cookie_ph": "",
    "shopee_cookie_th": "",
    "shopee_cookie_my": "",
    "license_key": "",
    "license_api_url": "https://www.shove.my.id/api/license/verify",
    "license_reset_api_url": "https://www.shove.my.id/api/license/reset",
}


SERVER_MANIFEST_NAME = "server.json"


def hidden_subprocess_kwargs():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def app_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root():
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()
    return app_root()


def user_data_root():
    """Folder data per-user yang DIJAMIN bisa ditulis saat runtime.

    Build terinstall berada di ``C:\\Program Files\\OMNIVORA`` (lihat setup.iss),
    yang read-only untuk user biasa. Menulis profil Chrome / cache ke ``app_root()``
    di situ gagal senyap (atau dialihkan UAC VirtualStore secara tidak konsisten
    antar-PC) — inilah penyebab login Google VO gagal di sebagian PC. Semua tulisan
    runtime harus lewat sini, bukan ``app_root()``.
    """
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    base = Path(local) / "OMNIVORA"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


def dir_writable(path):
    """True kalau folder ``path`` bisa dibuat & ditulis oleh proses ini."""
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".omnivora_write_test_{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


_RUNTIME_DATA_ROOT = None


def runtime_data_root():
    """Root untuk semua tulisan runtime (profil Chrome, log, output, download).

    Pakai ``app_root()`` bila writable (mode dev / install portable); kalau tidak
    (mis. terinstall di Program Files) jatuh ke ``user_data_root()``.
    """
    global _RUNTIME_DATA_ROOT
    if _RUNTIME_DATA_ROOT is None:
        root = app_root()
        _RUNTIME_DATA_ROOT = root if dir_writable(root / "data") else user_data_root()
    return _RUNTIME_DATA_ROOT


class EnvironmentManager:
    def __init__(self):
        self.root = app_root()
        self.bundle_root = bundle_root()
        # Config harus di folder yang bisa ditulis: install di Program Files
        # read-only, jadi jatuh ke %LOCALAPPDATA%\OMNIVORA\configs. Config lama
        # di folder install (bila ada) dimigrasikan sekali.
        primary_dir = self.root / "configs"
        if dir_writable(primary_dir):
            self.config_dir = primary_dir
        else:
            self.config_dir = user_data_root() / "configs"
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
                old_cfg = primary_dir / "environment.json"
                new_cfg = self.config_dir / "environment.json"
                if old_cfg.exists() and not new_cfg.exists():
                    shutil.copy2(old_cfg, new_cfg)
            except Exception:
                pass
        self.config_path = self.config_dir / "environment.json"
        self.config = self.load_config()

    def global_config_dir(self):
        if os.name == "nt":
            base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
            return Path(base) / "OMNIVORA"
        return Path.home() / ".OMNIVORA"

    def user_config_dir(self):
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return Path(base) / "OMNIVORA"
        return Path.home() / ".OMNIVORA"

    def server_manifest_paths(self):
        paths = [
            self.global_config_dir() / SERVER_MANIFEST_NAME,
            self.user_config_dir() / SERVER_MANIFEST_NAME,
            self.root / "server.json",
            self.root / "configs" / SERVER_MANIFEST_NAME,
        ]
        configured = self.config.get("server_manifest")
        if configured:
            paths.insert(0, self.resolve_path(configured))
        unique = []
        seen = set()
        for path in paths:
            key = str(path).lower()
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    def load_server_manifest(self):
        for path in self.server_manifest_paths():
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data["_manifest_path"] = str(path)
                        return data
            except Exception:
                continue
        return {}

    def _first_existing(self, paths):
        for path in paths:
            if path and Path(path).exists():
                return str(Path(path))
        return ""

    def local_adb_path(self):
        adb_name = "adb.exe" if os.name == "nt" else "adb"
        return self._first_existing([
            self.bundle_root / "tools" / "platform-tools" / adb_name,
            self.root / "tools" / "platform-tools" / adb_name,
        ])

    def local_appium_command(self):
        suffixes = ("appium.cmd", "appium.exe", "appium.ps1", "appium") if os.name == "nt" else ("appium",)
        candidates = []
        for base in (self.bundle_root, self.root):
            for name in suffixes:
                candidates.append(base / "tools" / "appium" / name)
        return self._first_existing(candidates)

    def write_server_manifest(self, values=None):
        values = values or {}
        install_path = values.get("install_path") or str(self.root)
        appium_command = values.get("appium_command") or self.local_appium_command() or self.find_appium_command()
        adb_path = values.get("adb_path") or self.local_adb_path() or self.find_adb()
        android_sdk_root = values.get("android_sdk_root") or self.infer_sdk_root(adb_path)
        helper_dir = values.get("helper_dir")
        if not helper_dir:
            bundled_helpers = self.bundle_root / "appium_helpers"
            root_helpers = Path(install_path) / "appium_helpers"
            helper_dir = str(bundled_helpers if bundled_helpers.exists() else root_helpers)
        data = {
            "install_path": install_path,
            "adb_path": adb_path,
            "android_sdk_root": android_sdk_root,
            "appium_command": appium_command,
            "appium_url": values.get("appium_url") or self.config.get("appium_url") or "http://localhost:4723",
            "appium_home": values.get("appium_home") or self.portable_appium_home(appium_command),
            "helper_dir": helper_dir,
        }
        last_error = None
        for config_dir in (self.global_config_dir(), self.user_config_dir(), self.root / "configs"):
            manifest_path = config_dir / SERVER_MANIFEST_NAME
            try:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                data["_manifest_path"] = str(manifest_path)
                return data
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return data

    def load_config(self):
        if not self.config_path.exists():
            self.config_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
            except Exception:
                pass
            return dict(DEFAULT_CONFIG)
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                deprecated_ai_provider = "open" + "agentic"
                for key in ("base_url", "api_key", "model"):
                    loaded.pop(f"{deprecated_ai_provider}_{key}", None)
                if str(loaded.get("ai_provider", "")).lower() == deprecated_ai_provider:
                    loaded["ai_provider"] = "Gemini"
            config = dict(DEFAULT_CONFIG)
            config.update(loaded if isinstance(loaded, dict) else {})
            return config
        except Exception:
            return dict(DEFAULT_CONFIG)

    def save_config(self, values):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config.update({k: v for k, v in values.items() if v is not None})
        self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        self.apply_process_environment()
        return self.config

    def resolve_path(self, value):
        if not value:
            return Path("")
        path = Path(value)
        if path.is_absolute():
            return path
        return self.root / path

    def adb_candidates(self):
        candidates = []

        manifest = self.load_server_manifest()
        if manifest.get("adb_path"):
            candidates.append(Path(manifest["adb_path"]))
        if manifest.get("install_path"):
            install_path = Path(manifest["install_path"])
            candidates.append(install_path / "tools" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))
            candidates.append(install_path / "_internal" / "tools" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))

        configured = self.config.get("adb_path")
        if configured:
            candidates.append(self.resolve_path(configured))

        for base in (self.root, self.bundle_root):
            candidates.append(base / "tools" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))

        common = [
            Path(r"C:\Program Files\platform-tools\adb.exe"),
            Path(r"C:\Users\Windre\AndroidSdk\platform-tools\adb.exe"),
            Path.home() / "AndroidSdk" / "platform-tools" / "adb.exe",
            Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
            Path(r"C:\Program Files (x86)\Minimal ADB and Fastboot\adb.exe"),
        ]
        candidates.extend(common)

        path_adb = shutil.which("adb")
        if path_adb:
            candidates.append(Path(path_adb))

        unique = []
        seen = set()
        for item in candidates:
            key = str(item).lower()
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def find_adb(self):
        for candidate in self.adb_candidates():
            if candidate.exists():
                return str(candidate)
        return ""

    def infer_sdk_root(self, adb_path):
        configured = self.config.get("android_sdk_root")
        configured_path = self.resolve_path(configured)
        if configured and configured_path.exists():
            return str(configured_path)

        manifest = self.load_server_manifest()
        manifest_sdk = manifest.get("android_sdk_root")
        if manifest_sdk and Path(manifest_sdk).exists():
            return str(Path(manifest_sdk))

        for env_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
            env_value = os.environ.get(env_name)
            if env_value and Path(env_value).exists():
                return env_value

        if adb_path:
            adb = Path(adb_path)
            if adb.name.lower() in ("adb.exe", "adb") and adb.parent.name.lower() == "platform-tools":
                sdk = adb.parent.parent
                if sdk.exists():
                    return str(sdk)
        return ""

    def apply_process_environment(self):
        adb_path = self.find_adb()
        sdk_root = self.infer_sdk_root(adb_path)

        if sdk_root:
            os.environ["ANDROID_HOME"] = sdk_root
            os.environ["ANDROID_SDK_ROOT"] = sdk_root

        if adb_path:
            platform_tools = str(Path(adb_path).parent)
            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if platform_tools not in path_parts:
                os.environ["PATH"] = platform_tools + os.pathsep + os.environ.get("PATH", "")

        return adb_path, sdk_root

    def persist_windows_environment(self, adb_path=None, sdk_root=None):
        adb_path = adb_path or self.config.get("adb_path") or self.find_adb()
        sdk_root = sdk_root or self.config.get("android_sdk_root") or self.infer_sdk_root(adb_path)
        if os.name != "nt":
            return False, "Persist environment hanya tersedia otomatis di Windows."
        if not sdk_root:
            return False, "Android SDK root belum valid."

        commands = [
            ["setx", "ANDROID_HOME", sdk_root],
            ["setx", "ANDROID_SDK_ROOT", sdk_root],
        ]
        try:
            for cmd in commands:
                subprocess.run(cmd, capture_output=True, text=True, timeout=20, **hidden_subprocess_kwargs())
            if adb_path:
                platform_tools = str(Path(adb_path).parent)
                current_path = os.environ.get("PATH", "")
                if platform_tools.lower() not in current_path.lower():
                    subprocess.run(
                        ["setx", "PATH", current_path + os.pathsep + platform_tools],
                        capture_output=True,
                        text=True,
                        timeout=20,
                        **hidden_subprocess_kwargs(),
                    )
            return True, "Environment Windows disimpan. Restart Appium dan OMNIVORA agar semua proses membaca path baru."
        except Exception as exc:
            return False, str(exc)

    def bundle_detected_adb(self, adb_path=None):
        adb_path = adb_path or self.config.get("adb_path") or self.find_adb()
        if not adb_path or not Path(adb_path).exists():
            return False, "ADB belum ditemukan."

        source_dir = Path(adb_path).parent
        target_dir = self.root / "tools" / "platform-tools"
        target_dir.mkdir(parents=True, exist_ok=True)

        copied = 0
        for item in source_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, target_dir / item.name)
                copied += 1

        bundled_adb = target_dir / Path(adb_path).name
        self.save_config({
            "adb_path": "tools/platform-tools/adb.exe" if os.name == "nt" else "tools/platform-tools/adb",
            "android_sdk_root": "tools",
        })
        return True, f"Bundled ADB siap di {target_dir} ({copied} file). Untuk distribusi baru, masukkan ADB ke paket OMNIVORA Server."

    def helper_directories(self):
        dirs = []
        manifest = self.load_server_manifest()
        if manifest.get("helper_dir"):
            dirs.append(Path(manifest["helper_dir"]))
        if manifest.get("install_path"):
            install_path = Path(manifest["install_path"])
            dirs.append(install_path / "appium_helpers")
            dirs.append(install_path / "_internal" / "appium_helpers")
        for base in (self.root, self.bundle_root):
            dirs.append(base / "appium_helpers")

        unique = []
        seen = set()
        for item in dirs:
            key = str(item).lower()
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def run_adb(self, adb_path, args, timeout=20):
        if not adb_path:
            return "", 1
        try:
            result = subprocess.run(
                [adb_path] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                **hidden_subprocess_kwargs(),
            )
            return ((result.stdout or "") + (result.stderr or "")).strip(), result.returncode
        except Exception as exc:
            return str(exc), 1

    def parse_devices(self, raw):
        devices = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                devices.append({"id": parts[0], "status": parts[1]})
        return devices

    def package_installed(self, adb_path, device_id, package_name):
        args = []
        if device_id:
            args += ["-s", device_id]
        args += ["shell", "pm", "list", "packages", "--user", "0", package_name]
        out, code = self.run_adb(adb_path, args, timeout=8)
        return code == 0 and f"package:{package_name}" in out
