"""
social_downloader_bridge.py — Worker Downloader untuk platform yang mengunci
akses tanpa login: Facebook, Instagram, Threads, X, Pinterest.

Kenapa perlu file terpisah dari yt-dlp (dipakai _run_downloader_job_thread /
_run_tiktok_account_job_thread di main.py):
- Instagram & Threads sekarang mewajibkan sesi login bahkan untuk 1 post publik
  (yt-dlp balas "Instagram sent an empty media response" / tidak ada extractor
  sama sekali untuk Threads). Datanya HANYA muncul di HTML kalau browser-nya
  benar-benar login (dibuktikan lewat percobaan langsung 27 Jul 2026).
- Facebook, X, Pinterest: yt-dlp masih bisa unduh 1 post/video langsung (link
  publik), tapi TIDAK BISA membuka daftar video/pin dari sebuah akun (menu
  "Per Akun") tanpa login — makanya listing-nya lewat browser, unduhannya
  tetap lewat yt-dlp (sudah terbukti jalan).

Dijalankan sebagai subprocess (pola sama seperti facebook_bridge.py):
    python social_downloader_bridge.py <job_json_path>

Job fields:
    platform     : facebook|instagram|threads|x|pinterest
    mode         : 'account' (Per Akun — target=username, limit=N) atau
                   'link' (Dari Link — urls=[...])
    profile_path : path folder profil Chrome GAM (WAJIB, harus sudah login)
    target       : username (mode account)
    urls         : list URL (mode link)
    limit        : jumlah item terbaru (mode account, 0 = semua/maks 60)
    media_type   : video|image|both (mode account; dipakai utk filter jenis)
    out_dir      : folder tujuan
    status_path, stop_path : diisi otomatis oleh _start_worker()
"""
import os
import sys
import re
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""

HOME_URL = {
    'facebook':  'https://www.facebook.com/',
    'instagram': 'https://www.instagram.com/',
    'threads':   'https://www.threads.com/',
    'x':         'https://x.com/home',
    'pinterest': 'https://www.pinterest.com/',
}


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


def _connect(profile_path, headless=False):
    from DrissionPage import ChromiumPage, ChromiumOptions
    chrome = get_chrome_path()
    if not chrome:
        raise RuntimeError("Google Chrome tidak ditemukan.")
    co = ChromiumOptions()
    co.set_browser_path(chrome)
    co.set_user_data_path(os.path.abspath(profile_path))
    co.set_local_port(_port_for(profile_path))
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


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


def _is_logged_in(page, platform):
    try:
        page.get(HOME_URL[platform], timeout=30)
    except Exception:
        pass
    time.sleep(4)
    url = (page.url or '').lower()
    if platform == 'facebook':
        return 'login' not in url and 'checkpoint' not in url
    if platform == 'instagram':
        return '/accounts/login' not in url
    if platform == 'threads':
        return 'login' not in url
    if platform == 'x':
        return '/login' not in url and '/i/flow' not in url
    if platform == 'pinterest':
        return 'login' not in url
    return True


# ─── Koleksi link post/video/pin dari halaman akun (scroll infinite) ─────────

_LISTING_URL = {
    'facebook':  lambda u: f'https://www.facebook.com/{u}/videos',
    'instagram': lambda u: f'https://www.instagram.com/{u}/',
    'threads':   lambda u: f'https://www.threads.com/@{u}',
    'x':         lambda u: f'https://x.com/{u}',
    'pinterest': lambda u: f'https://www.pinterest.com/{u}/_created/',
}

_LINK_PATTERN = {
    'facebook':  re.compile(r'/(?:[^/"]+/videos/(\d+)|watch/\?v=(\d+)|reel/(\d+))'),
    'instagram': re.compile(r'^/([\w.\-]+)/(p|reel)/([\w\-]+)/'),
    'threads':   re.compile(r'^/@([\w.\-]+)/post/([\w\-]+)'),
    'x':         re.compile(r'^/([\w]+)/status/(\d+)'),
    'pinterest': re.compile(r'/pin/(\d+)'),
}


def _norm_link(platform, href, username):
    if not href:
        return None
    href = href.split('?')[0]
    if platform == 'facebook':
        if href.startswith('/'):
            href = 'https://www.facebook.com' + href
        m = _LINK_PATTERN['facebook'].search(href)
        if not m:
            return None
        # /videos/<id> dan /reel/<id> sering menunjuk video YANG SAMA — normalisasi
        # ke satu bentuk kanonik pakai ID-nya saja supaya tidak double-download.
        vid_id = next((g for g in m.groups() if g), None)
        return f'https://www.facebook.com/reel/{vid_id}/' if vid_id else href
    if platform == 'instagram':
        m = _LINK_PATTERN['instagram'].match(href)
        if not m or m.group(1).lower() != username.lower():
            return None
        return f'https://www.instagram.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/'
    if platform == 'threads':
        m = _LINK_PATTERN['threads'].match(href)
        if not m or m.group(1).lower() != username.lower():
            return None
        return f'https://www.threads.com/@{m.group(1)}/post/{m.group(2)}'
    if platform == 'x':
        m = _LINK_PATTERN['x'].match(href)
        if not m or m.group(1).lower() != username.lower():
            return None
        return f'https://x.com/{m.group(1)}/status/{m.group(2)}'
    if platform == 'pinterest':
        m = _LINK_PATTERN['pinterest'].search(href)
        if not m:
            return None
        return f'https://www.pinterest.com/pin/{m.group(1)}/'
    return None


def collect_account_links(page, platform, username, limit, log):
    url = _LISTING_URL[platform](username)
    log(8, f'Membuka profil {platform} @{username}...')
    page.get(url, timeout=30)
    time.sleep(5)
    if platform in ('facebook', 'instagram') and '/login' in (page.url or '').lower():
        raise RuntimeError(f'Profil ini tidak login {platform} atau akun @{username} tidak ditemukan/privat.')

    found = []
    seen = set()
    stall = 0
    max_scrolls = 40
    want = limit if limit and limit > 0 else 60
    for i in range(max_scrolls):
        try:
            hrefs = page.run_js(
                "return Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.getAttribute('href'));"
            ) or []
        except Exception:
            hrefs = []
        new_count = 0
        for h in hrefs:
            link = _norm_link(platform, h, username)
            if link and link not in seen:
                seen.add(link)
                found.append(link)
                new_count += 1
        if len(found) >= want:
            break
        stall = stall + 1 if new_count == 0 else 0
        if stall >= 3:
            break
        log(8 + int(min(i, 20) / 20 * 12), f'Menyusuri postingan {platform} @{username}... ({len(found)} ditemukan)')
        try:
            page.run_js("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
        time.sleep(2.2)
    return found[:want]


# ─── Ekstraksi media 1 post — Instagram & Threads (skema Meta yang sama) ────

def _iter_json_blocks(html):
    for m in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        try:
            yield json.loads(m.group(1))
        except Exception:
            continue


def _walk_find_code(obj, code, _depth=0):
    """Cari (rekursif) dict yang punya field 'code' cocok DAN punya media
    (video_versions/image_versions2/carousel_media)."""
    if _depth > 60:
        return None
    if isinstance(obj, dict):
        if obj.get('code') == code and (
            'video_versions' in obj or 'image_versions2' in obj or 'carousel_media' in obj
        ):
            return obj
        for v in obj.values():
            r = _walk_find_code(v, code, _depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_find_code(v, code, _depth + 1)
            if r is not None:
                return r
    return None


def _media_from_node(node):
    """Return list of (url, is_video) dari 1 media node (post biasa/carousel)."""
    items = node.get('carousel_media') or [node]
    out = []
    for it in items:
        vv = it.get('video_versions')
        if vv:
            out.append((vv[0]['url'], True))
            continue
        iv = it.get('image_versions2') or {}
        cands = iv.get('candidates') or []
        if cands:
            out.append((cands[0]['url'], False))
    return out


def extract_meta_post(page, url, log):
    """Instagram/Threads: buka 1 post, ambil media + caption dari JSON yang
    di-render server (HANYA muncul kalau browser sedang login)."""
    log(20, f'Membuka {url} ...')
    page.get(url, timeout=30)
    time.sleep(5)
    m = re.search(r'/(?:p|reel|post)/([\w\-]+)', url) or re.search(r'@[\w.\-]+/post/([\w\-]+)', url)
    code = m.group(1) if m else None
    if not code:
        raise RuntimeError(f'Tidak bisa mengenali kode post dari URL: {url}')

    html = page.html
    node = None
    for block in _iter_json_blocks(html):
        node = _walk_find_code(block, code)
        if node:
            break
    if not node:
        # coba re-load sekali (kadang data baru muncul setelah full render)
        time.sleep(3)
        html = page.html
        for block in _iter_json_blocks(html):
            node = _walk_find_code(block, code)
            if node:
                break
    if not node:
        raise RuntimeError(
            'Media tidak ditemukan di halaman. Post mungkin privat/dihapus, '
            'atau profil belum benar-benar login.'
        )
    media = _media_from_node(node)
    if not media:
        raise RuntimeError('Post ditemukan tapi tidak ada video/gambar (mungkin post teks saja).')
    caption = ((node.get('caption') or {}).get('text') or '').strip()
    return media, caption


# ─── Pinterest: ambil gambar (atau video via yt-dlp kalau ada) ───────────────

def extract_pinterest(page, url, log):
    log(20, f'Membuka {url} ...')
    page.get(url, timeout=30)
    time.sleep(4)
    data = page.run_js("""
        const vids = Array.from(document.querySelectorAll('video')).map(v => v.src || v.currentSrc);
        const imgs = Array.from(document.querySelectorAll('img'))
            .map(i => ({src: i.src, w: i.naturalWidth}))
            .filter(i => i.w > 200 && i.src.includes('pinimg.com'));
        imgs.sort((a,b) => b.w - a.w);
        const h1 = document.querySelector('h1');
        return {vids, img: imgs.length ? imgs[0].src : null, title: h1 ? h1.innerText : ''};
    """) or {}
    if data.get('vids'):
        return None, data.get('title') or '', True   # sinyal: pakai yt-dlp
    img = data.get('img')
    if not img:
        raise RuntimeError('Gambar pin tidak ditemukan (mungkin pin privat/dihapus).')
    # JANGAN tebak ganti ke /originals/ — banyak pin balas 403 utk resolusi itu.
    # Pakai persis URL yang sudah terbukti render di browser (resolusi terbesar
    # yang benar-benar dipakai halaman).
    return [(img, False)], data.get('title') or '', False


# ─── Download helper ─────────────────────────────────────────────────────────

def _download_url(url, out_path, referer=None):
    import requests
    headers = {'User-Agent': 'Mozilla/5.0'}
    if referer:
        headers['Referer'] = referer
    r = requests.get(url, timeout=60, stream=True, headers=headers)
    r.raise_for_status()
    with open(out_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024 * 128):
            if chunk:
                f.write(chunk)
    return out_path


def _download_via_ytdlp_simple(url, out_dir, base):
    import yt_dlp
    opts = {
        'quiet': True, 'no_warnings': True,
        'outtmpl': os.path.join(out_dir, base + '.%(ext)s'),
        'format': 'bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1][acodec^=mp4a]/bv*+ba/b',
        'merge_output_format': 'mp4',
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not os.path.isfile(path):
        b = os.path.splitext(path)[0]
        for ext in ('.mp4', '.mkv', '.webm', '.jpg', '.png'):
            if os.path.isfile(b + ext):
                path = b + ext
                break
    caption = (info.get('description') or info.get('title') or '').strip()
    return path, caption


def _safe_name(s, fallback):
    s = re.sub(r'[\/*?:"<>|\r\n]', '_', (s or fallback)).strip()
    return (s or fallback)[:60]


def download_one_post(platform, page, url, out_dir, idx, log):
    """Kembalikan list of (media_path, caption, hashtags)."""
    base = f'{idx:02d}_{_safe_name(url.rstrip("/").split("/")[-1], platform)}'
    rows = []
    if platform in ('instagram', 'threads'):
        media, caption = extract_meta_post(page, url, log)
        referer = 'https://www.instagram.com/' if platform == 'instagram' else 'https://www.threads.com/'
        for j, (murl, is_video) in enumerate(media, 1):
            ext = '.mp4' if is_video else '.jpg'
            suffix = f'_{j}' if len(media) > 1 else ''
            out_path = os.path.join(out_dir, base + suffix + ext)
            _download_url(murl, out_path, referer=referer)
            rows.append((out_path, caption))
    elif platform == 'pinterest':
        media, title, use_ytdlp = extract_pinterest(page, url, log)
        if use_ytdlp:
            path, caption = _download_via_ytdlp_simple(url, out_dir, base)
            rows.append((path, caption or title))
        else:
            murl, _ = media[0]
            out_path = os.path.join(out_dir, base + '.jpg')
            _download_url(murl, out_path, referer='https://www.pinterest.com/')
            rows.append((out_path, title))
    else:  # facebook, x — sudah terbukti jalan via yt-dlp untuk link langsung
        path, caption = _download_via_ytdlp_simple(url, out_dir, base)
        rows.append((path, caption))
    return rows


# ─── Job runner ───────────────────────────────────────────────────────────────

def run_job(job):
    status_path = job['status_path']
    stop_path = job.get('stop_path') or ''
    platform = (job.get('platform') or '').strip().lower()
    profile_path = job.get('profile_path') or ''
    mode = job.get('mode') or 'account'

    state = {'pct': 0, 'message': 'Menyiapkan...', 'done': False, 'success': False, 'error': ''}

    def log(pct, msg, **extra):
        state['pct'] = pct
        state['message'] = msg
        state.update(extra)
        write_status(status_path, **state)

    def should_stop():
        return bool(stop_path) and os.path.isfile(stop_path)

    if platform not in HOME_URL:
        log(0, '', done=True, success=False, error=f'Platform "{platform}" belum didukung mode ini.')
        return 1
    if not profile_path or not os.path.isdir(profile_path):
        log(0, '', done=True, success=False,
            error='Pilih profil GAM yang sudah login ke platform ini terlebih dahulu.')
        return 1

    out_dir = job.get('out_dir') or os.path.join(_base_dir(), 'downloads', platform)
    os.makedirs(out_dir, exist_ok=True)

    page = None
    try:
        log(2, f'Membuka Chrome profil untuk {platform}...')
        page = _connect(profile_path, headless=bool(job.get('invisible')))
        if not _is_logged_in(page, platform):
            log(0, '', done=True, success=False,
                error=f'Profil ini belum login {platform}. Buka & login dulu lewat kartu profil, baru coba lagi.')
            return 1

        if mode == 'account':
            target = (job.get('target') or '').strip().lstrip('@')
            if not target:
                raise ValueError('Isi username akun target dulu.')
            raw_limit = int(job.get('limit') or 0)
            links = collect_account_links(page, platform, target, raw_limit, log)
            if not links:
                raise ValueError(
                    f'Tidak ada postingan ditemukan untuk @{target}. Cek username-nya, '
                    'atau akun tersebut privat/tidak ada video.'
                )
            sub_out = os.path.join(out_dir, re.sub(r'[^\w\-.]+', '_', target)[:40])
            os.makedirs(sub_out, exist_ok=True)
        else:
            links = [u.strip() for u in (job.get('urls') or []) if u.strip()]
            if not links:
                raise ValueError('Masukkan minimal 1 URL.')
            sub_out = out_dir

        saved, errors, csv_rows = [], [], []
        total = len(links)
        for i, url in enumerate(links, 1):
            if should_stop():
                log(int(20 + (i - 1) / total * 70), f'Dihentikan. {len(saved)} berhasil.',
                    done=True, success=bool(saved), files=saved, stopped=True)
                return 0
            log(int(20 + (i - 1) / total * 70), f'Mengunduh {i}/{total}: {url[:70]}')
            try:
                rows = download_one_post(platform, page, url, sub_out, i, log)
                for path, caption in rows:
                    caption = caption or ''
                    tags = re.findall(r'#[\w\d_]+', caption)
                    hashtags = ','.join(t.lstrip('#') for t in tags)
                    clean_caption = re.sub(r'\s{2,}', ' ', re.sub(r'#[\w\d_]+', '', caption)).strip()
                    saved.append(path)
                    csv_rows.append([path, clean_caption, hashtags, '', url])
            except Exception as e:
                errors.append(f'{i}. {url[:60]}: {str(e)[:150]}')

        if not saved:
            raise ValueError('Semua unduhan gagal:\n' + '\n'.join(errors[:5]))

        csv_path = os.path.join(sub_out, 'rekap_video.csv')
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            f.write('video_path;caption;hashtags;produk;product_link\n')
            for row in csv_rows:
                f.write(';'.join(str(c).replace(';', ',').replace('\n', ' ') for c in row) + '\n')

        msg = f'Selesai! {len(saved)} media {platform} tersimpan di {sub_out}'
        if errors:
            msg += f' ({len(errors)} gagal)'
        log(100, msg, done=True, success=True, files=saved, folder=sub_out,
            csv=csv_path, error='; '.join(errors[:3]))
        return 0
    except Exception as e:
        log(0, '', done=True, success=False, error=str(e))
        return 1
    finally:
        if page is not None:
            try:
                page.quit()
            except Exception:
                pass


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
