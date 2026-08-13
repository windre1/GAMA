"""
services/browser_upload_service.py
Spartan Tube — Browser Upload Service

Features:
- Multi-instance: setiap upload pakai port CDP berbeda (9222-9299)
- Invisible mode: headless=new, benar-benar tidak buka window
- Auto-delete: hapus file setelah upload sukses
- Browser tutup otomatis setelah selesai
- Support REAL_CHROME (session) dan COOKIES (inject)
"""

import os
import re
import json
import time
import random
import threading
import logging
from datetime import datetime, timedelta
from utils.csv_loader import load_video_csv
logger = logging.getLogger(__name__)

# Safe path setup
try:
    from config.paths import COOKIES_DIR, PROFILES_DIR, ROOT_DIR
except ImportError:
    from utils.resource_path import get_appdata_path
    _appdata = get_appdata_path()
    COOKIES_DIR  = os.path.join(_appdata, "yt_cookies")
    PROFILES_DIR = os.path.join(_appdata, "chrome_cookies_profiles")
    ROOT_DIR = os.path.dirname(_appdata)

LEGACY_ACCOUNTS_DIR = os.path.join(ROOT_DIR, "data", "youtube_accounts")

# Port manager — tiap instance pakai port random (10000-60000) agar tidak bentrok
_port_lock      = threading.Lock()
_ports_in_use   = set()
_PORT_START     = 10000
_PORT_END       = 60000


def acquire_port():
    """Ambil port CDP random yang bebas."""
    with _port_lock:
        for _ in range(50):
            p = random.randint(_PORT_START, _PORT_END)
            if p not in _ports_in_use:
                _ports_in_use.add(p)
                return p
    raise RuntimeError("Gagal mendapatkan port random! Port penuh atau sistem sibuk.")


def release_port(port):
    if port:
        with _port_lock:
            _ports_in_use.discard(port)


class BrowserUploadService:
    def safe_log(self, msg):
        """Log safely even with encoding restricted environments."""
        text = str(msg)
        try:
            print(text, flush=True)
        except Exception:
            # Console Windows non-UTF8 (cp1252) menolak karakter seperti ✓ ✗ —
            # dan versi lama MENJATUHKAN seluruh barisnya diam-diam, sehingga
            # log sukses/gagal hilang dari stdout. Turunkan ke ASCII saja.
            try:
                print(text.encode('ascii', 'replace').decode('ascii'), flush=True)
            except Exception:
                pass
        try:
            logger.info(text)
        except Exception:
            try:
                logger.info(text.encode("utf-8", "ignore").decode())
            except Exception:
                pass

    def __init__(self, debug_mode=True):
        self.page = None
        self._port = None
        self.debug_mode = debug_mode
        self._stop_requested = False
        self._upload_step = 0

    def _reset_upload_steps(self):
        self._upload_step = 0

    def _step_log(self, msg, pct=None):
        self._upload_step += 1
        prefix = f"STEP {self._upload_step}"
        if pct is not None:
            prefix += f" ({pct}%)"
        self.safe_log(f"[UPLOAD] {prefix} - {msg}")

    def _clean_path(self, path):
        value = str(path or "").strip().strip('"').strip("'")
        for quote in ('"', "'"):
            if quote in value:
                parts = [p.strip() for p in value.split(quote) if p.strip()]
                drive_parts = [p for p in parts if len(p) > 2 and p[1:3] in (":\\", ":/")]
                if drive_parts:
                    value = drive_parts[-1]
                    break
        return os.path.abspath(os.path.expandvars(os.path.expanduser(value)))

    def reset_stop(self):
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True
        try:
            if self.page:
                self.page.quit()
        except Exception:
            pass

    def _raise_if_stopped(self):
        if self._stop_requested:
            raise Exception("Upload stopped by user.")

    # ── Account Helpers ───────────────────────────────────────────────────────────

    def _get_account(self, account_id):
        account_id = str(account_id)
        try:
            from database.db_manager import Account
            # By channel_id
            acc = Account.get_or_none(Account.channel_id == account_id)
            if acc:
                return acc
            # By acc_N format
            if account_id.startswith("acc_"):
                try:
                    db_id = int(account_id.replace("acc_", ""))
                    acc = Account.get_or_none(Account.id == db_id)
                    if acc:
                        return acc
                except ValueError:
                    pass
            # By numeric id
            try:
                acc = Account.get_or_none(Account.id == int(account_id))
                if acc:
                    return acc
            except (ValueError, Exception):
                pass
            # By session_path
            for a in Account.select():
                if a.session_path and account_id in a.session_path:
                    return a
            # By name
            return Account.get_or_none(
                (Account.name == account_id) |
                (Account.channel_name == account_id))
        except Exception as e:
            logger.error(f"_get_account error: {e}")
        return None

    def _get_session_path(self, account_id):
        acc = self._get_account(account_id)
        if acc:
            sp = getattr(acc, 'session_path', None)
            if sp and os.path.exists(sp):
                return sp
        profile_root = os.path.join(PROFILES_DIR, str(account_id))
        if os.path.exists(profile_root):
            return profile_root
        legacy_root = os.path.join(LEGACY_ACCOUNTS_DIR, str(account_id))
        if os.path.exists(legacy_root):
            return legacy_root
        return None

    def _get_cookies_path(self, account_id):
        acc = self._get_account(account_id)
        if not acc:
            return None
        # token_path
        tp = getattr(acc, 'token_path', None) or ""
        if tp and os.path.exists(tp):
            return tp
        # cookies_json → save to file
        cj = getattr(acc, 'cookies_json', None) or ""
        if not cj:
            cj = getattr(acc, 'credentials_json', None) or ""
        if cj and len(str(cj)) > 50:
            d = COOKIES_DIR
            os.makedirs(d, exist_ok=True)
            safe = "".join(c for c in
                (getattr(acc, 'channel_name', '') or
                 getattr(acc, 'name', '') or "acc")
                if c.isalnum() or c in "-_ ")
            path = os.path.join(d, f"{safe}.txt")
            with open(path, 'w', encoding='utf-8') as f:
                f.write(str(cj))
            try:
                acc.token_path = path
                acc.save()
            except Exception:
                pass
            return path
        return None

    # ── Chrome Helpers ────────────────────────────────────────────────────────────

    def _get_chrome_path(self):
        paths = [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
            os.path.expandvars(
                "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"),
        ]
        for p in paths:
            if os.path.exists(p):
                return p
        raise Exception(
            "Chrome tidak ditemukan!\n"
            "Install Google Chrome: https://www.google.com/chrome")

    def kill_chrome_on_port(self, port=9222):
        """Killed process on specific port (Targeted)."""
        import subprocess
        import os
        try:
            # Netstat to find PID on Windows
            out = subprocess.check_output(
                f'netstat -ano | findstr :{port}',
                shell=True, stderr=subprocess.DEVNULL).decode()
            for line in out.strip().split('\n'):
                if f':{port}' in line:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        pid = parts[-1]
                        if pid and pid != '0' and str(pid) != str(os.getpid()):
                            subprocess.run(f"taskkill /F /PID {pid} /T", 
                                           shell=True, capture_output=True)
                            self.safe_log(f"[CLEANUP] Killed process {pid} on port {port}")
        except Exception:
            pass

    def kill_all_chrome_processes(self):
        """Brute-force cleanup of all chrome instances."""
        import subprocess
        try:
            self.safe_log("[CLEANUP] Performing global chrome cleanup...")
            subprocess.run("taskkill /F /IM chrome.exe /T", 
                           shell=True, capture_output=True)
            time.sleep(2)
        except Exception as e:
            self.safe_log(f"[WARNING] Global cleanup error: {e}")

    # ── Anti-Detection ────────────────────────────────────────────────────────────

    def _inject_stealth(self, page):
        stealth = """
        try {
            // 1. Sembunyikan webdriver
            Object.defineProperty(navigator, 'webdriver',
                {get: () => undefined, configurable: true});
            delete navigator.__proto__.webdriver;

            // 2. Plugins realistis (bukan array kosong)
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    var arr = [
                        {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format'},
                        {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:''},
                        {name:'Native Client', filename:'internal-nacl-plugin', description:''}
                    ];
                    arr.__proto__ = PluginArray.prototype;
                    return arr;
                }
            });

            // 3. Languages realistis
            Object.defineProperty(navigator, 'languages',
                {get: () => ['en-US', 'en', 'id']});

            // 4. Chrome runtime lengkap
            window.chrome = {
                runtime: {
                    id: undefined,
                    connect: function(){},
                    sendMessage: function(){},
                    onMessage: { addListener: function(){} }
                },
                loadTimes: function(){ return {
                    requestTime: Date.now()/1000 - Math.random()*2,
                    startLoadTime: Date.now()/1000 - Math.random(),
                    commitLoadTime: Date.now()/1000,
                    finishDocumentLoadTime: Date.now()/1000,
                    finishLoadTime: Date.now()/1000,
                    firstPaintTime: Date.now()/1000,
                    firstPaintAfterLoadTime: 0,
                    navigationType: 'Other',
                    wasFetchedViaSpdy: false,
                    wasNpnNegotiated: false,
                    npnNegotiatedProtocol: 'unknown',
                    wasAlternateProtocolAvailable: false,
                    connectionInfo: 'http/1.1'
                }; },
                csi: function(){ return {
                    startE: Date.now(),
                    onloadT: Date.now(),
                    pageT: Math.random() * 5000 + 1000,
                    tran: 15
                }; },
                app: {}
            };

            // 5. Permissions API realistis
            if (navigator.permissions) {
                var origQuery = navigator.permissions.query.bind(navigator.permissions);
                navigator.permissions.query = function(params) {
                    if (params.name === 'notifications') {
                        return Promise.resolve({state: Notification.permission});
                    }
                    return origQuery(params);
                };
            }

            // 6. Hardware concurrency realistis
            Object.defineProperty(navigator, 'hardwareConcurrency',
                {get: () => 8});

            // 7. Device memory realistis
            Object.defineProperty(navigator, 'deviceMemory',
                {get: () => 8});

            // 8. Platform realistis
            Object.defineProperty(navigator, 'platform',
                {get: () => 'Win32'});

            // 9. Vendor realistis
            Object.defineProperty(navigator, 'vendor',
                {get: () => 'Google Inc.'});

            // 10. Hapus ciri-ciri headless
            Object.defineProperty(navigator, 'maxTouchPoints',
                {get: () => 0});

            // 11. Screen dimensions realistis
            Object.defineProperty(screen, 'width', {get: () => 1920});
            Object.defineProperty(screen, 'height', {get: () => 1080});
            Object.defineProperty(screen, 'availWidth', {get: () => 1920});
            Object.defineProperty(screen, 'availHeight', {get: () => 1040});
            Object.defineProperty(screen, 'colorDepth', {get: () => 24});
            Object.defineProperty(screen, 'pixelDepth', {get: () => 24});

            // 12. WebGL vendor & renderer realistis
            var getParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {
                if (param === 37445) return 'Intel Inc.';
                if (param === 37446) return 'Intel(R) UHD Graphics 620';
                return getParam.call(this, param);
            };
            var getParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {
                if (param === 37445) return 'Intel Inc.';
                if (param === 37446) return 'Intel(R) UHD Graphics 620';
                return getParam2.call(this, param);
            };

        } catch(e) {}
        """
        try:
            page.run_js(stealth)
        except Exception:
            pass

        # Inject via CDP Page.addScriptToEvaluateOnNewDocument
        # agar stealth aktif SEBELUM halaman load (lebih efektif dari run_js)
        try:
            page.run_cdp('Page.addScriptToEvaluateOnNewDocument', source=stealth)
        except Exception:
            pass

    def _stealth_cdp_click(self, page, x, y):
        """
        Klik dengan simulasi gerakan mouse manusia sebelum klik.
        Works di visible & headless via CDP.
        """
        import random as _r
        ix, iy = int(x), int(y)

        # Simulasi mouse move dari posisi acak ke target (gerakan manusia)
        start_x = _r.randint(ix - 200, ix - 50)
        start_y = _r.randint(iy - 100, iy + 100)
        steps = _r.randint(5, 10)

        for i in range(steps):
            progress = (i + 1) / steps
            # Easing: ease-in-out
            ease = progress * progress * (3 - 2 * progress)
            mx = int(start_x + (ix - start_x) * ease)
            my = int(start_y + (iy - start_y) * ease)
            try:
                page.run_cdp('Input.dispatchMouseEvent',
                    type='mouseMoved', x=mx, y=my, button='none')
            except Exception:
                pass
            time.sleep(_r.uniform(0.01, 0.03))

        # Hover sebentar sebelum klik
        time.sleep(_r.uniform(0.05, 0.15))

        # mousePressed + mouseReleased
        try:
            page.run_cdp('Input.dispatchMouseEvent',
                type='mousePressed', x=ix, y=iy, button='left', clickCount=1)
            time.sleep(_r.uniform(0.05, 0.12))
            page.run_cdp('Input.dispatchMouseEvent',
                type='mouseReleased', x=ix, y=iy, button='left', clickCount=1)
        except Exception:
            pass

    # ── Page Factory ──────────────────────────────────────────────────────────────

    def _build_options(self, profile_path, port, invisible):
        """Build ChromiumOptions anti-detection maksimal."""
        from DrissionPage import ChromiumOptions
        import random as _r
        co = ChromiumOptions()
        co.set_local_port(port)
        co.set_user_data_path(os.path.abspath(profile_path))
        co.set_browser_path(self._get_chrome_path())

        # ── Anti-Detection Core ───────────────────────────────────────────
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        co.set_argument("--disable-infobars")

        # ── Hapus tanda-tanda bot ─────────────────────────────────────────
        co.set_argument("--disable-features=ChromeWhatsNewUI,HttpsUpgrades")
        co.set_argument("--disable-component-update")
        co.set_argument("--disable-background-networking")
        co.set_argument("--disable-sync")
        co.set_argument("--metrics-recording-only")
        co.set_argument("--disable-default-apps")
        co.set_argument("--no-pings")

        # ── Stabilitas ────────────────────────────────────────────────────
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--mute-audio")
        co.set_argument("--disable-features=IsolateOrigins,site-per-process")
        co.set_argument("--use-gl=angle")
        co.set_argument("--disable-extensions")

        # ── User-Agent realistis (Windows Chrome terbaru) ─────────────────
        ua_list = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]
        co.set_argument(f"--user-agent={_r.choice(ua_list)}")

        # ── Window size — samakan dengan Flow/generate (1280x680) ───────────
        # Ukuran konsisten membantu elemen UI YouTube Studio ter-render penuh
        # termasuk editor thumbnail dan radio Audience/Altered-content.
        w, h = 1280, 680
        co.set_argument(f"--window-size={w},{h}")

        # ── Gambar WAJIB diizinkan ────────────────────────────────────────
        # Mem-blokir gambar (dulu dipakai untuk menghemat bandwidth di mode
        # invisible) menulis preference PERMANEN ke profil Chrome, sehingga
        # ikut berlaku di mode TERLIHAT pada semua run berikutnya.
        # Akibatnya editor thumbnail YouTube Studio tidak bisa merender gambar
        # yang dipilih: file masuk ke input, event `change` trusted terkirim,
        # tapi preview tidak pernah muncul dan tidak ada pesan error apa pun —
        # persis gejala "upload thumbnail gagal" yang sulit dilacak.
        # Nilai 1 = izinkan; ditulis di SETIAP launch supaya profil yang
        # terlanjur ter-set 2 ikut diperbaiki.
        co.set_pref('profile.managed_default_content_settings.images', 1)
        co.set_pref('profile.default_content_setting_values.images', 1)

        if invisible:
            co.headless = True
            co.set_argument("--disable-gpu")
            co.set_argument(f"--window-size={w},{h}")
            co.set_pref('profile.default_content_setting_values.notifications', 2)
        else:
            co.set_argument(f"--window-size={w},{h}")
            co.set_argument("--window-position=0,0")

        return co

    def _wait_port_ready(self, port, timeout=12):
        """Wait for CDP JSON endpoint to be ready."""
        import urllib.request
        import json
        url = f"http://127.0.0.1:{port}/json"
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        data = json.loads(response.read().decode())
                        if data:
                            self.safe_log(f"[SUCCESS] Chrome CDP connected on port {port}")
                            return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _launch_chrome(self, co, port):
        """Universal Chrome Launcher with 2-stage fail-safe recovery.

        FIX: Stage 2 tidak lagi memanggil kill_all_chrome_processes() karena
        akan membunuh semua Chrome termasuk browser batch video sebelumnya yang
        masih aktif. Cukup kill port spesifik + tunggu lebih lama + retry.
        """
        from DrissionPage import ChromiumPage
        
        # Stage 1: Targeted attempt
        self.safe_log(f"[LAUNCH] Starting Chrome on port {port}...")
        try:
            self.kill_chrome_on_port(port)
            time.sleep(2)
            page = ChromiumPage(co)
            if self._wait_port_ready(port):
                return page
            raise RuntimeError("Port not ready after launch")
        except Exception as e:
            self.safe_log(f"[RETRY] First launch failed: {e}")
            
        # Stage 2: Retry dengan kill port spesifik saja (TIDAK kill semua Chrome)
        # kill_all_chrome_processes() dilarang di sini karena membunuh browser
        # batch video lain yang sedang aktif upload.
        self.safe_log("[LAUNCH] Stage 2 recovery: kill port + retry...")
        try:
            self.kill_chrome_on_port(port)
            time.sleep(4)
            page = ChromiumPage(co)
            if self._wait_port_ready(port, timeout=15):
                return page
        except Exception as e:
            self.safe_log(f"[ERROR] Fail-safe launch attempt failed: {e}")
            
        raise RuntimeError(f"Gagal menjalankan Chrome setelah 2 percobaan (Port {port})")
    def _debug_log(self, msg):
        """Hardware-flushed debug logging to catch hard crashes."""
        if not self.debug_mode: return
        try:
            from config.paths import LOGS_DIR
            import datetime
            log_file = os.path.join(LOGS_DIR, "automation_debug.log")
            timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")
            with open(log_file, "a") as f:
                f.write(f"[{timestamp}] {msg}\n")
                f.flush()
                os.fsync(f.fileno())
        except:
            pass

    def _wait_and_click(self, page, keywords, timeout=15, block_publish=False):
        """Finds any button by label keywords (DOM text) and clicks it (Safe Iteration)."""
        if isinstance(keywords, str): keywords = [keywords]
        keywords = [k.upper() for k in keywords]
        
        # Block Publish keywords when scheduled (unified safety)
        blocked = ["PUBLISH", "PUBLIKASIKAN", "SAVE", "DONE", "SELESAI"]
        if block_publish:
            original_len = len(keywords)
            keywords = [k for k in keywords if not any(b in k for b in blocked)]
            if len(keywords) < original_len:
                self._debug_log("[BATCH] Publish blocked keywords removed from search.")

        start = time.time()
        while time.time() - start < timeout:
            for k in keywords:
                # Use a single-string match to avoid list-parameter errors in DrissionPage
                res = page.run_js("""
                    (function(target, blockPublish){
                        var blocked = ["PUBLISH", "PUBLIKASIKAN", "SAVE", "DONE", "SELESAI"];
                        var btns = document.querySelectorAll('ytcp-button, tp-yt-paper-button, button, a, [role="button"]');
                        for(var b of btns){
                            var t = (b.innerText || b.textContent || b.getAttribute('aria-label') || '').trim().toUpperCase();
                            if(blockPublish && blocked.some(kw => t.includes(kw))) continue;
                            
                            if(t === target || t.startsWith(target + ' ') || t.includes(' ' + target)){
                                var d = b.hasAttribute('disabled') || 
                                        b.getAttribute('aria-disabled') === 'true' ||
                                        b.classList.contains('ytcp-button--disabled');
                                if(!d){ b.click(); return true; }
                            }
                        }
                        return false;
                    })('""" + k + """', """ + str(block_publish).lower() + """)
                """)
                if res:
                    return True
            time.sleep(1)
        return False

    def _click_next(self, page):
        """Cari & klik tombol Next via page.eles() + JS fallback."""
        try:
            btns = []
            for sel in ('tag:ytcp-button', 'tag:tp-yt-paper-button', 'tag:button'):
                try:
                    btns += list(page.eles(sel, timeout=2) or [])
                except Exception:
                    pass
            for b in btns:
                try:
                    txt = (b.text or '').strip().lower()
                except Exception:
                    continue
                if not (txt == 'next' or txt.startswith('next') or
                        txt == 'berikutnya' or txt.startswith('berikut')):
                    continue
                try:
                    if b.attr('disabled') is not None or b.attr('aria-disabled') == 'true':
                        continue
                except Exception:
                    pass
                # Coba native click dulu
                try:
                    b.click()
                    return True
                except Exception:
                    pass
                # Fallback: JS click
                try:
                    page.run_js('arguments[0].click()', b)
                    return True
                except Exception:
                    continue
            # Fallback total: JS querySelector cari tombol NEXT
            try:
                ok = page.run_js("""
                return (function(){
                    var btns = Array.from(document.querySelectorAll('ytcp-button, button, tp-yt-paper-button'));
                    for (var b of btns) {
                        var txt = (b.innerText || b.textContent || '').trim().toUpperCase();
                        if (txt === 'NEXT' || txt === 'BERIKUTNYA' || txt.startsWith('NEXT')) {
                            if (!b.disabled && b.getAttribute('aria-disabled') !== 'true') {
                                b.click();
                                return true;
                            }
                        }
                    }
                    return false;
                })()
                """)
                if ok:
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _get_page(self, session_path, invisible=False):
        """Buka Chrome dengan profile session (REAL_CHROME)."""
        abs_session = os.path.abspath(session_path)
        if os.path.exists(os.path.join(abs_session, "Default")) or os.path.exists(os.path.join(abs_session, "Local State")):
            profile_path = abs_session
        else:
            profile_path = os.path.join(abs_session, "profile")
        os.makedirs(profile_path, exist_ok=True)

        port = self._port or acquire_port()
        self._port = port
        self.kill_chrome_on_port(port)
        time.sleep(1.2)

        co = self._build_options(profile_path, port, invisible)
        self.page = self._launch_chrome(co, port)
        self._inject_stealth(self.page)
        logger.info(f"Chrome launched (session_path={profile_path}, port={port}, headless={invisible})")
        return self.page


    def _get_page_with_cookies(self, cookies_path, invisible=False):
        """Buka Chrome fresh + inject cookies (COOKIES account)."""
        safe = "".join(
            c for c in os.path.splitext(os.path.basename(cookies_path))[0]
            if c.isalnum() or c in "-_ ")
        
        # Legacy fallback check
        profile_path = os.path.join(PROFILES_DIR, "chrome_cookies_profiles", safe)
        if not os.path.exists(profile_path):
            profile_path = os.path.join(PROFILES_DIR, safe)
            
        os.makedirs(profile_path, exist_ok=True)

        port = self._port or acquire_port()
        self._port = port
        self.kill_chrome_on_port(port)
        time.sleep(1.2)

        co = self._build_options(profile_path, port, invisible)
        self.page = self._launch_chrome(co, port)
        self._inject_stealth(self.page)

        # Navigate → inject cookies → refresh
        self.page.get("https://www.youtube.com/?hl=en")
        time.sleep(3)
        self._inject_cookies(self.page, cookies_path)
        time.sleep(0.5)
        self.page.refresh()
        time.sleep(2)

        logger.info(f"Chrome launched (cookies, port={port}, headless={invisible})")
        return self.page

    def _inject_cookies(self, page, cookies_path):
        """Inject Netscape cookies ke browser via CDP."""
        injected = 0
        try:
            with open(cookies_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) < 7:
                    continue
                try:
                    domain, _, path, secure, expiry, name, value = parts[:7]
                    cookie = {
                        'name':   name,
                        'value':  value,
                        'domain': domain if domain.startswith('.') else f".{domain}",
                        'path':   path,
                        'secure': secure.upper() == 'TRUE',
                    }
                    if expiry and expiry not in ('0', ''):
                        try:
                            cookie['expires'] = int(float(expiry))
                        except Exception:
                            pass
                    page.run_cdp('Network.setCookie', **cookie)
                    injected += 1
                except Exception:
                    continue
            logger.info(f"Injected {injected} cookies")
        except Exception as e:
            logger.error(f"Cookie inject error: {e}")

    # ── JS Helpers ────────────────────────────────────────────────────────────────

    def _human_delay(self, min_s=1.0, max_s=3.0):
        end_time = time.time() + random.uniform(min_s, max_s)
        while time.time() < end_time:
            self._raise_if_stopped()
            time.sleep(min(0.25, max(0, end_time - time.time())))

    def _emit_progress(self, cb, pct, msg):
        self._raise_if_stopped()
        self._step_log(msg, pct)
        if cb:
            try:
                cb(pct, msg)
            except Exception:
                pass
        logger.info(f"[{pct}%] {msg}")

    def _wait_upload_100(self, page, cb, timeout=600):
        """Tunggu sampai indikator upload YouTube mencapai 100% sebelum lanjut.

        YouTube Studio menampilkan progress bar dengan teks seperti:
        'Uploading... 45%', 'Processing will begin shortly', 'Upload complete'.
        Engine ini poll setiap 3 detik sampai indikator 100%/selesai muncul
        atau timeout (default 10 menit).

        Ini mencegah batch mode lanjut ke video berikutnya terlalu cepat
        sebelum upload video sebelumnya benar-benar selesai di server YouTube.
        """
        self.safe_log("[WAIT100] Menunggu indikator upload YouTube mencapai 100%...")
        deadline = time.time() + timeout
        last_pct_reported = 90
        check_interval = 3
        # Berapa kali berturut-turut halaman terlihat "tidak sedang mengunggah".
        # Dipakai supaya satu pembacaan sekejap (mis. saat DOM sedang berganti)
        # tidak langsung dianggap selesai.
        tenang = 0

        while time.time() < deadline:
            self._raise_if_stopped()
            try:
                result = page.run_js("""
                return (function(){
                    // Cari elemen progress upload YouTube Studio
                    var selectors = [
                        'ytcp-video-upload-progress',
                        'ytcp-uploads-still-processing-dialog',
                        '#upload-progress',
                        'ytcp-upload-progress-bar',
                    ];
                    for (var s of selectors) {
                        var el = document.querySelector(s);
                        if (el) {
                            var txt = (el.innerText || el.textContent || '').toUpperCase();
                            return { found: true, text: txt };
                        }
                    }
                    // Cari teks "%" di area upload dialog
                    var dialogs = document.querySelectorAll('ytcp-uploads-dialog, ytcp-upload-dialog');
                    for (var d of dialogs) {
                        var txt = (d.innerText || d.textContent || '').toUpperCase();
                        if (txt.includes('%') || txt.includes('UPLOAD') || txt.includes('PROCESS')) {
                            return { found: true, text: txt };
                        }
                    }
                    // Fallback: cek body untuk indikator upload
                    var body = (document.body.innerText || '').toUpperCase();
                    var hasUpload = body.includes('UPLOADING') || body.includes('MENGUPLOAD') ||
                                   body.includes('PROCESSING') || body.includes('MEMPROSES') ||
                                   body.includes('UPLOAD COMPLETE') || body.includes('UPLOAD SELESAI');
                    return { found: hasUpload, text: hasUpload ? body.substring(0, 200) : '' };
                })()
                """)

                if not isinstance(result, dict):
                    time.sleep(check_interval)
                    continue

                txt = result.get('text', '')

                # PERSENTASE DIPERIKSA LEBIH DULU, sebelum kata-kata "selesai".
                #
                # Kenapa: YouTube menampilkan "Uploading 45%... Processing will
                # begin shortly" SECARA BERSAMAAN. Versi lama menaruh
                # 'PROCESSING WILL BEGIN' di daftar tanda selesai dan
                # memeriksanya duluan, sehingga upload 45% dianggap sudah
                # kelar. Itulah sebabnya batch 8 video hanya 1 yang jadi —
                # sisanya ditinggal di 51-73%.
                import re
                angka = [int(m) for m in re.findall(r'(\d{1,3})\s*%', txt)]
                sedang_naik = ('UPLOADING' in txt or 'MENGUNGGAH' in txt
                               or 'MENGUPLOAD' in txt or 'MEMPROSES' in txt)

                if angka or sedang_naik:
                    tenang = 0  # masih ada tanda unggahan berjalan
                if angka:
                    # Diambil yang TERENDAH, bukan tertinggi. Setelah Schedule
                    # ditekan, YouTube kembali ke daftar konten yang bisa
                    # menampilkan beberapa baris sekaligus ("Uploading 65%",
                    # "51%", "73%"). Memakai yang tertinggi membuat GAMA
                    # berhenti menunggu padahal masih ada video yang 51%.
                    cur_pct = min(angka)
                    if cur_pct >= 100:
                        self.safe_log("[WAIT100] Semua indikator upload sudah 100%.")
                        return True
                    gama_pct = 90 + int(cur_pct * 8 / 100)
                    if gama_pct > last_pct_reported:
                        last_pct_reported = gama_pct
                        self._emit_progress(cb, gama_pct, f"Menunggu upload selesai... {cur_pct}%")
                    time.sleep(check_interval)
                    continue          # masih berjalan — JANGAN cek tanda selesai

                if sedang_naik:
                    # Ada kata "uploading" tapi persentasenya belum terbaca —
                    # tetap dianggap berjalan, jangan buru-buru lanjut.
                    time.sleep(check_interval)
                    continue

                # Tanda selesai hanya dipercaya kalau TIDAK ada indikator
                # unggahan yang masih berjalan.
                done_signals = [
                    'UPLOAD COMPLETE', 'SELESAI DIUPLOAD', 'UPLOAD SELESAI',
                    'PROCESSING COMPLETE', 'PEMROSESAN SELESAI',
                    'VIDEO PUBLISHED', 'VIDEO DIPUBLIKASIKAN',
                    'YOUR VIDEO IS LIVE',
                ]
                if any(sig in txt for sig in done_signals):
                    self.safe_log("[WAIT100] Upload selesai (tanda selesai terdeteksi).")
                    return True

                # TIDAK ada persentase & TIDAK ada kata "uploading" = transfer
                # berkas sudah kelar. Ini jalan keluar yang dulu tidak ada, dan
                # ketiadaannya bikin loop berputar sampai timeout: untuk video
                # pendek Studio cuma menulis "Processing will begin shortly"
                # (tanpa angka %), sementara 'PROCESSING' memang tidak masuk
                # daftar sedang_naik MAUPUN daftar tanda selesai — jadi tidak
                # pernah cocok ke cabang mana pun. Terbukti live: video 13
                # detik menggantung 25 menit di sini.
                #
                # Menunggu PEMROSESAN selesai sengaja TIDAK dilakukan: YouTube
                # memproses video setelah unggahan tuntas, dan visibility/
                # jadwal boleh diatur selagi pemrosesan berjalan. Yang wajib
                # selesai hanyalah transfer berkasnya.
                tenang += 1
                if tenang >= 3:
                    self.safe_log("[WAIT100] Tidak ada lagi indikator unggahan "
                                  "berjalan (3x cek berturut-turut) — transfer "
                                  "berkas dianggap selesai. Pemrosesan YouTube "
                                  "boleh lanjut di latar.")
                    return True
                time.sleep(check_interval)
                continue

                # Tidak ada indikator upload → kemungkinan sudah redirect/selesai
                if not result.get('found'):
                    url = (page.url or '').lower()
                    if any(p in url for p in ['/content', '/videos', '/dashboard']):
                        self.safe_log("[WAIT100] Redirect ke content/dashboard → upload selesai.")
                        return True

            except Exception as e:
                pesan = str(e).lower()
                # Kalau tab/browser-nya sudah mati, mengulang 30 menit tidak
                # ada gunanya — lebih baik berhenti supaya baris berikutnya
                # tidak ikut tertahan.
                if ('disconnect' in pesan or 'closed' in pesan
                        or 'not found' in pesan or 'target' in pesan):
                    self.safe_log(f"[WAIT100] Koneksi ke halaman terputus: {e}")
                    return False
                self.safe_log(f"[WAIT100] Error saat cek progress: {e}")

            time.sleep(check_interval)

        self.safe_log(f"[WAIT100] Timeout {timeout}s — lanjut meski belum konfirmasi 100%.")
        return False

    def _js_set_text(self, page, selector, text):
        """Set text di contenteditable YouTube Studio (menembus shadow DOM).

        Satu script JS yang langsung find + focus + clear + insert teks.
        Tidak coba return DOM element (DrissionPage run_js hanya bisa return primitive).
        Gunakan CDP Input.insertText sebagai metode utama, fallback ke execCommand.
        """
        is_title = "title" in selector
        is_desc  = "description" in selector
        ancestor = "ytcp-video-title" if is_title else ("ytcp-description" if is_desc else "")
        idx      = 0 if is_title else 1

        # Escape untuk JS template literal
        safe_js = (str(text)
                   .replace('\\', '\\\\')
                   .replace('`', '\\`')
                   .replace('$', '\\$')
                   .replace('\r\n', '\\n')
                   .replace('\n', '\\n'))

        # Step 1: Focus elemen via JS (find + focus dalam 1 script)
        page.run_js(f"""
        return (function(){{
            function walkAll(root, cb) {{
                if (!root) return;
                var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (var i=0; i<all.length; i++) {{
                    cb(all[i]);
                    if (all[i].shadowRoot) walkAll(all[i].shadowRoot, cb);
                }}
            }}
            function findTextbox() {{
                // Metode 0: untuk selector non-title/non-desc, coba querySelector langsung dulu
                var directSel = "{selector}";
                if (directSel && !"{ancestor}") {{
                    var direct = document.querySelector(directSel);
                    if (direct) return direct;
                    // Coba via shadow DOM walk dengan selector
                    var found = null;
                    walkAll(document, function(el) {{
                        if (found) return;
                        try {{
                            var inner = el.shadowRoot && el.shadowRoot.querySelector(directSel);
                            if (inner) found = inner;
                        }} catch(e) {{}}
                    }});
                    if (found) return found;
                }}
                // Metode 1: ancestor tag (untuk title/description)
                var ancestorTag = "{ancestor}";
                if (ancestorTag) {{
                    var found = null;
                    walkAll(document, function(el) {{
                        if (found) return;
                        if ((el.tagName||'').toLowerCase() === ancestorTag) {{
                            var sr = el.shadowRoot;
                            if (sr) {{
                                var tb = sr.querySelector('#textbox') ||
                                         sr.querySelector('[contenteditable="true"]');
                                if (tb) {{ found = tb; return; }}
                            }}
                            var tb2 = el.querySelector('#textbox, [contenteditable="true"]');
                            if (tb2) found = tb2;
                        }}
                    }});
                    if (found) return found;
                }}
                // Metode 2: semua #textbox contenteditable
                var boxes = [];
                walkAll(document, function(el) {{
                    if ((el.id||'').toLowerCase() === 'textbox' && el.isContentEditable)
                        boxes.push(el);
                }});
                return boxes[{idx}] || boxes[0] || null;
            }}
            var el = findTextbox();
            if (!el) return false;
            el.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            return true;
        }})()
        """)
        time.sleep(0.15)

        # Step 2: Insert teks via CDP Input.insertText (paling reliable)
        inserted = False
        try:
            page.run_cdp('Input.insertText', text=str(text))
            inserted = True
            self.safe_log(f"[SET_TEXT] CDP Input.insertText OK — {'title' if is_title else ('desc' if is_desc else selector)}")
        except Exception as e:
            self.safe_log(f"[SET_TEXT] CDP insertText gagal ({e}), fallback execCommand")

        if not inserted:
            # Fallback: execCommand insertText
            page.run_js(f"""
            return (function(){{
                // Untuk selector non-title/non-desc, coba querySelector langsung dulu
                var directSel = "{selector}";
                var el = null;
                if (directSel && !"{ancestor}") {{
                    el = document.querySelector(directSel);
                    if (!el) {{
                        function walkDirect(root) {{
                            var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                            for (var i=0; i<all.length; i++) {{
                                if (all[i].shadowRoot) {{
                                    try {{
                                        var inn = all[i].shadowRoot.querySelector(directSel);
                                        if (inn) return inn;
                                    }} catch(e) {{}}
                                    var res = walkDirect(all[i].shadowRoot);
                                    if (res) return res;
                                }}
                            }}
                            return null;
                        }}
                        el = walkDirect(document);
                    }}
                }}
                if (!el && "{ancestor}") {{
                    function walkAll(root, cb) {{
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++) {{
                            cb(all[i]);
                            if (all[i].shadowRoot) walkAll(all[i].shadowRoot, cb);
                        }}
                    }}
                    walkAll(document, function(e) {{
                        if (el) return;
                        if ((e.tagName||'').toLowerCase() === "{ancestor}") {{
                            var sr = e.shadowRoot;
                            if (sr) {{
                                var tb = sr.querySelector('#textbox') || sr.querySelector('[contenteditable="true"]');
                                if (tb) {{ el = tb; return; }}
                            }}
                            var tb2 = e.querySelector('#textbox, [contenteditable="true"]');
                            if (tb2) el = tb2;
                        }}
                    }});
                }}
                if (!el) return false;
                el.focus();
                document.execCommand('insertText', false, `{safe_js}`);
                return true;
            }})()
            """)

        # Step 3: Dispatch reactive events
        page.run_js(f"""
        return (function(){{
            function walkAll(root, cb) {{
                if (!root) return;
                var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (var i=0; i<all.length; i++) {{
                    cb(all[i]);
                    if (all[i].shadowRoot) walkAll(all[i].shadowRoot, cb);
                }}
            }}
            var ancestorTag = "{ancestor}";
            var found = null;
            if (ancestorTag) {{
                walkAll(document, function(el) {{
                    if (found) return;
                    if ((el.tagName||'').toLowerCase() === ancestorTag) {{
                        var sr = el.shadowRoot;
                        if (sr) {{
                            var tb = sr.querySelector('#textbox') ||
                                     sr.querySelector('[contenteditable="true"]');
                            if (tb) {{ found = tb; return; }}
                        }}
                        var tb2 = el.querySelector('#textbox, [contenteditable="true"]');
                        if (tb2) found = tb2;
                    }}
                }});
            }}
            if (!found) {{
                var boxes = [];
                walkAll(document, function(el) {{
                    if ((el.id||'').toLowerCase() === 'textbox' && el.isContentEditable)
                        boxes.push(el);
                }});
                found = boxes[{idx}] || boxes[0] || null;
            }}
            if (!found) return false;
            found.dispatchEvent(new Event('input',  {{bubbles:true}}));
            found.dispatchEvent(new Event('change', {{bubbles:true}}));
            found.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText'}}));
            return true;
        }})()
        """)
        time.sleep(0.4)

    def _js_get_text(self, page, is_title=True):
        """Baca isi textbox (title atau description) via shadow DOM walk."""
        ancestor = "ytcp-video-title" if is_title else "ytcp-description"
        idx      = 0 if is_title else 1
        return page.run_js(f"""
        return (function(){{
            function walkAll(root, cb) {{
                if (!root) return;
                var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (var i=0; i<all.length; i++) {{
                    cb(all[i]);
                    if (all[i].shadowRoot) walkAll(all[i].shadowRoot, cb);
                }}
            }}
            var found = null;
            walkAll(document, function(el) {{
                if (found) return;
                if ((el.tagName||'').toLowerCase() === "{ancestor}") {{
                    var sr = el.shadowRoot;
                    if (sr) {{
                        var tb = sr.querySelector('#textbox') ||
                                 sr.querySelector('[contenteditable="true"]');
                        if (tb) {{ found = tb; return; }}
                    }}
                    var tb2 = el.querySelector('#textbox, [contenteditable="true"]');
                    if (tb2) found = tb2;
                }}
            }});
            if (!found) {{
                var boxes = [];
                walkAll(document, function(el) {{
                    if ((el.id||'').toLowerCase() === 'textbox' && el.isContentEditable)
                        boxes.push(el);
                }});
                found = boxes[{idx}] || null;
            }}
            return found ? (found.innerText || found.textContent || '').trim() : '';
        }})()
        """)


    def _js_click(self, page, target_text, timeout=10):
        """Klik tombol by teks."""
        t = target_text.upper()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raise_if_stopped()
            result = page.run_js(
                "(function(t){"
                "var all=document.querySelectorAll("
                "'ytcp-button,tp-yt-paper-button,button,[role=\"button\"]');"
                "for(var b of all){"
                "var txt=(b.innerText||b.textContent||'').trim().toUpperCase();"
                "if(txt===t||txt.startsWith(t)){"
                "var dis=b.hasAttribute('disabled')||"
                "b.getAttribute('aria-disabled')==='true'||"
                "b.classList.contains('ytcp-button--disabled');"
                "if(!dis){b.click();return true;}"
                "}"
                "}"
                "return false;"
                "})('" + t + "')"
            )
            if result:
                return True
            time.sleep(0.8)
        return False

    def _dismiss_verify_dialog(self, page):
        """Dismiss common YouTube popups (Verification, Tips, etc.) using DOM state."""
        try:
            page.run_js("""
            return (function(){
                var btns = document.querySelectorAll('button,[role="button"], ytcp-button, tp-yt-paper-button');
                var targets = ['NEXT', 'SKIP', 'DISMISS', 'GOT IT', 'OK', 'CLOSE'];
                for(var b of btns){
                    var t = (b.innerText||b.textContent||b.getAttribute('aria-label')||'').trim().toUpperCase();
                    for(var target of targets){
                        if(t === target || t.startsWith(target)){
                            // Headless check: skip disabled buttons
                            if(!b.hasAttribute('disabled') && b.getAttribute('aria-disabled') !== 'true'){
                                b.click(); return t;
                            }
                        }
                    }
                }
            })()
            """)
        except Exception:
            pass

    def _upload_dialog_started(self, page):
        """Kembalikan True jika YouTube Studio telah menerima file video dan
        menampilkan form Details (bukan hanya kartu picker awal).

        FIX: Versi lama memakai getBoundingClientRect() width>0 && height>0
        sebagai syarat — dialog YouTube render di background sehingga rect=0
        meski elemen sudah ada di DOM. Sekarang cukup cek keberadaan elemen
        (document.querySelector) yang sudah terbukti bekerja dari debug output.
        """
        try:
            # KRITIS: harus ada "return" di depan IIFE — DrissionPage page.run_js()
            # hanya mengembalikan nilai jika ada return statement di level terluar.
            # Tanpa "return", selalu None → bool(None) = False.
            return bool(page.run_js("""
            return (function(){
                // 1. Cek ytcp-uploads-dialog dengan video-id (paling akurat)
                var dlg = document.querySelector('ytcp-uploads-dialog[video-id]') ||
                          document.querySelector('ytcp-video-upload-dialog[video-id]') ||
                          document.querySelector('[video-id]');
                if (dlg && dlg.getAttribute('video-id')) return true;

                // 2. Cek keberadaan elemen metadata (TANPA syarat rect > 0)
                var metaTags = [
                    'ytcp-uploads-dialog',
                    'ytcp-video-metadata-editor',
                    'ytcp-video-metadata-editor-basics',
                    'ytcp-video-title',
                    'ytcp-thumbnail-uploader'
                ];
                for (var i = 0; i < metaTags.length; i++) {
                    if (document.querySelector(metaTags[i])) return true;
                }

                // 3. Cek teks indikator upload/processing di body
                var text = (document.body.innerText || '').toUpperCase();
                if (text.includes('UPLOADING') || text.includes('PROCESSING') ||
                    text.includes('CHECKING') || text.includes('MENGUPLOAD') ||
                    text.includes('MEMPROSES') || text.includes('DRAFT SAVED') ||
                    text.includes('SAVED AS DRAFT') || text.includes('SAVED AS PRIVATE') ||
                    (text.includes('DETAILS') && (text.includes('VISIBILITY') || text.includes('ELEMENTS')))) {
                    return true;
                }

                return false;
            })()
            """))
        except Exception:
            return False



    def _find_upload_file_input(self, page, timeout=25):
        deadline = time.time() + timeout
        selectors = [
            'css:input[type="file"]',
            '@type=file',
            'xpath://input[@type="file"]',
        ]
        while time.time() < deadline:
            self._raise_if_stopped()
            for selector in selectors:
                try:
                    ele = page.ele(selector, timeout=2)
                    if ele:
                        accept = (ele.attr('accept') or '').lower()
                        ele_type = (ele.attr('type') or '').lower()
                        if ele_type == 'file' or 'video' in accept or '*' in accept or not accept:
                            self.safe_log(f"[UPLOAD] Valid file input ditemukan: selector='{selector}' accept='{accept}'")
                            return ele
                except Exception:
                    pass
            time.sleep(1)
        return None


    def _cdp_set_upload_file(self, page, video_path):
        expr = r"""
        (function(){
            function find(root){
                if(!root) return null;
                const direct = root.querySelector && root.querySelector('input[type="file"]');
                if(direct) return direct;
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for(const el of all){
                    if((el.tagName || '').toLowerCase() === 'input' && el.type === 'file') return el;
                    if(el.shadowRoot){
                        const found = find(el.shadowRoot);
                        if(found) return found;
                    }
                }
                return null;
            }
            return find(document);
        })()
        """
        res = page.run_cdp('Runtime.evaluate', expression=expr, objectGroup='upload-file-input')
        obj_id = (res.get('result') or {}).get('objectId')
        if not obj_id:
            return False
        node = page.run_cdp('DOM.requestNode', objectId=obj_id)
        node_id = node.get('nodeId')
        if not node_id:
            return False
        page.run_cdp('DOM.setFileInputFiles', files=[video_path], nodeId=node_id)
        return True

    def _find_thumbnail_host(self, page, timeout=8):
        """Cari editor thumbnail via `page.ele()` — method NATIVE DrissionPage
        (jalan lewat CDP DOM.*, otomatis tembus shadow DOM Polymer/LitElement
        Studio). TERVERIFIKASI LIVE: pendekatan lama pakai `page.run_js` +
        `document.querySelector` manual (rekursif tembus shadowRoot pun)
        SELALU gagal menemukan host ini walau elemennya jelas ada & terlihat
        di layar (screenshot) — sementara `page.ele('tag:ytcp-thumbnail-uploader')`
        langsung ketemu instan. Nama tag-nya SUDAH BENAR sejak awal; akar
        masalahnya di teknik pencariannya (JS manual), bukan di selector-nya."""
        for sel in ('tag:ytcp-thumbnail-uploader', 'tag:ytcp-video-thumbnail-editor',
                    'tag:ytcp-video-custom-still-editor'):
            try:
                host = page.ele(sel, timeout=timeout)
            except Exception:
                host = None
            if host:
                return host
            timeout = 1.0  # hanya coba lama di selector pertama
        return None

    # Domain gambar yang DIBUAT SENDIRI oleh YouTube (auto-still hasil proses
    # video, avatar channel, dll). Thumbnail milik user tidak pernah dari sini,
    # jadi kemunculannya BUKAN bukti thumbnail custom sudah terpasang.
    _YT_IMAGE_HOSTS = ('ytimg.com', 'ggpht.com', 'googleusercontent.com',
                       'gstatic.com', 'youtube.com/s/')

    def _is_youtube_still(self, src):
        s = (src or '').lower()
        if not s:
            return True
        if s.startswith('blob:') or s.startswith('data:'):
            return False          # ini justru kandidat thumbnail user
        return any(h in s for h in self._YT_IMAGE_HOSTS)

    def _thumbnail_state(self, page):
        """Snapshot editor thumbnail dalam SATU panggilan JS (~puluhan ms).

        Dipakai oleh semua polling: tunggu-siap, verifikasi preview, dan
        deteksi penolakan file.

        KENAPA: sebelumnya setiap putaran polling memanggil `page.ele()`
        berlapis (3 selector, masing-masing dengan timeout) + `host.eles('tag:img')`.
        Sekali "mengintip" bisa makan 6-9 detik walau tidak ada yang berubah,
        dan itu dilakukan puluhan kali — inilah yang bikin langkah thumbnail
        terasa menggantung bermenit-menit di UI.

        Return: {found, imgs, custom, rejected, text}
        """
        empty = {'found': False, 'imgs': 0, 'custom': False, 'rejected': False,
                 'text': '', 'srcs': [], 'fileset': False, 'filename': ''}
        try:
            res = page.run_js("""
            return (function(){
                var hosts = [];
                function collect(root, depth){
                    if (!root || depth > 10) return;
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i=0; i<all.length; i++){
                        var el = all[i];
                        var tag = (el.tagName||'').toLowerCase();
                        if (tag === 'ytcp-thumbnail-uploader' ||
                            tag === 'ytcp-video-thumbnail-editor' ||
                            tag === 'ytcp-video-custom-still-editor') hosts.push(el);
                        if (el.shadowRoot) collect(el.shadowRoot, depth+1);
                    }
                }
                collect(document, 0);
                var out = {found: hosts.length > 0, imgs: 0, custom: false, text: '',
                           srcs: [], fileset: false, filename: '', applied: false};
                function terlihat(el){
                    var r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    var s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                function addSrc(src){
                    if (!src) return;
                    out.imgs++;
                    if (out.srcs.indexOf(src) < 0) out.srcs.push(src);
                    if (src.indexOf('blob:') === 0 || src.indexOf('data:') === 0) out.custom = true;
                }
                for (var k=0; k<hosts.length; k++){
                    var h = hosts[k];
                    out.text += ' ' + ((h.innerText || h.textContent || '').toLowerCase());
                    var stack = [h];
                    while (stack.length){
                        var node = stack.shift();
                        if (!node) continue;
                        var imgs = node.querySelectorAll ? node.querySelectorAll('img') : [];
                        for (var i=0; i<imgs.length; i++){
                            addSrc(imgs[i].src || imgs[i].getAttribute('src') || '');
                        }
                        // Studio kadang merender still sebagai background-image
                        // pada <div>, bukan sebagai <img> — ikut dihitung.
                        var els = node.querySelectorAll ? node.querySelectorAll('*') : [];
                        for (var j=0; j<els.length; j++){
                            var el = els[j];
                            try {
                                var bg = window.getComputedStyle(el).backgroundImage || '';
                                var m = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
                                if (m) addSrc(m[1]);
                            } catch(e) {}
                            if (el.tagName === 'INPUT' && el.type === 'file' &&
                                el.files && el.files.length > 0) {
                                out.fileset = true;
                                out.filename = el.files[0].name || '';
                            }
                            // SINYAL SUKSES YANG SEBENARNYA (terbukti live):
                            // begitu Studio menerima thumbnail, tombol
                            // #preview-button / #options-button jadi terlihat
                            // dan file di input dikonsumsi (files kembali 0).
                            // URL blob: TIDAK selalu muncul, jadi jangan
                            // dijadikan satu-satunya patokan.
                            if ((el.id === 'preview-button' || el.id === 'options-button')
                                && terlihat(el)) {
                                out.applied = true;
                            }
                            if (el.shadowRoot) stack.push(el.shadowRoot);
                        }
                        if (node.shadowRoot) stack.push(node.shadowRoot);
                    }
                }
                return out;
            })()
            """)
        except Exception:
            return dict(empty)
        if not isinstance(res, dict):
            return dict(empty)
        text = str(res.get('text') or '')
        res['text'] = text
        res.setdefault('srcs', [])
        res['rejected'] = any(bad in text for bad in (
            'not an image', 'bukan gambar', 'too large', 'terlalu besar',
            'file is too big', 'exceeds the maximum'))
        # Thumbnail custom hanya tersedia untuk channel TERVERIFIKASI (nomor
        # telepon). Kalau channel belum verified, YouTube menampilkan ajakan
        # verifikasi di area ini dan file yang dikirim diabaikan diam-diam —
        # tidak ada metode otomatisasi apa pun yang bisa menembusnya.
        res['need_verify'] = any(v in text for v in (
            'verify', 'verification', 'verifikasi', 'verifying'))
        return res

    def _wait_processing_ready(self, page, timeout=45):
        """Tunggu form Details benar-benar siap diisi.

        Langsung setelah dialog upload muncul, section Thumbnail/Audience
        sudah ADA di DOM tapi belum fungsional: file thumbnail diterima tanpa
        error namun tidak benar-benar ter-apply. Form baru siap setelah backend
        YouTube mulai memproses video — ditandai munculnya auto-thumbnail.

        Timeout 45s (dulu 120s): kalau dalam 45 detik auto-thumbnail belum
        muncul, menunggu lebih lama praktis tidak menolong — lebih baik
        langsung coba pasang thumbnail-nya dan biarkan verifikasi yang
        memutuskan. Poll-nya juga murah sekarang (1 panggilan JS per 1,5 detik).
        """
        deadline = time.time() + timeout
        started = time.time()
        last_log = -10
        while True:
            st = self._thumbnail_state(page)
            if st.get('imgs'):
                self.safe_log(f"[READY] Auto-thumbnail terdeteksi ({st['imgs']} img) — "
                              f"form Details siap ({int(time.time() - started)}s).")
                return True
            elapsed = int(time.time() - started)
            if elapsed - last_log >= 10:
                last_log = elapsed
                self.safe_log(f"[READY] Menunggu form thumbnail siap... "
                              f"({elapsed}s/{timeout}s, editor "
                              f"{'ada' if st.get('found') else 'belum ada'})")
            if time.time() >= deadline:
                break
            time.sleep(1.5)
        self.safe_log(f"[READY] Timeout {timeout}s — tetap lanjut pasang thumbnail.")
        return False

    def _arm_upload_intercept(self, page, abs_path):
        """Pasang file-chooser intercept DrissionPage. False kalau gagal.

        Dipisah jadi helper supaya pemanggil bisa MEMBATALKAN klik ketika
        intercept tidak terpasang — tanpa intercept, klik tombol upload
        membuka dialog file Windows asli yang membekukan Chrome.
        """
        try:
            page.set.upload_files(abs_path)
            return True
        except Exception as e:
            self.safe_log(f"[THUMBNAIL] Gagal memasang intercept: {e}")
            return False

    def _scroll_to_thumbnail_host(self, page):
        """Scroll editor thumbnail ke tengah viewport supaya koordinat tombol
        'Upload file' valid (klik CDP memakai koordinat viewport — elemen di
        luar layar menghasilkan klik yang meleset / tidak kena apa-apa)."""
        try:
            page.run_js("""
            return (function(){
                function walkAll(root) {
                    var els = root.querySelectorAll('ytcp-thumbnail-uploader, ytcp-video-thumbnail-editor, ytcp-video-custom-still-editor');
                    if (els.length > 0) { els[0].scrollIntoView({behavior:'instant', block:'center'}); return true; }
                    var all = root.querySelectorAll('*');
                    for (var el of all) { if (el.shadowRoot && walkAll(el.shadowRoot)) return true; }
                    return false;
                }
                return walkAll(document);
            })()
            """)
            time.sleep(0.8)
        except Exception:
            pass

    def _fire_file_input_events(self, page):
        """Dispatch input+change pada file input GAMBAR supaya Polymer Studio
        memproses file yang baru di-set lewat CDP.

        PENTING — jangan menyaring berdasarkan id: TERBUKTI LIVE bahwa input
        thumbnail di Studio juga ber-id `file-loader`
        (accept=image/jpeg,image/png), sama seperti input video. Menyaring
        `id !== 'file-loader'` membuat event tidak pernah terkirim ke input
        yang benar, jadi file sudah masuk tapi preview tidak pernah berubah.
        Penyaringan yang benar HANYA lewat `accept`.
        """
        try:
            return int(page.run_js("""
            return (function(){
                var n = 0;
                function isImageInput(el){
                    var accept = (el.accept || el.getAttribute('accept') || '').toLowerCase();
                    if (!accept || accept.indexOf('video') >= 0) return false;
                    return accept.indexOf('image') >= 0 || accept.indexOf('png') >= 0 ||
                           accept.indexOf('jpg') >= 0 || accept.indexOf('jpeg') >= 0 ||
                           accept.indexOf('webp') >= 0;
                }
                function walkAll(root, depth) {
                    if (!root || depth > 10) return;
                    var all = root.querySelectorAll ? root.querySelectorAll('input[type="file"]') : [];
                    for (var i=0; i<all.length; i++) {
                        var el = all[i];
                        if (!isImageInput(el)) continue;
                        if (!el.files || el.files.length === 0) continue;  // belum ada file
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        n++;
                    }
                    var els = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var j=0; j<els.length; j++) {
                        if (els[j].shadowRoot) walkAll(els[j].shadowRoot, depth+1);
                    }
                }
                walkAll(document, 0);
                return n;
            })()
            """) or 0)
        except Exception:
            return 0

    def _thumbnail_rejected(self, page):
        """True bila YouTube menampilkan pesan penolakan file thumbnail
        (format salah / ukuran > 2MB). Percuma retry kalau ini muncul."""
        st = self._thumbnail_state(page)
        if st.get('rejected'):
            self.safe_log(f"[THUMBNAIL] YouTube menolak file: {st.get('text', '')[:120].strip()}")
            return True
        return False

    def _thumbnail_coord_click(self, page, abs_path):
        """Klik tombol 'Upload file' di editor thumbnail via koordinat CDP,
        dengan file-chooser intercept terpasang lebih dulu.

        Pencarian tombol DIBATASI ke dalam host thumbnail — versi lama memindai
        seluruh dokumen dengan `txt.startsWith('UPLOAD')` sehingga bisa nyasar
        ke tombol 'Upload videos'/'Upload' lain di Studio dan klik-nya jatuh di
        tempat yang salah."""
        self._scroll_to_thumbnail_host(page)
        btn_coord = page.run_js("""
        return (function(){
            function collectHosts(root, out, depth) {
                if (!root || depth > 10) return;
                var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (var i=0; i<all.length; i++) {
                    var el = all[i];
                    var tag = (el.tagName||'').toLowerCase();
                    if (tag === 'ytcp-thumbnail-uploader' ||
                        tag === 'ytcp-video-thumbnail-editor' ||
                        tag === 'ytcp-video-custom-still-editor') out.push(el);
                    if (el.shadowRoot) collectHosts(el.shadowRoot, out, depth+1);
                }
            }
            function findBtn(root, depth) {
                if (!root || depth > 8) return null;
                var sel = 'button, ytcp-button, tp-yt-paper-button, [role="button"]';
                var all = root.querySelectorAll ? root.querySelectorAll(sel) : [];
                for (var i=0; i<all.length; i++) {
                    var el = all[i];
                    var txt = ((el.innerText||el.textContent||el.getAttribute('aria-label')||'')
                               .trim().toUpperCase());
                    if (txt.indexOf('UPLOAD FILE') < 0 && txt.indexOf('UNGGAH FILE') < 0 &&
                        txt.indexOf('UPLOAD THUMBNAIL') < 0 && txt.indexOf('UNGGAH THUMBNAIL') < 0) continue;
                    try { el.scrollIntoView({behavior:'instant', block:'center'}); } catch(e) {}
                    var r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 &&
                        r.top >= 0 && r.bottom <= (window.innerHeight || 0) + 2) {
                        return {x: r.left + r.width/2, y: r.top + r.height/2,
                                w: r.width, h: r.height, txt: txt};
                    }
                }
                var els = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (var j=0; j<els.length; j++) {
                    if (els[j].shadowRoot) {
                        var res = findBtn(els[j].shadowRoot, depth+1);
                        if (res) return res;
                    }
                }
                return null;
            }
            var hosts = [];
            collectHosts(document, hosts, 0);
            for (var h of hosts) {
                var res = findBtn(h, 0) || (h.shadowRoot ? findBtn(h.shadowRoot, 0) : null);
                if (res) return res;
            }
            return null;
        })()
        """)

        if not (btn_coord and isinstance(btn_coord, dict)):
            self.safe_log("[THUMBNAIL] Tombol 'Upload file' tidak ditemukan di editor thumbnail.")
            return False

        bx = btn_coord.get('x', 0)
        by = btn_coord.get('y', 0)
        self.safe_log(f"[THUMBNAIL] Tombol Upload file: ({int(bx)},{int(by)}) txt={btn_coord.get('txt')}")
        # Intercept HARUS terpasang sebelum klik. Kalau GAGAL terpasang, JANGAN
        # diklik: klik-nya akan membuka dialog file Windows asli yang MEMBEKUKAN
        # Chrome (dan seluruh worker) sampai timeout — ini salah satu sumber
        # "stuck" yang paling parah karena tidak ada yang bisa menutupnya.
        if not self._arm_upload_intercept(page, abs_path):
            self.safe_log("[THUMBNAIL] Intercept file chooser gagal dipasang — klik dibatalkan.")
            return False
        time.sleep(0.3)
        self._stealth_cdp_click(page, bx, by)
        self.safe_log("[THUMBNAIL] CDP klik tombol Upload file OK")
        time.sleep(3)
        return True

    def _apply_thumbnail_on_current_page(self, page, thumbnail_path,
                                         wait_ready=True, budget=75):
        """Upload thumbnail ke halaman Studio.

        Metode dicoba berurutan dari yang paling andal ke yang paling rapuh,
        dan SETIAP metode diverifikasi sebelum dianggap sukses:
          1. CDP DOM.setFileInputFiles ke input[type=file] accept=image
             (menembus shadow DOM, tanpa dialog OS, jalan di headless).
          2. DrissionPage: cari input file di dalam host thumbnail.
          3. Klik koordinat tombol 'Upload file' + file-chooser intercept.
          4. DrissionPage file chooser (klik elemen tombol).

        Versi lama HANYA memakai metode 3 lalu 4 — dua metode CDP (yang paling
        andal) sudah ditulis tapi tidak pernah dipanggil sama sekali, dan bila
        verifikasi gagal hasilnya tetap dianggap 'sukses' asal tidak ada teks
        error, sehingga thumbnail diam-diam tidak terpasang.

        `budget` = batas waktu KERAS (detik) untuk seluruh langkah ini.
        Thumbnail sifatnya opsional — video tetap bisa dipublish/dijadwalkan
        tanpa custom thumbnail — jadi lebih baik menyerah tepat waktu daripada
        menahan upload berlama-lama.
        """
        abs_path = os.path.abspath(thumbnail_path)
        if not os.path.isfile(abs_path):
            self.safe_log(f"[THUMBNAIL] File tidak ditemukan: {abs_path}")
            return False

        started = time.time()
        deadline = started + max(20, int(budget))
        self.safe_log(f"[THUMBNAIL] Path: {abs_path} (batas waktu {int(budget)}s)")

        if wait_ready:
            self.safe_log("[THUMBNAIL] Menunggu form siap sebelum upload...")
            # Sisakan waktu untuk mencoba metode-metodenya, jangan habiskan
            # seluruh budget cuma untuk menunggu.
            self._wait_processing_ready(page, timeout=min(45, max(10, int(budget * 0.5))))
        self._scroll_to_thumbnail_host(page)

        methods = (
            ('Metode 1 (CDP setFileInputFiles)', self._cdp_set_thumbnail_direct),
            ('Metode 2 (DrissionPage host input)', self._drission_host_input),
            ('Metode 3 (koordinat + file chooser)', self._thumbnail_coord_click),
            ('Metode 4 (DrissionPage file chooser)', self._file_chooser_thumbnail),
        )

        for idx, (label, method) in enumerate(methods):
            sisa = deadline - time.time()
            if sisa <= 3:
                self.safe_log(f"[THUMBNAIL] Batas waktu {int(budget)}s habis — "
                              f"berhenti sebelum {label}.")
                break
            # Foto keadaan editor SEBELUM kirim file, supaya verifikasi bisa
            # mendeteksi "ada gambar baru" alih-alih menebak pola URL.
            before_srcs = list(self._thumbnail_state(page).get('srcs') or [])
            try:
                sent = method(page, abs_path)
            except Exception as e:
                self.safe_log(f"[THUMBNAIL] {label} exception: {e}")
                sent = False
            finally:
                self._clear_upload_files_intercept(page)

            if self._thumbnail_rejected(page):
                # File-nya yang bermasalah (format/ukuran) — metode lain sia-sia.
                return False
            if not sent:
                self.safe_log(f"[THUMBNAIL] {label}: file tidak terkirim, coba metode berikutnya.")
                continue

            # Metode pertama dapat jatah verifikasi paling panjang; metode
            # berikutnya lebih pendek supaya total tetap di dalam budget.
            verify_timeout = int(min(12 if idx == 0 else 8, max(3, deadline - time.time())))
            self.safe_log(f"[THUMBNAIL] {label}: file terkirim, verifikasi preview "
                          f"({verify_timeout}s)...")
            if self._verify_thumbnail_applied(page, timeout=verify_timeout,
                                              before_srcs=before_srcs,
                                              expect_name=os.path.basename(abs_path)):
                self.safe_log(f"[THUMBNAIL] ✓ Preview terverifikasi via {label} "
                              f"({int(time.time() - started)}s)")
                return True
            self.safe_log(f"[THUMBNAIL] {label}: preview belum berubah, coba metode berikutnya.")

        self.safe_log(f"[THUMBNAIL] ✗ Gagal dalam {int(time.time() - started)}s — "
                      f"thumbnail tidak terpasang.")
        return False

    def _drission_host_input(self, page, abs_path):
        """Cari input[type=file] di dalam ytcp-thumbnail-uploader via
        DrissionPage, lalu set file via CDP node_id (BUKAN inp.input()).

        KENAPA BUKAN inp.input():
        inp.input() pada file input di dalam shadow DOM bisa memicu native OS
        file dialog yang MEMBLOKIR browser automation (penyebab 'macet').
        Solusi: ambil node_id dari DrissionPage element, lalu panggil CDP
        DOM.setFileInputFiles langsung — bypass shadow DOM & bypass OS dialog.
        """
        try:
            host = self._find_thumbnail_host(page, timeout=4)
            if not host:
                self.safe_log("[THUMBNAIL] host input: thumbnail host tidak ditemukan")
                return False

            for sel in ('@type=file', 'css:input[type="file"]'):
                inp = None
                try:
                    inp = host.ele(sel, timeout=2)
                except Exception as e:
                    self.safe_log(f"[THUMBNAIL] host input sel '{sel}': {e}")
                if not inp:
                    continue

                accept = (inp.attr('accept') or '').lower()
                inp_id = (inp.attr('id') or '')
                self.safe_log(f"[THUMBNAIL] host input: id='{inp_id}' accept='{accept}'")

                # Ambil CDP node_id dari DrissionPage element
                # DrissionPage 4.x: _get_node_id() dan _get_backend_id() adalah METODE
                nid = None
                for method_name in ('_get_node_id', '_get_backend_id'):
                    try:
                        method = getattr(inp, method_name, None)
                        if callable(method):
                            v = method()
                            if v:
                                nid = int(v)
                                self.safe_log(f"[THUMBNAIL] host input: node_id via {method_name}()={nid}")
                                break
                    except Exception:
                        pass
                if not nid:
                    for attr in ('node_id', '_node_id', 'backend_id', '_backend_id'):
                        try:
                            v = getattr(inp, attr, None)
                            if v and not callable(v):
                                nid = int(v)
                                break
                        except Exception:
                            pass

                if nid:
                    self.safe_log(f"[THUMBNAIL] host input: CDP setFileInputFiles nodeId={nid}")
                    page.run_cdp('DOM.setFileInputFiles', files=[abs_path], nodeId=nid)
                    self._fire_file_input_events(page)
                    time.sleep(2)
                    return True

                # Fallback: chooser intercept + click element
                self.safe_log("[THUMBNAIL] host input: node_id N/A, coba chooser intercept...")
                if not self._arm_upload_intercept(page, abs_path):
                    return False
                time.sleep(0.3)
                try:
                    inp.click()
                except Exception:
                    pass
                time.sleep(2)
                self._clear_upload_files_intercept(page)
                return True

            self.safe_log("[THUMBNAIL] host input: input[type=file] tidak ditemukan di host")
        except Exception as e:
            self.safe_log(f"[THUMBNAIL] host input exception: {e}")
        return False


    def _file_chooser_thumbnail(self, page, abs_path):
        """Metode 3: File chooser intercept + klik tombol 'Upload file'.
        Penting: gunakan btn.click() bukan _coord_click (method yang tidak ada)."""
        try:
            host = self._find_thumbnail_host(page, timeout=4)
            if not host:
                self.safe_log("[THUMBNAIL] Metode 3: thumbnail host tidak ditemukan")
                return False

            btn = None
            # Cari tombol Upload file di dalam host (shadow DOM-aware via DrissionPage)
            for sel in ('@aria-label=Upload file', '@aria-label=Unggah file',
                        '@aria-label^Upload', '@aria-label^Unggah',
                        'text:Upload file', 'text:Unggah file',
                        'text:Upload thumbnail', 'text:Upload a thumbnail'):
                try:
                    candidate = host.ele(sel, timeout=2)
                    if candidate:
                        # Pastikan ini button, bukan span
                        tag = (candidate.tag or '').lower()
                        if tag != 'button':
                            try:
                                candidate = candidate.parent('tag:button')
                            except Exception:
                                pass  # pakai candidate apa adanya
                        btn = candidate
                        self.safe_log(f"[THUMBNAIL] Metode 3: tombol ditemukan sel='{sel}'")
                        break
                except Exception:
                    pass

            # Fallback: tombol pertama apapun di dalam host
            if not btn:
                try:
                    btn = host.ele('tag:button', timeout=2)
                    if btn:
                        self.safe_log("[THUMBNAIL] Metode 3: fallback ke tombol pertama di host")
                except Exception:
                    pass

            if not btn:
                self.safe_log("[THUMBNAIL] Metode 3: tidak ada tombol ditemukan di host")
                return False

            # Urutan BENAR: set intercept dulu, LALU klik tombol.
            # YouTube Studio membuka file dialog saat tombol diklik, jadi
            # intercept harus sudah terpasang. Kalau gagal terpasang, batalkan
            # klik — dialog file Windows asli akan membekukan Chrome.
            if not self._arm_upload_intercept(page, abs_path):
                self.safe_log("[THUMBNAIL] Intercept gagal dipasang — klik dibatalkan.")
                return False
            time.sleep(0.3)
            try:
                btn.click()  # DrissionPage element .click() — BUKAN _coord_click
            except Exception as ce:
                self.safe_log(f"[THUMBNAIL] btn.click() error: {ce}, coba JS click...")
                try:
                    page.run_js("arguments[0].click()", btn)
                except Exception:
                    pass
            self.safe_log("[THUMBNAIL] Tombol Upload diklik (file chooser)")
            time.sleep(3)
            self._clear_upload_files_intercept(page)
            return True
        except Exception as e:
            self.safe_log(f"[THUMBNAIL] Metode 3 exception: {e}")
            self._clear_upload_files_intercept(page)
        return False

    def _cdp_set_thumbnail_direct(self, page, abs_path):
        """Set file thumbnail via CDP DOM.performSearch + setFileInputFiles.

        DOM.querySelectorAll tidak bisa tembus shadow DOM (YouTube Studio pakai
        Polymer shadow DOM); DOM.performSearch dengan includeUserAgentShadowDOM
        bisa. Node dipilih berdasar atribut `accept`: input GAMBAR dulu, lalu
        input netral. Input video utama (accept video / id=file-loader) selalu
        dilewati — versi lama hanya melewatinya bila id=file-loader DAN accept
        bukan image, sehingga file thumbnail bisa masuk ke input video.
        """
        search_id = ''
        try:
            search_res = page.run_cdp(
                'DOM.performSearch',
                query='input[type="file"]',
                includeUserAgentShadowDOM=True
            )
            search_id = search_res.get('searchId', '')
            count     = search_res.get('resultCount', 0)
            self.safe_log(f"[THUMBNAIL] CDP performSearch: {count} input[type=file] (pierce shadow DOM)")
            if not count:
                return False

            node_res = page.run_cdp('DOM.getSearchResults',
                                    searchId=search_id, fromIndex=0, toIndex=count)
            node_ids = node_res.get('nodeIds', [])

            image_nodes, neutral_nodes = [], []
            for node_id in node_ids:
                try:
                    attrs_r = page.run_cdp('DOM.getAttributes', nodeId=node_id)
                    attrs   = attrs_r.get('attributes', [])
                    attr_d  = {attrs[i]: attrs[i + 1] for i in range(0, len(attrs) - 1, 2)}
                except Exception:
                    continue
                accept = (attr_d.get('accept', '') or '').lower()
                inp_id = attr_d.get('id', '') or ''
                self.safe_log(f"[THUMBNAIL] CDP node {node_id}: id={inp_id} accept={accept}")
                if any(k in accept for k in ('image', 'png', 'jpg', 'jpeg', 'webp')):
                    image_nodes.append(node_id)
                elif inp_id != 'file-loader' and 'video' not in accept:
                    neutral_nodes.append(node_id)

            for node_id in image_nodes + neutral_nodes:
                try:
                    page.run_cdp('DOM.setFileInputFiles',
                                 files=[abs_path], nodeId=node_id)
                    self.safe_log(f"[THUMBNAIL] CDP setFileInputFiles OK node={node_id}")
                    n_ev = self._fire_file_input_events(page)
                    self.safe_log(f"[THUMBNAIL] Event change dikirim ke {n_ev} input gambar "
                                  f"(0 = file tidak benar-benar masuk ke input)")
                    time.sleep(2)
                    return True
                except Exception as e:
                    self.safe_log(f"[THUMBNAIL] CDP node {node_id} error: {e}")
                    continue
            self.safe_log("[THUMBNAIL] CDP: tidak ada input file gambar yang cocok.")
        except Exception as e:
            self.safe_log(f"[THUMBNAIL] CDP performSearch error: {e}")
        finally:
            if search_id:
                try:
                    page.run_cdp('DOM.discardSearchResults', searchId=search_id)
                except Exception:
                    pass
        return False

    def _drission_set_thumbnail(self, page, abs_path):
        """Fallback DrissionPage: cari input dari page root (kurang akurat,
        dipakai jika _drission_host_input gagal karena host tidak ditemukan)."""
        try:
            for sel in ('xpath://input[@type="file"]', 'css:input[type="file"]', '@type=file'):
                try:
                    inputs = list(page.eles(sel, timeout=3) or [])
                    for inp in inputs:
                        try:
                            accept = (inp.attr('accept') or '').lower()
                            inp_id = inp.attr('id') or ''
                            # Skip input video utama
                            if inp_id == 'file-loader' and 'image' not in accept:
                                continue
                            self.safe_log(f"[THUMBNAIL] DrissionPage fallback: id={inp_id} accept={accept}")
                            inp.input(abs_path, clear=False)
                            self.safe_log("[THUMBNAIL] DrissionPage fallback .input() OK")
                            time.sleep(2)
                            return True
                        except Exception as e:
                            self.safe_log(f"[THUMBNAIL] DrissionPage fallback input error: {e}")
                            continue
                except Exception:
                    pass
        except Exception as e:
            self.safe_log(f"[THUMBNAIL] DrissionPage fallback error: {e}")
        return False

    def _verify_thumbnail_applied(self, page, timeout=12, before_srcs=None,
                                  expect_name=None):
        """Verifikasi thumbnail user benar-benar terpasang.

        Deteksi berbasis PERUBAHAN, bukan tebak-tebakan pola URL: bandingkan
        daftar gambar di editor SEBELUM vs SESUDAH file dikirim. Munculnya
        gambar baru = thumbnail user masuk. Cara ini tidak bergantung pada
        bentuk URL yang dipakai Studio (blob:, data:, atau background-image),
        yang ternyata berbeda-beda antar build.

        Kalau tetap tidak terdeteksi, daftar src dicetak ke log supaya
        kegagalan berikutnya bisa didiagnosis tanpa menebak.
        """
        before = set(before_srcs or [])
        deadline = time.time() + timeout
        last = {}
        while True:
            st = self._thumbnail_state(page)
            last = st
            if st.get('rejected'):
                self.safe_log(f"[THUMBNAIL] YouTube menolak: {st.get('text', '')[:100].strip()}")
                return False
            if st.get('applied'):
                self.safe_log("[THUMBNAIL] Studio menerima thumbnail "
                              "(tombol preview/options muncul).")
                return True
            if st.get('custom'):
                self.safe_log("[THUMBNAIL] Preview thumbnail user terdeteksi (blob/data).")
                return True
            # Auto-still bikinan YouTube (i9.ytimg.com/vi/<id>/mq*.jpg) MUNCUL
            # BERTAHAP selama video diproses, jadi "ada src baru" saja bukan
            # bukti — itu sempat lolos sebagai false positive. Domain milik
            # YouTube dikecualikan; thumbnail user tidak pernah dari sana.
            baru = [s for s in (st.get('srcs') or [])
                    if s not in before and not self._is_youtube_still(s)]
            if baru:
                self.safe_log(f"[THUMBNAIL] Gambar non-YouTube baru di editor: {baru[0][:90]}")
                return True
            if time.time() >= deadline:
                break
            time.sleep(1)

        srcs = last.get('srcs') or []
        self.safe_log(f"[THUMBNAIL] Preview belum berubah dalam {timeout}s "
                      f"(img={last.get('imgs')}, file_di_input={last.get('fileset')}, "
                      f"nama='{last.get('filename')}')")
        if last.get('need_verify'):
            self.safe_log("[THUMBNAIL] !! Area thumbnail menyebut VERIFIKASI — "
                          "custom thumbnail butuh channel terverifikasi (nomor HP). "
                          "Selama belum diverifikasi, tidak ada cara otomatis yang bisa.")
        self.safe_log(f"[THUMBNAIL] Teks area thumbnail: "
                      f"{(last.get('text') or '').strip()[:200]}")
        for s in srcs[:6]:
            self.safe_log(f"[THUMBNAIL]   src: {s[:110]}")
        return False

    def _get_current_video_id(self, page):
        try:
            video_id = page.run_js(r"""
            return (function(){
                const dialog = document.querySelector('ytcp-uploads-dialog[video-id]');
                if(dialog) return dialog.getAttribute('video-id') || '';
                const link = document.querySelector('a[href*="youtu.be/"], a[href*="/watch?v="]');
                if(link){
                    const href = link.getAttribute('href') || '';
                    let m = href.match(/youtu\.be\/([A-Za-z0-9_-]{6,})/i);
                    if(m) return m[1];
                    m = href.match(/[?&]v=([A-Za-z0-9_-]{6,})/i);
                    if(m) return m[1];
                }
                const body = document.body ? (document.body.innerText || '') : '';
                let m = body.match(/https?:\/\/youtu\.be\/([A-Za-z0-9_-]{6,})/i);
                if(m) return m[1];
                let m2 = (location.href || '').match(/\/video\/([^\/\?]+)/i);
                return m2 ? m2[1] : '';
            })()
            """)
            return str(video_id or "").strip()
        except Exception:
            return ""

    def _upload_thumbnail_via_edit_page(self, page, video_id, thumbnail_path):
        if not video_id:
            self.safe_log("[THUMBNAIL] Video ID tidak ditemukan untuk fallback edit page.")
            return False
        self.safe_log(f"[THUMBNAIL] Fallback ke edit page video: {video_id}")
        page.get(f"https://studio.youtube.com/video/{video_id}/edit?hl=en")
        time.sleep(6)
        self._inject_stealth(page)
        self._dismiss_verify_dialog(page)
        self._clear_upload_files_intercept(page)
        return self._apply_thumbnail_on_current_page(page, thumbnail_path)

    def _save_edit_page_if_present(self, page):
        try:
            clicked = bool(page.run_js(r"""
            return (function(){
                const btn = Array.from(
                    document.querySelectorAll('ytcp-button, button, [role="button"]')
                ).find(el => {
                    const txt = ((el.innerText || el.textContent || '').trim().toUpperCase());
                    const disabled = el.disabled || el.hasAttribute('disabled') ||
                        el.getAttribute('aria-disabled') === 'true' ||
                        el.classList.contains('ytcp-button--disabled');
                    return txt === 'SAVE' && !disabled;
                });
                if(!btn) return false;
                btn.scrollIntoView({behavior:'instant', block:'center'});
                btn.click();
                return true;
            })()
            """))
            if clicked:
                self.safe_log("[THUMBNAIL] Save diklik di edit page.")
            return clicked
        except Exception:
            return False


    def _clear_upload_files_intercept(self, page):
        for candidate in (None, "", []):
            try:
                page.set.upload_files(candidate)
                return True
            except Exception:
                continue
        return False

    def _click_select_files_button(self, page):
        """Cari dan klik tombol 'SELECT FILES' / 'PILIH FILE' di YouTube Studio.
        Gunakan DrissionPage native ele() untuk memotong shadow DOM."""
        for sel in ('#select-files-button', '@id=select-files-button',
                    'tag:ytcp-button@id=select-files-button',
                    '@aria-label*SELECT FILES', '@aria-label*PILIH FILE',
                    'text:SELECT FILES', 'text:PILIH FILE', 'text:PILIH FILES'):
            try:
                btn = page.ele(sel, timeout=2)
                if btn:
                    try:
                        btn.click()
                    except Exception:
                        page.run_js("arguments[0].click()", btn)
                    self.safe_log(f"[UPLOAD] Tombol Select Files diklik via '{sel}'")
                    return True
            except Exception:
                pass

        # Fallback JS deep walk
        try:
            return bool(page.run_js("""
            return (function(){
                function findBtn(root) {
                    if (!root) return null;
                    var targets = ['SELECT FILES', 'PILIH FILE', 'PILIH FILES', 'UPLOAD VIDEO', 'UNGGAH VIDEO'];
                    var all = root.querySelectorAll ? root.querySelectorAll('ytcp-button, tp-yt-paper-button, button, [role="button"]') : [];
                    for (var el of all) {
                        var txt = ((el.innerText || el.textContent || el.getAttribute('aria-label')) || '').trim().toUpperCase();
                        var disabled = el.disabled || el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true';
                        if (!disabled && targets.some(t => txt === t || txt.includes(t))) {
                            return el;
                        }
                    }
                    var children = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var c of children) {
                        if (c.shadowRoot) {
                            var found = findBtn(c.shadowRoot);
                            if (found) return found;
                        }
                    }
                    return null;
                }
                var b = findBtn(document);
                if (b) {
                    b.scrollIntoView({behavior:'instant', block:'center'});
                    b.click();
                    return true;
                }
                return false;
            })()
            """))
        except Exception:
            return False

    def _send_upload_file(self, page, file_input, video_path):
        """Kirim file video ke YouTube Studio.

        Urutan metode (dari paling reliable):
        Metode 0: CDP setFileInputFiles langsung via node_id (TIDAK memicu OS dialog)
        Metode 1: DrissionPage set.upload_files intercept + file_input.input()
                  (intercept dipasang SEBELUM input agar OS dialog tidak blocking)
        Metode 2: CDP setFileInputFiles via shadow DOM search + composed events
        Metode 3: File Chooser Intercept + klik tombol Select Files

        FIX: Masalah utama adalah Metode 1 lama (file_input.input() tanpa
        intercept) bisa memicu OS file dialog yang blocking di non-headless mode.
        Sekarang intercept dipasang dulu via page.set.upload_files() sebelum
        memanggil input(), sehingga OS dialog tidak pernah muncul.
        """
        abs_path = os.path.abspath(video_path)
        self.safe_log(f"[UPLOAD] Memulai pengiriman file video: {os.path.basename(abs_path)}")

        def _dispatch_change_events():
            """Dispatch change/input events ke semua file inputs (shadow DOM aware)."""
            try:
                page.run_js(r"""
                return (function(){
                    function findInputs(root, out){
                        if (!root) return;
                        var els = root.querySelectorAll ? root.querySelectorAll('input[type="file"]') : [];
                        for (var el of els) out.push(el);
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var c of all){
                            if (c.shadowRoot) findInputs(c.shadowRoot, out);
                        }
                    }
                    var inputs = [];
                    findInputs(document, inputs);
                    for (var inp of inputs) {
                        try {
                            inp.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                            inp.dispatchEvent(new Event('input',  {bubbles: true, composed: true}));
                        } catch(e){}
                    }
                })()
                """)
            except Exception:
                pass

        def _wait_dialog(label, wait_secs=20):
            """Tunggu hingga upload dialog muncul. Return True jika berhasil."""
            time.sleep(2)
            for i in range(int(wait_secs / 1.5)):
                if self._upload_dialog_started(page):
                    self.safe_log(f"[UPLOAD] ✓ File diterima via {label}!")
                    return True
                time.sleep(1.5)
            return False

        # ── Metode 0: CDP setFileInputFiles via node_id (paling aman) ────────
        # Tidak memicu OS dialog sama sekali, langsung inject file ke input element
        try:
            self.safe_log("[UPLOAD] Metode 0: CDP setFileInputFiles via node_id...")
            if self._cdp_set_upload_file(page, abs_path):
                _dispatch_change_events()
                if _wait_dialog("CDP setFileInputFiles (node_id)"):
                    return True
        except Exception as e:
            self.safe_log(f"[UPLOAD] Metode 0 error: {e}")

        # ── Metode 1: set.upload_files intercept + file_input.input() ─────────
        # KRITIS: intercept harus dipasang SEBELUM input() agar OS dialog
        # tidak pernah muncul dan blocking thread.
        try:
            if file_input:
                self.safe_log("[UPLOAD] Metode 1: upload_files intercept + file_input.input()...")
                page.set.upload_files(abs_path)   # intercept dulu
                time.sleep(0.2)
                file_input.input(abs_path)         # sekarang input() tidak memicu OS dialog
                _dispatch_change_events()
                self._clear_upload_files_intercept(page)
                if _wait_dialog("upload_files intercept + input()"):
                    return True
        except Exception as e:
            self.safe_log(f"[UPLOAD] Metode 1 error: {e}")
            self._clear_upload_files_intercept(page)

        # ── Metode 2: CDP setFileInputFiles via shadow DOM walk ───────────────
        try:
            self.safe_log("[UPLOAD] Metode 2: CDP setFileInputFiles shadow DOM walk...")
            # Cari node_id file input via shadow DOM walk JS
            expr = r"""
            (function(){
                function find(root){
                    if(!root) return null;
                    const direct = root.querySelector && root.querySelector('input[type="file"]');
                    if(direct) return direct;
                    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for(const el of all){
                        if((el.tagName || '').toLowerCase() === 'input' && el.type === 'file') return el;
                        if(el.shadowRoot){
                            const found = find(el.shadowRoot);
                            if(found) return found;
                        }
                    }
                    return null;
                }
                return find(document);
            })()
            """
            res = page.run_cdp('Runtime.evaluate', expression=expr, objectGroup='upload-file-m2')
            obj_id = (res.get('result') or {}).get('objectId')
            if obj_id:
                node = page.run_cdp('DOM.requestNode', objectId=obj_id)
                node_id = node.get('nodeId')
                if node_id:
                    page.run_cdp('DOM.setFileInputFiles', files=[abs_path], nodeId=node_id)
                    _dispatch_change_events()
                    if _wait_dialog("CDP shadow DOM walk"):
                        return True
        except Exception as e:
            self.safe_log(f"[UPLOAD] Metode 2 error: {e}")

        # ── Metode 3: File Chooser Intercept + klik Select Files button ───────
        try:
            self.safe_log("[UPLOAD] Metode 3: File chooser intercept + Select Files button...")
            page.set.upload_files(abs_path)
            time.sleep(0.3)
            self._click_select_files_button(page)
            try:
                if file_input:
                    file_input.click()
            except Exception:
                pass
            if _wait_dialog("file chooser intercept"):
                self._clear_upload_files_intercept(page)
                return True
        except Exception as e:
            self.safe_log(f"[UPLOAD] Metode 3 error: {e}")

        self._clear_upload_files_intercept(page)
        self.safe_log("[UPLOAD] Memeriksa status akhir dialog upload...")
        return self._upload_dialog_started(page)






    def _is_upload_success(self, page):
        """Deteksi upload sukses dari URL redirect atau dialog hilang."""
        try:
            url = (page.url or "").lower()
            # 1. URL patterns setelah publish sukses
            success_pats = ["/videos/short", "/content", "/videos?", "/video/", "/dashboard"]
            for pat in success_pats:
                if pat in url and "upload" not in url:
                    return True
            
            # 2. Dialog upload hilang/hidden
            # Kita cek beberapa elemen dialog sekaligus
            gone = page.run_js("""
                var selectors = [
                    'ytcp-uploads-dialog', 
                    'ytcp-video-upload-dialog', 
                    'ytcp-upload-indicator-root',
                    'ytcp-video-metadata-editor'
                ];
                for(var s of selectors){
                    var d = document.querySelector(s);
                    if(d){
                        var style = window.getComputedStyle(d);
                        if(style.display !== 'none' && style.visibility !== 'hidden' && d.offsetWidth > 0){
                            return false; // Masih ada dialog yang terlihat
                        }
                    }
                }
                return true; // Tidak ada dialog yang terlihat
            """)
            
            # Jika dialog hilang dan kita di studio, kemungkinan besar sukses
            # Pastikan bukan di awal proses (Cek path lebih spesifik)
            if gone and "studio.youtube.com" in url:
                # Jika sudah redirect ke content atau dashboard, itu sukses
                if "/content" in url or "/videos" in url or "/dashboard" in url:
                    return True
                # Jika dialog hilang total dan URL tidak lagi mengandung "upload", itu sukses
                if "upload" not in url:
                    return True
                
            # 3. Cek tombol "Close", "Done", atau teks sukses
            has_success_indicators = page.run_js("""
                var txt = document.body.innerText.toUpperCase();
                var success_words = ['FINISHED', 'COMPLETE', 'PUBLISHED', 'UPLOADED', 'SELESAI', 'BERHASIL'];
                var found_word = success_words.some(w => txt.includes(w));
                
                var btns = document.querySelectorAll('ytcp-button, button');
                var found_close = Array.from(btns).some(b => {
                    var t = (b.innerText||'').trim().toUpperCase();
                    return t === 'CLOSE' || t === 'DONE' || t === 'SELESAI' || t === 'TUTUP';
                });
                
                return found_word || found_close;
            """)
            
            if has_success_indicators and gone:
                return True

            return False
        except Exception:
            return False

    # ── Main Upload ───────────────────────────────────────────────────────────────

    def upload_single(self, account_id, video_data, options=None,
                      progress_callback=None):
        account_id   = str(account_id)
        options      = options or {}
        invisible    = bool(options.get('invisible_mode', False))
        delete_after = bool(options.get('delete_after_upload', False))
        video_data   = dict(video_data or {})
        # Handle ai_content/made_for_kids dari options:
        # - True/False eksplisit → override video_data
        # - None → jangan override (ikuti nilai dari CSV/video_data)
        ai_opt = options.get('ai_content')
        if ai_opt is not None:
            video_data['ai_content'] = bool(ai_opt)
        # made_for_kids TIDAK pernah di-override jadi True: GAMA selalu upload
        # sebagai "Not made for kids" (lihat _do_upload / _select_audience).
        if options.get('made_for_kids'):
            self.safe_log("[AUDIENCE] options.made_for_kids=True diabaikan — "
                          "GAMA selalu 'Not made for kids'.")
        video_data['made_for_kids'] = False
        self.reset_stop()

        self._port = acquire_port()
        logger.info(f"Upload port: {self._port} | headless: {invisible}")

        try:
            result = self._do_upload(
                account_id, video_data, invisible, delete_after,
                progress_callback)
            return result
        except Exception as e:
            # Log error secara eksplisit sebelum re-raise supaya masuk ke status file
            err_msg = str(e)
            self.safe_log(f"[FATAL ERROR] Upload gagal: {err_msg}")
            raise
        finally:
            # Selalu tutup browser (sukses maupun gagal)
            # FIX: tambah kill_chrome_on_port setelah quit() untuk memastikan
            # proses Chrome benar-benar terminated sebelum profil bisa dibuka
            # lagi oleh video berikutnya dalam batch.
            if self.page:
                try:
                    time.sleep(1.0)
                    self.page.quit()
                    logger.info("Browser closed.")
                except Exception:
                    pass
                self.page = None
            # Pastikan port & profil benar-benar bebas sebelum lanjut
            if self._port:
                try:
                    self.kill_chrome_on_port(self._port)
                except Exception:
                    pass
            # Sleep 5s agar Chrome benar-benar terminate dan profil unlock
            # sebelum video berikutnya dalam batch mencoba buka profil yang sama
            time.sleep(5)
            # Release port
            release_port(self._port)
            self._port = None

    def _do_upload(self, account_id, video_data, invisible,
                   delete_after, cb):
        self._raise_if_stopped()
        self._reset_upload_steps()
        # ── Get account ───────────────────────────────────────────────────
        acc = self._get_account(account_id)
        if not acc:
            raise Exception(f"Akun tidak ditemukan: {account_id}")

        auth       = (getattr(acc, 'auth_method', '') or "").upper()
        is_cookies = auth == "COOKIES"

        video_path = self._clean_path(video_data.get('video_path', ''))
        self._step_log(f"Validasi file video: {video_path}")
        if not os.path.exists(video_path):
            # Try fallback placeholder located in project root
            placeholder = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sample_video.mp4'))
            if os.path.exists(placeholder):
                self.safe_log(f"[WARNING] File video tidak ditemukan, menggunakan placeholder: {placeholder}")
                video_path = placeholder
            else:
                # Create an empty placeholder file
                try:
                    with open(placeholder, 'wb') as f:
                        f.write(b'')
                    self.safe_log(f"[INFO] Placeholder video dibuat: {placeholder}")
                    video_path = placeholder
                except Exception as e:
                    raise Exception(f"File video tidak ditemukan: {video_path}. Gagal membuat placeholder: {e}")

        title       = str(video_data.get('title', '') or
                         f"Video {datetime.now().strftime('%Y-%m-%d')}")
        description = str(video_data.get('description', '') or '')
        tags           = str(video_data.get('tags', '') or '')
        thumbnail_path = self._clean_path(video_data.get('thumbnail_path')) if video_data.get('thumbnail_path') else None
        privacy        = str(video_data.get('privacy', 'public') or 'public').lower()
        raw_ai_content = video_data.get('ai_content')
        if raw_ai_content is None:
            # Tidak diisi di CSV / 0 — default False (tidak mengandung AI)
            ai_content = False
        elif isinstance(raw_ai_content, str):
            ai_content = raw_ai_content.strip().lower() in ("1", "true", "yes", "y", "ya", "iya", "checked", "x")
        else:
            ai_content = bool(raw_ai_content)

        # ── Audience: SELALU "Not made for kids" ──────────────────────────
        # Kebijakan GAMA: semua upload YouTube ditandai BUKAN konten anak.
        # Nilai dari CSV/checkbox sengaja diabaikan (bukan dibaca lalu
        # ditimpa) supaya tidak ada jalur yang bisa menyetel MFK=yes —
        # video yang ditandai "made for kids" kehilangan komentar, notifikasi,
        # playlist, dan monetisasi personalisasi.
        made_for_kids = False
        raw_made_for_kids = video_data.get('made_for_kids')
        if raw_made_for_kids not in (None, '', False, 0, '0'):
            self.safe_log(f"[AUDIENCE] made_for_kids='{raw_made_for_kids}' diabaikan — "
                          f"GAMA selalu memilih 'Not made for kids'.")
        thumbnail_uploaded = False
        upload_video_id = ""

        self.safe_log(f"[UPLOADING] {os.path.basename(video_path)} | {title[:40]}"
                     f" | headless={invisible}")

        # ── 1. Launch Browser ─────────────────────────────────────────────
        self._emit_progress(cb, 5, "Membuka browser...")
        if is_cookies:
            cookies_path = self._get_cookies_path(account_id)
            if not cookies_path:
                raise Exception(
                    "File cookies tidak ditemukan!\n"
                    "Hapus akun dan tambahkan ulang dengan import cookies baru.")
            page = self._get_page_with_cookies(cookies_path, invisible=invisible)
        else:
            session_path = self._get_session_path(account_id)
            if not session_path:
                raise Exception(
                    "Session folder tidak ditemukan!\n"
                    "Buka akun ini di browser untuk login ulang.")
            page = self._get_page(session_path, invisible=invisible)

        self._emit_progress(cb, 12, "Browser terbuka, membuka YouTube Studio...")

        # ── 2. Buka YouTube Studio ────────────────────────────────────────
        page.get("https://studio.youtube.com/?hl=en")
        time.sleep(4)
        self._inject_stealth(page)
        self._dismiss_verify_dialog(page)

        # Poll hingga 10 detik untuk beri waktu Studio memuat UI
        is_logged_in = False
        for _ in range(6):
            try:
                is_logged_in = bool(page.run_js("""
                    return !!(document.querySelector('button#avatar-btn') ||
                              document.querySelector('ytcp-home-page') ||
                              document.querySelector('ytcp-navigation-sidebar') ||
                              document.querySelector('ytcp-header') ||
                              document.querySelector('ytcp-app'));
                """))
                if is_logged_in:
                    break
            except Exception:
                pass
            time.sleep(1.5)

        current_url = (page.url or '').lower()
        if not is_logged_in and ('accounts.google.com' in current_url or 'signin' in current_url):
            if is_cookies:
                raise Exception(
                    "Cookies expired! Export cookies baru dari browser "
                    "dan update akun ini.")
            raise Exception(
                "Session expired! Klik 'Buka' pada akun ini untuk login ulang.")

        if "/studio.youtube.com" not in page.url:
            page.get("https://studio.youtube.com/?hl=en")
            time.sleep(5 if invisible else 2)

        self._emit_progress(cb, 20, "YouTube Studio terbuka...")

        # ── 3. Cari file input ────────────────────────────────────────────
        self._emit_progress(cb, 25, "Mencari tombol upload...")
        file_input = None

        page.get("https://www.youtube.com/upload?hl=en")
        time.sleep(5 if invisible else 3)
        self._inject_stealth(page)

        for attempt in range(8):
            file_input = self._find_upload_file_input(page, timeout=5)
            if file_input:
                self.safe_log(f"[UPLOAD] File input ditemukan attempt {attempt + 1}.")
                break
            if attempt == 3:
                page.get("https://studio.youtube.com/?hl=en")
                time.sleep(3)
                page.run_js("""
                return (function(){
                    var btns = document.querySelectorAll(
                        'ytcp-button,button,ytcp-icon-button');
                    for(var b of btns){
                        var lbl = (b.getAttribute('aria-label')||
                                   b.innerText||'').toUpperCase();
                        if(lbl.includes('CREATE')||lbl.includes('UPLOAD')){
                            b.click(); return;
                        }
                    }
                })()
                """)
                time.sleep(2)
                self._js_click(page, "Upload videos", timeout=4)
                time.sleep(2)
            time.sleep(2)

        if not file_input:
            raise Exception(
                "[Step 3/Cari Upload Button] File input tidak ditemukan di YouTube Studio. "
                "Kemungkinan session expired atau YouTube mengubah tampilan UI. "
                "Coba login ulang akun ini.")

        # ── 4. Input File ─────────────────────────────────────────────────
        self._emit_progress(cb, 30, f"Mengirim file: {os.path.basename(video_path)}")
        if not self._send_upload_file(page, file_input, video_path):
            raise Exception(
                f"[Step 4/Kirim File] Gagal memulai upload '{os.path.basename(video_path)}'. "
                "File terdeteksi tapi YouTube tidak merespons. "
                "Pastikan file tidak dipakai aplikasi lain dan koneksi internet stabil.")

        # ── 5. Tunggu Dialog Metadata ─────────────────────────────────────
        self._emit_progress(cb, 35, "Menunggu dialog upload...")
        dialog_ready = False
        for i in range(30 if invisible else 20):
            try:
                if self._upload_dialog_started(page):
                    dialog_ready = True
                    break
                self._dismiss_verify_dialog(page)
            except Exception:
                pass
            time.sleep(2)

        if not dialog_ready:
            raise Exception(
                "[Step 5/Tunggu Dialog] Dialog metadata upload tidak muncul setelah file dikirim. "
                "Cek koneksi internet, atau coba format file lain (MP4 lebih stabil dari TS).")
        upload_video_id = self._get_current_video_id(page)

        self._human_delay(2.0, 3.0)

        # ── 6. Title ──────────────────────────────────────────────────────
        self._emit_progress(cb, 40, "Mengisi judul...")
        title_set = False
        for attempt in range(5):
            self._js_set_text(page, "#title-textarea #textbox", title[:100])
            time.sleep(0.5)
            # Verifikasi via shadow DOM walk (document.querySelector tidak bisa
            # menembus shadow DOM boundary — bug lama)
            actual = self._js_get_text(page, is_title=True)
            if actual and actual.strip():
                self.safe_log(f"[TITLE] OK (attempt {attempt+1}): {actual[:60]}")
                logger.info(f"Title set: {actual[:50]}")
                title_set = True
                break
            self.safe_log(f"[TITLE] Belum terisi (attempt {attempt+1}/5), retry...")
            time.sleep(1.5)
        if not title_set:
            self.safe_log(f"[TITLE] WARNING: judul mungkin tidak terisi setelah 5 percobaan")

        # ── 7. Description ────────────────────────────────────────────────
        self._emit_progress(cb, 45, "Mengisi deskripsi...")
        if description.strip():
            self._js_set_text(
                page, "#description-textarea #textbox",
                description[:5000])
        self._human_delay(1.0, 2.0)

        # ── 8. Thumbnail (Optional) ───────────────────────────────────────
        if thumbnail_path:
            self.safe_log(f"[THUMBNAIL] Path: {thumbnail_path}")
            self.safe_log(f"[THUMBNAIL] File ada: {os.path.exists(thumbnail_path)}")
            if not os.path.exists(thumbnail_path):
                self.safe_log(f"[THUMBNAIL] SKIP — file tidak ditemukan: {thumbnail_path}")
        else:
            self.safe_log("[THUMBNAIL] SKIP — tidak ada thumbnail_path")

        if thumbnail_path and os.path.exists(thumbnail_path):
            self._emit_progress(cb, 50, f"Mengunggah thumbnail: {os.path.basename(thumbnail_path)}...")
            # 2x putaran saja: tiap panggilan _apply_thumbnail_on_current_page
            # sudah mencoba 4 metode berbeda + verifikasi masing-masing, jadi
            # 3 putaran (versi lama) cuma menahan upload berlama-lama saat
            # thumbnail memang tidak bisa dipasang.
            for thumb_attempt in range(1, 3):
                self.safe_log(f"[THUMBNAIL] Putaran {thumb_attempt}/2 — "
                              f"{os.path.basename(thumbnail_path)} "
                              f"({os.path.getsize(thumbnail_path)} bytes)")
                try:
                    thumbnail_uploaded = self._apply_thumbnail_on_current_page(
                        page, thumbnail_path,
                        # Putaran 2: form sudah terbukti siap di putaran 1,
                        # jangan menunggu auto-thumbnail dari nol lagi.
                        wait_ready=(thumb_attempt == 1),
                        budget=75 if thumb_attempt == 1 else 40)
                except Exception as te:
                    self.safe_log(f"[THUMBNAIL] Exception putaran {thumb_attempt}: {te}")
                    thumbnail_uploaded = False
                if thumbnail_uploaded:
                    self.safe_log(f"[THUMBNAIL] ✓ BERHASIL pada putaran {thumb_attempt}")
                    break
                if thumb_attempt < 2:
                    self.safe_log("[THUMBNAIL] ✗ Gagal putaran 1 — tunggu 3s lalu ulangi...")
                    time.sleep(3)
            if not thumbnail_uploaded:
                # Thumbnail gagal = WARNING saja, upload tetap lanjut.
                # Video tetap bisa dipublish/schedule tanpa custom thumbnail.
                self.safe_log(
                    f"[THUMBNAIL] ✗ GAGAL setelah 2 putaran — "
                    f"upload tetap LANJUT tanpa thumbnail custom. "
                    f"File: {os.path.basename(thumbnail_path)}"
                )

        # ── 9. Tags (Optional) ────────────────────────────────────────────
        if tags.strip():
            self._emit_progress(cb, 53, "Mengisi tags...")
            try:
                self._debug_log("Expanding Show More for tags...")
                # Klik "SHOW MORE" via text detection (Sequential Check)
                for label in ["SHOW MORE", "TAMPILKAN LEBIH BANYAK"]:
                    try:
                        if self._js_click(page, label, timeout=5):
                            self.safe_log(f"[NAVIGATE] Clicked button: {label}")
                            break
                    except:
                        continue
                time.sleep(2.5)
                
                # Scroll ke bawah untuk memastikan input "terdeteksi" oleh DOM
                page.run_js("window.scrollTo(0, document.body.scrollHeight/2);")
                self._human_delay(1.5, 2.5)
                
                # Isi tags via JS set text
                self._js_set_text(page, "#tags-container #text-input", tags)
                logger.info(f"Tags set: {tags[:50]}...")
                time.sleep(1.5)
            except Exception as tge:
                logger.warning(f"Gagal mengisi tags: {tge}")

        # ── 9b. Buka "Show more" SELALU ───────────────────────────────────
        # Section 'Altered content' (dan beberapa opsi lain) berada di dalam
        # area yang tersembunyi di balik "Show more". Sebelumnya tombol itu
        # hanya diklik saat kolom TAGS terisi, sehingga untuk baris CSV tanpa
        # tags radio Altered content TIDAK PERNAH ada di DOM — dan langkah AI
        # diam-diam salah sasaran.
        self._expand_show_more(page)

        # ── 10. Audience (Not for Kids) ───────────────────────────────────
        self._emit_progress(cb, 55, "Memeriksa status Audience...")
        self._debug_log("Checking Audience (Not Made for Kids)...")
        self._human_delay(2.0, 4.0) # Human pause before selecting
        self._select_audience(page, made_for_kids)
        self._human_delay(2.0, 3.5)

        # ── 11. Navigasi STRICT 3x NEXT (Human-paced) ─────────────────────
        self._emit_progress(
            cb, 58,
            "Memilih Altered content: YES" if ai_content else "Memilih Altered content: NO"
        )
        self._select_altered_content_disclosure(page, ai_content)
        self._human_delay(2.0, 3.0)  # beri waktu UI update setelah AI selection

        # PENGAMAN: pastikan Audience masih 'Not made for kids'.
        # Langkah Altered-content sempat mengklik radio "Yes, it's made for kids"
        # (teksnya diawali "Yes,") dan membatalkan pilihan yang sudah benar —
        # hasil upload jadi made-for-kids=YES walau log Audience bilang sukses.
        # Sumbernya sudah diperbaiki, ini jaring pengaman terakhir sebelum NEXT.
        self._select_audience(page)

        self._emit_progress(cb, 60, "Navigasi 3 langkah dengan jeda manusiawi...")

        for i in range(3):
            self._human_delay(2.5, 4.0)
            self.safe_log(f"[NAVIGATE] Mencoba klik NEXT ke-{i+1}/3")

            # Scroll ke atas sebelum klik NEXT — tombol bisa tersembunyi
            try:
                page.run_js("window.scrollTo(0, 0);")
                time.sleep(0.5)
            except Exception:
                pass

            success = False
            for retry in range(4):
                if self._click_next(page):
                    success = True
                    self.safe_log(f"[SUCCESS] Berhasil klik NEXT ke-{i+1}")
                    break

                # Cek apakah sudah lompat ke Visibility
                if i >= 1:
                    try:
                        is_moved = page.run_js("return document.querySelector('ytcp-video-visibility-select') !== null")
                        if is_moved:
                            self.safe_log(f"[SYNC] Step {i+1} sudah di Visibility, skip.")
                            success = True
                            break
                    except Exception:
                        pass

                time.sleep(2.5)

            if not success:
                is_vis = page.run_js("return document.querySelector('ytcp-video-visibility-select') !== null")
                if is_vis:
                    self.safe_log(f"[SYNC] Sinkronisasi ulang: Sudah di Visibility.")
                    break
                else:
                    upload_video_id = upload_video_id or self._get_current_video_id(page)
                    if upload_video_id:
                        self.safe_log(f"[NEXT] Gagal klik NEXT ke-{i+1} — video ID: {upload_video_id}, coba edit page fallback.")
                        if thumbnail_path and os.path.exists(thumbnail_path) and not thumbnail_uploaded:
                            try:
                                self.safe_log(f"[THUMBNAIL] Mencoba upload via edit page untuk {upload_video_id}...")
                                thumbnail_uploaded = self._upload_thumbnail_via_edit_page(
                                    page, upload_video_id, thumbnail_path
                                )
                                if thumbnail_uploaded:
                                    self.safe_log(f"[THUMBNAIL] ✓ Berhasil via edit page.")
                                    self._save_edit_page_if_present(page)
                                else:
                                    self.safe_log(f"[THUMBNAIL] ✗ Edit page juga gagal.")
                            except Exception as te:
                                self.safe_log(f"[THUMBNAIL] ✗ Edit page exception: {te}")
                                logger.warning(f"[THUMBNAIL] Edit page fallback gagal: {te}")
                        self._emit_progress(cb, 100, "Tugas Selesai (via fallback)!")
                        self.safe_log("[FINISHED] Selesai via fallback edit page setelah NEXT gagal.")
                        try:
                            page.quit()
                        except Exception:
                            pass
                        return True
                    # Tidak ada video_id → log kondisi halaman untuk debug
                    try:
                        page_url = page.url
                        page_text = (page.run_js("return document.body?.innerText?.substring(0,300)") or '')
                        self.safe_log(f"[NEXT] FATAL — tidak ada video_id. URL: {page_url}")
                        self.safe_log(f"[NEXT] Isi halaman: {page_text[:200]}")
                    except Exception as dbg_e:
                        self.safe_log(f"[NEXT] Debug page gagal: {dbg_e}")
                    raise Exception(
                        f"NEXT step {i+1} gagal setelah 4 retry. "
                        f"Audience/AI selection mungkin belum selesai atau form belum siap. "
                        f"Cek log [AUDIENCE] dan [AI] di atas untuk detail."
                    )

        # ── 12. Stage 12: Manual Schedule Fix (User Blueprint) ──────────
        # csv_loader menulis None (bukan '') untuk baris tanpa jadwal, jadi
        # `.get("date", "")` tetap mengembalikan None dan `.strip()` meledak
        # dengan AttributeError sebelum sempat publish. Bungkus dengan `or ''`.
        date_str = str(video_data.get("date") or "").strip()
        time_str = str(video_data.get("time") or "").strip()

        # ── TUNGGU UPLOAD 100% SEBELUM MENJADWALKAN ──────────────────────
        #
        # URUTANNYA PENTING dan sempat salah: penungguan ini dulu ditaruh
        # SESUDAH tombol Schedule ditekan. Itu tidak cukup, karena setelah
        # setiap video GAMA menutup browser lalu mem-force-kill Chrome
        # (lihat blok finally di upload_single) — dan YouTube meneruskan
        # unggahan HANYA selama tab-nya hidup. Begitu ditutup, unggahan yang
        # belum selesai dibatalkan; hasilnya video tertinggal di 31%/99%
        # dengan status "Tertunda", tidak pernah jadi "Terjadwal".
        #
        # Dengan menunggu DI SINI, berkasnya sudah utuh di server YouTube
        # sebelum jadwal dipasang, sehingga penutupan browser sesudahnya
        # tidak lagi merusak apa pun.
        self._wait_upload_100(page, cb, timeout=1800)

        # ── CASE A: SCHEDULE ────────────────────────────────────────────
        if date_str and time_str:
            self.safe_log(f"[MODE] SCHEDULE (Target: {date_str} {time_str})")
            # Tunggu Visibility panel muncul sebelum schedule
            self.safe_log("[SCHED] Tunggu ytcp-video-visibility-select...")
            for _ in range(15):
                try:
                    vis = page.run_js("return !!document.querySelector('ytcp-video-visibility-select')")
                    if vis:
                        self.safe_log("[SCHED] Visibility panel siap.")
                        break
                except Exception:
                    pass
                time.sleep(1)
            time.sleep(2)  # Extra buffer
            self._do_schedule_cdp(page, date_str, time_str)
            # Jadwal BELUM tentu tersimpan begitu dialog menutup. Browser
            # ditutup+force-kill sesaat setelah ini, dan kalau Studio belum
            # sempat menyimpan, videonya tertinggal sebagai DRAF — persis yang
            # terjadi pada 5 video user tanggal 10 Agu. Jadi status barisnya
            # dibaca dulu sampai benar-benar "Scheduled".
            self._pastikan_terjadwal(page, str(video_data.get("title") or ""))
            self.safe_log("[FINISH] Alur Schedule selesai.")

        # ── CASE B: PUBLISH NOW (USER BLUEPRINT) ────────────────────────
        else:
            self._emit_progress(cb, 95, "Langsung Publish ke Publik...")
            self.safe_log("[MODE] PUBLISH (Immediate)")
            self._wait_and_click(page, ["PUBLISH", "PUBLIKASIKAN", "SAVE", "SIMPAN"], timeout=15)

        # ── 15. Beri waktu dialog menutup ─────────────────────────────────
        # Penungguan 100% sudah dilakukan SEBELUM penjadwalan, jadi di sini
        # cukup jeda pendek untuk memberi Studio waktu menutup dialognya.
        self._human_delay(6, 9)

        # ── 16. Final Cleanup ─────────────────────────────────────────────
        # Hapus Video hanya jika memang diminta.
        if delete_after and os.path.exists(video_path):
            try:
                os.remove(video_path)
                self.safe_log(f"[CLEANUP] Video dihapus: {video_path}")
            except Exception as e:
                self.safe_log(f"[WARNING] Gagal hapus video: {e}")

        # Hapus Thumbnail
        if delete_after and thumbnail_path and os.path.exists(thumbnail_path):
            try:
                os.remove(thumbnail_path)
                self.safe_log(f"[CLEANUP] Thumbnail dihapus: {thumbnail_path}")
            except Exception as e:
                self.safe_log(f"[WARNING] Gagal hapus thumbnail: {e}")

        self._emit_progress(cb, 100, "Tugas Selesai!")
        self.safe_log("[FINISHED] Sesi Berakhir.")

        try:
            page.quit()
        except:
            pass

        return True

    def _pastikan_terjadwal(self, page, judul: str, timeout: int = 180) -> bool:
        """Tunggu sampai baris video BENAR-BENAR berstatus Scheduled.

        KENAPA PERLU: menutup dialog TIDAK sama dengan jadwal tersimpan.
        Terbukti live 10 Agu — log melaporkan "berhasil dijadwalkan", tombol
        sudah terverifikasi berbunyi 'Schedule', tapi videonya tetap mendarat
        sebagai DRAF. Sebabnya: sesudah step Visibility, GAMA cuma menunggu
        6-9 detik lalu menutup DAN mem-force-kill Chrome. Kalau Studio masih
        menjalankan pemeriksaan/pemrosesan, penyimpanan jadwal belum tuntas
        dan ikut mati bersama browsernya.

        Jadi di sini status barisnya dibaca berulang sampai muncul 'Scheduled'
        (atau 'Terjadwal'). Selama masih 'Draft', browser TIDAK boleh ditutup.
        """
        potong = (judul or '').strip()[:40]
        if not potong:
            time.sleep(12)
            return False

        # Barisnya dicari MENEMBUS shadow DOM. Versi pertama cuma memakai
        # document.querySelectorAll biasa dan selalu melaporkan "tidak ketemu"
        # (false negative) walau videonya benar-benar sudah terjadwal.
        js = """
        return (function(){
            var cari = (%s).toLowerCase();
            var rows = [], seen = new Set();
            function kumpul(root, d){
                if (!root || d > 10) return;
                var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (var i = 0; i < all.length; i++){
                    var e = all[i];
                    if (seen.has(e)) continue;
                    seen.add(e);
                    if ((e.tagName || '').toLowerCase() === 'ytcp-video-row') rows.push(e);
                    if (e.shadowRoot) kumpul(e.shadowRoot, d + 1);
                }
            }
            kumpul(document, 0);
            for (var i = 0; i < rows.length; i++){
                var t = (rows[i].innerText || '');
                if (t.toLowerCase().indexOf(cari) < 0) continue;
                var u = t.toUpperCase();
                return {
                    ketemu: true,
                    baris: rows.length,
                    terjadwal: u.indexOf('SCHEDULED') >= 0 || u.indexOf('TERJADWAL') >= 0,
                    draf: u.indexOf('DRAFT') >= 0 || u.indexOf('DRAF') >= 0,
                    cuplikan: t.replace(/\\s+/g, ' ').slice(0, 150)
                };
            }
            return {ketemu: false, baris: rows.length};
        })()
        """ % json.dumps(potong)

        self.safe_log(f"[VERIFY] Menunggu status '{potong}' jadi Scheduled...")
        batas = time.time() + timeout
        terakhir = None
        kosong = 0
        while time.time() < batas:
            self._raise_if_stopped()
            try:
                info = page.run_js(js)
            except Exception:
                info = None
            if isinstance(info, dict) and info.get('ketemu'):
                terakhir = info
                kosong = 0
                if info.get('terjadwal') and not info.get('draf'):
                    self.safe_log("[VERIFY] Status TERJADWAL terkonfirmasi di "
                                  "daftar konten.")
                    return True
            else:
                kosong += 1
                # JANGAN memuat ulang halaman di sini.
                #
                # Percobaan sebelumnya memanggil page.refresh() ketika baris
                # tidak ketemu. Itu SALAH dan justru merusak: dialog upload
                # masih terbuka, sehingga Chrome memunculkan prompt bawaan
                # "Reload site? Changes you made may not be saved". Prompt
                # native itu MEMBEKUKAN halaman (semua eksekusi JS lewat CDP
                # ikut mati — koneksinya sampai terputus), jadi pengecekan
                # berikutnya pasti membaca 0 baris. Dan kalau prompt itu
                # dijawab "Reload", jadwal yang belum tersimpan malah HILANG,
                # menghasilkan draf — persis kegagalan yang sedang dikejar.
                if kosong == 4:
                    self.safe_log("[VERIFY] Daftar video belum ter-render. "
                                  "Menunggu tanpa memuat ulang (memuat ulang "
                                  "akan memunculkan prompt native dan "
                                  "membuang jadwal yang belum tersimpan).")
            time.sleep(5)
            # Daftar konten tidak selalu menyegarkan sendiri.
            try:
                page.run_js("return (function(){"
                            "var b=document.querySelector('#refresh-button');"
                            "if(b) b.click(); return 1;})()")
            except Exception:
                pass

        # Diagnostik: kalau barisnya tidak pernah ketemu, yang paling sering
        # jadi sebab adalah masih ada DIALOG lain yang menutupi halaman (mis.
        # konfirmasi "video masih diproses"). Tanpa dicetak, penyebabnya cuma
        # bisa ditebak-tebak.
        try:
            sisa = page.run_js("""
            return (function(){
                function vis(el){
                    var r = el.getBoundingClientRect();
                    var s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.display !== 'none' && s.visibility !== 'hidden';
                }
                var dlg = [], seen = new Set();
                function jelajah(root, d){
                    if (!root || d > 10) return;
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i = 0; i < all.length; i++){
                        var e = all[i];
                        if (seen.has(e)) continue;
                        seen.add(e);
                        var tag = (e.tagName || '').toLowerCase();
                        if ((tag.indexOf('dialog') >= 0 || e.getAttribute('role') === 'dialog')
                            && vis(e)){
                            dlg.push(tag + '|' + (e.innerText || '')
                                     .replace(/\\s+/g, ' ').trim().slice(0, 90));
                        }
                        if (e.shadowRoot) jelajah(e.shadowRoot, d + 1);
                    }
                }
                jelajah(document, 0);
                return {url: location.href,
                        dialog: dlg.slice(0, 5),
                        teks: (document.body ? document.body.innerText : '')
                              .replace(/\\s+/g, ' ').slice(0, 260)};
            })()
            """)
            self.safe_log(f"[VERIFY] Keadaan halaman saat gagal: {sisa}")
        except Exception as e:
            self.safe_log(f"[VERIFY] Tidak bisa membaca keadaan halaman: {e}")

        self.safe_log(f"[VERIFY] GAGAL: status tidak pernah jadi Scheduled dalam "
                      f"{timeout}s. Terakhir terbaca: {terakhir}")
        return False

    def _scroll_details_dialog_to_bottom(self, page):
        """Scroll dialog upload ke bawah supaya section yang di-lazy-render
        Studio (Audience, Altered content, dst - di bawah Thumbnail/Playlist)
        benar-benar TER-INSTANTIASI di DOM sebelum dicari. Banyak komponen
        Polymer/LitElement Studio baru membuat elemennya saat discroll ke
        viewport; kalau cuma nunggu tanpa scroll, elemennya bisa jadi memang
        belum ada sama sekali di DOM (bukan cuma soal shadow-DOM)."""
        try:
            page.run_js(r"""
            return (function(){
                function findScrollable(root, out){
                    if(!root) return;
                    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for(const el of all){
                        try{
                            if(el.scrollHeight - el.clientHeight > 40){
                                const style = window.getComputedStyle(el);
                                if(style.overflowY === 'auto' || style.overflowY === 'scroll'){
                                    out.push(el);
                                }
                            }
                        }catch(e){}
                        if(el.shadowRoot) findScrollable(el.shadowRoot, out);
                    }
                }
                const candidates = [];
                findScrollable(document, candidates);
                candidates.forEach(function(el){ el.scrollTop = el.scrollHeight; });
                window.scrollTo(0, document.body.scrollHeight);
                return candidates.length;
            })()
            """)
        except Exception:
            pass

    def _expand_show_more(self, page):
        """Klik "Show more" di dialog Details supaya section yang tersembunyi
        (termasuk 'Altered content') benar-benar ter-render di DOM.

        Idempoten: kalau area sudah terbuka, tombolnya tidak ada dan fungsi ini
        tidak melakukan apa-apa.
        """
        self._scroll_details_dialog_to_bottom(page)
        time.sleep(0.8)
        try:
            res = page.run_js("""
            return (function(){
                function visible(el){
                    var r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    var s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                var target = null;
                function scan(root, d){
                    if (!root || d > 12 || target) return;
                    var sel = 'ytcp-button, button, tp-yt-paper-button, [role="button"]';
                    var els = root.querySelectorAll ? root.querySelectorAll(sel) : [];
                    for (var i=0; i<els.length; i++){
                        var el = els[i];
                        var t = (el.innerText || el.textContent || '').trim().toUpperCase();
                        if ((t === 'SHOW MORE' || t === 'TAMPILKAN LEBIH BANYAK' ||
                             t === 'SELENGKAPNYA' || t === 'LAINNYA') && visible(el)) {
                            target = el; return;
                        }
                    }
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var j=0; j<all.length; j++){
                        if (all[j].shadowRoot) scan(all[j].shadowRoot, d+1);
                    }
                }
                scan(document, 0);
                if (!target) return 'tombol tidak ada (mungkin sudah terbuka)';
                target.scrollIntoView({behavior:'instant', block:'center'});
                target.click();
                return 'diklik';
            })()
            """)
            self.safe_log(f"[DETAILS] Show more: {res}")
        except Exception as e:
            self.safe_log(f"[DETAILS] Show more error: {e}")
        time.sleep(2.0)
        self._scroll_details_dialog_to_bottom(page)
        time.sleep(1.0)

    def _select_radio_by_text(self, page, target_words, other_words, log_tag):
        """Cari tp-yt-paper-radio-button / [role=radio] via DrissionPage (pierce shadow DOM).

        FIX: Versi lama pakai logika 'not any(w in txt for w in other_words)' yang SALAH:
        'MADE FOR KIDS' adalah substring dari 'NOT MADE FOR KIDS', sehingga radio
        'NOT MADE FOR KIDS' selalu di-skip karena other_words match.

        Versi baru pakai LONGEST-MATCH PRIORITY:
        - Hitung panjang best-matching target_word yang ada di txt
        - Hitung panjang best-matching other_word yang ada di txt
        - Pilih radio ini jika best_target > best_other (target lebih spesifik)
        Contoh: txt='NOT MADE FOR KIDS'
          best_target = len('NOT MADE FOR KIDS') = 17
          best_other  = len('MADE FOR KIDS')     = 13  (substring)
          17 > 13 -> KLIK (benar!) OK
        """
        for attempt in range(4):
            try:
                radios = list(page.eles('tag:tp-yt-paper-radio-button', timeout=4) or [])
                try:
                    radios += list(page.eles('@role=radio', timeout=1) or [])
                except Exception:
                    pass
                self.safe_log(f"[{log_tag}] Ditemukan {len(radios)} radio button (percobaan {attempt+1}/4)")
                for r in radios:
                    try:
                        txt = (r.text or '').strip().upper()
                    except Exception:
                        continue
                    if not txt:
                        continue

                    # Longest-match: target harus match lebih panjang dari other
                    best_target = max((len(w) for w in target_words if w in txt), default=0)
                    best_other  = max((len(w) for w in other_words  if w in txt), default=0)

                    if best_target > 0 and best_target > best_other:
                        try:
                            checked = (r.attr('aria-checked') == 'true')
                        except Exception:
                            checked = False
                        self.safe_log(f"[{log_tag}] Radio match: '{txt[:80]}' "
                                      f"(target={best_target}, other={best_other}, checked={checked})")
                        try:
                            r.click()
                            time.sleep(0.5)
                        except Exception:
                            pass
                        self.safe_log(f"[{log_tag}] ✓ Dipilih (percobaan {attempt+1})")
                        return True
                    elif best_target > 0:
                        self.safe_log(f"[{log_tag}] Radio '{txt[:60]}' terdeteksi target={best_target} "
                                      f"tapi other={best_other} — di-skip (other lebih spesifik)")
                self.safe_log(f"[{log_tag}] Opsi tidak ditemukan (percobaan {attempt+1}/4). "
                              f"Teks radio: {[((r.text or '').strip()[:40]) for r in radios[:5]]}")
            except Exception as e:
                self.safe_log(f"[{log_tag}] Error percobaan {attempt+1}: {e}")
            time.sleep(1.5)
        self.safe_log(f"[{log_tag}] Tidak ditemukan setelah 4 percobaan.")
        return False

    def _select_audience(self, page, made_for_kids=False):
        """Pilih Audience = "No, it's not made for kids".

        GAMA SELALU memilih 'Not made for kids'. Parameter dipertahankan supaya
        pemanggil lama tetap jalan, tapi nilai True diabaikan (dan dicatat di log).

        Hasil klik DIVERIFIKASI: radio target harus checked DAN radio
        'Made for kids' harus TIDAK checked. Versi lama menganggap sukses hanya
        karena klik tidak melempar error ("Klik OK tapi checked tidak
        terverifikasi ... return True"), sehingga Audience bisa diam-diam tidak
        terisi dan tombol NEXT menolak lanjut.
        """
        if made_for_kids:
            self.safe_log("[AUDIENCE] Permintaan 'Made for kids' DIABAIKAN — "
                          "GAMA selalu memilih 'Not made for kids'.")
        name_target = 'VIDEO_MADE_FOR_KIDS_NOT_MFK'
        name_wrong  = 'VIDEO_MADE_FOR_KIDS_MFK'
        self.safe_log("[AUDIENCE] Target: NOT MADE FOR KIDS")

        self._scroll_details_dialog_to_bottom(page)
        time.sleep(2.0)

        def _state():
            """(target_checked, wrong_checked) — dibaca menembus shadow DOM.

            CATATAN PENTING soal argumen JS: DrissionPage membungkus script
            jadi BODY sebuah function, jadi `arguments` hanya valid di level
            teratas script. Kalau dibaca DI DALAM IIFE, `arguments` merujuk ke
            argumen IIFE itu sendiri (kosong) → nilainya `undefined`. Karena
            itu argumen dioper eksplisit: `(function(want, other){...})(
            arguments[0], arguments[1])`.
            """
            try:
                res = page.run_js("""
                return (function(want, other){
                    var out = {target:false, wrong:false};
                    function isChecked(el){
                        return !!(el.checked || el.getAttribute('checked') !== null ||
                                  el.getAttribute('aria-checked') === 'true');
                    }
                    function scan(root, depth){
                        if (!root || depth > 10) return;
                        var els = root.querySelectorAll ? root.querySelectorAll('[name]') : [];
                        for (var i=0; i<els.length; i++){
                            var n = els[i].getAttribute('name') || '';
                            if (n === want && isChecked(els[i])) out.target = true;
                            if (n === other && isChecked(els[i])) out.wrong = true;
                        }
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var j=0; j<all.length; j++){
                            if (all[j].shadowRoot) scan(all[j].shadowRoot, depth+1);
                        }
                    }
                    scan(document, 0);
                    return out;
                })(arguments[0], arguments[1])
                """, name_target, name_wrong)
                if isinstance(res, dict):
                    return bool(res.get('target')), bool(res.get('wrong'))
            except Exception as e:
                self.safe_log(f"[AUDIENCE] Baca state radio gagal: {e}")
            return False, False

        def _click_target():
            for sel in (f'@name={name_target}', f'css:[name="{name_target}"]'):
                try:
                    el = page.ele(sel, timeout=4)
                    if not el:
                        continue
                    try:
                        page.run_js("arguments[0].scrollIntoView({behavior:'instant',block:'center'})", el)
                        time.sleep(0.3)
                    except Exception:
                        pass
                    el.click()
                    self.safe_log(f"[AUDIENCE] Radio diklik via '{sel}'")
                    return True
                except Exception as e:
                    self.safe_log(f"[AUDIENCE] sel '{sel}' error: {e}")
            try:
                js_res = page.run_js("""
                return (function(nameTarget){
                    function scan(root, depth) {
                        if (!root || depth > 10) return null;
                        var els = root.querySelectorAll ? root.querySelectorAll('[name="' + nameTarget + '"]') : [];
                        if (els.length > 0) return els[0];
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++) {
                            if (all[i].shadowRoot) {
                                var r = scan(all[i].shadowRoot, depth+1);
                                if (r) return r;
                            }
                        }
                        return null;
                    }
                    var t = scan(document, 0);
                    if (!t) return 'not_found';
                    t.scrollIntoView({behavior:'instant', block:'center'});
                    t.click();
                    return 'clicked';
                })(arguments[0])
                """, name_target)
                if js_res == 'clicked':
                    self.safe_log("[AUDIENCE] Radio diklik via JS.")
                    return True
            except Exception as e:
                self.safe_log(f"[AUDIENCE] Klik via JS error: {e}")
            return False

        # Pemilihan HANYA lewat atribut name yang eksak
        # (VIDEO_MADE_FOR_KIDS_NOT_MFK). Pencocokan berbasis TEKS sengaja tidak
        # dipakai di sini: teks 'MADE FOR KIDS' adalah substring dari
        # 'NOT MADE FOR KIDS', jadi sekali salah tebak yang keklik justru
        # 'Yes, it's made for kids' — persis kondisi yang harus dihindari.
        for attempt in range(1, 4):
            checked, wrong = _state()
            if checked and not wrong:
                self.safe_log(f"[AUDIENCE] ✓ 'Not made for kids' terpilih (cek {attempt}).")
                return True
            if wrong:
                self.safe_log("[AUDIENCE] 'Made for kids' terdeteksi ter-check — dikoreksi.")
            if not _click_target():
                self.safe_log(f"[AUDIENCE] Radio '{name_target}' tidak ditemukan "
                              f"(attempt {attempt}/3).")
            time.sleep(1.0)
            checked, wrong = _state()
            if checked and not wrong:
                self.safe_log(f"[AUDIENCE] ✓ Terverifikasi 'Not made for kids' (attempt {attempt}).")
                return True
            self.safe_log(f"[AUDIENCE] Belum terverifikasi (attempt {attempt}/3): "
                          f"not_mfk={checked} mfk={wrong}")
            self._scroll_details_dialog_to_bottom(page)
            time.sleep(1.5)

        checked, wrong = _state()
        self.safe_log(f"[AUDIENCE] WARNING: gagal memastikan 'Not made for kids' "
                      f"(not_mfk={checked}, mfk={wrong}).")
        return False

    def _select_altered_content_disclosure(self, page, ai_content):
        """Pilih disclosure 'Altered or synthetic content' (Yes/No).

        JANGAN PERNAH mencocokkan teks radio dengan `startswith('YES,')`.
        TERBUKTI LIVE: radio Audience berbunyi "Yes, it's made for kids",
        sehingga pencocokan itu MENGKLIK radio made-for-kids dan membatalkan
        pilihan 'Not made for kids' yang sudah benar sedetik sebelumnya —
        hasil upload jadi "made for kids = yes" padahal log AUDIENCE bilang
        sudah terverifikasi, dan log AI cuma menulis 'Dipilih via text match'.

        Karena itu: radio milik grup made-for-kids DIKECUALIKAN eksplisit, dan
        pencocokan teks harus PERSIS 'Yes'/'No'.
        """
        want_yes = bool(ai_content)
        self.safe_log(f"[AI] Target: {'YES (konten AI)' if want_yes else 'NO (bukan AI)'}")
        self._scroll_details_dialog_to_bottom(page)
        time.sleep(1.0)

        try:
            res = page.run_js("""
            return (function(wantYes){
                function visible(el){
                    var r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    var s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                function isKids(el){
                    var n = (el.getAttribute('name') || '').toUpperCase();
                    var t = (el.innerText || el.textContent || '').toUpperCase();
                    return n.indexOf('MADE_FOR_KIDS') >= 0 || n.indexOf('MFK') >= 0 ||
                           t.indexOf('MADE FOR KIDS') >= 0 || t.indexOf('FOR KIDS') >= 0 ||
                           t.indexOf('UNTUK ANAK') >= 0 || t.indexOf('UNTUK ANAK-ANAK') >= 0;
                }
                var kandidat = [];
                function scan(root, d){
                    if (!root || d > 12) return;
                    var sel = 'tp-yt-paper-radio-button, [role="radio"], input[type="radio"]';
                    var els = root.querySelectorAll ? root.querySelectorAll(sel) : [];
                    for (var i=0; i<els.length; i++){
                        var el = els[i];
                        if (isKids(el)) continue;      // JANGAN SENTUH grup made-for-kids
                        kandidat.push({
                            el: el,
                            name: (el.getAttribute('name')||'').toUpperCase(),
                            val:  (el.getAttribute('value')||'').toUpperCase(),
                            txt:  (el.innerText||el.textContent||'').trim().toUpperCase(),
                            vis:  visible(el)
                        });
                    }
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var j=0; j<all.length; j++){
                        if (all[j].shadowRoot) scan(all[j].shadowRoot, d+1);
                    }
                }
                scan(document, 0);

                var wantTxt = wantYes ? ['YES','YA'] : ['NO','TIDAK'];
                var wantVal = wantYes ? ['TRUE','YES'] : ['FALSE','NO'];
                function pilih(c, cara){
                    try { c.el.scrollIntoView({behavior:'instant', block:'center'}); } catch(e){}
                    c.el.click();
                    return {ok:true, cara:cara, name:c.name, val:c.val, txt:c.txt.slice(0,40)};
                }
                // 1) atribut name/value yang eksplisit soal altered content
                for (var i=0; i<kandidat.length; i++){
                    var c = kandidat[i];
                    if ((c.name.indexOf('ALTERED') >= 0 || c.name.indexOf('SYNTHETIC') >= 0 ||
                         c.name.indexOf('DISCLOSURE') >= 0) && wantVal.indexOf(c.val) >= 0) {
                        return pilih(c, 'atribut');
                    }
                }
                // 2) teks PERSIS 'Yes'/'No' — bukan "Yes, it's made for kids"
                for (var i=0; i<kandidat.length; i++){
                    var c = kandidat[i];
                    if (c.vis && wantTxt.indexOf(c.txt) >= 0) return pilih(c, 'teks-persis');
                }
                return {ok:false, daftar: kandidat.map(function(c){
                    return c.name + '|' + c.val + '|"' + c.txt.slice(0,26) + '"|vis=' + c.vis;
                }).slice(0, 12)};
            })(arguments[0])
            """, bool(want_yes))
        except Exception as e:
            self.safe_log(f"[AI] Error memilih disclosure: {e}")
            return False

        if isinstance(res, dict) and res.get('ok'):
            self.safe_log(f"[AI] ✓ Dipilih via {res.get('cara')}: "
                          f"name={res.get('name')} val={res.get('val')} txt=\"{res.get('txt')}\"")
            return True

        self.safe_log("[AI] Radio Altered content tidak ditemukan. Kandidat non-kids:")
        for d in ((res or {}).get('daftar') or []):
            self.safe_log(f"[AI]   {d}")
        return False

    def _convert_time_24_to_12(self, time_str):
        """Konversi HH:MM (24-jam) ke format 12-jam YouTube Studio, misal '23:00' -> '11:00 PM'."""
        try:
            parts = time_str.strip().split(':')
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            period = 'AM' if h < 12 else 'PM'
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            return f"{h12:02d}:{m:02d} {period}"
        except Exception:
            return time_str

    def _do_schedule_cdp(self, page, date_str, time_str):
        """Jadwalkan video pada step Visibility.

        Alur: klik radio "Schedule" → tunggu picker tanggal/jam muncul → isi
        tanggal & jam → VERIFIKASI nilai yang benar-benar masuk ke picker →
        klik tombol Schedule → tunggu konfirmasi YouTube.

        Tiga metode pengisian dicoba berurutan, masing-masing diverifikasi:
          A. klik koordinat input + ketik via CDP Input.insertText
          B. set .value langsung via JS + dispatch input/change (Polymer)
          C. koordinat ytcp-text-dropdown-trigger (layout Studio lama)
        """
        # ── Bahasa panel jadwal TIDAK selalu Inggris ──────────────────────
        # TERBUKTI DARI LOG USER: walau URL dibuka dengan ?hl=en, panel bisa
        # tampil dalam bahasa akun, mis. Indonesia:
        #     date='9 Agu 2026'   time='11.00'
        # Mengetik 'Aug 10, 2026' ditolak (tanggal tetap default), dan
        # '11:00 PM' masuk sebagai '11.00' — penanda PM HILANG, sehingga
        # video terjadwal pukul 11 PAGI, bukan 11 malam.
        #
        # Karena itu format tulis MENGIKUTI GAYA nilai yang sedang tampil di
        # field, dan verifikasi dilakukan dengan MEM-PARSE nilainya (bukan
        # mencocokkan teks), sehingga bebas dari bahasa maupun format.
        MONTH_EN = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
        MONTH_ID = ('Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun',
                    'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des')
        # Nama bulan -> nomor, mencakup Inggris & Indonesia sekaligus
        NAMA_BULAN = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'mei': 5,
            'jun': 6, 'jul': 7, 'aug': 8, 'agu': 8, 'agt': 8, 'sep': 9,
            'oct': 10, 'okt': 10, 'nov': 11, 'dec': 12, 'des': 12,
        }

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except Exception as e:
            raise Exception(f"[SCHED] Format tanggal tidak valid: '{date_str}' ({e})")
        t_year, t_month, t_day = dt.year, dt.month, dt.day

        try:
            bagian = str(time_str).replace('.', ':').split(':')
            t_hour, t_minute = int(bagian[0]), int(bagian[1]) if len(bagian) > 1 else 0
        except Exception as e:
            raise Exception(f"[SCHED] Format jam tidak valid: '{time_str}' ({e})")

        def _urai_tanggal(val):
            """'Aug 10, 2026' / '10 Agu 2026' -> (tahun, bulan, hari) atau None."""
            v = (val or '').strip().lower()
            if not v:
                return None
            bulan = None
            for nama, nomor in NAMA_BULAN.items():
                if nama in v:
                    bulan = nomor
                    break
            angka = [int(x) for x in re.findall(r'\d+', v)]
            if not bulan or len(angka) < 2:
                return None
            tahun = next((a for a in angka if a >= 1900), None)
            hari = next((a for a in angka if 1 <= a <= 31), None)
            if not tahun or not hari:
                return None
            return (tahun, bulan, hari)

        def _urai_jam(val):
            """'11:00 PM' / '23.00' / '11.00' -> (jam24, menit) atau None."""
            v = (val or '').strip().lower()
            m = re.search(r'(\d{1,2})\s*[:.]\s*(\d{2})', v)
            if not m:
                return None
            jam, menit = int(m.group(1)), int(m.group(2))
            if 'pm' in v and jam < 12:
                jam += 12
            elif 'am' in v and jam == 12:
                jam = 0
            return (jam, menit)

        def _tulis_tanggal(contoh):
            """Format tanggal target mengikuti gaya nilai yang sedang tampil."""
            v = (contoh or '').strip().lower()
            # Gaya Indonesia: hari dulu lalu nama bulan -> '9 Agu 2026'
            gaya_id = bool(re.match(r'^\d{1,2}\s+[a-z]', v)) or \
                any(n in v for n in ('mei', 'agu', 'agt', 'okt', 'des'))
            if gaya_id:
                return f"{t_day} {MONTH_ID[t_month - 1]} {t_year}"
            return f"{MONTH_EN[t_month - 1]} {t_day}, {t_year}"

        def _tulis_jam(contoh):
            """Format jam target mengikuti gaya nilai yang sedang tampil.

            Ini yang mencegah '11:00 PM' berubah jadi '11.00' (jam 11 pagi)
            di field 24 jam — kalau field-nya 24 jam, kita kirim '23.00'.
            """
            v = (contoh or '').strip().lower()
            pemisah = '.' if ('.' in v and ':' not in v) else ':'
            if 'am' in v or 'pm' in v:
                j12 = t_hour % 12 or 12
                ampm = 'AM' if t_hour < 12 else 'PM'
                return f"{j12:02d}{pemisah}{t_minute:02d} {ampm}"
            return f"{t_hour:02d}{pemisah}{t_minute:02d}"

        # Nilai awal (dipakai kalau field masih kosong / gaya belum diketahui)
        date_text = f"{MONTH_EN[t_month - 1]} {t_day}, {t_year}"
        time_text = self._convert_time_24_to_12(time_str)
        self.safe_log(f"[SCHED] Target: {t_day}-{t_month}-{t_year} "
                      f"{t_hour:02d}:{t_minute:02d}")

        # Virtual-key code wajib diisi, kalau tidak Chrome mengabaikan
        # Backspace/Enter (event masuk tapi tidak mengubah isi field).
        _VK = {'Enter': 13, 'Backspace': 8, 'Tab': 9, 'Escape': 27, 'Delete': 46}

        def cdp_click(x, y):
            self._stealth_cdp_click(page, x, y)

        def js(script):
            """Runtime.evaluate — ekspresi murni, TANPA 'return' di awal
            (kalau ada, Chrome melempar SyntaxError dan hasilnya selalu None)."""
            try:
                res = page.run_cdp('Runtime.evaluate',
                                   expression=script,
                                   returnByValue=True,
                                   awaitPromise=False)
                return res.get('result', {}).get('value')
            except Exception:
                return None

        def _cdp_key(key, code=None, modifiers=0):
            code = code or key
            vk = _VK.get(key, 0)
            for ev in ('keyDown', 'keyUp'):
                params = dict(type=ev, key=key, code=code, modifiers=modifiers,
                              windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
                if ev == 'keyDown' and key == 'Enter':
                    params['text'] = '\r'
                try:
                    page.run_cdp('Input.dispatchKeyEvent', **params)
                except Exception:
                    pass
                time.sleep(0.05)

        def _cdp_select_all():
            for ev, key, code, vk, mod in (
                    ('keyDown', 'Control', 'ControlLeft', 17, 2),
                    ('keyDown', 'a', 'KeyA', 65, 2),
                    ('keyUp',   'a', 'KeyA', 65, 2),
                    ('keyUp',   'Control', 'ControlLeft', 17, 0)):
                try:
                    page.run_cdp('Input.dispatchKeyEvent', type=ev, key=key, code=code,
                                 windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk,
                                 modifiers=mod)
                except Exception:
                    pass
                time.sleep(0.05)
            time.sleep(0.1)

        def _cdp_insert(text):
            page.run_cdp('Input.insertText', text=text)
            time.sleep(0.2)

        # ── Pembacaan picker ──────────────────────────────────────────────
        def _picker_fields():
            """(field_tanggal, field_jam) di panel jadwal Studio.

            STRUKTUR SEBENARNYA (terbukti dari dump live, bukan asumsi):
              - TANGGAL = <ytcp-text-dropdown-trigger id="datepicker-trigger">,
                sebuah DROPDOWN/tombol — bukan <input>. Nilainya dibaca dari
                teksnya, dan diubah dengan mengkliknya lalu mengisi kalender
                yang muncul.
              - JAM = SATU-SATUNYA <input> yang terlihat di dalam
                <ytcp-datetime-picker>.

            Versi sebelumnya menganggap input pertama = tanggal, sehingga teks
            tanggal diketik ke kolom JAM: picker berbunyi "Invalid Time" dan
            tanggalnya tidak pernah berubah.

            Tiap field: {x, y, w, value, kind}. kind = 'trigger' | 'input'.
            """
            try:
                res = page.run_js("""
                return (function(){
                    function visible(el){
                        var r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        var s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    function box(el, kind, value){
                        var r = el.getBoundingClientRect();
                        return {x: r.left + r.width/2, y: r.top + r.height/2,
                                w: r.width, value: value || '', kind: kind};
                    }
                    var root = null;
                    function findRoot(r, d){
                        if (!r || d > 12 || root) return;
                        var all = r.querySelectorAll ? r.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++){
                            var el = all[i], tag = (el.tagName||'').toLowerCase();
                            if (tag === 'ytcp-datetime-picker' && visible(el)) { root = el; return; }
                            if (el.shadowRoot) findRoot(el.shadowRoot, d+1);
                        }
                    }
                    findRoot(document, 0);
                    if (!root) return {date: null, time: null};

                    var trigger = null, inputs = [];
                    var stack = [root], guard = 0;
                    while (stack.length && guard < 3000){
                        var n = stack.shift(); if (!n) continue; guard++;
                        var all = n.querySelectorAll ? n.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++){
                            var el = all[i], tag = (el.tagName||'').toLowerCase();
                            if (!trigger && tag === 'ytcp-text-dropdown-trigger' && visible(el)) {
                                trigger = el;
                            }
                            if (tag === 'input' && visible(el)) {
                                var t = (el.type||'').toLowerCase();
                                if (t !== 'file' && t !== 'hidden' && t !== 'checkbox' && t !== 'radio') {
                                    inputs.push(el);
                                }
                            }
                            if (el.shadowRoot) stack.push(el.shadowRoot);
                        }
                    }
                    inputs.sort(function(a,b){
                        var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                        return (ra.top - rb.top) || (ra.left - rb.left);
                    });

                    var out = {date: null, time: null};
                    if (trigger) {
                        out.date = box(trigger, 'trigger',
                                       (trigger.innerText || '').replace(/\\n/g, ' ').trim());
                        if (inputs.length > 0) out.time = box(inputs[0], 'input', inputs[0].value);
                    } else {
                        // Layout lama: dua input berdampingan (tanggal lalu jam)
                        if (inputs.length > 0) out.date = box(inputs[0], 'input', inputs[0].value);
                        if (inputs.length > 1) out.time = box(inputs[1], 'input', inputs[1].value);
                    }
                    return out;
                })()
                """)
            except Exception as e:
                self.safe_log(f"[SCHED] Gagal membaca picker: {e}")
                return None, None
            if not isinstance(res, dict):
                return None, None
            return res.get('date'), res.get('time')

        def _set_date_via_trigger(field, text, exclude=None):
            """Ubah tanggal lewat dropdown: klik trigger -> kalender muncul ->
            isi input di kalender itu -> Enter.

            `exclude` = koordinat field JAM, supaya input kalender tidak
            tertukar dengan input jam yang memang sudah ada di layar.
            """
            if not field:
                return False
            exclude_xy = exclude or {}
            try:
                cdp_click(field['x'], field['y'])
                time.sleep(1.2)
            except Exception as e:
                self.safe_log(f"[SCHED] Klik trigger tanggal gagal: {e}")
                return False

            # Cari input kalender HANYA di dalam wadah popup, dan harus dekat
            # dengan trigger yang barusan diklik.
            #
            # TERBUKTI SAAT UJI: pencarian bebas ke seluruh halaman memilih
            # input di (632,32) — itu kotak "Search across your channel" milik
            # Studio, bukan kalender. Tanggal jadi diketik ke kolom pencarian,
            # tanggal asli tak pernah berubah, dan layar berantakan.
            popup = page.run_js("""
            return (function(excludeX, excludeY, trigX, trigY){
                function visible(el){
                    var r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    var s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                // Wadah yang sah untuk kalender/popup Studio
                var WADAH = ['ytcp-date-picker', 'ytcp-datetime-picker',
                             'tp-yt-paper-dialog', 'iron-dropdown',
                             'tp-yt-iron-dropdown', 'ytcp-dropdown-dialog'];
                var kandidat = [];
                function cari(root, d, dalamWadah){
                    if (!root || d > 12) return;
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i=0; i<all.length; i++){
                        var el = all[i];
                        var tag = (el.tagName||'').toLowerCase();
                        var kini = dalamWadah || (WADAH.indexOf(tag) >= 0);
                        if (kini && tag === 'input'){
                            var t = (el.type||'').toLowerCase();
                            if (t !== 'file' && t !== 'hidden' && t !== 'checkbox' &&
                                t !== 'radio' && visible(el)){
                                var r = el.getBoundingClientRect();
                                var cx = r.left + r.width/2, cy = r.top + r.height/2;
                                var bukanJam = !(Math.abs(cx - excludeX) < 8 &&
                                                 Math.abs(cy - excludeY) < 8);
                                // Kalender selalu muncul dekat trigger-nya.
                                var dekat = Math.abs(cy - trigY) < 320 &&
                                            Math.abs(cx - trigX) < 420;
                                if (bukanJam && dekat){
                                    kandidat.push({x: cx, y: cy, w: r.width,
                                                   value: el.value || '',
                                                   jarak: Math.abs(cy - trigY)});
                                }
                            }
                        }
                        if (el.shadowRoot) cari(el.shadowRoot, d+1, kini);
                    }
                }
                cari(document, 0, false);
                if (!kandidat.length) return null;
                kandidat.sort(function(a,b){ return a.jarak - b.jarak; });
                return kandidat[0];
            })(arguments[0], arguments[1], arguments[2], arguments[3])
            """, float(exclude_xy.get('x', -1)), float(exclude_xy.get('y', -1)),
                 float(field.get('x', 0)), float(field.get('y', 0)))

            if popup and isinstance(popup, dict):
                self.safe_log(f"[SCHED] Input kalender ditemukan "
                              f"({int(popup['x'])},{int(popup['y'])})")
                return _type_at(popup, text, 'tanggal (kalender)')

            # Di build Studio ini kalender berupa GRID tanggal, tanpa kolom
            # ketik. Kalau dibiarkan terbuka, dialog-nya merebut fokus dan
            # membuat metode berikutnya (ketik di trigger) ikut gagal —
            # persis yang terjadi saat uji: fokus nyangkut di
            # <tp-yt-paper-dialog> lalu semua percobaan berikutnya batal.
            # Jadi tutup dulu dengan Escape sebelum menyerah.
            self.safe_log("[SCHED] Kalender tanpa kolom ketik — ditutup, "
                          "lanjut isi lewat trigger.")
            try:
                _cdp_key('Escape')
                time.sleep(0.5)
            except Exception:
                pass
            return False

        def _dump_schedule_panel():
            """Cetak struktur panel jadwal ke log.

            Dipakai saat field jam tidak ketemu: TERBUKTI LIVE bahwa tanggal
            masuk normal tapi `time` selalu kosong, artinya jam di build ini
            bukan <input> biasa. Tanpa dump kita cuma menebak-nebak.
            """
            try:
                dump = page.run_js("""
                return (function(){
                    function visible(el){
                        var r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        var s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    var root = null;
                    function findRoot(r, d){
                        if (!r || d > 12 || root) return;
                        var all = r.querySelectorAll ? r.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++){
                            var el = all[i], tag = (el.tagName||'').toLowerCase();
                            if (tag === 'ytcp-visibility-scheduler' ||
                                tag === 'ytcp-datetime-picker') { root = el; return; }
                            if (el.shadowRoot) findRoot(el.shadowRoot, d+1);
                        }
                    }
                    findRoot(document, 0);
                    if (!root) return {found: false};
                    var out = {found: true, items: []};
                    var stack = [root], guard = 0;
                    while (stack.length && guard < 3000){
                        var n = stack.shift(); if (!n) continue; guard++;
                        var all = n.querySelectorAll ? n.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++){
                            var el = all[i];
                            var tag = (el.tagName||'').toLowerCase();
                            var role = (el.getAttribute && el.getAttribute('role')) || '';
                            var menarik = (tag === 'input' || tag === 'select' ||
                                tag.indexOf('dropdown') >= 0 || tag.indexOf('picker') >= 0 ||
                                tag.indexOf('listbox') >= 0 || tag.indexOf('time') >= 0 ||
                                role === 'combobox' || role === 'listbox' || role === 'button');
                            if (menarik && visible(el)) {
                                var r = el.getBoundingClientRect();
                                out.items.push(tag + ' id=' + (el.id||'-') +
                                    ' role=' + (role||'-') +
                                    ' val=' + JSON.stringify((el.value||'').slice(0,22)) +
                                    ' txt=' + JSON.stringify((el.innerText||'').replace(/\\n/g,' ').trim().slice(0,26)) +
                                    ' @' + Math.round(r.left+r.width/2) + ',' + Math.round(r.top+r.height/2) +
                                    ' w=' + Math.round(r.width));
                            }
                            if (el.shadowRoot) stack.push(el.shadowRoot);
                        }
                    }
                    out.items = out.items.slice(0, 30);
                    return out;
                })()
                """)
            except Exception as e:
                self.safe_log(f"[SCHED] Dump panel jadwal gagal: {e}")
                return
            if not isinstance(dump, dict) or not dump.get('found'):
                self.safe_log("[SCHED] DUMP: panel jadwal tidak ditemukan di DOM.")
                return
            self.safe_log("[SCHED] DUMP isi panel jadwal (elemen terlihat):")
            for it in (dump.get('items') or []):
                self.safe_log(f"[SCHED] DUMP  {it}")

        # Verifikasi dengan MEM-PARSE nilai, bukan mencocokkan teks.
        # Versi lama membandingkan string ('AUG' harus ada di dalam nilai),
        # sehingga panel berbahasa Indonesia ('9 Agu 2026', '11.00') selalu
        # dianggap salah walau isinya sebenarnya sudah benar — dan yang
        # lebih berbahaya, '11.00' (jam 11 pagi) tidak terdeteksi keliru.
        def _date_ok(val):
            return _urai_tanggal(val) == (t_year, t_month, t_day)

        def _time_ok(val):
            return _urai_jam(val) == (t_hour, t_minute)

        def _fields_ok(log=True):
            d, t = _picker_fields()
            dv = (d or {}).get('value', '')
            tv = (t or {}).get('value', '')
            ok_d, ok_t = _date_ok(dv), _time_ok(tv)
            if log:
                self.safe_log(
                    f"[SCHED] Isi picker: date='{dv}' [{'ok' if ok_d else 'X'}] "
                    f"time='{tv}' [{'ok' if ok_t else 'X'}] → "
                    f"{'SESUAI' if (ok_d and ok_t) else 'belum sesuai'}")
            return ok_d and ok_t

        def _fokus_di_input():
            """Nama elemen yang sedang fokus — dipakai memastikan ketikan
            benar-benar masuk ke kolom, bukan ke halaman."""
            try:
                return page.run_js("""
                return (function(){
                    var el = document.activeElement;
                    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
                        el = el.shadowRoot.activeElement;
                    }
                    if (!el) return '';
                    return (el.tagName || '').toUpperCase();
                })()
                """) or ''
            except Exception:
                return ''

        # ── Metode A: klik koordinat + ketik ──────────────────────────────
        def _type_at(field, text, label):
            if not field:
                return False
            try:
                cdp_click(field['x'], field['y'])
                time.sleep(0.6)

                # Ctrl+A HANYA aman kalau fokus sudah di dalam <input>.
                # TERLIHAT DI LAYAR SAAT UJI: kalau klik meleset, fokus tetap
                # di <body> dan Ctrl+A memblok SELURUH HALAMAN (semua teks
                # jadi biru), lalu Backspace/ketikan bisa mendarat entah ke
                # mana. Jadi kalau fokus bukan input, jangan pilih-semua —
                # bersihkan isi kolom lewat triple-click saja.
                fokus = _fokus_di_input()
                if fokus == 'INPUT':
                    _cdp_select_all()
                    _cdp_key('Backspace')
                else:
                    # Fokus di luar <input> BUKAN berarti klik meleset:
                    # trigger tanggal itu dropdown, dan saat terbuka fokus
                    # memang berpindah ke <tp-yt-paper-dialog>. Komponen itu
                    # tetap menerima ketikan, jadi JANGAN dibatalkan —
                    # cukup jangan pilih-semua (itu yang dulu memblok
                    # seluruh halaman jadi biru).
                    self.safe_log(f"[SCHED] Fokus di <{fokus or '?'}> saat isi {label} "
                                  f"— pilih-semua dilewati, ketik langsung.")
                time.sleep(0.15)
                _cdp_insert(text)
                time.sleep(0.5)
                # Enter meng-commit nilai sekaligus menutup popup kalender.
                # JANGAN Tab lalu Enter seperti versi lama: Tab memindah fokus
                # ke kontrol berikutnya, jadi Enter menekan tombol yang salah.
                _cdp_key('Enter')
                time.sleep(0.8)
                self.safe_log(f"[SCHED] Ketik {label}: '{text}'")
                return True
            except Exception as e:
                self.safe_log(f"[SCHED] Ketik {label} gagal: {e}")
                return False

        # ── Metode B: set value langsung via JS ───────────────────────────
        def try_direct_schedule_inputs():
            """Set nilai input picker langsung via JS + dispatch event.

            Dipakai bila klik-koordinat tidak berhasil (input tertutup overlay
            / di luar viewport). Versi lama MEMANGGIL fungsi bernama sama
            padahal tidak pernah mendefinisikannya → NameError yang membatalkan
            seluruh upload begitu jalur ini tersentuh.
            """
            # Baca gaya penulisan yang sedang dipakai field (Inggris/Indonesia,
            # 12/24 jam) supaya nilai yang di-set cocok dengan yang diharapkan
            # komponennya.
            _d_now, _t_now = _picker_fields()
            _now_d = (_d_now or {}).get('value', '')
            _now_t = (_t_now or {}).get('value', '')
            try:
                res = page.run_js("""
                return (function(dateText, timeText){
                    function firstInput(host){
                        var stack = [host];
                        while (stack.length){
                            var node = stack.shift();
                            if (!node) continue;
                            var inputs = node.querySelectorAll ? node.querySelectorAll('input') : [];
                            for (var i=0; i<inputs.length; i++){
                                var t = (inputs[i].type||'').toLowerCase();
                                if (t==='file'||t==='hidden'||t==='checkbox'||t==='radio') continue;
                                return inputs[i];
                            }
                            if (node.shadowRoot) stack.push(node.shadowRoot);
                            var all = node.querySelectorAll ? node.querySelectorAll('*') : [];
                            for (var j=0; j<all.length; j++){
                                if (all[j].shadowRoot) stack.push(all[j].shadowRoot);
                            }
                        }
                        return null;
                    }
                    function setValue(inp, val){
                        // Jangan pernah menulis nilai kosong/undefined ke field
                        // jadwal — lebih baik gagal terang-terangan.
                        if (!inp || typeof val !== 'string' || !val) return false;
                        try {
                            var desc = Object.getOwnPropertyDescriptor(
                                Object.getPrototypeOf(inp), 'value');
                            if (desc && desc.set) desc.set.call(inp, val);
                            else inp.value = val;
                        } catch(e) { inp.value = val; }
                        inp.dispatchEvent(new Event('input', {bubbles:true}));
                        inp.dispatchEvent(new Event('change', {bubbles:true}));
                        try {
                            inp.dispatchEvent(new KeyboardEvent('keydown',
                                {key:'Enter', keyCode:13, which:13, bubbles:true}));
                            inp.dispatchEvent(new KeyboardEvent('keyup',
                                {key:'Enter', keyCode:13, which:13, bubbles:true}));
                        } catch(e) {}
                        inp.dispatchEvent(new Event('blur', {bubbles:true}));
                        return true;
                    }
                    var hosts = {date:null, time:null};
                    function scan(root, depth){
                        if (!root || depth > 10) return;
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++){
                            var el = all[i];
                            var tag = (el.tagName||'').toLowerCase();
                            if (tag === 'ytcp-date-picker' && !hosts.date) hosts.date = el;
                            if (tag === 'ytcp-time-of-day-picker' && !hosts.time) hosts.time = el;
                            if (el.shadowRoot) scan(el.shadowRoot, depth+1);
                        }
                    }
                    scan(document, 0);
                    var okDate = hosts.date ? setValue(firstInput(hosts.date), dateText) : false;
                    var okTime = hosts.time ? setValue(firstInput(hosts.time), timeText) : false;
                    return {date: okDate, time: okTime};
                })(arguments[0], arguments[1])
                """, _tulis_tanggal(_now_d), _tulis_jam(_now_t))
                if isinstance(res, dict) and (res.get('date') or res.get('time')):
                    self.safe_log(f"[SCHED] Set langsung via JS: {res}")
                    time.sleep(1.0)
                    return True
                self.safe_log("[SCHED] Set langsung via JS: input picker tidak ditemukan.")
            except Exception as e:
                self.safe_log(f"[SCHED] Set langsung via JS gagal: {e}")
            return False

        # ── Metode C: koordinat trigger (layout Studio lama) ──────────────
        def _read_schedule_triggers():
            return js("""
            (function(){
                function walkAll(root, out) {
                    var els = root.querySelectorAll('*');
                    for (var el of els) {
                        var tag = (el.tagName||'').toLowerCase();
                        if (tag === 'ytcp-text-dropdown-trigger' || tag === 'ytcp-date-picker' ||
                            tag === 'ytcp-time-of-day-picker' || tag === 'ytcp-datetime-picker') {
                            var r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                out.push({
                                    text:(el.innerText || el.textContent || '').trim(),
                                    x:r.left+r.width/2, y:r.top+r.height/2,
                                    w:r.width, h:r.height, tag:tag
                                });
                            }
                        }
                        if (el.shadowRoot) walkAll(el.shadowRoot, out);
                    }
                }
                var out = [];
                walkAll(document, out);
                out.sort(function(a,b){ return (a.y-b.y) || (a.x-b.x); });
                return out;
            })()
            """)

        def try_keyboard_schedule_inputs():
            """Ketik lewat koordinat trigger dropdown (layout Studio lama)."""
            try:
                triggers = _read_schedule_triggers()
                if not isinstance(triggers, list) or not triggers:
                    self.safe_log("[SCHED] Trigger jadwal tidak ditemukan.")
                    return False

                date_coord = triggers[0]
                # Default: field jam ada di kanan field tanggal.
                time_coord = {'x': date_coord['x'] + 155, 'y': date_coord['y']}
                for tr in triggers[1:]:
                    txt = str(tr.get('text', '')).lower()
                    if 'gmt' in txt or 'local time' in txt or 'waktu' in txt:
                        continue  # itu dropdown zona waktu, bukan jam
                    if abs(tr.get('y', 0) - date_coord.get('y', 0)) <= 20 and \
                            tr.get('x', 0) > date_coord.get('x', 0):
                        time_coord = tr
                        break

                self.safe_log(f"[SCHED] Trigger tanggal ({int(date_coord['x'])},{int(date_coord['y'])}) "
                              f"jam ({int(time_coord['x'])},{int(time_coord['y'])})")
                # Ikut gaya nilai yang sedang tampil di masing-masing trigger
                _type_at(date_coord, _tulis_tanggal(date_coord.get('text')),
                         'tanggal (trigger)')
                _type_at(time_coord, _tulis_jam(time_coord.get('text')),
                         'jam (trigger)')
                return True
            except Exception as e:
                self.safe_log(f"[SCHED] Input via trigger gagal: {e}")
                return False

        # ── Radio "Schedule" ──────────────────────────────────────────────
        def _dump_visibility_panel():
            """Cetak isi panel Visibility ke log saat opsi Schedule tidak ketemu.

            Tanpa ini kita cuma tahu "tidak ditemukan" tanpa tahu SEBENARNYA ada
            apa di sana — dan layout Studio berubah-ubah antar akun/rollout.
            """
            try:
                dump = page.run_js("""
                return (function(){
                    function visible(el){
                        var r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        var s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    var panel = null;
                    function findPanel(root, depth){
                        if (!root || depth > 12 || panel) return;
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++){
                            var el = all[i];
                            if ((el.tagName||'').toLowerCase() === 'ytcp-video-visibility-select') {
                                panel = el; return;
                            }
                            if (el.shadowRoot) findPanel(el.shadowRoot, depth+1);
                        }
                    }
                    findPanel(document, 0);
                    if (!panel) return {panel: false};
                    var out = {panel: true, text: (panel.innerText||'').replace(/\\n+/g, ' / ').slice(0, 400),
                               items: []};
                    var stack = [panel], seen = 0;
                    while (stack.length && seen < 4000){
                        var n = stack.shift(); if (!n) continue; seen++;
                        var all = n.querySelectorAll ? n.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++){
                            var el = all[i];
                            var tag = (el.tagName||'').toLowerCase();
                            var own = '';
                            for (var c = el.firstChild; c; c = c.nextSibling){
                                if (c.nodeType === 3) own += c.nodeValue;
                            }
                            own = own.trim();
                            var idv = (el.id||'') + '|' + (el.getAttribute('name')||'') +
                                      '|' + (el.getAttribute('role')||'');
                            if (own && own.length < 40 && visible(el)) {
                                out.items.push(tag + ' [' + idv + '] "' + own + '"');
                            } else if (/schedule|jadwal/i.test(idv)) {
                                out.items.push(tag + ' [' + idv + '] (vis=' + visible(el) + ')');
                            }
                            if (el.shadowRoot) stack.push(el.shadowRoot);
                        }
                    }
                    out.items = out.items.slice(0, 40);
                    return out;
                })()
                """)
            except Exception as e:
                self.safe_log(f"[SCHED] Dump panel gagal: {e}")
                return
            if not isinstance(dump, dict):
                return
            if not dump.get('panel'):
                self.safe_log("[SCHED] DUMP: ytcp-video-visibility-select tidak ada di DOM.")
                return
            self.safe_log(f"[SCHED] DUMP teks panel: {dump.get('text', '')}")
            for it in (dump.get('items') or []):
                self.safe_log(f"[SCHED] DUMP  {it}")

        # Label tombol kanan-bawah dialog. Tombol yang SAMA (#done-button)
        # berganti teks mengikuti keadaan bagian Schedule — ini inti masalahnya,
        # lihat _buka_bagian_schedule di bawah.
        _LABEL_JADWALKAN = ('schedule', 'jadwalkan')
        _LABEL_SIMPAN = ('save', 'simpan')

        def _status_panel_jadwal():
            """Keadaan bagian Schedule: picker terlihat?, label #done-button,
            dan koordinat tombol expander-nya."""
            return page.run_js("""
            return (function(){
                function vis(el){
                    var r = el.getBoundingClientRect();
                    var s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.display !== 'none' && s.visibility !== 'hidden';
                }
                var out = {picker: false, done: '', expander: null};
                var seen = new Set();
                function jelajah(root, d){
                    if (!root || d > 12) return;
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i = 0; i < all.length; i++){
                        var e = all[i];
                        if (seen.has(e)) continue;
                        seen.add(e);
                        var tag = (e.tagName || '').toLowerCase();
                        if (tag === 'ytcp-datetime-picker' && vis(e)) out.picker = true;
                        if (e.id === 'done-button') out.done = (e.innerText || '').trim();
                        if (/^second-container-expand/.test(e.id || '') && vis(e)){
                            // WAJIB digulir dulu sebelum diukur. Bagian
                            // Schedule ada di bawah panel Visibility dan
                            // koordinatnya sering jatuh di LUAR viewport,
                            // sehingga klik CDP mendarat di ruang kosong dan
                            // panelnya tidak pernah terbuka. Persis kegagalan
                            // yang sama sudah terbukti di uploader TikTok
                            // (tombol terukur y=1140 pada jendela 705px).
                            try { e.scrollIntoView({block: 'center'}); } catch(err) {}
                            var r = e.getBoundingClientRect();
                            out.expander = {x: Math.round(r.left + r.width/2),
                                            y: Math.round(r.top + r.height/2),
                                            dalam_layar: r.top >= 0 &&
                                                         r.bottom <= window.innerHeight};
                        }
                        if (e.shadowRoot) jelajah(e.shadowRoot, d + 1);
                    }
                }
                jelajah(document, 0);
                return out;
            })()
            """)

        def _label_jadwalkan(teks):
            return (teks or '').strip().lower() in _LABEL_JADWALKAN

        def _buka_bagian_schedule(percobaan=3):
            """Buka bagian 'Schedule' lewat tombol expander-nya yang PASTI.

            AKAR MASALAH LAMA (terbukti dari DOM live + log user 10 Agu 14:20):
            panel Visibility punya DUA <p id="visibility-title"> — satu berbunyi
            'Save or publish', satu 'Schedule'. Yang kedua cuma JUDUL section,
            bukan kontrol. Versi lama mengkliknya (plus 3 induknya secara buta)
            lalu mengira Schedule sudah terpilih.

            Yang menentukan: tombol kanan-bawah #done-button BERGANTI TEKS —
                bagian Schedule tertutup -> 'Save'      (menyimpan DRAF privat)
                bagian Schedule terbuka  -> 'Schedule'  (benar-benar menjadwalkan)
            Karena tombolnya diklik tanpa memeriksa teksnya, yang tertekan
            adalah 'Save'. Itulah kenapa 5 video user berakhir jadi Draf,
            padahal log melaporkan "berhasil dijadwalkan".

            Kontrol yang benar: ytcp-icon-button#second-container-expand-button.
            """
            for i in range(percobaan):
                st = _status_panel_jadwal() or {}
                if st.get('picker') and _label_jadwalkan(st.get('done')):
                    self.safe_log(f"[SCHED] Bagian Schedule terbuka "
                                  f"(tombol='{st.get('done')}').")
                    return True
                exp = st.get('expander')
                if not exp:
                    self.safe_log("[SCHED] Tombol expander Schedule tidak ada "
                                  f"(tombol='{st.get('done')}', picker={st.get('picker')}).")
                    return False
                self.safe_log(f"[SCHED] Membuka bagian Schedule via expander "
                              f"({exp['x']},{exp['y']}) dalam_layar={exp.get('dalam_layar')} "
                              f"— percobaan {i+1}/{percobaan}, "
                              f"tombol sekarang '{st.get('done')}'.")
                cdp_click(exp['x'], exp['y'])
                time.sleep(2.5)

                # Cadangan: klik langsung lewat JS. Klik koordinat masih bisa
                # meleset kalau ada elemen lain menimpa titiknya atau panelnya
                # bergeser sesudah digulir.
                lanjut = _status_panel_jadwal() or {}
                if not (lanjut.get('picker') and _label_jadwalkan(lanjut.get('done'))):
                    try:
                        ditekan = page.run_js("""
                        return (function(){
                            var hasil = '';
                            var seen = new Set();
                            function jelajah(root, d){
                                if (hasil || !root || d > 12) return;
                                var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                                for (var i = 0; i < all.length; i++){
                                    var e = all[i];
                                    if (seen.has(e)) continue;
                                    seen.add(e);
                                    if (/^second-container-expand/.test(e.id || '')){
                                        try { e.click(); hasil = e.id; } catch(err) {}
                                        return;
                                    }
                                    if (e.shadowRoot) jelajah(e.shadowRoot, d + 1);
                                }
                            }
                            jelajah(document, 0);
                            return hasil;
                        })()
                        """)
                        if ditekan:
                            self.safe_log(f"[SCHED] Klik cadangan lewat JS pada #{ditekan}.")
                            time.sleep(2.5)
                    except Exception:
                        pass
            st = _status_panel_jadwal() or {}
            self.safe_log(f"[SCHED] Gagal membuka bagian Schedule "
                          f"(tombol='{st.get('done')}', picker={st.get('picker')}).")
            return False

        def _pastikan_premiere_mati():
            """Matikan centang Premiere kalau sempat menyala.

            Menjadwalkan video TIDAK sama dengan menjadikannya Premiere:
            Premiere memutar video serentak dengan ruang obrolan langsung dan
            mengubah cara tayangnya. User tidak pernah memintanya, jadi kalau
            centang itu menyala (mis. karena klik nyasar), dimatikan lagi.
            """
            try:
                hasil = page.run_js("""
                return (function(){
                    function vis(el){
                        var r = el.getBoundingClientRect();
                        var s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                               s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    var dimatikan = [];
                    var seen = new Set();
                    function jelajah(root, d){
                        if (!root || d > 12) return;
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var i = 0; i < all.length; i++){
                            var e = all[i];
                            if (seen.has(e)) continue;
                            seen.add(e);
                            if (/premiere|schedule-type/i.test(e.id || '') && vis(e)){
                                var on = e.getAttribute('aria-checked') === 'true' ||
                                         e.hasAttribute('checked');
                                if (on){ try { e.click(); dimatikan.push(e.id); } catch(err){} }
                            }
                            if (e.shadowRoot) jelajah(e.shadowRoot, d + 1);
                        }
                    }
                    jelajah(document, 0);
                    return dimatikan;
                })()
                """)
                if hasil:
                    self.safe_log(f"[SCHED] Centang Premiere dimatikan: {hasil}")
                    time.sleep(1.5)
            except Exception:
                pass

        def _click_schedule():
            """Pilih opsi 'Schedule' di step Visibility.

            Jalur utama sekarang lewat tombol expander ber-id pasti
            (_buka_bagian_schedule). Pencarian berbasis teks di bawah hanya
            dipakai sebagai cadangan kalau id itu hilang di build Studio lain —
            dan cadangan itu TIDAK lagi dianggap cukup, karena hasilnya selalu
            diverifikasi ulang lewat label #done-button sebelum tombol ditekan.
            """
            if _buka_bagian_schedule():
                return True
            self.safe_log("[SCHED] Expander tidak berhasil, coba cara lama "
                          "(cari elemen berteks 'Schedule')...")
            # Scroll panel Visibility ke bawah supaya opsi Schedule ter-render
            try:
                page.run_js("""
                return (function(){
                    function scan(root, depth) {
                        if (!root || depth > 12) return false;
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var i=0; i<all.length; i++) {
                            var el = all[i];
                            if ((el.tagName||'').toLowerCase() === 'ytcp-video-visibility-select') {
                                el.scrollTop = 9999;
                                el.scrollIntoView({behavior:'instant', block:'center'});
                                return true;
                            }
                            if (el.shadowRoot && scan(el.shadowRoot, depth+1)) return true;
                        }
                        return false;
                    }
                    var r = scan(document, 0);
                    window.scrollTo(0, document.body.scrollHeight);
                    return r;
                })()
                """)
                time.sleep(1.0)
            except Exception:
                pass

            res = page.run_js("""
            return (function(){
                function visible(el){
                    var r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    var s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                function ownText(el){
                    var t = '';
                    for (var c = el.firstChild; c; c = c.nextSibling){
                        if (c.nodeType === 3) t += c.nodeValue;
                    }
                    return t.trim().toUpperCase();
                }
                var hits = [];
                function scan(root, depth){
                    if (!root || depth > 12) return;
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i=0; i<all.length; i++){
                        var el = all[i];
                        var idv = (el.id||'') + ' ' + (el.getAttribute('name')||'') + ' ' +
                                  (el.getAttribute('value')||'');
                        // JANGAN pernah menyentuh kontrol Premiere. Id
                        // 'schedule-type-checkbox' & 'enable-premiere-checkbox'
                        // ikut tertangkap pola /schedule/ dan sekali diklik
                        // video BERUBAH JADI PREMIERE — perubahan besar yang
                        // tidak pernah diminta user. Terbukti saat pengujian:
                        // satu video jadi "Premiere Aug 20, 2026 Scheduled".
                        if (/premiere|schedule-type/i.test(el.id||'')) {
                            if (el.shadowRoot) scan(el.shadowRoot, depth+1);
                            continue;
                        }
                        var byId = /schedule/i.test(idv);
                        var byText = (ownText(el) === 'SCHEDULE' || ownText(el) === 'JADWALKAN');
                        if ((byId || byText) && visible(el)) hits.push({el: el, byId: byId});
                        if (el.shadowRoot) scan(el.shadowRoot, depth+1);
                    }
                }
                scan(document, 0);
                if (!hits.length) return null;
                // Prioritaskan yang cocok lewat id/name (lebih pasti kontrolnya)
                hits.sort(function(a,b){ return (b.byId?1:0) - (a.byId?1:0); });
                var target = hits[0].el;
                var r = target.getBoundingClientRect();
                try { target.scrollIntoView({behavior:'instant', block:'center'}); } catch(e){}
                // Klik node-nya + naik sampai 3 induk: handler Polymer sering
                // dipasang di pembungkus, bukan di node yang memuat teksnya.
                var node = target, path = [];
                for (var k = 0; k < 4 && node; k++){
                    path.push((node.tagName||'').toLowerCase() + (node.id ? '#' + node.id : ''));
                    try { node.click(); } catch(e){}
                    node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
                }
                return {tag: (target.tagName||'').toLowerCase(), id: target.id || '',
                        byId: hits[0].byId, hits: hits.length, path: path.join(' < '),
                        x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
            })()
            """)
            if res and isinstance(res, dict):
                self.safe_log(f"[SCHED] Opsi Schedule diklik: <{res.get('tag')}> "
                              f"id='{res.get('id')}' viaId={res.get('byId')} "
                              f"kandidat={res.get('hits')} rantai={res.get('path')}")
                return True

            self.safe_log("[SCHED] Opsi 'Schedule' tidak ditemukan di panel Visibility.")
            _dump_visibility_panel()
            return False

        def _wait_picker(timeout=15):
            """Tunggu panel tanggal/jam benar-benar TERLIHAT.

            TERBUKTI LIVE di Studio: `ytcp-datetime-picker` SUDAH ada di DOM
            sejak awal step Visibility, tapi dalam keadaan tersembunyi
            (width/height 0) sampai opsi Schedule dipilih. Mengecek sekadar
            "elemennya ada" (versi sebelumnya) SELALU mengembalikan True,
            sehingga engine mengira panel terbuka lalu membuang 4 percobaan
            mengisi field yang tidak kelihatan. Syaratnya harus VISIBLE.
            """
            deadline = time.time() + timeout
            while True:
                try:
                    found = page.run_js("""
                    return (function(){
                        function visible(el){
                            var r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) return false;
                            var s = window.getComputedStyle(el);
                            return s.display !== 'none' && s.visibility !== 'hidden';
                        }
                        function scan(root, depth){
                            if (!root || depth > 12) return false;
                            var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                            for (var i=0; i<all.length; i++){
                                var el = all[i];
                                var tag = (el.tagName||'').toLowerCase();
                                if ((tag === 'ytcp-datetime-picker' || tag === 'ytcp-date-picker')
                                    && visible(el)) return true;
                                if (el.shadowRoot && scan(el.shadowRoot, depth+1)) return true;
                            }
                            return false;
                        }
                        return scan(document, 0);
                    })()
                    """)
                    if found:
                        self.safe_log("[SCHED] Panel tanggal/jam TERLIHAT.")
                        return True
                except Exception:
                    pass
                if time.time() >= deadline:
                    return False
                time.sleep(1)

        # ── Tombol Schedule final ─────────────────────────────────────────
        def click_final_schedule():
            # PENJAGA UTAMA: #done-button dipakai bersama untuk 'Save' dan
            # 'Schedule'. Menekannya saat masih berbunyi 'Save' TIDAK
            # menjadwalkan apa pun — video cuma tersimpan sebagai draf privat
            # (persis yang terjadi pada 5 video user tanggal 10 Agu).
            # Jadi labelnya diperiksa dulu, dan kalau salah, bagian Schedule
            # dibuka ulang sebelum diklik.
            st = _status_panel_jadwal() or {}
            if not _label_jadwalkan(st.get('done')):
                self.safe_log(f"[SCHED] Tombol masih berbunyi '{st.get('done')}' "
                              "— bagian Schedule belum aktif, dibuka ulang.")
                _buka_bagian_schedule()
                st = _status_panel_jadwal() or {}
            if not _label_jadwalkan(st.get('done')):
                raise Exception(
                    "[SCHED] Menolak menekan tombol karena teksnya "
                    f"'{st.get('done')}', bukan 'Schedule'/'Jadwalkan'. "
                    "Menekannya hanya akan menyimpan video sebagai DRAF, "
                    "bukan menjadwalkannya.")
            _pastikan_premiere_mati()
            self.safe_log(f"[SCHED] Tombol siap: '{st.get('done')}'.")

            for sel in ('css:#done-button', '@id=done-button'):
                try:
                    el = page.ele(sel, timeout=2)
                    if el:
                        try:
                            page.run_js("arguments[0].scrollIntoView({behavior:'instant',block:'center'})", el)
                            time.sleep(0.3)
                        except Exception:
                            pass
                        el.click()
                        self.safe_log(f"[SCHED] Tombol Schedule diklik via '{sel}'")
                        return True
                except Exception:
                    pass

            sched_coord = page.run_js("""
            return (function(){
                function walkAll(root, out, depth) {
                    if (!root || depth > 10) return;
                    var els = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i=0; i<els.length; i++) {
                        var el = els[i];
                        var id = (el.id||'').toLowerCase();
                        var tag = (el.tagName||'').toLowerCase();
                        var role = (el.getAttribute('role')||'').toLowerCase();
                        if (id === 'done-button' || id === 'save-button' ||
                            tag === 'button' || tag === 'ytcp-button' ||
                            tag === 'tp-yt-paper-button' || role === 'button') out.push(el);
                        if (el.shadowRoot) walkAll(el.shadowRoot, out, depth+1);
                    }
                }
                var btns = [];
                walkAll(document, btns, 0);
                for (var i=0; i<btns.length; i++) {
                    var b = btns[i];
                    var txt = (b.innerText || b.textContent ||
                               b.getAttribute('aria-label') || '').trim().toUpperCase();
                    var id = (b.id||'').toLowerCase();
                    var disabled = b.disabled || b.hasAttribute('disabled') ||
                                   b.getAttribute('aria-disabled') === 'true';
                    if (disabled) continue;
                    var r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 &&
                        (id === 'done-button' ||
                         txt === 'SCHEDULE' || txt === 'JADWALKAN' ||
                         txt.endsWith(' SCHEDULE') || txt.endsWith(' JADWALKAN'))) {
                        return {x: r.left + r.width/2, y: r.top + r.height/2, txt: txt};
                    }
                }
                return null;
            })()
            """)
            # Versi lama mengembalikan koordinat tebakan (74% x 88% layar) kalau
            # tombol tidak ketemu — klik buta di tengah dialog yang bisa memicu
            # aksi lain. Sekarang: tidak ketemu = gagal, dan itu dilaporkan.
            if sched_coord and isinstance(sched_coord, dict) and 'x' in sched_coord:
                self.safe_log(f"[SCHED] CDP klik Schedule ({int(sched_coord['x'])},"
                              f"{int(sched_coord['y'])}) txt={sched_coord.get('txt')}")
                cdp_click(sched_coord['x'], sched_coord['y'])
                return True
            self.safe_log("[SCHED] Tombol Schedule/Done tidak ditemukan.")
            return False

        def _jawab_dialog_konfirmasi():
            """Jawab dialog konfirmasi yang muncul SESUDAH tombol Schedule.

            Untuk berkas besar, YouTube kadang menampilkan konfirmasi tersendiri
            (mis. 'video masih diproses / masih diunggah, tetap jadwalkan?').
            Dialog itu BUKAN ytcp-uploads-dialog, jadi pemeriksa di bawah
            menganggap dialog utama sudah tutup lalu melaporkan sukses palsu —
            padahal jadwalnya belum tersimpan dan videonya berakhir jadi DRAF.
            """
            try:
                hasil = page.run_js("""
                return (function(){
                    function vis(el){
                        var r = el.getBoundingClientRect();
                        var s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                               s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    // 'SCHEDULE'/'JADWALKAN' SENGAJA tidak masuk daftar: tombol
                    // itu milik dialog upload dan penekanannya diurus khusus
                    // oleh _tuntaskan_schedule. Kalau ikut di sini, urutan
                    // penelusuran DOM bisa membuatnya tertekan LEBIH DULU
                    // daripada peringatan "Got it" yang menutupinya.
                    var OK = ['GOT IT','MENGERTI','CONTINUE','LANJUTKAN','LANJUT',
                              'CONFIRM','KONFIRMASI','OK','YES','YA'];
                    var ditekan = null, seen = new Set();
                    function jelajah(root, d){
                        if (ditekan || !root || d > 10) return;
                        var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (var i = 0; i < all.length; i++){
                            var e = all[i];
                            if (seen.has(e)) continue;
                            seen.add(e);
                            var tag = (e.tagName || '').toLowerCase();
                            if ((tag.indexOf('dialog') >= 0 ||
                                 e.getAttribute('role') === 'dialog') && vis(e)){
                                var judul = (e.innerText || '').replace(/\\s+/g,' ')
                                            .trim().slice(0, 70);
                                var tombol = e.querySelectorAll(
                                    'ytcp-button, tp-yt-paper-button, button');
                                for (var k = 0; k < tombol.length; k++){
                                    var b = tombol[k];
                                    if (!vis(b)) continue;
                                    if (b.hasAttribute('disabled')) continue;
                                    var s = (b.innerText || b.getAttribute('aria-label') || '')
                                            .trim().toUpperCase();
                                    if (OK.indexOf(s) >= 0){
                                        try { b.click(); ditekan = s + ' @ ' + judul; } catch(err){}
                                        break;
                                    }
                                }
                            }
                            if (e.shadowRoot) jelajah(e.shadowRoot, d + 1);
                        }
                    }
                    jelajah(document, 0);
                    return ditekan;
                })()
                """)
                if hasil:
                    self.safe_log(f"[SCHED] Dialog konfirmasi dijawab: {hasil}")
                    time.sleep(2.5)
                    return True
            except Exception:
                pass
            return False

        def _keadaan_dialog_upload():
            """Apakah dialog upload masih terbuka, dan apa label tombol akhirnya."""
            return page.run_js("""
            return (function(){
                function vis(el){
                    var r = el.getBoundingClientRect();
                    var s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.display !== 'none' && s.visibility !== 'hidden';
                }
                var dlg = document.querySelector('ytcp-uploads-dialog') ||
                          document.querySelector('ytcp-video-upload-dialog');
                var done = null, seen = new Set();
                function jelajah(root, d){
                    if (!root || d > 10) return;
                    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (var i = 0; i < all.length; i++){
                        var e = all[i];
                        if (seen.has(e)) continue;
                        seen.add(e);
                        if (e.id === 'done-button' && vis(e))
                            done = (e.innerText || '').trim();
                        if (e.shadowRoot) jelajah(e.shadowRoot, d + 1);
                    }
                }
                jelajah(document, 0);
                return {terbuka: !!(dlg && vis(dlg)), done: done};
            })()
            """)

        def _tuntaskan_schedule(putaran=6):
            """Tuntaskan penjadwalan sesudah tombol Schedule ditekan.

            TERBUKTI DARI LAYAR USER (berkas 764 MB): klik pertama TIDAK
            langsung menjadwalkan. YouTube memunculkan peringatan
            "We're still checking your content" dengan tombol "Got it", dan di
            baliknya tombol "Schedule" MASIH ADA — tanda jadwal belum
            diterapkan. Versi sebelumnya berhenti di sini, mengira sudah
            selesai, lalu browser ditutup; videonya tertinggal sebagai DRAF.

            Jadi: tutup peringatannya, lalu tekan Schedule LAGI, sampai dialog
            uploadnya benar-benar hilang. Video pendek biasanya tidak memicu
            peringatan ini — itu sebabnya uji 13 detik lolos tapi video 1 jam
            gagal.
            """
            for i in range(putaran):
                time.sleep(3)
                st = _keadaan_dialog_upload()
                if not isinstance(st, dict):
                    return False
                if not st.get('terbuka'):
                    self.safe_log(f"[SCHED] Dialog upload sudah tertutup "
                                  f"(putaran {i+1}).")
                    return True
                # Peringatan "still checking" / onboarding harus ditutup dulu,
                # kalau tidak kliknya tidak sampai ke tombol Schedule.
                _jawab_dialog_konfirmasi()
                st = _keadaan_dialog_upload()
                if isinstance(st, dict) and st.get('terbuka') \
                        and _label_jadwalkan(st.get('done')):
                    self.safe_log(f"[SCHED] Tombol '{st.get('done')}' masih ada — "
                                  f"jadwal belum diterapkan, ditekan lagi "
                                  f"(putaran {i+1}/{putaran}).")
                    click_final_schedule()
            self.safe_log("[SCHED] Dialog upload masih terbuka setelah "
                          f"{putaran} putaran.")
            return False

        def wait_for_schedule_completion(timeout=90):
            deadline = time.time() + timeout
            last_state = None
            while time.time() < deadline:
                self._raise_if_stopped()
                _jawab_dialog_konfirmasi()
                try:
                    state = js("""
                    (function(){
                        function visible(el){
                            if(!el) return false;
                            var r = el.getBoundingClientRect();
                            var s = window.getComputedStyle(el);
                            return r.width > 0 && r.height > 0 &&
                                   s.display !== 'none' &&
                                   s.visibility !== 'hidden';
                        }
                        var dialog =
                            document.querySelector('ytcp-uploads-dialog') ||
                            document.querySelector('ytcp-video-upload-dialog');
                        var text = (document.body ? document.body.innerText : '').toUpperCase();
                        return {
                            url: location.href,
                            dialogVisible: visible(dialog),
                            scheduledText: text.includes('VIDEO SCHEDULED') ||
                                           text.includes('TELAH DIJADWALKAN') ||
                                           text.includes('BERHASIL DIJADWALKAN') ||
                                           text.includes('YOUR VIDEO WILL BE SET TO PUBLIC') ||
                                           text.includes('WILL BE SET TO PUBLIC ON') ||
                                           text.includes('AKAN DISETEL KE PUBLIK'),
                            errorText: text.includes('SOMETHING WENT WRONG') ||
                                       text.includes('UPLOAD FAILED') ||
                                       text.includes('UPLOAD GAGAL') ||
                                       text.includes('AN ERROR OCCURRED') ||
                                       text.includes('TERJADI KESALAHAN'),
                            sample: text.slice(-500)
                        };
                    })()
                    """)
                    last_state = state
                    if isinstance(state, dict):
                        url = str(state.get('url') or '').lower()
                        if state.get('scheduledText') and not state.get('errorText'):
                            self.safe_log("[SCHED] Konfirmasi scheduled text ditemukan.")
                            return True
                        # Dialog tutup TANPA teks konfirmasi = sinyal LEMAH.
                        # Dulu ini langsung dianggap sukses, dan itulah yang
                        # menyembunyikan kegagalan: tombol 'Save' tertekan,
                        # dialog tutup, video jadi draf, tapi GAMA melaporkan
                        # "berhasil dijadwalkan". Sekarang tombolnya sudah
                        # dipastikan berbunyi 'Schedule' sebelum ditekan
                        # (lihat click_final_schedule), jadi jalur ini boleh
                        # dipakai — TAPI dicatat apa adanya, bukan disamakan
                        # dengan konfirmasi sungguhan.
                        if not state.get('dialogVisible') and 'studio.youtube.com' in url:
                            self.safe_log(
                                "[SCHED] Dialog tertutup tanpa teks konfirmasi. "
                                "Tombol yang ditekan sudah terverifikasi "
                                "'Schedule', jadi dianggap berhasil — tapi ini "
                                "sinyal lemah, cek daftar video kalau ragu.")
                            return True
                        if state.get('errorText'):
                            raise Exception(
                                "[SCHED] YouTube menampilkan error setelah klik Schedule: "
                                f"{str(state.get('sample') or '')[-180:]}"
                            )
                except Exception as e:
                    if str(e).startswith("[SCHED]"):
                        raise
                time.sleep(2)
            raise Exception(
                "[SCHED] Setelah klik Schedule, YouTube tidak memberi konfirmasi. "
                f"State terakhir: {last_state}"
            )

        # ── Alur utama ────────────────────────────────────────────────────
        self.safe_log("[SCHED] STEP 1 — pilih Schedule lalu isi tanggal/jam...")
        for attempt in range(1, 5):
            _click_schedule()
            if not _wait_picker(15):
                self.safe_log(f"[SCHED] Panel tanggal/jam belum muncul "
                              f"(percobaan {attempt}/4), klik Schedule lagi...")
                time.sleep(2)
                continue

            filled = _fields_ok(log=False)

            # A. ketik di koordinat input picker — HANYA field yang masih salah.
            #    Versi sebelumnya selalu mengetik ulang keduanya; kalau
            #    koordinat jam meleset, teks jam mendarat di field tanggal dan
            #    MERUSAK tanggal yang sudah benar (terbukti live: date berubah
            #    dari 'Aug 10, 2026' jadi '11:00 PM').
            if not filled:
                d_field, t_field = _picker_fields()
                # JAM dulu: field-nya <input> biasa dan posisinya stabil.
                # Tanggal dikerjakan belakangan karena mengkliknya membuka
                # kalender yang bisa menutupi kolom jam.
                if t_field and not _time_ok(t_field.get('value')):
                    # Format ditulis MENGIKUTI gaya nilai yang sedang tampil:
                    # field 24 jam ('11.00') diberi '23.00', bukan '11:00 PM'
                    # yang PM-nya akan hilang dan bikin jadwal meleset 12 jam.
                    jam_teks = _tulis_jam(t_field.get('value'))
                    _type_at(t_field, jam_teks, 'jam')
                elif not t_field:
                    self.safe_log("[SCHED] Kolom jam tidak ditemukan di panel.")

                d_field, t_field = _picker_fields()
                if d_field and not _date_ok(d_field.get('value')):
                    tgl_teks = _tulis_tanggal(d_field.get('value'))
                    if d_field.get('kind') == 'trigger':
                        # Tanggal berupa dropdown -> harus lewat kalender.
                        _set_date_via_trigger(d_field, tgl_teks, exclude=t_field)
                    else:
                        _type_at(d_field, tgl_teks, 'tanggal')
                elif not d_field:
                    self.safe_log("[SCHED] Kolom tanggal tidak ditemukan di panel.")
                filled = _fields_ok()

            # B. set value langsung via JS
            if not filled and try_direct_schedule_inputs():
                filled = _fields_ok()

            # C. koordinat trigger dropdown (layout lama) — hanya dipakai kalau
            #    TANGGAL pun masih salah. Kalau tanggal sudah benar, metode ini
            #    justru menimpanya dengan teks jam.
            if not filled:
                d_now, _ = _picker_fields()
                if not _date_ok((d_now or {}).get('value')):
                    if try_keyboard_schedule_inputs():
                        filled = _fields_ok()
                else:
                    self.safe_log("[SCHED] Tanggal sudah benar, jam belum — metode trigger "
                                  "dilewati supaya tanggal tidak tertimpa.")

            if not filled:
                self.safe_log(f"[SCHED] Tanggal/jam belum masuk (percobaan {attempt}/4).")
                if attempt == 1:
                    _dump_schedule_panel()
                if attempt < 4:
                    time.sleep(3)
                    continue
                raise Exception(
                    f"[SCHED] Gagal mengisi tanggal/jam ({date_text} {time_text}) "
                    "setelah 4 percobaan — layout YouTube Studio mungkin berubah.")

            self.safe_log("[SCHED] Tanggal/jam terverifikasi, klik tombol Schedule...")
            if not click_final_schedule():
                raise Exception("[SCHED] Tombol Schedule tidak ditemukan di dialog.")
            _tuntaskan_schedule()
            wait_for_schedule_completion()
            self.safe_log("[SCHED] Video berhasil dijadwalkan!")
            self._human_delay(3.0, 5.0)
            return True

        raise Exception("[SCHED] Panel tanggal/jam tidak pernah muncul setelah 4 percobaan "
                        "— radio 'Schedule' kemungkinan tidak terpilih.")
