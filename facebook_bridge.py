"""
facebook_bridge.py â€” Worker upload Facebook untuk GAM (Reels personal & Fanpage + Foto).

Dijalankan sebagai subprocess:
    python facebook_bridge.py <job_json_path>
    python facebook_bridge.py --selftest

Berbeda dari YouTube/TikTok, Facebook TIDAK punya engine jadi, jadi otomasi
ditulis sendiri memakai DrissionPage yang menyetir profil Chrome GAM
(data/google_profiles/<email>) â€” profil harus SUDAH login Facebook
(buka shortcut Facebook di kartu profil lalu login).

Mode job:
  - target 'personal' : upload Reels/Foto ke akun pribadi.
  - target 'page'     : upload Reels/Foto ke Fanpage (butuh page_id/url).
  - mode 'list_pages' : deteksi daftar Fanpage yang dikelola akun.

Selector Facebook sering berubah â€” memakai banyak fallback + logging. Untuk
upload sungguhan pertama kali disarankan mode terlihat (non-headless).
"""
import os
import sys
import json
import re
import time
import argparse


def _base_dir():
    """Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CHROME_PATHS = [
    os.path.join(_base_dir(), "assets", "chrome", "chrome.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]

REELS_CREATE_URL = "https://www.facebook.com/reels/create"
PHOTO_CREATE_URL = "https://www.facebook.com/"
PAGE_PHOTO_URL = "https://www.facebook.com/{pid}"
PAGE_POST_URL = "https://www.facebook.com/{pid}"
GROUP_URL = "https://www.facebook.com/groups/{gid}"
GROUP_DISCOVER_URL = "https://www.facebook.com/groups/discover/"
GROUP_FEED_URL = "https://www.facebook.com/groups/{gid}/?sorting_setting=CHRONOLOGICAL"

# Meta Business Suite â€” Content Scheduler (mendukung jadwal FB & IG)
META_SUITE_SCHEDULER_FB = "https://business.facebook.com/latest/content_scheduler/create/"
META_SUITE_SCHEDULER_IG = "https://business.facebook.com/latest/content_scheduler/create/?channel=instagram"

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}


def _is_image(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in IMAGE_EXTS


def _detect_video_codec(path):
    try:
        import subprocess
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            os.path.abspath(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode == 0:
            return (res.stdout or '').strip().lower()
    except Exception:
        pass
    return ''


def _prepare_facebook_video(video_path, log):
    codec = _detect_video_codec(video_path)
    if codec not in {'hevc', 'h265', 'x265'}:
        return video_path, False
    try:
        import hashlib
        import subprocess
        import tempfile
        src = os.path.abspath(video_path)
        digest = hashlib.md5(src.encode('utf-8')).hexdigest()[:10]
        base = os.path.splitext(os.path.basename(src))[0]
        out_path = os.path.join(tempfile.gettempdir(), f'gama_fb_{base}_{digest}_h264.mp4')
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            log(18, 'Memakai cache video H.264 untuk Facebook...')
            return out_path, True
        log(18, 'Mengonversi video ke H.264/AAC agar kompatibel dengan Facebook...')
        cmd = [
            'ffmpeg', '-y', '-i', src,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            out_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if res.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return out_path, True
        tail = (res.stderr or res.stdout or '').strip().splitlines()
        tail_msg = tail[-1] if tail else 'ffmpeg gagal tanpa detail.'
        log(18, f'Konversi H.264 gagal, lanjut pakai file asli: {tail_msg[:120]}')
    except Exception as e:
        log(18, f'Konversi H.264 gagal, lanjut pakai file asli: {e}')
    return video_path, False


def get_chrome_path():
    env = os.environ.get("GAMA_BROWSER")
    if env and os.path.isfile(env):
        return env
    for p in CHROME_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def _port_for(profile_path):
    return 9500 + (abs(hash(profile_path)) % 400)


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
try {
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
} catch (e) {}
"""


def _connect(profile_path, headless=False):
    """Luncurkan Chrome pada profil GAM & kembalikan ChromiumPage."""
    from DrissionPage import ChromiumPage, ChromiumOptions
    chrome = get_chrome_path()
    if not chrome:
        raise RuntimeError("Google Chrome tidak ditemukan.")
    co = ChromiumOptions()
    co.set_browser_path(chrome)
    co.set_user_data_path(os.path.abspath(profile_path))
    co.set_local_port(_port_for(profile_path))
    # CATATAN: --disable-blink-features=AutomationControlled SENGAJA tidak dipakai lagi --
    # sejak Chrome versi baru, flag ini justru memicu infobar kuning "You are using an
    # unsupported command-line flag" yang tampil di SETIAP halaman, jadi malah jadi
    # penanda otomasi yang mencolok (lihat/ketahuan situs & manusia). navigator.webdriver
    # tetap dipatch, tapi lewat JS injeksi CDP (add_init_js) yang tidak meninggalkan jejak
    # di UI Chrome.
    # --lang=en-US: standarkan UI Facebook ke Inggris utk semua profil (disepakati 26 Jul --
    # label/aria-label Facebook versi Indonesia sering beda format antar level menu, bikin
    # selector switch-Fanpage gampang meleset; Inggris jauh lebih stabil & predictable).
    # CATATAN: ini cuma pengaruh ke akun yang BELUM pernah eksplisit set bahasa di FB
    # Settings -- kalau akun sudah pernah diset manual ke Indonesia, itu override server-side
    # dan --lang= flag ini tidak berlaku; perlu _ensure_english_language() (lihat di bawah).
    for arg in ('--no-first-run', '--no-default-browser-check',
                '--disable-notifications', '--lang=en-US'):
        co.set_argument(arg)
    if headless:
        co.headless(True)
    page = ChromiumPage(co)
    try:
        page.add_init_js(_STEALTH_JS)
    except Exception:
        pass
    return page


def _is_logged_in(page):
    """True jika profil sudah login Facebook."""
    try:
        page.get('https://www.facebook.com/me', timeout=25)
    except Exception:
        pass
    time.sleep(3)
    url = (page.url or '').lower()
    if 'login' in url or 'checkpoint' in url:
        return False
    try:
        return bool(page.run_js("""
            return !!(document.querySelector('[aria-label=\"Your profile\"]') ||
                      document.querySelector('[aria-label=\"Akun Anda\"]') ||
                      document.querySelector('div[role=\"navigation\"]') ||
                      document.querySelector('[aria-label*=\"Create a post\" i]'));
        """))
    except Exception:
        return 'login' not in url


def _find_file_input(page, timeout=25, prefer='any'):
    end = time.time() + timeout
    while time.time() < end:
        selectors = []
        if prefer == 'video':
            selectors.extend((
                'xpath://input[@type="file" and contains(translate(@accept,"VIDEO","video"),"video")]',
                'xpath://input[@type="file" and contains(translate(@accept,"MP4MOVWEBM","mp4movwebm"),"mp4")]',
            ))
        elif prefer == 'image':
            selectors.extend((
                'xpath://input[@type="file" and contains(translate(@accept,"IMAGE","image"),"image")]',
                'xpath://input[@type="file" and contains(translate(@accept,"JPGPNGGIFWEBP","jpgpnggifwebp"),"jpg")]',
            ))
        selectors.extend(('tag:input@type=file', 'xpath://input[@type="file"]'))
        for sel in selectors:
            try:
                el = page.ele(sel, timeout=2)
                if el:
                    return el
            except Exception:
                pass
        try:
            best_idx = page.run_js(f"""
                const prefer = {prefer!r};
                const all = Array.from(document.querySelectorAll('input[type=file]'));
                if (!all.length) return -1;
                function scoreInput(el) {{
                    const accept = (el.getAttribute('accept') || '').toLowerCase();
                    const txt = ((((el.closest('[role="dialog"]') || el.parentElement || el).innerText) || '') + ' ' +
                                 ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('name') || ''))).toLowerCase();
                    const rect = el.getBoundingClientRect();
                    let score = 0;
                    if (rect.width > 0 && rect.height > 0) score += 4;
                    if (el.closest('[role="dialog"]')) score += 5;
                    if (txt.includes('add photo/video') || txt.includes('photo/video') || txt.includes('media')) score += 3;
                    if (prefer === 'video') {{
                        if (accept.includes('video')) score += 20;
                        if (accept.includes('image') && !accept.includes('video')) score -= 20;
                        if (accept.includes('mp4') || accept.includes('mov') || accept.includes('webm')) score += 12;
                        if (txt.includes('video') || txt.includes('reel')) score += 6;
                    }} else if (prefer === 'image') {{
                        if (accept.includes('image')) score += 20;
                        if (accept.includes('video') && !accept.includes('image')) score -= 20;
                        if (accept.includes('jpg') || accept.includes('png') || accept.includes('webp')) score += 12;
                        if (txt.includes('photo') || txt.includes('image') || txt.includes('foto')) score += 6;
                    }} else {{
                        if (accept) score += 3;
                    }}
                    return score;
                }}
                let best = {{ idx: 0, score: -999 }};
                all.forEach((el, idx) => {{
                    const score = scoreInput(el);
                    if (score > best.score) best = {{ idx, score }};
                }});
                return best.score <= -999 ? -1 : best.idx;
            """)
            if isinstance(best_idx, int) and best_idx >= 0:
                try:
                    el = page.ele(f'xpath:(//input[@type="file"])[{best_idx + 1}]', timeout=2)
                    if el:
                        return el
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(1)
    return None


def _click_by_text(page, texts, timeout=12):
    """Klik tombol berdasarkan aria-label / teks (dengan fallback)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            clicked = page.run_js(r"""
                const wanted = arguments[0].map(t => t.toLowerCase());
                const nodes = document.querySelectorAll(
                    'div[role="button"],button,a[role="button"],div[aria-label],span[role="button"],a,div,span');
                for (const n of nodes) {
                    const style = window.getComputedStyle ? window.getComputedStyle(n) : null;
                    if (style && (style.pointerEvents === 'none' || style.visibility === 'hidden')) continue;
                    const lbl = ((n.getAttribute('aria-label')||'') + ' ' + (n.getAttribute('title')||'') + ' ' + (n.innerText||''))
                                .replace(/\s+/g, ' ').trim().toLowerCase();
                    if (!lbl || lbl.length < 2) continue;
                    for (const w of wanted) {
                        if (lbl === w || lbl.startsWith(w) || lbl.includes(w)) {
                            const host = n.closest('button,[role="button"],a,div,span') || n;
                            if (host && host.offsetParent !== null) { host.click(); return lbl; }
                        }
                    }
                }
                return '';
            """, texts if isinstance(texts, list) else [texts])
            if clicked:
                return clicked
        except Exception:
            pass
        time.sleep(1)
    return ''


def _norm_text(val):
    return re.sub(r'\s+', ' ', str(val or '').strip()).lower()


def _debug_dump(page, tag):
    try:
        dump_dir = os.path.join(_base_dir(), 'data', 'debug_fb')
        os.makedirs(dump_dir, exist_ok=True)
        safe_tag = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(tag or 'debug')).strip('_') or 'debug'
        stamp = time.strftime('%Y%m%d_%H%M%S')
        base = os.path.join(dump_dir, f'{stamp}_{safe_tag}')
        try:
            page.get_screenshot(path=dump_dir, name=f'{stamp}_{safe_tag}.png', full_page=True)
        except Exception:
            pass
        try:
            html = page.run_js("return document.documentElement ? document.documentElement.outerHTML : ''; ") or ''
            with open(base + '.html', 'w', encoding='utf-8') as fh:
                fh.write(html)
        except Exception:
            pass
        try:
            body = page.run_js("return document.body ? (document.body.innerText || '') : ''; ") or ''
            with open(base + '.txt', 'w', encoding='utf-8') as fh:
                fh.write(f'URL: {page.url or ""}\n\n{body}')
        except Exception:
            pass
    except Exception:
        pass


def _fill_caption(page, caption):
    if not caption:
        return
    try:
        box = None
        for sel in ('xpath://div[@contenteditable="true" and @role="textbox"]',
                    'xpath://div[@contenteditable="true"]',
                    'tag:div@role=textbox'):
            box = page.ele(sel, timeout=3)
            if box:
                break
        if box:
            box.click()
            time.sleep(0.5)
            # Clear konten lama sebelum isi caption baru
            try:
                box.run_js("""
                    this.focus();
                    if (this.isContentEditable) {
                        document.execCommand('selectAll', false, null);
                        document.execCommand('delete', false, null);
                    } else {
                        this.value = '';
                        this.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                """)
                time.sleep(0.2)
            except Exception:
                pass
            try:
                box.input(caption, clear=True)
            except Exception:
                try:
                    page.actions.key('ctrl+a')
                    time.sleep(0.1)
                    page.actions.type(caption)
                except Exception:
                    page.actions.type(caption)
    except Exception:
        pass


def _fill_bulk_upload_caption(page, caption):
    if not caption:
        return False
    try:
        # Cari textbox yang visible
        el = None
        for sel in (
            'xpath://div[@role="textbox"]',
            'xpath://*[@contenteditable="true"]',
            'tag:textarea',
        ):
            try:
                candidate = page.ele(sel, timeout=3)
                if candidate:
                    el = candidate
                    break
            except Exception:
                pass

        if not el:
            return False

        # Klik dulu untuk fokus
        try:
            el.click()
        except Exception:
            pass
        time.sleep(0.5)

        # Pilih semua lalu hapus konten lama
        try:
            el.run_js("""
                this.focus();
                if (this.isContentEditable) {
                    const range = document.createRange();
                    range.selectNodeContents(this);
                    window.getSelection().removeAllRanges();
                    window.getSelection().addRange(range);
                }
            """)
        except Exception:
            pass
        time.sleep(0.3)

        # Input via DrissionPage (trigger React synthetic events dengan benar)
        try:
            el.input(caption, clear=True)
            time.sleep(0.5)
            current = (el.run_js("return this.innerText || this.value || '';") or '').strip()
            if caption.strip() in current or current in caption.strip():
                return True
        except Exception:
            pass

        # Fallback: ketik via keyboard actions (paling reliable untuk React)
        try:
            el.click()
            time.sleep(0.3)
            # Ctrl+A untuk select all, lalu ketik
            el.run_js("""
                this.focus();
                document.execCommand('selectAll', false, null);
            """)
            time.sleep(0.2)
            page.actions.key('ctrl+a')
            time.sleep(0.2)
            page.actions.type(caption)
            time.sleep(0.5)
            current = (el.run_js("return this.innerText || this.value || '';") or '').strip()
            return bool(current and (caption.strip() in current or current in caption.strip()))
        except Exception:
            pass

        return False
    except Exception:
        return False
_PAGE_SCRAPE_JS = r"""
    const BAD_NAME = /^(pesan|message|messenger|inbox|notification|notifikasi|keterangan foto tidak tersedia\.?|lihat semua|see all|beranda|home)$/i;
    const BAD_TEXT = /(keterangan foto tidak tersedia|photo unavailable|message|messenger|inbox|notification|notifikasi|create|buat|meta|business|ads manager|events manager|help|bantuan|settings?|pengaturan)/i;
    const out = [];
    const seen = new Set();
    const anchors = Array.from(document.querySelectorAll('a[href*="asset_id="]'));
    for (const a of anchors) {
        const href = a.href || '';
        const hit = href.match(/[?&]asset_id=(\d+)/);
        if (!hit) continue;
        const id = hit[1];
        let name = '';
        const lines = (a.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        if (lines.length) name = lines[0];
        if (!name) {
            const img = a.querySelector('img[alt]');
            if (img) name = (img.getAttribute('alt') || '').trim();
        }
        if (!name) {
            const host = a.closest('[role="option"],[role="menuitem"],[role="listitem"],li,div');
            if (host) {
                const nearby = (host.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                name = nearby.find(t => t && t.length > 2 && !/^\d+$/.test(t)) || '';
            }
        }
        name = name.replace(/\s+/g, ' ').trim();
        if (!name || name.length < 2 || name.length > 80) continue;
        if (BAD_NAME.test(name) || BAD_TEXT.test(name)) continue;
        if (!/[A-Za-z]/.test(name)) continue;
        const key = id.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ id, name });
    }
    return out.slice(0, 30);
"""


def _scroll_page(page, rounds=3):
    for _ in range(rounds):
        try:
            page.run_js("window.scrollTo(0, document.body.scrollHeight || 0);")
        except Exception:
            pass
        time.sleep(1.2)


_PAGE_SCRAPE_FALLBACK_JS = r"""
    const REJECT = /^(pesan|message|messenger|inbox|notification|notifikasi|keterangan foto tidak tersedia\.?|lihat semua|see all|beranda|home)$/i;
    const BAD = /(keterangan foto tidak tersedia|photo unavailable|message|messenger|inbox|notification|notifikasi|meta|business|create|buat|help|bantuan|settings?|pengaturan)/i;
    const scope = document.querySelector('[role="main"]') || document.body;
    const out = []; const seen = new Set();
    scope.querySelectorAll('a[href]').forEach(a => {
        if (a.closest('[role="banner"],[role="navigation"],[aria-label*="notification" i],[aria-label*="notifikasi" i],[aria-label*="menu" i]')) return;
        const href = a.href || '';
        let id = '';
        let m = href.match(/\/profile\.php\?id=(\d+)/);
        if (m) id = m[1];
        if (!id) {
            m = href.match(/facebook\.com\/([A-Za-z0-9.][A-Za-z0-9.\-]{2,})\/?($|\?|\/\?)/);
            if (m) id = m[1];
        }
        if (!id) return;
        let name = (a.innerText || '').trim().split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
        if (!name) {
            const img = a.querySelector('img[alt]');
            if (img) name = (img.getAttribute('alt') || '').trim();
        }
        name = name.replace(/\s+/g, ' ').trim();
        if (!name || name.length < 2 || name.length > 80) return;
        if (REJECT.test(name) || BAD.test(name)) return;
        if (!/[A-Za-z]/.test(name)) return;
        const key = id.toLowerCase();
        if (seen.has(key)) return;
        seen.add(key);
        out.push({ id, name });
    });
    return out.slice(0, 40);
"""

def _clean_page_candidates(items):
    seen = set()
    clean = []
    reject_exact = {
        'pesan', 'message', 'messenger', 'inbox', 'notification', 'notifikasi',
        'keterangan foto tidak tersedia.', 'keterangan foto tidak tersedia'
    }
    for item in items or []:
        pid = str(item.get('id') or '').strip()
        name = ' '.join(str(item.get('name') or '').strip().split())
        if not pid or not name:
            continue
        if not re.fullmatch(r'\d+', pid):
            continue
        if name.lower() in reject_exact:
            continue
        if 'keterangan foto tidak tersedia' in name.lower():
            continue
        if not re.search(r'[A-Za-z]', name):
            continue
        key = pid.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append({'id': pid, 'name': name})
    return clean


def _open_page_switcher(page):
    for texts in (
        ['switch page', 'pilih halaman', 'pilih page', 'see all pages', 'lihat semua halaman'],
        ['all pages', 'halaman'],
    ):
        clicked = _click_by_text(page, texts, timeout=4)
        if clicked:
            time.sleep(2)
            return True
    return False


def _list_pages(page):
    """Deteksi Fanpage yang dikelola akun ini.

    Urutan sengaja memprioritaskan halaman Facebook biasa lebih dulu karena
    banyak akun mentok atau diarahkan ke onboarding saat masuk Business Suite.
    Meta Business tetap dipakai sebagai fallback terakhir bila halaman biasa
    belum menghasilkan page apa pun.
    """
    merged = []
    seen = set()

    def add_items(items):
        for item in _clean_page_candidates(items):
            key = item['id'].lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

    fallback_urls = (
        'https://www.facebook.com/pages/?category=your_pages',
        'https://www.facebook.com/pages/',
        'https://www.facebook.com/bookmarks/pages',
        'https://www.facebook.com/bookmarks/?ref=bookmarks',
    )
    for url in fallback_urls:
        try:
            page.get(url, timeout=20)
            time.sleep(3)
            try:
                page.run_js("document.body && document.body.click();")
            except Exception:
                pass
            _scroll_page(page, rounds=4)
            add_items(page.run_js(_PAGE_SCRAPE_FALLBACK_JS) or [])
            if merged:
                return merged
        except Exception:
            continue

    business_urls = (
        'https://business.facebook.com/latest/home',
        'https://business.facebook.com/latest/posts/published_posts',
        'https://business.facebook.com/latest/content_scheduler/create/',
    )
    for url in business_urls:
        try:
            page.get(url, timeout=15)
            time.sleep(4)
            _open_page_switcher(page)
            _scroll_page(page, rounds=3)
            add_items(page.run_js(_PAGE_SCRAPE_JS) or [])
            if merged:
                return merged
        except Exception:
            continue

    return merged


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


def _open_composer(page, target, page_id, log, is_photo=False):
    if is_photo:
        if target == 'page' and page_id:
            log(25, f'Membuka halaman Fanpage untuk foto ({page_id})...')
            url = PAGE_PHOTO_URL.format(pid=page_id)
        else:
            log(25, 'Membuka Facebook untuk upload foto...')
            url = PHOTO_CREATE_URL
        page.get(url, timeout=30)
        time.sleep(5)
        _click_by_text(page, ['photo', 'foto', "what's on your mind", 'apa yang anda pikirkan',
                               'apa yang ada di pikiranmu', 'buat postingan'], timeout=8)
        time.sleep(2)
        _click_by_text(page, ['photo/video', 'foto/video', 'photo', 'foto', 'add photo', 'tambahkan foto'], timeout=8)
        time.sleep(2)
    else:
        log(25, 'Membuka composer Reels...')
        page.get(REELS_CREATE_URL, timeout=30)
        time.sleep(5)


def _current_asset_id(page):
    try:
        m = re.search(r'[?&]asset_id=(\d+)', str(page.url or ''))
        return m.group(1) if m else ''
    except Exception:
        return ''


def _active_facebook_actor(page):
    try:
        return page.run_js(r"""
            const out = { id:'', name:'', href: location.href || '' };
            try {
                if (typeof require === 'function') {
                    const data = require('CurrentUserInitialData');
                    if (data) {
                        out.id = String(data.USER_ID || data.ACCOUNT_ID || '');
                        out.name = String(data.NAME || data.SHORT_NAME || '');
                    }
                }
            } catch (e) {}
            if (!out.id || !out.name) {
                const html = document.documentElement ? document.documentElement.outerHTML : '';
                if (!out.id) {
                    const hit = html.match(/"USER_ID":"(\d+)"/) || html.match(/__user=(\d+)/);
                    if (hit) out.id = hit[1];
                }
                if (!out.name) {
                    const hit = html.match(/"NAME":"([^"]+)"/);
                    if (hit) out.name = hit[1];
                }
            }
            return out;
        """) or {}
    except Exception:
        return {}


def _active_actor_matches(page, page_id, page_name=''):
    want_id = str(page_id or '').strip()
    want_name = _norm_text(page_name)
    actor = _active_facebook_actor(page)
    actor_id = str(actor.get('id') or '').strip()
    actor_name = _norm_text(actor.get('name') or '')
    if actor_id and want_id and actor_id == want_id:
        return True
    if want_name and actor_name and actor_name == want_name:
        return True
    return False


def _target_lock_info(page, page_id, page_name=''):
    actor = _active_facebook_actor(page)
    return {
        'asset_id': _current_asset_id(page),
        'actor_id': str(actor.get('id') or '').strip(),
        'actor_name': str(actor.get('name') or '').strip(),
        'href': str(actor.get('href') or page.url or ''),
        'want_id': str(page_id or '').strip(),
        'want_name': str(page_name or '').strip(),
    }


def _format_target_lock_error(info):
    active = (info.get('actor_name') or info.get('actor_id') or '?')
    want = (info.get('want_name') or info.get('want_id') or '?')
    asset = info.get('asset_id') or '?'
    return f'Target Fanpage tidak terkunci. Aktif: {active}, asset_id URL: {asset}, pilihan: {want}.'


def _target_lock_ok(page, page_id, page_name=''):
    want = str(page_id or '').strip()
    if not want:
        return False
    asset_id = _current_asset_id(page)
    actor = _active_facebook_actor(page)
    actor_id = str(actor.get('id') or '').strip()
    actor_name = _norm_text(actor.get('name') or '')
    want_name = _norm_text(page_name)
    actor_ok = bool((actor_id and actor_id == want) or (want_name and actor_name and actor_name == want_name))
    if actor_ok:
        return not asset_id or asset_id == want
    if asset_id and asset_id != want:
        return False
    if _page_surface_matches(page, want, page_name):
        return True
    if actor_id or actor_name:
        return False
    return bool(asset_id and asset_id == want)

def _page_surface_matches(page, page_id, page_name=''):
    want_id = str(page_id or '').strip().lower()
    want_name = _norm_text(page_name)
    try:
        info = page.run_js(r"""
            const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || n.getAttribute('title') || '')) || '')
                .replace(/\s+/g, ' ').trim();
            const href = (location.href || '').toLowerCase();
            const title = (document.title || '').toLowerCase();
            const main = document.querySelector('[role="main"]') || document.body;
            const body = textOf(main).slice(0, 8000).toLowerCase();
            return { href, title, body };
        """) or {}
    except Exception:
        info = {}
    hay = ' '.join([str(info.get('href') or ''), str(info.get('title') or ''), str(info.get('body') or '')])
    if want_id and want_id in hay:
        return True
    if want_name and want_name in _norm_text(hay):
        return True
    return False


def _open_target_page_surface(page, page_id, page_name, log):
    want_id = str(page_id or '').strip()
    if not want_id:
        return False
    if _page_surface_matches(page, want_id, page_name):
        log(18, f'Halaman Fanpage {page_name or want_id} sudah aktif, lanjut tanpa switch.')
        return True

    for url in (PAGE_POST_URL.format(pid=want_id),):
        try:
            log(18, f'Membuka halaman Fanpage target {page_name or want_id}...')
            page.get(url, timeout=30)
            time.sleep(5)
            if _page_surface_matches(page, want_id, page_name):
                return True
        except Exception:
            pass
    return False


def _asset_matches(page, page_id, page_name=''):
    want = str(page_id or '').strip()
    return bool(want) and _target_lock_ok(page, want, page_name)


def _target_context_ok(page, page_id, page_name='', trust_context=False):
    if _asset_matches(page, page_id, page_name):
        return True
    asset_id = _current_asset_id(page)
    if asset_id and asset_id != str(page_id or '').strip():
        return False
    if not trust_context or asset_id:
        return False
    return True


def _try_switch_dp_profile(page, page_id='', page_name='', log=None):
    """Beralih profil/actor Facebook ke Fanpage target (DP)."""
    want_id = str(page_id or '').strip()
    want_name = _norm_text(page_name)

    if _active_actor_matches(page, want_id, page_name):
        if log:
            log(20, f'Profil Fanpage {page_name or want_id} sudah aktif.')
        return True

    if log:
        log(20, f'Mencoba beralih profil Facebook ke Fanpage {page_name or want_id}...')

    # Step 1: Klik tombol Switch / Beralih pada halaman Fanpage
    try:
        clicked_initial = page.run_js(r"""
            const wantName = (arguments[0] || '').toLowerCase();
            const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || n.getAttribute('title') || '')) || '')
                .replace(/\s+/g, ' ').trim().toLowerCase();

            const nodes = Array.from(document.querySelectorAll('div[role="button"],button,a[role="button"]'));
            let best = null;
            for (const n of nodes) {
                const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                const txt = textOf(n);
                if (!txt && !aria) continue;
                const r = n.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;

                // Abaikan tombol/elemen pencarian
                if (aria.includes('search') || txt.includes('search') || aria.includes('cari') || txt.includes('cari')) continue;

                if (wantName && (aria === `switch to ${wantName}` || aria === `beralih ke ${wantName}` || txt === `switch to ${wantName}` || txt === `beralih ke ${wantName}`)) {
                    best = n;
                    break;
                }
                // SENGAJA TIDAK ADA fallback generic "switch"/"beralih" tanpa nama --
                // itu bisa nyasar klik tombol toggle lain yang tidak relevan di
                // halaman (banyak elemen UI Facebook punya label generik serupa),
                // mengacaukan state sebelum Method 2 (avatar switcher) sempat jalan.
            }
            if (best) {
                best.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(type => best.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                if (typeof best.click === 'function') best.click();
                return best.getAttribute('aria-label') || textOf(best);
            }
            return '';
        """, want_name) or ''

        if clicked_initial:
            if log:
                log(22, f'Mengklik tombol switch di halaman ({clicked_initial})...')
            time.sleep(3)

            # Step 2: Klik tombol Switch / Beralih di DALAM modal dialog konfirmasi
            dialog_clicked = page.run_js(r"""
                const dialogs = Array.from(document.querySelectorAll('div[role="dialog"]'));
                for (const dlg of dialogs) {
                    const btns = Array.from(dlg.querySelectorAll('div[role="button"],button,span[role="button"],a[role="button"]'));
                    for (const b of btns) {
                        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                        const txt = (b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        if (aria === 'switch' || aria === 'beralih' || txt === 'switch' || txt === 'beralih') {
                            b.scrollIntoView({block:'center'});
                            ['pointerdown','mousedown','mouseup','click'].forEach(t => b.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                            if (typeof b.click === 'function') b.click();
                            return aria || txt;
                        }
                    }
                }
                return '';
            """)

            if dialog_clicked:
                if log:
                    log(25, f'Mengklik konfirmasi switch pada dialog ({dialog_clicked})...')
                time.sleep(6)
                if _active_actor_matches(page, want_id, page_name):
                    return True
    except Exception:
        pass

    # Fallback Method 2: buka profile switcher (avatar "Your profile" di pojok kanan
    # atas) lalu klik entri "Switch to <PageName>" -- ini elemen [role="button"] YANG
    # PUNYA aria-label="Switch to <PageName>" persis. JANGAN cari berdasar teks yang
    # match di sembarang elemen (div/span) lalu klik elemen itu langsung -- teks nama
    # Page ikut muncul di banyak elemen pembungkus bersarang (div>div>div>...>span),
    # dan .closest()/pencarian teks generik sering nyasar klik wrapper luar yang tidak
    # benar-benar clickable, sehingga switch gagal diam-diam. Method ini terbukti live
    # (26 Jul) berhasil switch akun yang kelola 12 Fanpage sekaligus.
    try:
        avatar_clicked = page.run_js(r"""
            const n = document.querySelector('[aria-label="Your profile"],[aria-label="Profil Anda"]');
            if (!n) return false;
            n.scrollIntoView({block:'center'});
            ['pointerdown','mousedown','mouseup','click'].forEach(t => n.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
            return true;
        """)
        if avatar_clicked:
            time.sleep(2)

            def _click_switch_to_button():
                return page.run_js(r"""
                    const wantId = (arguments[0] || '').toLowerCase();
                    const wantName = (arguments[1] || '').toLowerCase();
                    const btns = Array.from(document.querySelectorAll('[role="button"][aria-label]'));
                    let exact = null, partial = null;
                    for (const b of btns) {
                        const ariaRaw = b.getAttribute('aria-label') || '';
                        const aria = ariaRaw.toLowerCase();
                        let name = null;
                        // Pola 1 (daftar pendek): "Switch to X" / "Beralih ke X"
                        if (aria.startsWith('switch to ')) name = aria.slice('switch to '.length).trim();
                        else if (aria.startsWith('beralih ke ')) name = aria.slice('beralih ke '.length).trim();
                        else {
                            // Pola 2 (daftar "See all profiles"/"Lihat semua profil"):
                            // "X, N notifikasi" / "X, N notifications" / "X, N unseen updates" / "X" polos
                            const stripped = aria.replace(/,\s*\d+\s*(notifikasi|notifications?|unseen updates|pembaruan belum dilihat)\b.*$/i, '').trim();
                            if (stripped && stripped !== aria) name = stripped;
                        }
                        if (!name) continue;
                        if (wantName && name === wantName) { exact = b; break; }
                        if ((wantName && (name.includes(wantName) || wantName.includes(name))) || (wantId && name.includes(wantId))) {
                            partial = partial || b;
                        }
                    }
                    const target = exact || partial;
                    if (!target) return '';
                    target.scrollIntoView({block:'center'});
                    ['pointerdown','mousedown','mouseup','click'].forEach(t => target.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                    return target.getAttribute('aria-label') || '';
                """, want_id, want_name) or ''

            clicked_label = _click_switch_to_button()
            if not clicked_label:
                # Page target belum ada di daftar pendek (recently used) -- coba
                # perluas lewat "See all profiles" lalu cari lagi. WAJIB pakai
                # [aria-label="See all profiles"] persis -- teks yang sama juga
                # muncul di elemen wrapper luar yang tidak clickable (pola sama
                # dengan bug di atas).
                page.run_js(r"""
                    const btn = document.querySelector('[role="button"][aria-label="See all profiles"],[role="button"][aria-label="Lihat semua profil"]');
                    if (btn) {
                        btn.scrollIntoView({block:'center'});
                        ['pointerdown','mousedown','mouseup','click'].forEach(t => btn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                    }
                """)
                time.sleep(3)
                clicked_label = _click_switch_to_button()

            if not clicked_label:
                # Masih belum ketemu -- akun dengan BANYAK Fanpage (10+) perlu 1 level
                # ekspansi lagi ("See More Profiles"/"Lihat Profil Selengkapnya"). Tombol
                # ini TIDAK punya aria-label sama sekali, jadi cari via teks lalu ambil
                # ancestor [role="button"] terdekat (bukan klik node teks itu sendiri).
                page.run_js(r"""
                    const textOf = (n) => ((n && (n.innerText || n.textContent || '')) || '').replace(/\s+/g,' ').trim();
                    const nodes = Array.from(document.querySelectorAll('span,div'));
                    const leaf = nodes.find(n => n.children.length === 0 &&
                        (textOf(n) === 'See More Profiles' || textOf(n) === 'Lihat Profil Selengkapnya'));
                    if (leaf) {
                        const btn = leaf.closest('[role="button"]');
                        if (btn) {
                            btn.scrollIntoView({block:'center'});
                            ['pointerdown','mousedown','mouseup','click'].forEach(t => btn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                        }
                    }
                """)
                time.sleep(3)
                clicked_label = _click_switch_to_button()

            if clicked_label:
                if log:
                    log(22, f'Klik "{clicked_label}" di profile switcher...')
                time.sleep(5)
                if _active_actor_matches(page, want_id, page_name):
                    return True
    except Exception:
        pass

    return _active_actor_matches(page, want_id, page_name)


def _open_page_professional_dashboard(page, page_id, page_name, log):
    """Buka Professional Dashboard untuk Fanpage target.

    PENTING (26 Jul, dibuktikan live pakai akun 12-Fanpage): asset_id di URL
    professional_dashboard TIDAK selalu bikin context ke FP yang benar --
    dashboard bisa saja tetap tampil punya Fanpage LAIN (mis. Fanpage
    default/terakhir aktif akun itu). Jadi setelah navigasi, WAJIB verifikasi
    identitas aktif (_active_actor_matches, baca CurrentUserInitialData
    Facebook langsung) dan kalau salah, switch manual lewat profile switcher
    (_try_switch_dp_profile) -- itu satu-satunya cara yang terbukti reliable
    ganti "acting as" ke Fanpage yang benar.
    """
    want_id = str(page_id or '').strip()
    if not re.fullmatch(r'\d+', want_id or ''):
        log(20, 'Batal: page_id Fanpage harus berupa angka asset_id.')
        return False

    def is_on_dashboard():
        try:
            url = str(page.url or '').lower()
            return 'professional_dashboard' in url or 'bulk_upload' in url
        except Exception:
            return False

    # Optimasi batch: kalau item SEBELUMNYA di batch yang sama sudah nge-lock
    # Fanpage ini (identitas aktif SUDAH benar & masih di halaman dashboard-ish),
    # skip total navigasi ulang -- hemat ~6-15 detik reload per item utk batch
    # dengan banyak item beruntun ke Fanpage yang sama.
    if is_on_dashboard() and _active_actor_matches(page, want_id, page_name):
        log(26, f'Sudah di Dashboard Profesional Fanpage {page_name or want_id} (skip reload).')
        return True

    log(20, f'Membuka Dashboard Profesional Fanpage {page_name or want_id}...')

    url = f'https://www.facebook.com/professional_dashboard/?asset_id={want_id}'
    for attempt in range(3):
        try:
            # Dismiss dialog box yang mungkin muncul sebelum navigasi
            try:
                page.handle_alert(accept=True, next_one=False)
            except Exception:
                pass
            page.get(url, timeout=40)
            time.sleep(6 + attempt * 3)
            if is_on_dashboard():
                if not _active_actor_matches(page, want_id, page_name):
                    log(23, f'Dashboard aktif belum di Fanpage {page_name or want_id}, coba switch profil...')
                    _try_switch_dp_profile(page, want_id, page_name, log)
                    if not _active_actor_matches(page, want_id, page_name):
                        log(24, f'Gagal switch ke Fanpage {page_name or want_id} yang benar, batal supaya tidak salah target.')
                        return False
                log(26, f'Berhasil masuk ke Dashboard Profesional {page_name or want_id}.')
                try:
                    page.run_js('window.scrollTo(0, 0);')
                except Exception:
                    pass
                return True
            log(22, f'Percobaan {attempt+1}: URL belum di dashboard ({page.url}), coba lagi...')
        except Exception as e:
            # Coba dismiss dialog lalu retry
            try:
                page.handle_alert(accept=True, next_one=False)
            except Exception:
                pass
            log(22, f'Percobaan {attempt+1} gagal: {e}')
        time.sleep(3)

    _debug_dump(page, 'dashboard_target_fail')
    log(26, f'Gagal membuka Dashboard Profesional untuk Fanpage {page_name or want_id}. '
            f'URL saat ini: {page.url or "?"}')
    return False


def _open_page_post_composer(page, log, is_photo=False):
    target_label = 'post' if is_photo else 'reel'
    log(28, f"Mencari composer {target_label} Fanpage...")
    try:
        page.run_js('window.scrollTo(0, 0);')
    except Exception:
        pass

    try:
        clicked = page.run_js(r"""
            const targetLabel = arguments[0];
            const textOf = (n) => (((n && (n.getAttribute('aria-label') || n.getAttribute('placeholder') || n.innerText || n.textContent || '')) || '')
                .replace(/\s+/g, ' ').trim().toLowerCase());
            const clickNode = (n) => {
                if (!n) return false;
                n.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(type => n.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                if (typeof n.click === 'function') n.click();
                return true;
            };
            const nodes = Array.from(document.querySelectorAll('div[role="button"],button,a[role="button"],span[role="button"],a,div,span'));
            let createBtn = null;
            for (const n of nodes) {
                const txt = textOf(n);
                if (!txt) continue;
                if (!(txt === 'create a post' || txt === 'create post' || txt.startsWith('create a post ') || txt.startsWith('create post '))) continue;
                const r = n.getBoundingClientRect();
                if (r.width < 80 || r.height < 24) continue;
                createBtn = n;
            }
            if (!clickNode(createBtn)) return { ok:false, stage:'create' };
            const deadline = Date.now() + 5000;
            while (Date.now() < deadline) {
                const opts = Array.from(document.querySelectorAll('div[role="button"],button,a[role="button"],span[role="button"],a,div,span'));
                const wanted = targetLabel === 'post'
                    ? ['post', 'create post', 'buat postingan']
                    : ['bulk upload reels', 'reel'];
                for (const n of opts) {
                    const txt = textOf(n);
                    if (!txt) continue;
                    if (!wanted.some(w => txt === w || txt.includes(w))) continue;
                    const r = n.getBoundingClientRect();
                    if (r.width < 40 || r.height < 20) continue;
                    if (clickNode(n)) return { ok:true, stage:txt };
                }
            }
            return { ok:false, stage:'menu' };
        """, target_label) or {}
        if clicked.get('ok'):
            time.sleep(4)
            return str(clicked.get('stage') or target_label)
    except Exception:
        pass

    for texts in (
        ['create post', 'create a post', 'buat postingan', "what's on your mind", 'apa yang anda pikirkan'],
        ['photo/video', 'foto/video', 'add photo', 'add video', 'tambahkan foto', 'tambahkan video'],
        ['bulk upload reels', 'reel'] if not is_photo else ['post', 'buat postingan'],
    ):
        clicked = _click_by_text(page, texts, timeout=6)
        if clicked:
            time.sleep(2)
            return clicked
    try:
        clicked = page.run_js(r"""
            const wanted = arguments[0];
            const nodes = Array.from(document.querySelectorAll('div[role="button"],button,a[role="button"],div[aria-label],span[role="button"],textarea,[contenteditable="true"]'));
            let best = null;
            for (const n of nodes) {
                const txt = (((n.getAttribute('aria-label')||'') + ' ' + (n.getAttribute('placeholder')||'') + ' ' + (n.innerText||'')).replace(/\s+/g, ' ').trim().toLowerCase());
                if (!txt) continue;
                if (!wanted.some(w => txt.includes(w))) continue;
                if (n.closest('article,[role="article"],[aria-label*="comment" i],[aria-label*="komentar" i]')) continue;
                const r = n.getBoundingClientRect();
                if (r.width < 40 || r.height < 20) continue;
                if (r.top < 0 || r.top > window.innerHeight * 0.80) continue;
                if (!best || r.top > best.r.top) best = { el: n, r };
            }
            if (best && best.el) { best.el.click(); return true; }
            return false;
        """, ['create post','create a post','buat postingan',"what's on your mind",'apa yang anda pikirkan','photo/video','foto/video','bulk upload reels','reel','post'])
        if clicked:
            time.sleep(3)
            return 'fallback-composer'
    except Exception:
        pass
    return ''


def _open_professional_reels_uploader(page, video_path, page_id, page_name, log, dry_run=False, trust_context=False):
    log(34, 'Membuka uploader video Bulk upload reels...')
    abs_path = os.path.abspath(video_path)
    want_id = str(page_id or '').strip()

    def body_text():
        try:
            raw = page.run_js(
                "return document.body ? (document.body.innerText || '') : ''; "
            ) or ''
            # encode/decode untuk buang karakter non-latin yang bikin crash di Windows
            return raw.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore').lower()
        except Exception:
            return ''

    def _url_has_asset():
        """Cek asset_id di URL — cara paling reliable untuk konfirmasi konteks FP."""
        try:
            url = str(page.url or '')
            return want_id in url
        except Exception:
            return False

    def is_bulk_page():
        url = str(page.url or '').lower()
        on_bulk = '/bulk_upload' in url
        # Validasi utama: URL sudah benar (asset_id + bulk_upload) → tidak perlu cek body
        if _url_has_asset() and on_bulk:
            return True
        # Fallback: cek body text (EN + ID)
        txt = body_text()
        has_content = (
            'bulk upload reels' in txt or
            'unggah massal reel' in txt or
            'bulk upload items' in txt or
            'upload from your computer' in txt or
            'unggah dari komputer' in txt or
            'add more videos' in txt or
            'tambah lebih banyak video' in txt or
            'drag and drop' in txt or
            'serat dan lepas' in txt
        )
        if _url_has_asset():
            return has_content
        return trust_context and (on_bulk or has_content)

    def has_bulk_items():
        txt = body_text()
        url = str(page.url or '').lower()
        context_ok = _url_has_asset() or trust_context or 'professional_dashboard' in url
        return context_ok and (
            'bulk upload items' in txt or
            "what's on your mind" in txt or
            'apa yang anda pikirkan' in txt or
            'publish now' in txt or
            'terbitkan sekarang' in txt or
            'checking for copyrighted content' in txt or
            'uploaded ' in txt or
            'sequence posts' in txt or
            'jadwalkan postingan' in txt
        )

    def trigger_upload_once():
        try:
            page.set.upload_files(abs_path)
        except Exception:
            pass
        # Coba berbagai teks tombol upload
        for btn_texts in (
            ['upload from your computer', 'add more videos'],
            ['tambah lebih banyak video', 'unggah dari komputer'],
            ['upload', 'add video'],
        ):
            if _click_by_text(page, btn_texts, timeout=4):
                break
        time.sleep(2)
        finp = _find_file_input(page, timeout=10, prefer='video')
        if finp:
            try:
                finp.input(abs_path)
                return True
            except Exception:
                pass
        # Fallback: coba set upload via DrissionPage upload_files lalu input ke file input
        try:
            page.set.upload_files(abs_path)
            finp2 = _find_file_input(page, timeout=5, prefer='video')
            if finp2:
                finp2.input(abs_path)
                time.sleep(1)
                return True
        except Exception:
            pass
        return False

    if not want_id:
        return False

    # HANYA gunakan URL dengan asset_id — jangan fallback ke URL tanpa asset_id
    # karena Facebook akan pakai FP default jika tidak ada asset_id
    direct_urls = [
        f'https://www.facebook.com/professional_dashboard/bulk_upload/?asset_id={want_id}',
    ]

    for idx, url in enumerate(direct_urls):
        try:
            log(35, f'Membuka halaman Bulk Upload Fanpage {idx+1}/{len(direct_urls)}...')
            # Dismiss dialog dulu jika ada
            try:
                page.handle_alert(accept=True, next_one=False)
            except Exception:
                pass
            page.get(url, timeout=40)
            time.sleep(6)
            # Validasi ketat: asset_id HARUS ada di URL
            cur_url = str(page.url or '')
            if want_id not in cur_url:
                log(35, f'asset_id tidak ada di URL setelah navigasi: {cur_url}')
                continue
            if not is_bulk_page():
                continue
            if dry_run:
                return True
            trigger_upload_once()
            wait_end = time.time() + 45
            while time.time() < wait_end:
                if has_bulk_items():
                    _debug_dump(page, 'bulk_page_opened')
                    return True
                if 'upload from your computer' in body_text() and 'bulk upload items' not in body_text():
                    trigger_upload_once()
                time.sleep(2)
        except Exception:
            pass

    _debug_dump(page, 'bulk_page_opened_failed')
    return False

def _check_facebook_rate_limit(page):
    """Deteksi dialog rate-limit/anti-spam Facebook ("You Can't Use This Feature
    Right Now" / versi Indonesia serupa) yang muncul SETELAH klik Save/Publish --
    ini penting karena klik-nya sendiri sering "berhasil" (elemen ketemu & ke-klik)
    padahal submit-nya DITOLAK Facebook, jadi tanpa cek ini caller bisa salah lapor
    sukses. Ditemukan 26 Jul: klik Save bulk upload bisa "berhasil" tapi Facebook
    diam-diam nolak dgn dialog ini kalau akun kena rate-limit."""
    try:
        txt = (page.run_js("return (document.body && document.body.innerText || '')") or '').lower()
        return (
            "can't use this feature right now" in txt or
            'we limit how often you can post' in txt or
            'tidak dapat menggunakan fitur ini' in txt or
            'kami membatasi seberapa sering' in txt
        )
    except Exception:
        return False


def _click_bulk_upload_save(page, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            clicked = page.run_js(r''' 
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const visible = (n) => {
                    if (!n || !n.isConnected) return false;
                    const style = window.getComputedStyle ? window.getComputedStyle(n) : null;
                    if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
                    const r = n.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
                };
                const nodes = Array.from(document.querySelectorAll('div[role="button"],button,a[role="button"],span[role="button"],div,span')).filter(visible);
                let best = null;
                for (const n of nodes) {
                    const txt = norm((n.getAttribute('aria-label') || '') + ' ' + (n.innerText || n.textContent || ''));
                    if (!(txt === 'save' || txt.startsWith('save ') || txt.includes(' save') || txt === 'simpan' || txt.includes('simpan'))) continue;
                    const host = n.closest('button,[role="button"],a,div,span') || n;
                    const disabled = host.getAttribute('aria-disabled') === 'true' || host.getAttribute('disabled') !== null;
                    const r = host.getBoundingClientRect();
                    const score = (r.top * 10000) + r.left + (disabled ? -10000000 : 0);
                    if (!best || score > best.score) best = { el: host, score, disabled, txt };
                }
                if (!best || best.disabled) return '';
                best.el.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(type => best.el.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                if (typeof best.el.click === 'function') best.el.click();
                return best.txt || 'save';
            ''') or ''
            if clicked:
                return clicked
        except Exception:
            pass
        time.sleep(1)
    return ''


def _wait_bulk_upload_ready(page, log, page_id='', page_name='', timeout=600, trust_context=False):
    log(66, 'Menunggu proses video Facebook selesai...')
    end = time.time() + timeout
    last_msg = ''
    want_id = str(page_id or '').strip()
    while time.time() < end:
        # Cek konteks FP tidak berubah selama menunggu
        if want_id:
            cur_url = str(page.url or '')
            if want_id not in cur_url and not trust_context:
                return False, (f'Konteks Fanpage hilang saat menunggu proses upload. '
                               f'asset_id {want_id} tidak ada di URL: {cur_url}')
        try:
            info = page.run_js(r"""
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const txt = norm(document.body && document.body.innerText || '');
                const visible = (n) => {
                    if (!n || !n.isConnected) return false;
                    const style = window.getComputedStyle ? window.getComputedStyle(n) : null;
                    if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
                    const r = n.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
                };
                const buttons = Array.from(document.querySelectorAll('div[role="button"],button,a[role="button"],span[role="button"]')).filter(visible);
                const save = buttons.find(n => {
                    const label = norm((n.getAttribute('aria-label') || '') + ' ' + (n.innerText || n.textContent || ''));
                    return label === 'save' || label.startsWith('save ') || label === 'simpan' || label.startsWith('simpan ');
                });
                const saveDisabled = save ? (save.getAttribute('aria-disabled') === 'true' || save.getAttribute('disabled') !== null) : true;
                const pct = (txt.match(/(\d{1,3})%/) || [,''])[1];
                const checking = /(checking for copyrighted content|memeriksa konten berhak cipta|copyrighted content|memeriksa konten)/i.test(txt) &&
                    !/(no copyright issues|you'?re good to go|safe to publish|tidak ada masalah hak cipta|anda sudah siap|tidak terdeteksi masalah hak cipta)/i.test(txt);
                const uploaded = /uploaded\s+\d+\s*\/\s*\d+\s+videos?/i.test(txt) ||
                    /diunggah\s+\d+\s*\/\s*\d+/i.test(txt) ||
                    /mengunggah\s+[1-9]\d*\s*\/\s*\d+/i.test(txt);
                const good = /(no copyright issues|you'?re good to go|safe to publish|tidak ada masalah hak cipta|anda sudah siap|tidak terdeteksi masalah hak cipta)/i.test(txt);
                const failed = /(couldn'?t be uploaded|can'?t read files|gagal upload|tidak bisa diunggah|copyright issue detected|masalah hak cipta)/i.test(txt) &&
                    !/(no copyright issues|tidak ada masalah hak cipta|tidak terdeteksi masalah hak cipta|anda sudah siap|you'?re good to go|safe to publish)/i.test(txt);
                return { pct, checking, uploaded, good, failed, saveFound: !!save, saveDisabled, sample: txt.slice(0, 500) };
            """) or {}
        except Exception:
            info = {}
        if info.get('failed'):
            return False, 'Facebook menolak media atau mendeteksi masalah pada video.'
        # Harus tunggu good=True (copyright clear) — jangan rely pada saveFound saja
        if info.get('uploaded') and info.get('good') and not info.get('checking'):
            return True, ''
        pct = str(info.get('pct') or '').strip()
        msg = 'Menunggu copyright check Facebook selesai'
        if pct:
            msg += f' ({pct}%)'
        elif info.get('uploaded'):
            msg += ' (uploaded, finalisasi)'
        if msg != last_msg:
            log(68, msg + '...')
            last_msg = msg
        time.sleep(5)
    return False, 'Proses upload/copyright check Facebook terlalu lama.'


def _open_bulk_publish_dropdown(page):
    """Buka dropdown opsi publish (di sebelah tombol 'Publish now') supaya bisa pilih
    "Schedule" alih-alih publish langsung.

    BUG BERBAHAYA YANG DIPERBAIKI (26 Jul): versi lama klik LANGSUNG elemen bertuliskan
    "Publish now" -- itu TOMBOL AKSI UTAMA yang langsung mem-publish konten asli, BUKAN
    dropdown toggle-nya! Ini menyebabkan konten ter-publish seketika padahal user minta
    mode Jadwalkan. Sekarang fungsi ini TIDAK PERNAH klik elemen bertuliskan "Publish now"
    sama sekali -- cuma cari toggle dropdown DI SEBELAHNYA (elemen [role="button"] lain
    dalam grup yang sama, yang py aria-haspopup atau tanpa teks/cuma ikon panah). Kalau
    toggle-nya tidak ketemu dengan yakin, GAGAL (return False) -- lebih baik jadwal gagal
    diset drpd berisiko publish konten yang tidak diinginkan.
    """
    try:
        return bool(page.run_js(r'''
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const visible = (n) => {
                if (!n || !n.isConnected) return false;
                const style = window.getComputedStyle ? window.getComputedStyle(n) : null;
                if (style && (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none')) return false;
                const r = n.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
            };
            const isPublishNowText = (n) => {
                const txt = norm(n.innerText || n.textContent || '');
                return txt === 'publish now' || txt === 'terbitkan sekarang';
            };
            const all = Array.from(document.querySelectorAll('div,span,button,[role="button"]')).filter(visible);
            const publishLabel = all.find(isPublishNowText);
            if (!publishLabel) return false;
            const publishBtn = publishLabel.closest('button,[role="button"]') || publishLabel;
            // Cari kandidat toggle: elemen [role="button"]/button lain yang posisinya
            // berdekatan (sibling/dalam container yang sama) dengan tombol Publish now,
            // TAPI bukan tombol Publish now itu sendiri, dan py sinyal jelas ini toggle
            // (aria-haspopup, atau nyaris tanpa teks -- cuma ikon panah).
            const container = publishBtn.parentElement ? publishBtn.parentElement.parentElement || publishBtn.parentElement : null;
            if (!container) return false;
            const candidates = Array.from(container.querySelectorAll('button,[role="button"]')).filter(visible);
            let toggle = null;
            for (const c of candidates) {
                if (c === publishBtn || c.contains(publishBtn) || publishBtn.contains(c)) continue;
                if (isPublishNowText(c)) continue;
                const txt = norm(c.innerText || c.textContent || '');
                const hasPopup = c.getAttribute('aria-haspopup');
                if (hasPopup || txt.length === 0) { toggle = c; break; }
            }
            if (!toggle) return false;
            toggle.scrollIntoView({block:'center'});
            ['pointerdown','mousedown','mouseup','click'].forEach(t => toggle.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
            return true;
        '''))
    except Exception:
        return False


def _pick_bulk_schedule_option(page):
    for texts in (['schedule', 'jadwalkan'], ['schedule post', 'jadwalkan postingan']):
        if _click_by_text(page, texts, timeout=3):
            time.sleep(2)
            return True
    try:
        return bool(page.run_js(r'''
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const visible = (n) => {
                if (!n || !n.isConnected) return false;
                const style = window.getComputedStyle ? window.getComputedStyle(n) : null;
                if (style && (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none')) return false;
                const r = n.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
            };
            const nodes = Array.from(document.querySelectorAll('div[role="menuitem"],div[role="option"],li,button,a[role="button"],span[role="button"],div,span')).filter(visible);
            let best = null;
            for (const n of nodes) {
                const txt = norm((n.getAttribute('aria-label') || '') + ' ' + (n.innerText || n.textContent || ''));
                if (!(txt === 'schedule' || txt.startsWith('schedule') || txt === 'jadwalkan' || txt.includes('schedule post') || txt.includes('jadwalkan'))) continue;
                const r = n.getBoundingClientRect();
                if (!best || r.top < best.r.top) best = { el:n, r };
            }
            if (!best) return false;
            best.el.scrollIntoView({block:'center'});
            best.el.click();
            return true;
        '''))
    except Exception:
        return False


def _set_bulk_upload_schedule(page, schedule_time_str, log, page_id='', page_name='', trust_context=False):
    try:
        from datetime import datetime
        dt = datetime.strptime(schedule_time_str.strip(), '%Y-%m-%d %H:%M')
    except ValueError:
        log(70, f'Format jadwal tidak dikenali ({schedule_time_str}), skip jadwal.')
        return False

    if page_id and not _target_context_ok(page, page_id, page_name, trust_context=trust_context):
        log(70, f'Batal schedule: {_format_target_lock_error(_target_lock_info(page, page_id, page_name))}')
        return False

    date_display = dt.strftime('%b %d, %Y').replace(' 0', ' ')
    date_us = dt.strftime('%m/%d/%Y')
    date_iso = dt.strftime('%Y-%m-%d')
    time_12 = dt.strftime('%I:%M %p')
    time_12_plain = time_12.lstrip('0')
    time_24 = dt.strftime('%H:%M')

    try:
        try:
            opened = _open_bulk_publish_dropdown(page)
            picked = False
            if opened:
                picked = _pick_bulk_schedule_option(page)
            if not (opened and picked):
                log(70, 'Dropdown Publish now belum membuka opsi Schedule.')
        except Exception:
            pass
        log(70, f'Mengatur jadwal Bulk Upload Fanpage: {schedule_time_str}...')

        # Step A: Klik 'Edit video' jika modal edit reel belum terbuka
        page.run_js(r"""
            const btns = Array.from(document.querySelectorAll('div[role="button"], button'));
            const editBtn = btns.find(n => {
                const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                const txt = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                return aria.includes('edit video') || aria.includes('edit reel') || aria.includes('sunting') || txt.includes('edit video');
            });
            if (editBtn) {
                ['pointerdown','mousedown','mouseup','click'].forEach(t => editBtn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                if (typeof editBtn.click === 'function') editBtn.click();
            }
        """)
        time.sleep(2)

        # Step B: Klik Next pada Step 1
        page.run_js(r"""
            const btns = Array.from(document.querySelectorAll('div[role="button"], button'));
            const nextBtn = btns.find(n => {
                const r = n.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0 || r.left < 0) return false;
                const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                const txt = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                return aria === 'next' || aria === 'berikutnya' || txt === 'next' || txt === 'berikutnya';
            });
            if (nextBtn) {
                ['pointerdown','mousedown','mouseup','click'].forEach(t => nextBtn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                if (typeof nextBtn.click === 'function') nextBtn.click();
            }
        """)
        time.sleep(2)

        # Step C: Klik Next pada Step 2
        page.run_js(r"""
            const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || '')) || '').replace(/\s+/g, ' ').trim();
            const btns = Array.from(document.querySelectorAll('div[role="button"], button'));
            const nextBtn = btns.find(n => {
                const r = n.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0 || r.left < 0) return false;
                const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                const txt = textOf(n).toLowerCase();
                return aria === 'next' || aria === 'berikutnya' || txt === 'next' || txt === 'berikutnya';
            });
            if (nextBtn) {
                ['pointerdown','mousedown','mouseup','click'].forEach(t => nextBtn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                if (typeof nextBtn.click === 'function') nextBtn.click();
            }
        """)
        time.sleep(3)

        # Step D: Klik tombol 'Scheduling options' sub-panel
        page.run_js(r"""
            const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || '')) || '').replace(/\s+/g, ' ').trim();
            const btns = Array.from(document.querySelectorAll('div[role="button"]'));
            const target = btns.find(n => {
                const txt = textOf(n).toLowerCase();
                return txt.includes('scheduling options') || txt.includes('opsi penjadwalan');
            });
            if (target) {
                target.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(t => target.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                if (typeof target.click === 'function') target.click();
            }
        """)
        time.sleep(2)

        # Step E: Set Tanggal dan Jam pada input
        res = page.run_js(r"""
            const want = arguments[0];
            const inputs = Array.from(document.querySelectorAll('input'));
            const visibleInputs = inputs.filter(n => {
                const r = n.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.left > 300;
            });
            let dateInput = visibleInputs[0];
            let timeInput = visibleInputs[1];
            if (!dateInput) dateInput = inputs.find(n => n.value && (n.value.includes('202') || n.value.includes('/')));
            if (!timeInput) timeInput = inputs.find(n => n.value && (n.value.includes('AM') || n.value.includes('PM') || n.value.includes(':')));

            const setVal = (el, val) => {
                if (!el) return false;
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(el, val); else el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                try { el.blur(); } catch (e) {}
                return true;
            };
            const dRes = setVal(dateInput, want.dateDisplay) || setVal(dateInput, want.dateUs) || setVal(dateInput, want.dateIso);
            const tRes = setVal(timeInput, want.time12) || setVal(timeInput, want.time12Plain) || setVal(timeInput, want.time24);
            return (dRes || tRes) && (dateInput && !!dateInput.value);
        """, {
            'dateDisplay': date_display,
            'dateUs': date_us,
            'dateIso': date_iso,
            'time12': time_12,
            'time12Plain': time_12_plain,
            'time24': time_24,
        })
        time.sleep(1)

        if not res:
            # Field tanggal/jam tidak ketemu/tidak ke-set -- JANGAN lanjut klik Save,
            # itu bisa mem-publish/menyimpan dalam state yang salah (bukan terjadwal
            # sesuai permintaan). Lebih baik gagal jelas drpd asal lanjut.
            log(70, 'Field tanggal/jam jadwal tidak ketemu di Bulk Upload, batal supaya tidak salah publish.')
            return False

        # Step F: Klik 'Schedule for later' / 'Save'
        page.run_js(r"""
            const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || '')) || '').replace(/\s+/g, ' ').trim();
            const btns = Array.from(document.querySelectorAll('div[role="button"], button'));
            const btn = btns.find(n => {
                const txt = textOf(n).toLowerCase();
                const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                return aria.includes('schedule for later') || aria === 'save' || txt === 'schedule for later' || txt === 'simpan' || txt === 'save';
            });
            if (btn) {
                btn.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(t => btn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                if (typeof btn.click === 'function') btn.click();
            }
        """)
        time.sleep(3)
        return True
    except Exception as e:
        log(70, f'Gagal set jadwal Bulk Upload Fanpage: {e}')
        return False


def _set_datetime_facebook_composer(page, schedule_time_str, log, page_id='', page_name='', trust_context=False):
    try:
        from datetime import datetime
        dt = datetime.strptime(schedule_time_str.strip(), '%Y-%m-%d %H:%M')
    except ValueError:
        log(70, f'Format jadwal tidak dikenali ({schedule_time_str}), skip jadwal.')
        return False

    log(70, f'Mengatur jadwal postingan: {schedule_time_str}...')
    if '/bulk_upload' in str(page.url or '').lower():
        return _set_bulk_upload_schedule(page, schedule_time_str, log, page_id=page_id, page_name=page_name, trust_context=trust_context)
    date_display = dt.strftime('%b %d, %Y').replace(' 0', ' ')
    date_us = dt.strftime('%m/%d/%Y')
    date_iso = dt.strftime('%Y-%m-%d')
    time_12 = dt.strftime('%I:%M %p')
    time_12_plain = time_12.lstrip('0')
    time_24 = dt.strftime('%H:%M')

    try:
        for _ in range(3):
            _click_by_text(page,
                ['schedule post', 'schedule', 'jadwalkan postingan', 'jadwalkan', 'schedule for later'], timeout=6)
            time.sleep(1)
            result = page.run_js(r''' 
                const want = arguments[0];
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const textOf = (n) => norm((n && (n.value || n.innerText || n.textContent || n.getAttribute('aria-label') || n.getAttribute('placeholder') || n.getAttribute('title') || '')) || '');
                const visible = (n) => {
                    if (!n || !n.isConnected) return false;
                    const style = window.getComputedStyle ? window.getComputedStyle(n) : null;
                    if (style && (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none')) return false;
                    const r = n.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
                };
                const clickNode = (n) => {
                    if (!n || !visible(n)) return false;
                    n.scrollIntoView({block:'center'});
                    ['pointerdown','mousedown','mouseup','click'].forEach(type => n.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                    if (typeof n.click === 'function') n.click();
                    return true;
                };
                const setValue = (el, value) => {
                    if (!el) return false;
                    if (el.isContentEditable) {
                        el.focus();
                        try { document.execCommand('selectAll', false, null); } catch (e) {}
                        el.textContent = value;
                        el.dispatchEvent(new InputEvent('input', { bubbles:true, data:value, inputType:'insertText' }));
                        el.dispatchEvent(new Event('change', { bubbles:true }));
                        return true;
                    }
                    if ('value' in el) {
                        const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && desc.set) desc.set.call(el, value); else el.value = value;
                        el.dispatchEvent(new Event('input', { bubbles:true }));
                        el.dispatchEvent(new Event('change', { bubbles:true }));
                        try { el.blur(); } catch (e) {}
                        return true;
                    }
                    return false;
                };
                const nearbyEditor = (host) => {
                    const hosts = [];
                    if (host) {
                        hosts.push(host);
                        if (host.parentElement) hosts.push(host.parentElement);
                        const box = host.closest('[role="dialog"],form,section,div');
                        if (box) hosts.push(box);
                    }
                    for (const root of hosts) {
                        if (!root) continue;
                        const found = Array.from(root.querySelectorAll('input:not([type=file]), textarea, [contenteditable="true"]')).find(visible);
                        if (found) return found;
                    }
                    const focused = document.activeElement;
                    if (focused && visible(focused) && (focused.matches('input:not([type=file]), textarea, [contenteditable="true"]') || focused.isContentEditable)) return focused;
                    const all = Array.from(document.querySelectorAll('input:not([type=file]), textarea, [contenteditable="true"]')).filter(visible);
                    if (!host || !all.length) return null;
                    const hr = host.getBoundingClientRect();
                    let best = null;
                    for (const el of all) {
                        const r = el.getBoundingClientRect();
                        const dist = Math.abs(r.top - hr.top) + Math.abs(r.left - hr.left);
                        if (!best || dist < best.dist) best = { el, dist };
                    }
                    return best ? best.el : null;
                };
                const fillField = (kind, values) => {
                    const selectors = 'input:not([type=file]), textarea, [contenteditable="true"], [role="combobox"], [role="textbox"], [role="button"], button, div';
                    const nodes = Array.from(document.querySelectorAll(selectors)).filter(visible);
                    let best = null;
                    for (const n of nodes) {
                        const txt = textOf(n);
                        if (!txt && !n.matches('input,textarea,[contenteditable="true"]')) continue;
                        if (n.closest('[aria-label*="comment" i], [aria-label*="komentar" i], article[role="article"]')) continue;
                        const r = n.getBoundingClientRect();
                        let score = 0;
                        if (kind === 'date') {
                            if (n.matches('input[type="date"]')) score += 250;
                            if (txt.includes('date') || txt.includes('tanggal')) score += 150;
                            if (/[a-z]{3}\s+\d{1,2},\s+\d{4}/i.test(txt) || /\d{1,2}\/\d{1,2}\/\d{4}/.test(txt) || /\d{4}-\d{2}-\d{2}/.test(txt)) score += 120;
                            if (txt.includes('schedule') || txt.includes('scheduling options')) score += 40;
                        } else {
                            if (n.matches('input[type="time"]')) score += 250;
                            if (txt.includes('time') || txt.includes('waktu') || txt.includes('jam')) score += 150;
                            if (/\d{1,2}:\d{2}\s*(am|pm)/i.test(txt) || /\d{2}:\d{2}/.test(txt)) score += 120;
                            if (txt.includes('asia/jakarta') || txt.includes('gmt') || txt.includes('utc')) score += 80;
                            if (txt.includes('schedule') || txt.includes('scheduling options')) score += 40;
                        }
                        if (r.left > window.innerWidth * 0.45) score += 15;
                        if (r.top < window.innerHeight * 0.8) score += 10;
                        if (score <= 0) continue;
                        if (!best || score > best.score) best = { el:n, score };
                    }
                    if (!best) return false;
                    clickNode(best.el);
                    const editor = nearbyEditor(best.el) || best.el;
                    for (const value of values) {
                        if (!value) continue;
                        if (setValue(editor, value)) {
                            clickNode(best.el);
                            return true;
                        }
                    }
                    return false;
                };
                const clickAction = (words) => {
                    const nodes = Array.from(document.querySelectorAll('div[role="button"],button,a[role="button"],span[role="button"],div,span')).filter(visible);
                    for (const n of nodes) {
                        const txt = textOf(n);
                        if (!txt) continue;
                        if (words.some(w => txt === w || txt.startsWith(w) || txt.includes(w))) {
                            if (clickNode(n)) return txt;
                        }
                    }
                    return '';
                };

                clickAction(['schedule post', 'schedule for later', 'schedule', 'jadwalkan postingan', 'jadwalkan']);
                const dateFilled = fillField('date', [want.dateDisplay, want.dateUs, want.dateIso]);
                const timeFilled = fillField('time', [want.time12, want.time12Plain, want.time24]);
                clickAction(['done', 'ok', 'oke', 'confirm', 'save', 'apply', 'simpan']);

                const body = norm(document.body && document.body.innerText || '');
                const values = Array.from(document.querySelectorAll('input:not([type=file]), textarea, [contenteditable="true"], [role="combobox"], [role="textbox"], [role="button"], button')).filter(visible).map(textOf).join(' | ');
                const hay = values + ' | ' + body;
                const dateOk = hay.includes(norm(want.dateDisplay)) || hay.includes(norm(want.dateUs)) || hay.includes(norm(want.dateIso));
                const timeOk = hay.includes(norm(want.time12)) || hay.includes(norm(want.time12Plain)) || hay.includes(norm(want.time24));
                return { dateFilled, timeFilled, dateOk, timeOk, values: values.slice(0, 1500), body: body.slice(0, 2000) };
            ''', {
                'dateDisplay': date_display,
                'dateUs': date_us,
                'dateIso': date_iso,
                'time12': time_12,
                'time12Plain': time_12_plain,
                'time24': time_24,
            }) or {}
            if result.get('dateOk') and result.get('timeOk'):
                time.sleep(1)
                return True
            time.sleep(2)
        try:
            debug = page.run_js(r'''
                const txt = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim();
                return txt.slice(0, 2500);
            ''') or ''
        except Exception:
            debug = ''
        _debug_dump(page, 'schedule_fail')
        log(70, f'Field jadwal belum cocok. Snapshot UI: {debug[:280]}')
        return False
    except Exception as e:
        log(70, f'Gagal set jadwal Dashboard Profesional: {e}')
        return False


def _resolve_schedule_time(item):
    schedule_time = (item.get('schedule_time') or '').strip()
    if schedule_time:
        return schedule_time
    date_str = (item.get('date') or '').strip()
    time_str = (item.get('time') or '').strip()
    if date_str and time_str:
        return f'{date_str} {time_str}'
    return ''


def _upload_via_professional_dashboard(page, video_path, caption, schedule_time, page_id, page_name, dry_run, log, publish_mode='schedule', post_delay=0):
    is_photo = _is_image(video_path)
    media_label = 'foto' if is_photo else 'video'

    opened_dash = _open_page_professional_dashboard(page, page_id, page_name, log)
    if not opened_dash:
        return False, f'Tidak berhasil mengunci Dashboard Profesional ke Fanpage {page_name or page_id}.'
    trusted_page_context = _target_context_ok(page, page_id, page_name, trust_context=True)

    bulk_started = False
    if is_photo:
        opened = _open_page_post_composer(page, log, is_photo=True)
        if not opened:
            return False, ('Tidak menemukan composer post Fanpage. '
                           'Bot menghindari kolom komentar agar tidak salah kirim ke komentar post lama.')
        log(38, f'Mencari input file ({media_label})...')
        _click_by_text(page,
            ['upload from your computer', 'upload', 'upload photo', 'photo/video', 'foto/video', 'add media', 'add photo', 'tambahkan foto'], timeout=8)
        time.sleep(2)
    else:
        opened = _open_professional_reels_uploader(
            page, video_path, page_id, page_name, log,
            dry_run=dry_run, trust_context=trusted_page_context
        )
        if not opened:
            return False, 'Tidak berhasil membuka halaman Bulk Upload Reels Fanpage di Dashboard Profesional.'
        bulk_started = True


    if dry_run:
        return True, '__DRYRUN__'

    if bulk_started:
        log(46, f'Memicu upload {media_label} via Bulk upload reels...')
        time.sleep(10)
    else:
        finp = _find_file_input(page, timeout=25, prefer='image' if is_photo else 'video')
        if not finp:
            return False, ('Tidak menemukan input upload di composer post Fanpage. '
                           'Coba buka mode terlihat dan pastikan modal post baru benar-benar terbuka, bukan kolom komentar.')

        log(46, f'Mengunggah {media_label}...')
        try:
            finp.input(os.path.abspath(video_path))
        except Exception as e:
            return False, f'Gagal upload {media_label}: {e}'
        time.sleep(10)

    log(60, 'Mengisi caption...')
    if bulk_started:
        want_id = str(page_id or '').strip()
        url_ok = want_id in str(page.url or '') if want_id else True
        if page_id and not url_ok:
            log(26, f'Peringatan: asset_id tidak terdeteksi di URL saat isi caption. URL: {page.url}')
        if caption:
            if not _fill_bulk_upload_caption(page, caption):
                return False, 'Caption Bulk Upload tidak berhasil diisi. Upload dibatalkan supaya tidak publish caption kosong.'
        # Jika caption kosong, lanjut saja tanpa isi caption
    else:
        _fill_caption(page, caption)
    time.sleep(1)
    if bulk_started and publish_mode != 'schedule':
        _debug_dump(page, 'bulk_after_caption')
        ready_ok, ready_info = _wait_bulk_upload_ready(
            page, log, page_id=page_id, page_name=page_name,
            trust_context=trusted_page_context
        )
        if not ready_ok:
            return False, ready_info

    if not is_photo and not bulk_started:
        for _ in range(3):
            moved = _click_by_text(page, ['next', 'continue', 'berikutnya', 'lanjut'], timeout=4)
            if not moved:
                break
            time.sleep(2)

    scheduled = False
    if publish_mode == 'schedule' and schedule_time:
        # Tunggu copyright check selesai dulu sebelum set jadwal
        if bulk_started:
            _debug_dump(page, 'before_schedule')
            ready_ok, ready_info = _wait_bulk_upload_ready(
                page, log, page_id=page_id, page_name=page_name,
                trust_context=trusted_page_context
            )
            if not ready_ok:
                return False, ready_info
        scheduled = _set_datetime_facebook_composer(
            page, schedule_time, log, page_id=page_id,
            page_name=page_name, trust_context=trusted_page_context
        )
        if not scheduled:
            return False, (f'Jadwal {schedule_time} gagal diterapkan di Dashboard Profesional. '
                           'Upload dibatalkan supaya tidak publish langsung.')
    elif publish_mode == 'now':
        # _wait_bulk_upload_ready sudah dipanggil di atas untuk bulk_started (schedule mode).
        # Untuk mode 'now' + bulk, tunggu copyright check selesai di sini sebelum publish.
        # (Tidak double call karena blok di atas hanya jalan saat bulk_started=True AND
        #  kita baru tiba di sini, sedangkan blok sebelumnya sudah return jika tidak ready.)
        if bulk_started:
            ready_now_ok, ready_now_info = _wait_bulk_upload_ready(
                page, log, page_id=page_id, page_name=page_name,
                trust_context=trusted_page_context
            )
            if not ready_now_ok:
                return False, ready_now_info
        if post_delay > 0:
            log(85, f'Menunggu {post_delay} detik sebelum publish...')
            time.sleep(post_delay)

    log(88, 'Mengirim postingan Fanpage...')
    if bulk_started:
        if page_id and not _target_context_ok(page, page_id, page_name, trust_context=trusted_page_context):
            return False, _format_target_lock_error(_target_lock_info(page, page_id, page_name))
        published = _click_bulk_upload_save(page, timeout=40)
    elif scheduled:
        published = _click_by_text(page,
            ['schedule', 'jadwalkan', 'schedule post', 'jadwalkan postingan', 'save', 'simpan', 'done'], timeout=18)
    else:
        published = _click_by_text(page,
            ['publish', 'post', 'share', 'bagikan', 'kirim', 'terbitkan', 'publish now'], timeout=18)

    if not published:
        _debug_dump(page, 'submit_fail')
        return False, 'Tombol Publish/Schedule tidak ditemukan di composer post Fanpage.'
    time.sleep(8)

    if _check_facebook_rate_limit(page):
        return False, ('Facebook MENOLAK submit -- rate-limit/anti-spam ("You Can\'t Use This '
                        'Feature Right Now"). Post/jadwal TIDAK tersimpan. Tunggu beberapa jam '
                        'sebelum coba lagi (batasan level akun, ganti Fanpage tidak membantu).')

    sched_info = f'dijadwalkan {schedule_time}' if scheduled else 'dipublish langsung'
    return True, f'Fanpage {media_label} {sched_info} via Dashboard Profesional (perlu cek manual).'


def _set_datetime_meta_suite(page, schedule_time_str, log):
    """Isi field tanggal & jam di Meta Business Suite Content Scheduler.

    schedule_time_str format: 'YYYY-MM-DD HH:MM' (mis. '2026-08-15 09:00')
    Strategi: cari input date + input time di halaman dan isi via JS.
    """
    try:
        from datetime import datetime
        dt = datetime.strptime(schedule_time_str.strip(), '%Y-%m-%d %H:%M')
    except ValueError:
        log(70, f'Format jadwal tidak dikenali ({schedule_time_str}), skip jadwal.')
        return False

    log(70, f'Mengatur jadwal: {schedule_time_str}...')
    try:
        # Klik tombol "Schedule" / "Jadwalkan" untuk membuka date-time picker
        _click_by_text(page, ['schedule', 'jadwalkan', 'schedule for later', 'jadwalkan untuk nanti'], timeout=8)
        time.sleep(2)

        # Meta Suite kadang pakai input native 24-jam, kadang textbox 12-jam.
        # Coba keduanya lalu verifikasi bahwa field benar-benar terisi.
        date_str = dt.strftime('%m/%d/%Y')
        time_12 = dt.strftime('%I:%M %p')
        time_24 = dt.strftime('%H:%M')

        filled = page.run_js("""
            const dateVal = arguments[0];
            const time12 = arguments[1];
            const time24 = arguments[2];
            const inputs = Array.from(document.querySelectorAll('input'));
            let dateOk = false, timeOk = false;
            const setNativeValue = (inp, value) => {
                const nativeInput = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                nativeInput.set.call(inp, value);
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));
                inp.dispatchEvent(new Event('blur', { bubbles: true }));
            };
            for (const inp of inputs) {
                const t = (inp.type || '').toLowerCase();
                const ph = (inp.placeholder || '').toLowerCase();
                const lbl = (inp.getAttribute('aria-label') || '').toLowerCase();
                const marker = `${t} ${ph} ${lbl}`;
                if (!dateOk && (t === 'date' || marker.includes('date') || marker.includes('tanggal'))) {
                    setNativeValue(inp, dateVal);
                    dateOk = !!String(inp.value || '').trim();
                }
                if (!timeOk && (t === 'time' || marker.includes('time') || marker.includes('waktu') || marker.includes('jam'))) {
                    const prefer = t === 'time' ? time24 : time12;
                    setNativeValue(inp, prefer);
                    let val = String(inp.value || '').trim();
                    if (!val && prefer !== time24) {
                        setNativeValue(inp, time24);
                        val = String(inp.value || '').trim();
                    }
                    timeOk = !!val;
                }
            }
            return {dateOk, timeOk};
        """, date_str, time_12, time_24) or {}

        time.sleep(1)
        # Konfirmasi / tutup picker
        _click_by_text(page, ['done', 'ok', 'oke', 'confirm', 'save', 'apply', 'simpan'], timeout=4)
        time.sleep(1)
        return bool(filled.get('dateOk') and filled.get('timeOk'))
    except Exception as e:
        log(70, f'Gagal set jadwal: {e}')
        return False


def _open_meta_suite_for_page(page, page_id, log):
    """Buka Meta Business Suite dalam konteks Fanpage target.

    Alur:
    1. Navigasi ke MBS home dengan asset_id FP target — ini set context FP
    2. Dari homepage MBS yang sudah dalam konteks FP, klik tombol "Buat/Create"
       di sidebar untuk membuka content scheduler
    """
    want_id = str(page_id or '').strip()
    if not want_id:
        return False

    # Step 1: Buka MBS home dengan asset_id untuk set context FP
    log(18, f'Membuka Meta Business Suite untuk Fanpage {want_id}...')
    try:
        page.get(f'https://business.facebook.com/latest/home?asset_id={want_id}', timeout=35)
        time.sleep(8)
        cur_url = str(page.url or '')
        if 'business.facebook.com' not in cur_url.lower():
            return False
        if want_id not in cur_url:
            log(20, f'asset_id tidak ada di URL, URL: {cur_url[:80]}')
            return False
        log(22, f'Konteks MBS berhasil ke Fanpage {want_id}.')
    except Exception:
        return False

    # Step 2: Klik tombol "Buat" / "Create" di sidebar MBS
    # (ini membuka content scheduler dalam konteks FP yang sudah aktif)
    log(24, 'Mencari tombol Buat/Create di sidebar MBS...')
    try:
        clicked = page.run_js(r"""
            const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
            const all = Array.from(document.querySelectorAll(
                'a[href],div[role="button"],li,span[role="button"]'
            ));
            // Cari link "Buat" / "Create" yang menuju content_scheduler
            for (const n of all) {
                const href = (n.href || n.getAttribute('href') || '').toLowerCase();
                const txt = norm(n.innerText || n.getAttribute('aria-label') || '');
                if (href.includes('content_scheduler') || href.includes('create')) {
                    const r = n.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        n.scrollIntoView({block:'center'});
                        n.click();
                        return href || txt;
                    }
                }
                // Cari LI "Buat" / "Create" di sidebar nav
                if ((txt === 'buat' || txt === 'create') && n.tagName === 'LI') {
                    const a = n.querySelector('a[href]');
                    if (a) { a.click(); return a.getAttribute('href') || 'buat'; }
                }
            }
            return '';
        """) or ''

        if clicked:
            log(26, f'Klik create/buat: {clicked[:60]}')
            time.sleep(5)
            return True
    except Exception:
        pass

    # Fallback: tombol Buat/Create tidak ketemu -- langsung ke halaman COMPOSE
    # (META_SUITE_SCHEDULER_FB = content_scheduler/create/). PENTING: jangan pernah
    # pakai posts/scheduled_posts di sini -- itu halaman untuk MELIHAT jadwal yang
    # sudah ada, bukan compose, dan sering nampilin "Sorry, this content isn't
    # available right now" kalau diakses via hard-navigate begini. Konteks FP sudah
    # ke-set dari step 1 (asset_id di /latest/home), jadi cukup buka create/ polos.
    try:
        page.get(META_SUITE_SCHEDULER_FB, timeout=30)
        time.sleep(5)
        body = _norm_text(page.run_js("return document.body ? document.body.innerText : ''") or '')
        if "isn't available" in body or 'tidak tersedia' in body or 'content isn' in body:
            log(24, 'Halaman compose Meta Suite gagal dimuat (isn\'t available), coba lagi...')
            page.get(META_SUITE_SCHEDULER_FB, timeout=30)
            time.sleep(5)
            body = _norm_text(page.run_js("return document.body ? document.body.innerText : ''") or '')
            if "isn't available" in body or 'tidak tersedia' in body:
                return False
        return True
    except Exception:
        pass

    return False


def _open_meta_suite_add_media(page, log, video_path):
    log(34, 'Membuka pemilih media Meta Business Suite...')
    abs_path = os.path.abspath(video_path)
    # Klik tombol Add photo/video dulu — BARU set upload file setelah input muncul
    selectors = (
        'xpath://div[@role="button" and contains(.,"Add photo/video")]',
        'xpath://button[contains(.,"Add photo/video")]',
        'xpath://*[contains(@aria-label,"Add photo/video")]',
        'xpath://div[@role="button" and contains(.,"Photo/video")]',
        'xpath://button[contains(.,"Photo/video")]',
    )
    for sel in selectors:
        try:
            btn = page.ele(sel, timeout=3)
            if btn:
                btn.click()
                time.sleep(3)
                # Set file setelah input file sudah muncul di DOM
                finp = _find_file_input(page, timeout=10, prefer='video')
                if finp:
                    try:
                        finp.input(abs_path)
                    except Exception:
                        page.set.upload_files(abs_path)
                        finp.click()
                else:
                    page.set.upload_files(abs_path)
                time.sleep(5)
                return True
        except Exception:
            pass
    return False



def _click_meta_suite_button(page, labels, timeout=12, pick='last'):
    wanted = [str(x).strip().lower() for x in (labels or []) if str(x).strip()]
    end = time.time() + timeout
    while time.time() < end:
        for label in wanted:
            selectors = (
                f'xpath://button[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{label}")]',
                f'xpath://div[@role="button" and contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{label}")]',
                f'xpath://span[@role="button" and contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{label}")]',
                f'xpath://*[contains(translate(@aria-label,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{label}")]',
            )
            for sel in selectors:
                try:
                    els = page.eles(sel, timeout=2)
                except Exception:
                    els = None
                if not els:
                    continue
                seq = list(els)
                if pick == 'last':
                    seq = list(reversed(seq))
                for el in seq:
                    try:
                        el.click()
                        return label
                    except Exception:
                        pass
        time.sleep(1)
    return ''


def _close_meta_suite_popups(page, log=None):
    for labels in (['done'], ['close', 'tutup'], ['not now', 'lain kali']):
        clicked = _click_meta_suite_button(page, labels, timeout=2, pick='last')
        if clicked:
            if log:
                log(36, f'Menutup popup Meta Suite: {clicked}...')
            time.sleep(1)


def _wait_meta_suite_media_ready(page, log, timeout=420):
    log(46, 'Menunggu upload media di Meta Business Suite selesai...')
    end = time.time() + timeout
    last_pct = ''
    stable_rounds = 0
    while time.time() < end:
        _close_meta_suite_popups(page)
        try:
            info = page.run_js(r"""
                const txt = (document.body && document.body.innerText || '');
                const m = txt.match(/(\d{1,3})%/);
                return {
                    pct: m ? m[1] : '',
                    wait: /please wait for the media to upload/i.test(txt),
                    ready: /your video is safe to publish|no copyright issues were found/i.test(txt),
                    cantRead: /can't read files|couldn't be uploaded|gagal upload|tidak bisa diunggah/i.test(txt)
                };
            """) or {}
        except Exception:
            info = {}
        if info.get('cantRead'):
            return False, 'Meta Business Suite menolak media yang diunggah.'
        pct = str(info.get('pct') or '').strip()
        wait_flag = bool(info.get('wait'))
        ready_flag = bool(info.get('ready'))
        if ready_flag:
            return True, ''
        if pct:
            if pct != last_pct:
                log(48, f'Upload media diproses {pct}%...')
                last_pct = pct
                stable_rounds = 0
            else:
                stable_rounds += 1
            if pct == '100' and not wait_flag:
                return True, ''
        elif not wait_flag:
            return True, ''
        else:
            stable_rounds = 0
        if stable_rounds >= 18:
            return False, f'Upload media macet di sekitar {pct}% di Meta Business Suite.'
        time.sleep(5)
    return False, 'Upload media di Meta Business Suite terlalu lama atau belum selesai.'


def _advance_meta_suite_reel(page, log):
    _close_meta_suite_popups(page, log)
    for _ in range(3):
        if _meta_suite_has_share_screen(page):
            log(78, 'Melanjutkan ke langkah Share/Jadwal...')
            return True
        clicked = _click_meta_suite_button(page, ['next', 'continue', 'berikutnya', 'lanjut'], timeout=12, pick='last')
        if not clicked:
            break
        time.sleep(4)
        try:
            still_waiting = bool(page.run_js(r"""
                const txt = (document.body && document.body.innerText || '');
                return /(please wait for the media to upload|\d{1,3}%)/i.test(txt) &&
                       !/your video is safe to publish|no copyright issues were found/i.test(txt);
            """))
        except Exception:
            still_waiting = False
        if still_waiting:
            return False
    if _meta_suite_has_share_screen(page):
        log(78, 'Melanjutkan ke langkah Share/Jadwal...')
        return True
    return False



def _meta_suite_has_share_screen(page):
    try:
        return bool(page.run_js(r"""
            const txt = (document.body && document.body.innerText || '').toLowerCase();
            return txt.includes('scheduling options') ||
                   txt.includes('share now') ||
                   txt.includes('save as draft') ||
                   txt.includes('select a future date and time to publish your reel at.');
        """))
    except Exception:
        return False


def _meta_suite_submit_seen(page):
    try:
        return bool(page.run_js(r"""
            const txt = (document.body && document.body.innerText || '').toLowerCase();
            return txt.includes('video post processing') ||
                   txt.includes('once your video finishes processing, it will be published and you will be notified.');
        """))
    except Exception:
        return False


def _enable_meta_suite_schedule(page, log, timeout=15):
    log(72, 'Mengaktifkan opsi Schedule di Meta Business Suite...')
    end = time.time() + timeout
    while time.time() < end:
        try:
            state = page.run_js(r"""
                const buttons = Array.from(document.querySelectorAll('div[role="button"][aria-pressed],button[aria-pressed],span[role="button"][aria-pressed]'));
                const scheduleBtn = buttons.find(n => ((n.innerText||n.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim().toLowerCase()) === 'schedule');
                const shareNowBtn = buttons.find(n => ((n.innerText||n.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim().toLowerCase()) === 'share now');
                if (!scheduleBtn) return {found:false, pressed:false};
                if (scheduleBtn.getAttribute('aria-pressed') === 'true') return {found:true, pressed:true};
                scheduleBtn.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(type => scheduleBtn.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                scheduleBtn.click();
                return {
                    found:true,
                    pressed: scheduleBtn.getAttribute('aria-pressed') === 'true',
                    shareNowPressed: shareNowBtn ? shareNowBtn.getAttribute('aria-pressed') : null,
                };
            """) or {}
        except Exception:
            state = {}
        if state.get('pressed'):
            time.sleep(1)
            return True
        time.sleep(1)
    return False


def _open_meta_suite_create_post(page, log):
    log(28, 'Membuka composer Create Post di Meta Business Suite...')
    try:
        clicked = page.run_js(r"""
            const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
            const keywords = [
                'create post', 'create a post', 'buat postingan',
                'make a post', 'write a post', 'what\'s on your mind',
                'create reel', 'buat reel',
                'add photo/video', 'photo/video',
            ];
            const nodes = Array.from(document.querySelectorAll(
                'div[role="button"],button,a[role="button"],span[role="button"],a[href]'
            ));
            let best = null;
            for (const n of nodes) {
                const txt = norm((n.getAttribute('aria-label')||'') + ' ' + (n.innerText||''));
                if (!txt) continue;
                const r = n.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                if (r.top < 0 || r.top > window.innerHeight * 0.9) continue;
                // Abaikan nav items
                if (n.closest('nav,[role="navigation"]')) continue;
                for (const kw of keywords) {
                    if (txt === kw || txt.startsWith(kw) || txt.includes(kw)) {
                        if (!best || r.top < best.r.top) best = { el: n, r };
                        break;
                    }
                }
            }
            if (!best) return '';
            best.el.scrollIntoView({block:'center'});
            ['pointerdown','mousedown','mouseup','click'].forEach(type =>
                best.el.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window}))
            );
            if (typeof best.el.click === 'function') best.el.click();
            return best.el.innerText || best.el.getAttribute('aria-label') || 'clicked';
        """)
        if clicked:
            end = time.time() + 20
            while time.time() < end:
                try:
                    ready = page.run_js("""
                        const txt = (document.body && document.body.innerText || '').toLowerCase();
                        return location.href.includes('/composer/') ||
                               location.href.includes('content_scheduler') ||
                               txt.includes('post to ') ||
                               txt.includes('add photo/video') ||
                               txt.includes('photo/video') ||
                               txt.includes('schedule') ||
                               !!document.querySelector('[contenteditable="true"],[role="textbox"]');
                    """)
                    if ready:
                        time.sleep(2)
                        return True
                except Exception:
                    pass
                time.sleep(1)
            return True
    except Exception:
        pass
    return False


def _meta_suite_target_snapshot(page):
    try:
        return page.run_js(r"""
            const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || '')) || '')
                .replace(/\s+/g, ' ').trim();
            const href = location.href || '';
            const bodyText = textOf(document.body).slice(0, 12000);
            let activeText = '';
            let activeHref = '';
            let postToText = '';
            const labels = Array.from(document.querySelectorAll('label,div,span,h1,h2,h3,h4'));
            const postToNode = labels.find(n => textOf(n).toLowerCase() === 'post to');
            if (postToNode) {
                const scope = postToNode.closest('[role="dialog"],form,section,div') || postToNode.parentElement || postToNode;
                postToText = textOf(scope);
                const picker = scope.querySelector('[role="button"],button,[role="combobox"],a[href]');
                if (picker) {
                    activeText = textOf(picker);
                    activeHref = picker.href || '';
                }
            }
            if (!activeText) {
                const nodes = Array.from(document.querySelectorAll('[role="button"],button,[role="combobox"],a[href],span,div'));
                const candidate = nodes.find(n => {
                    const txt = textOf(n).toLowerCase();
                    return txt.startsWith('post to ') || txt.includes(' post to ');
                });
                if (candidate) {
                    activeText = textOf(candidate);
                    activeHref = candidate.href || '';
                    postToText = activeText;
                }
            }
            return { href, bodyText, activeText, activeHref, postToText };
        """) or {}
    except Exception:
        return {}


def _meta_suite_target_matches(snapshot, page_id, page_name=''):
    page_id = str(page_id or '').strip().lower()
    page_name = _norm_text(page_name)
    hay = ' '.join([
        str(snapshot.get('href') or ''),
        str(snapshot.get('bodyText') or ''),
        str(snapshot.get('activeHref') or ''),
        str(snapshot.get('activeText') or ''),
        str(snapshot.get('postToText') or ''),
    ]).lower()
    if page_id and page_id in hay:
        return True
    if page_name and page_name in _norm_text(hay):
        return True
    return False


def _open_meta_suite_target_picker(page):
    try:
        opened = page.run_js(r"""
            const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || '')) || '')
                .replace(/\s+/g, ' ').trim().toLowerCase();
            const labels = Array.from(document.querySelectorAll('label,div,span,h1,h2,h3,h4'));
            const postToNode = labels.find(n => textOf(n) === 'post to');
            if (postToNode) {
                const scope = postToNode.closest('[role="dialog"],form,section,div') || postToNode.parentElement || postToNode;
                const picker = scope.querySelector('[role="button"],button,[role="combobox"],a[href]');
                if (picker) {
                    picker.scrollIntoView({block:'center'});
                    ['pointerdown','mousedown','mouseup','click'].forEach(type => picker.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                    return true;
                }
            }
            const fallback = Array.from(document.querySelectorAll('[role="button"],button,[role="combobox"],a[href],span,div')).find(n => {
                const txt = textOf(n);
                return txt.startsWith('post to ') || txt.includes(' post to ');
            });
            if (fallback) {
                fallback.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(type => fallback.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                return true;
            }
            return false;
        """)
        return bool(opened)
    except Exception:
        return False


def _pick_meta_suite_target(page, page_id, page_name, log, timeout=20):
    want_id = str(page_id or '').strip().lower()
    want_name = _norm_text(page_name)
    end = time.time() + timeout
    while time.time() < end:
        try:
            result = page.run_js(r"""
                const wantId = arguments[0];
                const wantName = arguments[1];
                const textOf = (n) => ((n && (n.innerText || n.textContent || n.getAttribute('aria-label') || n.getAttribute('title') || '')) || '')
                    .replace(/\s+/g, ' ').trim();
                const nodes = Array.from(document.querySelectorAll(
                    '[role="option"],[role="menuitem"],[role="listbox"] [role="button"],button,a[href],div[role="button"],span[role="button"],div,span'
                ));
                let best = null;
                for (const n of nodes) {
                    const txt = textOf(n);
                    const low = txt.toLowerCase();
                    const href = (n.href || n.getAttribute('href') || '').toLowerCase();
                    if (!txt && !href) continue;
                    if (low.includes('meta business suite') || low === 'post to') continue;
                    let score = -999;
                    if (wantId && (href.includes(wantId) || low.includes(wantId))) score = 200;
                    if (wantName && low.includes(wantName)) score = Math.max(score, 180);
                    if (score < 0) continue;
                    const r = n.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (!best || score > best.score) best = { node: n, score, txt, href };
                }
                if (!best) return { clicked:false };
                best.node.scrollIntoView({block:'center'});
                ['pointerdown','mousedown','mouseup','click'].forEach(type => best.node.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
                if (typeof best.node.click === 'function') best.node.click();
                return { clicked:true, txt: best.txt, href: best.href };
            """, want_id, want_name) or {}
            if result.get('clicked'):
                picked = (result.get('txt') or result.get('href') or '').strip()
                if picked:
                    log(38, f'Memilih target Fanpage di composer: {picked[:80]}...')
                time.sleep(3)
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _ensure_meta_suite_page_target(page, page_id, page_name, log):
    if not page_id:
        return True
    snapshot = _meta_suite_target_snapshot(page)
    if _meta_suite_target_matches(snapshot, page_id, page_name):
        return True
    log(34, f'Memastikan target composer sesuai Fanpage {page_name or page_id}...')
    picker_opened = _open_meta_suite_target_picker(page)
    time.sleep(2)
    if picker_opened and _pick_meta_suite_target(page, page_id, page_name, log):
        snapshot = _meta_suite_target_snapshot(page)
        if _meta_suite_target_matches(snapshot, page_id, page_name):
            return True
    snapshot = _meta_suite_target_snapshot(page)
    if _meta_suite_target_matches(snapshot, page_id, page_name):
        return True
    if page_name:
        page_name_norm = _norm_text(page_name)
        body_norm = _norm_text(snapshot.get('bodyText') or '')
        # Fallback aman: beberapa layout Meta Suite belum menampilkan komponen
        # "Post to", tapi nama halaman aktif sudah jelas di sidebar/header.
        if page_name_norm and page_name_norm in body_norm:
            log(36, f'Target Fanpage dikonfirmasi dari konteks halaman Meta Suite: {page_name}...')
            return True
    return False


def _upload_via_meta_suite(page, video_path, caption, schedule_time, target, page_id, page_name, dry_run, log, channel='facebook', publish_mode='schedule', post_delay=0):
    """Upload via Meta Business Suite.
    
    publish_mode: 'schedule' (default) atau 'now' (publish langsung)
    post_delay: jeda dalam detik sebelum publish (hanya untuk mode 'now')
    """
    """Upload & jadwalkan konten via Meta Business Suite Content Scheduler.

    channel: 'facebook' atau 'instagram'
    Digunakan saat schedule_time terisi.
    """
    is_photo = _is_image(video_path)
    media_label = 'foto' if is_photo else 'video'

    # Navigasi ke Meta Business Suite
    if channel == 'instagram':
        suite_url = META_SUITE_SCHEDULER_IG
        log(22, 'Membuka Meta Business Suite (Instagram scheduler)...')
        page.get(suite_url, timeout=35)
        time.sleep(6)
    else:
        suite_url = META_SUITE_SCHEDULER_FB
        opened_from_page = False
        if page_id and target == 'page':
            opened_from_page = _open_meta_suite_for_page(page, page_id, log)
            if opened_from_page:
                _open_meta_suite_create_post(page, log)
                time.sleep(3)
        if not opened_from_page:
            log(22, 'Membuka Meta Business Suite (Facebook scheduler)...')
            page.get(suite_url, timeout=35)
            time.sleep(6)
            _open_meta_suite_create_post(page, log)
        # Validasi target FP — jika sudah switch via combobox, percaya saja.
        # Hanya validasi ketat jika belum switch via combobox.
        if page_id and not opened_from_page:
            if not _ensure_meta_suite_page_target(page, page_id, page_name, log):
                return False, (f'Composer Meta Suite tidak berhasil dikunci ke Fanpage '
                               f'{page_name or page_id}. Batal upload supaya tidak salah target.')
        elif page_id and opened_from_page:
            # PENTING: composer bisa default ke Fanpage LAIN (mis. Fanpage pertama
            # di daftar Business Manager) kalau konteks asset_id tidak ke-inherit
            # dengan benar dari step sebelumnya -- jangan pernah asumsikan target
            # sudah benar tanpa verifikasi ulang setelah retry (dulu di sini upload
            # tetap lanjut walau target masih salah, bisa posting ke FP yang salah).
            snapshot = _meta_suite_target_snapshot(page)
            if not _meta_suite_target_matches(snapshot, page_id, page_name):
                log(28, f'Target composer belum sesuai Fanpage {page_name or page_id}, coba switch manual...')
                _open_meta_suite_target_picker(page)
                time.sleep(2)
                _pick_meta_suite_target(page, page_id, page_name, log)
                time.sleep(3)
                snapshot = _meta_suite_target_snapshot(page)
                if not _meta_suite_target_matches(snapshot, page_id, page_name):
                    return False, (f'Composer Meta Suite tidak berhasil dikunci ke Fanpage '
                                   f'{page_name or page_id}. Batal upload supaya tidak salah target.')

    log(32, f'Menyiapkan upload {media_label}...')
    if dry_run:
        return True, '__DRYRUN__'

    uploaded = False
    if channel == 'facebook' and target == 'page':
        uploaded = _open_meta_suite_add_media(page, log, video_path)
    if not uploaded:
        for texts in (
            ['add photo/video', 'photo/video', 'foto/video'],
            ['add photo', 'add video', 'tambahkan foto', 'tambahkan video', 'upload'],
            ['add media', 'photo or video', 'foto atau video'],
        ):
            _click_by_text(page, texts, timeout=6)
            time.sleep(1)

        finp = _find_file_input(page, timeout=30, prefer='image' if is_photo else 'video')
        if not finp:
            return False, (f'Tidak menemukan input upload di Meta Business Suite. '
                           'Pastikan sudah login Facebook dan memiliki akses ke halaman.')

        log(42, f'Mengunggah {media_label}...')
        try:
            finp.input(os.path.abspath(video_path))
        except Exception as e:
            return False, f'Gagal upload {media_label}: {e}'
        time.sleep(8)

    ready_ok, ready_info = _wait_meta_suite_media_ready(page, log)
    if not ready_ok:
        return False, ready_info

    log(55, 'Mengisi caption...')
    _fill_caption(page, caption)
    time.sleep(1)

    if channel == 'facebook' and target == 'page' and not is_photo:
        if not _advance_meta_suite_reel(page, log):
            return False, 'Tombol Next reel tidak ditemukan di Meta Business Suite.'

    # Publish mode: schedule atau now
    if publish_mode == 'schedule' and schedule_time:
        # Mode schedule: set jadwal lalu publish
        if channel == 'facebook' and target == 'page' and not is_photo:
            _enable_meta_suite_schedule(page, log)
        scheduled = _set_datetime_meta_suite(page, schedule_time, log)
        _close_meta_suite_popups(page, log)

        log(88, 'Menjadwalkan post...')
        if not scheduled:
            return False, (f'Jadwal {schedule_time} gagal diterapkan di Meta Business Suite. '
                           'Upload dibatalkan supaya tidak publish langsung.')

        published = (_click_meta_suite_button(page,
            ['schedule', 'jadwalkan', 'schedule post', 'jadwalkan postingan', 'save', 'simpan'], timeout=20, pick='last')
            or _click_by_text(page,
            ['schedule', 'jadwalkan', 'schedule post', 'jadwalkan postingan', 'save', 'simpan'], timeout=10))

        if not published:
            return False, 'Tombol Schedule tidak ditemukan di Meta Business Suite.'
        time.sleep(6)
        
        ch_label = 'Instagram' if channel == 'instagram' else 'Facebook'
        sched_info = f'dijadwalkan {schedule_time}'
        if _meta_suite_submit_seen(page):
            _close_meta_suite_popups(page, log)
        return True, f'{ch_label} {media_label} {sched_info} via Meta Business Suite (perlu cek manual).'
    
    else:
        # Mode now: publish langsung dengan optional delay
        if post_delay > 0:
            log(85, f'Menunggu {post_delay} detik sebelum publish...')
            time.sleep(post_delay)
        
        log(88, 'Publish sekarang...')
        published = (_click_meta_suite_button(page,
            ['post', 'publish', 'share', 'publish now', 'bagikan', 'terbitkan', 'kirim'], timeout=20, pick='last')
            or _click_by_text(page,
            ['post', 'publish', 'share', 'publish now', 'bagikan', 'terbitkan', 'kirim'], timeout=10))

        if not published:
            return False, 'Tombol Publish tidak ditemukan di Meta Business Suite.'
        time.sleep(6)
        
        ch_label = 'Instagram' if channel == 'instagram' else 'Facebook'
        if _meta_suite_submit_seen(page):
            _close_meta_suite_popups(page, log)
        return True, f'{ch_label} {media_label} dipublish langsung via Meta Business Suite (perlu cek manual).'


def _bulk_item_set_schedule(page, idx, schedule_time_str, log):
    """Set 'Schedule for later' + tanggal/jam utk item ke-`idx` (0-based) di
    halaman Bulk Upload Reels MULTI-ITEM (banyak video sekaligus dlm 1 list).

    Beda dari _set_bulk_upload_schedule (itu utk alur SATU item / modal
    step-by-step lama) -- UI multi-item pakai <label role="combobox"> per baris
    (isi teks "Publish now") + panel kalender INLINE saat combobox diklik dan
    "Schedule for later" dipilih dari dropdown-nya (bukan modal terpisah).
    """
    try:
        from datetime import datetime
        dt = datetime.strptime(schedule_time_str.strip(), '%Y-%m-%d %H:%M')
    except ValueError:
        log(70, f'Format jadwal tidak dikenali ({schedule_time_str}).')
        return False

    opened = page.run_js(r"""
        const idx = arguments[0];
        const boxes = Array.from(document.querySelectorAll('label[role="combobox"]'));
        const box = boxes[idx];
        if (!box) return false;
        box.scrollIntoView({block:'center'});
        ['pointerdown','mousedown','mouseup','click'].forEach(t => box.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
        return true;
    """, idx)
    if not opened:
        log(70, f'Combobox Publish/Schedule item ke-{idx + 1} tidak ketemu.')
        return False
    time.sleep(1.5)

    picked = page.run_js(r"""
        const norm = (s) => (s||'').replace(/\s+/g,' ').trim();
        const opts = Array.from(document.querySelectorAll('[role="option"],[role="menuitem"]'));
        const target = opts.find(o => {
            const t = norm(o.innerText||o.textContent||'');
            return t === 'Schedule for later' || t === 'Jadwalkan untuk nanti';
        });
        if (!target) return false;
        target.scrollIntoView({block:'center'});
        ['pointerdown','mousedown','mouseup','click'].forEach(t => target.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
        return true;
    """)
    if not picked:
        log(70, f'Opsi "Schedule for later" item ke-{idx + 1} tidak ketemu.')
        return False
    time.sleep(1.5)

    # Navigasi kalender ke bulan yang benar (klik panah "next month" berkali-kali
    # sampai judul bulan cocok).
    month_target = dt.strftime('%B %Y')
    for _ in range(12):
        cur_month = page.run_js(r"""
            const norm = (s) => (s||'').replace(/\s+/g,' ').trim();
            const h = Array.from(document.querySelectorAll('div,span')).find(n =>
                n.children.length === 0 && /^[A-Za-z]+ \d{4}$/.test(norm(n.innerText||''))
            );
            return h ? norm(h.innerText) : '';
        """) or ''
        if cur_month == month_target:
            break
        page.run_js(r"""
            const btns = Array.from(document.querySelectorAll('[role="button"],button'));
            const next = btns.find(b => (b.getAttribute('aria-label')||'').toLowerCase().includes('next month'));
            if (next) { ['pointerdown','mousedown','mouseup','click'].forEach(t => next.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); }
        """)
        time.sleep(0.8)

    # Klik tanggal. PENTING: grid kalender ikut nampilin tanggal ekor bulan
    # sebelum/sesudahnya (di-grey/disabled) yang teksnya bisa SAMA dgn tanggal
    # target bulan ini (mis. sama-sama "28") -- filter cell yg aria-disabled,
    # dan dari sisa kandidat ambil yang PALING TERAKHIR di urutan DOM (overflow
    # bulan sebelumnya selalu di awal grid, bulan target selalu setelahnya).
    day_str = str(dt.day)
    day_clicked = page.run_js(r"""
        const day = arguments[0];
        const norm = (s) => (s||'').replace(/\s+/g,' ').trim();
        const cells = Array.from(document.querySelectorAll('div,span,td,button')).filter(n =>
            n.children.length === 0 && norm(n.innerText||n.textContent||'') === day
        );
        const valid = cells.filter(c => {
            const host = c.closest('[aria-disabled],[role="gridcell"]') || c;
            return host.getAttribute('aria-disabled') !== 'true';
        });
        const pool = valid.length ? valid : cells;
        const pick = pool[pool.length - 1];
        if (!pick) return false;
        pick.scrollIntoView({block:'center'});
        ['pointerdown','mousedown','mouseup','click'].forEach(t => pick.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
        return true;
    """, day_str)
    if not day_clicked:
        log(70, f'Tanggal {dt.day} item ke-{idx + 1} tidak ketemu di kalender.')
        return False
    time.sleep(0.8)

    # Field jam berformat teks 12-jam (mis. "6:00 AM") -- identifikasi via nilai
    # SAAT INI yang sudah cocok pola jam, jangan asal ambil input[type=text] pertama.
    time_str = dt.strftime('%I:%M %p').lstrip('0')
    time_set = page.run_js(r"""
        const val = arguments[0];
        const inputs = Array.from(document.querySelectorAll('input[type="text"]')).filter(n => {
            const r = n.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && /\d{1,2}:\d{2}\s*(AM|PM)/i.test(n.value || '');
        });
        const inp = inputs[0];
        if (!inp) return false;
        inp.focus();
        const proto = window.HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(inp, val); else inp.value = val;
        inp.dispatchEvent(new Event('input', {bubbles:true}));
        inp.dispatchEvent(new Event('change', {bubbles:true}));
        try { inp.blur(); } catch (e) {}
        return true;
    """, time_str)
    if not time_set:
        log(70, f'Field jam item ke-{idx + 1} tidak ketemu.')
        return False
    time.sleep(0.5)

    applied = page.run_js(r"""
        const norm = (s) => (s||'').replace(/\s+/g,' ').trim();
        const btns = Array.from(document.querySelectorAll('[role="button"],button'));
        const apply = btns.find(b => {
            const t = norm(b.innerText||b.textContent||'');
            return t === 'Apply' || t === 'Terapkan';
        });
        if (!apply) return false;
        apply.scrollIntoView({block:'center'});
        ['pointerdown','mousedown','mouseup','click'].forEach(t => apply.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
        return true;
    """)
    if not applied:
        log(70, f'Tombol Apply item ke-{idx + 1} tidak ketemu.')
        return False
    time.sleep(1)
    return True


def _bulk_upload_multi(page, page_id, page_name, rows, dry_run, log):
    """Upload BANYAK video sekaligus ke satu Fanpage (true bulk, bukan satu-satu
    buka dashboard+composer per video) lewat Bulk Upload Reels Facebook -- file
    input di halaman ini punya atribut `multiple`, jadi semua file dikirim dalam
    SATU panggilan .input([...]) (DOM.setFileInputFiles via CDP, native).

    rows: list of dict {video_path, caption, schedule_time}. schedule_time format
    'YYYY-MM-DD HH:MM'; kalau kosong/None utk suatu item, item itu TETAP di mode
    default "Publish now" Facebook.

    PENTING -- WAJIB dipahami sebelum dipakai: tombol "Save" di halaman ini
    berlaku utk SEMUA item SEKALIGUS. Item yang TIDAK diberi schedule_time akan
    PUBLISH LANGSUNG begitu Save diklik (itu perilaku asli Facebook, bukan bug
    GAMA) -- caller WAJIB isi schedule_time utk setiap item yang tidak boleh
    langsung tayang.
    """
    want_id = str(page_id or '').strip()
    if not re.fullmatch(r'\d+', want_id or ''):
        log(20, 'Batal: page_id Fanpage harus berupa angka asset_id.')
        return False, 'page_id tidak valid.'
    if not rows:
        return False, 'Tidak ada video untuk di-upload.'

    if not _active_actor_matches(page, want_id, page_name):
        ok_dash = _open_page_professional_dashboard(page, want_id, page_name, log)
        if not ok_dash:
            return False, f'Tidak berhasil mengunci Fanpage {page_name or want_id} sebelum bulk upload.'

    url = f'https://www.facebook.com/professional_dashboard/bulk_upload/?asset_id={want_id}'
    log(20, f'Membuka Bulk Upload Reels Fanpage {page_name or want_id}...')
    page.get(url, timeout=40)
    time.sleep(6)
    if want_id not in str(page.url or ''):
        return False, f'Gagal membuka Bulk Upload Reels untuk Fanpage {page_name or want_id}.'

    if dry_run:
        return True, '__DRYRUN__'

    abs_paths = [os.path.abspath(r['video_path']) for r in rows]
    finp = _find_file_input(page, timeout=15, prefer='video')
    if not finp:
        return False, 'Input file Bulk Upload Reels tidak ketemu.'
    try:
        finp.input(abs_paths)
    except Exception as e:
        return False, f'Gagal set {len(abs_paths)} file sekaligus: {e}'

    total = len(rows)
    log(30, f'Menunggu {total} video selesai upload & cek copyright Facebook...')
    end = time.time() + max(60, total * 20)
    good = 0
    while time.time() < end:
        try:
            txt = (page.run_js("return (document.body && document.body.innerText || '')") or '').lower()
        except Exception:
            txt = ''
        good = txt.count('good to go') + txt.count('siap diposting')
        failed = txt.count('fail to upload') + txt.count('gagal diunggah')
        if failed:
            return False, (f'{failed} video gagal di-upload Facebook (bisa jadi format tidak '
                            f'didukung, atau Facebook membatasi upload beruntun -- coba lagi nanti). Batal.')
        if good >= total:
            break
        log(30 + int(20 * good / max(total, 1)), f'Upload+cek copyright: {good}/{total} selesai...')
        time.sleep(6)
    if good < total:
        return False, f'Timeout menunggu upload -- baru {good}/{total} video selesai diproses Facebook.'

    log(55, f'Semua {total} video siap, mengisi caption & jadwal per item...')
    for idx, row in enumerate(rows):
        caption = (row.get('caption') or '').strip()
        schedule_time = (row.get('schedule_time') or '').strip()
        pct = 55 + int(35 * (idx + 1) / total)

        if caption:
            filled = page.run_js(r"""
                const idx = arguments[0];
                const caption = arguments[1];
                const boxes = Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(n => {
                    const r = n.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                const box = boxes[idx];
                if (!box) return false;
                box.scrollIntoView({block:'center'});
                box.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, caption);
                box.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            """, idx, caption)
            if not filled:
                log(pct, f'[{idx + 1}/{total}] Gagal isi caption item ini, lanjut...')

        if schedule_time:
            ok = _bulk_item_set_schedule(page, idx, schedule_time, log)
            if not ok:
                return False, (f'Gagal set jadwal item ke-{idx + 1} ({row.get("video_path")}). '
                                f'Batal supaya tidak ada yang salah jadwal/ke-publish tak sengaja.')
            log(pct, f'[{idx + 1}/{total}] Jadwal diset: {schedule_time}')
        else:
            log(pct, f'[{idx + 1}/{total}] Caption diisi (tanpa jadwal -> Publish now saat Save).')

    log(92, 'Menyimpan semua item (Save)...')
    saved = _click_bulk_upload_save(page, timeout=40)
    if not saved:
        return False, 'Tombol Save Bulk Upload tidak ketemu/tidak bisa diklik.'
    time.sleep(5)
    if _check_facebook_rate_limit(page):
        return False, ('Facebook MENOLAK submit -- rate-limit/anti-spam ("You Can\'t Use This '
                        'Feature Right Now"). TIDAK ADA video yang tersimpan/terjadwal. '
                        'Tunggu beberapa jam sebelum coba lagi (ini batasan level akun, ganti '
                        'Fanpage tidak membantu).')
    return True, f'{total} video diproses via Bulk Upload (WAJIB cek manual di Facebook utk pastikan semua status benar).'


def _upload_one(page, item, target, page_id, page_name, dry_run, log):
    video_path = item.get('video_path') or ''
    caption = item.get('caption') or ''
    schedule_time = _resolve_schedule_time(item)
    publish_mode = (item.get('publish_mode') or 'schedule').lower()  # 'now' | 'schedule'
    post_delay = int(item.get('post_delay') or 0)  # detik jeda sebelum publish (mode now)

    # Group posting — video_path boleh kosong (post teks saja)
    if target == 'group':
        group_id = str(item.get('group_id') or page_id or '').strip()
        group_name = str(item.get('group_name') or page_name or '').strip()
        item['post_delay'] = post_delay
        return _upload_to_group(page, item, group_id, group_name, dry_run, log)

    if video_path and not os.path.isfile(video_path):
        return False, f'File media tidak ditemukan: {video_path}'

    if not video_path:
        return False, 'File media wajib untuk target personal/page.'

    is_photo = _is_image(video_path)
    if not is_photo:
        video_path, _ = _prepare_facebook_video(video_path, log)
    media_label = 'foto' if is_photo else 'video'

    # FP pakai Dashboard Profesional (BUKAN Meta Business Suite) -- terbukti live
    # (26 Jul) Meta Suite composer TIDAK PERNAH menghormati asset_id/context Fanpage
    # yang dipilih, selalu default ke Fanpage lain milik akun (risiko salah posting).
    # Dashboard Profesional + switch profil manual (_try_switch_dp_profile) terbukti
    # reliable mengunci Fanpage yang benar sebelum posting.
    if target == 'page' and page_id:
        return _upload_via_professional_dashboard(
            page, video_path, caption, schedule_time,
            page_id, page_name, dry_run, log,
            publish_mode=publish_mode, post_delay=post_delay
        )

    if schedule_time:
        return _upload_via_meta_suite(
            page, video_path, caption, schedule_time,
            target, page_id, page_name, dry_run, log, channel='facebook'
        )

    _open_composer(page, target, page_id, log, is_photo=is_photo)

    log(35, f'Mencari input file ({media_label})...')
    finp = _find_file_input(page, timeout=30, prefer='image' if is_photo else 'video')
    if not finp:
        return False, (f'Tidak menemukan input upload {media_label}. Kemungkinan tampilan '
                       'Facebook berubah atau composer tidak tersedia untuk akun ini.')

    if dry_run:
        return True, '__DRYRUN__'

    log(45, f'Mengunggah {media_label}...')
    try:
        finp.input(os.path.abspath(video_path))
    except Exception as e:
        return False, f'Gagal set file {media_label}: {e}'
    time.sleep(8)

    log(60, 'Mengisi caption...')
    _fill_caption(page, caption)
    time.sleep(1)

    if is_photo:
        log(85, 'Posting foto...')
        published = _click_by_text(page, ['post', 'kirim', 'publish', 'bagikan', 'share', 'terbitkan'], timeout=15)
    else:
        log(75, 'Melanjutkan (Next)...')
        for _ in range(3):
            if _click_by_text(page, ['next', 'berikutnya', 'lanjut'], timeout=6):
                time.sleep(3)
            else:
                break
        log(88, 'Publish...')
        published = _click_by_text(page, ['publish', 'share', 'post', 'bagikan', 'kirim', 'terbitkan'], timeout=12)
    if not published:
        return False, f'Tombol Publish/Post tidak ditemukan ({media_label}).'
    time.sleep(8)

    ok = True
    try:
        ok = not bool(page.run_js("""
            return !!document.querySelector('input[type=file]');
        """))
    except Exception:
        pass
    label_result = 'Foto' if is_photo else 'Reels'
    return (True, f'{label_result} terpublish (perlu cek manual di Facebook).') if ok else (False, 'Tidak yakin terpublish - cek manual di Facebook.')


def run_job(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    target = (job.get('target') or 'personal').lower()
    page_id = (job.get('page_id') or '').strip()
    page_name = ' '.join(str(job.get('page_name') or '').strip().split())
    invisible = bool(job.get('invisible'))
    state = {'pct': 0, 'message': 'Menyiapkan Facebook...', 'done': False,
             'success': False, 'error': None}

    def log(pct, msg):
        state['pct'] = pct
        state['message'] = msg
        write_status(status_path, **state)

    log(5, 'Membuka profil Chrome...')

    if not os.path.isdir(profile_path):
        state.update(done=True, error='Folder profil tidak ditemukan.')
        write_status(status_path, **state)
        return 1

    try:
        page = _connect(profile_path, headless=invisible)
    except Exception as e:
        state.update(done=True, error=f'Gagal membuka Chrome: {e}')
        write_status(status_path, **state)
        return 1

    try:
        log(15, 'Memeriksa sesi login Facebook...')
        if not _is_logged_in(page):
            state.update(done=True, error='Profil belum login Facebook. Buka shortcut '
                                          'Facebook di kartu profil lalu login dulu.')
            write_status(status_path, **state)
            return 1

        mode = (job.get('mode') or 'single').lower()
        if mode == 'batch':
            rows = job.get('rows') or []
            total = len(rows)
            state['rows'] = [{'title': (r.get('caption') or os.path.basename(r.get('video_path') or f'video {i+1}'))[:80],
                              'status': 'antre'} for i, r in enumerate(rows)]
            success = 0
            for i, item in enumerate(rows):
                state['rows'][i]['status'] = 'proses'
                base = int(i / total * 100)

                def rlog(pct, msg, _b=base, _i=i):
                    state['pct'] = min(99, _b + int(pct / total))
                    state['message'] = f'[{_i+1}/{total}] {msg}'
                    write_status(status_path, **state)

                try:
                    if i > 0:
                        # Jeda antar upload (pakai post_delay dari job jika ada)
                        delay_sec = int(job.get('post_delay') or 6)
                        time.sleep(max(delay_sec, 6))
                    row_page_id = (item.get('page_id') or page_id or '').strip()
                    row_page_name = ' '.join(str(item.get('page_name') or page_name or '').strip().split())
                    # Propagate publish_mode dan post_delay ke tiap row
                    if 'publish_mode' not in item:
                        item['publish_mode'] = job.get('publish_mode') or 'schedule'
                    if 'post_delay' not in item:
                        item['post_delay'] = job.get('post_delay') or 0
                    ok, info = _upload_one(page, item, target, row_page_id, row_page_name, job.get('dry_run'), rlog)
                except Exception as e:
                    ok, info = False, str(e)
                    if 'disconnected' in str(e).lower():
                        try:
                            page.quit()
                        except Exception:
                            pass
                state['rows'][i]['status'] = 'sukses' if ok else f'gagal: {str(info)[:50]}'
                if ok:
                    success += 1
                write_status(status_path, **state)

                fatal_page_state = (
                    not ok and target == 'page' and (
                        'Tidak menemukan composer post Fanpage' in str(info)
                        or 'Tidak menemukan input upload di composer post Fanpage' in str(info)
                        or 'Tombol Publish/Schedule tidak ditemukan di composer post Fanpage' in str(info)
                    )
                )
                if fatal_page_state:
                    for j in range(i + 1, total):
                        state['rows'][j]['status'] = 'dibatalkan: composer Fanpage tidak tersedia'
                    state.update(
                        pct=100,
                        done=True,
                        success=(success > 0),
                        message=f'Dihentikan di item {i+1}/{total}: composer Fanpage tidak tersedia.',
                        error=('Composer post Fanpage tidak tersedia pada page ini. '
                               'Batch dihentikan supaya tidak retry gagal berulang di kondisi yang sama.')
                    )
                    write_status(status_path, **state)
                    return 0 if state['success'] else 1

                if i < total - 1:
                    time.sleep(6)
            state.update(pct=100, done=True, success=(success > 0),
                         message=f'Selesai: {success}/{total} berhasil.')
            if success == 0:
                state['error'] = 'Semua upload gagal.'
        else:
            ok, info = _upload_one(page, job, target, page_id, page_name, job.get('dry_run'), log)
            if job.get('dry_run') and info == '__DRYRUN__':
                state.update(pct=100, done=True, success=True,
                             message='Dry run OK — login Facebook valid & composer siap, tidak ada upload.')
            elif ok:
                state.update(pct=100, done=True, success=True, message=info)
            else:
                state.update(done=True, success=False, error=info)
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_list_pages(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    headless = bool(job.get('headless', False))
    state = {'pct': 10, 'message': 'Membuka profil...', 'done': False,
             'success': False, 'error': None, 'pages': []}
    write_status(status_path, **state)
    try:
        page = _connect(profile_path, headless=headless)
    except Exception as e:
        state.update(done=True, error=f'Gagal membuka Chrome: {e}')
        write_status(status_path, **state)
        return 1
    try:
        if not _is_logged_in(page):
            state.update(done=True, error='Profil belum login Facebook.')
            write_status(status_path, **state)
            return 1
        state.update(pct=60, message='Membaca daftar Fanpage...')
        write_status(status_path, **state)
        pages = _list_pages(page)
        state.update(pct=100, done=True, success=True,
                     message=f'{len(pages)} Fanpage ditemukan.', pages=pages)
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


# ─── Facebook Group Functions ─────────────────────────────────────────────────

def _list_groups(page, keyword='', max_results=50):
    """Scrape daftar grup Facebook yang sudah di-join atau dari discover."""
    groups = []
    try:
        # Buka halaman grup yang sudah di-join
        page.get('https://www.facebook.com/groups/feed/', timeout=30)
        time.sleep(4)
        found = page.run_js(r"""
            const norm = s => (s||'').replace(/\s+/g,' ').trim();
            const clean = s => {
                let t = norm(s);
                // Notifikasi/badge ikut kepencet aria-label/innerText, mis.
                // "UnreadYou can now post and comment in NAMA GRUP" -> "NAMA GRUP"
                const m1 = t.match(/post(?:ed|ing)?\s+and\s+comment(?:ed)?\s+in\s+(.+)$/i);
                if (m1) t = m1[1].trim();
                const m2 = t.match(/(?:postingan|komentar).*?(?:di|ke)\s+(.+)$/i);
                if (m2) t = m2[1].trim();
                t = t.replace(/^(unread|belum dibaca)\s*/i, '').trim();
                // Buang ekor status "Last active ... ago" / "Aktif terakhir ..."
                t = t.replace(/\s*(last active|aktif terakhir).*$/i, '').trim();
                return t;
            };
            const seen = new Set();
            const out = [];
            const links = Array.from(document.querySelectorAll('a[href]'));
            for (const a of links) {
                const href = a.href || a.getAttribute('href') || '';
                const m = href.match(/facebook\.com\/groups\/(\d+|[\w.-]+)\/?/);
                if (!m) continue;
                const gid = m[1];
                if (seen.has(gid)) continue;
                // skip non-group urls
                if (['feed','discover','joins','members','search'].includes(gid)) continue;
                seen.add(gid);
                const txt = clean(a.getAttribute('aria-label') || a.innerText || '');
                if (!txt || txt.length < 2) continue;
                out.push({ id: gid, name: txt.slice(0, 80) });
                if (out.length >= 100) break;
            }
            return out;
        """) or []
        groups.extend(found)
    except Exception:
        pass

    # Jika ada keyword, cari juga via search
    if keyword:
        try:
            search_url = f'https://www.facebook.com/search/groups/?q={keyword.replace(" ", "+")}'
            page.get(search_url, timeout=30)
            time.sleep(4)
            found2 = page.run_js(r"""
                const norm = s => (s||'').replace(/\s+/g,' ').trim();
                const clean = s => {
                    let t = norm(s);
                    const m1 = t.match(/post(?:ed|ing)?\s+and\s+comment(?:ed)?\s+in\s+(.+)$/i);
                    if (m1) t = m1[1].trim();
                    const m2 = t.match(/(?:postingan|komentar).*?(?:di|ke)\s+(.+)$/i);
                    if (m2) t = m2[1].trim();
                    t = t.replace(/^(unread|belum dibaca)\s*/i, '').trim();
                    t = t.replace(/\s*(last active|aktif terakhir).*$/i, '').trim();
                    return t;
                };
                const seen = new Set();
                const out = [];
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const a of links) {
                    const href = a.href || a.getAttribute('href') || '';
                    const m = href.match(/facebook\.com\/groups\/(\d+|[\w.-]+)\/?/);
                    if (!m) continue;
                    const gid = m[1];
                    if (seen.has(gid)) continue;
                    if (['feed','discover','joins','members','search'].includes(gid)) continue;
                    seen.add(gid);
                    const txt = clean(a.getAttribute('aria-label') || a.innerText || '');
                    if (!txt || txt.length < 2) continue;
                    out.push({ id: gid, name: txt.slice(0, 80) });
                    if (out.length >= 100) break;
                }
                return out;
            """) or []
            for g in found2:
                if not any(x['id'] == g['id'] for x in groups):
                    groups.append(g)
        except Exception:
            pass

    # Filter keyword
    if keyword:
        kw = keyword.lower()
        groups = [g for g in groups if kw in g.get('name', '').lower() or kw in str(g.get('id', '')).lower()]

    return groups[:max_results]


def _open_group_composer(page, group_id, log):
    """Buka composer post di Facebook Group."""
    gid = str(group_id or '').strip()
    if not gid:
        return False
    log(25, f'Membuka halaman Group {gid}...')
    try:
        page.get(f'https://www.facebook.com/groups/{gid}/', timeout=30)
        time.sleep(5)
        body = (page.run_js("return document.body ? document.body.innerText : '';") or '').lower()
        if 'join group' in body or 'bergabung' in body or 'request to join' in body or 'minta bergabung' in body:
            log(25, f'Belum bergabung dengan Group {gid}.')
            return False
        # Klik "Write something..." / "Tulis sesuatu..."
        _click_by_text(page, [
            "what's on your mind", 'apa yang ada di pikiranmu',
            'write something', 'tulis sesuatu',
            'create post', 'buat postingan'
        ], timeout=8)
        time.sleep(2)
        return True
    except Exception:
        return False


def _upload_to_group(page, item, group_id, group_name, dry_run, log):
    """Upload post (teks/foto/video) ke Facebook Group."""
    video_path = item.get('video_path') or ''
    caption = item.get('caption') or ''
    post_delay = int(item.get('post_delay') or 0)

    # Validasi file jika ada
    if video_path and not os.path.isfile(video_path):
        return False, f'File media tidak ditemukan: {video_path}'

    is_photo = _is_image(video_path) if video_path else False
    if video_path and not is_photo:
        video_path, _ = _prepare_facebook_video(video_path, log)
    media_label = 'foto' if is_photo else ('video' if video_path else 'teks')

    # Buka composer group
    if not _open_group_composer(page, group_id, log):
        return False, f'Tidak berhasil membuka composer di Group {group_name or group_id}.'

    if dry_run:
        return True, '__DRYRUN__'

    # Upload media jika ada
    if video_path:
        log(40, f'Mencari input file ({media_label})...')
        _click_by_text(page, ['photo/video', 'foto/video', 'add photo/video',
                               'add photo', 'add video', 'tambahkan foto'], timeout=8)
        time.sleep(2)
        finp = _find_file_input(page, timeout=20, prefer='image' if is_photo else 'video')
        if not finp:
            return False, f'Tidak menemukan input upload {media_label} di Group.'
        log(50, f'Mengunggah {media_label}...')
        try:
            finp.input(os.path.abspath(video_path))
        except Exception as e:
            return False, f'Gagal upload {media_label}: {e}'
        time.sleep(10)

    # Isi caption/teks
    log(65, 'Mengisi caption...')
    if caption:
        _fill_caption(page, caption)
        time.sleep(1)

    # Jeda opsional sebelum posting
    if post_delay > 0:
        log(80, f'Menunggu {post_delay} detik sebelum posting...')
        time.sleep(post_delay)

    # Klik Post / Publish
    log(88, 'Posting ke Group...')
    published = _click_by_text(page, [
        'post', 'kirim', 'publish', 'bagikan', 'share', 'terbitkan', 'submit'
    ], timeout=15)
    if not published:
        return False, 'Tombol Post tidak ditemukan di composer Group.'
    time.sleep(6)
    return True, f'Post {media_label} berhasil dikirim ke Group {group_name or group_id}.'


def _join_group(page, group_id, log):
    """Join satu Facebook Group."""
    gid = str(group_id or '').strip()
    if not gid:
        return False, 'Group ID kosong.'
    log(30, f'Membuka Group {gid}...')
    try:
        page.get(f'https://www.facebook.com/groups/{gid}/', timeout=30)
        time.sleep(5)
    except Exception as e:
        return False, f'Gagal membuka Group {gid}: {e}'

    try:
        body = (page.run_js("return document.body ? document.body.innerText : '';") or '').lower()
        # Cek apakah sudah member
        if 'discussion' in body or 'diskusi' in body or 'write something' in body or 'tulis sesuatu' in body:
            return True, f'Sudah menjadi anggota Group {gid}.'

        # Klik Join Group
        clicked = _click_by_text(page, [
            'join group', 'bergabung', 'join', 'request to join',
            'minta bergabung', 'ask to join'
        ], timeout=8)
        if not clicked:
            return False, f'Tombol Join tidak ditemukan di Group {gid}.'
        time.sleep(3)

        # Konfirmasi dialog jika ada
        _click_by_text(page, ['join group', 'bergabung', 'confirm', 'ok'], timeout=5)
        time.sleep(2)

        return True, f'Permintaan bergabung ke Group {gid} berhasil dikirim.'
    except Exception as e:
        return False, f'Error saat join Group {gid}: {e}'


def _check_group_privacy(page, group_id):
    """Deteksi privasi satu Facebook Group: 'private', 'public', atau 'unknown'."""
    gid = str(group_id or '').strip()
    if not gid:
        return 'unknown'
    try:
        page.get(f'https://www.facebook.com/groups/{gid}/', timeout=30)
        time.sleep(3)
        body = (page.run_js("return document.body ? document.body.innerText : '';") or '').lower()
        if 'grup privat' in body or 'private group' in body:
            return 'private'
        if 'grup publik' in body or 'public group' in body:
            return 'public'
        return 'unknown'
    except Exception:
        return 'unknown'


def run_post_group(job):
    """Job handler untuk posting ke Facebook Group (single atau batch)."""
    status_path = job['status_path']
    profile_path = job['profile_path']
    group_id = str(job.get('group_id') or '').strip()
    group_name = str(job.get('group_name') or '').strip()
    invisible = bool(job.get('invisible', False))
    raw_mode = (job.get('mode') or 'single').lower()
    # mode 'post_group' dari batch dispatch → perlakukan sebagai batch jika ada rows
    rows = job.get('rows') or []
    is_batch = raw_mode in ('batch', 'post_group') and len(rows) > 0

    state = {'pct': 5, 'message': 'Membuka Chrome...', 'done': False, 'success': False, 'error': None}
    write_status(status_path, **state)
    try:
        page = _connect(profile_path, headless=invisible)
    except Exception as e:
        state.update(done=True, error=f'Gagal membuka Chrome: {e}')
        write_status(status_path, **state)
        return 1

    def log(pct, msg):
        state.update(pct=pct, message=msg)
        write_status(status_path, **state)

    try:
        if not _is_logged_in(page):
            state.update(done=True, error='Profil belum login Facebook.')
            write_status(status_path, **state)
            return 1

        if is_batch:
            total = len(rows)
            state['rows'] = [{'title': (r.get('caption') or os.path.basename(r.get('video_path') or f'post {i+1}'))[:60],
                              'status': 'antre'} for i, r in enumerate(rows)]
            success = 0
            for i, item in enumerate(rows):
                state['rows'][i]['status'] = 'proses'
                base = int(i / total * 100)
                def rlog(pct, msg, _b=base, _i=i):
                    state['pct'] = min(99, _b + int(pct / total))
                    state['message'] = f'[{_i+1}/{total}] {msg}'
                    write_status(status_path, **state)
                row_group_id = (item.get('group_id') or group_id or '').strip()
                row_group_name = (item.get('group_name') or group_name or '').strip()
                if not item.get('post_delay'):
                    item['post_delay'] = job.get('post_delay') or 0
                if not row_group_id:
                    state['rows'][i]['status'] = 'gagal: group_id kosong'
                    write_status(status_path, **state)
                    continue
                try:
                    ok, info = _upload_to_group(page, item, row_group_id, row_group_name, job.get('dry_run'), rlog)
                except Exception as e:
                    ok, info = False, str(e)
                state['rows'][i]['status'] = 'sukses' if ok else f'gagal: {str(info)[:50]}'
                if ok:
                    success += 1
                write_status(status_path, **state)
                if i < total - 1:
                    delay = int(job.get('post_delay') or 5)
                    time.sleep(max(delay, 3))
            state.update(pct=100, done=True, success=(success > 0),
                         message=f'Selesai: {success}/{total} berhasil.')
            if success == 0:
                state['error'] = 'Semua posting ke Group gagal.'
        else:
            # Single post
            if not group_id:
                state.update(done=True, error='Group ID tidak boleh kosong.')
                write_status(status_path, **state)
                return 1
            ok, info = _upload_to_group(page, job, group_id, group_name, job.get('dry_run'), log)
            if job.get('dry_run') and info == '__DRYRUN__':
                state.update(pct=100, done=True, success=True,
                             message='Dry run OK — login valid & composer Group siap.')
            elif ok:
                state.update(pct=100, done=True, success=True, message=info)
            else:
                state.update(done=True, success=False, error=info)
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_list_groups(job):
    """Job handler untuk scrape daftar Facebook Group."""
    status_path = job['status_path']
    profile_path = job['profile_path']
    keyword = str(job.get('keyword') or '').strip()
    max_results = int(job.get('max_results') or 100)
    headless = bool(job.get('headless', False))

    state = {'pct': 10, 'message': 'Membuka Chrome...', 'done': False,
             'success': False, 'error': None, 'groups': []}
    write_status(status_path, **state)
    try:
        page = _connect(profile_path, headless=headless)
    except Exception as e:
        state.update(done=True, error=f'Gagal membuka Chrome: {e}')
        write_status(status_path, **state)
        return 1
    try:
        if not _is_logged_in(page):
            state.update(done=True, error='Profil belum login Facebook.')
            write_status(status_path, **state)
            return 1
        state.update(pct=40, message=f'Scraping grup Facebook{" (keyword: " + keyword + ")" if keyword else ""}...')
        write_status(status_path, **state)
        groups = _list_groups(page, keyword=keyword, max_results=max_results)
        state.update(pct=100, done=True, success=True,
                     message=f'{len(groups)} grup ditemukan.', groups=groups)
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_join_groups(job):
    """Job handler untuk auto join Facebook Group dari list."""
    status_path = job['status_path']
    profile_path = job['profile_path']
    group_ids = job.get('group_ids') or []  # list of {id, name}
    invisible = bool(job.get('invisible', False))
    delay_sec = int(job.get('delay') or 10)  # jeda antar join (detik)

    state = {'pct': 5, 'message': 'Membuka Chrome...', 'done': False,
             'success': False, 'error': None, 'rows': []}
    write_status(status_path, **state)
    try:
        page = _connect(profile_path, headless=invisible)
    except Exception as e:
        state.update(done=True, error=f'Gagal membuka Chrome: {e}')
        write_status(status_path, **state)
        return 1
    try:
        if not _is_logged_in(page):
            state.update(done=True, error='Profil belum login Facebook.')
            write_status(status_path, **state)
            return 1

        if not group_ids:
            state.update(done=True, error='Tidak ada grup yang dipilih untuk di-join.')
            write_status(status_path, **state)
            return 1

        total = len(group_ids)
        state['rows'] = [{'title': g.get('name') or g.get('id') or f'Group {i+1}',
                          'status': 'antre'} for i, g in enumerate(group_ids)]
        write_status(status_path, **state)

        success = 0
        for i, g in enumerate(group_ids):
            gid = str(g.get('id') or '').strip()
            gname = str(g.get('name') or '').strip()
            state['rows'][i]['status'] = 'proses'
            state['pct'] = int(i / total * 100)
            state['message'] = f'[{i+1}/{total}] Join group {gname or gid}...'
            write_status(status_path, **state)

            ok, info = _join_group(page, gid, lambda pct, msg: None)
            state['rows'][i]['status'] = 'sukses' if ok else f'gagal: {info[:50]}'
            if ok:
                success += 1
            write_status(status_path, **state)

            if i < total - 1:
                time.sleep(max(delay_sec, 3))

        state.update(pct=100, done=True, success=(success > 0),
                     message=f'Selesai: {success}/{total} grup berhasil di-join.')
        if success == 0:
            state['error'] = 'Semua permintaan join gagal.'
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_check_privacy(job):
    """Job handler untuk cek privasi (privat/publik) sekumpulan Facebook Group,
    dipakai sebelum posting supaya user tahu privasi grup tujuannya."""
    status_path = job['status_path']
    profile_path = job['profile_path']
    group_ids = [str(g or '').strip() for g in (job.get('group_ids') or []) if str(g or '').strip()]
    invisible = bool(job.get('invisible', False))

    state = {'pct': 5, 'message': 'Membuka Chrome...', 'done': False,
             'success': False, 'error': None, 'results': []}
    write_status(status_path, **state)
    try:
        page = _connect(profile_path, headless=invisible)
    except Exception as e:
        state.update(done=True, error=f'Gagal membuka Chrome: {e}')
        write_status(status_path, **state)
        return 1
    try:
        if not _is_logged_in(page):
            state.update(done=True, error='Profil belum login Facebook.')
            write_status(status_path, **state)
            return 1
        if not group_ids:
            state.update(done=True, error='Tidak ada grup untuk dicek.')
            write_status(status_path, **state)
            return 1

        total = len(group_ids)
        results = []
        for i, gid in enumerate(group_ids):
            state.update(pct=int(i / total * 100), message=f'[{i+1}/{total}] Cek privasi Group {gid}...')
            write_status(status_path, **state)
            privacy = _check_group_privacy(page, gid)
            results.append({'id': gid, 'privacy': privacy})
            state['results'] = results
            write_status(status_path, **state)
        state.update(pct=100, done=True, success=True, message=f'Selesai cek privasi {total} grup.')
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            page.quit()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_selftest():
    result = {'drission_ok': False, 'chrome': '', 'error': None}
    try:
        from DrissionPage import ChromiumPage, ChromiumOptions  # noqa
        result['drission_ok'] = True
        result['chrome'] = get_chrome_path() or ''
    except Exception as e:
        result['error'] = str(e)
    print(json.dumps(result))
    return 0 if result['drission_ok'] and result['chrome'] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job', nargs='?')
    parser.add_argument('--selftest', action='store_true')
    args = parser.parse_args()
    if args.selftest:
        return run_selftest()
    if not args.job or not os.path.isfile(args.job):
        print('Job file tidak ditemukan', file=sys.stderr)
        return 2
    with open(args.job, 'r', encoding='utf-8') as f:
        job = json.load(f)
    if (job.get('mode') or '').lower() == 'list_pages':
        return run_list_pages(job)
    mode = (job.get('mode') or '').lower()
    if mode == 'list_groups':
        return run_list_groups(job)
    if mode == 'join_groups':
        return run_join_groups(job)
    if mode == 'check_privacy':
        return run_check_privacy(job)
    if mode in ('post_group', 'group_post'):
        return run_post_group(job)
    return run_job(job)


if __name__ == '__main__':
    raise SystemExit(main())
