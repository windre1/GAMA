"""Verifikasi lisensi OMNIVORA — KEYGEN OFFLINE (Ed25519).

Alur:
  1. User membuka dialog Lisensi → menyalin Machine ID → dikirim ke admin.
  2. Admin menjalankan tools/keygen.py (berisi PRIVATE KEY) untuk membuat license key
     dengan masa aktif tertentu, terikat ke Machine ID user.
  3. User memasukkan license key → aplikasi memverifikasi tanda tangan SECARA OFFLINE
     memakai PUBLIC KEY di bawah (tanpa internet). Key tidak bisa dipalsukan/diubah
     tanggal-nya karena ditandatangani private key admin.

Format key:
  LEGACY : base64url( expiry(4 byte, YYYYMMDD) + signature(64 byte) )
           pesan = f"{machine_id}:{expiry_yyyymmdd}" → plan enterprise/lifetime.
  V2     : base64url( expiry(4) + plan_code(1) + signature(64) )
           pesan = f"{machine_id}:{expiry_yyyymmdd}:{plan_code}" → mendukung
           plan "global" (semua region). Kedua format diterima verifikator.
Semua diberi prefix "OMNIVORA-".
"""
import base64
import hashlib
import platform
import socket
import struct
import uuid
from datetime import date, datetime

from omnivora.environment import app_root, user_data_root

# ---------------------------------------------------------------------------
# PUBLIC KEY Ed25519 (32 byte hex). PRIVATE KEY ada di admin (tools/keygen.py)
# dan TIDAK boleh ikut didistribusikan ke user.
# ---------------------------------------------------------------------------
_PUBLIC_KEY_HEX = "8b89ae28fe2de14631646842d50de71d22228c88512fc5434fe47d15355c5463"

_CACHE_FILES = (
    app_root() / "configs" / "license_cache.json",
    user_data_root() / "configs" / "license_cache.json",
)
_KEY_PREFIX = "OMNIVORA-"

# Kode plan pada format key V2 (byte ke-5). 0 dicadangkan = enterprise.
_PLAN_CODES = {0: "enterprise", 1: "enterprise", 2: "global"}


def _machine_id_candidates() -> list:
    """Semua kandidat Machine ID perangkat ini, paling stabil duluan.

    Kandidat utama = MachineGuid Windows (stabil per instalasi OS, tidak
    berubah saat ganti adapter/VPN). Kandidat legacy (hostname+MAC) tetap
    dicoba saat verifikasi supaya license key lama tetap valid — uuid.getnode()
    bisa berubah kalau adapter jaringan berubah, bahkan acak bila MAC tak
    terbaca, jadi tidak dipakai lagi sebagai ID yang ditampilkan ke user.
    """
    ids = []

    def add(raw):
        digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
        if digest not in ids:
            ids.append(digest)

    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        if guid:
            add(f"machineguid:{guid}")
    except Exception:
        pass

    try:
        add(f"{platform.node()}:{hex(uuid.getnode())}")
    except Exception:
        pass

    try:
        add(socket.gethostname())
    except Exception:
        pass

    return ids or [hashlib.sha256(b"omnivora-unknown-device").hexdigest()[:32]]


def get_machine_id() -> str:
    """ID unik perangkat (dikirim user ke admin untuk pembuatan license key)."""
    return _machine_id_candidates()[0]


def _decode_key(license_key: str) -> bytes:
    s = (license_key or "").strip()
    if s.upper().startswith(_KEY_PREFIX):
        s = s[len(_KEY_PREFIX):]
    s = "".join(s.split())  # buang spasi/enter yang mungkin terbawa saat copy
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def _signed_message(machine_id: str, expiry_yyyymmdd: int) -> bytes:
    return f"{machine_id}:{expiry_yyyymmdd}".encode("utf-8")


def verify_license(license_key: str) -> dict:
    """Verifikasi lisensi offline. Return dict:
    { valid, plan, expires_at, message, source }."""
    if not license_key or license_key.strip() in ("", "BELUM AKTIF"):
        return {"valid": False, "plan": "", "expires_at": None,
                "message": "Masukkan license key. Kirim Machine ID Anda ke admin untuk mendapatkannya.",
                "source": "local"}

    plan_name = "enterprise"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        # Tanpa modul cryptography verifikasi mustahil — jangan menyesatkan user
        # dengan pesan "key tidak valid" padahal masalahnya di instalasi aplikasi.
        return {"valid": False, "plan": "", "expires_at": None,
                "message": "Komponen verifikasi lisensi (cryptography) tidak ter-install. "
                           "Install ulang aplikasi atau jalankan: pip install cryptography.",
                "source": "offline"}

    try:
        blob = _decode_key(license_key)
        if len(blob) < 4 + 64:
            raise ValueError("panjang key tidak valid")
        expiry_int = struct.unpack(">I", blob[:4])[0]  # YYYYMMDD

        if len(blob) >= 4 + 1 + 64:
            # Format V2: expiry(4) + plan_code(1) + signature(64)
            plan_code = blob[4]
            signature = blob[5:5 + 64]
            messages = [f"{mid}:{expiry_int}:{plan_code}".encode("utf-8")
                        for mid in _machine_id_candidates()]
            plan_name = _PLAN_CODES.get(plan_code, "enterprise")
        else:
            # Format legacy: expiry(4) + signature(64)
            signature = blob[4:4 + 64]
            messages = [_signed_message(mid, expiry_int)
                        for mid in _machine_id_candidates()]

        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(_PUBLIC_KEY_HEX))
        verified = False
        for message in messages:
            try:
                pub.verify(signature, message)
                verified = True
                break
            except Exception:
                continue
        if not verified:
            raise ValueError("tanda tangan tidak cocok")
    except Exception:
        return {"valid": False, "plan": "", "expires_at": None,
                "message": "License key tidak valid untuk perangkat ini.",
                "source": "offline"}

    # Tanda tangan sah → cek masa aktif
    try:
        exp = datetime.strptime(str(expiry_int), "%Y%m%d").date()
    except Exception:
        return {"valid": False, "plan": "", "expires_at": None,
                "message": "Format tanggal pada lisensi tidak valid.", "source": "offline"}

    expires_at = exp.isoformat()
    today = date.today()
    if exp < today:
        return {"valid": False, "plan": "expired", "expires_at": expires_at,
                "message": f"Lisensi kedaluwarsa pada {expires_at}. Minta perpanjangan ke admin.",
                "source": "offline"}

    if exp.year >= 2099:
        return {"valid": True, "plan": "lifetime", "expires_at": expires_at,
                "message": "Lisensi LIFETIME aktif.", "source": "offline"}

    days_left = (exp - today).days
    # Plan dari key: legacy = enterprise (ID+SG); V2 bisa "global" (semua region).
    return {"valid": True, "plan": plan_name, "expires_at": expires_at,
            "message": f"Lisensi {plan_name.upper()} aktif hingga {expires_at} ({days_left} hari lagi).",
            "source": "offline"}


def clear_license_cache() -> None:
    """Hapus file cache lisensi lokal (kalau ada)."""
    for cache_file in _CACHE_FILES:
        try:
            if cache_file.exists():
                cache_file.unlink()
        except Exception:
            pass


def reset_license_device(license_key: str) -> dict:
    """Lisensi offline terkunci ke Machine ID; tidak ada binding server untuk direset."""
    return {"ok": False,
            "message": ("Lisensi offline terkunci ke Machine ID perangkat ini. "
                        "Untuk pindah/ganti device, minta admin membuat license key baru "
                        "memakai Machine ID device yang baru."),
            "source": "offline"}
