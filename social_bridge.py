"""
social_bridge.py — Worker upload untuk Pinterest, Threads, dan X (Twitter).

Dijalankan sebagai subprocess:
    python social_bridge.py <job_json_path>       (job['platform'] = pinterest|threads|x)
    python social_bridge.py --selftest

Sama seperti Facebook/Instagram: otomasi ditulis sendiri dengan DrissionPage yang
menyetir profil Chrome GAM. Profil harus SUDAH login platform terkait (buka
shortcut-nya di kartu profil lalu login). Selector platform sering berubah — pakai
banyak fallback + logging; untuk upload sungguhan pertama kali pakai mode terlihat.
"""
import os
import sys
import json
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

PLATFORMS = {
    'pinterest': {'home': 'https://www.pinterest.com/', 'schedule': True, 'media_required': True},
    'threads':   {'home': 'https://www.threads.net/',   'schedule': False, 'media_required': False},
    'x':         {'home': 'https://x.com/home',          'schedule': True, 'media_required': False},
}


def get_chrome_path():
    env = os.environ.get("GAMA_BROWSER")
    if env and os.path.isfile(env):
        return env
    for p in CHROME_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def _port_for(profile_path, platform):
    return 9750 + (abs(hash(profile_path + platform)) % 240)


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


def _connect(profile_path, platform, headless=False):
    from DrissionPage import ChromiumPage, ChromiumOptions
    chrome = get_chrome_path()
    if not chrome:
        raise RuntimeError("Google Chrome tidak ditemukan.")
    co = ChromiumOptions()
    co.set_browser_path(chrome)
    co.set_user_data_path(os.path.abspath(profile_path))
    co.set_local_port(_port_for(profile_path, platform))
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


def _is_logged_in(page, platform):
    home = PLATFORMS[platform]['home']
    try:
        page.get(home, timeout=25)
    except Exception:
        pass
    time.sleep(4)
    url = (page.url or '').lower()
    if 'login' in url or 'signup' in url or '/accounts/login' in url:
        return False
    checks = {
        'pinterest': '[data-test-id="header-profile"],[aria-label="Profile"],div[role="navigation"]',
        'threads':   'a[href="/settings"],[aria-label="Profile"],[aria-label="Create"],svg[aria-label="Create"]',
        'x':         '[data-testid="SideNav_NewTweet_Button"],[aria-label="Post"],[data-testid="AppTabBar_Home_Link"]',
    }[platform]
    try:
        return bool(page.run_js(f"return !!document.querySelector('{checks}');"))
    except Exception:
        return 'login' not in url


def _find_file_input(page, timeout=20):
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
                    'div[role="button"],button,a[role="button"],[aria-label],[role="menuitem"],span[role="button"]');
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


def _fill_text(page, text, selectors):
    if not text:
        return False
    try:
        box = None
        for sel in selectors:
            box = page.ele(sel, timeout=3)
            if box:
                break
        if not box:
            return False
        box.click()
        time.sleep(0.4)
        try:
            box.input(text)
        except Exception:
            page.actions.type(text)
        return True
    except Exception:
        return False


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


# ── Composer per platform ─────────────────────────────────────────────────────

def _upload_x(page, item, dry_run, log):
    caption = item.get('caption') or ''
    media = item.get('video_path') or ''
    schedule = item.get('schedule_time')
    log(30, 'Membuka composer X...')
    try:
        page.get('https://x.com/compose/post', timeout=30)
    except Exception:
        pass
    time.sleep(4)
    editor_sel = ['xpath://div[@data-testid="tweetTextarea_0"]',
                  'xpath://div[@role="textbox"]']
    if dry_run:
        ok = bool(page.ele(editor_sel[0], timeout=8) or page.ele(editor_sel[1], timeout=3))
        return (True, '__DRYRUN__') if ok else (False, 'Composer X tidak ditemukan.')
    _fill_text(page, caption, editor_sel)
    time.sleep(1)
    if media and os.path.isfile(media):
        log(55, 'Melampirkan media...')
        finp = page.ele('xpath://input[@data-testid="fileInput"]', timeout=6) or _find_file_input(page, 10)
        if finp:
            try:
                finp.input(os.path.abspath(media))
                time.sleep(8)
            except Exception:
                pass
    if schedule:
        log(75, 'Menjadwalkan...')
        _try_schedule_x(page, schedule)
    log(90, 'Posting...')
    posted = _click_by_text(page, ['post', 'kirim', 'tweet', 'schedule', 'jadwalkan'], timeout=12)
    if not posted:
        return False, 'Tombol Post tidak ditemukan.'
    time.sleep(6)
    return True, 'Diposting ke X (perlu cek manual).'


def _try_schedule_x(page, schedule):
    try:
        _click_by_text(page, ['schedule post', 'jadwalkan postingan', 'schedule'], timeout=5)
        time.sleep(2)
        # (best-effort) — pengisian tanggal/jam X berubah-ubah; dilewati bila gagal
    except Exception:
        pass


def _upload_threads(page, item, dry_run, log):
    caption = item.get('caption') or ''
    media = item.get('video_path') or ''
    log(30, 'Membuka composer Threads...')
    try:
        page.get('https://www.threads.net/', timeout=30)
    except Exception:
        pass
    time.sleep(4)
    _click_by_text(page, ['create', 'buat', 'new thread', 'post baru', 'mulai thread'], timeout=8)
    time.sleep(2)
    editor_sel = ['xpath://div[@contenteditable="true" and @role="textbox"]',
                  'xpath://div[@contenteditable="true"]',
                  'tag:textarea']
    if dry_run:
        ok = bool(page.ele(editor_sel[0], timeout=8) or page.ele(editor_sel[1], timeout=3))
        return (True, '__DRYRUN__') if ok else (False, 'Composer Threads tidak ditemukan.')
    _fill_text(page, caption, editor_sel)
    time.sleep(1)
    if media and os.path.isfile(media):
        log(55, 'Melampirkan media...')
        finp = _find_file_input(page, 10)
        if finp:
            try:
                finp.input(os.path.abspath(media))
                time.sleep(8)
            except Exception:
                pass
    log(90, 'Posting...')
    posted = _click_by_text(page, ['post', 'kirim', 'bagikan'], timeout=12)
    if not posted:
        return False, 'Tombol Post tidak ditemukan.'
    time.sleep(6)
    return True, 'Diposting ke Threads (perlu cek manual).'


def _upload_pinterest(page, item, dry_run, log):
    media = item.get('video_path') or ''
    title = item.get('title') or ''
    desc = item.get('caption') or ''
    link = item.get('link') or ''
    schedule = item.get('schedule_time')
    if not os.path.isfile(media):
        return False, f'File media tidak ditemukan: {media}'
    log(28, 'Membuka Pin builder...')
    try:
        page.get('https://www.pinterest.com/pin-builder/', timeout=30)
    except Exception:
        pass
    time.sleep(5)
    finp = _find_file_input(page, 20)
    if not finp:
        _click_by_text(page, ['upload', 'unggah', 'select', 'pilih'], timeout=5)
        finp = _find_file_input(page, 12)
    if not finp:
        return False, 'Input upload Pinterest tidak ditemukan.'
    if dry_run:
        return True, '__DRYRUN__'
    log(45, 'Mengunggah media...')
    try:
        finp.input(os.path.abspath(media))
        time.sleep(9)
    except Exception as e:
        return False, f'Gagal set media: {e}'
    log(65, 'Mengisi judul & deskripsi...')
    _fill_text(page, title, ['xpath://textarea[@id="storyboard-selector-title" or contains(@aria-label,"title") or contains(@aria-label,"Judul")]',
                             'xpath://div[@contenteditable="true"][1]'])
    _fill_text(page, desc, ['xpath://div[contains(@aria-label,"description") or contains(@aria-label,"deskripsi")]//div[@contenteditable="true"]',
                            'xpath://div[@contenteditable="true"][2]'])
    if link:
        _fill_text(page, link, ['xpath://textarea[contains(@aria-label,"link") or contains(@id,"link")]',
                                'xpath://input[contains(@placeholder,"link") or contains(@placeholder,"URL")]'])
    if schedule:
        log(80, 'Menjadwalkan (best-effort)...')
        _click_by_text(page, ['publish at a later date', 'jadwalkan', 'publish later'], timeout=5)
        time.sleep(1)
    log(90, 'Publish...')
    posted = _click_by_text(page, ['publish', 'terbitkan', 'simpan', 'save', 'schedule'], timeout=12)
    if not posted:
        return False, 'Tombol Publish Pinterest tidak ditemukan.'
    time.sleep(8)
    return True, 'Pin dipublish (perlu cek manual).'


_COMPOSERS = {'x': _upload_x, 'threads': _upload_threads, 'pinterest': _upload_pinterest}


def _upload_one(page, platform, item, dry_run, log):
    if PLATFORMS[platform]['media_required']:
        mp = item.get('video_path') or ''
        if not os.path.isfile(mp):
            return False, f'File media tidak ditemukan: {mp}'
    return _COMPOSERS[platform](page, item, dry_run, log)


def run_job(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    platform = (job.get('platform') or '').lower()
    invisible = bool(job.get('invisible'))
    state = {'pct': 0, 'message': f'Menyiapkan {platform}...', 'done': False,
             'success': False, 'error': None}

    def log(pct, msg):
        state['pct'] = pct
        state['message'] = msg
        write_status(status_path, **state)

    if platform not in PLATFORMS:
        state.update(done=True, error=f'Platform tidak didukung: {platform}')
        write_status(status_path, **state)
        return 1

    log(5, 'Membuka profil Chrome...')
    if not os.path.isdir(profile_path):
        state.update(done=True, error='Folder profil tidak ditemukan.')
        write_status(status_path, **state)
        return 1

    try:
        page = _connect(profile_path, platform, headless=invisible)
    except Exception as e:
        state.update(done=True, error=f'Gagal membuka Chrome: {e}')
        write_status(status_path, **state)
        return 1

    try:
        log(15, f'Memeriksa sesi login {platform}...')
        if not _is_logged_in(page, platform):
            state.update(done=True, error=f'Profil belum login {platform}. Buka shortcut '
                                          f'{platform} di kartu profil lalu login dulu.')
            write_status(status_path, **state)
            return 1

        mode = (job.get('mode') or 'single').lower()
        if mode == 'batch':
            rows = job.get('rows') or []
            total = len(rows)
            state['rows'] = [{'title': (r.get('caption') or r.get('title')
                              or os.path.basename(r.get('video_path') or f'item {i+1}'))[:80],
                              'status': 'antre'} for i, r in enumerate(rows)]
            success = 0
            for i, item in enumerate(rows):
                state['rows'][i]['status'] = 'proses'
                base = int(i / total * 100)

                def rlog(pct, msg, _b=base, _i=i):
                    state['pct'] = min(99, _b + int(pct / total))
                    state['message'] = f'[{_i+1}/{total}] {msg}'
                    write_status(status_path, **state)

                ok, info = _upload_one(page, platform, item, job.get('dry_run'), rlog)
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
            ok, info = _upload_one(page, platform, job, job.get('dry_run'), log)
            if job.get('dry_run') and info == '__DRYRUN__':
                state.update(pct=100, done=True, success=True,
                             message=f'Dry run OK — login {platform} valid & composer siap, tidak ada upload.')
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
    result = {'drission_ok': False, 'chrome': '', 'platforms': list(PLATFORMS), 'error': None}
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
