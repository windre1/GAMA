# -*- mode: python ; coding: utf-8 -*-
"""
gama.spec — Build GAMA jadi folder distribusi (onedir) berisi BANYAK .exe:
    GAMA.exe                      -> app utama (Flask + pywebview)
    facebook_bridge.exe           -> worker upload/grup Facebook
    instagram_bridge.exe          -> worker upload Instagram
    social_bridge.exe             -> worker upload Pinterest/Threads/X
    social_downloader_bridge.exe  -> worker Downloader (FB/IG/Threads/X/Pinterest)
    shopee_adb_bridge.exe         -> worker Shopee via ADB (console=True, sengaja)
    gmaps_bridge.exe              -> worker Prospek GMaps
    metadata_cleaner_bridge.exe   -> worker Metadata Cleaner
    video_generator_bridge.exe    -> worker Video Generator (Google Flow)
    voice_over_bridge.exe         -> worker Voice Over (Gemini Canvas)
    yt_bridge.exe                 -> worker upload YouTube (DOM automation)
    tiktok_bridge.exe             -> worker upload TikTok (via TEKOTOK)

Kenapa banyak exe, bukan satu: main.py men-spawn tiap fitur berat sebagai
proses TERPISAH (_start_worker di main.py) — supaya 1 upload/scrape yang
nge-hang/crash tidak menjatuhkan seluruh app, dan supaya Shopee/ADB bisa
punya console sendiri. Semua exe di sini duduk di SATU folder output yang
sama (lihat COLLECT paling bawah) sehingga saling menemukan lewat BASE_DIR
(folder GAMA.exe) — lihat fungsi frozen-mode BASE_DIR di tiap file.

SENGAJA TIDAK ikut ke build (lihat datas di bawah — cuma whitelist eksplisit):
    data/   -> akun Google, password, API key, lisensi ADMIN — JANGAN PERNAH ikut.
    tools/  -> keygen (private key) + license_registry.json (data pelanggan).
    downloads/, output/, exports/, docs/, Upload/, __pycache__/ -> tidak relevan/dev-only.

Cara build:
    pyinstaller gama.spec --noconfirm --clean
Hasil ada di dist/GAMA/ (folder itu yang didistribusikan/di-zip ke pembeli).

PENTING — `contents_directory='.'` di COLLECT() paling bawah file ini:
PyInstaller 6+ defaultnya menaruh SEMUA file bundel (assets/, templates/,
engines/, dll) di subfolder `_internal/`, BUKAN langsung di dist/GAMA/. Tapi
BASE_DIR frozen-mode di main.py/semua bridge/engines (`os.path.dirname(
sys.executable)`) mengasumsikan bundel ada LANGSUNG di folder GAMA.exe (sama
seperti data/downloads/output yang memang harus di situ, bukan di _internal).
`contents_directory='.'` mematikan subfolder _internal itu (balik ke layout
lama, flat) supaya BASE_DIR yang sama bisa dipakai baik untuk baca bundel
maupun tulis data — TANPA ini, Chrome bundel/adb bundel/engines/assets semua
tidak ketemu di hasil compile (JANGAN pakai flag CLI --contents-directory —
itu cuma valid saat generate .spec baru, error kalau .spec sudah ada).
"""
import os
from PyInstaller.utils.hooks import collect_all

PROJECT_DIR = os.path.abspath(os.path.dirname(SPEC))
block_cipher = None

# yt-dlp memuat banyak extractor secara dinamis (pkgutil) — auto-detect
# PyInstaller sering miss extractor-extractor itu kalau tidak di-collect eksplisit.
ytdlp_datas, ytdlp_binaries, ytdlp_hidden = collect_all('yt_dlp')

# googleapiclient juga load resource discovery secara dinamis.
gapi_datas, gapi_binaries, gapi_hidden = collect_all('googleapiclient')

# peewee (ORM dipakai engine YT Uploader) — insurance, biasanya cukup pure python.
peewee_datas, peewee_binaries, peewee_hidden = collect_all('peewee')

# ── neonize (WhatsApp engine) ─────────────────────────────────────────────────
# collect_all bundel: semua .py, proto/_pb2.py, DAN DLL
# (neonize-windows-amd64.dll) yang di-load via ctypes.CDLL() secara dinamis.
# PyInstaller tidak bisa mendeteksi ctypes.CDLL() statis — WAJIB eksplisit.
# DLL dibundel ke subfolder neonize/ supaya root_dir di _binder.py ketemu
# (di-patch lewat rthook_neonize.py saat frozen).
neonize_datas, neonize_binaries, neonize_hidden = collect_all('neonize')

COMMON_HIDDEN = [
    'cryptography.hazmat.backends.openssl',
    'engineio.async_drivers.threading',
    # websocket-client — dipakai gama_identitas.py untuk memasang fingerprint
    # lewat CDP. Diimpor di dalam fungsi, jadi PyInstaller tidak melihatnya
    # dan tanpa baris ini fingerprint diam-diam mati di versi EXE.
    'websocket',
    'websocket._core',
    'websocket._app',
    # Modul TikTok yang juga diimpor di dalam fungsi (bukan di kepala berkas),
    # jadi tidak terlihat oleh PyInstaller. Tanpa baris ini, metadata lengkap
    # diam-diam hilang di versi EXE.
    'tiktok_downloader',
    # neonize & semua submodulnya (proto, utils, aioze, ext, events, client)
    'neonize',
    'neonize._binder',
    'neonize.client',
    'neonize.events',
    'neonize.utils',
    'neonize.proto',
    'neonize.download',
    'neonize.exc',
    'neonize.types',
    'neonize.const',
] + ytdlp_hidden + gapi_hidden + peewee_hidden + neonize_hidden

# datas yang HANYA perlu nempel di GAMA.exe (resource dipakai Flask/UI utama).
MAIN_DATAS = [
    (os.path.join(PROJECT_DIR, 'templates'), 'templates'),
    (os.path.join(PROJECT_DIR, 'static'), 'static'),
    (os.path.join(PROJECT_DIR, 'assets'), 'assets'),
    (os.path.join(PROJECT_DIR, 'engines'), 'engines'),
] + ytdlp_datas + gapi_datas + peewee_datas + neonize_datas

# neonize DLL (neonize-windows-amd64.dll) harus ikut sebagai binary supaya
# ctypes.CDLL() bisa menemukannya. collect_all() sudah memasukkannya ke
# neonize_binaries dengan dest path 'neonize/'.
COMMON_BINARIES = ytdlp_binaries + gapi_binaries + peewee_binaries + neonize_binaries

# Catatan: tidak ada "exclude list" aktif di sini karena datas di atas sudah
# whitelist eksplisit (cuma templates/static/assets) — apa pun di luar itu
# (data/, tools/, downloads/, dst.) otomatis TIDAK ikut karena tidak disebut.


def make_analysis(script, datas=None, hiddenimports=None):
    return Analysis(
        [os.path.join(PROJECT_DIR, script)],
        pathex=[PROJECT_DIR],
        binaries=COMMON_BINARIES,
        datas=datas or [],
        hiddenimports=COMMON_HIDDEN + (hiddenimports or []),
        hookspath=[os.path.join(PROJECT_DIR, 'hooks')],
        hooksconfig={},
        runtime_hooks=[os.path.join(PROJECT_DIR, 'hooks', 'rthook_neonize.py')],
        excludes=[],
        noarchive=False,
        cipher=block_cipher,
    )


def make_exe(analysis, name, console=False, icon=None):
    pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)
    return EXE(
        pyz, analysis.scripts, [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=console,
        icon=icon,
        # '.' matikan subfolder _internal/ (default PyInstaller 6+) — parameter
        # ini sebenarnya milik EXE(), BUKAN COLLECT() (sempat salah taruh).
        contents_directory='.',
    )


# ─── GAMA.exe (app utama) ────────────────────────────────────────────────────
# gama_update di-import DI DALAM fungsi endpoint (lazy). PyInstaller biasanya
# tetap menangkap import semacam itu, tapi kalau sampai terlewat, fitur update
# mati diam-diam di build padahal jalan normal saat dites dari source —
# jenis kegagalan yang paling sulit dilacak. Didaftarkan eksplisit supaya pasti.
a_main = make_analysis('main.py', datas=MAIN_DATAS,
                       hiddenimports=['gama_update'])
exe_main = make_exe(a_main, 'GAMA', console=False,
                     icon=os.path.join(PROJECT_DIR, 'assets', 'logo.ico'))

# ─── video_generator_bridge.exe & voice_over_bridge.exe (dua-duanya
# `from main import ...`, jadi butuh Analysis sendiri, bukan SIMPLE_WORKERS) ─
a_videogen = make_analysis('video_generator_bridge.py')
exe_videogen = make_exe(a_videogen, 'video_generator_bridge', console=False)

a_voiceover = make_analysis('voice_over_bridge.py')
exe_voiceover = make_exe(a_voiceover, 'voice_over_bridge', console=False)

# ─── shopee_adb_bridge.exe (SENGAJA console=True — adb butuh console nyata) ─
a_shopee = make_analysis('shopee_adb_bridge.py')
exe_shopee = make_exe(a_shopee, 'shopee_adb_bridge', console=True)

# ─── Worker sisanya — windowed (console=False), tidak butuh datas sendiri ───
SIMPLE_WORKERS = [
    'facebook_bridge',
    'instagram_bridge',
    'social_bridge',
    'social_downloader_bridge',
    'gmaps_bridge',
    'metadata_cleaner_bridge',
    'yt_bridge',
    'tiktok_bridge',
]

# yt_bridge.py & tiktok_bridge.py TIDAK meng-import DrissionPage/peewee di
# level file mereka sendiri (baru dipakai lewat sys.path.insert() ke folder
# engines/ saat runtime) — PyInstaller tidak bisa mendeteksi ini secara statis,
# jadi WAJIB dipaksa via hiddenimports supaya ikut ke-bundel exe-nya.
EXTRA_HIDDEN = {
    'yt_bridge':      ['DrissionPage', 'peewee'],
    'tiktok_bridge':  ['DrissionPage'],
}

simple_analyses = []
simple_exes = []
for worker in SIMPLE_WORKERS:
    a = make_analysis(worker + '.py', hiddenimports=EXTRA_HIDDEN.get(worker))
    e = make_exe(a, worker, console=False)
    simple_analyses.append(a)
    simple_exes.append(e)

# ─── Satu COLLECT gabungan — semua exe + dependency-nya duduk 1 folder ──────
all_analyses = [a_main, a_videogen, a_voiceover, a_shopee] + simple_analyses
all_exes = [exe_main, exe_videogen, exe_voiceover, exe_shopee] + simple_exes

merged_binaries = sum((a.binaries for a in all_analyses), [])
merged_zipfiles = sum((a.zipfiles for a in all_analyses), [])
merged_datas = sum((a.datas for a in all_analyses), [])

coll = COLLECT(
    *all_exes,
    merged_binaries,
    merged_zipfiles,
    merged_datas,
    strip=False,
    upx=False,
    name='GAMA',
)
