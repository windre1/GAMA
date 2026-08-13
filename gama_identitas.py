"""
gama_identitas.py — Penerap fingerprint & login proxy lewat CDP.

KENAPA LEWAT CDP, BUKAN EKSTENSI:
Sampai Chrome 136, nilai seperti navigator.hardwareConcurrency dan WebGL
renderer bisa ditimpa oleh ekstensi yang dimuat dengan --load-extension.
Chrome 137 menonaktifkan flag itu dan Chrome 151 menghapusnya sepenuhnya —
sudah diuji, --disable-features pun tidak mengembalikannya. Satu-satunya jalur
yang masih bekerja adalah Chrome DevTools Protocol.

CARA KERJANYA:
Chrome dijalankan dengan --remote-debugging-port pada port acak yang hanya
mendengar di 127.0.0.1. Satu thread latar menempel ke sana, lalu untuk SETIAP
tab yang lahir memasang:
  - Page.addScriptToEvaluateOnNewDocument  -> core, RAM, layar, WebGL, canvas
  - Emulation.setTimezoneOverride          -> zona waktu
  - Emulation.setUserAgentOverride         -> Accept-Language
  - Fetch (hanya bila proxy pakai user:pass) -> menjawab tantangan login proxy

Thread ini berhenti sendiri saat Chrome ditutup.

BATASNYA, supaya jelas: kalau GAMA ditutup lebih dulu sementara Chrome masih
terbuka, tab yang SUDAH ada tetap tersamar, tapi tab BARU setelah itu tidak.
"""

import json
import socket
import threading
import time
import urllib.request

import gama_fingerprint as _fp

# Berapa lama menunggu Chrome membuka port debug-nya. Chrome yang baru pertama
# kali dijalankan (profil kosong) butuh lebih lama daripada yang sudah hangat.
_BATAS_TUNGGU = 30


def port_kosong() -> int:
    """Ambil port yang benar-benar bebas, bukan menebak lalu berharap."""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _url_debug(port: int):
    batas = time.time() + _BATAS_TUNGGU
    while time.time() < batas:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=2) as r:
                return json.load(r)['webSocketDebuggerUrl']
        except Exception:
            time.sleep(0.4)
    return ''


class _Sesi:
    """Pembungkus tipis websocket CDP. Sengaja tidak memakai DrissionPage:
    di sini yang dibutuhkan hanya menempel dan mendengarkan, tanpa mengambil
    alih tab yang sedang dipakai user."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._kunci = threading.Lock()

    def kirim(self, method, params=None, sid=None):
        with self._kunci:
            self._id += 1
            pesan = {'id': self._id, 'method': method, 'params': params or {}}
            if sid:
                pesan['sessionId'] = sid
            self.ws.send(json.dumps(pesan))
            return self._id


def tersedia() -> bool:
    """Apakah penjaga bisa jalan sama sekali di PC ini."""
    try:
        import websocket                                  # noqa: F401
        return True
    except ImportError:
        return False


def _pasang_ke_tab(sesi, sid, fp, px, skrip):
    """Pasang semua penyamaran pada satu tab yang baru lahir."""
    sesi.kirim('Page.enable', {}, sid)          # wajib, kalau tidak addScript diabaikan
    sesi.kirim('Page.addScriptToEvaluateOnNewDocument', {'source': skrip}, sid)
    # addScript hanya berlaku untuk dokumen BERIKUTNYA. Kalau tab ini sudah
    # terlanjur memuat halaman (mis. tab yang sudah terbuka saat penjaga baru
    # menyambung), dokumen yang sedang tampil harus ditambal langsung.
    sesi.kirim('Runtime.evaluate', {'expression': skrip}, sid)

    if fp.get('timezone'):
        sesi.kirim('Emulation.setTimezoneOverride', {'timezoneId': fp['timezone']}, sid)
    if fp.get('lang'):
        # userAgent tetap UA Chrome asli — hanya Accept-Language yang dibedakan,
        # supaya Client Hints (Sec-CH-UA) tidak jadi bertentangan dengan UA.
        sesi.kirim('Emulation.setUserAgentOverride', {
            'userAgent': fp.get('user_agent') or '',
            'acceptLanguage': fp['lang'],
        }, sid)

    if px.get('user'):
        # Hanya dinyalakan kalau proxy memang minta login. Fetch menahan SEMUA
        # request, jadi menyalakannya tanpa perlu hanya memperlambat browsing.
        sesi.kirim('Fetch.enable', {'handleAuthRequests': True,
                                    'patterns': [{'urlPattern': '*'}]}, sid)


def _penjaga(port, fp, proxy, url_awal='', saat_gagal=None):
    """Thread latar: menempel ke Chrome dan melayani tab sampai Chrome tutup.

    url_awal dibuka oleh penjaga, BUKAN lewat baris perintah. Alasannya:
    Page.addScriptToEvaluateOnNewDocument hanya berlaku untuk dokumen
    berikutnya. Kalau URL tujuan ikut di baris perintah, halaman itu sudah
    selesai dimuat sebelum penjaga sempat memasang apa pun — dan situs sudah
    membaca navigator yang asli. Maka Chrome dibuka di about:blank dulu.
    """
    try:
        import websocket
    except ImportError:
        if saat_gagal:
            saat_gagal('Modul websocket-client tidak ada — fingerprint tidak dipasang.')
        return

    wsurl = _url_debug(port)
    if not wsurl:
        if saat_gagal:
            saat_gagal('Chrome tidak membuka port debug — fingerprint tidak dipasang.')
        return

    px = _fp.urai_proxy(proxy)
    skrip = _fp.skrip_penyamaran(fp)

    try:
        ws = websocket.create_connection(wsurl, suppress_origin=True, timeout=60)
    except Exception:
        if saat_gagal:
            saat_gagal('Gagal menempel ke Chrome — fingerprint tidak dipasang.')
        return

    sesi = _Sesi(ws)
    # waitForDebuggerOnStart menahan tab sebelum skrip halaman pertama jalan.
    # Tanpa ini ada balapan: halaman bisa keburu membaca navigator yang asli.
    sesi.kirim('Target.setAutoAttach', {'autoAttach': True,
                                        'waitForDebuggerOnStart': True,
                                        'flatten': True})
    # setAutoAttach saja TIDAK cukup: sudah diuji, tab yang dibuka sendiri oleh
    # user (Ctrl+T) tidak ikut tertangkap dan lolos tanpa penyamaran sama
    # sekali. setDiscoverTargets memberi tahu setiap tab baru, lalu kita
    # menempel manual.
    sesi.kirim('Target.setDiscoverTargets', {'discover': True})

    sudah = set()

    while True:
        try:
            pesan = json.loads(ws.recv())
        except Exception:
            break                                    # Chrome ditutup — selesai

        metode = pesan.get('method')
        sid = pesan.get('sessionId')

        if metode == 'Target.targetCreated':
            info = pesan['params']['targetInfo']
            if info.get('type') == 'page' and info['targetId'] not in sudah:
                sudah.add(info['targetId'])
                sesi.kirim('Target.attachToTarget',
                           {'targetId': info['targetId'], 'flatten': True})

        elif metode == 'Target.targetDestroyed':
            sudah.discard(pesan['params'].get('targetId'))

        elif metode == 'Target.attachedToTarget':
            anak = pesan['params']['sessionId']
            sudah.add(pesan['params']['targetInfo'].get('targetId'))
            if pesan['params']['targetInfo'].get('type') == 'page':
                try:
                    _pasang_ke_tab(sesi, anak, fp, px, skrip)
                    if url_awal:
                        sesi.kirim('Page.navigate', {'url': url_awal}, anak)
                        url_awal = ''            # hanya tab pertama
                except Exception:
                    pass
            # Selalu dilepas, termasuk untuk target non-halaman — kalau tidak,
            # worker/iframe menggantung dan sebagian situs gagal dimuat.
            sesi.kirim('Runtime.runIfWaitingForDebugger', {}, anak)

        elif metode == 'Fetch.authRequired':
            sesi.kirim('Fetch.continueWithAuth', {
                'requestId': pesan['params']['requestId'],
                'authChallengeResponse': {
                    'response': 'ProvideCredentials',
                    'username': px.get('user', ''),
                    'password': px.get('pass', ''),
                },
            }, sid)

        elif metode == 'Fetch.requestPaused':
            # Fetch menahan tiap request; harus dilepas atau halaman diam saja.
            sesi.kirim('Fetch.continueRequest',
                       {'requestId': pesan['params']['requestId']}, sid)

    try:
        ws.close()
    except Exception:
        pass


def argumen_chrome(fp: dict, proxy: str = ''):
    """Flag Chrome + port debug yang dipakai penjaga.

    Return (daftar_argumen, port).
    """
    port = port_kosong()
    args = _fp.chrome_args(fp, _fp.proxy_untuk_flag(proxy))
    args.append(f'--remote-debugging-port={port}')
    # Dibatasi ke localhost supaya port debug tidak bisa dihubungi dari luar PC.
    args.append('--remote-debugging-address=127.0.0.1')
    return args, port


def mulai_penjaga(port: int, fp: dict, proxy: str = '', url_awal: str = '',
                  saat_gagal=None):
    """Jalankan penjaga di thread latar (daemon, ikut mati bersama GAMA)."""
    t = threading.Thread(target=_penjaga,
                         args=(port, fp, proxy, url_awal, saat_gagal),
                         daemon=True, name=f'identitas-{port}')
    t.start()
    return t
