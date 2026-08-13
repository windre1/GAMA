import os
import base64
import json
import re
import time
import hashlib
import shutil
import threading
import urllib.request
import zipfile
import subprocess
from datetime import datetime
from pathlib import Path
from omnivora.environment import runtime_data_root

# Beberapa slot generate berjalan paralel dan sering memakai SATU gambar yang
# sama → folder tujuan (folder tempat foto) juga sama. Penamaan file output
# harus di-serialize supaya dua slot tidak memilih nama yang sama lalu saling
# menimpa (video hilang / "ngacak"). Lock proses-wide menjaga langkah
# "cari nama unik + pesan (reserve) file" bersifat atomik antar-thread.
_OUTPUT_NAMING_LOCK = threading.Lock()

def get_chrome_exe():
    # Prioritas: env GAMA_BROWSER → Chrome yang dibundel installer
    # (<project>/assets/chrome) → Chrome ter-install.
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

def sanitize_email(email):
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', email)


def sanitize_file_stem(name, fallback="hasil"):
    stem = re.sub(r'[\\/*?:"<>|]+', '_', str(name or '')).strip()
    stem = re.sub(r'\s+', '_', stem)
    stem = re.sub(r'[^0-9A-Za-z_.-]+', '_', stem)
    stem = re.sub(r'_+', '_', stem).strip('._-')
    return (stem[:90] or fallback).lower()


def get_profile_dir(email):
    base = runtime_data_root() / "data" / "google_profiles"
    base.mkdir(parents=True, exist_ok=True)
    return base / sanitize_email(email)

def get_debug_port(email):
    # Rentang 9600-12599: tidak overlap dengan port session Shopee (9300-9499)
    # dan cukup lebar supaya peluang dua akun kebagian port sama praktis nol
    # (tabrakan port = DrissionPage nyambung/membunuh browser akun lain).
    key = f"omnivora-google-{email}"
    return 9600 + int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % 3000

def scrub_profile_extensions(profile_root, log_cb=None):
    log_cb = log_cb or (lambda msg, lvl="info": None)
    profile_root = Path(profile_root)
    if not profile_root.exists():
        return False

    cleaned = False
    default_dir = profile_root / "Default"
    removable_dirs = (
        default_dir / "Extensions",
        default_dir / "Extension Rules",
        default_dir / "Extension Scripts",
        default_dir / "Extension State",
        default_dir / "Local Extension Settings",
        default_dir / "Sync Extension Settings",
        default_dir / "Extension Cookies",
        default_dir / "Web Applications",
    )
    removable_files = (
        default_dir / "Extensions Toolbar Menu",
        default_dir / "Extension Cookies-journal",
    )
    for target in removable_dirs:
        if target.exists():
            try:
                shutil.rmtree(target, ignore_errors=True)
                log_cb(f"[{profile_root.name}] Membersihkan folder extension Chrome: {target.name}")
                cleaned = True
            except Exception as exc:
                log_cb(f"[{profile_root.name}] Gagal membersihkan {target.name}: {exc}", "warning")
    for target in removable_files:
        if target.exists():
            try:
                target.unlink()
                log_cb(f"[{profile_root.name}] Membersihkan file extension Chrome: {target.name}")
                cleaned = True
            except Exception as exc:
                log_cb(f"[{profile_root.name}] Gagal membersihkan file {target.name}: {exc}", "warning")

    def _clean_json_file(pref_path, scope_name):
        nonlocal cleaned
        if not pref_path.exists():
            return
        try:
            data = json.loads(pref_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log_cb(f"[{profile_root.name}] Gagal membaca {scope_name}: {exc}", "warning")
            return

        changed = False
        ext = data.get("extensions")
        if isinstance(ext, dict):
            settings = ext.get("settings")
            if isinstance(settings, dict) and settings:
                ext["settings"] = {}
                changed = True
            for key in ("toolbar", "ui", "alerts", "pinned_extensions", "ack_external"):
                if key in ext:
                    ext.pop(key, None)
                    changed = True
        for key in ("extensions_ui", "extension_commands", "external_extensions", "enabled_labs_experiments"):
            if data.pop(key, None) is not None:
                changed = True
        sync_cfg = data.get("sync")
        if isinstance(sync_cfg, dict):
            desired = {
                "keep_everything_synced": False,
                "extensions": False,
                "apps": False,
                "preferences": False,
            }
            for key, value in desired.items():
                if sync_cfg.get(key) != value:
                    sync_cfg[key] = value
                    changed = True
        account_values = data.get("account_info")
        if isinstance(account_values, list):
            for account in account_values:
                if isinstance(account, dict) and account.get("is_child_account") is not None:
                    pass

        # launch_browser() selalu _kill_profile_chrome() dulu (force-kill,
        # bukan graceful) sebelum relaunch - itu bikin Chrome berikutnya kali
        # dibuka mengira "tidak shutdown dengan benar" dan menampilkan popup
        # native "Restore pages?" yang mengganggu (bukan bagian DOM halaman,
        # tidak bisa di-otomasi/di-klik lewat DrissionPage). Paksa tandai
        # exit bersih di sini supaya popup itu tidak pernah muncul.
        if scope_name == "Preferences":
            if data.get("exit_type") != "Normal":
                data["exit_type"] = "Normal"
                changed = True
            if data.get("exited_cleanly") is not True:
                data["exited_cleanly"] = True
                changed = True
            profile_scope = data.get("profile")
            if isinstance(profile_scope, dict):
                if profile_scope.get("exit_type") != "Normal":
                    profile_scope["exit_type"] = "Normal"
                    changed = True
                if profile_scope.get("exited_cleanly") is not True:
                    profile_scope["exited_cleanly"] = True
                    changed = True

        if changed:
            try:
                pref_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                log_cb(f"[{profile_root.name}] Membersihkan konfigurasi extension/sync di {scope_name}.")
                cleaned = True
            except Exception as exc:
                log_cb(f"[{profile_root.name}] Gagal menyimpan {scope_name}: {exc}", "warning")

    # CATATAN: "Secure Preferences" sengaja TIDAK diedit. File itu dilindungi
    # HMAC per-entry oleh Chrome; menulis ulang dari luar merusak MAC-nya dan
    # memicu Chrome mereset setting profil. Extension pihak ketiga sudah
    # diblokir total lewat flag --disable-extensions saat launch.
    _clean_json_file(default_dir / "Preferences", "Preferences")
    _clean_json_file(profile_root / "Local State", "Local State")
    return cleaned

def scrub_all_google_profiles_extensions(log_cb=None, verbose=False):
    """Bersihkan extension di semua profil. verbose=False (default) hanya
    melaporkan ringkasan + warning — detail per-profil untuk ratusan akun
    membanjiri log UI (ratusan event) dan ikut membebani main thread."""
    log_cb = log_cb or (lambda msg, lvl="info": None)
    if verbose:
        detail_cb = log_cb
    else:
        def detail_cb(msg, lvl="info"):
            if lvl != "info":
                log_cb(msg, lvl)
    base = runtime_data_root() / "data" / "google_profiles"
    if not base.exists():
        return 0
    cleaned_count = 0
    for profile_root in sorted(p for p in base.iterdir() if p.is_dir()):
        if scrub_profile_extensions(profile_root, log_cb=detail_cb):
            cleaned_count += 1
    log_cb(f"Pembersihan extension Chrome selesai. Profil dibersihkan: {cleaned_count}.")
    return cleaned_count

class GoogleAccountSession:
    def __init__(self, email, user_agent=None, log_cb=None, window_index=0, hidden=False):
        self.email = email
        self.profile_path = get_profile_dir(email)
        self.port = get_debug_port(email)
        # Kosong = pakai user-agent bawaan Chrome (lebih aman dari deteksi bot)
        self.user_agent = (user_agent or "").strip()
        self.log_cb = log_cb or (lambda msg, lvl="info": None)
        # Slot untuk mengatur posisi window saat banyak browser paralel (biar tidak numpuk)
        self.window_index = max(0, int(window_index or 0))
        # Mode sembunyi: jendela ditaruh JAUH di luar layar supaya tak mengganggu
        # user. Browser tetap render penuh (klik/screenshot via CDP tetap jalan),
        # dan tidak memakai --headless yang rawan deteksi bot Google.
        self.hidden = bool(hidden)
        self.browser_proc = None

    def log(self, msg, lvl="info"):
        self.log_cb(msg, lvl)

    def _cleanup_locks(self):
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = self.profile_path / name
            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass
        scrub_profile_extensions(self.profile_path, log_cb=self.log)

    def _count_profile_chrome(self):
        count = 0
        try:
            import psutil
            target = str(self.profile_path).lower()
            port_token = f"--remote-debugging-port={self.port}".lower()
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    if "chrome" not in name:
                        continue
                    cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                    if target in cmdline or port_token in cmdline:
                        count += 1
                except Exception:
                    pass
        except Exception:
            pass
        return count

    def _kill_profile_chrome(self):
        try:
            import psutil
            target = str(self.profile_path).lower()
            port_token = f"--remote-debugging-port={self.port}".lower()
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    if "chrome" not in name:
                        continue
                    cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                    if target in cmdline or port_token in cmdline:
                        proc.kill()
                except Exception:
                    pass
        except Exception:
            pass

    def _debugger_ready(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1.5)
            return True
        except Exception:
            return False

    def _wait_debugger(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._debugger_ready():
                return True
            time.sleep(0.5)
        return False

    def launch_browser(self, url="https://accounts.google.com/", headful=True):
        chrome = get_chrome_exe()
        if not chrome:
            raise RuntimeError("Google Chrome tidak ditemukan. Silakan install Chrome terlebih dahulu.")

        # Pastikan tidak ada window/proses lama yang tertinggal untuk profil ini.
        self._kill_profile_chrome()
        self.profile_path.mkdir(parents=True, exist_ok=True)
        self._cleanup_locks()

        cmd = [
            chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_path}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            # Matikan SEMUA extension (IDM Integration Module, McAfee WebAdvisor, dll)
            # yang di-inject sistem-wide ke profil Chrome & mengganggu otomasi bot.
            # Browser bot tidak butuh extension apa pun.
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            # Ukuran disamakan dengan OMNIVORA FIX: muat di laptop 1366x768
            # dan viewport CSS stabil walau Windows scaling 125%/150%.
            # Posisi di-offset per slot supaya banyak browser paralel tidak menumpuk.
            "--window-size=1280,680",
            "--force-device-scale-factor=1",
        ]
        if self.hidden:
            # Jauh di luar area layar mana pun (multi-monitor sekalipun) — jendela
            # tak terlihat tapi Chrome tetap render penuh (tidak headless).
            cmd.append("--window-position=-32000,-32000")
        else:
            cmd.append(f"--window-position={30 + (self.window_index % 6) * 70},{10 + (self.window_index % 6) * 25}")
        if self.user_agent:
            cmd.append(f"--user-agent={self.user_agent}")
        if not headful:
            cmd.extend(["--headless", "--disable-gpu"])
        cmd.append(url)

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags
        )
        self.browser_proc = proc
        # Tunggu port debug siap SEBELUM ada yang connect. Tanpa ini,
        # ChromiumPage(set_local_port) yang menemukan port kosong akan
        # meluncurkan Chrome-nya SENDIRI dengan profil default → akun terbaca
        # "Perlu Login" padahal sudah login, plus jendela Chrome liar menumpuk.
        if not self._wait_debugger(30):
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome untuk {self.email} tertutup saat dibuka. "
                    "Tutup Chrome lama untuk profil ini lalu coba lagi.")
            raise RuntimeError(
                f"Chrome untuk {self.email} tidak merespons di port debug {self.port}.")
        return proc

    def close_browser(self, page=None):
        """Tutup browser Chrome untuk akun ini (persiapan pindah akun berikutnya).

        PENTING: JANGAN pakai page.quit() DrissionPage di sini — implementasinya
        mem-polling 'tasklist | findstr' via os.popen, dan di aplikasi GUI tanpa
        console setiap polling memunculkan jendela console hitam. Saat cek massal
        (quit dipanggil per akun) jendela-jendela itu menumpuk memenuhi layar.
        Tutup rapi cukup lewat perintah CDP Browser.close (tanpa subprocess);
        sisa proses disapu psutil di _kill_profile_chrome."""
        for target in (page,):
            try:
                if target is not None:
                    target.run_cdp("Browser.close")
            except Exception:
                pass
        if page is None:
            # Fallback: hubungkan dulu bila session masih hidup, lalu tutup via CDP.
            try:
                self.get_page().run_cdp("Browser.close")
            except Exception:
                pass
        # 3) Tutup handle proses yang kita buat jika masih hidup.
        try:
            if self.browser_proc is not None:
                self.browser_proc.kill()
        except Exception:
            pass
        self.browser_proc = None
        time.sleep(1.5)
        # 4) Sapu bersih proses Chrome profil ini supaya slot tidak bocor.
        self._kill_profile_chrome()
        # 5) Tunggu benar-benar bersih sebelum slot dipakai lagi.
        deadline = time.time() + 12
        while time.time() < deadline:
            if self._count_profile_chrome() <= 0:
                break
            time.sleep(0.5)
            self._kill_profile_chrome()

    def get_page(self):
        try:
            from DrissionPage import ChromiumOptions, ChromiumPage
        except ImportError:
            raise RuntimeError("DrissionPage belum terinstall. Silakan instal terlebih dahulu.")
        # Hanya connect ke browser yang KITA luncurkan. Kalau port belum hidup,
        # gagal dengan jelas — jangan biarkan DrissionPage meluncurkan Chrome
        # sendiri dengan profil kosong (hasil cek jadi salah).
        if not self._debugger_ready() and not self._wait_debugger(10):
            raise RuntimeError(
                f"Browser profil {self.email} belum siap di port {self.port}. "
                "Jalankan ulang cek untuk akun ini.")
        options = ChromiumOptions()
        options.set_local_port(self.port)
        return ChromiumPage(options)

    def check_login(self):
        proc = self.launch_browser("https://myaccount.google.com/", headful=True)
        time.sleep(2)
        page = None
        try:
            page = self.get_page()
            time.sleep(3)
            current_url = (page.url or "").strip()
            self.log(f"Verifikasi URL login Google: {current_url}")
            # Login valid hanya bila halaman akun benar-benar termuat. Halaman
            # gagal load / about:blank juga "tidak mengandung accounts.google.com",
            # jadi jangan dianggap sudah login.
            if not current_url or current_url.startswith(("about:", "chrome:", "data:")):
                return False
            if "accounts.google.com" in current_url or "signin" in current_url.lower():
                return False
            return "myaccount.google.com" in current_url or "google.com" in current_url
        except Exception as e:
            self.log(f"Gagal verifikasi login: {e}", "warning")
            return False
        finally:
            self.close_browser(page)

    _STOPPED_RESULT = {"state": "stopped", "label": "Dihentikan",
                       "reason": "Cek dihentikan oleh user."}

    def check_generate_ready(self, model="Omni Flash", stop_event=None):
        def _stopped():
            return stop_event is not None and stop_event.is_set()

        if _stopped():
            return dict(self._STOPPED_RESULT)

        flow_url = "https://labs.google/fx/tools/flow"
        self.log(f"[{self.email}] Mulai cek credit Flow untuk model {model}...")
        self.log(f"[{self.email}] Membuka Google Flow...")
        self.launch_browser(flow_url, headful=True)
        time.sleep(2.5)
        page = None
        try:
            if _stopped():
                return dict(self._STOPPED_RESULT)
            page = self.get_page()
            time.sleep(4)
            if _stopped():
                return dict(self._STOPPED_RESULT)
            current_url = (page.url or "").strip()
            self.log(f"[{self.email}] URL Flow terbaca: {current_url}")
            url_lower = current_url.lower()
            if "accounts.google.com" in url_lower or "signin" in url_lower:
                self.log(f"[{self.email}] Hasil cek: akun belum login di Google Flow.", "warning")
                return {"state": "login_required", "label": "Perlu Login", "reason": "Akun belum login di Google Flow."}

            def visible(sel, timeout=1):
                try:
                    eles = page.eles(sel, timeout=timeout)
                except Exception:
                    return []
                out = []
                for e in eles:
                    try:
                        if e.states.is_displayed and e.rect.size[0] > 0:
                            out.append(e)
                    except Exception:
                        pass
                return out

            def find_text(labels, timeout=0.8):
                for lbl in labels:
                    hits = visible(f"text={lbl}", timeout=timeout)
                    if hits:
                        return hits[0]
                    hits = visible(f"text:{lbl}", timeout=timeout)
                    if hits:
                        return hits[0]
                return None

            def coord_click(ele, pause=1.4):
                try:
                    loc = ele.rect.location
                    size = ele.rect.size
                    page.actions.move_to((int(loc[0] + size[0] / 2), int(loc[1] + size[1] / 2))).click()
                    time.sleep(pause)
                    return True
                except Exception:
                    return False

            def dismiss_dialogs():
                for _ in range(4):
                    clicked = False
                    for txt in ("Got it", "Mengerti", "Accept all", "Terima semua", "I agree", "Setuju", "Berikutnya"):
                        hit = find_text((txt,), timeout=0.4)
                        if hit:
                            self.log(f"[{self.email}] Menutup dialog: {txt}")
                            coord_click(hit, 0.8)
                            clicked = True
                            break
                    if not clicked:
                        break

            def handle_consent():
                next_labels = ("Next", "Berikutnya", "Continue", "Lanjutkan", "Get started", "Mulai", "Done", "Selesai", "Agree & continue", "Setuju & lanjutkan")
                for _ in range(10):
                    btn = find_text(next_labels, timeout=0.4)
                    if not btn:
                        return
                    try:
                        loc = btn.rect.location
                        page.actions.move_to((int(loc[0]), max(100, int(loc[1]) - 200)))
                        for _ in range(12):
                            page.actions.scroll(delta_y=400)
                    except Exception:
                        pass
                    time.sleep(0.5)
                    btn = find_text(next_labels, timeout=0.4)
                    if btn:
                        self.log(f"[{self.email}] Menyelesaikan onboarding/consent...")
                        coord_click(btn, 1.3)

            def body_text():
                try:
                    return (page.run_js("return document.body ? (document.body.innerText || '') : ''; ") or "")
                except Exception:
                    return ""

            def at_project_area(txt_lower):
                if "new project" in txt_lower or "project baru" in txt_lower:
                    return True
                for btn in visible("tag:button", timeout=0.5):
                    try:
                        low = (btn.text or "").strip().lower()
                        if low.startswith("add_2") or low.startswith("play_circle"):
                            return True
                    except Exception:
                        pass
                return False

            dismiss_dialogs()
            handle_consent()
            if _stopped():
                return dict(self._STOPPED_RESULT)
            txt = body_text()
            txt_lower = txt.lower()

            if "unusual activity" in txt_lower or "aktivitas yang tidak biasa" in txt_lower:
                self.log(f"[{self.email}] Hasil cek: terdeteksi unusual activity.", "warning")
                return {"state": "limited", "label": "Limit/Credit", "reason": "Google mendeteksi unusual activity atau akun dibatasi untuk generate."}

            credit_markers = (
                "not enough credits", "out of credits", "upgrade to continue",
                "kredit tidak cukup", "insufficient"
            )
            if any(k in txt_lower for k in credit_markers):
                self.log(f"[{self.email}] Hasil cek: sinyal limit/credit habis terdeteksi langsung di halaman Flow.", "warning")
                return {"state": "limited", "label": "Limit/Credit", "reason": "Akun terdeteksi kena limit atau credit habis."}

            for _ in range(2):
                if _stopped():
                    return dict(self._STOPPED_RESULT)
                if at_project_area(txt_lower):
                    self.log(f"[{self.email}] Sudah berada di area project/dashboard Flow.")
                    break
                cta = find_text(("Create with Google Flow", "Buat dengan Google Flow"), timeout=1.5)
                if cta:
                    self.log(f"[{self.email}] Akun first-login/intro terdeteksi, masuk ke dashboard/project dulu...")
                    coord_click(cta, 6)
                    dismiss_dialogs()
                    handle_consent()
                    txt = body_text()
                    txt_lower = txt.lower()
                else:
                    break

            profile_btn = None
            candidates = []

            def add_profile_candidate(ele, text_hint=""):
                try:
                    loc = ele.rect.location
                    size = ele.rect.size
                    if loc[1] > 180 or loc[0] < 900 or size[0] <= 0:
                        return
                    hint = (text_hint or "").strip().lower()
                    score = (loc[1], -loc[0], -size[0], -size[1])
                    candidates.append((score, ele))
                except Exception:
                    pass

            for btn in visible("tag:button", timeout=1):
                try:
                    txt_btn = (btn.text or "").strip()
                    low = txt_btn.lower()
                    loc = btn.rect.location
                    size = btn.rect.size
                    if loc[1] > 180 or loc[0] < 900 or size[0] <= 0:
                        continue
                    if low.startswith("flow tv") or low == "pro" or low.startswith("more_vert") or "bantuan" in low or "help" in low:
                        continue
                    if len(txt_btn) <= 3 or "@gmail.com" in low or self.email.lower() in low:
                        add_profile_candidate(btn, txt_btn)
                except Exception:
                    pass

            for img in visible("tag:img", timeout=1):
                try:
                    src = (img.attr("src") or "").lower()
                    loc = img.rect.location
                    size = img.rect.size
                    if loc[1] > 180 or loc[0] < 1100 or size[0] < 24 or size[1] < 24:
                        continue
                    if "googleusercontent.com" in src or "=s" in src or "lh3.googleusercontent.com" in src:
                        add_profile_candidate(img, src)
                except Exception:
                    pass

            if candidates:
                candidates.sort(key=lambda x: x[0])
                profile_btn = candidates[0][1]

            if profile_btn is None:
                if at_project_area(txt_lower):
                    self.log(f"[{self.email}] Gagal menemukan tombol avatar profil setelah masuk area project.", "warning")
                    return {"state": "limited", "label": "Limit/Credit", "reason": "Sudah masuk area project, tetapi panel profil/credit tidak bisa dibaca. Akun dianggap belum siap generate."}
                self.log(f"[{self.email}] Belum berhasil masuk ke area project/dashboard untuk akun baru.", "warning")
                return {"state": "limited", "label": "Limit/Credit", "reason": "Bot tidak berhasil mencapai area project/dashboard, sehingga akun dianggap belum siap generate."}

            if _stopped():
                return dict(self._STOPPED_RESULT)
            self.log(f"[{self.email}] Klik avatar profil untuk membuka panel credit...")
            if not coord_click(profile_btn, 2):
                self.log(f"[{self.email}] Gagal klik avatar profil.", "warning")
                return {"state": "limited", "label": "Limit/Credit", "reason": "Tombol avatar profil gagal dibuka, sehingga credit tidak bisa divalidasi dan akun dianggap belum siap generate."}

            # Angka credit bisa berformat ribuan ('1.000' / '2,500') — tangkap
            # beserta separatornya lalu buang non-digit. Regex '(\d+)' polos
            # SALAH BACA '1.000' sebagai '000' → akun sehat dicap limit.
            _NUM = r"(\d[\d.,]*)"
            credit_patterns = (
                rf"{_NUM}\s+google\s+flow\s+credits",
                rf"{_NUM}\s+poin\s+google\s+flow",
                rf"{_NUM}\s+google\s+flow\s+points",
                rf"google\s+flow\s+credits\D{{0,20}}{_NUM}",
                rf"google\s+flow\s+poin\D{{0,20}}{_NUM}",
                rf"google\s+flow\s+points\D{{0,20}}{_NUM}",
            )

            def parse_credits(text):
                for pattern in credit_patterns:
                    m = re.search(pattern, text, flags=re.IGNORECASE)
                    if m:
                        digits = re.sub(r"[^\d]", "", m.group(1))
                        if digits:
                            return int(digits)
                return None

            # Panel profil kadang lambat render → coba baca beberapa kali,
            # dan re-klik avatar bila panel tampak belum terbuka. Jangan
            # memvonis limit hanya karena baca pertama kosong.
            credits = None
            txt = ""
            for attempt in range(4):
                if _stopped():
                    return dict(self._STOPPED_RESULT)
                txt = body_text()
                credits = parse_credits(txt)
                if credits is not None:
                    break
                panel_open = any(k in txt.lower() for k in
                                 ("google flow credits", "google flow poin", "google flow points"))
                if not panel_open and attempt < 3:
                    retry_btn = profile_btn
                    try:
                        coord_click(retry_btn, 1.5)
                    except Exception:
                        pass
                time.sleep(1.5)
            txt_lower = txt.lower()
            if credits is not None:
                self.log(f"[{self.email}] Credit terbaca dari panel profil: {credits} Google Flow credit/poin.")
                if credits > 0:
                    self.log(f"[{self.email}] Hasil cek: akun siap generate.", "success")
                    return {
                        "state": "ready",
                        "label": "Siap Generate",
                        "reason": f"Credit tersedia: {credits} Google Flow credit/poin. Akun siap dipakai generate model {model}."
                    }
                self.log(f"[{self.email}] Hasil cek: credit/poin 0, akun belum siap generate.", "warning")
                return {
                    "state": "limited",
                    "label": "Limit/Credit",
                    "reason": "Google Flow credit/poin terdeteksi 0. Akun belum siap dipakai generate."
                }

            if (("google flow credits" in txt_lower) or ("google flow poin" in txt_lower) or ("google flow points" in txt_lower)) and "upgrade" in txt_lower:
                self.log(f"[{self.email}] Panel credit terbuka, tetapi akun diminta upgrade tanpa credit/poin siap pakai.", "warning")
                return {
                    "state": "limited",
                    "label": "Limit/Credit",
                    "reason": "Panel credit/poin terbuka, tetapi akun diminta upgrade dan credit siap pakai tidak terdeteksi."
                }

            self.log(f"[{self.email}] Panel profil terbuka, tetapi angka credit tidak terbaca.", "warning")
            return {"state": "limited", "label": "Limit/Credit", "reason": "Panel profil terbuka tetapi angka Google Flow credits tidak terbaca, sehingga akun dianggap belum siap generate."}
        except Exception as e:
            self.log(f"[{self.email}] Gagal cek limit/generate ready: {e}", "warning")
            return {"state": "error", "label": "Error Cek", "reason": str(e)}
        finally:
            self.log(f"[{self.email}] Menutup browser cek credit...")
            self.close_browser(page)

class GoogleVideoGenerator:
    FLOW_URL = "https://labs.google/fx/tools/flow"

    def __init__(self, email, image_path, prompt, ratio="9:16", count=1, duration="8s", model="Omni Flash", user_agent=None, log_cb=None, stop_event=None, window_index=0, hidden=False, output_name="", pause_cb=None, resume_check=None):
        self.email = email
        self.image_path = image_path
        self.prompt = re.sub(r"\s*\n+\s*", " ", str(prompt or "")).strip()
        self.ratio = ratio
        self.count = min(4, max(1, int(count)))
        self.duration = str(duration or "8s")
        self.model = str(model or "Omni Flash").strip()
        self.output_name = sanitize_file_stem(output_name, "") if output_name else ""
        self.is_image = "banana" in self.model.lower()
        self.stop_event = stop_event
        # Callbacks untuk unusual activity pause/resume
        # pause_cb() dipanggil saat unusual activity terdeteksi → bridge set state paused
        # resume_check() dipanggil tiap 2 detik → return True jika user sudah klik Lanjutkan
        self._pause_cb = pause_cb
        self._resume_check = resume_check or (lambda: False)
        self._stop_event_check = lambda: self.stop_event is not None and self.stop_event.is_set()
        self.last_output_paths = []
        # Nomor slot disimpan di sini juga, bukan hanya diteruskan ke session.
        # Folder staging download dipisah per slot memakai nilai ini; tanpa
        # atribut sendiri, self.window_index melempar AttributeError.
        self.window_index = max(0, int(window_index or 0))
        self.session = GoogleAccountSession(email, user_agent=user_agent, log_cb=log_cb,
                                            window_index=window_index, hidden=hidden)

    def _stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def _sleep(self, seconds):
        """Tidur yang bisa diinterupsi oleh stop_event. Return False jika dihentikan."""
        if self.stop_event is not None:
            return not self.stop_event.wait(seconds)
        time.sleep(seconds)
        return True

    def _retry_after_unusual_activity(self, page, status):
        """Klik Generate ulang setelah user selesaikan verifikasi unusual activity,
        lalu kembalikan True agar loop tunggu-download bisa lanjut.

        Parameter out_dir/download_dirs/before_files DIHAPUS: tidak pernah
        dipakai di dalam sini, tapi keberadaannya memaksa pemanggil menyebut
        `download_dirs` dan `before_files` yang TIDAK ADA di lingkupnya —
        variabelnya bernama download_watch_dirs. Jadi begitu Google benar-
        benar memunculkan "unusual activity", baris itu melempar NameError
        dan seluruh proses generate mati di tengah jalan."""
        try:
            gen_btn = (
                self._button_starts(page, "send")
                or self._button_eq(page, "Generate")
                or self._button_contains(page, "generate")
            )
            if gen_btn:
                self._coord_click(page, gen_btn, 1)
                status(f"[{self.email}] Klik Generate ulang setelah verifikasi.")
                self._sleep(3)
                return True
            status(f"[{self.email}] Tombol Generate tidak ditemukan setelah verifikasi — lanjut tunggu hasil.")
            return True
        except Exception as e:
            status(f"[{self.email}] WARNING: Gagal retry setelah unusual activity: {e}")
            return False

    def _visible(self, page, sel, timeout=2):
        """Semua elemen cocok yang benar-benar tampil (punya ukuran di layar)."""
        out = []
        try:
            eles = page.eles(sel, timeout=timeout)
        except Exception:
            return out
        for e in eles:
            try:
                if e.states.is_displayed and e.rect.size[0] > 0:
                    out.append(e)
            except Exception:
                pass
        return out

    def _coord_click(self, page, ele, pause=1.5):
        """Klik pakai koordinat mouse asli (elemen Flow sering tidak merespons klik JS)."""
        loc = ele.rect.location
        size = ele.rect.size
        page.actions.move_to((int(loc[0] + size[0] / 2), int(loc[1] + size[1] / 2))).click()
        self._sleep(pause)

    def _button_meta_text(self, ele):
        parts = []
        for getter in (
            lambda: ele.text,
            lambda: ele.attr("aria-label"),
            lambda: ele.attr("title"),
            lambda: ele.attr("data-tooltip"),
            lambda: ele.attr("mattooltip"),
        ):
            try:
                val = (getter() or "").strip()
            except Exception:
                val = ""
            if val:
                parts.append(val)
        return " ".join(parts).strip()

    def _button_eq(self, page, text_eq, min_y=0):
        """Cari button yang teks/atribut gabungannya persis text_eq."""
        for e in self._visible(page, "tag:button"):
            try:
                meta = self._button_meta_text(e)
                if meta == text_eq and e.rect.location[1] >= min_y:
                    return e
            except Exception:
                pass
        return None

    def _button_starts(self, page, prefix, min_y=0):
        """Cari button berdasarkan awalan nama ikon/atribut utama."""
        for e in self._visible(page, "tag:button"):
            try:
                meta = self._button_meta_text(e)
                if meta.startswith(prefix) and e.rect.location[1] >= min_y:
                    return e
            except Exception:
                pass
        return None

    def _button_contains(self, page, needle, min_y=0):
        """Cari button yang teks/atribut gabungannya mengandung needle."""
        needle = str(needle or "").lower()
        for e in self._visible(page, "tag:button"):
            try:
                meta = self._button_meta_text(e)
                if needle in meta.lower() and e.rect.location[1] >= min_y:
                    return e
            except Exception:
                pass
        return None

    def _dump_bottom_buttons(self, page, status, label="bottom buttons"):
        try:
            seen = []
            for b in self._visible(page, "tag:button", timeout=1):
                loc = b.rect.location
                if loc[1] < 560:
                    continue
                meta = self._button_meta_text(b).replace("\n", " ").strip()
                if not meta:
                    continue
                seen.append(f"{int(loc[0])},{int(loc[1])}:{meta[:60]}")
            if seen:
                status(f"[{self.email}] DEBUG {label}: " + " | ".join(seen[:24]))
        except Exception:
            pass

    def _wait_new_file(self, out_dir, before, timeout):
        """Tunggu file baru muncul di out_dir (deteksi download lebih andal daripada
        page.wait.download_begin). Return set nama file baru, kosong jika timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                now = {f.name for f in Path(out_dir).glob("*")}
            except Exception:
                now = set()
            new = {n for n in now - before if not n.endswith((".crdownload", ".tmp"))}
            if new:
                self._sleep(2)  # beri waktu ukuran file stabil
                return new
            if not self._sleep(3):
                return set()
        return set()

    def _snapshot_download_dirs(self, dirs):
        snap = {}
        for d in dirs:
            try:
                snap[str(Path(d))] = {f.name for f in Path(d).glob("*")}
            except Exception:
                snap[str(Path(d))] = set()
        return snap

    def _wait_new_download_files(self, dirs, before_map, timeout, status=None):
        """Tunggu berkas hasil download benar-benar utuh.

        PENTING — unduhan yang MASIH BERJALAN tidak boleh dianggap gagal.
        Chrome menulis berkas sementara berakhiran .crdownload dan baru
        menamainya final setelah selesai. Versi sebelumnya cuma melewati berkas
        itu lalu menunggu tenggat habis: untuk video atau zip project yang
        besar, tenggat 8/20 detik sering lewat SELAGI unduhannya sehat.
        Akibatnya dilaporkan "Download langsung tidak jalan", lalu dipicu
        unduhan KEDUA lewat Download Project (zip) untuk berkas yang sama —
        dua unduhan berebut, dan prosesnya terlihat macet.

        Sekarang: selama berkas .crdownload UKURANNYA MASIH BERTAMBAH, tenggat
        diperpanjang dan kemajuannya dilaporkan. Menyerah hanya kalau tidak ada
        berkas sementara sama sekali, atau ukurannya berhenti tumbuh.
        """
        MEDIA = (".mp4", ".zip", ".jpg", ".jpeg", ".png", ".webp")
        deadline = time.time() + timeout
        ukuran_lama = {}
        macet_sejak = None
        lapor_terakhir = 0.0

        while time.time() < deadline:
            found, sementara = [], []
            for d in dirs:
                try:
                    base = before_map.get(str(Path(d)), set())
                    now = {f.name for f in Path(d).glob("*")}
                    for name in now - base:
                        fp = Path(d) / name
                        if name.endswith((".crdownload", ".tmp")):
                            sementara.append(fp)
                            continue
                        # Hanya hasil download media yang dihitung — file lain
                        # (log, dump) yang kebetulan muncul jangan diklaim.
                        # Image mode (Nano Banana) menghasilkan jpg/png/webp.
                        if not name.lower().endswith(MEDIA):
                            continue
                        if fp.exists():
                            found.append(fp)
                except Exception:
                    pass

            if found:
                self._sleep(2)
                uniq, seen = [], set()
                for fp in found:
                    key = str(fp)
                    if key not in seen:
                        uniq.append(fp)
                        seen.add(key)
                return uniq

            # Ada unduhan berjalan? Perpanjang selama ukurannya bertambah.
            tumbuh = False
            total = 0
            for fp in sementara:
                try:
                    sz = fp.stat().st_size
                except OSError:
                    continue
                total += sz
                if sz > ukuran_lama.get(str(fp), -1):
                    ukuran_lama[str(fp)] = sz
                    tumbuh = True

            if tumbuh:
                macet_sejak = None
                deadline = max(deadline, time.time() + 30)
                sekarang = time.time()
                if status and sekarang - lapor_terakhir > 5:
                    lapor_terakhir = sekarang
                    status(f"[{self.email}] Mengunduh... {total / (1024 * 1024):.1f} MB")
            elif sementara:
                # Berkas sementara ada tapi tidak bertambah — beri kesempatan
                # 60 detik (koneksi bisa tersendat) sebelum dinyatakan gagal.
                macet_sejak = macet_sejak or time.time()
                if time.time() - macet_sejak > 60:
                    if status:
                        status(f"[{self.email}] Unduhan berhenti bertambah "
                               f"selama 60 detik — dianggap gagal.")
                    return []

            if not self._sleep(3):
                return []
        return []

    def _find_text_hit(self, page, labels, timeout=1):
        for lbl in labels:
            hits = self._visible(page, f"text={lbl}", timeout=timeout)
            if hits:
                return hits[0]
            hits = self._visible(page, f"text:{lbl}", timeout=timeout)
            if hits:
                return hits[0]
        return None

    def _find_download_button(self, page):
        btn = self._button_starts(page, "download")
        if btn:
            return btn
        btn = self._find_text_hit(page, ("Download", "Unduh"), timeout=1)
        if btn:
            return btn
        candidates = []
        for e in self._visible(page, "tag:button", timeout=1):
            try:
                meta = self._button_meta_text(e).lower()
                if not meta:
                    continue
                if ("download" in meta or "unduh" in meta) and not any(bad in meta for bad in ("download project", "unduh project", "download proyek", "unduh proyek")):
                    loc = e.rect.location
                    candidates.append(((loc[1], -loc[0]), e))
            except Exception:
                pass
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    def _find_more_button(self, page):
        btn = self._button_starts(page, "more_vert")
        if btn:
            return btn
        for e in self._visible(page, "tag:button", timeout=1):
            try:
                meta = self._button_meta_text(e).lower()
                if meta.startswith("more_vert") or meta in ("lainnya", "more", "opsi lainnya"):
                    return e
            except Exception:
                pass
        return None

    def _attempt_direct_download(self, page, download_dirs, before_files, status):
        for attempt in range(3):
            dl_btn = self._find_download_button(page)
            if not dl_btn:
                status(f"[{self.email}] Tombol download per-video belum terlihat (percobaan {attempt + 1}/3).")
                if not self._sleep(2):
                    return []
                continue
            status(f"[{self.email}] Klik tombol download per-video ({attempt + 1}/3)...")
            try:
                self._coord_click(page, dl_btn, 1.2)
            except Exception:
                try:
                    page.run_js("arguments[0].click();", dl_btn)
                except Exception:
                    pass
                if not self._sleep(1.2):
                    return []

            picked = False
            # Tunggu dropdown resolusi muncul (maks 8 detik)
            for _ in range(8):
                for label in ("720p", "720", "1080p", "1080"):
                    hits = self._visible(page, f"text:{label}", timeout=0.4)
                    if hits:
                        self._coord_click(page, hits[0], 1.5)
                        picked = True
                        break
                if picked:
                    break
                if not self._sleep(1):
                    return []
            if picked:
                # Beri Chrome waktu yang cukup untuk mulai download setelah
                # resolusi dipilih — video bisa besar, jangan timeout terlalu cepat.
                new_files = self._wait_new_download_files(download_dirs, before_files, 60, status)
                if new_files:
                    return new_files
            else:
                # Tidak ada dropdown resolusi — coba langsung tunggu download
                # (beberapa akun langsung download tanpa pilih resolusi)
                new_files = self._wait_new_download_files(download_dirs, before_files, 30, status)
                if new_files:
                    return new_files
            status(f"[{self.email}] Klik download belum memicu file baru (percobaan {attempt + 1}/3).")
        return []

    def _close_viewer(self, page):
        try:
            page.actions.key_down('ESCAPE').key_up('ESCAPE')
        except Exception:
            pass
        back = self._button_starts(page, "arrow_back")
        if back and not self._button_starts(page, "add_2", min_y=700):
            try:
                self._coord_click(page, back, 2)
            except Exception:
                pass

    def _download_project_zip(self, page, download_dirs, before_map, status):
        more = self._find_more_button(page)
        if not more:
            return []
        self._coord_click(page, more, 2)
        item = None
        for lbl in (
            "downloadDownload Project", "downloadUnduh Project",
            "downloadUnduh Proyek", "downloadDownload Proyek",
        ):
            item = self._button_eq(page, lbl)
            if item:
                break
        if not item:
            item = self._find_text_hit(page, (
                "Download Project", "Unduh Project", "Unduh Proyek", "Download Proyek"
            ), timeout=1)
        if not item:
            status(f"[{self.email}] Menu 'Download Project' tidak ditemukan di viewer.")
            return []
        self._coord_click(page, item, 2)
        return self._wait_new_download_files(download_dirs, before_map, 240, status)

    def _unique_output_path(self, out_dir, filename):
        """Nama file output yang PASTI unik di folder tujuan dan langsung
        dipesan (dibuat file kosong) di bawah lock, supaya slot paralel yang
        memakai folder sama tidak pernah memilih nama yang sama lalu saling
        menimpa. Nama disematkan identitas email + timestamp agar mudah
        dilacak ke akun sumbernya."""
        base = os.path.basename(str(filename or "")).strip() or "video.mp4"
        suffix = Path(base).suffix or ".mp4"
        stem = self.output_name or Path(base).stem or "video"
        tag = f"{sanitize_email(self.email)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        base_stem = f"{stem}_{tag}"
        out_dir = Path(out_dir)
        with _OUTPUT_NAMING_LOCK:
            candidate = out_dir / f"{base_stem}{suffix}"
            idx = 1
            while candidate.exists():
                candidate = out_dir / f"{base_stem}_{idx}{suffix}"
                idx += 1
            # Reserve nama sekarang juga (file kosong) selagi memegang lock,
            # supaya thread lain yang mengecek exists() melihatnya "terpakai".
            try:
                candidate.touch()
            except Exception:
                pass
        return candidate

    def _finalize_downloaded_files(self, final_dir, downloaded_files, status):
        final_dir = Path(final_dir)
        final_dir.mkdir(parents=True, exist_ok=True)
        final_paths = []
        reserved = []  # semua nama yang sempat dipesan (untuk sapu bersih sisa)

        # Tunggu semua partial download selesai SEBELUM memindahkan file.
        # Chrome menulis .crdownload/.tmp yang baru diganti nama setelah selesai.
        # Harus dicek di staging (sebelum file dipindah), bukan sesudah.
        if downloaded_files:
            staging = Path(list(downloaded_files)[0]).parent
            for _ in range(60):  # max 60 detik tunggu
                partials = list(staging.glob("*.crdownload")) + list(staging.glob("*.tmp"))
                if not partials:
                    break
                status(f"[{self.email}] Menunggu download selesai ({len(partials)} file partial)...")
                time.sleep(1)

        for file_path in list(downloaded_files):
            file_path = Path(file_path)
            if not file_path.exists():
                continue
            if file_path.suffix.lower() == ".zip":
                extracted_any = False
                try:
                    with zipfile.ZipFile(file_path) as zf:
                        for member in zf.namelist():
                            if not member.lower().endswith(
                                    (".mp4", ".jpg", ".jpeg", ".png", ".webp")):
                                continue
                            target = self._unique_output_path(final_dir, os.path.basename(member))
                            reserved.append(target)
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            extracted_any = True
                            if target.exists():
                                final_paths.append(target)
                                status(f"[{self.email}] Video diekstrak ke folder produk: {target.name}")
                                if target.stat().st_size <= 0:
                                    status(f"[{self.email}] WARNING: File MP4 hasil ekstrak kosong: {target.name}")
                except Exception as exc:
                    status(f"[{self.email}] WARNING: Gagal ekstrak zip: {exc}")
                finally:
                    try:
                        file_path.unlink()
                    except Exception:
                        pass
                if not extracted_any:
                    status(f"[{self.email}] WARNING: Zip terdownload tapi tidak ada file MP4 di dalamnya.")
            elif file_path.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png", ".webp"):
                if file_path.parent == final_dir and not self.output_name:
                    target = file_path
                else:
                    target = self._unique_output_path(final_dir, file_path.name)
                    reserved.append(target)
                try:
                    if file_path != target:
                        try:
                            file_path.replace(target)
                        except OSError:
                            # replace() gagal cross-drive — pakai shutil.move sebagai fallback
                            shutil.move(str(file_path), str(target))
                        status(f"[{self.email}] File hasil dipindahkan ke folder produk: {target.name}")
                    final_paths.append(target)
                except Exception as exc:
                    status(f"[{self.email}] WARNING: Gagal memindahkan file hasil ke folder produk: {exc}")

        verified = []
        for fp in final_paths:
            try:
                if not fp.exists():
                    status(f"[{self.email}] WARNING: File tidak ada setelah dipindah: {fp.name}")
                    continue
                if fp.stat().st_size <= 0:
                    status(f"[{self.email}] WARNING: File 0-byte: {fp.name}")
                    try:
                        fp.unlink()
                    except Exception:
                        pass
                    continue

                # Tunggu ukuran stabil — MP4 bisa besar, butuh beberapa detik
                is_mp4 = fp.suffix.lower() == '.mp4'
                stable_wait  = 3.0 if is_mp4 else 1.2
                retry_wait   = 3.0 if is_mp4 else 1.5
                max_retries  = 5  if is_mp4 else 2

                size1 = fp.stat().st_size
                time.sleep(stable_wait)
                size2 = fp.stat().st_size if fp.exists() else 0

                if size2 <= 0:
                    status(f"[{self.email}] WARNING: File hilang/0-byte setelah tunggu: {fp.name}")
                    continue

                retries = 0
                while size2 != size1 and retries < max_retries:
                    retries += 1
                    status(f"[{self.email}] File masih ditulis ({size1} → {size2} bytes), tunggu lagi... ({retries}/{max_retries})")
                    size1 = size2
                    time.sleep(retry_wait)
                    size2 = fp.stat().st_size if fp.exists() else 0

                if size2 > 0:
                    status(f"[{self.email}] File terverifikasi: {fp.name} ({size2:,} bytes)")
                    verified.append(fp)
                else:
                    status(f"[{self.email}] WARNING: File kosong setelah tunggu: {fp.name}")
            except Exception as e:
                status(f"[{self.email}] WARNING: Error verifikasi {fp.name}: {e}")

        # Sapu bersih sisa file kosong (0-byte) yang namanya sempat dipesan tapi
        # gagal terisi — jangan biarkan mengotori folder foto.
        verified_set = {str(p) for p in verified}
        for rp in reserved:
            try:
                if str(rp) not in verified_set and rp.exists() and rp.stat().st_size <= 0:
                    rp.unlink()
            except Exception:
                pass
        return verified

    # Tombol maju di wizard onboarding/persetujuan akun baru (EN & ID)
    _CONSENT_NEXT_LABELS = ("Next", "Berikutnya", "Continue", "Lanjutkan",
                            "Get started", "Mulai", "Done", "Selesai",
                            "Agree & continue", "Setuju & lanjutkan")

    def _find_consent_next(self, page):
        # timeout kecil: sebagian besar akun (tanpa wizard) tidak punya tombol ini,
        # jadi pencarian per-label harus cepat supaya transisi antar-akun tidak lemot.
        for lbl in self._CONSENT_NEXT_LABELS:
            hits = self._visible(page, f"text={lbl}", timeout=0.3)
            if hits:
                return hits[0]
        return None

    def _handle_consent(self, page):
        """Wizard onboarding & persetujuan privasi akun baru: klik Next lalu Continue
        berulang. Screen 'Your data and Google Flow' butuh di-scroll dulu supaya tombol
        Continue aktif. Kalau tidak ada wizard, langsung keluar (akun lama)."""
        for _ in range(10):
            btn = self._find_consent_next(page)
            if not btn:
                return
            # Scroll isi dialog ke bawah supaya tombol maju benar-benar aktif
            try:
                loc = btn.rect.location
                page.actions.move_to((int(loc[0]), max(100, int(loc[1]) - 200)))
                for _ in range(15):
                    page.actions.scroll(delta_y=400)
            except Exception:
                pass
            if not self._sleep(0.4):
                return
            btn = self._find_consent_next(page)  # re-find setelah scroll
            if btn:
                self._coord_click(page, btn, 1.4)

    def _dismiss_dialogs(self, page):
        """Tutup dialog sambutan/cookie/tur fitur yang mungkin muncul saat buka Flow."""
        for _ in range(4):
            clicked = False
            for txt in ("Got it", "Mengerti", "Accept all", "Terima semua", "I agree", "Setuju", "Berikutnya"):
                eles = self._visible(page, f"text:{txt}", timeout=0.3)
                if eles:
                    try:
                        self._coord_click(page, eles[0], 0.6)
                        clicked = True
                    except Exception:
                        pass
            if not clicked:
                break

    def _list_result_images(self, page):
        """Semua <img> hasil media server (Google) di area grid — dipakai untuk
        mendeteksi gambar hasil generate Nano Banana."""
        out = []
        for e in self._visible(page, "tag:img", timeout=1):
            try:
                src = (e.attr("src") or "")
                low = src.lower()
                if not low or "/a/" in low or "flower" in low:
                    continue
                if not any(k in low for k in
                           ("labs.google", "googleusercontent", "getmediaurl", "/media", "storage.googleapis")):
                    continue
                loc = e.rect.location
                sz = e.rect.size
                if sz[0] > 50 and sz[1] > 50 and 60 < loc[1] < 800:
                    out.append((e, src))
            except Exception:
                pass
        return out

    def _find_image_download_button(self, page):
        """Ikon download (panah ke bawah) di header viewer gambar. Prioritas:
        meta text 'download'/'unduh', lalu ikon di area kanan-atas viewer."""
        def dom_meta(ele):
            meta = self._button_meta_text(ele)
            try:
                extra = page.run_js("""
                    const el = arguments[0];
                    const q = s => Array.from(el.querySelectorAll(s))
                        .map(n => (n.textContent || '').trim()).filter(Boolean).join(' ');
                    return [
                        el.innerText,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('data-tooltip'),
                        el.getAttribute('mattooltip'),
                        q('mat-icon,.material-icons,.google-symbols,[data-icon-name]')
                    ].filter(Boolean).join(' ');
                """, ele, timeout=3) or ""
                if extra:
                    meta = f"{meta} {extra}".strip()
            except Exception:
                pass
            return meta

        good_tokens = (
            "download", "unduh", "file_download", "arrow_downward",
            "download_for_offline", "vertical_align_bottom", "save_alt", "get_app",
        )
        for b in self._visible(page, "tag:button", timeout=1):
            try:
                meta = dom_meta(b).lower()
                if any(tok in meta for tok in good_tokens) and not any(bad in meta for bad in ("project", "proyek")):
                    return b
            except Exception:
                pass

        # 1) via meta text/aria-label
        for b in self._visible(page, "tag:button", timeout=1):
            try:
                meta = self._button_meta_text(b).lower()
                if not meta:
                    continue
                if (("download" in meta or "unduh" in meta)
                        and not any(bad in meta for bad in ("project", "proyek"))):
                    return b
            except Exception:
                pass
        # 2) via posisi: ikon di header (y<90), bukan paling kiri (back) / kanan (Done)
        cands = []
        for b in self._visible(page, "tag:button", timeout=1):
            try:
                loc = b.rect.location
                sz = b.rect.size
                if loc[1] < 130 and 120 < loc[0] and sz[0] < 90 and sz[1] < 90:
                    cands.append((loc[0], b))
            except Exception:
                pass
        # ikon download biasanya di gugus kanan (love, download, trash, share) —
        # ambil yang kedua dari kiri di gugus kanan bila ada beberapa
        cands.sort()
        right = [b for x, b in cands if x > 520]
        if len(right) >= 2:
            return right[1]  # love=0, download=1
        if right:
            return right[0]
        return None

    def _image_ext_from_source(self, src="", mime=""):
        low_mime = (mime or "").lower()
        if "jpeg" in low_mime or "jpg" in low_mime:
            return ".jpg"
        if "webp" in low_mime:
            return ".webp"
        if "png" in low_mime:
            return ".png"
        low = (src or "").lower().split("?", 1)[0]
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            if low.endswith(ext) or ext in low:
                return ".jpg" if ext == ".jpeg" else ext
        return ".png"

    def _write_image_bytes(self, download_dir, data, src="", mime=""):
        if not data or len(data) <= 2000:
            return None
        out_dir = Path(download_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = self._image_ext_from_source(src, mime)
        prefix = self.output_name or "nano"
        name = f"{prefix}_{sanitize_email(self.email)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}{ext}"
        path = out_dir / name
        path.write_bytes(data)
        return path

    def _image_src_from_element(self, page, ele):
        if not ele:
            return ""
        try:
            src = (ele.attr("src") or ele.attr("currentSrc") or "").strip()
            if src:
                return src
        except Exception:
            pass
        try:
            return (page.run_js("""
                const img = arguments[0];
                return (img && (img.currentSrc || img.src || img.getAttribute('src'))) || '';
            """, ele, timeout=3) or "").strip()
        except Exception:
            return ""

    def _viewer_image_candidates(self, page, fallback_ele=None):
        cands = []
        if fallback_ele:
            src = self._image_src_from_element(page, fallback_ele)
            if src:
                try:
                    size = fallback_ele.rect.size
                    area = max(1, int(size[0] * size[1]))
                except Exception:
                    area = 1
                cands.append((area, fallback_ele, src))
        for e in self._visible(page, "tag:img", timeout=1):
            try:
                src = self._image_src_from_element(page, e)
                low = src.lower()
                if not src or "/a/" in low or "flower" in low:
                    continue
                size = e.rect.size
                if size[0] < 80 or size[1] < 80:
                    continue
                cands.append((int(size[0] * size[1]), e, src))
            except Exception:
                pass
        cands.sort(key=lambda it: it[0], reverse=True)
        out, seen = [], set()
        for _area, ele, src in cands:
            if src in seen:
                continue
            seen.add(src)
            out.append((ele, src))
        return out

    def _save_image_from_browser(self, page, download_dir, image_ele, src, status):
        """Simpan gambar lewat browser session. Lebih kuat dari urllib biasa
        karena fetch berjalan dengan cookie/session Flow yang sedang login."""
        if not src:
            return None
        try:
            if src.startswith("data:image/"):
                header, b64 = src.split(",", 1)
                mime = header.split(";", 1)[0].split(":", 1)[-1]
                data = base64.b64decode(b64)
                return self._write_image_bytes(download_dir, data, src, mime)
        except Exception:
            pass

        if image_ele:
            try:
                res = page.run_js("""
                    const img = arguments[0];
                    if (!img || !img.naturalWidth || !img.naturalHeight) {
                        return {ok:false, error:'image element belum siap'};
                    }
                    const canvas = document.createElement('canvas');
                    canvas.width = img.naturalWidth;
                    canvas.height = img.naturalHeight;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0);
                    const dataUrl = canvas.toDataURL('image/png');
                    return {ok:true, mime:'image/png', data:dataUrl.split(',')[1] || ''};
                """, image_ele, timeout=6)
                if isinstance(res, dict) and res.get("ok") and res.get("data"):
                    data = base64.b64decode(res["data"])
                    path = self._write_image_bytes(download_dir, data, src, res.get("mime") or "image/png")
                    if path:
                        return path
            except Exception:
                pass

        try:
            res = page.run_js("""
                const src = arguments[0];
                return fetch(src, {credentials: 'include', cache: 'no-store'})
                    .then(r => {
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        return r.blob();
                    })
                    .then(blob => new Promise(resolve => {
                        const reader = new FileReader();
                        reader.onload = () => resolve({
                            ok: true,
                            mime: blob.type || '',
                            data: String(reader.result || '').split(',')[1] || ''
                        });
                        reader.onerror = () => resolve({ok: false, error: String(reader.error || 'reader error')});
                        reader.readAsDataURL(blob);
                    }))
                    .catch(e => ({ok: false, error: String(e)}));
            """, src, timeout=12)
            if isinstance(res, dict) and res.get("ok") and res.get("data"):
                data = base64.b64decode(res["data"])
                path = self._write_image_bytes(download_dir, data, src, res.get("mime") or "")
                if path:
                    return path
            elif isinstance(res, dict) and res.get("error"):
                status(f"[{self.email}] Browser fetch gambar gagal: {res.get('error')}")
        except Exception as exc:
            status(f"[{self.email}] Browser fetch gambar gagal: {exc}")

        if image_ele:
            try:
                res = page.run_js("""
                    const img = arguments[0];
                    if (!img || !img.naturalWidth || !img.naturalHeight) {
                        return {ok:false, error:'image element belum siap'};
                    }
                    const canvas = document.createElement('canvas');
                    canvas.width = img.naturalWidth;
                    canvas.height = img.naturalHeight;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0);
                    const dataUrl = canvas.toDataURL('image/png');
                    return {ok:true, mime:'image/png', data:dataUrl.split(',')[1] || ''};
                """, image_ele, timeout=20)
                if isinstance(res, dict) and res.get("ok") and res.get("data"):
                    data = base64.b64decode(res["data"])
                    path = self._write_image_bytes(download_dir, data, src, res.get("mime") or "image/png")
                    if path:
                        return path
                elif isinstance(res, dict) and res.get("error"):
                    status(f"[{self.email}] Canvas fallback gambar gagal: {res.get('error')}")
            except Exception as exc:
                status(f"[{self.email}] Canvas fallback gambar gagal: {exc}")

        try:
            with urllib.request.urlopen(src, timeout=10) as resp:
                data = resp.read()
            return self._write_image_bytes(download_dir, data, src, "")
        except Exception as exc:
            status(f"[{self.email}] Direct URL gambar gagal: {exc}")
        return None

    def _download_result_image(self, page, download_dirs, before_files, status, snap):
        """Unduh foto hasil dari viewer. Kembalikan list file baru (kosong=gagal)."""
        for attempt in range(3):
            btn = self._find_image_download_button(page)
            if not btn:
                status(f"[{self.email}] Tombol download foto belum terlihat ({attempt + 1}/3).")
                if not self._sleep(1.5):
                    return []
                continue
            status(f"[{self.email}] Klik download foto ({attempt + 1}/3)...")
            try:
                self._coord_click(page, btn, 1.5)
            except Exception:
                try:
                    page.run_js("arguments[0].click();", btn)
                except Exception:
                    pass
                self._sleep(1.2)
            snap(f"after_dl_{attempt}")

            # Submenu opsional: pilih format/kualitas gambar bila muncul.
            for lbl in ("PNG", "JPG", "JPEG", "Original", "Asli", "High quality",
                        "Kualitas tinggi", "Download", "Unduh", "Download image",
                        "Unduh gambar", "Save image", "Simpan gambar", "1080", "2K", "4K"):
                hits = self._visible(page, f"text:{lbl}", timeout=0.3)
                if hits:
                    self._coord_click(page, hits[0], 1)
                    break

            new_files = self._wait_new_download_files(download_dirs, before_files, 35, status)
            if new_files:
                return new_files
            status(f"[{self.email}] Klik download belum memicu file ({attempt + 1}/3).")
        return []

    def _wait_and_download_image(self, page, out_dir, download_dirs, base_img_n, status, stopped):
        """Tunggu foto hasil Nano Banana muncul lalu unduh. Hasil image = <img>
        media server BARU (jumlah bertambah dari baseline). Download: hover kartu
        → menu ⋮ → 'Download'/'Unduh', atau klik kanan → Save (fallback)."""
        debug = bool(os.environ.get("GAMA_DEBUG_IMG"))
        dbg_dir = Path(out_dir) / "_debug_flow"
        if debug:
            dbg_dir.mkdir(parents=True, exist_ok=True)

        def snap(tag):
            if not debug:
                return
            try:
                page.get_screenshot(path=str(dbg_dir), name=f"img_{tag}.png", full_page=True)
            except Exception:
                pass

        status(f"[{self.email}] [8/8] Menunggu foto hasil Nano Banana (maks 5 menit)...")
        deadline = time.time() + 300
        wait_start = time.time()
        new_card = None
        while time.time() < deadline:
            if stopped():
                return False
            # Kredit habis / ditolak
            for kw in ("not enough credits", "out of credits", "Upgrade to continue",
                       "kredit tidak cukup", "tidak dapat", "can't generate", "cannot generate",
                       "violates", "melanggar", "tidak didukung", "unable to"):
                if self._visible(page, f"text:{kw}", timeout=0.5):
                    snap("rejected")
                    status(f"[{self.email}] ERROR: Google menolak generate foto ('{kw}'). "
                           f"Coba prompt/gambar lain.")
                    return False

            imgs = self._list_result_images(page)
            # Gambar hasil = kartu media melebihi baseline (frame upload sudah ada
            # di baseline). Ambil yang paling kiri-atas sebagai hasil terbaru.
            if len(imgs) > base_img_n:
                imgs.sort(key=lambda it: (it[0].rect.location[1], it[0].rect.location[0]))
                new_card = imgs[0][0]
                # Pastikan bukan masih placeholder loading: tunggu progres % hilang
                busy = False
                for e in self._visible(page, "text:%", timeout=0.5):
                    try:
                        if re.fullmatch(r"\d{1,3}%", (e.text or "").strip()) and e.rect.location[1] < 800:
                            busy = True
                            break
                    except Exception:
                        pass
                if not busy:
                    status(f"[{self.email}] Foto hasil terdeteksi ({len(imgs)} kartu, baseline {base_img_n}).")
                    snap("done")
                    break
                new_card = None

            if debug and int(time.time() - wait_start) % 15 < 3:
                snap(f"wait_{int(time.time() - wait_start)}")
            if not self._sleep(4):
                return False

        if new_card is None:
            snap("timeout")
            status(f"[{self.email}] ERROR: Foto hasil tidak muncul (timeout / mungkin ditolak Google).")
            return False

        # Buka kartu hasil → viewer, lalu download.
        before_files = self._snapshot_download_dirs(download_dirs)
        self._coord_click(page, new_card, 2.5)
        snap("viewer")

        if debug:
            # Log semua button di header viewer untuk memetakan tombol download.
            for b in self._visible(page, "tag:button", timeout=1):
                try:
                    meta = self._button_meta_text(b)
                    loc = b.rect.location
                    if meta and loc[1] < 120:
                        status(f"[DEBUG] btn header: '{meta[:40]}' @ {loc}")
                except Exception:
                    pass

        new_files = []
        status(f"[{self.email}] Menyimpan foto langsung dari viewer...")
        for img_ele, src in self._viewer_image_candidates(page, new_card):
            saved = self._save_image_from_browser(page, download_dirs[0], img_ele, src, status)
            if saved:
                new_files = [saved]
                status(f"[{self.email}] Foto berhasil disimpan cepat: {saved.name}")
                break

        if not new_files:
            status(f"[{self.email}] Simpan cepat gagal, coba tombol download Flow...")
            new_files = self._download_result_image(page, download_dirs, before_files, status, snap)

        if not new_files:
            status(f"[{self.email}] Download UI gagal, simpan gambar langsung dari browser...")
            for img_ele, src in self._viewer_image_candidates(page, new_card):
                saved = self._save_image_from_browser(page, download_dirs[0], img_ele, src, status)
                if saved:
                    new_files = [saved]
                    status(f"[{self.email}] Fallback browser berhasil menyimpan foto: {saved.name}")
                    break

        self._close_viewer(page)
        if not new_files:
            snap("nodownload")
            status(f"[{self.email}] ERROR: Foto sudah jadi tetapi gagal diunduh.")
            return False

        final_paths = self._finalize_downloaded_files(out_dir, new_files, status)
        if not final_paths:
            status(f"[{self.email}] ERROR: File foto terdownload tetapi finalisasi gagal.")
            return False
        names = ", ".join(p.name for p in final_paths)
        self.last_output_paths = [str(p) for p in final_paths]
        status(f"[{self.email}] Foto final tersimpan di folder produk: {out_dir}")
        status(f"[{self.email}] File final: {names}")
        status(f"[{self.email}] Generate foto selesai sukses!")
        return True

    def run_generate(self, status_cb=None):
        def status(msg):
            # Pakai satu jalur log saja supaya tidak dobel (status_cb & log_cb di UI
            # biasanya mengarah ke box log yang sama).
            if status_cb:
                status_cb(msg)
            else:
                self.session.log(msg)

        def stopped():
            if self._stopped():
                status(f"[{self.email}] Proses dihentikan oleh user.")
                return True
            return False

        if stopped():
            return False

        status(f"[{self.email}] Memulai inisialisasi browser...")
        try:
            self.session.launch_browser(self.FLOW_URL, headful=True)
        except Exception as e:
            status(f"[{self.email}] ERROR launching browser: {e}")
            return False

        if not self._sleep(3) or stopped():
            return False

        # Debug screenshot opsional (env GAMA_DEBUG_IMG=1) untuk menelusuri
        # tahap upload/generate saat bermasalah.
        _dbg = bool(os.environ.get("GAMA_DEBUG_IMG"))
        _dbg_dir = Path(self.image_path).resolve().parent / "_debug_flow"

        def dbg_snap(tag):
            if not _dbg:
                return
            try:
                _dbg_dir.mkdir(parents=True, exist_ok=True)
                page.get_screenshot(path=str(_dbg_dir), name=f"run_{tag}.png", full_page=True)
            except Exception:
                pass

        page = None
        try:
            status(f"[{self.email}] Menghubungkan ke browser session...")
            page = self.session.get_page()
            if not self._sleep(3) or stopped():
                return False

            status(f"[{self.email}] Verifikasi status login di Google Flow...")
            if "signin" in page.url or "accounts.google.com" in page.url:
                status(f"[{self.email}] ERROR: Akun belum login. Silakan login terlebih dahulu.")
                return False

            self._dismiss_dialogs(page)
            self._handle_consent(page)

            # STEP 1: Buka proyek baru. Label tombol tergantung bahasa UI
            # ('add_2New project' / 'add_2Project baru') → cocokkan awalan ikonnya.
            status(f"[{self.email}] [1/8] Membuka proyek baru...")

            def find_new_project():
                for _ in range(5):
                    btn = self._button_starts(page, "add_2")
                    if btn:
                        return btn
                    if not self._sleep(1):
                        return None
                return None

            np_btn = find_new_project()
            if not np_btn:
                # Akun yang baru pertama kali buka Flow mendarat di halaman marketing:
                # klik CTA 'Create with Google Flow' dulu untuk masuk ke dashboard.
                for label in ("Create with Google Flow", "Buat dengan Google Flow",
                              "Get started", "Mulai", "Mulai sekarang"):
                    ctas = [e for e in self._visible(page, f"text={label}", timeout=2) if e.rect.location[1] < 900]
                    if ctas:
                        status(f"[{self.email}] Halaman perkenalan terdeteksi, masuk ke dashboard Flow...")
                        self._coord_click(page, ctas[0], 8)
                        break
                self._dismiss_dialogs(page)
                self._handle_consent(page)
                np_btn = find_new_project()
            if not np_btn:
                status(f"[{self.email}] ERROR: Tombol 'New project' tidak ditemukan.")
                return False
            self._coord_click(page, np_btn, 7)
            if stopped():
                return False

            # STEP 2-3: Tutup panel chat agent bila terbuka, matikan mode agentic,
            # lalu pastikan chip pengaturan model muncul di composer.
            # Chip model = button di composer bawah yang teksnya memuat 'crop_'
            # (contoh: 'Video · 10scrop_9_161x' atau 'Nano Banana 2crop_16_9x2').
            status(f"[{self.email}] [2/8] Menyiapkan mode Video...")

            def find_model_chip():
                for b in self._visible(page, "tag:button"):
                    try:
                        t = (b.text or "").strip()
                        meta = self._button_meta_text(b)
                        hay = (t + " " + meta).lower()
                        y = b.rect.location[1]
                        if y > 180 and (
                            "crop_" in hay
                            or "nano banana" in hay
                            or "veo" in hay
                            or "omni" in hay
                            or "video" in hay and ("1x" in hay or "x2" in hay or "9_16" in hay or "16_9" in hay)
                        ):
                            return b
                    except Exception:
                        pass
                return None

            def model_popover_is_open():
                checks = (
                    lambda: self._button_starts(page, "play_circle"),
                    lambda: self._button_starts(page, "crop_free"),
                    lambda: self._button_contains(page, "Nano Banana"),
                    lambda: self._button_contains(page, "Veo"),
                    lambda: self._button_contains(page, "Omni"),
                    lambda: self._button_eq(page, "1x"),
                    lambda: self._button_eq(page, "x2"),
                    lambda: self._button_starts(page, "crop_9_16"),
                    lambda: self._button_starts(page, "crop_16_9"),
                    lambda: self._button_starts(page, "crop_square"),
                )
                for fn in checks:
                    try:
                        if fn():
                            return True
                    except Exception:
                        pass
                return False

            chip = None
            for attempt in range(5):
                if stopped():
                    return False
                chip = find_model_chip()
                if chip:
                    break
                # Panel chat agent menutupi composer → tutup dulu (button 'closeClose'/'closeTutup')
                close_btn = self._button_starts(page, "close")
                if close_btn and close_btn.rect.location[1] < 300:
                    self._coord_click(page, close_btn, 2)
                    continue
                # Mode agentic aktif → matikan lewat pill 'Agent'/'Agen' (label ikut bahasa UI)
                pill = (self._button_eq(page, "Agent", min_y=180)
                        or self._button_eq(page, "Agen", min_y=180))
                if pill:
                    self._coord_click(page, pill, 2)
                    continue
                if not self._sleep(3):
                    return False
            if not chip:
                status(f"[{self.email}] ERROR: Chip pengaturan model tidak ditemukan di composer.")
                return False

            # STEP 4: Buka popover chip → tab Video → pilih model/agent → rasio → durasi → jumlah output
            status(f"[{self.email}] [3/8] Memilih model {self.model}, rasio {self.ratio}, durasi {self.duration}, {self.count} output...")
            popover_open = False
            for attempt in range(6):
                if stopped():
                    return False
                chip = find_model_chip()
                if not chip:
                    if not self._sleep(2):
                        return False
                    continue
                try:
                    self._coord_click(page, chip, 1.2)
                except Exception:
                    pass
                if model_popover_is_open():
                    popover_open = True
                    break
                # Flow kadang menempatkan panah dropdown di sisi kanan chip, dan
                # klik tengah chip hanya fokus composer. Klik beberapa titik aman.
                try:
                    loc = chip.rect.location
                    size = chip.rect.size
                    points = (
                        (int(loc[0] + size[0] - 22), int(loc[1] + size[1] / 2)),
                        (int(loc[0] + 22), int(loc[1] + size[1] / 2)),
                        (int(loc[0] + size[0] / 2), int(loc[1] + size[1] - 8)),
                    )
                    for pt in points:
                        page.actions.move_to(pt).click()
                        if not self._sleep(1):
                            return False
                        if model_popover_is_open():
                            popover_open = True
                            break
                    if popover_open:
                        break
                except Exception:
                    pass
                try:
                    page.run_js("arguments[0].click();", chip)
                    if not self._sleep(1):
                        return False
                    if model_popover_is_open():
                        popover_open = True
                        break
                except Exception:
                    pass
            if not popover_open:
                self._dump_bottom_buttons(page, status, "model popover gagal")
                status(f"[{self.email}] ERROR: Popover pengaturan model tidak mau terbuka.")
                return False
            tab_video = (self._button_starts(page, "videocam")
                         or self._button_starts(page, "play_circle")
                         or self._find_text_hit(page, ("Video",), timeout=1))
            if tab_video:
                self._coord_click(page, tab_video, 1.5)

            # Aktifkan mode FRAME (image-to-video): memunculkan slot 'Start/Mulai' di
            # composer untuk gambar frame awal. Tanpa ini, gambar tidak jadi referensi
            # video → hasil tidak mengikuti produk. (icon 'crop_free' = tab Frame/Frames)
            frame_tab = (self._button_starts(page, "crop_free")
                         or self._find_text_hit(page, ("Frame", "Frames", "Bingkai"), timeout=1))
            if frame_tab:
                self._coord_click(page, frame_tab, 1.5)
                status(f"[{self.email}] Mode Frame (image-to-video) aktif.")
            else:
                status(f"[{self.email}] WARNING: Tab Frame tidak ditemukan, gambar mungkin tidak jadi referensi.")

            def select_model_dropdown():
                ok = False
                drops = self._visible(page, "text:arrow_drop_down", timeout=3)
                if drops:
                    self._coord_click(page, drops[0], 1.5)
                    # Cocokkan opsi model persis dengan nama agent yang dipilih user
                    for o in self._visible(page, f"text={self.model}", timeout=3):
                        if (o.text or "").strip() == self.model:
                            self._coord_click(page, o, 1.5)
                            ok = True
                            break
                    if not ok:
                        # Fallback: cocokkan sebagian (mis. 'Nano Banana 2' / 'Veo 3.1 - Fast')
                        for o in self._visible(page, f"text:{self.model}", timeout=1):
                            if self.model.lower() in (o.text or "").strip().lower():
                                self._coord_click(page, o, 1.5)
                                ok = True
                                break
                if ok:
                    status(f"[{self.email}] Model {self.model} dipilih.")
                else:
                    status(f"[{self.email}] WARNING: Model '{self.model}' tidak ditemukan di dropdown, pakai model aktif.")
                return ok

            def apply_ratio():
                ratio_map = {"9:16": "crop_9_16", "16:9": "crop_16_9",
                             "1:1": "crop_1_1", "4:3": "crop_4_3", "3:4": "crop_3_4"}
                ratio_txt = ratio_map.get(self.ratio)
                if not ratio_txt:
                    status(f"[{self.email}] WARNING: Rasio {self.ratio} tidak dikenali, pakai default.")
                    return
                hit = self._button_starts(page, ratio_txt)
                if not hit and self.ratio == "1:1":
                    hit = self._button_starts(page, "crop_square")
                if hit:
                    self._coord_click(page, hit, 1)
                else:
                    status(f"[{self.email}] WARNING: Opsi rasio {self.ratio} tidak ditemukan.")

            def apply_count():
                # Jumlah output per generate: tombol '1x' / 'x2' / 'x3' / 'x4'
                count_label = {1: "1x", 2: "x2", 3: "x3", 4: "x4"}[self.count]
                cnt_btn = self._button_eq(page, count_label)
                if cnt_btn:
                    self._coord_click(page, cnt_btn, 1)
                else:
                    status(f"[{self.email}] WARNING: Opsi jumlah {count_label} tidak ditemukan, pakai default.")

            if not self.is_image:
                select_model_dropdown()
                apply_ratio()
                # Durasi video: tombol '4s'/'6s'/'8s'/'10s'. Hanya Omni Flash yang punya
                # opsi durasi; Veo 3.1 durasinya fixed (tidak ada tombol) → dilewati.
                if re.fullmatch(r"\d{1,2}s", self.duration):
                    dur_btn = self._button_eq(page, self.duration)
                    if dur_btn:
                        self._coord_click(page, dur_btn, 1)
                    else:
                        status(f"[{self.email}] WARNING: Opsi durasi {self.duration} tidak ditemukan, pakai default.")
                else:
                    status(f"[{self.email}] Durasi otomatis (model {self.model} tidak punya opsi durasi).")
                apply_count()
            else:
                # Mode image: composer sementara dibiarkan di tab Video+Frame hanya
                # untuk membuka slot upload; pengaturan model Nano Banana dilakukan
                # setelah gambar selesai diupload (lihat blok switch di bawah).
                status(f"[{self.email}] Mode Image (Nano Banana): pakai slot Frame untuk upload gambar dulu.")

            try:
                page.actions.key_down('ESCAPE').key_up('ESCAPE')
            except Exception:
                pass
            if not self._sleep(1) or stopped():
                return False

            # Validasi: chip harus sudah mode Video sebelum lanjut (upload gambar
            # lewat slot Frame butuh composer mode Video, termasuk untuk job image).
            chip = find_model_chip()
            if chip and "Video" not in (chip.text or ""):
                status(f"[{self.email}] ERROR: Composer masih mode gambar ({(chip.text or '')[:30]}), batal submit.")
                return False

            # STEP 5: Upload gambar sebagai FRAME AWAL (image-to-video).
            # Alur benar: klik slot 'Start/Mulai' → media picker terbuka → klik
            # 'Upload media' (file dialog diisi via set.upload_files) → dialog hak
            # gambar 'Pemberitahuan' → 'Saya setuju' → tunggu thumbnail muncul di
            # picker → klik thumbnail untuk mengisi slot frame.
            img_path = str(Path(self.image_path).absolute())
            img_name = os.path.basename(self.image_path)
            status(f"[{self.email}] [4/8] Mengunggah gambar sebagai frame awal: {img_name}...")

            def find_start_slot():
                for sel in ("tag:button", "tag:div", "tag:span"):
                    for e in self._visible(page, sel, timeout=0.7):
                        try:
                            text = (e.text or "").strip()
                            if text not in ("Start", "Mulai"):
                                continue
                            loc = e.rect.location
                            size = e.rect.size
                            if size[0] <= 0 or size[1] <= 0:
                                continue
                            # Window sekarang 1280x680 seperti OMNIVORA FIX, jadi
                            # slot Frame ada di area tengah-bawah, bukan y>600 lagi.
                            if 160 < loc[1] < 670:
                                return e
                        except Exception:
                            pass
                return None

            # Deteksi gambar terupload lewat elemen <img> ASLI (src media Google),
            # BUKAN teks nama file (yang hanya render saat hover). Jauh lebih andal &
            # cepat — tidak perlu menunggu 90 detik sia-sia. (Didefinisikan sebelum
            # blok upload karena dipakai untuk memverifikasi upload berhasil.)
            stem = os.path.splitext(img_name)[0]

            def find_uploaded_img():
                # HANYA gambar yang SUDAH terupload ke server Google (URL media), BUKAN
                # preview lokal (blob:/data:) yang muncul sebelum upload selesai — kalau
                # tidak, bot mengira upload beres padahal masih proses → error prematur.
                best = None
                best_key = None
                for e in self._visible(page, "tag:img", timeout=0.5):
                    try:
                        src = (e.attr("src") or "").lower()
                        if not src or "/a/" in src or "flower" in src:
                            continue  # avatar profil / placeholder bunga
                        if not any(k in src for k in
                                   ("labs.google", "googleusercontent", "getmediaurl", "/media", "storage.googleapis")):
                            continue  # bukan gambar hasil upload di server (mis. blob/data preview)
                        loc = e.rect.location
                        sz = e.rect.size
                        if sz[0] > 50 and sz[1] > 50 and loc[1] > 60:
                            key = (loc[1], loc[0])  # paling kiri-atas = paling relevan
                            if best_key is None or key < best_key:
                                best, best_key = e, key
                    except Exception:
                        pass
                return best

            start_slot = find_start_slot()
            if not start_slot:
                for label in ("Start", "Mulai"):
                    hits = self._visible(page, f"text={label}", timeout=1)
                    for hit in hits:
                        try:
                            if hit.rect.size[0] > 0 and hit.rect.size[1] > 0:
                                start_slot = hit
                                break
                        except Exception:
                            pass
                    if start_slot:
                        break
            if not start_slot:
                self._dump_bottom_buttons(page, status, "slot Start/Mulai gagal")
                status(f"[{self.email}] ERROR: Slot frame awal (Start/Mulai) tidak ditemukan - mode Frame gagal aktif.")
                return False
            self._coord_click(page, start_slot, 2)  # buka media picker
            dbg_snap("picker_open")

            # Upload gambar. Cara PALING andal: isi langsung <input type="file">
            # (tak bergantung timing dialog native yang kadang gagal senyap untuk
            # nama file berspasi/berkurung atau akun lambat). Fallback: arm
            # upload_files + klik 'Upload media'.
            def direct_file_input():
                try:
                    inputs = page.eles('xpath://input[@type="file"]', timeout=2)
                except Exception:
                    inputs = []
                ok = False
                for inp in inputs:
                    try:
                        inp.input(img_path)
                        ok = True
                    except Exception:
                        pass
                return ok

            armed = direct_file_input()
            if armed:
                status(f"[{self.email}] Gambar diisi langsung ke input file.")
                self._sleep(2)

            # Fallback / pelengkap: klik 'Upload media' dengan file di-arm.
            if not armed or find_uploaded_img() is None:
                um = None
                for lbl in ("Upload media", "Unggah media", "Upload"):
                    hits = self._visible(page, f"text={lbl}", timeout=1)
                    if hits:
                        um = hits[0]
                        break
                if um is None and not armed:
                    status(f"[{self.email}] ERROR: Tombol 'Upload media' tidak ditemukan di picker.")
                    return False
                if um is not None:
                    try:
                        page.set.upload_files(img_path)
                    except Exception:
                        pass
                    self._coord_click(page, um, 2)
                    try:
                        page.wait.upload_paths_inputted()
                    except Exception:
                        pass
                    # Dialog kadang memunculkan input file baru → coba isi lagi.
                    direct_file_input()

            def find_add_to_prompt_item():
                for lbl in ("Add to prompt", "Add to Prompt",
                            "Tambahkan ke perintah", "Tambah ke perintah",
                            "Tambahkan ke prompt", "Tambahkan ke Prompt",
                            "Tambah ke prompt", "Tambah ke Prompt"):
                    hits = self._visible(page, f"text={lbl}", timeout=0.4)
                    if hits:
                        return hits[0]
                return None

            # STEP 6: setujui dialog hak gambar (jika muncul) & tunggu upload selesai —
            # STEP 6a: setujui dialog hak gambar bila muncul (maks ~18 dtk), lalu tutup
            # picker. Dialog 'Saya setuju' hanya muncul sekali per akun.
            status(f"[{self.email}] [5/8] Konfirmasi hak gambar & menunggu upload selesai...")
            consent_deadline = time.time() + 18
            while time.time() < consent_deadline:
                if stopped():
                    return False
                clicked = False
                for lbl in ("Saya setuju", "I agree", "Agree", "Setuju"):
                    hits = self._visible(page, f"text={lbl}", timeout=0.3)
                    if hits:
                        self._coord_click(page, hits[0], 1.5)
                        status(f"[{self.email}] Dialog hak gambar disetujui.")
                        clicked = True
                        break
                if clicked:
                    break
                # Gambar server sudah tampil di picker → tidak akan ada dialog lagi
                if find_uploaded_img() is not None:
                    break
                if not self._sleep(1.2):
                    return False

            # Tutup picker → upload lanjut di background & gambar akan muncul di grid
            try:
                page.actions.key_down('ESCAPE').key_up('ESCAPE')
            except Exception:
                pass
            if not self._sleep(1.5) or stopped():
                return False

            dbg_snap("after_consent")

            # STEP 6b: TUNGGU upload benar-benar selesai = gambar (URL media server)
            # muncul di grid. Di sinilah kesabaran menunggu upload (maks 3 menit),
            # jadi bot tidak error prematur saat upload masih proses.
            card = None
            grid_deadline = time.time() + 180
            _snapped = set()
            while time.time() < grid_deadline:
                if stopped():
                    return False
                card = find_uploaded_img()
                if card:
                    break
                if _dbg:
                    el = int(time.time() - (grid_deadline - 180))
                    bucket = el // 20
                    if bucket not in _snapped:
                        _snapped.add(bucket)
                        dbg_snap(f"grid_wait_{el}")
                if not self._sleep(2):
                    return False
            if card is None:
                dbg_snap("grid_timeout")
                status(f"[{self.email}] ERROR: Gagal mengunggah gambar ke Google Flow (timeout). "
                       f"Akun ini mungkin belum bisa upload media di Flow (baru/dibatasi) atau koneksi lambat. "
                       f"Coba akun lain, atau buka Flow manual di akun ini untuk memastikan fitur upload aktif.")
                return False

            # Job IMAGE (Nano Banana): gambar sudah terupload ke library — sekarang
            # pindahkan composer dari mode Video ke tab Image + pilih model, SEBELUM
            # gambar dilampirkan (lampiran mengikuti mode composer yang aktif).
            if self.is_image:
                status(f"[{self.email}] Memindahkan composer ke mode Image (Nano Banana)...")
                switched = False
                for attempt in range(3):
                    if stopped():
                        return False
                    chip = find_model_chip()
                    if not chip:
                        if not self._sleep(2):
                            return False
                        continue
                    self._coord_click(page, chip, 2)
                    # Tab Image di popover: ikon 'image' (label ikut bahasa UI)
                    img_tab = None
                    for prefix in ("imageImage", "imageGambar", "image"):
                        img_tab = self._button_starts(page, prefix)
                        if img_tab:
                            break
                    if not img_tab:
                        img_tab = self._find_text_hit(page, ("Image", "Gambar"), timeout=1)
                    if img_tab:
                        self._coord_click(page, img_tab, 1.5)
                        select_model_dropdown()
                        apply_ratio()
                        apply_count()
                        try:
                            page.actions.key_down('ESCAPE').key_up('ESCAPE')
                        except Exception:
                            pass
                        if not self._sleep(1):
                            return False
                        chip = find_model_chip()
                        if chip and "Video" not in (chip.text or ""):
                            switched = True
                            status(f"[{self.email}] Composer sudah mode Image: {(chip.text or '')[:40]}")
                            break
                    status(f"[{self.email}] Tab Image belum aktif, mengulang... ({attempt + 1}/3)")
                    try:
                        page.actions.key_down('ESCAPE').key_up('ESCAPE')
                    except Exception:
                        pass
                    if not self._sleep(1.5):
                        return False
                if not switched:
                    status(f"[{self.email}] ERROR: Gagal memindahkan composer ke mode Image (Nano Banana).")
                    return False

            status(f"[{self.email}] Upload selesai. Melampirkan gambar via menu ⋮...")

            # Lampirkan lewat menu ⋮ pada kartu gambar di grid: hover → ⋮ → 'Add to prompt'.
            # (Tombol 'Add to Prompt' di picker sering gagal, jadi pakai menu ⋮ kartu.)
            attached = False
            for attempt in range(6):
                if stopped():
                    return False
                card = find_uploaded_img()
                if not card:
                    if not self._sleep(1.2):
                        return False
                    continue
                cloc = card.rect.location
                csz = card.rect.size
                # Hover kartu (mouse asli + event JS) supaya tombol ⋮ muncul
                try:
                    page.actions.move_to((int(cloc[0] + csz[0] / 2), int(cloc[1] + csz[1] / 2)))
                    page.run_js(
                        "arguments[0].dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));"
                        "arguments[0].dispatchEvent(new MouseEvent('mouseenter',{bubbles:true}));",
                        card)
                except Exception:
                    pass
                self._sleep(0.8)
                # Tombol ⋮ (more_vert) di area kartu (beri margin agar toleran)
                m = 12
                more_btn = None
                for b in self._visible(page, "tag:button", timeout=0.5):
                    try:
                        if not (b.text or "").strip().startswith("more_vert"):
                            continue
                        bl = b.rect.location
                        bs = b.rect.size
                        bcx, bcy = bl[0] + bs[0] / 2, bl[1] + bs[1] / 2
                        if (cloc[0] - m <= bcx <= cloc[0] + csz[0] + m
                                and cloc[1] - m <= bcy <= cloc[1] + csz[1] + m):
                            more_btn = b
                            break
                    except Exception:
                        pass
                if more_btn:
                    self._coord_click(page, more_btn, 1.0)
                else:
                    # Fallback: klik pojok kanan-atas kartu (posisi ikon ⋮)
                    page.actions.move_to((int(cloc[0] + csz[0] - 26), int(cloc[1] + 22))).click()
                    self._sleep(1.0)

                # Menu bisa butuh sedikit waktu render → coba beberapa kali cepat
                add_item = None
                for _ in range(4):
                    add_item = find_add_to_prompt_item()
                    if add_item:
                        break
                    if not self._sleep(0.5):
                        return False
                if add_item:
                    self._coord_click(page, add_item, 2.5)
                    attached = True
                    status(f"[{self.email}] Gambar dilampirkan ke prompt via menu ⋮ → Add to prompt.")
                    break
                # Menu tak muncul → tutup lalu ulangi (cepat)
                try:
                    page.actions.key_down('ESCAPE').key_up('ESCAPE')
                except Exception:
                    pass
                status(f"[{self.email}] Menu 'Add to prompt' belum muncul, mengulang... ({attempt + 1}/6)")
                if not self._sleep(1.0):
                    return False

            if not attached:
                status(f"[{self.email}] ERROR: Gagal melampirkan gambar via menu ⋮ (Add to prompt tidak ketemu).")
                return False

            # Beri jeda supaya frame benar-benar terpasang sebelum lanjut ke prompt
            if not self._sleep(3) or stopped():
                return False

            # STEP 7: Isi prompt lalu klik Create.
            # Composer Flow = contenteditable (editor Lexical), BUKAN textarea. Klik ke
            # placeholder span sering tidak memfokuskan input, dan .input() pada editor
            # ini MENUMPUK teks (bukan replace). Solusi: fokus → Ctrl+A → ketik (ketik
            # mengganti seleksi). Verifikasi isi via JS innerText yang andal.
            try:
                from DrissionPage.common import Keys
            except Exception:
                Keys = None
            status(f"[{self.email}] [6/8] Memasukkan prompt ({len(self.prompt)} karakter)...")

            def find_composer_edit():
                candidates = []
                for e in self._visible(page, "@contenteditable=true", timeout=2):
                    try:
                        y = e.rect.location[1]
                        if y > 180:
                            candidates.append((y, e))
                    except Exception:
                        pass
                if candidates:
                    candidates.sort(key=lambda item: item[0], reverse=True)
                    return candidates[0][1]
                for e in self._visible(page, "tag:textarea", timeout=1):
                    return e
                return None

            def composer_text(box):
                # Baca isi editor secara andal via JS (box.text sering kosong di Lexical)
                if not box:
                    return ""
                for getter in ("innerText", "textContent", "value"):
                    try:
                        val = page.run_js(f"return arguments[0].{getter} || '';", box)
                        if val and val.strip():
                            return val.strip()
                    except Exception:
                        pass
                try:
                    return (box.text or "").strip()
                except Exception:
                    return ""

            entered = False
            for attempt in range(3):
                if stopped():
                    return False
                box = find_composer_edit()
                if box:
                    self._coord_click(page, box, 0.4)
                else:
                    for txt in ("What do you want to create", "Apa yang ingin"):
                        hits = self._visible(page, f"text:{txt}", timeout=1)
                        if hits:
                            self._coord_click(page, hits[0], 0.4)
                            box = None
                            break
                    else:
                        # Fallback: klik tombol Create sebagai anchor lalu geser ke area teks
                        anchor = self._button_starts(page, "arrow_forward", min_y=180)
                        if anchor:
                            loc = anchor.rect.location
                            page.actions.move_to((int(loc[0] - 200), int(loc[1] - 30))).click()
                # Select-all + hapus lalu ketik ulang → mencegah teks menumpuk
                try:
                    if Keys:
                        page.actions.key_down(Keys.CTRL).type('a').key_up(Keys.CTRL)
                        page.actions.type(Keys.BACKSPACE)
                except Exception:
                    pass
                self._sleep(0.3)
                page.actions.type(self.prompt)
                if not self._sleep(1):
                    return False

                current = composer_text(find_composer_edit())
                if current:
                    # Terisi benar bila panjang mendekati prompt & tidak menumpuk (>1.5x)
                    if len(self.prompt) * 0.7 <= len(current) <= len(self.prompt) * 1.5:
                        entered = True
                        break
                    status(f"[{self.email}] Prompt terbaca {len(current)} char (target {len(self.prompt)}), mengulang ({attempt + 1}/3)...")
                else:
                    # Pembacaan tidak konklusif: percaya ketikan sudah masuk, lanjut.
                    entered = True
                    break

            if not entered:
                status(f"[{self.email}] ERROR: Gagal mengisi prompt ke composer dengan benar.")
                return False
            if stopped():
                return False

            # Untuk mode IMAGE: out_dir ke output GAMA, bukan folder gambar referensi user.
            # Gambar referensi bisa ada di mana saja (Downloads, Desktop, dll) — GAMA tidak
            # boleh menulis ke sana. Mode VIDEO tetap pakai parent image_path (folder produk).
            if self.is_image:
                out_dir = (runtime_data_root() / "output" / sanitize_email(self.email)).resolve()
            else:
                out_dir = Path(self.image_path).resolve().parent
            out_dir.mkdir(parents=True, exist_ok=True)
            # Folder staging DIPISAH PER SLOT, bukan cuma per email.
            #
            # Dengan beberapa browser paralel memakai AKUN YANG SAMA, semua slot
            # dulu berbagi satu folder ini. Akibatnya berkas milik slot A ikut
            # terlihat "baru" oleh slot B dan C — dan siapa pun yang mengklaim
            # duluan MEMINDAHKANNYA keluar. Slot pemiliknya lalu menunggu berkas
            # yang sudah tidak ada, sampai kehabisan waktu. Terbukti dari log
            # user: 3 browser paralel, scene 1 sukses, scene 2 & 3 menggantung.
            staging_dir = (runtime_data_root() / "output" / "_downloads" /
                           sanitize_email(self.email) /
                           f"slot{self.window_index:02d}").resolve()
            staging_dir.mkdir(parents=True, exist_ok=True)
            # Bersihkan sisa file dari run sebelumnya agar snapshot baseline
            # tidak menganggap file lama sebagai "sudah ada" — kalau tidak
            # dibersihkan, now - base = kosong dan download dianggap gagal
            # padahal file MP4-nya sudah ada dari job sebelumnya.
            for _old in staging_dir.glob("*"):
                try:
                    if _old.is_file():
                        _old.unlink()
                except Exception:
                    pass
            download_watch_dirs = [staging_dir]
            try:
                page.set.download_path(str(staging_dir))
                status(f"[{self.email}] Folder staging: {staging_dir}")
                status(f"[{self.email}] Folder hasil akhir: {out_dir}")
            except Exception as exc:
                status(f"[{self.email}] ERROR: Gagal mengunci folder staging download: {exc}")
                return False

            # Baseline SEBELUM klik Create: kartu sukses/gagal yang sudah ada di grid
            # tidak boleh dianggap hasil run ini (grid Semua Media memuat kartu lama).
            def count_cards():
                if self.is_image:
                    # Kartu hasil image tidak punya ikon play — hitung <img> media
                    # server di area grid (logika sama dengan find_uploaded_img).
                    plays = []
                    for e in self._visible(page, "tag:img", timeout=1):
                        try:
                            src = (e.attr("src") or "").lower()
                            if not src or "/a/" in src or "flower" in src:
                                continue
                            if not any(k in src for k in
                                       ("labs.google", "googleusercontent", "getmediaurl", "/media", "storage.googleapis")):
                                continue
                            loc = e.rect.location
                            sz = e.rect.size
                            if sz[0] > 50 and sz[1] > 50 and 60 < loc[1] < 760:
                                plays.append(e)
                        except Exception:
                            pass
                else:
                    plays = [e for e in self._visible(page, "text=play_circle", timeout=1) if 60 < e.rect.location[1] < 760]
                fails = []
                for fx in ("Failed", "Gagal"):
                    fails += [e for e in self._visible(page, f"text={fx}", timeout=1) if e.rect.location[1] < 760]
                return plays, fails

            base_plays, base_fails = count_cards()
            base_play_n, base_fail_n = len(base_plays), len(base_fails)

            # STEP 7 (lanjutan): Klik Create sekarang
            status(f"[{self.email}] [7/8] Klik Create, memulai generate...")
            create_btn = self._button_starts(page, "arrow_forward", min_y=180)
            if not create_btn:
                # Tombol Create diganti ikon peringatan (ⓘ) bila POIN/KREDIT tidak cukup —
                # umum terjadi pada model mahal (Veo). Beri pesan jelas, bukan sekadar
                # "tombol tidak ditemukan".
                for kw in ("tidak cukup", "not enough", "Dapatkan lebih banyak poin",
                           "Get more", "insufficient", "kredit tidak cukup"):
                    if self._visible(page, f"text:{kw}", timeout=1):
                        status(f"[{self.email}] ERROR: Poin/kredit Google Flow TIDAK CUKUP untuk model '{self.model}'. "
                               f"Pakai Omni Flash (lebih murah) atau tambah poin akun.")
                        return False
                status(f"[{self.email}] ERROR: Tombol Create tidak ditemukan.")
                return False
            self._coord_click(page, create_btn, 3)

            # STEP 8 (IMAGE): alur foto berbeda dari video — tidak ada ikon play,
            # durasi jadi cepat, dan hasil muncul sebagai <img> media server baru.
            if self.is_image:
                return self._wait_and_download_image(
                    page, out_dir, download_watch_dirs, base_play_n, status, stopped)

            # STEP 8: Tunggu kartu video BARU selesai (ikon play bertambah), lalu download.
            # PRINSIP: utamakan menunggu kartu SUKSES. Sebagian output boleh gagal — selama
            # ada minimal 1 sukses, tetap lanjut download. GAGAL total baru dilaporkan bila
            # semua output gagal ATAU lewat masa tenggang tanpa sukses sama sekali.
            status(f"[{self.email}] [8/8] Menunggu video selesai (maks 10 menit)...")
            expected = max(1, self.count)
            grace_no_success = 150  # detik: batas sabar bila ada gagal & belum ada sukses
            wait_start = time.time()
            deadline = time.time() + 600
            done_streak = 0
            download_retry_count = 0
            while time.time() < deadline:
                if stopped():
                    return False
                # Kredit habis / butuh langganan: Flow menampilkan ajakan upgrade
                for kw in ("not enough credits", "out of credits", "Upgrade to continue", "kredit tidak cukup"):
                    if self._visible(page, f"text:{kw}", timeout=1):
                        status(f"[{self.email}] ERROR: Kredit Flow habis atau akun perlu langganan (Google AI Pro). Generate dibatalkan.")
                        return False

                plays, fails = count_cards()
                new_success = len(plays) - base_play_n
                new_fail = len(fails) - base_fail_n

                # Masih diproses jika ada indikator persen (mis. '28%') di area grid
                pct_busy = False
                for e in self._visible(page, "text:%", timeout=1):
                    try:
                        if re.fullmatch(r"\d{1,3}%", (e.text or "").strip()) and e.rect.location[1] < 760:
                            pct_busy = True
                            break
                    except Exception:
                        pass

                # GAGAL TOTAL: hanya bila belum ada satu pun sukses DAN (semua output
                # gagal ATAU sudah lewat masa tenggang). Sukses parsial tidak di-abort.
                if new_success <= 0 and new_fail > 0:
                    if new_fail >= expected or (time.time() - wait_start) > grace_no_success:
                        # Cek unusual activity
                        is_unusual = (
                            self._visible(page, "text:unusual activity", timeout=1)
                            or self._visible(page, "text:aktivitas yang tidak biasa", timeout=1)
                            or self._visible(page, "text:Verify it's you", timeout=1)
                        )
                        # Catatan: "Verifikasi" TIDAK dipakai karena kata ini muncul
                        # di UI Flow normal (mis. label form) dan menyebabkan false-positive
                        # yang memblokir semua thread paralel ke pause loop.
                        if is_unusual and self._pause_cb:
                            # Beri tahu bridge: pause, tunggu user selesaikan verifikasi
                            status(f"[{self.email}] UNUSUAL_ACTIVITY: Google mendeteksi aktivitas tidak wajar. Menunggu user menyelesaikan verifikasi...")
                            self._pause_cb()
                            # Tunggu resume signal (max 10 menit)
                            resume_wait = 0
                            while self._pause_cb and not self._stop_event_check():
                                time.sleep(2)
                                resume_wait += 2
                                if self._resume_check():
                                    status(f"[{self.email}] Resume diterima, melanjutkan...")
                                    break
                                if resume_wait > 600:
                                    status(f"[{self.email}] ERROR: Timeout menunggu resume (10 menit).")
                                    return False
                            # Coba generate ulang setelah resume
                            status(f"[{self.email}] Mencoba generate ulang setelah verifikasi...")
                            return self._retry_after_unusual_activity(page, status)
                        else:
                            reason = ""
                            if is_unusual:
                                reason = " (terdeteksi 'unusual activity' oleh Google — coba akun lain / kurangi frekuensi)"
                            status(f"[{self.email}] ERROR: Generate GAGAL di sisi Google{reason}.")
                            return False

                # SELESAI bila SEMUA output selesai (sukses+gagal >= jumlah diminta),
                # minimal 1 sukses, & tidak ada lagi yang diproses. Untuk output x2/x3/x4
                # ini memastikan bot menunggu semua video jadi sebelum download (zip),
                # bukan langsung download saat baru 1 video selesai.
                all_done = (new_success + new_fail) >= expected
                if new_success >= 1 and all_done and not pct_busy:
                    done_streak += 1
                else:
                    done_streak = 0

                if done_streak >= 3:
                    # Snapshot SEBELUM klik card — file lama di staging tidak
                    # akan ikut dihitung sebagai hasil download baru.
                    before_files = self._snapshot_download_dirs(download_watch_dirs)

                    # Kartu terbaru = paling kiri-atas (grid Flow menaruh hasil baru di depan)
                    newest = min(plays, key=lambda e: (e.rect.location[1], e.rect.location[0]))
                    status(f"[{self.email}] Video tampak selesai, membuka viewer untuk download...")
                    card_body = newest
                    node = newest
                    for _ in range(6):
                        try:
                            node = node.parent()
                            if node.rect.size[0] > 150 and node.rect.size[1] > 200:
                                card_body = node
                                break
                        except Exception:
                            break
                    self._coord_click(page, card_body, 3)

                    dl_btn = None
                    more_btn = None
                    for attempt in range(6):
                        if stopped():
                            return False
                        dl_btn = self._find_download_button(page)
                        more_btn = self._find_more_button(page)
                        back_btn = self._button_starts(page, "arrow_back")
                        if dl_btn or more_btn or back_btn:
                            break
                        if not self._sleep(2):
                            return False

                    new_files = []
                    # Selalu coba download per-video langsung terlebih dahulu,
                    # baik untuk count=1 maupun count>1.  Zip hanya dipakai
                    # sebagai fallback terakhir jika tombol download tidak ada
                    # dan direct download gagal.
                    if dl_btn:
                        new_files = self._attempt_direct_download(page, download_watch_dirs, before_files, status)
                    else:
                        status(f"[{self.email}] Tombol download per-video belum terlihat, akan coba fallback...")

                    if not new_files:
                        # Fallback: unduh seluruh project sebagai zip lalu ekstrak.
                        # Hanya ditempuh jika direct download benar-benar gagal.
                        status(f"[{self.email}] Download langsung tidak jalan, pakai Download Project (zip) sebagai fallback...")
                        new_files = self._download_project_zip(page, download_watch_dirs, before_files, status)

                    if not new_files:
                        download_retry_count += 1
                        status(
                            f"[{self.email}] Download belum berhasil walau video sudah jadi. "
                            f"Tutup viewer dan coba lagi ({download_retry_count}/3)..."
                        )
                        self._close_viewer(page)
                        done_streak = 0
                        if download_retry_count >= 3:
                            status(f"[{self.email}] ERROR: Download gagal setelah 3 percobaan, meski video sudah berhasil dibuat.")
                            return False
                        if not self._sleep(8):
                            return False
                        continue

                    download_retry_count = 0

                    final_paths = self._finalize_downloaded_files(out_dir, new_files, status)
                    if not final_paths:
                        download_retry_count += 1
                        status(
                            f"[{self.email}] File hasil terdownload belum berhasil difinalkan ke MP4. "
                            f"Coba lagi ({download_retry_count}/3)..."
                        )
                        self._close_viewer(page)
                        done_streak = 0
                        if download_retry_count >= 3:
                            status(f"[{self.email}] ERROR: File hasil ada, tapi finalisasi MP4 gagal setelah 3 percobaan.")
                            return False
                        if not self._sleep(8):
                            return False
                        continue

                    final_names = ", ".join(p.name for p in final_paths)
                    self.last_output_paths = [str(p) for p in final_paths]
                    status(f"[{self.email}] Video final tersimpan di folder produk: {out_dir}")
                    status(f"[{self.email}] File final tervalidasi: {final_names}")
                    status(f"[{self.email}] Generate selesai sukses!")
                    return True

                if not self._sleep(5):
                    return False

            status(f"[{self.email}] ERROR: Video belum selesai setelah 10 menit.")
            return False
        except Exception as e:
            status(f"[{self.email}] ERROR selama proses generate: {e}")
            return False
        finally:
            # Selalu tutup browser di akhir (sukses/gagal) agar siap pindah akun berikutnya
            status(f"[{self.email}] Menutup browser...")
            self.session.close_browser(page)
