"""
instagram_bridge.py — Worker upload Instagram (Reels + Foto/Post) untuk GAM.

Dijalankan sebagai subprocess:
    python instagram_bridge.py <job_json_path>
    python instagram_bridge.py --selftest

Sama seperti Facebook, Instagram tidak punya engine jadi — otomasi ditulis
sendiri dengan DrissionPage menyetir profil Chrome GAM. Profil harus SUDAH
login Instagram (buka shortcut Instagram di kartu profil lalu login).

Instagram web:
- Video → tombol "New post" → pilih video (jadi Reels) → Next → Next → caption → Share.
- Foto  → tombol "New post" → pilih foto (Post biasa) → Next → Next → caption → Share.
- Jadwal → via Meta Business Suite (perlu akun IG terhubung ke Facebook Page).
Selector IG sering berubah — banyak fallback + logging.
Untuk upload sungguhan pertama kali disarankan mode terlihat (non-headless).
"""
import os
import sys
import json
import time
import argparse
import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))


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

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}


def _is_image(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in IMAGE_EXTS


def get_chrome_path():
    env = os.environ.get("GAMA_BROWSER")
    if env and os.path.isfile(env):
        return env
    for p in CHROME_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def _port_for(profile_path):
    return 9700 + (abs(hash(profile_path)) % 250)


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
    from DrissionPage import ChromiumPage, ChromiumOptions
    chrome = get_chrome_path()
    if not chrome:
        raise RuntimeError("Google Chrome tidak ditemukan.")
    co = ChromiumOptions()
    co.set_browser_path(chrome)
    co.set_user_data_path(os.path.abspath(profile_path))
    co.set_local_port(_port_for(profile_path))
    # --disable-blink-features=AutomationControlled dibuang -- Chrome baru malah
    # menampilkan infobar "unsupported command-line flag" tiap halaman kalau flag ini
    # dipakai, jadi jejak otomasi malah makin mencolok. navigator.webdriver dipatch
    # lewat JS injeksi CDP (add_init_js) yang tidak meninggalkan jejak di UI Chrome.
    # --lang=en-US: standarkan UI ke Inggris (disepakati 26 Jul, sama seperti facebook_bridge.py).
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
    try:
        page.get('https://www.instagram.com/', timeout=25)
    except Exception:
        pass
    time.sleep(4)
    url = (page.url or '').lower()
    if '/accounts/login' in url or 'login' in url:
        return False
    try:
        return bool(page.run_js("""
            return !!(document.querySelector('svg[aria-label="New post"]') ||
                      document.querySelector('svg[aria-label="Postingan baru"]') ||
                      document.querySelector('[aria-label="Home"]') ||
                      document.querySelector('[aria-label="Beranda"]') ||
                      document.querySelector('nav a[href="/"]'));
        """))
    except Exception:
        return 'login' not in url


def _find_file_input(page, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        for sel in ('tag:input@type=file', 'xpath://input[@type="file"]'):
            try:
                el = page.ele(sel, timeout=2)
                if el:
                    return el
            except Exception:
                pass
        time.sleep(1)
    return None


def _click_by_text(page, texts, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        try:
            clicked = page.run_js("""
                const wanted = arguments[0].map(t => t.toLowerCase());
                const nodes = document.querySelectorAll(
                    'div[role="button"],button,a[role="button"],[aria-label],[role="menuitem"]');
                for (const n of nodes) {
                    const lbl = ((n.getAttribute('aria-label')||'') + ' ' + (n.innerText||''))
                                .trim().toLowerCase();
                    if (!lbl) continue;
                    for (const w of wanted) {
                        if (lbl === w || lbl.startsWith(w) || lbl.includes(w)) {
                            if (n.offsetParent !== null) { n.click(); return lbl; }
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


def _fill_caption(page, caption):
    if not caption:
        return
    try:
        box = None
        for sel in ('xpath://div[@aria-label="Write a caption..." or @aria-label="Tulis caption..."]',
                    'xpath://textarea[contains(@aria-label,"caption") or contains(@aria-label,"Caption")]',
                    'xpath://div[@contenteditable="true" and @role="textbox"]',
                    'xpath://div[@contenteditable="true"]'):
            box = page.ele(sel, timeout=3)
            if box:
                break
        if box:
            box.click()
            time.sleep(0.5)
            try:
                box.input(caption)
            except Exception:
                page.actions.type(caption)
    except Exception:
        pass


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


def _open_composer_and_input(page, log):
    """Buka composer IG dan kembalikan file input, atau None."""
    log(28, 'Membuka composer Instagram...')
    try:
        page.get('https://www.instagram.com/', timeout=30)
    except Exception:
        pass
    time.sleep(5)
    # Tombol "New post / Buat"
    _click_by_text(page, ['new post', 'postingan baru', 'buat', 'create'], timeout=10)
    time.sleep(2)
    # Kadang muncul submenu "Post/Postingan"
    _click_by_text(page, ['post', 'postingan'], timeout=4)
    time.sleep(2)
    finp = _find_file_input(page, timeout=20)
    if not finp:
        _click_by_text(page, ['select from computer', 'pilih dari komputer', 'select from device'], timeout=5)
        finp = _find_file_input(page, timeout=15)
    return finp


def _upload_one(page, item, dry_run, log):
    video_path = item.get('video_path') or ''
    caption = item.get('caption') or ''
    schedule_time = (item.get('schedule_time') or '').strip()

    if not os.path.isfile(video_path):
        return False, f'File media tidak ditemukan: {video_path}'

    # Jika ada jadwal → gunakan Meta Business Suite (channel instagram)
    if schedule_time:
        try:
            from facebook_bridge import _upload_via_meta_suite
        except ImportError:
            return False, 'Gagal import facebook_bridge untuk jadwal Meta Suite.'
        return _upload_via_meta_suite(
            page, video_path, caption, schedule_time,
            'personal', '', '', dry_run, log, channel='instagram'
        )

    # Tanpa jadwal → upload langsung (flow biasa)
    is_photo = _is_image(video_path)
    media_label = 'foto' if is_photo else 'video'

    finp = _open_composer_and_input(page, log)
    if not finp:
        return False, ('Tidak menemukan input upload. Tampilan Instagram mungkin berubah, '
                       'atau composer tidak tersedia untuk akun ini.')
    if dry_run:
        return True, '__DRYRUN__'

    log(45, f'Mengunggah {media_label}...')
    try:
        finp.input(os.path.abspath(video_path))
    except Exception as e:
        return False, f'Gagal set file {media_label}: {e}'
    time.sleep(9)

    if not is_photo:
        # Video: kadang muncul dialog "Video akan dibagikan sebagai reel" → OK
        _click_by_text(page, ['ok', 'oke'], timeout=4)

    # Next (crop) → Next (edit/filter)
    log(65, 'Melanjutkan (Next)...')
    for _ in range(2):
        if _click_by_text(page, ['next', 'berikutnya', 'lanjut'], timeout=8):
            time.sleep(3)
        else:
            break

    log(80, 'Mengisi caption...')
    _fill_caption(page, caption)
    time.sleep(1)

    log(90, 'Share...')
    published = _click_by_text(page, ['share', 'bagikan', 'post', 'kirim'], timeout=12)
    if not published:
        return False, 'Tombol Share/Bagikan tidak ditemukan.'
    time.sleep(10)
    post_type = 'Foto' if is_photo else 'Reels'
    return True, f'{post_type} diposting ke Instagram (perlu cek manual).'


def run_job(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    invisible = bool(job.get('invisible'))
    state = {'pct': 0, 'message': 'Menyiapkan Instagram...', 'done': False,
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
        log(15, 'Memeriksa sesi login Instagram...')
        if not _is_logged_in(page):
            state.update(done=True, error='Profil belum login Instagram. Buka shortcut '
                                          'Instagram di kartu profil lalu login dulu.')
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

                ok, info = _upload_one(page, item, job.get('dry_run'), rlog)
                state['rows'][i]['status'] = 'sukses' if ok else f'gagal: {str(info)[:50]}'
                if ok:
                    success += 1
                write_status(status_path, **state)
                if i < total - 1:
                    time.sleep(6)
            state.update(pct=100, done=True, success=(success > 0),
                         message=f'Selesai: {success}/{total} berhasil.')
            if success == 0:
                state['error'] = 'Semua upload gagal.'
        else:
            ok, info = _upload_one(page, job, job.get('dry_run'), log)
            if job.get('dry_run') and info == '__DRYRUN__':
                state.update(pct=100, done=True, success=True,
                             message='Dry run OK — login Instagram valid & composer siap, tidak ada upload.')
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
    return run_job(job)


if __name__ == '__main__':
    raise SystemExit(main())
