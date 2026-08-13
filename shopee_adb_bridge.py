"""
shopee_adb_bridge.py — Download Shopee Video dari HP Android via ADB untuk GAMA.

Shopee mengunci feed Shopee Video hanya di aplikasi HP (browser diblokir),
jadi satu-satunya jalur andal adalah lewat HP asli. Worker ini:

  1. Membuka profil kreator target di aplikasi Shopee lewat deep link (link share).
  2. Menyusuri video (swipe) supaya aplikasi men-stream & men-cache tiap video.
  3. Shopee/ExoPlayer menyimpan video sebagai potongan `.exo` di
     files/mmsplayer/cache/. Potongan dengan uid sama = satu video; digabung
     urut-offset menghasilkan MP4 asli (tanpa watermark, kualitas penuh).
  4. Tiap video juga difoto layarnya (screenshot) supaya caption + produk +
     link affiliate tetap terbaca user, dan dicoba dibaca via uiautomator.

Dijalankan sebagai subprocess:
    python shopee_adb_bridge.py <job_json_path>
    python shopee_adb_bridge.py --selftest --device <serial>

Job JSON: {device, target(share link/deep link), limit, out_dir, adb_path,
           package, status_path}
"""
import os
import re
import sys
import time
import json
import argparse
import subprocess

# Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
           else os.path.dirname(os.path.abspath(__file__))
FFPROBE = os.path.join(BASE_DIR, 'assets', 'bin', 'ffprobe.exe')


def _default_adb():
    """adb bundel GAMA dulu, fallback ke ShoveBot."""
    bundled = os.path.join(BASE_DIR, 'assets', 'platform-tools', 'adb.exe')
    if os.path.isfile(bundled):
        return bundled
    return os.path.join(os.environ.get('SHOVEBOT_DIR', r'D:\TOOLS\ShoveBot - 3.6'),
                        'tools', 'platform-tools', 'adb.exe')


DEFAULT_ADB = _default_adb()

PKG_BY_REGION = {
    'id': 'com.shopee.id', 'sg': 'com.shopee.sg', 'my': 'com.shopee.my',
    'th': 'com.shopee.th', 'ph': 'com.shopee.ph',
}


class Adb:
    def __init__(self, adb_path, serial):
        self.adb = adb_path
        self.serial = serial

    def _base(self):
        b = [self.adb]
        if self.serial:
            b += ['-s', self.serial]
        return b

    def shell(self, cmd, timeout=30):
        try:
            r = subprocess.run(self._base() + ['shell', cmd],
                               capture_output=True, text=True, timeout=timeout)
            return r.stdout
        except Exception:
            return ''

    def shell_bytes(self, cmd, timeout=60):
        try:
            r = subprocess.run(self._base() + ['exec-out', cmd],
                               capture_output=True, timeout=timeout)
            return r.stdout
        except Exception:
            return b''

    def pull(self, remote, local, timeout=120):
        try:
            r = subprocess.run(self._base() + ['pull', remote, local],
                               capture_output=True, text=True, timeout=timeout)
            return 'file pulled' in (r.stdout + r.stderr) or os.path.isfile(local)
        except Exception:
            return False

    def screen_size(self):
        out = self.shell('wm size', timeout=8)
        m = re.search(r'(\d+)x(\d+)', out)
        if m:
            return int(m.group(1)), int(m.group(2))
        return 1080, 2400

    def _dump_xml(self, retries=8, delay=0.8):
        """Dump UI hierarchy dan VERIFIKASI dump-nya benar sukses (bukan file
        basi dari panggilan sebelumnya) sebelum dipakai.

        BUG KRITIS yang diperbaiki: `uiautomator dump` sering gagal dengan
        'ERROR: could not get idle state' saat ada animasi (video sedang main)
        — TAPI exit code shell tetap 0 dan file /sdcard/gama_ui.xml LAMA (dari
        dump sukses sebelumnya, bisa MENIT yang lalu) masih ada di situ. Kode
        lama langsung pull+parse file itu TANPA cek apakah dump barusan benar
        sukses → sering baca STATE BASI (mis. teks dari sheet yang sudah
        ditutup lama), menyebabkan deteksi salah & navigasi ngaco. Sekarang:
        hapus file remote dulu, jalankan dump, WAJIB verifikasi output
        mengandung 'dumped to' (bukan 'ERROR'), retry singkat kalau gagal
        (animasi biasanya settle dalam <2 detik). Return None kalau tetap
        gagal setelah semua retry (caller harus anggap 'tidak terdeteksi',
        BUKAN diam-diam pakai data lama)."""
        remote = '/sdcard/gama_ui.xml'
        local = os.path.join(os.environ.get('TEMP', '.'), 'gama_ui.xml')
        for _attempt in range(retries):
            self.shell(f'rm -f {remote}', timeout=8)
            out = self.shell(f'uiautomator dump {remote} 2>&1', timeout=15)
            if 'dumped to' in out and 'ERROR' not in out:
                if self.pull(remote, local, timeout=15) and os.path.isfile(local):
                    try:
                        with open(local, encoding='utf-8') as f:
                            return f.read()
                    except Exception:
                        pass
            time.sleep(delay)
        return None

    def dump_texts(self):
        """uiautomator dump → list teks (kosong kalau gagal setelah retry)."""
        xml = self._dump_xml()
        if not xml:
            return []
        return [t for t in re.findall(r'text="([^"]*)"', xml)]

    def find_bounds(self, needle):
        """Kembalikan titik tengah (x,y) node PERTAMA yang teksnya mengandung
        needle (urutan dokumen, bukan urutan posisi layar)."""
        matches = self.find_all_bounds(needle)
        return matches[0] if matches else None

    def find_all_bounds(self, needle):
        """Kembalikan list titik tengah (x,y) SEMUA node yang teksnya mengandung
        needle. Dipakai saat needle bisa ambigu (cocok di beberapa elemen) dan
        caller perlu memilih berdasar posisi, bukan cuma match pertama."""
        xml = self._dump_xml()
        if not xml:
            return []
        out = []
        for m in re.finditer(r'text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            if needle.lower() in m.group(1).lower():
                x1, y1, x2, y2 = map(int, m.groups()[1:])
                out.append(((x1 + x2) // 2, (y1 + y2) // 2))
        return out

    def tap(self, x, y):
        self.shell(f'input tap {int(x)} {int(y)}')

    def key(self, code):
        self.shell(f'input keyevent {code}')

    def screencap(self, local):
        data = self.shell_bytes('screencap -p')
        if not data:
            return False
        # normalisasi CRLF (adb exec-out biasanya sudah bersih, tapi jaga-jaga)
        with open(local, 'wb') as f:
            f.write(data)
        return os.path.getsize(local) > 1000


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


# ── ExoPlayer cache (.exo) → mp4 ─────────────────────────────────────────────

EXO_RE = re.compile(r'(\d+)\.(\d+)\.(\d+)\.v\d+\.exo$')


def clear_exo_cache(adb, pkg):
    """Hapus semua potongan .exo supaya video yang di-cache setelah ini pasti
    milik sesi/kreator yang sedang ditelusuri (cache mmsplayer campur semua
    video yang pernah ditonton user)."""
    cache = f'/sdcard/Android/data/{pkg}/files/mmsplayer/cache'
    adb.shell(f"find {cache} -iname '*.exo' -delete 2>/dev/null", timeout=30)


def list_exo_chunks(adb, pkg):
    """Kembalikan dict uid -> list of (offset, size, mtime, remote_path)."""
    cache = f'/sdcard/Android/data/{pkg}/files/mmsplayer/cache'
    # format: "<size> <mtime_epoch> <path>"
    out = adb.shell(
        f"find {cache} -iname '*.exo' 2>/dev/null | while read f; do "
        f"stat -c '%s %Y %n' \"$f\"; done", timeout=40)
    chunks = {}
    for line in out.splitlines():
        parts = line.strip().split(' ', 2)
        if len(parts) != 3:
            continue
        size_s, mtime_s, path = parts
        m = EXO_RE.search(path)
        if not m:
            continue
        uid, offset = m.group(1), int(m.group(2))
        try:
            size, mtime = int(size_s), int(mtime_s)
        except ValueError:
            continue
        chunks.setdefault(uid, []).append((offset, size, mtime, path))
    return chunks


def reconstruct_video(adb, uid, parts, work_dir, out_path):
    """Gabung potongan .exo (urut offset) jadi mp4. Hanya kalau kontigu dari 0."""
    parts = sorted(parts, key=lambda x: x[0])
    if not parts or parts[0][0] != 0:
        return False, 'potongan awal (offset 0) tidak ada'
    # cek kontiguitas
    expected = 0
    for offset, size, _mt, _p in parts:
        if offset != expected:
            return False, 'potongan tidak lengkap (ada bagian video yang belum di-cache)'
        expected += size
    # pull semua potongan lalu gabung
    tmp_files = []
    try:
        for i, (offset, size, _mt, path) in enumerate(parts):
            local = os.path.join(work_dir, f'{uid}_{i}.part')
            if not adb.pull(path, local):
                return False, f'gagal tarik potongan {i}'
            tmp_files.append(local)
        with open(out_path, 'wb') as out:
            for tf in tmp_files:
                with open(tf, 'rb') as f:
                    out.write(f.read())
    finally:
        for tf in tmp_files:
            try:
                os.remove(tf)
            except OSError:
                pass
    # validasi mp4
    if os.path.getsize(out_path) < 20000:
        return False, 'file hasil terlalu kecil'
    with open(out_path, 'rb') as f:
        head = f.read(16)
    if b'ftyp' not in head:
        return False, 'bukan mp4 valid (mungkin terenkripsi/DRM)'
    return True, ''


def capture_product_link(adb, W, H):
    """Best-effort ambil link produk/affiliate dari video yang sedang tampil.

    Alur TERVERIFIKASI LIVE end-to-end (device asli, 21 Jul):
      1. tap kartu produk (kiri-bawah, di ATAS caption) → sheet detail produk
      2. tap ikon Share (kanan-atas sheet) → sheet 'Bagikan link & komisi'
      3. tap 'Salin Link' → link affiliate masuk clipboard (sheet TIDAK
         auto-close setelah ini — harus ditutup manual, 2x BACK)
      4. tap ikon search Shopee (posisi tetap, BUKAN cari teks 'Cari' yang
         ambigu dengan chip 'Cari produk di video' di dalam video)
      5. tap field 'Cari produk, toko dan lainnya...' → PASTE → baca URL
      6. tutup search screen (2x BACK) — video-feed 'Untuk Anda' boleh
         menampilkan video lain setelah ini (refresh normal Shopee, bukan bug)
    `capture_product_link` hanya bertanggung jawab MEMBUKA kartu produk (retry
    di posisi yang SAMA, tanpa tap buta lain — itu penyebab lama 'swipe
    kemana-mana'). Sisa navigasi (buka+tutup) sepenuhnya di `_share_copy_read`."""
    url = ''
    try:
        share = None
        for _attempt in range(3):
            adb.tap(W * 0.13, H * 0.72)
            time.sleep(2.5)
            share = _find_share_icon(adb)
            if share:
                break
            time.sleep(1.0)  # retry di posisi SAMA, tanpa tap lain
        if share:
            url = _share_copy_read(adb, W, H, share)
    except Exception:
        url = ''
    return url


def _screen_is_static(adb, W=None, H=None, thresh=6.0):
    """Deteksi CEPAT & ANDAL: apakah layar sekarang STATIS (sheet/dialog/layar
    lain menutupi) atau masih video mentah yang beranimasi terus.

    AKAR MASALAH yang diperbaiki (ditemukan live, device 90f4455b7d78, akun
    zs7zb7u0): deteksi lama pakai `uiautomator dump` + marker teks. Tapi
    `uiautomator dump` WAJIB menunggu 'idle state' — dan video Shopee yang
    sedang main TIDAK PERNAH idle (animasi terus-menerus), jadi dump bisa
    HANG/gagal berkali-kali sampai >100 DETIK (terverifikasi: 1x panggilan
    makan waktu 102 detik lalu tetap gagal total) alih-alih gagal cepat.
    Ini fatal untuk flow yang butuh cek berkali-kali per video.

    Gantinya: ambil 2 screenshot berjarak ~0.3 detik, ukur beda piksel
    (grayscale) di pita TENGAH layar (hindari status bar & mini-card bawah).
    Video mentah = beda tinggi (terverifikasi live: 69-89), sheet/layar
    statis apa pun di atasnya = beda ~0 (terverifikasi live: 0.0). Screenshot
    sendiri TIDAK menunggu idle sama sekali jadi selalu cepat (~1-2 detik)
    apa pun kondisi layarnya."""
    try:
        if W is None or H is None:
            W, H = adb.screen_size()
        box = (int(W * 0.1), int(H * 0.25), int(W * 0.9), int(H * 0.55))
        tmp = os.environ.get('TEMP', '.')
        p1 = os.path.join(tmp, 'gama_diff_a.png')
        p2 = os.path.join(tmp, 'gama_diff_b.png')
        if not adb.screencap(p1):
            return False
        time.sleep(0.3)
        if not adb.screencap(p2):
            return False
        from PIL import Image
        import numpy as np
        a = np.asarray(Image.open(p1).convert('L').crop(box), dtype=np.int16)
        b = np.asarray(Image.open(p2).convert('L').crop(box), dtype=np.int16)
        return float(np.abs(a - b).mean()) < thresh
    except Exception:
        return False


def _find_share_icon(adb):
    """Kalau sheet detail produk PENUH terbuka (bukan mini-card kecil yang
    SELALU nempel di video), kembalikan titik ikon Share (kanan-atas sheet).
    Return (x,y) atau None kalau belum terbuka.

    Dulu deteksi pakai marker teks ('official'/'beli dengan voucher' dst)
    lewat uiautomator dump — selain rapuh (mini-card juga sempat ke-match via
    'terjual'), uiautomator sendiri terbukti bisa hang lama selama video
    masih main (lihat `_screen_is_static`). Sekarang pakai screenshot-diff:
    mini-card kecil TIDAK menutupi pita tengah layar (video di baliknya masih
    animasi → diff tinggi → None), sheet detail PENUH menutupi pita itu
    (statis → diff ~0 → sheet terdeteksi terbuka)."""
    W, H = adb.screen_size()
    if _screen_is_static(adb, W, H):
        return (int(W * 0.71), int(H * 0.19))
    return None


def _read_pasted_url(adb):
    for t in adb.dump_texts():
        m = re.search(r'https?://\S*shopee\S+|https?://s\.shopee\S+|https?://\S*shp\.ee\S+', t)
        if m:
            return m.group(0)
    return ''


def _find_top_search_field(adb, W, H, y_frac=0.25):
    """Cari field pencarian dekat ATAS layar (posisi, bukan placeholder
    persis, karena placeholder beda antar layout). Dipanggil HANYA saat layar
    sudah dipastikan statis (lihat `_open_search_and_paste`) jadi
    `uiautomator dump` aman & cepat di sini. Return (x,y) atau None."""
    candidates = [b for b in adb.find_all_bounds('cari') if b[1] < H * y_frac]
    if not candidates:
        return None
    candidates.sort(key=lambda b: b[1])
    return candidates[0]


def _open_search_and_paste(adb, W, H):
    """Buka layar pencarian Shopee lalu paste clipboard & baca URL affiliate.

    Shopee punya (minimal) 2 layout header: tab 'Untuk Anda' (ikon search
    KECIL di pojok kiri-atas, ~0.125W,0.056H) vs layar video profil kreator
    (search BAR PENUH sudah tampil permanen di atas, tap di titik mana pun
    pada bar langsung buka layar pencarian dengan field SUDAH FOKUS + keyboard
    tampil — TERVERIFIKASI LIVE, tak perlu tap kedua). Coba posisi bar-penuh
    dulu (target lebar, aman), fallback ke posisi ikon kecil kalau yang
    pertama tidak membuka apa-apa (dideteksi via `_screen_is_static` —
    dipilih justru karena TIDAK bergantung pada uiautomator sama sekali,
    jadi aman dipanggil walau skrin saat itu masih video yang beranimasi)."""
    opened = False
    for x_frac, y_frac in ((0.4, 0.06), (0.125, 0.056)):
        adb.tap(W * x_frac, H * y_frac)
        time.sleep(1.8)
        if _screen_is_static(adb, W, H):
            opened = True
            break
    if not opened:
        return ''
    # Skrin sudah statis (bukan video lagi) → uiautomator dump AMAN & cepat.
    field = _find_top_search_field(adb, W, H)
    if field:
        adb.tap(*field)
        time.sleep(0.8)
    adb.key('279')  # PASTE
    time.sleep(1.2)
    return _read_pasted_url(adb)


def _share_copy_read(adb, W, H, share_point=None):
    """Dari sheet detail produk yang sedang terbuka: tap ikon Share → Salin
    Link → tutup 2 sheet (verifikasi lewat `_screen_is_static`, BUKAN asal 2x
    BACK — beda layout Shopee punya kedalaman navigasi beda, BACK buta bisa
    overshoot ke layar lain) → buka pencarian & paste (`_open_search_and_paste`)
    → bersihkan field → tutup search. Fungsi ini MASUK dan KELUAR sepenuhnya
    sendiri (self-contained) — caller tidak perlu BACK tambahan apa pun.
    Kembalikan URL affiliate atau ''."""
    try:
        adb.tap(*(share_point if share_point else (W * 0.715, H * 0.19)))
        time.sleep(2.5)
        salin = adb.find_bounds('Salin Link') or adb.find_bounds('Salin Tautan')
        if salin:
            adb.tap(*salin)
            time.sleep(1.5)
        # VERIFIED: tap 'Salin Link' TIDAK menutup share sheet — tutup manual.
        adb.key('KEYCODE_BACK'); time.sleep(1.3)   # tutup share sheet
        # Detail-sheet: CEK dulu sebelum BACK lagi (jangan asal 2x) — kalau
        # sudah ikut tertutup, BACK kedua bisa overshoot ke layar lain
        # (mis. halaman profil kreator).
        if _find_share_icon(adb):
            adb.key('KEYCODE_BACK'); time.sleep(1.3)
        if not salin:
            return ''

        url = _open_search_and_paste(adb, W, H)
        try:
            adb.shell('input keyevent KEYCODE_MOVE_END')
            for _ in range(60):
                adb.key('67')  # DEL — bersihkan field pencarian
        except Exception:
            pass
        adb.key('KEYCODE_BACK'); time.sleep(1.2)   # tutup keyboard/search
        adb.key('KEYCODE_BACK'); time.sleep(1.2)   # tutup layar search (kalau ada)
        return url
    except Exception:
        return ''


def affiliate_link_for_url(adb, product_url):
    """Buka link produk di aplikasi Shopee via deep link lalu ambil link
    affiliate (dari akun yang login di HP). Tahan dua kondisi: halaman produk
    (Share langsung) atau layar video (tap kartu produk dulu). Return URL/''."""
    try:
        W, H = adb.screen_size()
        adb.shell('input keyevent KEYCODE_WAKEUP')
        time.sleep(1)
        adb.shell(f'am start -a android.intent.action.VIEW -d "{product_url}"', timeout=20)
        time.sleep(8)
        # Kalau mendarat di layar video (ada "Lihat Produk"), buka kartu produk dulu
        if adb.find_bounds('Lihat Produk'):
            adb.tap(W * 0.30, H * 0.60)
            time.sleep(3)
        link = _share_copy_read(adb, W, H)
        adb.key('KEYCODE_HOME')
        return link
    except Exception:
        try:
            adb.key('KEYCODE_HOME')
        except Exception:
            pass
        return ''


def probe_duration(path):
    if not os.path.isfile(FFPROBE):
        return None
    try:
        r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'default=nw=1:nk=1', path],
                           capture_output=True, text=True, timeout=20)
        return float(r.stdout.strip())
    except Exception:
        return None


# ── Metadata via uiautomator (best-effort) ───────────────────────────────────

import html as _html

_ZW = dict.fromkeys(map(ord, '​‌‍⁠﻿'), None)


def _clean_text(s):
    """Hapus karakter zero-width yang disisipkan Shopee + unescape HTML entity."""
    return _html.unescape(str(s or '').translate(_ZW)).strip()


def try_dump_metadata(adb):
    """Coba baca caption/hashtag/produk dari layar video via uiautomator.
    Kalau gagal (mis. animasi bikin tak pernah idle) return {}."""
    adb.shell('uiautomator dump --compressed /sdcard/gama_ui.xml 2>/dev/null', timeout=15)
    local = os.path.join(os.environ.get('TEMP', '.'), 'gama_ui.xml')
    if not adb.pull('/sdcard/gama_ui.xml', local, timeout=15):
        return {}
    try:
        with open(local, encoding='utf-8') as f:
            xml = f.read()
    except Exception:
        return {}
    texts = [_clean_text(t) for t in re.findall(r'text="([^"]*)"', xml)]
    texts = [t for t in texts if t]

    # Caption kreator = node yang memuat hashtag (bukan kartu produk).
    caption = ''
    for t in texts:
        if '#' in t and 'terjual' not in t.lower():
            # buang penanda "Lihat lainnya/lain..." di ekor
            caption = re.sub(r'\s*(Lihat lain\w*|\.\.\.)\s*$', '', t).strip()
            break
    hashtags = ','.join(dict.fromkeys(
        m.lstrip('#') for m in re.findall(r'#[\w\d_]+', caption or ' '.join(texts))))
    if caption:
        caption = re.sub(r'#[\w\d_]+', '', caption)
        caption = re.sub(r'\s{2,}', ' ', caption).strip()

    # Nama produk = judul panjang (untuk referensi + konversi affiliate nanti).
    products = []
    for t in texts:
        low = t.lower()
        if len(t) > 20 and any(k in low for k in (
                'jam tangan', 'strap', 'analog', 'quartz', 'stainless', 'fashion')):
            products.append(t)
    # dedup jaga urutan
    products = list(dict.fromkeys(products))
    return {'caption': caption, 'hashtags': hashtags, 'products': products}


# ── Job utama ────────────────────────────────────────────────────────────────

def run_job(job):
    status_path = job['status_path']
    adb = Adb(job.get('adb_path') or DEFAULT_ADB, job.get('device') or '')
    target = (job.get('target') or '').strip()
    region = (job.get('region') or 'id').lower()
    pkg = job.get('package') or PKG_BY_REGION.get(region, 'com.shopee.id')
    raw_limit = int(job.get('limit') or 0)
    download_all = raw_limit <= 0          # 0 / kosong = ambil SEMUA video kreator
    SAFETY_CAP = 1000                      # batas aman biar tidak loop selamanya
    limit = SAFETY_CAP if download_all else max(1, raw_limit)  # TANPA cap 30 lagi
    with_links = bool(job.get('with_links'))
    out_root = job.get('out_dir') or os.path.join(BASE_DIR, 'downloads', 'shopee_akun')
    stop_path = job.get('stop_path') or ''

    def stop_requested():
        return bool(stop_path) and os.path.isfile(stop_path)

    state = {'pct': 0, 'message': 'Menyiapkan HP...', 'done': False, 'success': False, 'error': ''}

    def flush():
        write_status(status_path, **state)

    def log(pct, msg):
        state['pct'] = pct
        state['message'] = msg
        flush()

    flush()

    if not os.path.isfile(adb.adb):
        state.update(done=True, error=f'adb.exe tidak ditemukan: {adb.adb}')
        flush(); return 1
    if not target:
        state.update(done=True, error='Isi link share profil Shopee Video (mis. https://id.shp.ee/xxxx).')
        flush(); return 1

    # cek device online
    devs = adb.shell('echo ok', timeout=10)
    if 'ok' not in devs:
        state.update(done=True, error='HP tidak merespons. Cek kabel/USB debugging dan otorisasi ADB di HP.')
        flush(); return 1

    out_dir = os.path.join(out_root, re.sub(r'[^\w\-.]+', '_', target.split('/')[-1])[:40] or 'shopee_hp')
    work_dir = os.path.join(out_dir, '_tmp')
    os.makedirs(work_dir, exist_ok=True)

    try:
        log(5, 'Membangunkan layar HP...')
        adb.shell('input keyevent KEYCODE_WAKEUP')
        time.sleep(1)
        adb.shell('input keyevent 82')  # coba buka kunci swipe
        time.sleep(1)

        log(10, 'Membuka profil kreator di aplikasi Shopee...')
        # deep link share → aplikasi Shopee membuka halaman profil
        adb.shell(f'am start -a android.intent.action.VIEW -d "{target}"', timeout=20)
        time.sleep(9)

        # Kosongkan cache video supaya yang ter-cache setelah ini = video kreator ini
        log(13, 'Menyiapkan cache HP...')
        clear_exo_cache(adb, pkg)

        W, H = adb.screen_size()

        # Buka video pertama dari grid (kiri-atas area posting)
        log(15, 'Membuka video pertama...')
        adb.tap(W * 0.16, H * 0.54)
        time.sleep(6)

        metas = []
        seen_uids = set()          # uid video unik yang sudah ter-cache
        stall = 0                  # berapa kali swipe berturut tanpa video baru
        STALL_LIMIT = 4            # 4x tanpa video baru → dianggap ujung feed
        stopped_by_user = False
        i = 0
        while i < limit:
            i += 1
            denom = (raw_limit if not download_all else max(i, len(seen_uids) or 1))
            label_total = '∞' if download_all else str(raw_limit)
            log(15 + int((i - 1) / max(denom, 1) * 45),
                f'Menyusuri & memuat video {i}/{label_total} di HP...')
            # biarkan video buffer penuh (video pendek ~ beberapa detik)
            time.sleep(4)
            # screenshot layar video (untuk caption/produk manual)
            shot = os.path.join(out_dir, f'{i:02d}_layar.png')
            adb.screencap(shot)
            # coba baca metadata (sering gagal karena animasi)
            meta = try_dump_metadata(adb)
            meta['screenshot'] = shot
            # opsional: ambil link produk/affiliate (lebih lambat, navigasi keluar-masuk)
            if with_links:
                log(15 + int((i - 1) / max(denom, 1) * 45),
                    f'Mengambil link produk video {i}/{label_total}...')
                meta['link'] = capture_product_link(adb, W, H)
            metas.append(meta)

            # Deteksi ujung feed (untuk mode "download semua" & agar tidak swipe
            # melewati batas): hitung uid unik yang sudah ter-cache.
            now_uids = set(list_exo_chunks(adb, pkg).keys())
            new_uids = now_uids - seen_uids
            if new_uids:
                seen_uids |= now_uids
                stall = 0
            else:
                stall += 1
            if stall >= STALL_LIMIT:
                log(60, f'Ujung feed tercapai — {len(seen_uids)} video terkumpul.')
                break

            # Stop kooperatif: user klik tombol Stop di UI (main.py tulis file
            # flag `stop_path`, lihat `stop_requested`) — BUKAN kill paksa
            # proses (itu bikin loop terpotong SEBELUM sampai ke rekonstruksi
            # video+tulis CSV di bawah, jadi hasilnya nihil walau videonya
            # sudah ter-cache di HP). Video ke-i yang baru saja diproses di
            # atas TETAP dihitung — berhenti di sini lalu lanjut normal ke
            # rekonstruksi+CSV, persis seperti kalau ujung feed tercapai.
            if stop_requested():
                stopped_by_user = True
                log(60, f'Dihentikan oleh pengguna — memproses {len(seen_uids)} video yang sudah diambil...')
                break

            # capture_product_link (kalau with_links aktif) sudah balik ke feed
            # sendiri lewat BACK deterministik; kalau with_links mati, tidak ada
            # navigasi keluar sama sekali. Jadi langsung swipe, tanpa deteksi.

            # Swipe ke video berikutnya di ZONA TENGAH video (hindari kartu
            # produk/action bar di bawah & header di atas). Durasi 600ms =
            # terkontrol, maju 1 video, tidak nge-fling.
            adb.shell(
                f'input swipe {int(W*0.5)} {int(H*0.62)} {int(W*0.5)} {int(H*0.32)} 600')
            time.sleep(2.5)

        # Beri waktu cache terakhir tersimpan
        log(62, 'Menyelesaikan cache video...')
        time.sleep(3)

        log(65, 'Merekonstruksi video dari cache HP...')
        chunks = list_exo_chunks(adb, pkg)
        # cache sudah dikosongkan di awal → semua uid = video sesi ini.
        # urut berdasar waktu cache paling awal (mendekati urutan tonton)
        ordered = sorted(chunks.items(), key=lambda kv: min(p[2] for p in kv[1]))

        saved, csv_rows, errors = [], [], []
        if not ordered:
            raise RuntimeError(
                'Tidak ada video baru ter-cache. Kemungkinan: link salah, profil tidak terbuka di '
                'aplikasi, atau HP butuh buka-kunci manual dulu. Buka HP, pastikan profil kreator '
                'tampil di aplikasi Shopee, lalu ulangi.')

        total = len(ordered)
        for idx, (uid, parts) in enumerate(ordered[:limit], 1):
            log(65 + int(idx / max(total, 1) * 30), f'Menyimpan video {idx}/{min(total, limit)}...')
            vpath = os.path.join(out_dir, f'{idx:02d}.mp4')
            ok, err = reconstruct_video(adb, uid, parts, work_dir, vpath)
            if not ok:
                errors.append(f'video {idx}: {err}')
                continue
            saved.append(vpath)
            meta = metas[idx - 1] if idx - 1 < len(metas) else {}
            caption = meta.get('caption', '')
            hashtags = meta.get('hashtags', '')
            products = ' | '.join(meta.get('products', []) or [])
            link = meta.get('link', '') or ''
            dur = probe_duration(vpath)
            # sidecar txt
            with open(os.path.join(out_dir, f'{idx:02d}.txt'), 'w', encoding='utf-8') as f:
                f.write((caption or '(caption belum terbaca — lihat screenshot ' +
                         f'{idx:02d}_layar.png)') + '\n')
                if hashtags:
                    f.write('\nHashtag: ' + hashtags + '\n')
                if products:
                    f.write('\nProduk: ' + products + '\n')
                if link:
                    f.write('\nLink produk (affiliate akun HP): ' + link + '\n')
                if dur:
                    f.write(f'\nDurasi: {dur:.1f}s\n')
            csv_rows.append([vpath, caption, hashtags, products, link])

        # bersihkan tmp
        try:
            for fn in os.listdir(work_dir):
                os.remove(os.path.join(work_dir, fn))
            os.rmdir(work_dir)
        except OSError:
            pass

        if not saved:
            raise RuntimeError('Semua rekonstruksi gagal: ' + '; '.join(errors[:3]))

        csv_path = os.path.join(out_dir, 'rekap_video.csv')
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            f.write('video_path;caption;hashtags;produk;product_link\n')
            for row in csv_rows:
                f.write(';'.join(str(c).replace(';', ',').replace('\n', ' ') for c in row) + '\n')

        # kembalikan HP ke home. Kadang launcher (MIUI) mendarat dalam mode
        # edit ikon (jiggle + tombol 'Selesai') kalau HOME ditekan setelah
        # jeda lama tanpa interaksi (rekonstruksi video bisa makan puluhan
        # detik) — terverifikasi live: 1x BACK tambahan membersihkan ini
        # dengan aman (no-op kalau memang sudah di home biasa).
        adb.shell('input keyevent KEYCODE_HOME')
        time.sleep(0.8)
        adb.shell('input keyevent KEYCODE_BACK')

        if stopped_by_user:
            msg = f'Dihentikan oleh pengguna. {len(saved)} video Shopee tersimpan di {out_dir}'
        else:
            msg = f'Selesai! {len(saved)} video Shopee tersimpan di {out_dir}'
        if errors:
            msg += f' ({len(errors)} gagal/parsial)'
        note = ('Caption/produk dibaca best-effort dari layar; kalau kosong, buka file '
                '..._layar.png untuk menyalin manual.')
        state.update(pct=100, message=msg + ' — ' + note, done=True, success=True,
                     stopped=stopped_by_user,
                     files=saved, folder=out_dir, csv=csv_path, error='; '.join(errors[:3]))
        flush()
        return 0
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
        flush()
        return 1


def run_selftest(device=None):
    adb = Adb(DEFAULT_ADB, device or '')
    result = {'adb_exists': os.path.isfile(adb.adb), 'ffprobe': os.path.isfile(FFPROBE),
              'device_ok': False, 'error': None}
    if result['adb_exists']:
        result['device_ok'] = 'ok' in adb.shell('echo ok', timeout=8)
    print(json.dumps(result))
    return 0 if result['adb_exists'] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job', nargs='?')
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('--device')
    args = parser.parse_args()
    if args.selftest:
        return run_selftest(args.device)
    if not args.job or not os.path.isfile(args.job):
        print('Job file tidak ditemukan', file=sys.stderr)
        return 2
    with open(args.job, 'r', encoding='utf-8') as f:
        job = json.load(f)
    return run_job(job)


if __name__ == '__main__':
    raise SystemExit(main())
