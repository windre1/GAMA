"""
gama_fingerprint.py — Fingerprint unik per profil Chrome.

PRINSIP: berbeda antar profil, tapi TIDAK PERNAH saling bertentangan.

GAMA menjalankan Chrome ASLI di Windows. Karena itu user agent TETAP jujur
(Chrome + Windows, versi mengikuti Chrome yang benar-benar terpasang).
Mengaku Firefox/Opera/macOS justru membuat deteksi lebih mudah: navigator.vendor,
objek window.chrome, header Sec-CH-UA, WebGL renderer, dan navigator.platform
semuanya tetap melaporkan Chrome di Windows. Enam sumber yang saling
bertentangan adalah sinyal bot yang jauh lebih kuat daripada UA yang apa adanya.

Yang divariasikan adalah hal-hal yang MEMANG berbeda antar komputer nyata:
resolusi layar, bahasa, zona waktu, jumlah core, RAM, dan kartu grafis.

Urutan pengaruh sebenarnya untuk memisahkan akun:
    1. IP berbeda (proxy)      <- paling menentukan
    2. Profil Chrome terpisah  <- cookie/localStorage terisolasi
    3. Fingerprint browser     <- yang diatur di file ini
"""

import hashlib
import json as _json
import random
import re as _re

# ── Nilai-nilai yang wajar untuk PC Windows sungguhan ───────────────────────
# Diambil dari kombinasi yang umum dipakai, bukan angka acak — resolusi aneh
# seperti 1728x912 justru menonjol karena hampir tidak ada yang memakainya.
_RESOLUSI = [
    (1920, 1080), (1920, 1080), (1920, 1080),   # paling umum, sengaja diperberat
    (1366, 768), (1536, 864), (1600, 900),
    (1440, 900), (1280, 720), (2560, 1440),
]

_BAHASA = [
    'id-ID,id,en-US,en',
    'id-ID,id,en',
    'en-US,en,id',
]

# Zona waktu Indonesia — harus cocok dengan lokasi IP/proxy yang dipakai.
_ZONA = ['Asia/Jakarta', 'Asia/Makassar', 'Asia/Pontianak']

_CORE = [4, 6, 8, 8, 12, 16]        # jumlah core CPU
_RAM = [4, 8, 8, 8, 16]             # deviceMemory (GB)

# WebGL renderer — string ANGLE persis seperti yang dilaporkan Chrome di Windows.
_GPU = [
    'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)',
]


def generate_fingerprint(user_agent: str = '') -> dict:
    """Buat satu set fingerprint yang saling konsisten untuk satu profil."""
    lebar, tinggi = random.choice(_RESOLUSI)
    return {
        'user_agent': user_agent,          # diisi pemanggil — tetap Chrome asli
        'screen_w': lebar,
        'screen_h': tinggi,
        # Ukuran window sedikit lebih kecil dari layar, seperti jendela nyata
        'window_w': lebar - random.choice([0, 0, 60, 100, 160]),
        'window_h': tinggi - random.choice([60, 80, 100, 120]),
        'lang': random.choice(_BAHASA),
        'timezone': random.choice(_ZONA),
        'cores': random.choice(_CORE),
        'memory': random.choice(_RAM),
        'gpu': random.choice(_GPU),
        # Seed dipakai untuk noise canvas/WebGL supaya nilainya tetap SAMA
        # setiap kali profil dibuka — fingerprint yang berubah-ubah tiap
        # kunjungan justru mencurigakan.
        'seed': hashlib.sha256(random.randbytes(16)).hexdigest()[:16],
    }


def ringkas(fp: dict) -> str:
    """Satu baris ringkas untuk ditampilkan di kartu profil."""
    if not fp:
        return ''
    return (f"{fp.get('screen_w')}x{fp.get('screen_h')} · "
            f"{fp.get('cores')} core · {fp.get('memory')}GB · "
            f"{(fp.get('gpu') or '').split(',')[0].replace('ANGLE (', '') or '?'}")


def chrome_args(fp: dict, proxy: str = '') -> list:
    """Flag Chrome dari fingerprint — hanya yang memang punya flag sendiri.

    Sisanya (navigator.*, screen, WebGL, canvas) TIDAK punya flag Chrome dan
    disuntikkan lewat CDP oleh gama_identitas.py.

    Catatan penting: sampai Chrome 136 hal ini bisa dikerjakan ekstensi lewat
    --load-extension. Sejak Chrome 137 flag itu dinonaktifkan, dan di Chrome
    151 sudah dihapus sepenuhnya (--disable-features juga tidak
    mengembalikannya). Karena itu jalur ekstensi tidak dipakai lagi.
    """
    args = []
    if fp.get('window_w') and fp.get('window_h'):
        args.append(f"--window-size={fp['window_w']},{fp['window_h']}")
    if fp.get('lang'):
        args.append(f"--lang={fp['lang'].split(',')[0]}")
    if proxy:
        args.append(f"--proxy-server={proxy}")
    return args


def urai_proxy(nilai: str) -> dict:
    """'user:pass@host:port' / 'socks5://host:port' -> bagian-bagiannya.

    Mengembalikan {} kalau formatnya tidak sah, supaya teks sembarang tidak
    ikut tersimpan ke profil dan baru ketahuan rusak saat Chrome dibuka.
    """
    p = (nilai or '').strip()
    if not p:
        return {}
    skema = 'http'
    m = _re.match(r'^(https?|socks[45]):/*(.+)$', p, _re.I)
    if m:
        skema, p = m.group(1).lower(), m.group(2)
    user = sandi = ''
    if '@' in p:
        kredensial, p = p.rsplit('@', 1)
        user, _, sandi = kredensial.partition(':')
    host, _, port = p.partition(':')
    if not _re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?', host or ''):
        return {}
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        return {}
    return {'scheme': skema, 'host': host, 'port': int(port),
            'user': user, 'pass': sandi}


def proxy_untuk_flag(nilai: str) -> str:
    """Bentuk yang boleh masuk --proxy-server: kredensial DIBUANG.

    Chrome mengabaikan user:pass di baris perintah dan malah memunculkan
    dialog login. Kredensialnya dijawab lewat CDP (Fetch.continueWithAuth).
    """
    px = urai_proxy(nilai)
    return f"{px['scheme']}://{px['host']}:{px['port']}" if px else ''


def normalisasi_proxy(nilai: str) -> str:
    """Bentuk rapi untuk disimpan/ditampilkan. '' kalau tidak sah."""
    px = urai_proxy(nilai)
    if not px:
        return ''
    kredensial = f"{px['user']}:{px['pass']}@" if px['user'] else ''
    return f"{px['scheme']}://{kredensial}{px['host']}:{px['port']}"


def proxy_pakai_kredensial(nilai: str) -> bool:
    return bool(urai_proxy(nilai).get('user'))


def skrip_penyamaran(fp: dict) -> str:
    """JS yang disuntikkan sebelum tiap halaman jalan (lewat CDP).

    Hanya menimpa nilai yang tidak punya flag Chrome. Zona waktu, bahasa, dan
    ukuran window ditangani di tempat lain karena Chrome sudah menyediakannya.
    """
    return """
(() => {
  const paksa = (obj, prop, nilai) => {
    try { Object.defineProperty(obj, prop, { get: () => nilai, configurable: true }); } catch (e) {}
  };

  paksa(navigator, 'hardwareConcurrency', %(cores)d);
  paksa(navigator, 'deviceMemory', %(memory)d);
  paksa(screen, 'width', %(w)d);   paksa(screen, 'height', %(h)d);
  paksa(screen, 'availWidth', %(w)d); paksa(screen, 'availHeight', %(h)d - 40);

  // WebGL: HANYA vendor/renderer yang diganti. Parameter lain dibiarkan asli
  // supaya tidak muncul kombinasi yang mustahil ada di komputer nyata.
  const GPU = %(gpu)s;
  const patchGL = (proto) => {
    if (!proto) return;
    const asli = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return GPU.replace('ANGLE (', '').split(',')[0];
      if (p === 37446) return GPU;
      return asli.apply(this, arguments);
    };
  };
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // Canvas: pergeseran SANGAT tipis dan TETAP (diturunkan dari seed profil).
  // Noise acak tiap panggilan justru menandai bot, karena komputer sungguhan
  // selalu menghasilkan canvas yang sama persis.
  const SEED = %(seed)s;
  let n = 0;
  for (let i = 0; i < SEED.length; i++) n = (n * 31 + SEED.charCodeAt(i)) >>> 0;
  const geser = (n %% 3) - 1;
  if (geser !== 0) {
    const asliData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function () {
      const d = asliData.apply(this, arguments);
      for (let i = 0; i < d.data.length; i += 4 * 977) {
        d.data[i] = Math.max(0, Math.min(255, d.data[i] + geser));
      }
      return d;
    };
  }
})();
""" % {
        'cores': int(fp.get('cores') or 8),
        'memory': int(fp.get('memory') or 8),
        'w': int(fp.get('screen_w') or 1920),
        'h': int(fp.get('screen_h') or 1080),
        'gpu': _json.dumps(fp.get('gpu') or ''),
        'seed': _json.dumps(fp.get('seed') or '0'),
    }


