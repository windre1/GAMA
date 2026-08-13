"""
gama_update.py — Auto-update GAMA lewat GitHub Releases.

Alur:
  1. App menanyakan rilis terbaru ke GitHub API (repo PUBLIK, tanpa token).
  2. Versi dibandingkan dengan APP_VERSION di bawah.
  3. Kalau lebih baru: installer diunduh ke folder temp sambil menampilkan
     progres, lalu ukurannya diverifikasi.
  4. Installer dijalankan, GAMA menutup diri supaya file-nya bisa ditimpa.

GAMA dipasang di %LOCALAPPDATA%\\Programs\\GAMA dengan PrivilegesRequired=lowest,
jadi installer baru bisa jalan TANPA UAC dan tanpa hak admin.

Yang perlu diisi sekali: GITHUB_OWNER dan GITHUB_REPO di bawah.
Repo harus PUBLIK — repo privat mengharuskan token ikut tertanam di aplikasi,
dan token yang dibagikan ke user sama saja dengan bocor.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

# ── Versi aplikasi — SATU-SATUNYA sumber kebenaran ────────────────────────
# setup.iss membaca nilai ini lewat build_installer (ISCC /DMyAppVersion=...),
# jadi versi di aplikasi dan di installer tidak akan pernah beda.
APP_VERSION = "1.1.17"

# ── Sumber rilis ──────────────────────────────────────────────────────────
GITHUB_OWNER = "windre1"
GITHUB_REPO = "GAMA"

_API_LATEST = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
_UA = {"User-Agent": "GAMA-Updater", "Accept": "application/vnd.github+json"}

# Nama file installer yang dicari di dalam aset rilis
_ASSET_RE = re.compile(r"^GAMA-Setup-.*\.exe$", re.IGNORECASE)

# ── State unduhan (dibaca endpoint /api/update/status) ────────────────────
_lock = threading.Lock()
_state = {
    "running": False,
    "done": False,
    "success": False,
    "error": None,
    "pct": 0,
    "downloaded": 0,
    "total": 0,
    "file": None,
    "version": None,
}


def _set(**kw):
    with _lock:
        _state.update(kw)


def get_state():
    with _lock:
        return dict(_state)


# ── Perbandingan versi ────────────────────────────────────────────────────
def parse_version(text):
    """'v1.2.10' -> (1, 2, 10). Bagian non-angka diabaikan."""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest, current):
    """True kalau `latest` lebih baru dari `current`.

    Perbandingan per-angka, BUKAN string: '1.1.10' > '1.1.9' walau secara
    string '1.1.10' < '1.1.9'.
    """
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def is_configured():
    return not (GITHUB_OWNER.startswith("GANTI") or GITHUB_REPO.startswith("GANTI"))


# ── Cek rilis terbaru ─────────────────────────────────────────────────────
def fetch_latest(timeout=15):
    """Ambil info rilis terbaru dari GitHub.

    Return dict: {version, url, size, notes, published, asset}
    Melempar Exception dengan pesan yang bisa ditampilkan ke user.
    """
    if not is_configured():
        raise Exception(
            "Sumber update belum diatur. Isi GITHUB_OWNER dan GITHUB_REPO "
            "di gama_update.py."
        )
    import requests

    url = _API_LATEST.format(owner=GITHUB_OWNER, repo=GITHUB_REPO)
    r = requests.get(url, headers=_UA, timeout=timeout)
    if r.status_code == 404:
        raise Exception("Belum ada rilis di repo update, atau repo-nya privat.")
    if r.status_code == 403:
        raise Exception("Batas permintaan GitHub tercapai. Coba lagi beberapa menit.")
    r.raise_for_status()
    data = r.json()

    aset = None
    for a in (data.get("assets") or []):
        if _ASSET_RE.match(a.get("name") or ""):
            aset = a
            break
    if not aset:
        raise Exception(
            "Rilis ditemukan tapi tidak ada file GAMA-Setup-*.exe di dalamnya."
        )

    return {
        "version": (data.get("tag_name") or "").lstrip("vV"),
        "url": aset.get("browser_download_url"),
        "size": int(aset.get("size") or 0),
        "notes": (data.get("body") or "").strip(),
        "published": data.get("published_at") or "",
        "asset": aset.get("name"),
    }


def check():
    """Bandingkan rilis terbaru dengan versi yang sedang jalan."""
    info = fetch_latest()
    info["current"] = APP_VERSION
    info["available"] = is_newer(info["version"], APP_VERSION)
    return info


# ── Unduh ─────────────────────────────────────────────────────────────────
def _download_dir():
    d = os.path.join(tempfile.gettempdir(), "gama_update")
    os.makedirs(d, exist_ok=True)
    return d


def _download(info):
    import requests

    dest = os.path.join(_download_dir(), info["asset"])
    tmp = dest + ".part"

    # Sudah pernah diunduh utuh? Pakai saja, jangan unduh 650 MB lagi.
    if os.path.isfile(dest) and info["size"] and os.path.getsize(dest) == info["size"]:
        _set(pct=100, downloaded=info["size"], total=info["size"],
             file=dest, done=True, success=True, running=False)
        return dest

    total = info.get("size") or 0
    _set(running=True, done=False, success=False, error=None, pct=0,
         downloaded=0, total=total, file=None, version=info["version"])

    with requests.get(info["url"], stream=True, timeout=60,
                      headers={"User-Agent": "GAMA-Updater"}) as r:
        r.raise_for_status()
        if not total:
            total = int(r.headers.get("Content-Length") or 0)
            _set(total=total)
        got = 0
        last = 0.0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                # Batasi update state supaya tidak membanjiri UI
                if now - last > 0.3 or got == total:
                    last = now
                    pct = int(got * 100 / total) if total else 0
                    _set(downloaded=got, pct=min(99, pct))

    # Ukuran harus persis. File installer yang terpotong akan gagal pasang
    # dengan pesan yang membingungkan, jadi lebih baik ditolak di sini.
    if total and os.path.getsize(tmp) != total:
        os.remove(tmp)
        raise Exception(
            "Unduhan tidak utuh (%d dari %d byte). Coba lagi."
            % (os.path.getsize(tmp), total)
        )

    if os.path.isfile(dest):
        os.remove(dest)
    os.replace(tmp, dest)
    _set(pct=100, downloaded=total, total=total, file=dest,
         done=True, success=True, running=False)
    return dest


def start_download(info):
    """Jalankan unduhan di thread terpisah. Return True kalau dimulai."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=False, success=False, error=None,
                      pct=0, downloaded=0, total=info.get("size") or 0,
                      file=None, version=info.get("version"))

    def kerja():
        try:
            _download(info)
        except Exception as e:
            _set(running=False, done=True, success=False, error=str(e))

    threading.Thread(target=kerja, daemon=True).start()
    return True


# ── Pasang ────────────────────────────────────────────────────────────────
def run_installer(path):
    """Jalankan installer lalu beri tahu pemanggil untuk menutup GAMA.

    Installer dijalankan lewat cmd `start` supaya proses-nya lepas dari GAMA;
    kalau tidak, menutup GAMA ikut mematikan installer-nya.
    Jeda 2 detik memberi waktu GAMA benar-benar keluar sebelum installer
    mulai menimpa file — kalau tidak, file terkunci dan pemasangan gagal.
    """
    if not path or not os.path.isfile(path):
        raise Exception("File installer tidak ditemukan: %s" % path)
    if os.name != "nt":
        raise Exception("Update otomatis hanya tersedia di Windows.")
    subprocess.Popen(
        'timeout /t 2 /nobreak >nul & start "" "%s"' % path,
        shell=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def cleanup_old(keep=None):
    """Hapus installer lama di folder temp supaya tidak menumpuk GB-an."""
    d = _download_dir()
    try:
        for nama in os.listdir(d):
            if keep and nama == os.path.basename(keep):
                continue
            p = os.path.join(d, nama)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                else:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass
