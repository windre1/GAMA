import hashlib
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from omnivora.environment import runtime_data_root
from omnivora import regions as _regions


AUTH_COOKIES = {"SPC_EC", "SPC_ST"}
_ACTIVE_PAGES = {}


def get_chrome_exe():
    env = os.environ.get("GAMA_BROWSER")
    if env and os.path.isfile(env):
        return env
    project_root = Path(__file__).resolve().parent.parent
    paths = [
        project_root / "assets" / "chrome" / "chrome.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for path in paths:
        if path.exists():
            return str(path)
    return ""


def shopee_domain(region):
    return _regions.web_domain(region)


def shopee_home(region):
    return _regions.web_home(region)


def affiliate_home(region):
    return _regions.affiliate_home(region)


def profile_dir(region):
    base = runtime_data_root() / "data" / "shopee_profiles"
    base.mkdir(parents=True, exist_ok=True)
    return base / _regions.normalize(region)


def debug_port(region):
    # Rentang 9300-9499, sengaja TIDAK overlap dengan port Google Flow
    # (video_generator.get_debug_port: 9600+) supaya tidak pernah nyambung /
    # membunuh browser milik sesi lain.
    key = f"omnivora-shopee-{_regions.normalize(region)}"
    return 9300 + int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % 200


def format_cookie_header(cookies, domain):
    pairs = []
    seen = set()
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        cookie_domain = (cookie.get("domain") or "").lstrip(".")
        if not name or value is None:
            continue
        if domain not in cookie_domain and cookie_domain not in domain:
            continue
        if name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def has_login_cookie(cookie_header):
    if not cookie_header:
        return False
    names = {part.split("=", 1)[0].strip() for part in cookie_header.split(";") if "=" in part}
    # Shopee sets many guest/session cookies before login. Only treat strong
    # account auth cookies as logged-in, otherwise Chrome closes before QR scan.
    return bool(AUTH_COOKIES.intersection(names))


class ShopeeLoginSession:
    def __init__(self, region, log_cb=None):
        self.region = _regions.normalize(region)
        self.domain = shopee_domain(self.region)
        self.profile_path = profile_dir(self.region)
        self.port = debug_port(self.region)
        self.log_cb = log_cb or (lambda message, level="info": None)
        self.page = None

    def log(self, message, level="info"):
        self.log_cb(message, level)

    def inject_cookies_if_needed(self, page):
        cookie_env = f"SHOPEE_COOKIE_{self.region.upper()}"
        cookie_str = os.environ.get(cookie_env, "")
        if cookie_str:
            self.log(f"Menginjeksi cookie Shopee {self.region.upper()} ke browser...", "info")
            try:
                url = getattr(page, "url", "") or ""
                if "shopee" not in url:
                    page.get(shopee_home(self.region), timeout=10)
                page.set.cookies(cookie_str)
            except Exception as e:
                self.log(f"Gagal menginjeksi cookie: {e}", "warning")

    def _remember_page(self, page):
        self.page = page
        _ACTIVE_PAGES[self.region] = page
        return page

    def _debugger_ready(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1.5)
            return True
        except Exception:
            return False

    def _wait_debugger(self):
        for _ in range(20):
            time.sleep(0.5)
            if self._debugger_ready():
                return True
        return False

    def _cleanup_stale_profile_locks(self):
        if self._debugger_ready():
            return
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = self.profile_path / name
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

    def _launch_chrome(self, url):
        chrome = get_chrome_exe()
        if not chrome:
            raise RuntimeError("Google Chrome tidak ditemukan. Install Chrome dulu untuk login session.")

        self.profile_path.mkdir(parents=True, exist_ok=True)
        if self._debugger_ready():
            self.log("Chrome session Shopee sudah aktif, memakai browser yang berjalan.", "info")
            return

        self._cleanup_stale_profile_locks()
        # Bersihkan sisa extension pihak ketiga (IDM Integration Module, McAfee
        # WebAdvisor, dll) yang di-inject sistem-wide ke profil Chrome.
        try:
            from omnivora.video_generator import scrub_profile_extensions
            scrub_profile_extensions(self.profile_path, log_cb=self.log_cb)
        except Exception:
            pass
        cmd = [
            chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_path}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-notifications",
            "--disable-popup-blocking",
            # Blokir SEMUA extension (IDM/McAfee/antivirus lain) supaya tidak
            # pernah muncul & mengganggu otomasi di browser session Shopee.
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-default-apps",
            "--disable-sync",
            "--start-maximized",
            url,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        if not self._wait_debugger():
            if proc.poll() is not None:
                raise RuntimeError("Chrome session tertutup saat dibuka. Tutup Chrome session lama lalu coba lagi.")
            raise RuntimeError("Chrome session tidak merespons. Tutup Chrome session lama lalu coba lagi.")

    def _connect_page(self):
        try:
            from DrissionPage import ChromiumOptions, ChromiumPage
        except Exception as exc:
            raise RuntimeError("Dependency DrissionPage belum terinstall. Jalankan: pip install DrissionPage") from exc

        options = ChromiumOptions()
        options.set_local_port(self.port)
        return self._remember_page(ChromiumPage(options))

    def read_cookie_header(self, page):
        cookies = page.cookies(all_domains=True, all_info=True)
        return format_cookie_header(cookies, self.domain)

    def _warm_login_cookies(self, page, timeout=12):
        try:
            if has_login_cookie(self.read_cookie_header(page)):
                return True
        except Exception:
            pass

        home_url = shopee_home(self.region)
        self.log(f"Memuat session Shopee {self.region.upper()} sebelum buka Affiliate...", "info")
        try:
            page.get(home_url, timeout=20)
        except Exception:
            pass

        started = time.time()
        while time.time() - started < timeout:
            try:
                if has_login_cookie(self.read_cookie_header(page)):
                    self.log(f"Session Shopee {self.region.upper()} aktif.", "success")
                    return True
            except Exception:
                pass
            time.sleep(1)

        self.log(
            f"Cookie login Shopee {self.region.upper()} belum terbaca dari profile. Affiliate tetap dibuka di browser session yang sama.",
            "warning",
        )
        return False

    def login_and_capture(self, timeout=300, status_cb=None):
        status = status_cb or (lambda message: None)
        login_url = f"{shopee_home(self.region)}/buyer/login"
        self.log(f"Membuka Chrome login Shopee {self.region.upper()}...", "info")
        self._launch_chrome(login_url)
        page = self._connect_page()
        page.get(login_url)
        status("Silakan scan QR / OTP di Chrome yang terbuka.")
        started = time.time()
        while time.time() - started < timeout:
            try:
                cookie_header = self.read_cookie_header(page)
                if has_login_cookie(cookie_header):
                    status("Login terdeteksi. Menyimpan session. Browser tetap dibuka.")
                    time.sleep(1)
                    self._remember_page(page)
                    return cookie_header
            except Exception:
                pass
            time.sleep(2)

        status("Login belum terdeteksi. Browser tetap dibuka untuk dicek manual.")
        raise TimeoutError("Login Shopee belum terdeteksi dalam 5 menit.")

    def open_url(self, url=None):
        target = url or shopee_home(self.region)
        try:
            page = self._connect_page()
        except Exception:
            self._launch_chrome(target)
            page = self._connect_page()

        try:
            page.get(target, timeout=20)
        except Exception:
            self._launch_chrome(target)
            page = self._connect_page()
            page.get(target, timeout=20)

        self.inject_cookies_if_needed(page)
        try:
            page.get(target, timeout=20)
        except Exception:
            pass
        return True

    def open_affiliate(self, url=None):
        target = url or affiliate_home(self.region)
        if not self._debugger_ready():
            self._launch_chrome(shopee_home(self.region))

        page = self._connect_page()
        self.inject_cookies_if_needed(page)
        self._warm_login_cookies(page)

        try:
            tab = page.new_tab(target)
            self._remember_page(page)
            try:
                page.activate_tab(tab)
            except Exception:
                pass
            return True
        except Exception:
            pass

        try:
            page.get(target, timeout=25)
        except Exception as exc:
            self.log(f"Affiliate dibuka, tapi halaman lambat merespons: {exc}", "warning")
        self._remember_page(page)
        return True
