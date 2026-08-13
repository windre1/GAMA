"""
tiktok_downloader.py — Ambil video TikTok dari sebuah username, lengkap dengan
metadatanya.

PEMBAGIAN SUMBER (hibrida):

    yt-dlp   -> daftar video milik satu username.
                tikwm punya endpoint /user/posts, tapi sudah diuji dan
                dijaga Cloudflare ("Just a moment..."), jadi tidak dipakai.

    tikwm    -> detail per video: file TANPA WATERMARK, caption, musik,
                dan statistik. Ini sumber yang sama yang sudah dipakai
                downloader TikTok GAMA sebelumnya.

    ADB/HP   -> hanya untuk yang tidak tersedia di web: link produk
                affiliate TikTok Shop dan tag lokasi. Sudah diuji: field
                `anchors` di tikwm sering kosong walau videonya jelas
                punya keranjang produk.

Hasil akhir per video: file .mp4 + file .txt berisi rekap seluruh metadata.
"""

import json
import os
import re

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36')

_TIKWM = 'https://www.tikwm.com/api//'


# ── Daftar video dari username ──────────────────────────────────────────────

def bersihkan_username(nilai: str) -> str:
    """'@budi', 'tiktok.com/@budi', 'budi' -> 'budi'."""
    s = (nilai or '').strip()
    m = re.search(r'tiktok\.com/@([^/?#]+)', s, re.I)
    if m:
        return m.group(1)
    return s.lstrip('@').strip()


def daftar_video(username: str, batas: int = 20, log=print) -> list:
    """URL video terbaru milik `username`, terbaru dulu."""
    import yt_dlp

    user = bersihkan_username(username)
    if not user:
        return []
    log(f'[LIST] Mengambil daftar video @{user} (maks {batas})...')
    opsi = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'extract_flat': True, 'playlistend': int(batas),
    }
    try:
        with yt_dlp.YoutubeDL(opsi) as y:
            info = y.extract_info(f'https://www.tiktok.com/@{user}', download=False)
    except Exception as e:
        log(f'[LIST] Gagal: {e}')
        return []

    hasil = []
    for e in (info.get('entries') or []):
        if not e:
            continue
        u = e.get('url') or ''
        if not u and e.get('id'):
            u = f"https://www.tiktok.com/@{user}/video/{e['id']}"
        if u:
            hasil.append(u)
    log(f'[LIST] {len(hasil)} video ditemukan.')
    return hasil


# ── Detail satu video ───────────────────────────────────────────────────────

def _hashtag_dari(teks: str) -> list:
    """Hashtag diambil dari caption karena tikwm tidak memberi daftar terpisah."""
    return re.findall(r'#([^\s#@]+)', teks or '')


def _caption_bersih(teks: str) -> str:
    """Caption tanpa hashtag di belakangnya — untuk dipakai ulang saat posting."""
    return re.sub(r'\s*#[^\s#@]+', '', teks or '').strip()


def _urai_anchors(anchors) -> dict:
    """Ambil produk & lokasi dari `anchors` kalau kebetulan tersedia.

    Bentuknya berubah-ubah dan sering kosong, jadi dibaca seadanya dan tidak
    pernah dianggap sumber utama — pengambilan yang bisa diandalkan lewat HP.
    """
    produk, lokasi = [], ''
    for a in (anchors or []):
        if not isinstance(a, dict):
            continue
        kunci = str(a.get('component_key') or '').lower()
        judul = a.get('keyword') or a.get('description') or ''
        tautan = a.get('schema') or a.get('url') or ''
        try:
            ekstra = json.loads(a.get('extra') or '{}')
        except Exception:
            ekstra = {}
        tautan = tautan or ekstra.get('url') or ekstra.get('schema') or ''

        if 'poi' in kunci or 'location' in kunci:
            lokasi = judul or lokasi
        elif any(k in kunci for k in ('shop', 'product', 'commerce', 'ecom')):
            produk.append({'nama': judul, 'link': tautan})
    return {'produk': produk, 'lokasi': lokasi}


def ambil_detail(url: str, timeout: int = 25) -> dict:
    """Metadata satu video lewat tikwm. Raise kalau gagal."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    r = requests.get(_TIKWM, params={'url': url, 'hd': 1},
                     headers={'User-Agent': _UA}, timeout=timeout, verify=False)
    r.raise_for_status()
    j = r.json()
    if j.get('code') != 0:
        raise ValueError(f"TikWM menolak: {j.get('msg')}")
    return detail_dari_data(url, j.get('data') or {})


def detail_dari_data(url: str, d: dict) -> dict:
    """Urai data mentah tikwm yang SUDAH dipegang pemanggil.

    Dipisah dari ambil_detail() supaya alur unduhan yang sudah memanggil
    tikwm tidak perlu memanggilnya dua kali hanya untuk melengkapi metadata.
    """
    musik = d.get('music_info') or {}
    penulis = d.get('author') or {}
    caption = d.get('title') or ''
    jangkar = _urai_anchors(d.get('anchors'))

    return {
        'url': url,
        'id': d.get('id') or '',
        'username': penulis.get('unique_id') or '',
        'nama_akun': penulis.get('nickname') or '',
        'caption_penuh': caption,
        'caption': _caption_bersih(caption),
        'hashtags': _hashtag_dari(caption),
        'durasi': d.get('duration') or 0,
        'region': d.get('region') or '',
        'dibuat': d.get('create_time') or 0,
        'views': d.get('play_count'),
        'likes': d.get('digg_count'),
        'komentar': d.get('comment_count'),
        'share': d.get('share_count'),
        'simpan': d.get('collect_count'),
        'musik_judul': musik.get('title') or '',
        'musik_artis': musik.get('author') or '',
        'musik_album': musik.get('album') or '',
        'musik_durasi': musik.get('duration') or 0,
        'musik_asli': bool(musik.get('original')),
        'musik_url': d.get('music') or musik.get('play') or '',
        'musik_id': musik.get('id') or '',
        # Dari web; dilengkapi/ditimpa hasil HP kalau jalur ADB dipakai.
        'produk': jangkar['produk'],
        'lokasi': jangkar['lokasi'],
        # URL unduhan: hdplay > play (dua-duanya TANPA watermark),
        # wmplay sengaja tidak dipakai karena ber-watermark.
        '_unduh': d.get('hdplay') or d.get('play') or '',
        '_ukuran': d.get('hd_size') or d.get('size') or 0,
        '_cover': d.get('origin_cover') or d.get('cover') or '',
    }


# ── Pelengkap dari halaman TikTok (lokasi & produk) ─────────────────────────
#
# tikwm tidak memberikan tag lokasi maupun link produk — field `anchors`-nya
# sering kosong walau videonya jelas punya. Sudah diuji juga lewat HP: TikTok
# memblokir pembacaan tampilan (uiautomator mengembalikan dump kosong), jadi
# membaca dari layar aplikasi bukan pilihan.
#
# Yang bekerja: halaman video di web menyimpan seluruh data video pada
# <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">. Di situ ada field `poi`
# berisi nama + alamat lengkap tempat — persis format yang dipakai kolom
# lokasi pada CSV upload, sehingga hasil unduhan bisa langsung dipakai ulang.

_JS_AMBIL_JSON = ("(document.getElementById("
                  "'__UNIVERSAL_DATA_FOR_REHYDRATION__')||{}).textContent||''")


def _item_struct(mentah: str) -> dict:
    try:
        d = json.loads(mentah)
    except Exception:
        return {}
    scope = d.get('__DEFAULT_SCOPE__') or {}
    detail = scope.get('webapp.video-detail') or {}
    return (detail.get('itemInfo') or {}).get('itemStruct') or {}


def _urai_produk(anchors) -> list:
    """Ambil produk dari `anchors`.

    Bentuk nyatanya bersarang tiga lapis (diperiksa langsung pada video
    ber-keranjang-kuning):

        anchors[i].extra  -> string JSON berisi LIST
          item.keyword    -> nama pendek produk, mis. "CELANA CARGO PRIA"
          item.id         -> product_id
          item.extra      -> string JSON lagi, berisi title panjang & gambar

    `anchors[i].schema` TIDAK dipakai sebagai link produk: pada video uji
    isinya justru "https://www.capcut.com" (jejak aplikasi editor), bukan
    tautan produknya. Link toko disusun dari product_id.
    """
    hasil = []
    for a in (anchors or []):
        if not isinstance(a, dict):
            continue
        try:
            daftar = json.loads(a.get('extra') or '[]')
        except Exception:
            daftar = []
        if isinstance(daftar, dict):
            daftar = [daftar]
        for it in daftar:
            if not isinstance(it, dict):
                continue
            pid = str(it.get('id') or '').strip()
            nama = (it.get('keyword') or '').strip()
            judul = ''
            try:
                dalam = json.loads(it.get('extra') or '{}')
                judul = (dalam.get('title') or '').strip()
                pid = pid or str(dalam.get('product_id') or '').strip()
            except Exception:
                pass
            if not (pid or nama):
                continue
            hasil.append({
                'nama': nama or judul[:60],
                'judul': judul,
                'id': pid,
                'link': f'https://shop.tiktok.com/view/product/{pid}' if pid else '',
            })
    return hasil


def _dari_item_struct(item: dict) -> dict:
    """Ambil lokasi & produk dari itemStruct halaman video."""
    poi = item.get('poi') or {}
    nama = (poi.get('name') or '').strip()
    alamat = (poi.get('address') or '').strip()
    # Digabung nama + alamat karena bentuk itulah yang diterima kolom
    # `location` pada CSV upload TikTok.
    lokasi = f'{nama} {alamat}'.strip() if nama else ''

    return {
        'lokasi': lokasi,
        'lokasi_nama': nama,
        'lokasi_alamat': alamat,
        'lokasi_kota': (poi.get('city') or '').strip(),
        'lokasi_id': (poi.get('id') or '').strip(),
        'produk': _urai_produk(item.get('anchors')),
    }


class PembacaHalaman:
    """Buka satu Chrome berprofil login, lalu baca banyak video lewat CDP.

    Dibuat sebagai context manager karena membuka browser per video terlalu
    mahal — satu jendela dipakai ulang untuk seluruh antrean.
    """

    def __init__(self, chrome_path: str, profile_path: str, port: int = 0,
                 log=print):
        self.chrome_path = chrome_path
        self.profile_path = profile_path
        self.log = log
        self.port = port
        self._proc = None
        self._ws = None
        self._id = 0

    def __enter__(self):
        import socket
        import subprocess
        if not self.port:
            s = socket.socket()
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
            s.close()
        self._proc = subprocess.Popen([
            self.chrome_path, f'--user-data-dir={self.profile_path}',
            f'--remote-debugging-port={self.port}',
            '--remote-debugging-address=127.0.0.1',
            '--no-first-run', '--no-default-browser-check',
            '--disable-notifications', '--window-size=1200,900',
            'about:blank',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._sambung()
        return self

    def _sambung(self, batas: int = 60):
        import urllib.request
        import websocket
        for _ in range(batas):
            import time as _t
            _t.sleep(1)
            try:
                tabs = json.load(urllib.request.urlopen(
                    f'http://127.0.0.1:{self.port}/json', timeout=3))
                halaman = [t for t in tabs if t.get('type') == 'page']
                if halaman:
                    self._ws = websocket.create_connection(
                        halaman[0]['webSocketDebuggerUrl'],
                        suppress_origin=True, timeout=60)
                    return
            except Exception:
                continue
        raise RuntimeError('Tidak bisa menyambung ke Chrome untuk membaca halaman.')

    def _cdp(self, method, **params):
        self._id += 1
        self._ws.send(json.dumps({'id': self._id, 'method': method,
                                  'params': params}))
        while True:
            m = json.loads(self._ws.recv())
            if m.get('id') == self._id:
                return m

    def _js(self, ekspresi):
        r = self._cdp('Runtime.evaluate', expression=ekspresi,
                      returnByValue=True, awaitPromise=True)
        hasil = r.get('result', {}).get('result', {})
        return hasil.get('value') if 'value' in hasil else None

    # Kata kunci halaman verifikasi TikTok. Halaman ini TIDAK memuat
    # __UNIVERSAL_DATA__, jadi tanpa deteksi khusus kegagalannya terlihat
    # seperti "video tidak punya lokasi" — menyesatkan.
    _CIRI_PUZZLE = ('verify', 'captcha', 'puzzle', 'geser', 'slide to',
                    'security check', 'verifikasi')

    def _ada_puzzle(self) -> bool:
        try:
            teks = (self._js('document.body ? document.body.innerText : ""') or '')[:400].lower()
        except Exception:
            return False
        return any(k in teks for k in self._CIRI_PUZZLE)

    def baca(self, url: str, tunggu: float = 12.0,
             batas_puzzle: float = 240.0) -> dict:
        """Lokasi & produk untuk satu video. {} kalau gagal.

        Kalau TikTok memunculkan puzzle verifikasi, fungsi ini MENUNGGU user
        menyelesaikannya di jendela Chrome yang terbuka, lalu lanjut sendiri.
        Puzzle biasanya hanya muncul sekali di awal sesi: setelah diselesaikan,
        cookie-nya berlaku untuk sisa antrean — karena itu satu jendela dipakai
        ulang untuk semua video, bukan buka-tutup per video.
        """
        import time as _t
        try:
            self._cdp('Page.navigate', url=url)
            _t.sleep(tunggu)

            mentah = self._js(_JS_AMBIL_JSON) or ''
            if not mentah and self._ada_puzzle():
                self.log('[HALAMAN] TikTok meminta verifikasi. Selesaikan '
                         'puzzle di jendela Chrome yang terbuka - GAMA lanjut '
                         'otomatis setelah itu.')
                batas = _t.time() + batas_puzzle
                while _t.time() < batas:
                    _t.sleep(3)
                    mentah = self._js(_JS_AMBIL_JSON) or ''
                    if mentah:
                        self.log('[HALAMAN] Verifikasi selesai, dilanjutkan.')
                        break
                    if not self._ada_puzzle():
                        # Puzzle hilang tapi data belum ada -> muat ulang.
                        self._cdp('Page.navigate', url=url)
                        _t.sleep(tunggu)
                        mentah = self._js(_JS_AMBIL_JSON) or ''
                        if mentah:
                            break
                else:
                    self.log('[HALAMAN] Verifikasi tidak selesai dalam '
                             f'{int(batas_puzzle)} detik - lokasi/produk dilewati.')

            if not mentah:
                return {}
            return _dari_item_struct(_item_struct(mentah))
        except Exception as e:
            self.log(f'[HALAMAN] Gagal membaca {url[-19:]}: {e}')
            return {}

    def daftar_video(self, username: str, batas: int = 20,
                     tunggu: float = 14.0) -> list:
        """Daftar URL video milik `username`, dibaca dari halaman profil.

        Dipakai sebagai cadangan yt-dlp: untuk sebagian akun yt-dlp gagal
        dengan "Unable to extract secondary user ID" dan tidak mengembalikan
        apa pun. Halaman profil tetap bisa dibaca selama browser-nya login,
        dan memakai jendela yang sama berarti verifikasi puzzle tidak terulang.
        """
        import time as _t
        user = bersihkan_username(username)
        if not user:
            return []
        try:
            self._cdp('Page.navigate', url=f'https://www.tiktok.com/@{user}')
            _t.sleep(tunggu)

            if self._ada_puzzle():
                self.log('[HALAMAN] Verifikasi diminta saat membuka profil. '
                         'Selesaikan puzzle di jendela Chrome - GAMA menunggu.')
                batas_waktu = _t.time() + 240
                while _t.time() < batas_waktu and self._ada_puzzle():
                    _t.sleep(3)
                _t.sleep(tunggu)

            # WAJIB dipastikan halaman profilnya benar-benar sudah berganti.
            # Tanpa ini, tautan yang terbaca masih milik halaman SEBELUMNYA —
            # terbukti saat pengujian: meminta @bng_aip tapi yang terunduh
            # video akun lain, tanpa satu pun pesan kesalahan.
            cocok = False
            batas_ganti = _t.time() + 30
            while _t.time() < batas_ganti:
                sekarang = (self._js('location.href') or '').lower()
                if f'/@{user.lower()}' in sekarang:
                    cocok = True
                    break
                _t.sleep(2)
            if not cocok:
                self.log(f'[HALAMAN] Halaman profil @{user} tidak terbuka '
                         f'(URL: {(self._js("location.href") or "")[:70]}).')
                return []

            terkumpul, macet = [], 0
            while len(terkumpul) < batas and macet < 3:
                # Tautan disaring: HANYA video milik username yang diminta,
                # supaya bagian "kamu mungkin suka" tidak ikut terbawa.
                mentah = self._js(
                    "JSON.stringify([...document.querySelectorAll('a[href*=\"/video/\"]')]"
                    ".map(a=>a.href).filter(h=>h.toLowerCase().includes('/@%s/video/')))"
                    % user.lower().replace("'", "")) or '[]'
                try:
                    baru = json.loads(mentah)
                except Exception:
                    baru = []
                sebelum = len(terkumpul)
                for u in baru:
                    if u not in terkumpul:
                        terkumpul.append(u)
                if len(terkumpul) == sebelum:
                    macet += 1
                else:
                    macet = 0
                if len(terkumpul) >= batas:
                    break
                # Gulir untuk memuat video berikutnya (profil TikTok lazy-load)
                self._js('window.scrollTo(0, document.body.scrollHeight)')
                _t.sleep(3)

            self.log(f'[HALAMAN] {len(terkumpul)} video ditemukan di profil @{user}.')
            return terkumpul[:batas]
        except Exception as e:
            self.log(f'[HALAMAN] Gagal membaca profil @{user}: {e}')
            return []

    def daftar_tautan(self, url: str, pola: str, batas: int = 20,
                      tunggu: float = 14.0) -> list:
        """Kumpulkan tautan dari sebuah halaman, digulir sampai cukup.

        Sengaja dibuat UMUM (url + pola regex) supaya bisa dipakai halaman
        pencarian platform mana pun, bukan cuma TikTok. Yang membedakan tiap
        platform hanya alamat pencarian dan bentuk tautan hasilnya.

        Berbeda dari daftar_video() yang menyaring ketat ke satu pemilik: hasil
        pencarian memang datang dari banyak akun, jadi penyaringnya cukup
        memastikan bentuk tautannya benar.
        """
        import time as _t
        try:
            self._cdp('Page.navigate', url=url)
            _t.sleep(tunggu)

            if self._ada_puzzle():
                self.log('[CARI] Verifikasi diminta. Selesaikan di jendela '
                         'Chrome - GAMA menunggu.')
                batas_waktu = _t.time() + 240
                while _t.time() < batas_waktu and self._ada_puzzle():
                    _t.sleep(3)
                _t.sleep(tunggu)

            terkumpul, macet = [], 0
            while len(terkumpul) < batas and macet < 4:
                mentah = self._js(
                    "JSON.stringify([...document.querySelectorAll('a[href]')]"
                    ".map(a=>a.href).filter(h=>%s.test(h)))" % pola
                ) or '[]'
                try:
                    baru = json.loads(mentah)
                except Exception:
                    baru = []
                sebelum = len(terkumpul)
                for u in baru:
                    if u not in terkumpul:
                        terkumpul.append(u)
                macet = macet + 1 if len(terkumpul) == sebelum else 0
                if len(terkumpul) >= batas:
                    break
                self._js('window.scrollTo(0, document.body.scrollHeight)')
                _t.sleep(3)

            return terkumpul[:batas]
        except Exception as e:
            self.log(f'[CARI] Gagal membaca {url[:60]}: {e}')
            return []

    def daftar_dari_pencarian(self, keyword: str, batas: int = 20,
                              tunggu: float = 14.0) -> list:
        """Hasil pencarian TikTok untuk sebuah kata kunci.

        yt-dlp tidak punya pencarian TikTok sama sekali, jadi jalur browser
        inilah satu-satunya cara.
        """
        from urllib.parse import quote as _quote
        kw = (keyword or '').strip()
        if not kw:
            return []
        hasil = self.daftar_tautan(
            f'https://www.tiktok.com/search/video?q={_quote(kw)}',
            r'/\/@[^/]+\/video\/\d+/', batas, tunggu)
        self.log(f'[CARI] {len(hasil)} video ditemukan untuk "{kw}".')
        return hasil

    def __exit__(self, *a):
        try:
            self._ws and self._ws.close()
        except Exception:
            pass
        try:
            self._proc and self._proc.terminate()
        except Exception:
            pass
        return False


# ── Unduh berkas ────────────────────────────────────────────────────────────

def _nama_aman(teks: str, cadangan: str = 'tiktok') -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', (teks or '').strip())
    s = re.sub(r'\s+', ' ', s).strip(' ._')
    return (s[:70] or cadangan)


def _unduh_ke(url: str, tujuan: str, timeout: int = 120) -> str:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    r = requests.get(url, headers={'User-Agent': _UA}, timeout=timeout,
                     stream=True, verify=False)
    r.raise_for_status()
    os.makedirs(os.path.dirname(tujuan) or '.', exist_ok=True)
    with open(tujuan, 'wb') as f:
        for potongan in r.iter_content(chunk_size=1 << 17):
            if potongan:
                f.write(potongan)
    return tujuan


def unduh_video(detail: dict, folder: str, nama_dasar: str = '') -> str:
    """Simpan video TANPA watermark. Return path berkasnya."""
    if not detail.get('_unduh'):
        raise ValueError('Tidak ada URL unduhan pada metadata video.')
    dasar = nama_dasar or _nama_aman(
        f"{detail.get('username','')}_{detail.get('id','')}", 'tiktok')
    tujuan = os.path.join(folder, dasar + '.mp4')
    n = 1
    while os.path.exists(tujuan):
        tujuan = os.path.join(folder, f'{dasar}_{n}.mp4')
        n += 1
    return _unduh_ke(detail['_unduh'], tujuan)


def unduh_musik(detail: dict, folder: str, nama_dasar: str = '') -> str:
    """Simpan berkas musiknya. '' kalau tidak ada."""
    if not detail.get('musik_url'):
        return ''
    dasar = nama_dasar or _nama_aman(
        f"{detail.get('username','')}_{detail.get('id','')}", 'tiktok')
    try:
        return _unduh_ke(detail['musik_url'], os.path.join(folder, dasar + '.mp3'))
    except Exception:
        return ''


# ── Rekap metadata ──────────────────────────────────────────────────────────

def _waktu(stempel) -> str:
    try:
        from datetime import datetime
        return datetime.fromtimestamp(int(stempel)).strftime('%d-%m-%Y %H:%M')
    except Exception:
        return '-'


def tulis_metadata(detail: dict, path_txt: str) -> str:
    """Tulis seluruh metadata ke satu berkas .txt yang enak dibaca."""
    tag = ' '.join('#' + t for t in detail.get('hashtags') or []) or '-'
    produk = detail.get('produk') or []
    baris = [
        '=' * 66,
        'METADATA VIDEO TIKTOK',
        '=' * 66,
        '',
        f"URL          : {detail.get('url','-')}",
        f"Video ID     : {detail.get('id','-')}",
        f"Akun         : @{detail.get('username','-')} ({detail.get('nama_akun','-')})",
        f"Diunggah     : {_waktu(detail.get('dibuat'))}",
        f"Durasi       : {detail.get('durasi', 0)} detik",
        f"Region       : {detail.get('region','-') or '-'}",
        '',
        '--- CAPTION ---',
        detail.get('caption_penuh') or '-',
        '',
        f"Caption tanpa hashtag : {detail.get('caption') or '-'}",
        f"Hashtag               : {tag}",
        '',
        '--- MUSIK ---',
        f"Judul        : {detail.get('musik_judul') or '-'}",
        f"Artis        : {detail.get('musik_artis') or '-'}",
        f"Album        : {detail.get('musik_album') or '-'}",
        f"Durasi       : {detail.get('musik_durasi') or 0} detik",
        f"Suara asli   : {'ya' if detail.get('musik_asli') else 'tidak'}",
        f"Sound ID     : {detail.get('musik_id') or '-'}",
        '',
        '--- LOKASI ---',
        # Baris pertama sengaja gabungan nama + alamat: bentuk inilah yang
        # diterima kolom `location` di CSV upload, jadi bisa disalin apa adanya.
        f"Tag lokasi   : {detail.get('lokasi') or '-'}",
        f"Nama tempat  : {detail.get('lokasi_nama') or '-'}",
        f"Alamat       : {detail.get('lokasi_alamat') or '-'}",
        f"Kota         : {detail.get('lokasi_kota') or '-'}",
        '',
        '--- PRODUK / AFFILIATE ---',
    ]
    if produk:
        for i, p in enumerate(produk, 1):
            baris.append(f"{i}. {p.get('nama') or '-'}")
            if p.get('judul') and p['judul'] != p.get('nama'):
                baris.append(f"   Judul lengkap : {p['judul']}")
            baris.append(f"   Product ID    : {p.get('id') or '-'}")
            baris.append(f"   Link          : {p.get('link') or '-'}")
    else:
        baris.append('-')
    baris += [
        '',
        '--- STATISTIK ---',
        f"Views        : {detail.get('views')}",
        f"Likes        : {detail.get('likes')}",
        f"Komentar     : {detail.get('komentar')}",
        f"Share        : {detail.get('share')}",
        f"Disimpan     : {detail.get('simpan')}",
        '',
    ]
    os.makedirs(os.path.dirname(path_txt) or '.', exist_ok=True)
    with open(path_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(str(b) for b in baris))
    return path_txt
