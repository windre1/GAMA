"""gama_license.py — Verifikasi lisensi GAMA per-fitur.

Hanya mendukung format GAMA2- (universal):
  - 1 lisensi = 1 device. TITIK. Device kedua DITOLAK, bukan menggusur.
  - Pindah device MANDIRI PENUH: user menekan "Pindahkan ke Device Ini",
    tanpa jeda dan tanpa perlu admin. Yang menjaga agar ini tidak jadi celah
    berbagi lisensi bukan lagi jeda waktu, melainkan pengecekan tiap 15 menit
    (_REVOCATION_CHECK_MINUTES) — device yang tergusur cepat berhenti jalan.
  - Menggusur TIDAK PERNAH otomatis: hanya terjadi kalau user menekan
    tombolnya. Pengecekan rutin tidak mengirim `transfer`.
  - Admin bisa revoke kapan saja via Vercel API
  - Aktivasi pertama wajib online; setelah itu offline tetap jalan lewat cache

Tab 'profiles', 'tutorial', 'lisensi' SELALU aktif tanpa key.
Hanya tab yang di-list di FEATURES yang dikunci.
"""

import base64
import hashlib
import json
import os
import platform
import socket
import struct
import sys
import uuid
from datetime import date, datetime

# Frozen (PyInstaller): folder GAMA.exe. Dev: folder file ini.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
           else os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
LICENSE_FILE          = os.path.join(DATA_DIR, 'license.json')
REVOCATION_CACHE_FILE = os.path.join(DATA_DIR, 'revocation_cache.json')

# ---------------------------------------------------------------------------
# PUBLIC KEY Ed25519 (32 byte hex). PRIVATE KEY hanya di tools/keygen_gama.py.
# ---------------------------------------------------------------------------
_PUBLIC_KEY_HEX = "5bd651ffdc985b6d69995180af5b0064aee229a2c3464ab1a6d83d015157669e"

_KEY_PREFIX    = "GAMA2-"
_UNIVERSAL_ID  = "universal"

# Vercel API
_REVOCATION_API_URL    = "https://gama-license-api.vercel.app/api/check"
_ACTIVATE_API_URL      = "https://gama-license-api.vercel.app/api/activate"
# Seberapa sering status lisensi dicek ulang ke server saat ONLINE.
#
# Dulu 4 jam. Sejak pemindahan device dibuat mandiri penuh (tanpa jeda dan
# tanpa admin), angka ini yang menjadi rem anti berbagi lisensi: device yang
# tergusur harus cepat berhenti, kalau tidak dua orang bisa bergantian pindah
# dan keduanya tetap jalan selama jendela cache.
#
# Pemakaian offline TIDAK terganggu: kalau server tidak bisa dihubungi, kode
# di bawah jatuh ke 'offline_cached' berapa pun umur cache-nya.
_REVOCATION_CHECK_MINUTES = 15

# ---------------------------------------------------------------------------
# FEATURES — tab yang bisa dikunci/dibuka lewat lisensi.
# ---------------------------------------------------------------------------
FEATURES = {
    'riset':       0,
    'downloader':  1,
    'repurpose':   2,
    'clipper':     3,
    'generate':    4,
    'uploader':    5,
    'prospek':     6,
    'fb-group':    7,
    'shovebot':    8,
    'chatbot':     9,
    'metadata':    10,
}
FEATURE_LABELS = {
    'riset':      'Riset',
    'downloader': 'Downloader',
    'repurpose':  'Repurpose',
    'clipper':    'Clipper',
    'generate':   'Generate',
    'uploader':   'Uploader',
    'prospek':    'Prospek',
    'fb-group':   'FB Grup',
    'shovebot':   'ShoveBot',
    'chatbot':    'Chat Grow',
    'metadata':   'Metadata Cleaner',
}


def features_to_mask(feature_names) -> int:
    mask = 0
    for name in feature_names or []:
        bit = FEATURES.get(name)
        if bit is not None:
            mask |= (1 << bit)
    return mask


def mask_to_features(mask: int) -> list:
    return [name for name, bit in FEATURES.items() if mask & (1 << bit)]


# ---------------------------------------------------------------------------
# Device hash — identifier unik device ini, dikirim ke server saat cek.
# Dibuat dari kombinasi hardware identifier, di-hash supaya aman.
# ---------------------------------------------------------------------------

_DEVICE_HASH_CACHE = None


def _get_device_hash() -> str:
    """Buat identifier unik device ini (24-char hex, di-hash dari hardware ID)."""
    global _DEVICE_HASH_CACHE
    if _DEVICE_HASH_CACHE:
        return _DEVICE_HASH_CACHE

    parts = []
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        if guid:
            parts.append(f"machineguid:{guid}")
    except Exception:
        pass

    try:
        parts.append(f"{platform.node()}:{hex(uuid.getnode())}")
    except Exception:
        pass

    try:
        parts.append(socket.gethostname())
    except Exception:
        pass

    raw = "|".join(parts) or "gama-unknown-device"
    _DEVICE_HASH_CACHE = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return _DEVICE_HASH_CACHE


# ---------------------------------------------------------------------------
# Key hash — untuk revocation lookup (tidak mengekspos key asli ke server)
# ---------------------------------------------------------------------------

def _key_hash(license_key: str) -> str:
    return hashlib.sha256(license_key.strip().encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Revocation cache (lokal, 1 hari)
# ---------------------------------------------------------------------------

def _load_revocation_cache() -> dict:
    try:
        with open(REVOCATION_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f) or {"entries": {}}
    except Exception:
        return {"entries": {}}


def _save_revocation_cache(cache: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = REVOCATION_CACHE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REVOCATION_CACHE_FILE)


def _call_activate(key_hash: str, device_hash: str, transfer: bool = False) -> dict:
    """Panggil /api/activate. Return dict apa adanya dari server.

    `transfer=True` HANYA dikirim kalau user menekan tombol pindah device
    sendiri. Pengecekan rutin tidak pernah mengirimnya, sehingga tidak bisa
    menggusur lisensi milik device lain diam-diam — itu celah yang membuat
    1 lisensi dulu bisa dipakai banyak device.
    """
    import urllib.error
    import urllib.request

    body = {'key_hash': key_hash, 'device_hash': device_hash}
    if transfer:
        body['transfer'] = True
    payload = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        _ACTIVATE_API_URL, data=payload, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'GAMA-App/2.0'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {'success': False, 'error': str(e)}
    except Exception as e:
        return {'success': False, 'error': f'Tidak bisa terhubung ke server: {e}'}


def _activate_device(key_hash: str, device_hash: str) -> bool:
    """Daftarkan device ini sebagai device aktif (hanya untuk key yang belum
    punya device — tidak pernah menggusur)."""
    return bool(_call_activate(key_hash, device_hash).get('success', False))


def transfer_to_this_device(license_key: str) -> dict:
    """Pindahkan lisensi ke device ini — DIPANGGIL SAAT USER MENEKAN TOMBOL.

    Return: {'success': bool, 'message': str}
    """
    h = _key_hash(license_key)
    data = _call_activate(h, _get_device_hash(), transfer=True)

    if data.get('success'):
        # Cache lama harus dibuang, kalau tidak status lama masih terpakai
        # sampai beberapa jam ke depan.
        try:
            cache = _load_revocation_cache()
            cache.get('entries', {}).pop(h, None)
            _save_revocation_cache(cache)
        except Exception:
            pass
        return {'success': True,
                'message': data.get('message', 'Lisensi berhasil dipindahkan ke device ini.')}

    sisa = data.get('retry_after_hours')
    if sisa:
        return {'success': False,
                'message': (f"Lisensi ini baru saja dipindahkan. Tunggu {sisa} jam lagi "
                            f"sebelum bisa dipindahkan ke device lain.")}
    return {'success': False,
            'message': data.get('error') or 'Gagal memindahkan lisensi.'}


def _get_revocation_status(license_key: str) -> dict:
    """Cek revocation status key ke server.

    Aturan:
    - 1 lisensi = 1 device. Device kedua DITOLAK (bukan menggusur yang lama).
    - Pindah device mandiri lewat tombol di UI; admin tidak dilibatkan.
    - Aktivasi PERTAMA wajib online → tolak jika offline.
    - Cache hasil cek disimpan _REVOCATION_CHECK_MINUTES menit.
    - Jika cache expired tapi server down → pakai cache lama (offline_cached).
    """
    h           = _key_hash(license_key)
    device_hash = _get_device_hash()
    cache       = _load_revocation_cache()
    entry       = cache.get("entries", {}).get(h)
    now         = datetime.now()

    # Cek apakah cache masih berlaku (< 1 hari) dan device sama
    if entry:
        try:
            checked_at = datetime.fromisoformat(entry['checked_at'])
            cache_valid = (now - checked_at).total_seconds() < _REVOCATION_CHECK_MINUTES * 60
            same_device = entry.get('device_hash', '') == device_hash
            if cache_valid and same_device:
                return {'revoked': entry.get('revoked', True), 'source': 'cache'}
        except Exception:
            pass

    # Cek online
    try:
        import urllib.request
        url = f"{_REVOCATION_API_URL}?h={h}&d={device_hash}"
        req = urllib.request.Request(url, headers={'User-Agent': 'GAMA-App/2.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        revoked       = bool(data.get('revoked', False))
        device_kicked = bool(data.get('device_kicked', False))
        reason        = data.get('reason', '')

        if revoked:
            return {'revoked': True, 'source': 'online'}

        if device_kicked:
            # 1 LISENSI = 1 DEVICE.
            #
            # Versi lama langsung memanggil _activate_device() walau server
            # bilang key ini sedang aktif di device LAIN ('wrong_device') —
            # artinya device mana pun bisa merebut lisensi kapan saja, diam-diam.
            #
            # Sekarang: aktivasi otomatis hanya untuk key yang BELUM punya
            # device. Kalau sudah dipegang device lain, tolak dan biarkan UI
            # menawarkan tombol "Pindahkan ke Device Ini" — perpindahan harus
            # keputusan sadar user, bukan efek samping pengecekan rutin.
            if reason == 'wrong_device':
                return {'revoked': True, 'source': 'device_taken'}

            ok = _activate_device(h, device_hash)
            if not ok:
                return {'revoked': True, 'source': 'activate_failed'}

        # Simpan cache
        cache.setdefault("entries", {})[h] = {
            'revoked':     False,
            'checked_at':  now.isoformat(timespec='seconds'),
            'device_hash': device_hash,
        }
        _save_revocation_cache(cache)
        return {
            'revoked': False,
            'source': 'online',
        }

    except Exception:
        if entry:
            # Server down sementara — pakai cache lama jika device sama
            if entry.get('device_hash', '') == device_hash:
                return {'revoked': entry.get('revoked', True), 'source': 'offline_cached'}
            # Device berbeda, server tidak bisa dicek → tolak
            return {'revoked': True, 'source': 'offline_device_mismatch'}
        # Belum pernah diaktivasi → wajib online
        return {'revoked': True, 'source': 'offline_new'}


# ---------------------------------------------------------------------------
# Decode key
# ---------------------------------------------------------------------------

def _decode_key(license_key: str) -> bytes:
    s = (license_key or "").strip()
    if s.upper().startswith(_KEY_PREFIX.upper()):
        s = s[len(_KEY_PREFIX):]
    s = "".join(s.split())
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


# ---------------------------------------------------------------------------
# Verifikasi key GAMA2-
# ---------------------------------------------------------------------------

def verify_license_key(license_key: str) -> dict:
    """Verifikasi license key GAMA2-."""
    s = (license_key or "").strip()
    if not s:
        return {"valid": False, "features": [], "expires_at": None, "message": "Key kosong."}

    if not s.upper().startswith(_KEY_PREFIX.upper()):
        return {"valid": False, "features": [], "expires_at": None,
                "message": f"Format key tidak valid. Key harus diawali dengan '{_KEY_PREFIX}'."}

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        return {"valid": False, "features": [], "expires_at": None,
                "message": "Komponen verifikasi (cryptography) tidak ter-install."}

    try:
        blob = _decode_key(license_key)

        # Dua format didukung:
        #
        #   v2 (70 byte) : expiry(4) + features(2) + sig(64)
        #       Format lama. Isinya HANYA masa berlaku + fitur, sehingga dua
        #       lisensi dengan fitur & tanggal sama menghasilkan key yang
        #       IDENTIK — tanda tangan Ed25519 bersifat deterministik.
        #       Tetap diterima supaya lisensi yang sudah terbit tidak mati.
        #
        #   v3 (74 byte) : expiry(4) + features(2) + serial(4) + sig(64)
        #       `serial` diturunkan dari nama + nomor WA pemilik, jadi tiap
        #       orang mendapat key yang berbeda walau fitur & tanggalnya sama.
        if len(blob) == 4 + 2 + 64:
            expiry_int   = struct.unpack(">I", blob[:4])[0]
            features_int = struct.unpack(">H", blob[4:6])[0]
            signature    = blob[6:70]
            message      = f"{_UNIVERSAL_ID}:{expiry_int}:{features_int}".encode("utf-8")
        elif len(blob) == 4 + 2 + 4 + 64:
            expiry_int   = struct.unpack(">I", blob[:4])[0]
            features_int = struct.unpack(">H", blob[4:6])[0]
            serial_int   = struct.unpack(">I", blob[6:10])[0]
            signature    = blob[10:74]
            message      = f"{_UNIVERSAL_ID}:{expiry_int}:{features_int}:{serial_int}".encode("utf-8")
        else:
            raise ValueError("panjang key tidak valid")

        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(_PUBLIC_KEY_HEX))
        pub.verify(signature, message)
    except Exception:
        return {"valid": False, "features": [], "expires_at": None,
                "message": "License key tidak valid."}

    try:
        exp = datetime.strptime(str(expiry_int), "%Y%m%d").date()
    except Exception:
        return {"valid": False, "features": [], "expires_at": None,
                "message": "Format tanggal pada key tidak valid."}

    expires_at    = exp.isoformat()
    feature_names = mask_to_features(features_int)
    today         = date.today()

    if exp < today:
        return {"valid": False, "features": feature_names, "expires_at": expires_at,
                "message": f"Lisensi kedaluwarsa pada {expires_at}. Hubungi admin untuk perpanjangan."}

    rev = _get_revocation_status(license_key)
    if rev.get('revoked'):
        src = rev.get('source', '')
        if src == 'offline_new':
            msg = ("Aktivasi pertama memerlukan koneksi internet. "
                   "Pastikan terhubung ke internet lalu coba lagi.")
        elif src == 'activate_failed':
            msg = ("Tidak bisa mendaftarkan device — server tidak bisa diakses. "
                   "Pastikan koneksi internet lalu coba lagi.")
        elif src == 'device_taken':
            msg = ("Lisensi ini sedang aktif di device lain. Satu lisensi hanya "
                   "untuk satu device. Kalau ini PC baru Anda, klik "
                   "\"Pindahkan ke Device Ini\" — bisa kapan saja, tanpa "
                   "menunggu dan tanpa perlu menghubungi admin. Device lama "
                   "berhenti bisa dipakai dalam beberapa menit.")
        elif src == 'offline_device_mismatch':
            msg = ("Key ini terdaftar di device lain dan server tidak bisa "
                   "dihubungi. Pastikan koneksi internet lalu coba lagi.")
        else:
            msg = "Lisensi ini telah dicabut oleh admin. Hubungi admin."
        # can_transfer dipakai UI untuk menampilkan tombol "Pindahkan ke
        # Device Ini". Hanya masuk akal saat lisensinya sah tapi sedang
        # dipegang device lain — bukan saat key dicabut atau offline.
        return {"valid": False, "features": feature_names, "expires_at": expires_at,
                "message": msg, "can_transfer": (src == 'device_taken')}

    src = rev.get('source', '')
    if exp.year >= 2099:
        msg = "Lisensi LIFETIME aktif."
    else:
        days_left = (exp - today).days
        msg = f"Lisensi aktif hingga {expires_at} ({days_left} hari lagi)."

    if src == 'offline_cached':
        msg += " [Mode offline — cache terakhir dipakai]"

    return {"valid": True, "features": feature_names, "expires_at": expires_at,
            "message": msg, "revocation_source": src}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load_keys() -> list:
    try:
        with open(LICENSE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f) or {}
            keys = data.get('keys', [])
            return [k for k in keys if isinstance(k, str) and k.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_keys(keys: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = LICENSE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'keys': keys}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LICENSE_FILE)


def add_key(license_key: str) -> dict:
    result = verify_license_key(license_key)
    if result['valid']:
        keys = load_keys()
        key  = license_key.strip()
        if key not in keys:
            keys.append(key)
            save_keys(keys)
    return result


def get_feature_status() -> dict:
    status = {name: {'unlocked': False, 'expires_at': None, 'label': FEATURE_LABELS[name]}
              for name in FEATURES}
    for key in load_keys():
        result = verify_license_key(key)
        if not result['valid']:
            continue
        for name in result['features']:
            if name not in status:
                continue
            cur        = status[name]
            new_expiry = result['expires_at']
            if not cur['unlocked'] or (new_expiry and (
                    not cur['expires_at'] or new_expiry > cur['expires_at'])):
                cur['unlocked']   = True
                cur['expires_at'] = new_expiry
    return status
