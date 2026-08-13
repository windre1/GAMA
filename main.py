"""
main.py — Backend Flask untuk Google Account Manager (GAM).

Jalankan:
    python main.py          → mode web (browser)
    python main.py --web    → paksa mode web

Mode desktop (pywebview) aktif otomatis bila pywebview terinstall dan
dijalankan tanpa --web.
"""
import os
import re
import sys
import json
import shutil
import subprocess
import threading
import time
import webbrowser
import mimetypes
from datetime import datetime
from urllib.parse import quote_plus, urlparse, unquote

from flask import Flask, render_template, request, jsonify, Response, send_from_directory

# ─── Path & konstanta ────────────────────────────────────────────────────────

# Frozen (dikompilasi PyInstaller): BASE_DIR = folder tempat GAMA.exe berada
# (persisten, TIDAK boleh pakai __file__ — itu resolve ke folder ekstrak
# sementara PyInstaller yang hilang tiap app ditutup). Mode dev: folder file ini.
BASE_DIR     = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
               else os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(BASE_DIR, 'data')
PROFILES_DIR = os.path.join(DATA_DIR, 'google_profiles')
META_FILE    = os.path.join(DATA_DIR, 'profiles_meta.json')
JOBS_DIR     = os.path.join(DATA_DIR, 'yt_jobs')
# Instalasi baru (belum pernah dibuka) tidak punya folder data/ sama sekali —
# pastikan ada dari awal supaya rute yang langsung nulis ke situ tidak gagal.
os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

def _resolve_engine_dir(bundled_name: str, env_var: str, default: str) -> str:
    """Cari folder engine (YT Uploader / TEKOTOK) dengan prioritas:
    1. Bundel di dalam GAMA: <BASE_DIR>/engines/<bundled_name> (dipakai installer).
    2. Override lewat environment variable.
    3. Default lama (D:\\TOOLS\\...) — untuk mesin dev sekarang.
    Path bundel yang ketemu diekspor ke env supaya worker subprocess (yt_bridge/
    tiktok_bridge yang baca os.environ) ikut memakai folder bundel."""
    bundled = os.path.join(BASE_DIR, 'engines', bundled_name)
    chosen = None
    if os.path.isdir(bundled):
        chosen = bundled
    elif os.environ.get(env_var) and os.path.isdir(os.environ[env_var]):
        chosen = os.environ[env_var]
    else:
        chosen = os.environ.get(env_var) or default
    os.environ[env_var] = chosen   # diwariskan ke subprocess bridge
    return chosen


YT_UPLOADER_DIR  = _resolve_engine_dir('yt_uploader', 'YT_UPLOADER_DIR', r'D:\TOOLS\YT Uploader')
TEKOTOK_DIR      = _resolve_engine_dir('tekotok',     'TEKOTOK_DIR',     r'D:\TOOLS\TEKOTOK')
SHOVEBOT_DIR     = os.environ.get('SHOVEBOT_DIR',      r'D:\TOOLS\ShoveBot - 3.6')
SHOVEBOT_SERVER  = os.path.join(SHOVEBOT_DIR, 'dist', 'ShoveBot', 'ShoveBot.exe')
SHOVEBOT_ADB     = os.path.join(SHOVEBOT_DIR, 'tools', 'platform-tools', 'adb.exe')
SHOVEBOT_PURCHASE_URL = 'https://www.shove.my.id'

# Paket aplikasi Shopee per region (download Shopee Video via ADB)
PKG_BY_REGION = {
    'id': 'com.shopee.id', 'sg': 'com.shopee.sg', 'my': 'com.shopee.my',
    'th': 'com.shopee.th', 'ph': 'com.shopee.ph',
}

# adb.exe yang dibundel bersama GAMA (mandiri, tidak butuh ShoveBot)
GAMA_ADB = os.path.join(BASE_DIR, 'assets', 'platform-tools', 'adb.exe')


def _resolve_adb() -> str | None:
    """adb yang dipakai GAMA: utamakan yang dibundel, fallback ke ShoveBot."""
    if os.path.isfile(GAMA_ADB):
        return GAMA_ADB
    info = _detect_shovebot()
    return info.get('adb_path')

# Chrome untuk otomasi. Prioritas: env GAMA_BROWSER → Chrome yang dibundel
# installer (assets/chrome) → Chrome ter-install. Installer nanti memastikan
# Chrome tersedia, jadi tidak perlu fallback browser lain.
CHROME_PATHS = [
    os.path.join(BASE_DIR, 'assets', 'chrome', 'chrome.exe'),   # dibundel installer
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    os.path.join(os.environ.get('LOCALAPPDATA', ''), r'Google\Chrome\Application\chrome.exe'),
]

# Platform yang belum punya engine upload dedicated
UNSUPPORTED_UPLOADS = {}   # kosong – semua platform sudah punya bridge

# Daftar situs shortcut yang bisa dibuka dari kartu profil, dikelompokkan per
# kategori. `category` dipakai UI kartu profil untuk menampilkan shortcut
# ber-grup (Sosmed / AI / Lainnya / Kustom). Site dasar (built-in) tidak bisa
# dihapus user; untuk URL custom lihat load_custom_sites()/api di bawah.
SITES = {
    # ── Sosial Media ───────────────────────────────────────────────────────
    'youtube':     {'label': 'YouTube',      'url': 'https://www.youtube.com',        'icon': 'fa-brands fa-youtube',         'color': '#ef4444',   'category': 'sosmed'},
    'facebook':    {'label': 'Facebook',     'url': 'https://www.facebook.com',       'icon': 'fa-brands fa-facebook-f',      'color': '#1877f2',   'category': 'sosmed'},
    'instagram':   {'label': 'Instagram',    'url': 'https://www.instagram.com',      'icon': 'fa-brands fa-instagram',       'color': '#e1306c',   'category': 'sosmed'},
    'x':           {'label': 'X (Twitter)',  'url': 'https://x.com',                  'icon': 'fa-brands fa-x-twitter',       'color': '#111111',   'category': 'sosmed'},
    'threads':     {'label': 'Threads',      'url': 'https://www.threads.net',        'icon': 'fa-brands fa-threads',         'color': '#111111',   'category': 'sosmed'},
    'tiktok':      {'label': 'TikTok',       'url': 'https://www.tiktok.com',         'icon': 'fa-brands fa-tiktok',          'color': '#ff0050',   'category': 'sosmed'},
    'pinterest':   {'label': 'Pinterest',    'url': 'https://www.pinterest.com',      'icon': 'fa-brands fa-pinterest',       'color': '#e60023',   'category': 'sosmed'},
    'dailymotion': {'label': 'Dailymotion',  'url': 'https://www.dailymotion.com/',   'icon': 'fa-brands fa-dailymotion',     'color': '#0066dc',   'category': 'sosmed'},

    # ── AI (bawaan, paling sering dipakai) — logo asli (inline SVG) ─────────
    'chatgpt':     {'label': 'ChatGPT',      'url': 'https://chatgpt.com',            'icon': '<svg viewBox="0 0 24 24" fill="#10a37f" xmlns="http://www.w3.org/2000/svg"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/></svg>', 'color': '#10a37f', 'category': 'ai'},
    'claude':      {'label': 'Claude',       'url': 'https://claude.ai',              'icon': '<svg viewBox="0 0 24 24" fill="#d97757" xmlns="http://www.w3.org/2000/svg"><path d="M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z"/></svg>', 'color': '#d97757', 'category': 'ai'},
    'gemini':      {'label': 'Gemini',       'url': 'https://gemini.google.com',      'icon': '<svg viewBox="0 0 24 24" fill="#4f8ef7" xmlns="http://www.w3.org/2000/svg"><path d="M11.04 19.32Q12 21.51 12 24q0-2.49.93-4.68.96-2.19 2.58-3.81t3.81-2.55Q21.51 12 24 12q-2.49 0-4.68-.93a12.3 12.3 0 0 1-3.81-2.58 12.3 12.3 0 0 1-2.58-3.81Q12 2.49 12 0q0 2.49-.96 4.68-.93 2.19-2.55 3.81a12.3 12.3 0 0 1-3.81 2.58Q2.49 12 0 12q2.49 0 4.68.96 2.19.93 3.81 2.55t2.55 3.81"/></svg>', 'color': '#4f8ef7', 'category': 'ai'},
    'qwen':        {'label': 'Qwen',         'url': 'https://chat.qwen.ai',           'icon': '<svg viewBox="0 0 24 24" fill="#7c3aed" xmlns="http://www.w3.org/2000/svg"><path d="M23.919 14.545 20.817 9.17l1.47-2.544a.56.56 0 0 0 0-.566l-1.633-2.83a.57.57 0 0 0-.49-.283h-6.207L12.487.402a.57.57 0 0 0-.49-.284H8.732a.56.56 0 0 0-.49.284L5.139 5.775h-2.94a.56.56 0 0 0-.49.284L.077 8.887a.56.56 0 0 0 0 .567L3.18 14.83l-1.47 2.545a.56.56 0 0 0 0 .566l1.634 2.83a.57.57 0 0 0 .49.283h6.205l1.47 2.545a.57.57 0 0 0 .49.284h3.266a.57.57 0 0 0 .49-.284l3.104-5.375h2.94a.57.57 0 0 0 .49-.283l1.634-2.828a.55.55 0 0 0-.004-.568M8.733.686l1.634 2.828-1.634 2.828H21.8L20.164 9.17H7.425L5.63 6.06Zm1.306 19.801-6.205-.002 1.634-2.83h3.265L2.201 6.344h3.267q3.182 5.517 6.367 11.032zm10.124-5.66L18.53 12l-6.532 11.315-1.634-2.83c2.129-3.673 4.25-7.351 6.373-11.028h3.592l3.102 5.374z"/></svg>', 'color': '#7c3aed', 'category': 'ai'},
    'perplexity':  {'label': 'Perplexity',   'url': 'https://www.perplexity.ai',      'icon': '<svg viewBox="0 0 24 24" fill="#20b8cd" xmlns="http://www.w3.org/2000/svg"><path d="M22.3977 7.0896h-2.3106V.0676l-7.5094 6.3542V.1577h-1.1554v6.1966L4.4904 0v7.0896H1.6023v10.3976h2.8882V24l6.932-6.3591v6.2005h1.1554v-6.0469l6.9318 6.1807v-6.4879h2.8882V7.0896zm-3.4657-4.531v4.531h-5.355l5.355-4.531zm-13.2862.0676 4.8691 4.4634H5.6458V2.6262zM2.7576 16.332V8.245h7.8476l-6.1149 6.1147v1.9723H2.7576zm2.8882 5.0404v-3.8852h.0001v-2.6488l5.7763-5.7764v7.0111l-5.7764 5.2993zm12.7086.0248-5.7766-5.1509V9.0618l5.7766 5.7766v6.5588zm2.8882-5.0652h-1.733v-1.9723L13.3948 8.245h7.8478v8.087z"/></svg>', 'color': '#20b8cd', 'category': 'ai'},
    'copilot':     {'label': 'Copilot',      'url': 'https://copilot.microsoft.com',  'icon': '<svg viewBox="0 0 24 24" fill="#217bfb" xmlns="http://www.w3.org/2000/svg"><path d="M12 3.2c-2.6 2.6-4.3 5.3-5 8.1l-1.4-1.1C4.6 12.8 4 15 4 17.3 4 20 5.6 21.8 8 22c1.3.1 2.5-.4 3.5-1.2.4.8 1 1.3 1.8 1.5 1.9.5 4.3-.7 4.3-4 0-3.1-1.4-6.2-3.9-9.3C12.2 6.3 12.2 4.5 12 3.2Z"/></svg>', 'color': '#217bfb', 'category': 'ai'},
    'grok':        {'label': 'Grok',         'url': 'https://grok.com',               'icon': '<svg viewBox="0 0 24 24" fill="#f59e0b" xmlns="http://www.w3.org/2000/svg"><path d="M12 1.2 14.5 9.5 22.8 12l-8.3 2.5L12 22.8l-2.5-8.3L1.2 12l8.3-2.5L12 1.2z"/></svg>', 'color': '#f59e0b', 'category': 'ai'},
    'deepseek':    {'label': 'DeepSeek',     'url': 'https://chat.deepseek.com',      'icon': '<svg viewBox="0 0 24 24" fill="#4D6BFE" xmlns="http://www.w3.org/2000/svg"><path d="M23.748 4.651c-.254-.124-.364.113-.512.233-.051.04-.094.09-.137.137-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.155-.708-.311-.955-.65-.172-.24-.219-.509-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.094.172.187.129.323-.082.28-.18.553-.266.833-.055.179-.137.218-.328.14a5.5 5.5 0 0 1-1.737-1.179c-.857-.828-1.631-1.743-2.597-2.46a12 12 0 0 0-.689-.47c-.985-.957.13-1.743.387-1.836.27-.098.094-.433-.778-.428-.872.003-1.67.295-2.687.685a3 3 0 0 1-.465.136 9.6 9.6 0 0 0-2.883-.101c-1.885.21-3.39 1.1-4.497 2.622C.082 8.776-.231 10.854.152 13.02c.403 2.284 1.568 4.175 3.36 5.653 1.857 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.132-.284 4.994-1.86.47.234.962.328 1.78.398.629.058 1.235-.031 1.705-.129.735-.155.684-.836.418-.961-2.155-1.004-1.682-.595-2.112-.926 1.095-1.295 2.768-3.598 3.284-6.733.05-.346.115-.834.108-1.114-.004-.171.035-.238.23-.257a4.2 4.2 0 0 0 1.545-.475c1.397-.763 1.96-2.016 2.093-3.517.02-.23-.004-.467-.247-.588M11.58 18.168c-2.088-1.642-3.101-2.183-3.52-2.16-.39.024-.32.472-.234.763.09.288.207.487.371.74.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.168-1.361-.801-2.5-1.86-3.301-3.306-.775-1.393-1.225-2.888-1.299-4.482-.02-.385.094-.522.477-.592a4.7 4.7 0 0 1 1.53-.038c2.131.311 3.946 1.264 5.467 2.774.868.86 1.525 1.887 2.202 2.89.72 1.066 1.494 2.082 2.48 2.915.348.291.626.513.892.677-.802.09-2.14.109-3.055-.615zm1.001-6.44a.306.306 0 0 1 .415-.287.3.3 0 0 1 .113.074.3.3 0 0 1 .086.214c0 .17-.136.307-.308.307a.303.303 0 0 1-.306-.307m3.11 1.596c-.2.081-.4.151-.591.16a1.25 1.25 0 0 1-.798-.254c-.274-.23-.47-.358-.551-.758a1.7 1.7 0 0 1 .015-.588c.07-.327-.007-.537-.238-.727-.188-.156-.426-.199-.689-.199a.6.6 0 0 1-.254-.078.253.253 0 0 1-.114-.358 1 1 0 0 1 .192-.21c.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.392.451.462.576.685.915.176.264.336.536.446.848.066.194-.02.353-.25.45"/></svg>', 'color': '#4d7cfe', 'category': 'ai'},
    'mistral':     {'label': 'Mistral',      'url': 'https://chat.mistral.ai',        'icon': '<svg viewBox="0 0 24 24" fill="#fa5300" xmlns="http://www.w3.org/2000/svg"><path d="M17.143 3.429v3.428h-3.429v3.429h-3.428V6.857H6.857V3.43H3.43v13.714H0v3.428h10.286v-3.428H6.857v-3.429h3.429v3.429h3.429v-3.429h3.428v3.429h-3.428v3.428H24v-3.428h-3.43V3.429z"/></svg>', 'color': '#fa5300', 'category': 'ai'},
    'meta_ai':     {'label': 'Meta AI',      'url': 'https://www.meta.ai',            'icon': '<svg viewBox="0 0 24 24" fill="#0866FF" xmlns="http://www.w3.org/2000/svg"><path d="M6.915 4.03c-1.968 0-3.683 1.28-4.871 3.113C.704 9.208 0 11.883 0 14.449c0 .706.07 1.369.21 1.973a6.624 6.624 0 0 0 .265.86 5.297 5.297 0 0 0 .371.761c.696 1.159 1.818 1.927 3.593 1.927 1.497 0 2.633-.671 3.965-2.444.76-1.012 1.144-1.626 2.663-4.32l.756-1.339.186-.325c.061.1.121.196.183.3l2.152 3.595c.724 1.21 1.665 2.556 2.47 3.314 1.046.987 1.992 1.22 3.06 1.22 1.075 0 1.876-.355 2.455-.843a3.743 3.743 0 0 0 .81-.973c.542-.939.861-2.127.861-3.745 0-2.72-.681-5.357-2.084-7.45-1.282-1.912-2.957-2.93-4.716-2.93-1.047 0-2.088.467-3.053 1.308-.652.57-1.257 1.29-1.82 2.05-.69-.875-1.335-1.547-1.958-2.056-1.182-.966-2.315-1.303-3.454-1.303zm10.16 2.053c1.147 0 2.188.758 2.992 1.999 1.132 1.748 1.647 4.195 1.647 6.4 0 1.548-.368 2.9-1.839 2.9-.58 0-1.027-.23-1.664-1.004-.496-.601-1.343-1.878-2.832-4.358l-.617-1.028a44.908 44.908 0 0 0-1.255-1.98c.07-.109.141-.224.211-.327 1.12-1.667 2.118-2.602 3.358-2.602zm-10.201.553c1.265 0 2.058.791 2.675 1.446.307.327.737.871 1.234 1.579l-1.02 1.566c-.757 1.163-1.882 3.017-2.837 4.338-1.191 1.649-1.81 1.817-2.486 1.817-.524 0-1.038-.237-1.383-.794-.263-.426-.464-1.13-.464-2.046 0-2.221.63-4.535 1.66-6.088.454-.687.964-1.226 1.533-1.533a2.264 2.264 0 0 1 1.088-.285z"/></svg>', 'color': '#0460ff', 'category': 'ai'},
    'leonardo':    {'label': 'Leonardo AI',  'url': 'https://app.leonardo.ai',        'icon': '<svg viewBox="0 0 24 24" fill="#7c3aed" xmlns="http://www.w3.org/2000/svg"><path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm0 2.4c5.302 0 9.6 4.298 9.6 9.6s-4.298 9.6-9.6 9.6S2.4 17.302 2.4 12 6.698 2.4 12 2.4zm-1.2 3.6v4.8H6l6 7.2 6-7.2h-4.8V6h-2.4z"/></svg>', 'color': '#f97316', 'category': 'ai'},
    'midjourney':  {'label': 'Midjourney',   'url': 'https://www.midjourney.com',     'icon': '<svg viewBox="0 0 24 24" fill="#111111" xmlns="http://www.w3.org/2000/svg"><path d="M2.405 2C1.077 2 0 3.077 0 4.405v15.19C0 20.923 1.077 22 2.405 22h6.932l2.663-6.357L9.325 12l2.675-6.357L14.663 12l-2.675 3.643L14.663 22h6.932C22.923 22 24 20.923 24 19.595V4.405C24 3.077 22.923 2 21.595 2H2.405z"/></svg>', 'color': '#000000', 'category': 'ai'},
    'runway':      {'label': 'Runway',       'url': 'https://runwayml.com',           'icon': '<svg viewBox="0 0 24 24" fill="#111111" xmlns="http://www.w3.org/2000/svg"><path d="M3 3h18v3H3zm0 4.5h10.5V12H3zm0 6h10.5v4.5H3zm12-6H21V21h-6z"/></svg>', 'color': '#7c3aed', 'category': 'ai'},
    'pika':        {'label': 'Pika',         'url': 'https://pika.art',               'icon': '<svg viewBox="0 0 24 24" fill="#7c3aed" xmlns="http://www.w3.org/2000/svg"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>', 'color': '#ec4899', 'category': 'ai'},
    'kling':       {'label': 'Kling AI',     'url': 'https://klingai.com',            'icon': '<svg viewBox="0 0 24 24" fill="#111111" xmlns="http://www.w3.org/2000/svg"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 14H9V8h2v8zm4 0h-2V8h2v8z"/></svg>', 'color': '#0ea5e9', 'category': 'ai'},
    'suno':        {'label': 'Suno',         'url': 'https://suno.com',               'icon': '<svg viewBox="0 0 24 24" fill="#f43f5e" xmlns="http://www.w3.org/2000/svg"><path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6z"/></svg>', 'color': '#f43f5e', 'category': 'ai'},

    # ── Lainnya ────────────────────────────────────────────────────────────
    'gmail':       {'label': 'Gmail',        'url': 'https://mail.google.com',        'icon': 'fa-solid fa-envelope',         'color': '#ea4335',   'category': 'lainnya'},
    'google':      {'label': 'Google',       'url': 'https://www.google.com',         'icon': 'fa-brands fa-google',          'color': '#4285f4',   'category': 'lainnya'},
    'login':       {'label': 'Login Google', 'url': 'https://accounts.google.com',    'icon': 'fa-solid fa-right-to-bracket', 'color': '#8b5cf6',   'category': 'lainnya'},
    'shopee':      {'label': 'Shopee',       'url': 'https://shopee.co.id',           'icon': 'fa-solid fa-bag-shopping',     'color': '#f97316',   'category': 'lainnya'},
    'shopee_aff':  {'label': 'Shopee Affiliate', 'url': 'https://affiliate.shopee.co.id/dashboard', 'icon': 'fa-solid fa-hand-holding-dollar', 'color': '#ee4d2d', 'category': 'lainnya'},
}

# Konstanta urutan kategori shortcut (dipakai UI & ditambah custom di akhir).
SITE_CATEGORY_ORDER = ['sosmed', 'ai', 'lainnya', 'custom']

# Custom site (URL tambahan user) — GLOBAL, berlaku untuk semua profil.
CUSTOM_SITES_FILE      = os.path.join(DATA_DIR, 'custom_sites.json')
CUSTOM_CATEGORIES_FILE = os.path.join(DATA_DIR, 'custom_categories.json')

# Kategori bawaan yang tidak bisa dihapus
BUILTIN_CATEGORIES = {'sosmed', 'ai', 'lainnya'}


def load_custom_categories() -> list[dict]:
    """Muat daftar kategori custom user. Format: [{id, label, icon}, ...]."""
    try:
        with open(CUSTOM_CATEGORIES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def save_custom_categories(cats: list) -> bool:
    try:
        with _meta_lock:
            tmp = CUSTOM_CATEGORIES_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CUSTOM_CATEGORIES_FILE)
        return True
    except Exception:
        return False


def load_custom_sites() -> list[dict]:
    """Muat daftar URL custom global. Format: [{label, url}, ...]."""
    try:
        with open(CUSTOM_SITES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def save_custom_sites(sites: list) -> bool:
    try:
        with _meta_lock:
            tmp = CUSTOM_SITES_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(sites, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CUSTOM_SITES_FILE)
        return True
    except Exception:
        return False

RISET_PLATFORMS = {
    'shopee': {
        'label': 'Shopee',
        'icon': 'fa-solid fa-bag-shopping',
        'supports_region': True,
        'mode': 'catalog',
    },
    'tiktok': {
        'label': 'TikTok',
        'icon': 'fa-brands fa-tiktok',
        'supports_region': False,
        'mode': 'discovery',
    },
    'youtube': {
        'label': 'YouTube',
        'icon': 'fa-brands fa-youtube',
        'supports_region': False,
        'mode': 'discovery',
    },
    'facebook': {
        'label': 'Facebook',
        'icon': 'fa-brands fa-facebook-f',
        'supports_region': False,
        'mode': 'discovery',
    },
    'instagram': {
        'label': 'Instagram',
        'icon': 'fa-brands fa-instagram',
        'supports_region': False,
        'mode': 'discovery',
    },
    'pinterest': {
        'label': 'Pinterest',
        'icon': 'fa-brands fa-pinterest',
        'supports_region': False,
        'mode': 'discovery',
    },
    'threads': {
        'label': 'Threads',
        'icon': 'fa-brands fa-threads',
        'supports_region': False,
        'mode': 'discovery',
    },
    'x': {
        'label': 'X',
        'icon': 'fa-brands fa-x-twitter',
        'supports_region': False,
        'mode': 'discovery',
    },
}


def _slug_keyword(keyword: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '', (keyword or '').lower())
    return slug or 'viral'


def _build_riset_links(platform: str, keyword: str, region: str = 'id') -> list[dict]:
    encoded = quote_plus(keyword)
    compact = _slug_keyword(keyword)
    platform = (platform or '').lower()
    region = (region or 'id').lower()
    shopee_domain = {
        'id': 'shopee.co.id',
        'sg': 'shopee.sg',
        'ph': 'shopee.ph',
        'th': 'shopee.co.th',
        'my': 'shopee.com.my',
    }.get(region, 'shopee.co.id')
    affiliate_domain = {
        'id': 'affiliate.shopee.co.id',
        'sg': 'affiliate.shopee.sg',
        'ph': 'affiliate.shopee.ph',
        'th': 'affiliate.shopee.co.th',
        'my': 'affiliate.shopee.com.my',
    }.get(region, 'affiliate.shopee.co.id')

    configs = {
        'shopee': [
            {
                'title': f'Cari produk "{keyword}" di Shopee {region.upper()}',
                'subtitle': 'Buka pencarian Shopee langsung dengan akun login yang dipilih untuk lihat harga, seller, dan kompetitor.',
                'url': f'https://{shopee_domain}/search?keyword={encoded}',
                'action_label': 'Buka Shopee Search',
            },
            {
                'title': 'Buka dashboard Affiliate Shopee',
                'subtitle': 'Masuk ke area affiliate untuk lanjut riset produk, komisi, dan penawaran yang tersedia di akun ini.',
                'url': f'https://{affiliate_domain}/offer/product_offer',
                'action_label': 'Buka Affiliate Dashboard',
            },
            {
                'title': 'Google site search Shopee',
                'subtitle': 'Alternatif cepat untuk riset keyword dan variasi listing dari pencarian Google.',
                'url': f'https://www.google.com/search?q={quote_plus("site:" + shopee_domain + " " + keyword)}',
                'action_label': 'Buka Google Search',
            },
        ],
        'tiktok': [
            {
                'title': f'Cari video "{keyword}" di TikTok',
                'subtitle': 'Pencarian native untuk lihat angle konten, creator, dan hook yang lagi ramai.',
                'url': f'https://www.tiktok.com/search?q={encoded}',
                'action_label': 'Buka TikTok Search',
            },
            {
                'title': f'Riset hashtag #{compact}',
                'subtitle': 'Cek volume konten dan pola hashtag yang sering dipakai kompetitor.',
                'url': f'https://www.tiktok.com/tag/{compact}',
                'action_label': 'Buka Hashtag',
            },
            {
                'title': 'Google site search TikTok',
                'subtitle': 'Alternatif cepat untuk bongkar variasi keyword dan landing video dari Google.',
                'url': f'https://www.google.com/search?q={quote_plus("site:tiktok.com " + keyword)}',
                'action_label': 'Buka Google Search',
            },
        ],
        'youtube': [
            {
                'title': f'Cari video "{keyword}" di YouTube',
                'subtitle': 'Lihat judul, thumbnail, dan format video yang paling dominan.',
                'url': f'https://www.youtube.com/results?search_query={encoded}',
                'action_label': 'Buka YouTube Search',
            },
            {
                'title': f'Riset hashtag #{compact}',
                'subtitle': 'Cocok untuk cek tema Shorts dan konten evergreen berbasis hashtag.',
                'url': f'https://www.youtube.com/hashtag/{compact}',
                'action_label': 'Buka Hashtag',
            },
            {
                'title': 'Google site search YouTube',
                'subtitle': 'Pakai ini untuk cari sudut konten yang lebih niche dari hasil pencarian umum.',
                'url': f'https://www.google.com/search?q={quote_plus("site:youtube.com " + keyword)}',
                'action_label': 'Buka Google Search',
            },
        ],
        'facebook': [
            {
                'title': f'Cari topik "{keyword}" di Facebook',
                'subtitle': 'Pantau postingan, reels, dan fanpage yang aktif di niche ini.',
                'url': f'https://www.facebook.com/search/top?q={encoded}',
                'action_label': 'Buka Facebook Search',
            },
            {
                'title': 'Google site search Facebook',
                'subtitle': 'Cepat untuk bongkar fanpage, grup, dan posting publik terkait keyword.',
                'url': f'https://www.google.com/search?q={quote_plus("site:facebook.com " + keyword)}',
                'action_label': 'Buka Google Search',
            },
            {
                'title': 'Riset kompetitor fanpage',
                'subtitle': 'Tambahkan kata brand atau kategori untuk cari page serupa dengan niche kamu.',
                'url': f'https://www.google.com/search?q={quote_plus("site:facebook.com/pages " + keyword)}',
                'action_label': 'Cari Fanpage',
            },
        ],
        'instagram': [
            {
                'title': f'Cari topik "{keyword}" di Instagram',
                'subtitle': 'Cari akun, reels, dan keyword turunan langsung dari search Instagram.',
                'url': f'https://www.instagram.com/explore/search/keyword/?q={encoded}',
                'action_label': 'Buka Instagram Search',
            },
            {
                'title': f'Riset hashtag #{compact}',
                'subtitle': 'Cek posting, reel, dan variasi angle visual yang beredar di hashtag utama.',
                'url': f'https://www.instagram.com/explore/tags/{compact}/',
                'action_label': 'Buka Hashtag',
            },
            {
                'title': 'Google site search Instagram',
                'subtitle': 'Dipakai untuk cari akun niche, roundup, dan postingan yang lebih mudah dipindai.',
                'url': f'https://www.google.com/search?q={quote_plus("site:instagram.com " + keyword)}',
                'action_label': 'Buka Google Search',
            },
        ],
        'pinterest': [
            {
                'title': f'Cari pin "{keyword}" di Pinterest',
                'subtitle': 'Bagus untuk riset visual hook, angle desain, dan intent belanja.',
                'url': f'https://www.pinterest.com/search/pins/?q={encoded}',
                'action_label': 'Buka Pinterest Search',
            },
            {
                'title': 'Google image-style search Pinterest',
                'subtitle': 'Lihat board dan pin yang sering ranking untuk keyword ini.',
                'url': f'https://www.google.com/search?q={quote_plus("site:pinterest.com " + keyword)}',
                'action_label': 'Buka Google Search',
            },
            {
                'title': 'Cari board kompetitor',
                'subtitle': 'Temukan board niche untuk memetakan kata kunci dan style visual yang dipakai.',
                'url': f'https://www.pinterest.com/search/boards/?q={encoded}',
                'action_label': 'Buka Board Search',
            },
        ],
        'threads': [
            {
                'title': f'Cari topik "{keyword}" di Threads',
                'subtitle': 'Pantau angle opini, obrolan, dan phrasing natural yang dipakai audiens.',
                'url': f'https://www.threads.net/search?q={encoded}',
                'action_label': 'Buka Threads Search',
            },
            {
                'title': 'Google site search Threads',
                'subtitle': 'Alternatif jika hasil native sedikit atau ingin cari thread publik dari Google.',
                'url': f'https://www.google.com/search?q={quote_plus("site:threads.net " + keyword)}',
                'action_label': 'Buka Google Search',
            },
            {
                'title': 'Cari pertanyaan audience',
                'subtitle': 'Tambahkan kata tanya di query untuk menemukan masalah nyata dari calon audience.',
                'url': f'https://www.google.com/search?q={quote_plus("site:threads.net " + keyword + " bagaimana OR kenapa OR review")}',
                'action_label': 'Buka Insight Query',
            },
        ],
        'x': [
            {
                'title': f'Cari topik "{keyword}" di X',
                'subtitle': 'Cocok untuk lihat percakapan real-time, opini, dan respon audience.',
                'url': f'https://x.com/search?q={encoded}&src=typed_query&f=live',
                'action_label': 'Buka X Search',
            },
            {
                'title': f'Riset hashtag #{compact}',
                'subtitle': 'Lihat bagaimana hashtag dipakai dalam diskusi dan kampanye yang sedang jalan.',
                'url': f'https://x.com/search?q={quote_plus("#" + compact)}&src=typed_query',
                'action_label': 'Buka Hashtag',
            },
            {
                'title': 'Google site search X',
                'subtitle': 'Bantu cari akun, thread, dan quote yang lebih mudah discan dari Google.',
                'url': f'https://www.google.com/search?q={quote_plus("site:x.com " + keyword)}',
                'action_label': 'Buka Google Search',
            },
        ],
    }

    if platform not in configs:
        configs[platform] = [
            {
                'title': f'Cari "{keyword}" via Google',
                'subtitle': 'Fallback riset umum untuk membuka jalur eksplorasi lintas platform.',
                'url': f'https://www.google.com/search?q={encoded}',
                'action_label': 'Buka Google Search',
            }
        ]
    return configs[platform]


# Registry job yang sedang berjalan: job_id → {proc, status_path, profile, title}
_yt_jobs: dict = {}
_yt_jobs_lock = threading.Lock()

# Metadata (tags, note, group, channels, last_launched) per profil
_meta_lock = threading.Lock()

# ─── Flask app ───────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder='templates', static_folder='static')
# Selalu baca ulang template & jangan cache file statis — supaya perubahan UI
# langsung terlihat begitu file diubah (cukup refresh, tanpa cache lama).
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.jinja_env.auto_reload = True

# ─── Helper: Chrome ──────────────────────────────────────────────────────────

def get_chrome_path():
    env = os.environ.get('GAMA_BROWSER')
    if env and os.path.isfile(env):
        return env
    for p in CHROME_PATHS:
        if p and os.path.exists(p):
            return p
    return None


_chrome_major_cache: int | None = None

def _installed_chrome_major() -> int:
    """Deteksi versi major Chrome yang BENAR-BENAR terpasang (binary yang dipakai get_chrome_path()).
    UA yang di-generate wajib ikut versi asli ini -- kalau tidak, UA string akan mismatch
    dengan Client Hints (Sec-CH-UA) & kemampuan render sesungguhnya, yang bikin situs seperti
    Facebook curiga/salah negosiasi fitur lalu gagal render video & gambar (kotak hitam/blank)."""
    global _chrome_major_cache
    if _chrome_major_cache is not None:
        return _chrome_major_cache
    major = 130  # fallback kalau deteksi gagal
    chrome_path = get_chrome_path()
    if chrome_path:
        # Chrome di Windows tidak bisa dicek lewat `chrome.exe --version` (proses baru,
        # tidak print ke stdout secara sinkron) -- baca dari nama folder versi yang
        # Chrome taruh sendiri di sebelah chrome.exe, mis. Application\150.0.7871.186\
        try:
            app_dir = os.path.dirname(chrome_path)
            for entry in os.listdir(app_dir):
                m = re.match(r'^(\d+)\.\d+\.\d+\.\d+$', entry)
                if m and os.path.isdir(os.path.join(app_dir, entry)):
                    major = int(m.group(1))
                    break
        except Exception:
            pass
    _chrome_major_cache = major
    return major


def generate_user_agent():
    """UA Chrome asli, format frozen (persis navigator.userAgent Chrome modern),
    dengan major version mengikuti Chrome yang benar-benar terpasang di mesin ini."""
    major = _installed_chrome_major()
    return (
        f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        f'AppleWebKit/537.36 (KHTML, like Gecko) '
        f'Chrome/{major}.0.0.0 Safari/537.36'
    )

# ─── Helper: Metadata ────────────────────────────────────────────────────────

_meta_cache: dict | None = None
_meta_cache_time: float = 0.0
_META_CACHE_TTL = 2.0  # detik

def load_meta() -> dict:
    """Load per-profile metadata (tags, notes, last launched). Di-cache selama 2 detik."""
    global _meta_cache, _meta_cache_time
    now = time.monotonic()
    if _meta_cache is not None and (now - _meta_cache_time) < _META_CACHE_TTL:
        return _meta_cache
    with _meta_lock:
        try:
            with open(META_FILE, 'r', encoding='utf-8') as f:
                _meta_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _meta_cache = {}
        _meta_cache_time = time.monotonic()
        return _meta_cache


def _invalidate_meta_cache():
    """Invalidasi cache meta — dipanggil setelah save."""
    global _meta_cache_time
    _meta_cache_time = 0.0


def save_meta(meta: dict):
    global _meta_cache, _meta_cache_time
    with _meta_lock:
        tmp = META_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, META_FILE)
        _meta_cache = meta
        _meta_cache_time = time.monotonic()

# ─── Helper: Profil ──────────────────────────────────────────────────────────

_profile_dirs_cache: list | None = None
_profile_dirs_cache_time: float = 0.0
_PROFILE_DIRS_TTL = 3.0  # detik

def list_profile_dirs() -> list[str]:
    """Kembalikan daftar nama profil (nama folder di PROFILES_DIR). Di-cache 3 detik."""
    global _profile_dirs_cache, _profile_dirs_cache_time
    now = time.monotonic()
    if _profile_dirs_cache is not None and (now - _profile_dirs_cache_time) < _PROFILE_DIRS_TTL:
        return _profile_dirs_cache
    if not os.path.isdir(PROFILES_DIR):
        return []
    try:
        with os.scandir(PROFILES_DIR) as entries:
            _profile_dirs_cache = sorted(e.name for e in entries if e.is_dir())
    except Exception:
        _profile_dirs_cache = []
    _profile_dirs_cache_time = now
    return _profile_dirs_cache



def _invalidate_profile_dirs_cache():
    """Invalidasi cache profil — dipanggil setelah tambah/hapus profil."""
    global _profile_dirs_cache_time
    _profile_dirs_cache_time = 0.0

# ─── Routes: halaman utama ───────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/sites')
def get_sites():
    return jsonify({
        'sites': SITES,
        'custom': load_custom_sites(),
        'category_order': SITE_CATEGORY_ORDER,
        'custom_categories': load_custom_categories(),
    })


@app.route('/api/sites/categories', methods=['POST'])
def add_custom_category():
    """Tambah kategori shortcut custom buatan user."""
    data = request.get_json() or {}
    label = (data.get('label') or '').strip()
    if not label:
        return jsonify({'error': 'Nama kategori wajib diisi.'})
    # Buat id dari label — huruf kecil, spasi jadi underscore
    cat_id = re.sub(r'[^a-z0-9_]', '', label.lower().replace(' ', '_'))
    if not cat_id:
        return jsonify({'error': 'Nama kategori tidak valid (gunakan huruf/angka).'})
    if cat_id in BUILTIN_CATEGORIES:
        return jsonify({'error': f'Nama "{label}" sudah dipakai kategori bawaan.'})
    cats = load_custom_categories()
    if any(c.get('id') == cat_id for c in cats):
        return jsonify({'error': f'Kategori "{label}" sudah ada.'})
    icon = (data.get('icon') or 'fa-solid fa-folder').strip()
    cats.append({'id': cat_id, 'label': label, 'icon': icon})
    if not save_custom_categories(cats):
        return jsonify({'error': 'Gagal menyimpan kategori.'})
    return jsonify({'success': True, 'custom_categories': cats})


@app.route('/api/sites/categories', methods=['DELETE'])
def remove_custom_category():
    """Hapus kategori custom user (beserta semua URL di kategori itu)."""
    data = request.get_json() or {}
    cat_id = (data.get('id') or '').strip()
    if not cat_id or cat_id in BUILTIN_CATEGORIES:
        return jsonify({'error': 'Kategori tidak valid atau tidak bisa dihapus.'})
    cats = [c for c in load_custom_categories() if c.get('id') != cat_id]
    # Hapus juga semua URL custom yang pakai kategori ini
    sites = [s for s in load_custom_sites() if s.get('category') != cat_id]
    save_custom_categories(cats)
    save_custom_sites(sites)
    return jsonify({'success': True, 'custom_categories': cats, 'custom': sites})


@app.route('/api/sites/custom', methods=['POST'])
def add_custom_site():
    """Tambah shortcut URL custom (GLOBAL — muncul di semua kartu profil)."""
    data = request.get_json() or {}
    label = (data.get('label') or '').strip()
    url = (data.get('url') or '').strip()
    category = (data.get('category') or 'lainnya').strip()
    # Validasi kategori — hanya yang dikenal
    if category not in ('sosmed', 'ai', 'lainnya'):
        category = 'lainnya'
    if not label or not url:
        return jsonify({'error': 'Label dan URL wajib diisi.'})
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return jsonify({'error': 'URL harus diawali http:// atau https://'})
    url = url.rstrip('/')
    custom = load_custom_sites()
    for s in custom:
        if s.get('url', '').rstrip('/').lower() == url.lower():
            return jsonify({'error': 'URL sudah terdaftar.', 'custom': custom})
    custom.append({'label': label, 'url': url, 'category': category})
    if not save_custom_sites(custom):
        return jsonify({'error': 'Gagal menyimpan shortcut.'})
    return jsonify({'success': True, 'custom': custom})


@app.route('/api/sites/custom', methods=['DELETE'])
def remove_custom_site():
    """Hapus shortcut URL custom (GLOBAL)."""
    data = request.get_json() or {}
    url = (data.get('url') or '').strip().rstrip('/')
    if not url:
        return jsonify({'error': 'URL wajib diisi.'})
    custom = [s for s in load_custom_sites() if s.get('url', '').rstrip('/').lower() != url.lower()]
    if not save_custom_sites(custom):
        return jsonify({'error': 'Gagal menyimpan perubahan.'})
    return jsonify({'success': True, 'custom': custom})


@app.route('/api/profiles')
def get_profiles():
    if not os.path.isdir(PROFILES_DIR):
        return jsonify({'error': 'Profiles directory not found.'})
    meta = load_meta()
    profiles = []
    groups_set = set()
    for name in list_profile_dirs():
        m = meta.get(name, {})
        groups = m.get('_groups', [])
        for g in groups:
            groups_set.add(g)
        posting_labels = m.get('posting_labels')
        if posting_labels is None:
            legacy_label = m.get('posting_label') or m.get('facebook_label') or ''
            posting_labels = [legacy_label] if legacy_label else []
        profiles.append({
            'name':          name,
            'user_agent':    m.get('user_agent', ''),
            'fingerprint':   m.get('fingerprint') or {},
            'proxy':         m.get('proxy', ''),
            'tags':          m.get('tags', []),
            'group':         m.get('group', ''),       # legacy single-group
            'groups':        groups,
            'note':          m.get('note', ''),
            'posting_labels': posting_labels,
            'posting_label': ', '.join(posting_labels),
            'facebook_label': m.get('facebook_label', ''),
            'password':      m.get('password', ''),
            'has_password':  bool(m.get('password', '')),
            'channels':      m.get('channels', []),
            'fb_pages':      m.get('fb_pages', []),
            'fb_groups':     m.get('fb_groups', []),
            'last_launched': m.get('last_launched', ''),
        })
    # Kumpulkan semua grup yang pernah ada (dari _groups_list di meta root)
    all_groups = meta.get('__groups__', sorted(groups_set))
    return jsonify({'profiles': profiles, 'groups': all_groups})

# ─── Routes: tag ─────────────────────────────────────────────────────────────

@app.route('/api/profiles/<profile>/tags', methods=['POST', 'DELETE'])
def manage_tags(profile):
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profile not found.'})
    data = request.get_json() or {}
    tag = (data.get('tags') or data.get('tag') or '').strip()
    if not tag:
        return jsonify({'error': 'Tag is required.'})
    meta = load_meta()
    m = meta.setdefault(profile, {})
    tags = m.get('tags', [])
    if request.method == 'POST':
        if tag not in tags:
            tags.append(tag)
    else:  # DELETE
        tags = [t for t in tags if t != tag]
    m['tags'] = tags
    save_meta(meta)
    return jsonify({'success': True, 'tags': tags})

# ─── Routes: grup ────────────────────────────────────────────────────────────

@app.route('/api/groups', methods=['GET', 'POST', 'DELETE'])
def manage_groups():
    meta = load_meta()
    all_groups: list = meta.get('__groups__', [])
    if request.method == 'GET':
        return jsonify({'success': True, 'groups': all_groups})

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Nama grup wajib diisi.'})

    if request.method == 'POST':
        if name in all_groups:
            return jsonify({'error': f"Grup '{name}' sudah ada."})
        all_groups.append(name)
        meta['__groups__'] = sorted(all_groups)
        save_meta(meta)
        return jsonify({'success': True, 'groups': meta['__groups__']})
    else:  # DELETE
        all_groups = [g for g in all_groups if g != name]
        meta['__groups__'] = all_groups
        # Lepas semua profil dari grup yang dihapus
        for pname, pm in meta.items():
            if pname.startswith('__'):
                continue
            if isinstance(pm, dict):
                pm['_groups'] = [g for g in pm.get('_groups', []) if g != name]
        save_meta(meta)
        return jsonify({'success': True, 'groups': all_groups})


@app.route('/api/profiles/<profile>/group', methods=['POST', 'DELETE'])
def set_group(profile):
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    data = request.get_json() or {}
    group = (data.get('group') or '').strip()
    meta = load_meta()
    all_groups = meta.get('__groups__', [])
    m = meta.setdefault(profile, {})
    groups = m.get('_groups', [])

    if request.method == 'POST':
        if group and group not in all_groups:
            return jsonify({'error': f"Grup '{group}' tidak ada."})
        if group and group not in groups:
            groups.append(group)
    else:  # DELETE
        groups = [g for g in groups if g != group]

    m['_groups'] = groups
    save_meta(meta)
    return jsonify({'success': True, 'groups': groups})

# ─── Routes: user-agent & note ───────────────────────────────────────────────

@app.route('/api/profiles/<profile>/regen_ua', methods=['POST'])
def regen_user_agent(profile):
    """Acak ulang FINGERPRINT profil.

    Dulu fungsi ini hanya memanggil generate_user_agent(), yang isinya konstan
    (Chrome + Windows + versi Chrome terpasang) — jadi menekan tombolnya tidak
    pernah mengubah apa pun. Terbukti: 5 kali klik menghasilkan 1 UA yang sama.

    User agent memang SENGAJA tetap jujur — mengaku Firefox/macOS padahal
    menjalankan Chrome di Windows justru membuat deteksi lebih mudah. Yang
    diacak adalah hal yang memang berbeda antar komputer nyata: resolusi,
    bahasa, zona waktu, jumlah core, RAM, dan kartu grafis.
    """
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    import gama_fingerprint as _fp
    ua = generate_user_agent()
    fp = _fp.generate_fingerprint(ua)
    meta = load_meta()
    m = meta.setdefault(profile, {})
    m['user_agent'] = ua
    m['fingerprint'] = fp
    save_meta(meta)
    return jsonify({'success': True, 'user_agent': ua,
                    'fingerprint': fp, 'ringkas': _fp.ringkas(fp)})


@app.route('/api/profiles/<profile>/proxy', methods=['POST'])
def set_proxy(profile):
    """Set proxy per profil — faktor terbesar untuk memisahkan akun.

    20 akun dari satu IP jauh lebih mencurigakan daripada 20 akun dengan
    fingerprint browser yang mirip.
    """
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    import gama_fingerprint as _fp
    nilai = ((request.get_json() or {}).get('proxy') or '').strip()

    if nilai and not _fp.urai_proxy(nilai).get('host'):
        return jsonify({'error': f'Format proxy tidak dikenali: {nilai}'})

    meta = load_meta()
    meta.setdefault(profile, {})['proxy'] = nilai
    save_meta(meta)
    # Kredensial ditangani ekstensi per-profil, jadi user:pass@ tetap didukung.
    return jsonify({'success': True, 'proxy': nilai,
                    'berkredensial': bool(_fp.urai_proxy(nilai).get('user'))})


@app.route('/api/profiles/proxy_massal', methods=['POST'])
def set_proxy_massal():
    """Bagikan daftar proxy ke banyak profil sekaligus.

    Untuk 175 profil, mengisi satu per satu tidak masuk akal. User menempel
    daftar proxy (satu per baris) lalu dibagikan berurutan: baris ke-1 ke
    profil ke-1, dan seterusnya.

    mode 'urut'  : 1 proxy untuk 1 profil (default — inilah yang benar, karena
                   dua profil ber-IP sama tetap terlihat sebagai satu orang).
    mode 'putar' : daftar diulang kalau profil lebih banyak dari proxy.
                   Disediakan untuk yang proxy-nya terbatas, tapi dilaporkan
                   berapa profil yang jadi berbagi IP supaya tidak tanpa sadar.
    """
    import gama_fingerprint as _fp
    data = request.get_json() or {}
    daftar = [b.strip() for b in (data.get('daftar') or '').splitlines() if b.strip()]
    profil = [p for p in (data.get('profiles') or []) if p in list_profile_dirs()]
    mode = (data.get('mode') or 'urut').strip()

    if not profil:
        return jsonify({'error': 'Tidak ada profil yang dipilih.'})
    if not daftar:
        return jsonify({'error': 'Daftar proxy kosong.'})

    salah = [b for b in daftar if not _fp.urai_proxy(b).get('host')]
    if salah:
        return jsonify({'error': f'{len(salah)} baris tidak dikenali, contoh: {salah[0]}'})

    if mode == 'urut' and len(daftar) < len(profil):
        return jsonify({'error': (
            f'Proxy hanya {len(daftar)} untuk {len(profil)} profil. Tambah proxy, '
            f'kurangi profil yang dipilih, atau pakai mode "putar" '
            f'(beberapa profil akan berbagi IP).')})

    meta = load_meta()
    for i, nama in enumerate(profil):
        meta.setdefault(nama, {})['proxy'] = daftar[i % len(daftar)]
    save_meta(meta)

    berbagi = max(0, len(profil) - len(daftar))
    return jsonify({
        'success': True,
        'terisi': len(profil),
        'berbagi_ip': berbagi,
        'pesan': (f'{len(profil)} profil terisi.'
                  + (f' {berbagi} di antaranya berbagi IP dengan profil lain.'
                     if berbagi else ' Semua dapat IP sendiri.')),
    })


def _uji_satu_proxy(baris: str, timeout: int = 12) -> dict:
    """Uji satu proxy lewat requests (bukan Chrome), lalu laporkan IP keluarnya."""
    import requests
    import gama_fingerprint as _fp
    from urllib.parse import quote
    px = _fp.urai_proxy(baris)
    if not px.get('host'):
        return {'proxy': baris, 'ok': False, 'pesan': 'format tidak dikenali'}
    # Sandi sering mengandung @ / : — tanpa di-encode, URL-nya terurai salah
    # dan proxy yang sebenarnya sehat dilaporkan gagal.
    kredensial = (f"{quote(px['user'], safe='')}:{quote(px['pass'], safe='')}@"
                  if px.get('user') else '')
    url = f"{px['scheme']}://{kredensial}{px['host']}:{px['port']}"
    try:
        r = requests.get('https://api.ipify.org?format=json',
                         proxies={'http': url, 'https': url}, timeout=timeout)
        ip = (r.json() or {}).get('ip', '?')
        return {'proxy': baris, 'ok': True, 'ip': ip, 'pesan': f'hidup — IP {ip}'}
    except Exception as e:
        # Pesan mentah requests ("HTTPSConnectionPool(host='api.ipify.org')...
        # Max retries exceeded") menyebut host TUJUAN, bukan proxy-nya, jadi
        # terbaca seolah api.ipify.org yang bermasalah. Diterjemahkan supaya
        # yang tampil adalah keadaan PROXY-nya.
        asli = str(e)
        r = asli.lower()
        if '407' in asli or 'proxyauthentication' in r or 'auth' in r:
            pesan = 'ditolak — user/password salah'
        elif 'timed out' in r or 'timeout' in r:
            pesan = 'tidak merespons (mati / IP Anda belum di-whitelist)'
        elif 'forcibly closed' in r or 'connection aborted' in r or 'reset' in r:
            pesan = 'koneksi diputus proxy'
        elif 'refused' in r:
            pesan = 'koneksi ditolak (port tertutup)'
        elif 'certificate' in r or 'ssl' in r:
            pesan = 'gagal SSL lewat proxy ini'
        elif 'max retries' in r or 'proxyerror' in r or 'cannot connect' in r:
            pesan = 'tidak bisa dihubungi'
        elif 'name or service' in r or 'getaddrinfo' in r:
            pesan = 'alamat proxy tidak ditemukan'
        else:
            pesan = asli[:110]
        return {'proxy': baris, 'ok': False, 'pesan': pesan}


def _uji_proxy_paralel(daftar, timeout: int = 12, pekerja: int = 20) -> list:
    """Uji banyak proxy sekaligus.

    Berurutan, 60 proxy x 12 detik bisa memakan 12 menit dan request HTTP-nya
    keburu menggantung. Paralel membuatnya hitungan puluhan detik.

    Proxy yang sama hanya diuji SEKALI lalu hasilnya dipakai ulang — pada 175
    profil yang berbagi beberapa proxy, ini memangkas pengujian secara drastis.
    Urutan hasil dijaga persis sama dengan urutan masukan supaya pemetaan ke
    profil tidak bergeser.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not daftar:
        return []
    unik = list(dict.fromkeys(daftar))
    with ThreadPoolExecutor(max_workers=max(1, min(pekerja, len(unik)))) as ex:
        hasil_unik = list(ex.map(lambda b: _uji_satu_proxy(b, timeout), unik))
    peta = {h['proxy']: h for h in hasil_unik}
    return [dict(peta[b]) for b in daftar]


@app.route('/api/profiles/tes_proxy', methods=['POST'])
def tes_proxy():
    """Cek proxy benar-benar hidup dan IP-nya apa."""
    data = request.get_json() or {}
    daftar = [b.strip() for b in (data.get('daftar') or '').splitlines() if b.strip()]
    if not daftar:
        nama = (data.get('profile') or '').strip()
        px = (load_meta().get(nama) or {}).get('proxy') or ''
        if not px:
            return jsonify({'error': 'Profil ini belum punya proxy.'})
        daftar = [px]

    hasil = _uji_proxy_paralel(daftar[:200], timeout=int(data.get('timeout') or 12))
    hidup = sum(1 for h in hasil if h['ok'])
    ip_unik = len({h.get('ip') for h in hasil if h.get('ip')})
    return jsonify({'success': True, 'hasil': hasil, 'hidup': hidup,
                    'total': len(hasil), 'ip_unik': ip_unik})


@app.route('/api/profiles/cek_proxy_terpasang', methods=['POST'])
def cek_proxy_terpasang():
    """Cek proxy yang SUDAH TERPASANG di profil — bukan daftar tempelan.

    Beda dengan 'Tes Proxy' yang menguji teks di kotak, ini membaca proxy milik
    tiap profil lalu melaporkannya per profil, sehingga ketahuan profil mana
    yang proxy-nya sudah mati dan mana yang berbagi IP.
    """
    data = request.get_json() or {}
    meta = load_meta()
    semua = list_profile_dirs()
    pilih = [p for p in (data.get('profiles') or []) if p in semua] or semua

    pasangan = [(n, ((meta.get(n) or {}).get('proxy') or '').strip()) for n in pilih]
    terpasang = [(n, p) for n, p in pasangan if p]
    tanpa = [n for n, p in pasangan if not p]
    if not terpasang:
        return jsonify({'error': 'Belum ada profil yang punya proxy terpasang.'})

    hasil = _uji_proxy_paralel([p for _, p in terpasang],
                               timeout=int(data.get('timeout') or 12))
    baris = [dict(h, profile=n) for (n, _), h in zip(terpasang, hasil)]

    hidup = [b for b in baris if b['ok']]
    mati = [b for b in baris if not b['ok']]
    # IP yang dipakai lebih dari satu profil = profil-profil itu terlihat
    # sebagai satu orang, jadi perlu ditonjolkan.
    hitung_ip = {}
    for b in hidup:
        if b.get('ip'):
            hitung_ip[b['ip']] = hitung_ip.get(b['ip'], 0) + 1
    berbagi = {ip: n for ip, n in hitung_ip.items() if n > 1}

    return jsonify({
        'success': True,
        'baris': baris,
        'total': len(baris),
        'hidup': len(hidup),
        'mati': len(mati),
        'ip_unik': len(hitung_ip),
        'ip_berbagi': berbagi,
        'tanpa_proxy': tanpa,
        'profil_mati': [b['profile'] for b in mati],
        'pesan': (f'{len(hidup)} hidup, {len(mati)} mati dari {len(baris)} profil berproxy'
                  + (f'; {len(tanpa)} profil belum punya proxy' if tanpa else '')
                  + (f'; {len(berbagi)} IP dipakai lebih dari satu profil' if berbagi else '')),
    })


@app.route('/api/profiles/ganti_proxy_mati', methods=['POST'])
def ganti_proxy_mati():
    """Ganti proxy profil yang mati dengan kandidat SEHAT dari daftar baru.

    Kandidat diuji dulu; yang mati tidak pernah dipasang — percuma menukar
    proxy mati dengan proxy mati lain. Kandidat yang sudah terpakai profil lain
    juga dilewati supaya tidak diam-diam membuat dua profil berbagi IP.
    """
    data = request.get_json() or {}
    semua = list_profile_dirs()
    target = [p for p in (data.get('profiles') or []) if p in semua]
    kandidat = [b.strip() for b in (data.get('daftar') or '').splitlines() if b.strip()]
    if not target:
        return jsonify({'error': 'Tidak ada profil bermasalah yang dipilih.'})
    if not kandidat:
        return jsonify({'error': 'Daftar proxy pengganti kosong.'})

    meta = load_meta()
    terpakai = {((meta.get(n) or {}).get('proxy') or '').strip()
                for n in semua if n not in target}
    kandidat = [k for k in dict.fromkeys(kandidat) if k not in terpakai]
    if not kandidat:
        return jsonify({'error': 'Semua proxy pengganti sudah dipakai profil lain.'})

    hasil = _uji_proxy_paralel(kandidat, timeout=int(data.get('timeout') or 12))
    sehat = [h['proxy'] for h in hasil if h['ok']]
    if not sehat:
        return jsonify({'error': (f'Tidak ada proxy pengganti yang hidup '
                                  f'(diuji {len(hasil)}). Tidak ada yang diubah.')})

    diganti, dilewati = [], []
    for i, nama in enumerate(target):
        if i < len(sehat):
            meta.setdefault(nama, {})['proxy'] = sehat[i]
            diganti.append({'profile': nama, 'proxy': sehat[i]})
        else:
            dilewati.append(nama)
    save_meta(meta)

    return jsonify({
        'success': True,
        'diganti': diganti,
        'dilewati': dilewati,
        'sehat': len(sehat),
        'diuji': len(hasil),
        'pesan': (f'{len(diganti)} profil dapat proxy baru yang sudah terbukti hidup.'
                  + (f' {len(dilewati)} profil belum kebagian karena proxy sehat habis.'
                     if dilewati else '')),
    })


@app.route('/api/profiles/<profile>/note', methods=['POST'])
def set_note(profile):
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profile not found.'})
    data = request.get_json() or {}
    note = (data.get('note') or '').strip()
    meta = load_meta()
    meta.setdefault(profile, {})['note'] = note
    save_meta(meta)
    return jsonify({'success': True})


@app.route('/api/profiles/<profile>/posting_labels', methods=['POST', 'DELETE'])
@app.route('/api/profiles/<profile>/posting_label', methods=['POST', 'DELETE'])
@app.route('/api/profiles/<profile>/facebook_label', methods=['POST', 'DELETE'])
def manage_posting_labels(profile):
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profile not found.'})
    data = request.get_json() or {}
    label = ' '.join(str(
        data.get('posting_label') or data.get('facebook_label') or data.get('label') or ''
    ).strip().split())[:60]
    meta = load_meta()
    m = meta.setdefault(profile, {})
    labels = m.get('posting_labels')
    if labels is None:
        legacy_label = m.get('posting_label') or m.get('facebook_label') or ''
        labels = [legacy_label] if legacy_label else []

    if request.method == 'POST':
        if not label:
            return jsonify({'error': 'Label wajib diisi.'})
        if label not in labels:
            labels.append(label)
    else:  # DELETE
        labels = [l for l in labels if l != label]

    m['posting_labels'] = labels
    m['posting_label'] = ', '.join(labels)
    m['facebook_label'] = m['posting_label']
    save_meta(meta)
    return jsonify({
        'success': True,
        'posting_labels': labels,
        'posting_label': m['posting_label'],
        'facebook_label': m['facebook_label'],
    })


@app.route('/api/profiles/<profile>/password', methods=['POST', 'DELETE'])
def set_profile_password(profile):
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profile not found.'})
    meta = load_meta()
    m = meta.setdefault(profile, {})
    if request.method == 'DELETE':
        m['password'] = ''
        save_meta(meta)
        return jsonify({'success': True, 'password': '', 'has_password': False})
    data = request.get_json() or {}
    password = str(data.get('password') or '')
    m['password'] = password
    save_meta(meta)
    return jsonify({'success': True, 'password': password, 'has_password': bool(password)})

# ─── Routes: tambah / hapus profil ───────────────────────────────────────────

@app.route('/api/profiles/add', methods=['POST'])
def add_profile():
    """Create a new isolated Chrome profile for a Gmail account and open the
    Google login page so the user can sign in once."""
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    tag = (data.get('tag') or '').strip()
    password = str(data.get('password') or '')
    auto_login = bool(data.get('auto_login', True))

    if not email:
        return jsonify({'error': 'Email wajib diisi.'})
    if not email.endswith('@gmail.com'):
        if '@' not in email:
            return jsonify({'error': 'Format email tidak valid.'})
    if not re.match(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', email):
        return jsonify({'error': 'Format email tidak valid.'})

    safe_name = email.replace('@', '_').replace('.', '_') + '.com' if '_gmail_com' not in email.replace('@','_').replace('.','_') else email.replace('@','_').replace('.','_')
    safe_name = email.replace('@', '_')   # e.g. user_gmail.com
    profile_path = os.path.join(PROFILES_DIR, safe_name)

    if os.path.exists(profile_path):
        return jsonify({'error': f'Profil {safe_name} sudah ada.'})

    try:
        os.makedirs(profile_path, exist_ok=True)
    except OSError as e:
        return jsonify({'error': f'Gagal membuat folder profil: {e}'})

    ua = generate_user_agent()
    import gama_fingerprint as _fp
    meta = load_meta()
    meta[safe_name] = {
        'user_agent':    ua,
        # Fingerprint dibuat sekali saat profil lahir, lalu tetap.
        'fingerprint':   _fp.generate_fingerprint(ua),
        'proxy':         '',
        'tags':          [tag] if tag else [],
        '_groups':       [],
        'note':          '',
        'posting_labels': [],
        'posting_label': '',
        'facebook_label': '',
        'password':      password,
        'channels':      [],
        'last_launched': '',
    }
    save_meta(meta)
    _invalidate_profile_dirs_cache()

    launched = False
    if auto_login:
        chrome = get_chrome_path()
        if chrome:
            try:
                import gama_identitas as _id
                LOGIN_GOOGLE = 'https://accounts.google.com'
                args, port = _id.argumen_chrome(meta[safe_name]['fingerprint'], '')
                pakai = _id.tersedia()
                subprocess.Popen([
                    chrome,
                    f'--user-data-dir={profile_path}',
                    f'--user-agent={ua}',
                    '--no-first-run',
                    '--no-default-browser-check',
                    *args,
                    'about:blank' if pakai else LOGIN_GOOGLE,
                ])
                if pakai:
                    _id.mulai_penjaga(
                        port, meta[safe_name]['fingerprint'], '', LOGIN_GOOGLE,
                        saat_gagal=lambda _p: subprocess.Popen(
                            [chrome, f'--user-data-dir={profile_path}', LOGIN_GOOGLE]))
                meta = load_meta()
                meta[safe_name]['last_launched'] = datetime.now().isoformat(timespec='seconds')
                save_meta(meta)
                launched = True
            except Exception:
                pass

    return jsonify({
        'success': True,
        'launched': launched,
        'profile': {
            'name':          safe_name,
            'user_agent':    ua,
            'tags':          [tag] if tag else [],
            'groups':        [],
            'note':          '',
            'posting_labels': [],
            'posting_label': '',
            'facebook_label': '',
            'password':      password,
            'has_password':  bool(password),
            'channels':      [],
            'last_launched': meta[safe_name].get('last_launched', ''),
        },
    })


@app.route('/api/profiles/<profile>', methods=['DELETE'])
def delete_profile(profile):
    """Permanently delete a profile folder and its metadata."""
    if profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    profile_path = os.path.join(PROFILES_DIR, profile)
    if not profile_path.startswith(PROFILES_DIR):
        return jsonify({'error': 'Path profil tidak valid.'})
    try:
        shutil.rmtree(profile_path)
    except OSError as e:
        return jsonify({'error': (
            'Gagal menghapus. Kemungkinan profil sedang dibuka di Chrome - '
            f'tutup jendela Chrome-nya dulu lalu coba lagi. ({e})'
        )})
    meta = load_meta()
    meta.pop(profile, None)
    save_meta(meta)
    _invalidate_profile_dirs_cache()
    return jsonify({'success': True})

# ─── Routes: launch shortcut ─────────────────────────────────────────────────

def _launch_profile_url(profile_name: str, url: str, label: str = ''):
    if not profile_name or profile_name not in list_profile_dirs():
        return {'error': 'Profile not found.'}

    chrome_path = get_chrome_path()
    if not chrome_path:
        return {'error': 'Google Chrome executable not found.'}
    if not url or not re.match(r'^https?://', url, re.IGNORECASE):
        return {'error': 'URL tidak valid.'}

    meta = load_meta()
    m = meta.setdefault(profile_name, {})
    ua = m.get('user_agent') or ''
    if f'Chrome/{_installed_chrome_major()}.' not in ua:
        # UA tersimpan kosong/stale (mis. Chrome sudah auto-update sejak profil dibuat) ->
        # regenerate supaya selalu match versi Chrome yang benar-benar dipakai.
        ua = generate_user_agent()
    m['user_agent'] = ua

    # Fingerprint per profil: dibuat sekali lalu DIPAKAI TERUS.
    # Fingerprint yang berubah-ubah tiap kali profil dibuka justru
    # mencurigakan — komputer sungguhan tidak berganti resolusi, jumlah core,
    # dan kartu grafis setiap kali browser dijalankan.
    import gama_fingerprint as _fp
    if not m.get('fingerprint'):
        m['fingerprint'] = _fp.generate_fingerprint(ua)
    m['fingerprint']['user_agent'] = ua

    profile_path = os.path.join(PROFILES_DIR, profile_name)
    proxy = m.get('proxy') or ''
    import gama_identitas as _id
    args, port = _id.argumen_chrome(m['fingerprint'], proxy)

    # Kalau penjaga bisa jalan, Chrome dibuka di about:blank dan penjaga yang
    # menavigasi — supaya halaman tujuan lahir SETELAH penyamaran terpasang.
    # Kalau tidak bisa, URL tetap dibuka langsung: lebih baik profil jalan
    # tanpa penyamaran daripada user dapat tab kosong.
    pakai_penjaga = _id.tersedia()

    def _buka_langsung(pesan=''):
        """Jaring pengaman: buka URL-nya tanpa penyamaran.

        Chrome yang dipanggil lagi dengan --user-data-dir yang sama tidak
        membuka jendela baru, melainkan tab baru di jendela yang sudah ada —
        jadi user tidak terdampar di about:blank kalau penjaga gagal.
        """
        if pesan:
            print(f'[identitas] {profile_name}: {pesan}')
        try:
            subprocess.Popen([chrome_path, f'--user-data-dir={profile_path}', url])
        except Exception:
            pass

    try:
        subprocess.Popen([
            chrome_path,
            f'--user-data-dir={profile_path}',
            f'--user-agent={ua}',
            '--no-first-run',
            '--no-default-browser-check',
            *args,
            'about:blank' if pakai_penjaga else url,
        ])
        if pakai_penjaga:
            _id.mulai_penjaga(port, m['fingerprint'], proxy, url,
                              saat_gagal=_buka_langsung)
    except Exception as e:
        return {'error': str(e)}

    m['last_launched'] = datetime.now().isoformat(timespec='seconds')
    save_meta(meta)
    return {'success': True, 'message': f'Launched {profile_name} -> {label or url}'}


@app.route('/api/launch', methods=['POST'])
def launch_profile():
    data = request.get_json() or {}
    profile_name = (data.get('profile') or '').strip()
    target_site  = (data.get('site') or '').strip().lower()
    direct_url   = (data.get('url') or '').strip()

    if not profile_name:
        return jsonify({'error': 'Profile wajib diisi.'})
    # Shortcut custom (URL langsung) — dilempar dari UI kartu profil.
    if direct_url:
        label = (data.get('label') or 'Shortcut').strip() or 'Shortcut'
        return jsonify(_launch_profile_url(profile_name, direct_url, label))
    if not target_site:
        return jsonify({'error': 'Profile and site are required.'})
    site = SITES.get(target_site)
    if not site:
        return jsonify({'error': 'Unknown site.'})
    return jsonify(_launch_profile_url(profile_name, site['url'], site['label']))


@app.route('/api/riset/open', methods=['POST'])
def riset_open():
    data = request.get_json() or {}
    profile_name = (data.get('profile') or '').strip()
    url = (data.get('url') or '').strip()
    label = (data.get('label') or 'Riset').strip()
    return jsonify(_launch_profile_url(profile_name, url, label))

# ─── Helper: YT Uploader engine ──────────────────────────────────────────────

def yt_uploader_available():
    """Cek apakah engine YT Uploader siap dipakai."""
    if not os.path.isdir(YT_UPLOADER_DIR):
        return False, f'Folder YT Uploader tidak ditemukan: {YT_UPLOADER_DIR}'
    svc = os.path.join(YT_UPLOADER_DIR, 'services', 'browser_upload_service.py')
    if not os.path.isfile(svc):
        return False, f'File browser_upload_service.py tidak ditemukan di {YT_UPLOADER_DIR}'
    return True, ''


def tiktok_available():
    if not os.path.isdir(TEKOTOK_DIR):
        return False, f'Folder TEKOTOK tidak ditemukan: {TEKOTOK_DIR}'
    uploader = os.path.join(TEKOTOK_DIR, 'app', 'core', 'uploader.py')
    if not os.path.isfile(uploader):
        return False, f'File uploader.py tidak ditemukan di {TEKOTOK_DIR}'
    return True, ''


@app.route('/api/youtube/available')
def youtube_available():
    ok_yt, reason_yt = yt_uploader_available()
    ok_tt, reason_tt = tiktok_available()
    platforms = {
        'youtube':   {'available': ok_yt, 'reason': reason_yt},
        'tiktok':    {'available': ok_tt, 'reason': reason_tt},
        'facebook':  {'available': True,  'reason': ''},
        'instagram': {'available': True,  'reason': ''},
        'pinterest': {'available': True,  'reason': ''},
        'threads':   {'available': True,  'reason': ''},
        'x':         {'available': True,  'reason': ''},
    }
    return jsonify({'youtube': ok_yt, 'available': ok_yt, 'reason': reason_yt, 'platforms': platforms})

# ─── Helper: start worker subprocess ─────────────────────────────────────────

def _start_worker(bridge_script: str, job: dict, console: bool = False, graceful_stop: bool = False) -> str:
    """Tulis job + status file, jalankan worker subprocess, catat di registry.
    console=True: worker punya jendela CMD sendiri yang start MINIMIZED (dipakai
    Shopee/ADB supaya semua adb.exe render di satu console, bisa di-minimize,
    tidak flashing).
    graceful_stop=True: worker BISA cek `job['stop_path']` sendiri (mis. di
    tengah loop download banyak item) dan berhenti RAPI (proses hasil yang
    sudah didapat lalu keluar normal) alih-alih di-kill paksa. Kalau True,
    `stop_job()` di bawah tulis file flag ini dan TIDAK langsung terminate —
    biar worker sempat menyelesaikan & menyimpan hasil parsial-nya sendiri."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_id = 'job_' + datetime.now().strftime('%Y%m%d%H%M%S') + '_' + os.urandom(3).hex()
    status_path = os.path.join(JOBS_DIR, job_id + '.status.json')
    job_path    = os.path.join(JOBS_DIR, job_id + '.job.json')
    stop_path   = os.path.join(JOBS_DIR, job_id + '.stop.flag') if graceful_stop else ''

    # Status awal
    with open(status_path, 'w', encoding='utf-8') as f:
        json.dump({'message': 'Antre...', 'done': False, 'success': False, 'error': '', 'pct': 0}, f)

    job['status_path'] = status_path
    if stop_path:
        job['stop_path'] = stop_path
    with open(job_path, 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False)

    popen_kw = {'cwd': BASE_DIR}
    if console and os.name == 'nt':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE — tampil minimized, tak curi fokus
        popen_kw['startupinfo'] = si
        popen_kw['creationflags'] = subprocess.CREATE_NEW_CONSOLE
    if getattr(sys, 'frozen', False):
        # Frozen (PyInstaller): tiap worker dikompilasi jadi .exe sendiri yang
        # duduk satu folder dengan GAMA.exe (lihat gama.spec) — tidak ada lagi
        # python.exe/skrip .py lepas di build, jadi panggil exe-nya langsung.
        worker_exe = os.path.join(BASE_DIR, os.path.splitext(bridge_script)[0] + '.exe')
        cmd = [worker_exe, job_path]
    else:
        # Worker python: pakai python.exe (bukan pythonw) supaya console muncul
        # saat console=True; child adb.exe akan menempel ke console ini (tanpa flashing).
        exe = sys.executable
        if console and exe.lower().endswith('pythonw.exe'):
            cand = os.path.join(os.path.dirname(exe), 'python.exe')
            if os.path.isfile(cand):
                exe = cand
        cmd = [exe, os.path.join(BASE_DIR, bridge_script), job_path]
    proc = subprocess.Popen(cmd, **popen_kw)

    with _yt_jobs_lock:
        _yt_jobs[job_id] = {
            'proc':        proc,
            'status_path': status_path,
            'stop_path':   stop_path,
            'job_path':    job_path,
            'profile':     job.get('profile', ''),
            'title':       job.get('title', ''),
        }
    return job_id

# ─── CSV helpers ─────────────────────────────────────────────────────────────

def _excel_serial_to_datetime(serial):
    import datetime as dt
    base = dt.datetime(1899, 12, 30)
    return base + dt.timedelta(days=float(serial))


def normalize_schedule_date(val):
    if not val:
        return ''
    val = str(val).strip()
    # Terima berbagai format umum; hari-duluan (gaya Indonesia/Excel) diprioritaskan
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y',
                '%Y/%m/%d', '%d/%m/%y', '%m/%d/%Y'):
        try:
            return datetime.strptime(val, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    try:
        d = _excel_serial_to_datetime(val)
        return d.strftime('%Y-%m-%d')
    except Exception:
        pass
    return ''


def normalize_schedule_time(val):
    if not val:
        return ''
    val = str(val).strip()
    # HH:MM, H.MM, HH:MM:SS, dst.
    m = re.search(r'\b(\d{1,2})[:.](\d{1,2})(?::\d{1,2})?\b', val)
    if m:
        h, mnt = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mnt <= 59:
            return f'{h:02d}:{mnt:02d}'
        return ''
    # Pecahan hari ala Excel (0.25 = 06:00)
    try:
        frac = float(val)
        if 0 <= frac < 1:
            total = round(frac * 24 * 60)
            return f'{total // 60:02d}:{total % 60:02d}'
    except ValueError:
        pass
    return ''


def normalize_schedule_parts(date_val, time_val):
    d = normalize_schedule_date(date_val)
    t = normalize_schedule_time(time_val)
    return d, t


def _read_csv_rows(csv_path: str) -> list[dict]:
    import csv
    for sep in (';', ',', '\t', '|'):
        try:
            with open(csv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f, delimiter=sep)
                rows = list(reader)
            if rows and len(rows[0]) > 1:
                return rows
        except Exception:
            pass
    try:
        with open(csv_path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter=';')
            return list(reader)
    except Exception:
        raise ValueError('Gagal membaca CSV.')


def _pick_csv_value(row: dict, *keys) -> str:
    for k in keys:
        for rk in row:
            if rk and rk.strip().lower() == k.lower():
                v = row[rk]
                if v is not None:
                    return str(v).strip()
    return ''


def _fb_page_meta(profile: str, page_id: str) -> dict:
    want = str(page_id or '').strip().lower()
    if not profile or not want:
        return {}
    try:
        pages = load_meta().get(profile, {}).get('fb_pages', []) or []
    except Exception:
        pages = []
    for page in pages:
        pid = str((page or {}).get('id') or '').strip().lower()
        if pid == want:
            return page or {}
    return {}


def _validate_fb_page_id(page_id: str, label='Fanpage') -> str:
    pid = str(page_id or '').strip()
    if not pid:
        raise ValueError(f'{label} belum dipilih.')
    if not re.fullmatch(r'\d+', pid):
        raise ValueError(f'{label} harus berupa ID angka. Klik Deteksi Fanpage lalu pilih dari dropdown.')
    return pid


def parse_tiktok_csv_rows(csv_path: str) -> list[dict]:
    """Parse CSV batch TikTok baris-per-baris secara TOLERAN: satu baris yang
    bermasalah (path video salah, caption kosong, jadwal tidak valid) ditandai
    lewat kunci '_error' TAPI TIDAK menjatuhkan seluruh batch - baris lain yang
    valid tetap diproses. Sebelumnya satu baris rusak bikin exception dan
    preview/upload SELURUH file kosong, kelihatan seperti "CSV-nya kehapus"
    padahal file di disk aman-aman saja."""
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f'File CSV tidak ditemukan: {csv_path}')
    raw = _read_csv_rows(csv_path)
    if not raw:
        raise ValueError('CSV TikTok kosong atau tidak dikenali.')
    items = []
    for i, row in enumerate(raw):
        video_path    = _pick_csv_value(row, 'video_path', 'video', 'file')
        caption       = _pick_csv_value(row, 'caption', 'title', 'description')
        hashtags      = _pick_csv_value(row, 'hashtags', 'tags', 'hashtag')
        schedule_date = _pick_csv_value(row, 'schedule_date', 'date')
        schedule_time = _pick_csv_value(row, 'schedule_time', 'time')
        product_name  = _pick_csv_value(row, 'product_name')
        product_id    = _pick_csv_value(row, 'product_id')
        location      = (_pick_csv_value(row, 'location') or '').strip()
        music         = (_pick_csv_value(row, 'music', 'musik', 'sound', 'suara', 'lagu') or '').strip()
        ai_generate_content = _csv_truthy(_pick_csv_value(row, 'ai_generate_content', 'ai_generate', 'ai_generated'))

        error = ''
        if not video_path:
            error = 'video_path wajib diisi.'
        elif not os.path.isfile(video_path):
            error = f'File video tidak ditemukan: {video_path}'
        elif not caption:
            error = 'caption wajib diisi.'

        d, t = normalize_schedule_parts(schedule_date, schedule_time)
        if not error and (schedule_date or schedule_time) and not (d and t):
            error = (f'Jadwal tidak valid ("{schedule_date} {schedule_time}"). '
                     'Pakai tanggal YYYY-MM-DD / DD/MM/YYYY dan jam HH:MM.')

        items.append({
            'video_path':           video_path,
            'caption':              caption,
            'hashtags':             hashtags,
            'schedule_date':        d,
            'schedule_time':        t,
            'product_name':         product_name,
            'product_id':           product_id,
            'location':             location,
            'music':                music,
            'ai_generate_content':  ai_generate_content,
            'date':                 d,
            'time':                 t,
            '_row':                 i + 1,
            '_error':               error,
        })
    return items


@app.route('/api/tiktok/csv_preview', methods=['POST'])
def tiktok_csv_preview():
    """Pratinjau CSV TikTok. Sama seperti Facebook, rutenya belum pernah ada
    padahal UI sudah memanggilnya — akibatnya tabel pratinjau TikTok selalu
    kosong dan kolom Status/Musik per baris tidak pernah kelihatan."""
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    try:
        return jsonify({'success': True, 'rows': parse_tiktok_csv_rows(csv_path)})
    except (ValueError, FileNotFoundError) as e:
        return jsonify({'error': str(e)})


# ─── Routes: YouTube channels ────────────────────────────────────────────────

@app.route('/api/youtube/channels', methods=['POST'])
def youtube_channels():
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})

    cached_only = bool(data.get('cached_only', False))
    headless = bool(data.get('headless', False))
    if cached_only:
        meta = load_meta()
        channels = meta.get(profile, {}).get('channels', [])
        return jsonify({'success': True, 'channels': channels})

    profile_path = os.path.join(PROFILES_DIR, profile)
    job = {
        'mode':         'list_channels',
        'profile_path': profile_path,
        'profile':      profile,
        'title':        '(deteksi channel)',
        'headless':     headless,
        'invisible':    headless,
    }
    job_id = _start_worker('yt_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/youtube/channels/save', methods=['POST'])
def youtube_channels_save():
    """Simpan hasil deteksi channel ke metadata profil."""
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    channels = data.get('channels', [])
    meta = load_meta()
    meta.setdefault(profile, {})['channels'] = channels
    save_meta(meta)
    return jsonify({'success': True})

# ─── Routes: YouTube CSV template & preview ──────────────────────────────────

CSV_TEMPLATE_YT = (
    'VIDEO PATH;TITLE;DESCRIPTION;TAGS;THUMBNAIL;DATE;TIME;AI CONTENT;MADE FOR KIDS\n'
    r'D:\Videos\video1.mp4;Judul Video 1;Deskripsi video pertama;tag1,tag2,tag3;D:\Images\thumb.jpg;;;0;0'
)

@app.route('/api/youtube/csv_template')
def youtube_csv_template():
    return Response(
        CSV_TEMPLATE_YT,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=template_upload_youtube.csv'},
    )


def _csv_truthy(val: str) -> bool:
    return (val or '').strip().lower() in ('1', 'true', 'yes', 'y', 'ya', 'iya', 'x', 'checked')


@app.route('/api/youtube/csv_preview', methods=['POST'])
def youtube_csv_preview():
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    try:
        raw = _read_csv_rows(csv_path)
    except Exception as e:
        return jsonify({'error': str(e)})
    rows = []
    for row in raw:
        # Preview LENGKAP jadi ceklis sebelum upload: selain judul/jadwal,
        # tampilkan juga apakah thumbnail-nya benar ada di disk (bukan cuma
        # path-nya kebaca), dan status AI-content/Made-for-Kids per baris —
        # supaya kesalahan (thumbnail salah path, lupa tandai AI content,
        # dsb) ketahuan SEBELUM upload jalan, bukan sesudah.
        thumb = _pick_csv_value(row, 'THUMBNAIL', 'thumbnail_path', 'thumbnail', 'thumb', 'image')
        rows.append({
            'video_path':    _pick_csv_value(row, 'VIDEO PATH', 'video_path', 'video', 'file'),
            'title':         _pick_csv_value(row, 'TITLE', 'title', 'judul'),
            'description':   _pick_csv_value(row, 'DESCRIPTION', 'description', 'deskripsi', 'desc'),
            'tags':          _pick_csv_value(row, 'TAGS', 'tags', 'tag', 'keywords'),
            'thumbnail_path': thumb,
            'thumbnail_ok':  bool(thumb) and os.path.isfile(thumb),
            'date':          normalize_schedule_date(_pick_csv_value(row, 'DATE', 'date')),
            'time':          normalize_schedule_time(_pick_csv_value(row, 'TIME', 'time')),
            'ai_content':    _csv_truthy(_pick_csv_value(row, 'AI CONTENT', 'ai_content', 'ai')),
            # Audience dikunci di seluruh GAMA: selalu "Not made for kids".
            # Kolom MADE FOR KIDS di CSV tidak lagi berpengaruh, jadi preview
            # menampilkan nilai yang benar-benar dipakai saat upload.
            'made_for_kids': False,
        })
    return jsonify({'rows': rows})

# ─── Routes: TikTok upload ───────────────────────────────────────────────────

CSV_TEMPLATE_TT_REGULAR = (
    'video_path;caption;hashtags;music;location;ai_generate_content;schedule_date;schedule_time\n'
    r'D:\Videos\tiktok1.mp4;Caption video pertama;#fyp #viral;Kita mulai dengan Bismillah;Jakarta, Indonesia;no;2026-08-01;08:00'
    '\n'
    r'D:\Videos\tiktok2.mp4;Caption video kedua;#viral;;;yes;2026-08-02;09:00'
)
CSV_TEMPLATE_TT_AFFILIATE = (
    'video_path;caption;hashtags;product_name;product_id;schedule_date;schedule_time\n'
    r'D:\Videos\tiktok1.mp4;Caption produk 1;fyp;Nama Produk;1234567890;;'
)


def _tiktok_upload(data: dict):
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})

    ok, reason = tiktok_available()
    if not ok:
        return jsonify({'error': reason})

    mode         = (data.get('mode') or 'single').lower()
    profile_path = os.path.join(PROFILES_DIR, profile)
    invisible    = bool(data.get('invisible', False))
    dry_run      = bool(data.get('dry_run', False))
    upload_type  = (data.get('upload_type') or 'regular').lower()
    delete_after = bool(data.get('delete_after', False))
    use_location = bool(data.get('use_location', False))
    ai_generate_content = bool(data.get('ai_generate_content', False))

    if mode == 'batch':
        csv_path = (data.get('csv_path') or '').strip()
        if not csv_path or not os.path.isfile(csv_path):
            return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
        try:
            rows = parse_tiktok_csv_rows(csv_path)
        except (ValueError, FileNotFoundError) as e:
            return jsonify({'error': str(e)})
        # filter baris yang dipilih
        selected = data.get('selected_rows')
        if selected is not None:
            rows = [rows[i] for i in selected if i < len(rows)]
        # Baris invalid (path video salah, caption kosong, dst) ditandai lewat
        # '_error' oleh parse_tiktok_csv_rows TAPI TIDAK menjatuhkan baris lain
        # yang valid - skip baris itu saja drpd batalkan seluruh batch.
        skipped = [r for r in rows if r.get('_error')]
        rows = [r for r in rows if not r.get('_error')]
        if not rows:
            reason = ('; '.join(f"Baris {r['_row']}: {r['_error']}" for r in skipped[:5])
                       if skipped else 'CSV TikTok kosong atau tidak dikenali.')
            return jsonify({'error': f'Tidak ada baris valid untuk diupload. {reason}'})
        # Terapkan flag global ke setiap row:
        # - use_location: jika False, hapus lokasi dari row agar tidak dikirim ke uploader
        # - ai_generate_content: jika True dari UI, override semua row jadi True
        for row in rows:
            if not use_location:
                row['location'] = ''
            if ai_generate_content:
                row['ai_generate_content'] = True
        job = {
            'platform':             'tiktok',
            'profile':              profile,
            'profile_path':         profile_path,
            'mode':                 'batch',
            'rows':                 rows,
            'invisible':            invisible,
            'dry_run':              dry_run,
            'upload_type':          upload_type,
            'delete_after':         delete_after,
            'title':                f'TikTok CSV ({len(rows)} video)',
            'skipped_rows':         [f"Baris {r['_row']}: {r['_error']}" for r in skipped],
        }
    else:
        video_path  = (data.get('video_path') or '').strip()
        caption     = (data.get('title') or data.get('caption') or '').strip()
        hashtags    = (data.get('tags') or data.get('hashtags') or '').strip()
        product_id  = (data.get('product_id') or '').strip()
        product_name = (data.get('product_name') or '').strip()
        date_str    = (data.get('date') or '').strip()
        time_str    = (data.get('time') or '').strip()
        music       = (data.get('music') or data.get('musik') or '').strip()
        # Dulu lokasi di mode single DIPAKSA kosong, jadi field lokasi apa pun
        # yang diisi user diam-diam hilang. TikTok hanya menerima tag lokasi
        # untuk tipe Regular, dan itu ditegakkan di sini juga — bukan cuma
        # disembunyikan di UI.
        location    = (data.get('location') or '').strip()
        if upload_type in ('affiliate', 'seller'):
            location = ''

        if not video_path:
            return jsonify({'error': 'File video tidak ditemukan: (kosong)'})
        if not os.path.isfile(video_path):
            return jsonify({'error': f'File video tidak ditemukan: {video_path}'})

        job = {
            'platform':             'tiktok',
            'profile':              profile,
            'profile_path':         profile_path,
            'mode':                 'single',
            'video_path':           video_path,
            'caption':              caption,
            'hashtags':             hashtags,
            'upload_type':          upload_type,
            'product_id':           product_id,
            'product_name':         product_name,
            'schedule_date':        date_str,
            'schedule_time':        time_str,
            'invisible':            invisible,
            'dry_run':              dry_run,
            'delete_after':         delete_after,
            'location':             location,
            'music':                music,
            'ai_generate_content':  ai_generate_content,
            'title':                caption[:60] if caption else os.path.basename(video_path),
        }

    job_id = _start_worker('tiktok_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


# ─── TikTok: unggah FOTO lewat WEB ───────────────────────────────────────────
#
# Dulu jalur ini lewat HP/ADB karena halaman upload dikira cuma menerima video.
# Itu keliru: TikTok Studio punya URL TERPISAH untuk foto,
# `?from=webapp&tab=photo`, yang input berkasnya
# accept="image/jpg,image/jpeg,image/png,image/webp" dengan multiple=true
# (sampai 35 foto). Menu pilihannya baru muncul kalau kursor DIARAHKAN ke
# tombol "+ Upload" tanpa diklik, dan tidak semua akun sudah kebagian
# (rollout per akun). Jalur ADB sudah dibuang seluruhnya.

def _foto_dari_kolom(nilai: str) -> list:
    """Terjemahkan kolom `photos` di CSV jadi daftar berkas.

    Menerima dua bentuk supaya user tidak perlu mengetik satu per satu:
      - path FOLDER  -> semua gambar di dalamnya, urut nama
      - beberapa path dipisah '|'  (bukan ';' karena itu pemisah kolom CSV)
    """
    teks = (nilai or '').strip()
    if not teks:
        return []
    EKST = ('.jpg', '.jpeg', '.png', '.webp')
    if os.path.isdir(teks):
        return [os.path.join(teks, f) for f in sorted(os.listdir(teks))
                if f.lower().endswith(EKST)]
    hasil = []
    for bagian in teks.split('|'):
        p = bagian.strip().strip('"')
        if p and os.path.isfile(p):
            hasil.append(p)
    return hasil


def parse_tiktok_foto_rows(csv_path: str) -> list[dict]:
    """Parse CSV foto TikTok. Sama tolerannya dengan versi video: baris yang
    bermasalah ditandai lewat '_error' tanpa menjatuhkan baris lain."""
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f'File CSV tidak ditemukan: {csv_path}')
    raw = _read_csv_rows(csv_path)
    if not raw:
        raise ValueError('CSV foto TikTok kosong atau tidak dikenali.')
    items = []
    for i, row in enumerate(raw):
        photos  = (_pick_csv_value(row, 'photos', 'photo', 'folder', 'gambar') or '').strip()
        caption = _pick_csv_value(row, 'caption', 'title', 'description')
        berkas  = _foto_dari_kolom(photos)

        error = ''
        if not photos:
            error = 'photos wajib diisi (path folder atau daftar berkas dipisah "|").'
        elif not berkas:
            error = f'Tidak ada gambar (.jpg/.png/.webp) yang terbaca di: {photos}'
        elif not caption:
            error = 'caption wajib diisi.'
        elif len(berkas) > 35:
            error = f'{len(berkas)} foto melebihi batas TikTok (maksimal 35).'

        schedule_date = _pick_csv_value(row, 'schedule_date', 'date')
        schedule_time = _pick_csv_value(row, 'schedule_time', 'time')
        d, t = normalize_schedule_parts(schedule_date, schedule_time)
        if not error and (schedule_date or schedule_time) and not (d and t):
            error = (f'Jadwal tidak valid ("{schedule_date} {schedule_time}"). '
                     'Pakai tanggal YYYY-MM-DD / DD/MM/YYYY dan jam HH:MM.')

        items.append({
            'photos':        photos,
            'caption':       caption,
            'hashtags':      _pick_csv_value(row, 'hashtags', 'tags', 'hashtag'),
            'music':         (_pick_csv_value(row, 'music', 'musik', 'sound', 'suara', 'lagu') or '').strip(),
            'location':      (_pick_csv_value(row, 'location') or '').strip(),
            'schedule_date': d,
            'schedule_time': t,
            'date':          d,
            'time':          t,
            'jumlah_foto':   len(berkas),
            '_row':          i + 1,
            '_error':        error,
        })
    return items


@app.route('/api/tiktok/foto/csv_preview', methods=['POST'])
def tiktok_foto_csv_preview():
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    try:
        return jsonify({'success': True, 'rows': parse_tiktok_foto_rows(csv_path)})
    except (ValueError, FileNotFoundError) as e:
        return jsonify({'error': str(e)})


@app.route('/api/tiktok/upload_foto', methods=['POST'])
def tiktok_upload_foto():
    """Unggah postingan multi-foto TikTok lewat WEB, pakai worker yang sama
    dengan upload video (tiktok_bridge.py) supaya profil, antrean job, dan
    pelaporan progres persis sama."""
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})

    ok, reason = tiktok_available()
    if not ok:
        return jsonify({'error': reason})

    # ── Mode SINGLE POST: daftar foto dikirim langsung, bukan lewat CSV ──
    # User memilih fotonya sendiri lalu mengisi caption/hashtag/musik/lokasi
    # secara manual. Jalurnya tetap memakai worker & job yang sama dengan CSV,
    # hanya barisnya dibentuk di sini.
    foto_langsung = data.get('photos')
    if foto_langsung:
        if isinstance(foto_langsung, str):
            foto_langsung = [foto_langsung]
        berkas = [p for p in (str(x).strip().strip('"') for x in foto_langsung) if p]
        hilang = [p for p in berkas if not os.path.isfile(p)]
        if hilang:
            return jsonify({'error': f'File foto tidak ditemukan: {hilang[0]}'})
        bukan_gambar = [p for p in berkas
                        if not p.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
        if bukan_gambar:
            return jsonify({'error': (f'Bukan berkas gambar: '
                                      f'{os.path.basename(bukan_gambar[0])}. '
                                      'Hanya .jpg/.jpeg/.png/.webp yang diterima.')})
        if not berkas:
            return jsonify({'error': 'Tidak ada foto yang bisa dibaca.'})
        if len(berkas) > 35:
            return jsonify({'error': f'{len(berkas)} foto melebihi batas TikTok '
                                     f'(maksimal 35 per postingan).'})
        caption = (data.get('caption') or '').strip()
        if not caption:
            return jsonify({'error': 'Caption wajib diisi.'})

        d, t = normalize_schedule_parts(data.get('schedule_date'),
                                        data.get('schedule_time'))
        if (data.get('schedule_date') or data.get('schedule_time')) and not (d and t):
            return jsonify({'error': 'Jadwal tidak valid. Isi tanggal DAN jam, '
                                     'atau kosongkan keduanya untuk posting sekarang.'})

        # Urutan foto DIPERTAHANKAN apa adanya — yang pertama jadi Cover.
        # Dipisah '|' karena itu bentuk yang sudah dimengerti foto_dari_kolom.
        rows = [{
            'photos':        '|'.join(berkas),
            'caption':       caption,
            'hashtags':      (data.get('hashtags') or '').strip(),
            'music':         (data.get('music') or '').strip(),
            'location':      (data.get('location') or '').strip(),
            'schedule_date': d,
            'schedule_time': t,
            'date':          d,
            'time':          t,
            'jumlah_foto':   len(berkas),
            '_row':          1,
            '_error':        '',
        }]
    else:
        csv_path = (data.get('csv_path') or '').strip()
        if not csv_path or not os.path.isfile(csv_path):
            return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
        try:
            rows = parse_tiktok_foto_rows(csv_path)
        except (ValueError, FileNotFoundError) as e:
            return jsonify({'error': str(e)})

        selected = data.get('selected_rows')
        if selected is not None:
            rows = [rows[i] for i in selected if i < len(rows)]

    skipped = [r for r in rows if r.get('_error')]
    rows = [r for r in rows if not r.get('_error')]
    if not rows:
        alasan = ('; '.join(f"Baris {r['_row']}: {r['_error']}" for r in skipped[:5])
                  if skipped else 'CSV foto TikTok kosong atau tidak dikenali.')
        return jsonify({'error': f'Tidak ada baris valid untuk diupload. {alasan}'})

    # Tidak ada saklar "gunakan lokasi" terpisah seperti di mode video: kolom
    # `location` di CSV itu sendiri yang jadi saklarnya (kosong = tanpa lokasi).
    simpan_draf = bool(data.get('draft', False))
    for row in rows:
        row['simpan_draf'] = simpan_draf

    job = {
        'platform':     'tiktok',
        'content':      'foto',
        'profile':      profile,
        'profile_path': os.path.join(PROFILES_DIR, profile),
        'mode':         'batch',
        'rows':         rows,
        'invisible':    bool(data.get('invisible', False)),
        'dry_run':      bool(data.get('dry_run', False)),
        'simpan_draf':  bool(data.get('draft', False)),
        'title':        f'TikTok foto ({len(rows)} postingan)',
        'skipped_rows': [f"Baris {r['_row']}: {r['_error']}" for r in skipped],
    }
    return jsonify({'success': True, 'job_id': _start_worker('tiktok_bridge.py', job)})


# Kolom `photos` diisi PATH FOLDER — satu folder = satu postingan, semua gambar
# di dalamnya diunggah urut nama. Ini bentuk yang paling praktis: user cukup
# menaruh foto per postingan di folder terpisah, tanpa menuliskan tiap berkas.
# Daftar berkas dipisah '|' tetap diterima sebagai alternatif, tapi TIDAK
# dicontohkan di template supaya tidak membingungkan.
CSV_TEMPLATE_TT_FOTO = (
    'photos;caption;hashtags;music;location;schedule_date;schedule_time\n'
    r'D:\Konten\postingan1;Caption pertama;fyp,viral;Kita mulai dengan Bismillah;Trans Studio Bandung;2026-08-15;10:00' + '\n'
    r'D:\Konten\postingan2;Caption kedua;promo,diskon;Ngeplak - Denny Caknan;;;' + '\n'
    r'D:\Konten\postingan3;Caption ketiga;fyp;;Alun-Alun Kota Subang;2026-08-16;19:30' + '\n'
)


@app.route('/api/tiktok/csv_template')
def tiktok_csv_template():
    """Template CSV upload TikTok (video). `type` menentukan regular vs
    affiliate/seller. Rutenya sudah lama dipanggil UI tapi belum pernah ada,
    jadi tombol Template CSV menghasilkan 404 saat GAMA dibuka di browser
    biasa — di aplikasi desktop tidak terasa karena memakai dialog Save native.
    """
    tipe = (request.args.get('type') or 'regular').strip().lower()
    tmpl = (CSV_TEMPLATE_TT_AFFILIATE if tipe in ('affiliate', 'seller')
            else CSV_TEMPLATE_TT_REGULAR)
    nama = 'affiliate' if tipe in ('affiliate', 'seller') else 'regular'
    return Response(
        tmpl,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=template_tiktok_{nama}.csv'},
    )


@app.route('/api/facebook/csv_template')
def facebook_csv_template():
    """Template CSV upload Facebook — pasangan dari /api/facebook/csv_preview."""
    return Response(
        CSV_TEMPLATE_FB,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=template_upload_facebook.csv'},
    )


@app.route('/api/tiktok/csv_template_foto')
def tiktok_csv_template_foto():
    """Dipakai fallback <a download> saat GAMA dibuka di browser biasa; di
    aplikasi desktop templatenya lewat dialog Save pywebview."""
    return Response(
        CSV_TEMPLATE_TT_FOTO,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=template_upload_tiktok_foto.csv'},
    )


@app.route('/api/facebook/pages', methods=['POST'])
def facebook_pages():
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})

    cached_only = bool(data.get('cached_only', False))
    headless = bool(data.get('headless', False))
    if cached_only:
        meta = load_meta()
        pages = meta.get(profile, {}).get('fb_pages', [])
        return jsonify({'success': True, 'pages': pages})

    profile_path = os.path.join(PROFILES_DIR, profile)
    job = {
        'platform':     'facebook',
        'mode':         'list_pages',
        'profile_path': profile_path,
        'profile':      profile,
        'title':        '(deteksi Fanpage)',
        'headless':     headless,
        'invisible':    headless,
    }
    job_id = _start_worker('facebook_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/facebook/pages/save', methods=['POST'])
def facebook_pages_save():
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    pages = data.get('pages', [])
    clean_pages = []
    seen = set()
    for page in pages or []:
        pid = str((page or {}).get('id') or '').strip()
        name = ' '.join(str((page or {}).get('name') or '').strip().split())
        if not pid or not name or not re.fullmatch(r'\d+', pid):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        clean_pages.append({'id': pid, 'name': name})
    meta = load_meta()
    meta.setdefault(profile, {})['fb_pages'] = clean_pages
    save_meta(meta)
    return jsonify({'success': True, 'pages': clean_pages})


# ─── Routes: Facebook Groups ─────────────────────────────────────────────────

@app.route('/api/facebook/groups', methods=['POST'])
def facebook_groups_detect():
    """Scrape daftar Facebook Group yang sudah di-join atau cari by keyword."""
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})

    cached_only = bool(data.get('cached_only', False))
    if cached_only:
        meta = load_meta()
        groups = meta.get(profile, {}).get('fb_groups', [])
        return jsonify({'success': True, 'groups': groups})

    keyword  = (data.get('keyword') or '').strip()
    headless = bool(data.get('headless', False))
    profile_path = os.path.join(PROFILES_DIR, profile)
    job = {
        'platform':     'facebook',
        'mode':         'list_groups',
        'profile_path': profile_path,
        'profile':      profile,
        'keyword':      keyword,
        'max_results':  int(data.get('max_results') or 100),
        'headless':     headless,
        'title':        f'(scrape grup{" - " + keyword if keyword else ""})',
    }
    job_id = _start_worker('facebook_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/facebook/groups/save', methods=['POST'])
def facebook_groups_save():
    """Simpan daftar Facebook Group ke profiles_meta.json."""
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    groups = data.get('groups', [])
    clean_groups = []
    seen = set()
    for g in groups or []:
        gid  = str((g or {}).get('id') or '').strip()
        name = ' '.join(str((g or {}).get('name') or '').strip().split())
        if not gid or not name:
            continue
        if gid in seen:
            continue
        seen.add(gid)
        clean_groups.append({'id': gid, 'name': name})
    meta = load_meta()
    meta.setdefault(profile, {})['fb_groups'] = clean_groups
    save_meta(meta)
    return jsonify({'success': True, 'groups': clean_groups})


@app.route('/api/facebook/groups/join', methods=['POST'])
def facebook_groups_join():
    """Auto join daftar Facebook Group."""
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    group_ids = data.get('group_ids') or []
    if not group_ids:
        return jsonify({'error': 'Pilih minimal 1 grup untuk di-join.'})
    invisible  = bool(data.get('invisible', False))
    delay      = int(data.get('delay') or 10)
    profile_path = os.path.join(PROFILES_DIR, profile)
    job = {
        'platform':     'facebook',
        'mode':         'join_groups',
        'profile_path': profile_path,
        'profile':      profile,
        'group_ids':    group_ids,
        'invisible':    invisible,
        'delay':        delay,
        'title':        f'(auto join {len(group_ids)} grup)',
    }
    job_id = _start_worker('facebook_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/facebook/groups/csv_load', methods=['POST'])
def facebook_groups_csv_load():
    """Baca file CSV (mis. hasil Export CSV dari Scrape Grup) dan ambil daftar
    Group ID — dipakai untuk mengisi form Auto Join tanpa paste manual."""
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    try:
        rows = _read_csv_rows(csv_path)
    except Exception as e:
        return jsonify({'error': str(e)})
    groups = []
    seen = set()
    for r in rows:
        gid = _pick_csv_value(r, 'group_id', 'id', 'groupid')
        name = _pick_csv_value(r, 'group_name', 'name', 'nama')
        if not gid or gid in seen:
            continue
        seen.add(gid)
        groups.append({'id': gid, 'name': name})
    return jsonify({'success': True, 'groups': groups})


@app.route('/api/facebook/group/post', methods=['POST'])
def facebook_group_post():
    """Post teks/foto/video ke satu atau beberapa Facebook Group sekaligus."""
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    video_path = (data.get('video_path') or '').strip()
    if video_path and not os.path.isfile(video_path):
        return jsonify({'error': f'File media tidak ditemukan: {video_path}'})
    caption = (data.get('caption') or '').strip()
    if not caption and not video_path:
        return jsonify({'error': 'Caption atau file media wajib diisi.'})

    # Terima 'group_ids' (banyak grup) atau 'group_id' lama (satu grup) untuk kompatibilitas.
    raw_groups = data.get('group_ids')
    if not raw_groups:
        single_id = (data.get('group_id') or '').strip()
        raw_groups = [{'id': single_id, 'name': (data.get('group_name') or '').strip()}] if single_id else []
    groups = []
    seen = set()
    for g in raw_groups:
        if isinstance(g, dict):
            gid = str(g.get('id') or '').strip()
            gname = str(g.get('name') or '').strip()
        else:
            gid = str(g or '').strip()
            gname = ''
        if not gid or gid in seen:
            continue
        seen.add(gid)
        groups.append({'id': gid, 'name': gname})
    if not groups:
        return jsonify({'error': 'Ceklis atau isi minimal 1 Group ID.'})

    profile_path = os.path.join(PROFILES_DIR, profile)
    invisible = bool(data.get('invisible', False))
    post_delay = int(data.get('post_delay') or 0)

    if len(groups) == 1:
        job = {
            'platform':     'facebook',
            'mode':         'post_group',
            'profile_path': profile_path,
            'profile':      profile,
            'group_id':     groups[0]['id'],
            'group_name':   groups[0]['name'],
            'video_path':   video_path,
            'caption':      caption,
            'invisible':    invisible,
            'post_delay':   post_delay,
            'title':        f'Post ke Group {groups[0]["id"]}',
        }
    else:
        rows = [{
            'video_path': video_path, 'caption': caption,
            'group_id': g['id'], 'group_name': g['name'], 'post_delay': post_delay,
        } for g in groups]
        job = {
            'platform':     'facebook',
            'mode':         'post_group',
            'profile_path': profile_path,
            'profile':      profile,
            'rows':         rows,
            'invisible':    invisible,
            'post_delay':   post_delay,
            'title':        f'Post ke {len(groups)} Group',
        }
    job_id = _start_worker('facebook_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/facebook/groups/privacy', methods=['POST'])
def facebook_groups_privacy():
    """Cek privasi (privat/publik) sekumpulan grup sebelum posting."""
    data = request.get_json() or {}
    profile = (data.get('profile') or '').strip()
    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})
    group_ids = [str(g or '').strip() for g in (data.get('group_ids') or []) if str(g or '').strip()]
    if not group_ids:
        return jsonify({'error': 'Pilih minimal 1 grup untuk dicek.'})
    invisible = bool(data.get('invisible', False))
    profile_path = os.path.join(PROFILES_DIR, profile)
    job = {
        'platform':     'facebook',
        'mode':         'check_privacy',
        'profile_path': profile_path,
        'profile':      profile,
        'group_ids':    group_ids,
        'invisible':    invisible,
        'title':        f'(cek privasi {len(group_ids)} grup)',
    }
    job_id = _start_worker('facebook_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


CSV_TEMPLATE_IG = (
    'video_path;caption\n'
    r'D:\Videos\ig1.mp4;Caption Reels atau deskripsi foto'
)


def _facebook_csv_rows(raw: list) -> list[dict]:
    """Pemetaan kolom CSV Facebook. Dipakai bersama oleh upload batch dan
    pratinjau supaya keduanya tidak pernah beda tafsir kolom."""
    return [{
        'video_path': _pick_csv_value(r, 'video_path', 'video', 'file'),
        'caption':    _pick_csv_value(r, 'caption', 'title', 'description'),
        'date':       normalize_schedule_date(_pick_csv_value(r, 'schedule_date', 'date')),
        'time':       normalize_schedule_time(_pick_csv_value(r, 'schedule_time', 'time')),
        'link':       _pick_csv_value(r, 'link', 'url'),
        'page_id':    _pick_csv_value(r, 'page_id', 'fanpage_id', 'fb_page_id'),
        'page_name':  _pick_csv_value(r, 'page_name', 'fanpage_name', 'fb_page_name'),
        'group_id':   _pick_csv_value(r, 'group_id', 'fb_group_id'),
        'group_name': _pick_csv_value(r, 'group_name', 'fb_group_name'),
        'publish_mode': _pick_csv_value(r, 'publish_mode'),
        'post_delay':   _pick_csv_value(r, 'post_delay'),
    } for r in raw]


@app.route('/api/facebook/csv_preview', methods=['POST'])
def facebook_csv_preview():
    """Pratinjau CSV Facebook. Sudah dipanggil UI sejak lama tapi rutenya
    belum pernah ada, jadi tabel pratinjau selalu kosong."""
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    try:
        return jsonify({'success': True, 'rows': _facebook_csv_rows(_read_csv_rows(csv_path))})
    except Exception as e:
        return jsonify({'error': str(e)})

# Kolomnya mengikuti apa yang benar-benar dibaca oleh /api/facebook/upload
# mode batch. Isi page_id ATAU group_id, tidak perlu keduanya.
CSV_TEMPLATE_FB = (
    'video_path;caption;link;page_id;group_id;schedule_date;schedule_time\n'
    r'D:\Videos\fb1.mp4;Caption video pertama;https://toko.com/produk;123456789;;2026-08-01;08:00'
    '\n'
    r'D:\Videos\fb2.mp4;Caption video kedua;;;987654321;;'
)


@app.route('/api/instagram/csv_template')
def instagram_csv_template():
    return Response(
        CSV_TEMPLATE_IG,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=template_upload_instagram.csv'},
    )


@app.route('/api/instagram/csv_preview', methods=['POST'])
def instagram_csv_preview():
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    try:
        raw = _read_csv_rows(csv_path)
    except Exception as e:
        return jsonify({'error': str(e)})
    rows = [{
        'video_path': _pick_csv_value(r, 'video_path', 'video', 'file'),
        'caption':    _pick_csv_value(r, 'caption', 'title', 'description'),
        'date': '', 'time': '',
    } for r in raw]
    return jsonify({'rows': rows})

# ─── Routes: Social (Pinterest / Threads / X) ────────────────────────────────

CSV_TEMPLATE_SOCIAL = {
    'pinterest': 'video_path;title;caption;link;schedule_date;schedule_time\n' + r'D:\Videos\pin1.jpg;Judul Pin;Deskripsi pin;https://toko.com/produk;2026-08-01;08:00',
    'threads':   'video_path;caption\n' + r'D:\Videos\th1.mp4;Caption Threads (video opsional)',
    'x':         'video_path;caption;schedule_date;schedule_time\n' + r'D:\Videos\x1.mp4;Caption tweet;2026-08-01;08:00',
}


@app.route('/api/social/<platform>/csv_template')
def social_csv_template(platform):
    tmpl = CSV_TEMPLATE_SOCIAL.get(platform, '')
    return Response(
        tmpl,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=template_upload_{platform}.csv'},
    )


@app.route('/api/social/<platform>/csv_preview', methods=['POST'])
def social_csv_preview(platform):
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    try:
        raw = _read_csv_rows(csv_path)
    except Exception as e:
        return jsonify({'error': str(e)})
    rows = [{
        'video_path': _pick_csv_value(r, 'video_path', 'video', 'file'),
        'caption':    _pick_csv_value(r, 'caption', 'title', 'description'),
        'date':       normalize_schedule_date(_pick_csv_value(r, 'schedule_date', 'date')),
        'time':       normalize_schedule_time(_pick_csv_value(r, 'schedule_time', 'time')),
    } for r in raw]
    return jsonify({'rows': rows})

# ─── Routes: Upload (unified) ────────────────────────────────────────────────

@app.route('/api/youtube/upload', methods=['POST'])
def youtube_upload():
    data = request.get_json() or {}
    profile  = (data.get('profile') or '').strip()
    platform = (data.get('platform') or 'youtube').strip().lower()

    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Profil tidak ditemukan.'})

    # ── TikTok ──
    if platform == 'tiktok':
        return _tiktok_upload(data)

    # ── Facebook / Instagram / Pinterest / Threads / X ──
    if platform in ('facebook', 'instagram', 'pinterest', 'threads', 'x'):
        ok, reason = True, ''
        profile_path = os.path.join(PROFILES_DIR, profile)
        mode     = (data.get('mode') or 'single').lower()
        invisible = bool(data.get('invisible', False))

        if mode == 'batch':
            csv_path = (data.get('csv_path') or '').strip()
            if not csv_path or not os.path.isfile(csv_path):
                return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
            try:
                raw = _read_csv_rows(csv_path)
            except Exception as e:
                return jsonify({'error': str(e)})
            rows = _facebook_csv_rows(raw)
            selected = data.get('selected_rows')
            if selected is not None:
                picked_rows = []
                for i in selected:
                    try:
                        idx = int(i)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= idx < len(rows):
                        picked_rows.append(rows[idx])
                rows = picked_rows
            target = (data.get('target') or 'personal').strip().lower()
            page_id = (data.get('page_id') or '').strip()
            page_name = ' '.join(str(data.get('page_name') or '').strip().split())
            group_id = (data.get('group_id') or '').strip()
            group_name = ' '.join(str(data.get('group_name') or '').strip().split())

            if platform == 'facebook' and target == 'page':
                try:
                    page_id = _validate_fb_page_id(page_id)
                    cached = _fb_page_meta(profile, page_id)
                    if cached.get('name'):
                        page_name = ' '.join(str(cached.get('name') or '').strip().split())
                    for idx, row in enumerate(rows, start=1):
                        row_page_id = (row.get('page_id') or page_id or '').strip()
                        row['page_id'] = _validate_fb_page_id(row_page_id, f'Fanpage baris {idx}')
                        if not row.get('page_name'):
                            row_cached = _fb_page_meta(profile, row['page_id'])
                            row['page_name'] = ' '.join(str(row_cached.get('name') or page_name or '').strip().split())
                except ValueError as e:
                    return jsonify({'error': str(e)})

            if platform == 'facebook' and target == 'group':
                # Propagate group_id ke tiap row yang tidak punya group_id sendiri
                for row in rows:
                    if not row.get('group_id'):
                        row['group_id'] = group_id
                    if not row.get('group_name'):
                        row['group_name'] = group_name

            title_suffix = ''
            if platform == 'facebook':
                if target == 'page':
                    title_suffix = f' -> {page_name or page_id}'
                elif target == 'group':
                    title_suffix = f' -> Grup {group_name or group_id}'

            job = {
                'platform':     platform,
                'mode':         'batch' if target != 'group' else 'post_group',
                'profile_path': profile_path,
                'profile':      profile,
                'rows':         rows,
                'invisible':    invisible,
                'target':       target,
                'page_id':      page_id,
                'page_name':    page_name,
                'group_id':     group_id,
                'group_name':   group_name,
                'title':        f'{platform.title()} CSV ({len(rows)} item){title_suffix}',
                'publish_mode': (data.get('publish_mode') or 'schedule').lower(),
                'post_delay':   int(data.get('post_delay') or 0),
            }
        else:
            video_path = (data.get('video_path') or '').strip()
            caption    = (data.get('title') or data.get('caption') or '').strip()
            date_str   = (data.get('date') or '').strip()
            time_str   = (data.get('time') or '').strip()
            link       = (data.get('link') or '').strip()
            target     = (data.get('target') or 'personal').strip()
            page_id    = (data.get('page_id') or '').strip()
            page_name  = ' '.join(str(data.get('page_name') or '').strip().split())

            if platform == 'facebook' and target.lower() == 'page':
                try:
                    page_id = _validate_fb_page_id(page_id)
                except ValueError as e:
                    return jsonify({'error': str(e)})
                cached = _fb_page_meta(profile, page_id)
                if cached.get('name'):
                    page_name = ' '.join(str(cached.get('name') or '').strip().split())

            media_optional = platform in ('threads', 'x') or (platform == 'facebook' and target == 'group')
            if not video_path and not media_optional:
                return jsonify({'error': f'File video tidak ditemukan: (kosong)'})
            if video_path and not os.path.isfile(video_path):
                return jsonify({'error': f'File video tidak ditemukan: {video_path}'})

            group_id   = (data.get('group_id') or '').strip()
            group_name = ' '.join(str(data.get('group_name') or '').strip().split())

            job = {
                'platform':     platform,
                'mode':         'single',
                'profile_path': profile_path,
                'profile':      profile,
                'video_path':   video_path,
                'caption':      caption,
                'date':         date_str,
                'time':         time_str,
                'link':         link,
                'target':       target,
                'page_id':      page_id,
                'page_name':    page_name,
                'group_id':     group_id,
                'group_name':   group_name,
                'invisible':    invisible,
                'title':        caption[:60] if caption else os.path.basename(video_path or platform),
                'publish_mode': (data.get('publish_mode') or 'schedule').lower(),
                'post_delay':   int(data.get('post_delay') or 0),
            }
            # Group posting perlu mode=post_group agar dispatch ke run_post_group
            if platform == 'facebook' and target == 'group':
                job['mode'] = 'post_group'

        bridge = 'facebook_bridge.py' if platform == 'facebook' else \
                 'instagram_bridge.py' if platform == 'instagram' else \
                 'social_bridge.py'
        job_id = _start_worker(bridge, job)
        return jsonify({'success': True, 'job_id': job_id})

    # ── YouTube ──
    ok, reason = yt_uploader_available()
    if not ok:
        return jsonify({'error': reason})

    mode       = (data.get('mode') or 'single').lower()
    channel_id = (data.get('channel_id') or '').strip()
    invisible  = bool(data.get('invisible', False))
    profile_path = os.path.join(PROFILES_DIR, profile)

    if mode == 'batch':
        csv_path = (data.get('csv_path') or '').strip()
        if not csv_path or not os.path.isfile(csv_path):
            return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
        job = {
            'platform':     'youtube',
            'mode':         'batch',
            'profile_path': profile_path,
            'channel_id':   channel_id,
            'csv_path':     csv_path,
            'invisible':    invisible,
            'dry_run':      bool(data.get('dry_run', False)),
            'ai_content':   bool(data.get('ai_content', False)),
            # Force override dari checkbox "tampilkan di depan" (up-common-bar):
            # kalau dicentang, berlaku utk SEMUA baris CSV di batch ini,
            # menimpa nilai kolom AI CONTENT per baris.
            'force_ai_content':    bool(data.get('force_ai_content', False)),
            # Audience dikunci: GAMA selalu upload "Not made for kids",
            # apa pun isi kolom CSV atau permintaan dari UI.
            'force_made_for_kids': False,
            # short (vertikal ≤3mnt) vs long form; privasi default batch
            'yt_type':      (data.get('yt_type') or 'short').lower(),
            'privacy':      (data.get('privacy') or 'public').lower(),
            'title':        f'YouTube CSV',
            'profile':      profile,
        }
        selected = data.get('selected_rows')
        if selected is not None:
            job['selected_rows'] = selected
    else:
        video_path = (data.get('video_path') or '').strip()
        thumb      = (data.get('thumbnail_path') or '').strip()
        date_str   = (data.get('date') or '').strip()
        time_str   = (data.get('time') or '').strip()

        if not video_path:
            return jsonify({'error': 'File video tidak ditemukan: (kosong)'})
        if not os.path.isfile(video_path):
            return jsonify({'error': f'File video tidak ditemukan: {video_path}'})
        if thumb and not os.path.isfile(thumb):
            return jsonify({'error': f'File thumbnail tidak ditemukan: {thumb}'})

        job = {
            'platform':     'youtube',
            'mode':         'single',
            'profile_path': profile_path,
            'channel_id':   channel_id,
            'invisible':    invisible,
            'dry_run':      bool(data.get('dry_run', False)),
            'ai_content':   bool(data.get('ai_content', False)),
            # Audience dikunci: GAMA selalu upload "Not made for kids".
            'made_for_kids': False,
            'profile':      profile,
            'video_data': {
                'video_path':     video_path,
                'thumbnail_path': thumb,
                'title':          (data.get('title') or '').strip(),
                'description':    (data.get('description') or '').strip(),
                'tags':           (data.get('tags') or '').strip(),
                'privacy':        (data.get('privacy') or 'public').strip(),
                'date':           date_str,
                'time':           time_str,
            },
            'title': (data.get('title') or os.path.basename(video_path))[:60],
        }

    job_id = _start_worker('yt_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


# ─── OAuth API upload: DIHAPUS — hanya browser automation yang digunakan ──────
# Metode upload via YouTube Data API v3 (OAuth) telah dihapus.
# Semua upload menggunakan browser automation (yt_bridge.py).

@app.route('/api/youtube/oauth/status')
def youtube_oauth_status():
    return jsonify({'error': 'Fitur OAuth API upload tidak tersedia.', 'disabled': True})


@app.route('/api/youtube/oauth/login', methods=['POST'])
def youtube_oauth_login():
    return jsonify({'error': 'Fitur OAuth API upload tidak tersedia. Gunakan browser upload biasa.', 'disabled': True})


@app.route('/api/youtube/api_upload', methods=['POST'])
def youtube_api_upload():
    return jsonify({'error': 'Fitur OAuth API upload tidak tersedia. Gunakan browser upload biasa.', 'disabled': True})


#─── Routes: Job status ──────────────────────────────────────────────────────

@app.route('/api/youtube/job/<job_id>')
def youtube_job(job_id):
    with _yt_jobs_lock:
        info = _yt_jobs.get(job_id)

    status_path = (info or {}).get('status_path') or os.path.join(JOBS_DIR, job_id + '.status.json')

    if not os.path.isfile(status_path):
        return jsonify({'error': 'Job tidak ditemukan.', 'done': True, 'success': False})

    try:
        with open(status_path, 'r', encoding='utf-8') as f:
            status = json.load(f)
    except (json.JSONDecodeError, OSError):
        status = {'message': 'Memproses...', 'done': False, 'success': False}

    # Cek apakah proses/thread masih hidup
    if info and not status.get('done'):
        proc = info.get('proc')
        if proc:
            rc = proc.poll()
            if rc is not None and not status.get('done'):
                status.setdefault('done', True)
                status.setdefault('success', rc == 0)
                if rc != 0:
                    status['error'] = status.get('error') or f'Worker berhenti tak terduga (exit {rc})'
        else:
            thread = info.get('thread')
            if thread and not thread.is_alive() and not status.get('done'):
                status.setdefault('done', True)
                status.setdefault('success', False)
                status['error'] = status.get('error') or 'Thread worker berhenti tak terduga.'

    status['profile'] = (info or {}).get('profile', '')
    status['title']   = (info or {}).get('title', '')
    return jsonify(status)

# ─── Helper: ShoveBot ────────────────────────────────────────────────────────

def _run_text(cmd: list, cwd: str | None = None, timeout: int = 30) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return (p.stdout or b'').decode('utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return '__TIMEOUT__'
    except Exception as e:
        return f'__ERROR__ {e}'


def _shovebot_manifest() -> dict:
    """Baca manifest server ShoveBot (menunjuk install berlisensi yang di-register)."""
    for p in [
        r'C:\ProgramData\ShoveBot\server.json',
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ShoveBot', 'server.json'),
    ]:
        if os.path.isfile(p):
            try:
                with open(p, encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _shovebot_dir_layouts(base: str):
    """Semua kemungkinan lokasi ShoveBot.exe & ShoveBotServer.exe di bawah `base`
    (base bisa folder root install, folder dev, atau parent). Menutup layout
    installer, layout asli, dan layout dev (dist/)."""
    base = base.rstrip('\\/')
    bot_layouts = [
        ('ShoveBot.exe',),
        ('ShoveBot', 'ShoveBot.exe'),
        ('dist', 'ShoveBot', 'ShoveBot.exe'),
    ]
    srv_layouts = [
        ('ShoveBotServer.exe',),
        ('ShoveBotServer', 'ShoveBotServer.exe'),
        ('ShoveBot', 'ShoveBotServer.exe'),
        ('dist', 'ShoveBotServer', 'ShoveBotServer.exe'),
        ('dist', 'ShoveBot', 'ShoveBotServer.exe'),
    ]
    bots = [os.path.join(base, *p) for p in bot_layouts]
    servers = [os.path.join(base, *p) for p in srv_layouts]
    return bots, servers


def _shovebot_running_dirs() -> list:
    """Kalau ShoveBot/ShoveBotServer sedang berjalan, ambil folder exe-nya —
    cara paling pasti menemukan lokasi install."""
    dirs = []
    try:
        import psutil
    except ImportError:
        return dirs
    for proc in psutil.process_iter(['name', 'exe']):
        try:
            nm = (proc.info.get('name') or '').lower()
            if 'shovebot' in nm:
                exe = proc.info.get('exe')
                if exe and os.path.isfile(exe):
                    d = os.path.dirname(exe)
                    if d not in dirs:
                        dirs.append(d)
                        dirs.append(os.path.dirname(d))  # parent (root install)
        except Exception:
            pass
    return dirs


def _shovebot_registry_dirs() -> list:
    """Cari lokasi install ShoveBot dari registry Uninstall Windows (paling andal
    untuk aplikasi yang dipasang lewat installer)."""
    dirs = []
    try:
        import winreg
    except ImportError:
        return dirs
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
        (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
    ]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                    i += 1
                except OSError:
                    break
                try:
                    sk = winreg.OpenKey(key, sub)
                except OSError:
                    continue
                try:
                    name = ''
                    try:
                        name = winreg.QueryValueEx(sk, 'DisplayName')[0]
                    except OSError:
                        pass
                    if 'shovebot' in str(name).lower():
                        for val in ('InstallLocation', 'DisplayIcon', 'UninstallString'):
                            try:
                                loc = str(winreg.QueryValueEx(sk, val)[0] or '').strip().strip('"')
                            except OSError:
                                loc = ''
                            if loc:
                                d = loc if os.path.isdir(loc) else os.path.dirname(loc)
                                if d and d not in dirs:
                                    dirs.append(d)
                finally:
                    sk.Close()
        finally:
            key.Close()
    return dirs


def _detect_shovebot() -> dict:
    """Deteksi otomatis install ShoveBot BERLISENSI (bukan build dev tanpa lisensi).

    Cerdas menemukan ShoveBot di mana pun ter-install: proses yang sedang jalan →
    registry Windows → manifest server → SHOVEBOT_DIR → scan lokasi umum di semua
    drive. Semua base dir diperluas ke semua kemungkinan layout exe.
    """
    manifest = _shovebot_manifest()

    # Kumpulkan base dir kandidat (urut prioritas).
    base_dirs = []

    def add_base(d):
        if d and d not in base_dirs:
            base_dirs.append(d)

    # 1. Proses yang sedang berjalan (paling pasti)
    for d in _shovebot_running_dirs():
        add_base(d)
    # 2. Registry Uninstall Windows
    for d in _shovebot_registry_dirs():
        add_base(d)
    # 3. Manifest server ShoveBot
    if manifest.get('install_path'):
        add_base(manifest['install_path'])
        add_base(os.path.dirname(manifest['install_path']))
    # 4. SHOVEBOT_DIR (env / default)
    add_base(SHOVEBOT_DIR)
    # 5. Scan lokasi umum di semua drive
    import string
    scan_parents = []
    for drive in string.ascii_uppercase:
        root = f'{drive}:\\'
        if not os.path.isdir(root):
            continue
        scan_parents += [
            root,
            os.path.join(root, 'Program Files'),
            os.path.join(root, 'Program Files (x86)'),
            os.path.join(root, 'SHOPEE'),
            os.path.join(root, 'TOOLS'),
        ]
    scan_parents += [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs'),
        os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop'),
    ]
    for parent in scan_parents:
        try:
            entries = os.listdir(parent)
        except OSError:
            continue
        for name in entries:
            if 'shovebot' in name.lower():
                c = os.path.join(parent, name)
                if os.path.isdir(c):
                    add_base(c)

    # Perluas tiap base ke semua layout exe, ambil yang pertama ada.
    bot_candidates, server_candidates = [], []
    for base in base_dirs:
        b, s = _shovebot_dir_layouts(base)
        bot_candidates += b
        server_candidates += s

    bot_exe    = next((p for p in bot_candidates    if os.path.isfile(p)), None)
    server_exe = next((p for p in server_candidates if os.path.isfile(p)), None)
    server_py  = os.path.join(SHOVEBOT_DIR, 'server_main.py')


    # ADB — utamakan adb di dalam install ShoveBot yang TERDETEKSI (biar sesuai
    # versinya), lalu SHOVEBOT_ADB (env/default), lalu adb bundel GAMA.
    adb = None
    adb_candidates = []
    if bot_exe:
        bdir = os.path.dirname(bot_exe)
        root = os.path.dirname(bdir)  # parent (root install)
        for basep in (bdir, root):
            adb_candidates += [
                os.path.join(basep, '_internal', 'tools', 'platform-tools', 'adb.exe'),
                os.path.join(basep, 'tools', 'platform-tools', 'adb.exe'),
                os.path.join(basep, 'platform-tools', 'adb.exe'),
            ]
    adb_candidates += [SHOVEBOT_ADB, GAMA_ADB]
    adb = next((p for p in adb_candidates if p and os.path.isfile(p)), None)

    installed = bool(bot_exe)
    return {
        'installed':    installed,
        'bot_exe':      bot_exe,
        'server_exe':   server_exe,
        'server_py':    server_py if os.path.isfile(server_py) else None,
        'adb_path':     adb,
        'install_dir':  os.path.dirname(bot_exe) if bot_exe else None,
        'purchase_url': SHOVEBOT_PURCHASE_URL,
        'source':       'detected' if installed else 'none',
    }


def _adb_devices(adb: str) -> list[dict]:
    if not os.path.isfile(adb):
        return []
    out = _run_text([adb, 'devices'], timeout=8)
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith('list of devices'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({'serial': parts[0], 'state': parts[1]})
    return devices


def _shovebot_server(info: dict, action: str, timeout: int = 20) -> str:
    """Kelola Appium via ShoveBotServer.exe (install) atau server_main.py (dev)."""
    server_exe = info.get('server_exe')
    server_py  = info.get('server_py')
    if server_exe:
        return _run_text([server_exe, f'--{action}'], timeout=timeout)
    if server_py:
        return _run_text([sys.executable, server_py, f'--{action}'], timeout=timeout)
    return '__ERROR__ Runtime server ShoveBot tidak ditemukan'


def _parse_server_status(out: str) -> tuple[bool, str]:
    running = False
    url = ''
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('Status: RUNNING'):
            running = True
        if line.startswith('URL:'):
            url = line.split(':', 1)[1].strip()
    return running, url


@app.route('/api/shovebot/status')
def shovebot_status():
    info = _detect_shovebot()
    if not info['installed']:
        return jsonify({
            'installed':    False,
            'purchase_url': SHOVEBOT_PURCHASE_URL,
            'reason':       'ShoveBot belum terpasang di komputer ini. Fitur ini butuh aplikasi ShoveBot berlisensi. Silakan beli & pasang ShoveBot dulu.',
        })
    out = _shovebot_server(info, '--status', timeout=8)
    running, url = _parse_server_status(out)
    adb = info.get('adb_path')
    return jsonify({
        'installed':      True,
        'bot_exe':        info.get('bot_exe'),
        'source':         info.get('source'),
        'has_adb':        bool(adb),
        'server_running': running,
        'server_url':     url,
        'devices':        _adb_devices(adb) if adb else [],
        'purchase_url':   SHOVEBOT_PURCHASE_URL,
    })


@app.route('/api/shovebot/scan')
def shovebot_scan():
    info = _detect_shovebot()
    adb = info.get('adb_path')
    return jsonify({'installed': info['installed'], 'devices': _adb_devices(adb) if adb else []})


# ─── Routes: Lisensi (verifikasi offline Ed25519, per-fitur — gama_license.py) ─

@app.route('/api/license/status')
def license_status():
    """Status buka/kunci tiap fitur + revocation status."""
    from gama_license import get_feature_status, load_keys, verify_license_key
    features = get_feature_status()
    rev_sources = set()
    for key in load_keys():
        result = verify_license_key(key)
        src = result.get('revocation_source', '')
        if src:
            rev_sources.add(src)
    if 'online' in rev_sources:
        rev_status = 'online'
    elif 'cache' in rev_sources:
        rev_status = 'cached'
    elif 'offline_cached' in rev_sources:
        rev_status = 'offline_cached'
    else:
        rev_status = 'offline'
    return jsonify({'features': features, 'revocation_status': rev_status})


@app.route('/api/license/verify', methods=['POST'])
def license_verify():
    """Verifikasi license key GAMA2-; kalau valid, fitur terbuka otomatis."""
    from gama_license import add_key, get_feature_status
    data = request.get_json() or {}
    key = (data.get('license_key') or '').strip()
    result = add_key(key)
    return jsonify({
        'result': result,
        'features': get_feature_status(),
    })


@app.route('/api/license/transfer', methods=['POST'])
def license_transfer():
    """Pindahkan lisensi ke device ini (ditekan sendiri oleh user).

    Dipisah dari /verify supaya pengecekan rutin TIDAK PERNAH bisa menggusur
    device lain — pemindahan hanya terjadi lewat tindakan sadar user.
    """
    from gama_license import transfer_to_this_device, get_feature_status
    data = request.get_json() or {}
    key = (data.get('license_key') or '').strip()
    if not key:
        return jsonify({'success': False, 'message': 'License key kosong.'})
    hasil = transfer_to_this_device(key)
    hasil['features'] = get_feature_status()
    return jsonify(hasil)


@app.route('/api/license/keys')
def license_keys():
    """Daftar semua license key yang tersimpan (hanya prefix + akhir, bukan full key)."""
    from gama_license import load_keys
    keys = load_keys()
    # Sensor key: tampilkan 10 char awal + ... + 6 char akhir supaya aman
    def mask(k):
        return k[:10] + '...' + k[-6:] if len(k) > 16 else k
    return jsonify({'keys': [{'full': k, 'display': mask(k)} for k in keys]})


@app.route('/api/license/remove', methods=['POST'])
def license_remove():
    """Hapus satu license key dari penyimpanan lokal."""
    from gama_license import load_keys, save_keys, get_feature_status
    data = request.get_json() or {}
    key = (data.get('license_key') or '').strip()
    if not key:
        return jsonify({'error': 'license_key wajib diisi'}), 400
    keys = load_keys()
    if key not in keys:
        return jsonify({'error': 'Key tidak ditemukan'}), 404
    keys = [k for k in keys if k != key]
    save_keys(keys)
    return jsonify({
        'success': True,
        'features': get_feature_status(),
    })


# ─── Routes: Metadata Cleaner (diadopsi dari D:\TOOLS\METADATA CLEANER) ──────

@app.route('/api/metadata/presets')
def metadata_presets():
    """Daftar preset device (brand -> model) untuk dropdown spoofing metadata."""
    from metadata_cleaner_bridge import DEVICE_PRESETS
    return jsonify({'presets': DEVICE_PRESETS})


@app.route('/api/metadata/scan_folder', methods=['POST'])
def metadata_scan_folder():
    """Scan satu folder (non-rekursif) untuk file video/gambar yang didukung."""
    from metadata_cleaner_bridge import scan_folder
    data = request.get_json() or {}
    folder = (data.get('folder') or '').strip()
    if not folder or not os.path.isdir(folder):
        return jsonify({'error': f'Folder tidak ditemukan: {folder}'})
    files = scan_folder(folder)
    return jsonify({'files': files})


@app.route('/api/metadata/clean', methods=['POST'])
def metadata_clean():
    """Mulai job pembersih/pemalsu metadata (batch video/gambar)."""
    data = request.get_json() or {}
    files = [f for f in (data.get('files') or []) if isinstance(f, str) and f.strip()]
    if not files:
        return jsonify({'error': 'Belum ada file di antrean.'})
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        return jsonify({'error': f'{len(missing)} file tidak ditemukan (mungkin sudah dipindah/dihapus).'})
    output_dir = (data.get('output_dir') or '').strip()
    if not output_dir:
        return jsonify({'error': 'Folder output belum dipilih.'})
    options = data.get('options') or {}
    job = {
        'files': files,
        'output_dir': output_dir,
        'options': options,
        'title': f'Metadata Cleaner ({len(files)} file)',
    }
    job_id = _start_worker('metadata_cleaner_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/shovebot/server', methods=['POST'])
def shovebot_server_control():
    info = _detect_shovebot()
    if not info['installed']:
        return jsonify({'error': 'ShoveBot belum terpasang.'})
    data   = request.get_json() or {}
    action = (data.get('action') or '').strip().lower()
    if action not in ('start', 'stop'):
        return jsonify({'error': "action harus 'start' atau 'stop'."})
    out = _shovebot_server(info, action, timeout=30)
    if out.startswith('__TIMEOUT__'):
        return jsonify({'error': 'Server tidak merespons (timeout).'})
    if out.startswith('__ERROR__'):
        return jsonify({'error': out.replace('__ERROR__', '').strip()})
    # Re-cek status setelah aksi
    out2 = _shovebot_server(info, '--status', timeout=8)
    running, url = _parse_server_status(out2)
    return jsonify({'success': True, 'message': out.strip(), 'server_running': running, 'server_url': url})


@app.route('/api/shovebot/purchase', methods=['POST'])
def shovebot_purchase():
    """Buka halaman pembelian ShoveBot di browser default."""
    data = request.get_json() or {}
    url  = (data.get('url') or SHOVEBOT_PURCHASE_URL).strip()
    if not (url.startswith('http://') or url.startswith('https://')):
        url = SHOVEBOT_PURCHASE_URL
    try:
        webbrowser.open(url)
    except Exception as e:
        return jsonify({'error': f'Gagal membuka browser: {e}'})
    return jsonify({'success': True})


@app.route('/api/shovebot/launch', methods=['POST'])
def shovebot_launch():
    info = _detect_shovebot()
    if not info['installed']:
        return jsonify({'error': 'ShoveBot belum terpasang.', 'purchase_url': SHOVEBOT_PURCHASE_URL})
    try:
        subprocess.Popen([info['bot_exe']], cwd=os.path.dirname(info['bot_exe']))
    except Exception as e:
        return jsonify({'error': str(e)})
    return jsonify({'success': True})

# ─── OMNIVORA & RISET INTEGRATION ───────────────────────────────────────────

OMNIVORA_SETTINGS_FILE = os.path.join(DATA_DIR, 'omnivora_settings.json')
DOWNLOADS_DIR = os.path.join(BASE_DIR, 'downloads')
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def _safe_download_name(name: str, fallback: str = 'media') -> str:
    name = re.sub(r'[\/*?:"<>|]+', '_', str(name or '')).strip()
    name = re.sub(r'\s+', '_', name)
    return (name[:120] or fallback).strip('_')


def _download_target_dir(platform: str) -> str:
    path = os.path.join(DOWNLOADS_DIR, _safe_download_name(platform.lower() or 'misc', 'misc'))
    os.makedirs(path, exist_ok=True)
    return path


def _filename_from_url(url: str, content_type: str = '', fallback: str = 'media') -> str:
    parsed = urlparse(url)
    base = os.path.basename(unquote(parsed.path or ''))
    if not base or '.' not in base:
        ext = mimetypes.guess_extension((content_type or '').split(';')[0].strip()) or ''
        base = _safe_download_name(fallback, fallback) + ext
    return _safe_download_name(base, fallback)


def _save_direct_media(url: str, platform: str, media_type: str = 'auto') -> str:
    import requests
    resp = requests.get(url, timeout=60, stream=True, headers={'User-Agent': 'Mozilla/5.0'})
    resp.raise_for_status()
    content_type = resp.headers.get('Content-Type', '')
    if media_type == 'image' and 'image' not in content_type.lower():
        raise ValueError(f'URL bukan gambar langsung: {url}')
    if media_type == 'video' and 'video' not in content_type.lower():
        raise ValueError(f'URL bukan video langsung: {url}')
    folder = _download_target_dir(platform)
    filename = _filename_from_url(url, content_type, f'{platform}_download')
    save_path = os.path.join(folder, filename)
    base, ext = os.path.splitext(save_path)
    idx = 1
    while os.path.exists(save_path):
        save_path = f'{base}_{idx}{ext}'
        idx += 1
    with open(save_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=1024 * 128):
            if chunk:
                f.write(chunk)
    return save_path


def _detect_platform(url: str) -> str:
    """Deteksi platform dari link — untuk mode Auto (global, tanpa region)."""
    u = (url or '').lower()
    if 'shopee' in u:
        return 'shopee'
    if 'tiktok' in u:
        return 'tiktok'
    if 'youtu' in u:
        return 'youtube'
    if 'instagram' in u:
        return 'instagram'
    if 'facebook' in u or 'fb.watch' in u or 'fb.me' in u:
        return 'facebook'
    if 'pinterest' in u or 'pin.it' in u:
        return 'pinterest'
    if 'threads' in u:
        return 'threads'
    if 'twitter' in u or 'x.com' in u:
        return 'x'
    return 'generic'


def _get_ffmpeg() -> str:
    """Dapatkan path ffmpeg — pakai yang dibundel di assets/bin/, fallback ke PATH."""
    local = os.path.join(BASE_DIR, 'assets', 'bin', 'ffmpeg.exe')
    if os.path.isfile(local):
        return local
    import shutil as _sh
    found = _sh.which('ffmpeg')
    return found or 'ffmpeg'


def _render_slideshow_to_mp4(images: list, music_url: str, save_path: str,
                              durasi_per_foto: float = 3.0) -> str:
    """Render daftar foto (URL) + musik latar menjadi satu file .mp4.

    Langkah:
    1. Unduh setiap gambar ke folder temp.
    2. Buat concat.txt untuk ffmpeg concat demuxer.
    3. Gabungkan gambar dengan -loop 1 menjadi video slideshow.
    4. Mix musik latar (diunduh terpisah) ke dalam video.
    5. Hapus file temp, kembalikan path .mp4.
    """
    import requests
    import urllib3
    import tempfile
    import subprocess
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    ffmpeg = _get_ffmpeg()
    _UA_DL = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

    tmp_dir = tempfile.mkdtemp(prefix='gama_slide_')
    _NOWIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    try:
        # ── 1. Unduh gambar ────────────────────────────────────────────────
        img_paths = []
        for idx, img_url in enumerate(images):
            img_path = os.path.join(tmp_dir, f'slide_{idx:03d}.jpg')
            try:
                r = requests.get(img_url, headers={'User-Agent': _UA_DL},
                                 timeout=30, stream=True, verify=False)
                r.raise_for_status()
                with open(img_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1 << 17):
                        if chunk:
                            f.write(chunk)
                img_paths.append(img_path)
            except Exception as e:
                print(f'[SLIDESHOW] Gagal unduh gambar {idx}: {e}')

        if not img_paths:
            raise ValueError('Tidak ada gambar yang berhasil diunduh untuk slideshow.')

        # ── 2. Unduh musik latar ───────────────────────────────────────────
        music_path = ''
        if music_url:
            music_path = os.path.join(tmp_dir, 'music.mp3')
            try:
                r = requests.get(music_url, headers={'User-Agent': _UA_DL},
                                 timeout=30, stream=True, verify=False)
                r.raise_for_status()
                with open(music_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1 << 17):
                        if chunk:
                            f.write(chunk)
            except Exception as e:
                print(f'[SLIDESHOW] Gagal unduh musik: {e}')
                music_path = ''

        # ── 3. Buat concat.txt ─────────────────────────────────────────────
        # ffmpeg concat demuxer: tiap gambar ditampilkan selama durasi_per_foto detik
        concat_txt = os.path.join(tmp_dir, 'concat.txt')
        with open(concat_txt, 'w', encoding='utf-8') as f:
            for p in img_paths:
                f.write(f"file '{p.replace(chr(39), chr(39)+chr(39))}'\n")
                f.write(f'duration {durasi_per_foto}\n')
            # Tambahkan frame terakhir sekali lagi agar frame penutup ikut di-encode
            f.write(f"file '{img_paths[-1].replace(chr(39), chr(39)+chr(39))}'\n")

        total_durasi = len(img_paths) * durasi_per_foto

        # ── 4. Render dengan ffmpeg ────────────────────────────────────────
        # Target resolusi 9:16 (1080x1920) — format TikTok standar.
        # Gambar di-scale + pad agar tidak terpotong.
        vf = ('scale=1080:1920:force_original_aspect_ratio=decrease,'
              'pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,'
              'format=yuv420p')
        os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)

        if music_path and os.path.isfile(music_path):
            cmd = [
                ffmpeg, '-y',
                '-f', 'concat', '-safe', '0', '-i', concat_txt,
                '-i', music_path,
                '-vf', vf,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '128k',
                '-t', str(total_durasi),
                '-shortest',
                '-movflags', '+faststart',
                save_path,
            ]
        else:
            # Tanpa musik: video bisu
            cmd = [
                ffmpeg, '-y',
                '-f', 'concat', '-safe', '0', '-i', concat_txt,
                '-vf', vf,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-an',
                '-t', str(total_durasi),
                '-movflags', '+faststart',
                save_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True,
                                creationflags=_NOWIN)
        if result.returncode != 0:
            raise RuntimeError(
                f'FFmpeg slideshow gagal (rc={result.returncode}): {result.stderr[-400:]}'
            )

        return save_path

    finally:
        # Hapus folder temp (gambar + musik sementara)
        try:
            import shutil as _sh
            _sh.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _download_tiktok_via_tikwm(url: str, media_type: str = 'auto', custom_path: str = None) -> tuple[str, str, dict]:
    """Unduh video TikTok tanpa watermark menggunakan TikWM public API.
    Kembalikan (save_path, title, info_dict_mentah_TikWM).

    Mendukung tiga tipe konten:
    - Video biasa : unduh langsung via hdplay → play (selalu .mp4)
    - Image slideshow : render foto-foto + musik latar menjadi .mp4 via ffmpeg
    - Audio only : simpan musik (.mp3) — hanya bila media_type='audio'

    Tidak ada fallback ke audio untuk konten video/slideshow.
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    api_url = "https://www.tikwm.com/api/"
    # hd=1 meminta TikWM menyertakan field hdplay (kualitas lebih tinggi)
    resp = requests.get(api_url, params={"url": url, "hd": 1}, timeout=15, verify=False)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(f"TikWM API error: {data.get('msg')}")

    d = data["data"]
    title = d.get("title") or "tiktok_video"

    # ── Tentukan path simpan ───────────────────────────────────────────────────
    if custom_path:
        # custom_path = path tanpa ekstensi; ekstensi ditentukan nanti
        save_base = custom_path
    else:
        folder = _download_target_dir("tiktok")
        safe_name = _safe_download_name(title, "tiktok_video")
        save_base = os.path.join(folder, safe_name)

    if media_type == 'audio':
        # ── Mode audio eksplisit ───────────────────────────────────────────
        music_url = d.get("music") or (d.get("music_info") or {}).get("play") or ""
        if not music_url:
            raise ValueError("Tidak ada URL audio yang ditemukan di respon TikWM.")
        save_path = save_base + ".mp3"
        base, ext = os.path.splitext(save_path)
        idx = 1
        while os.path.exists(save_path):
            save_path = f"{base}_{idx}{ext}"
            idx += 1
        r = requests.get(music_url, timeout=120, stream=True, verify=False)
        r.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if chunk:
                    f.write(chunk)
        return save_path, title, d

    # ── Deteksi tipe konten ────────────────────────────────────────────────────
    # TikTok Image Slideshow: TikWM menyertakan field `images` (list URL gambar).
    # Kalau field ini ada dan berisi item, konten ini bukan video — harus dirender.
    image_list = d.get("images") or []
    is_slideshow = bool(image_list and isinstance(image_list, list))

    if is_slideshow:
        # ── Image slideshow → render jadi MP4 ─────────────────────────────
        music_url = d.get("music") or (d.get("music_info") or {}).get("play") or ""
        save_path = save_base + ".mp4"
        base, ext = os.path.splitext(save_path)
        idx = 1
        while os.path.exists(save_path):
            save_path = f"{base}_{idx}{ext}"
            idx += 1
        save_path = _render_slideshow_to_mp4(image_list, music_url, save_path)
    else:
        # ── Video biasa: hdplay (HD) → play (SD), keduanya TANPA watermark ─
        video_url = d.get("hdplay") or d.get("play") or ""
        if not video_url:
            raise ValueError(
                "Tidak ada URL video yang ditemukan di respon TikWM. "
                "Video mungkin privat atau sudah dihapus."
            )
        save_path = save_base + ".mp4"
        base, ext = os.path.splitext(save_path)
        idx = 1
        while os.path.exists(save_path):
            save_path = f"{base}_{idx}{ext}"
            idx += 1
        r = requests.get(video_url, timeout=120, stream=True, verify=False)
        r.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if chunk:
                    f.write(chunk)

    return save_path, title, d


def _write_media_meta(base_path: str, info: dict, url: str) -> str:
    """Tulis sidecar metadata .info.txt di samping file media. Return path sidecar."""
    meta_path = os.path.splitext(base_path)[0] + '.info.txt'
    dur = info.get('duration') or 0
    dur_s = f'{int(dur // 60)}m {int(dur % 60)}s' if dur else '-'
    up_date = str(info.get('upload_date') or '')
    if len(up_date) == 8:
        up_date = f'{up_date[6:8]}-{up_date[4:6]}-{up_date[:4]}'
    lines = [
        f'Judul      : {info.get("title") or "-"}',
        f'Uploader   : {info.get("uploader") or info.get("channel") or "-"}',
        f'Durasi     : {dur_s}',
        f'Tanggal    : {up_date or "-"}',
        f'Views      : {info.get("view_count") if info.get("view_count") is not None else "-"}',
        f'Likes      : {info.get("like_count") if info.get("like_count") is not None else "-"}',
        f'Komentar   : {info.get("comment_count") if info.get("comment_count") is not None else "-"}',
        f'Resolusi   : {info.get("width") or "?"}x{info.get("height") or "?"}',
        f'Share      : {info.get("repost_count") if info.get("repost_count") is not None else "-"}',
        f'Musik      : {info.get("music") or "-"}',
        f'Tags       : {", ".join(info.get("tags") or []) or "-"}',
        f'Hashtags   : {" ".join(dict.fromkeys(re.findall(r"#[\wÀ-ɏ]+", (str(info.get("title") or "") + " " + str(info.get("description") or ""))))) or "-"}',
        f'Sumber     : {url}',
        '',
        'Deskripsi/Caption:',
        (info.get('description') or '-').strip(),
    ]
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return meta_path
    except OSError:
        return ''


def _row_from_info(info: dict, media_path: str, url: str) -> dict:
    """Bangun 1 baris rekap CSV dari metadata yt-dlp/TikWM."""
    title = (info.get('title') or '').strip()
    desc = (info.get('description') or title or '').strip()
    tags = info.get('tags') or []
    tagstr = ' '.join('#' + str(x).lstrip('#') for x in tags) if tags else ''
    if not tagstr:
        # ekstrak hashtag dari judul/caption
        found = re.findall(r'#[\wÀ-ɏ]+', title + ' ' + desc)
        tagstr = ' '.join(dict.fromkeys(found))
    return {
        'video_path': media_path,
        'title': title,
        'caption': desc,
        'hashtags': tagstr,
        'link': url,
    }


def _download_via_ytdlp(url: str, platform: str, media_type: str = 'auto',
                        with_meta: bool = False, meta_out: list = None) -> list:
    """Unduh media dari link halaman (global) via yt-dlp — video, gambar, atau
    AUDIO (diekstrak ke MP3). Return list path tersimpan (media + sidecar meta).
    meta_out: kalau list diberikan, 1 baris rekap {video_path,title,caption,hashtags,link}
    di-append (dipakai membuat rekap_video.csv siap konversi upload)."""
    if platform == 'tiktok' and media_type != 'audio':
        try:
            path, title, tk = _download_tiktok_via_tikwm(url, media_type)
            saved = [path]
            if with_meta:
                author = tk.get('author') or {}
                import datetime as _dt
                up_date = ''
                if tk.get('create_time'):
                    up_date = _dt.datetime.fromtimestamp(int(tk['create_time'])).strftime('%Y%m%d')
                desc_extra = ' '.join(str(x) for x in (tk.get('content_desc') or []) if x)
                info = {
                    'title': title,
                    'description': (title + (' ' + desc_extra if desc_extra else '')).strip(),  # caption+hashtag
                    'uploader': author.get('nickname') or author.get('unique_id') or '-',
                    'duration': tk.get('duration') or 0,
                    'upload_date': up_date,
                    'view_count': tk.get('play_count'),
                    'like_count': tk.get('digg_count'),
                    'comment_count': tk.get('comment_count'),
                    'repost_count': tk.get('share_count'),
                    'music': (tk.get('music_info') or {}).get('title'),
                }
                mp = _write_media_meta(path, info, url)
                if mp:
                    saved.append(mp)
                if meta_out is not None:
                    meta_out.append(_row_from_info(info, path, url))
            elif meta_out is not None:
                meta_out.append(_row_from_info({'title': title}, path, url))
            return saved
        except Exception as e:
            print(f"[TIKTOK] TikWM download failed: {e}. Falling back to yt-dlp.")
    import repurpose
    from yt_dlp import YoutubeDL
    folder = _download_target_dir(platform)
    if media_type == 'image':
        fmt = 'bestimage/best'
    elif media_type == 'audio':
        fmt = 'bestaudio/best'
    else:
        # Paksa H.264 (avc1) + AAC (mp4a) dulu - kombinasi paling kompatibel
        # buat container MP4. Tanpa ini, yt-dlp sering pilih "kualitas
        # terbaik" YouTube yang justru AV1+Opus: videonya jalan normal tapi
        # BANYAK player/software edit (Windows Media Player, Premiere, dll)
        # gagal baca audio Opus-in-MP4-nya (bukan standar, walau valid) -
        # hasilnya video tanpa suara padahal audionya sebenarnya ada.
        # Fallback ke bv*+ba/b kalau video itu memang tidak punya opsi H.264.
        fmt = ('bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1][acodec^=mp4a]/'
               'bv*+ba/b')
    opts = {
        'outtmpl': os.path.join(folder, '%(title).80s.%(ext)s'),
        'format': fmt,
        'quiet': True, 'no_warnings': True, 'noprogress': True,
        'ffmpeg_location': os.path.dirname(repurpose.FFMPEG) if os.path.isfile(repurpose.FFMPEG) else None,
    }
    if media_type == 'audio':
        # Ekstrak ke MP3 192k (butuh ffmpeg — sudah dibundel GAMA)
        opts['postprocessors'] = [{'key': 'FFmpegExtractAudio',
                                   'preferredcodec': 'mp3', 'preferredquality': '192'}]
    else:
        opts['merge_output_format'] = 'mp4'
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if media_type == 'audio':
        base = os.path.splitext(path)[0]
        for ext in ('.mp3', '.m4a', '.opus', '.wav'):
            if os.path.isfile(base + ext):
                path = base + ext
                break
    if not os.path.isfile(path):
        base = os.path.splitext(path)[0]
        for ext in ('.mp4', '.mkv', '.webm', '.jpg', '.png', '.m4a', '.mp3'):
            if os.path.isfile(base + ext):
                path = base + ext
                break
    if not os.path.isfile(path):
        raise ValueError('Unduhan selesai tapi file tidak ditemukan.')
    saved = [path]
    if with_meta:
        mp = _write_media_meta(path, info or {}, url)
        if mp:
            saved.append(mp)
    return saved


def _download_shopee_assets(url: str, region: str = 'id', media_type: str = 'all',
                            photo_limit: int = 0, _scraper=None) -> list[str]:
    saved, _folder, _title = _download_shopee_product(url, region, media_type, photo_limit, _scraper)
    if not saved:
        raise ValueError('Tidak ada media Shopee yang berhasil diunduh.')
    return saved


def _download_shopee_product(url: str, region: str = 'id', media_type: str = 'all',
                             photo_limit: int = 0, _scraper=None):
    """Unduh media 1 produk Shopee. photo_limit=0 → semua foto; N → N foto pertama.
    Kembalikan (saved_paths, folder, title)."""
    from omnivora.scraper import ShopeeScraper
    if _scraper is None:
        settings = load_omnivora_settings()
        _scraper = ShopeeScraper(region_cookies=settings.get('shopee_cookies', {}))
    res = _scraper.scrape_product(url, force_browser=True)
    if not res:
        raise ValueError('Gagal membaca produk Shopee.')
    title = _safe_download_name(res.get('title', 'produk_shopee'), 'produk_shopee')
    folder = os.path.join(_download_target_dir('shopee'), title)
    os.makedirs(folder, exist_ok=True)
    saved = []
    if media_type in ('all', 'image'):
        images = res.get('image_urls', []) or []
        if photo_limit and photo_limit > 0:
            images = images[:photo_limit]
        for idx, media_url in enumerate(images, 1):
            save_path = os.path.join(folder, f'image_{idx}.jpg')
            if _scraper.download_file(media_url, save_path):
                saved.append(save_path)
    if media_type in ('all', 'video'):
        videos = res.get('video_urls', []) or []
        for idx, media_url in enumerate(videos, 1):
            ext = os.path.splitext(urlparse(media_url).path)[1] or '.mp4'
            save_path = os.path.join(folder, f'video_{idx}{ext}')
            if _scraper.download_file(media_url, save_path):
                saved.append(save_path)
    return saved, folder, title


def _run_downloader_job_thread(job_id, job):
    status_path = job['status_path']
    urls = job.get('urls', [])
    platform = (job.get('platform') or 'generic').strip().lower()
    region = (job.get('region') or 'id').strip().lower()
    media_type = (job.get('media_type') or 'auto').strip().lower()
    state = {'message': 'Menyiapkan download...', 'done': False, 'success': False, 'pct': 0, 'error': '', 'files': []}

    def update_state(msg, pct=None, done=False, success=False, error='', **extra):
        state['message'] = msg
        if pct is not None:
            state['pct'] = pct
        state['done'] = done
        state['success'] = success
        state['error'] = error
        state.update(extra)
        try:
            with open(status_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    # Opsi khusus Shopee foto produk (mode Dari Link)
    photo_limit = int(job.get('photo_limit') or 0)
    with_links  = bool(job.get('with_links'))
    device      = (job.get('device') or '').strip()
    adb_path    = job.get('adb_path') or ''

    try:
        saved = []
        errors = []
        video_rows = []     # baris rekap CSV per video (semua platform)
        product_rows = []   # (image_path, title, affiliate_link) per foto
        shopee_scraper = None
        adb = None
        if with_links and device and adb_path and os.path.isfile(adb_path):
            try:
                from shopee_adb_bridge import Adb
                adb = Adb(adb_path, device)
            except Exception:
                adb = None
        total = len(urls)
        _stop = job.get('stop_event')
        for idx, url in enumerate(urls, 1):
            if _stop is not None and _stop.is_set():
                update_state(f'Dihentikan. {len(saved)} file terunduh.',
                             done=True, success=bool(saved), files=saved,
                             stopped=True)
                return
            # Mode Auto (atau generic) → deteksi platform dari link. Region hanya
            # berlaku untuk Shopee; platform lain diunduh global via yt-dlp.
            plat = _detect_platform(url) if platform in ('auto', 'generic', '') else platform
            update_state(f'Download {idx}/{total} [{plat}]: {url[:70]}',
                         int(((idx - 1) / max(total, 1)) * 100))
            try:
                if plat == 'shopee':
                    if shopee_scraper is None:
                        from omnivora.scraper import ShopeeScraper
                        shopee_scraper = ShopeeScraper(
                            region_cookies=load_omnivora_settings().get('shopee_cookies', {}))
                    mt = 'image' if photo_limit or media_type == 'image' else (
                        'all' if media_type == 'auto' else media_type)
                    prod_saved, folder, title = _download_shopee_product(
                        url, region=region, media_type=mt,
                        photo_limit=photo_limit, _scraper=shopee_scraper)
                    saved.extend(prod_saved)
                    # Ambil link affiliate via HP (opsional)
                    aff = ''
                    if adb is not None:
                        update_state(f'Ambil link affiliate {idx}/{total}...',
                                     int(((idx - 1) / max(total, 1)) * 100))
                        try:
                            from shopee_adb_bridge import affiliate_link_for_url
                            aff = affiliate_link_for_url(adb, url)
                        except Exception:
                            aff = ''
                    for p in prod_saved:
                        if os.path.splitext(p)[1].lower() in ('.jpg', '.jpeg', '.png', '.webp'):
                            product_rows.append((p, title, aff))
                    # sidecar txt per produk
                    if aff or title:
                        try:
                            with open(os.path.join(folder, '_info.txt'), 'w', encoding='utf-8') as f:
                                f.write((title or '') + '\n')
                                if aff:
                                    f.write('\nLink affiliate (akun HP): ' + aff + '\n')
                                f.write('\nSumber: ' + url + '\n')
                        except OSError:
                            pass
                else:
                    # Global: yt-dlp dari link halaman; fallback ke media langsung.
                    try:
                        saved.extend(_download_via_ytdlp(url, plat, media_type,
                                                         with_meta=bool(job.get('with_meta')),
                                                         meta_out=video_rows))
                    except Exception:
                        direct = _save_direct_media(url, plat, media_type)
                        saved.append(direct)
                        video_rows.append({'video_path': direct, 'title': '',
                                           'caption': '', 'hashtags': '', 'link': url})
            except Exception as e:
                errors.append(f'{url[:60]}: {e}')
        if not saved and errors:
            raise ValueError('Semua unduhan gagal:\n' + '\n'.join(errors[:5]))

        # CSV rekap video — siap dikonversi ke upload SEMUA platform (incl Shopee)
        csv_path = ''
        if video_rows:
            import csv as _csv
            csv_path = os.path.join(DOWNLOADS_DIR, 'rekap_video.csv')
            try:
                with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                    w = _csv.writer(f)
                    w.writerow(['video_path', 'title', 'caption', 'hashtags', 'link'])
                    for r in video_rows:
                        w.writerow([r.get('video_path', ''), r.get('title', ''),
                                    r.get('caption', ''), r.get('hashtags', ''), r.get('link', '')])
            except OSError:
                csv_path = ''

        # CSV rekap foto produk Shopee
        if product_rows:
            shopee_csv = os.path.join(_download_target_dir('shopee'), 'rekap_produk.csv')
            with open(shopee_csv, 'w', encoding='utf-8-sig', newline='') as f:
                f.write('video_path;caption;hashtags;produk;product_link\n')
                for img, title, aff in product_rows:
                    cells = [img, title, '', title, aff]
                    f.write(';'.join(str(c).replace(';', ',').replace('\n', ' ') for c in cells) + '\n')
            if not csv_path:
                csv_path = shopee_csv

        msg = f'Download selesai! {len(saved)} file' + (f', {len(errors)} gagal' if errors else '')
        extra = {'files': saved, 'folder': DOWNLOADS_DIR, 'error': '\n'.join(errors[:5])}
        if csv_path:
            extra['csv'] = csv_path
        update_state(msg, 100, done=True, success=True, **extra)
    except Exception as e:
        update_state(f'ERROR: {e}', done=True, success=False, error=str(e))

def load_omnivora_settings():
    if os.path.isfile(OMNIVORA_SETTINGS_FILE):
        try:
            with open(OMNIVORA_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
            
    omni_env = r'D:\OMNIVORA.3.0\configs\environment.json'
    if os.path.isfile(omni_env):
        try:
            with open(omni_env, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                return {
                    'ai_provider': cfg.get('ai_provider', 'Gemini'),
                    'gemini_model': cfg.get('gemini_model', 'gemini-2.0-flash'),
                    'gemini_api_key': cfg.get('gemini_api_key', ''),
                    'gemini_api_keys': cfg.get('gemini_api_keys', []),
                    'groq_model': cfg.get('groq_model', 'llama-3.3-70b-versatile'),
                    'groq_api_keys': cfg.get('groq_api_keys', []),
                    'shopee_cookies': {
                        'id': cfg.get('shopee_cookie_id', ''),
                        'sg': cfg.get('shopee_cookie_sg', ''),
                        'ph': cfg.get('shopee_cookie_ph', ''),
                        'th': cfg.get('shopee_cookie_th', ''),
                        'my': cfg.get('shopee_cookie_my', '')
                    }
                }
        except Exception:
            pass
            
    return {
        'ai_provider': 'Gemini',
        'gemini_model': 'gemini-2.0-flash',
        'gemini_api_key': '',
        'gemini_api_keys': [],
        'groq_model': 'llama-3.3-70b-versatile',
        'groq_api_keys': [],
        'shopee_cookies': {'id': '', 'sg': '', 'ph': '', 'th': '', 'my': ''}
    }

def _run_scrape_job_thread(job_id, job):
    status_path = job['status_path']
    mode = job.get('mode', 'single')
    region = job.get('region', 'id')
    
    settings = load_omnivora_settings()
    shopee_cookies = settings.get('shopee_cookies', {})
    
    from omnivora.scraper import ShopeeScraper
    from omnivora.ai_caption import GeminiCaptionGenerator
    
    state = {'message': 'Menghubungkan ke Shopee...', 'done': False, 'success': False, 'pct': 10, 'error': ''}
    def update_state(msg, pct=None, done=False, success=False, error='', **extra):
        state['message'] = msg
        if pct is not None:
            state['pct'] = pct
        state['done'] = done
        state['success'] = success
        state['error'] = error
        state.update(extra)
        try:
            with open(status_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    try:
        scraper = ShopeeScraper(
            log_cb=lambda m, l='info': update_state(f'{m}'),
            region_cookies=shopee_cookies
        )
        
        gemini_keys = settings.get('gemini_api_keys', []) or [settings.get('gemini_api_key', '')]
        gemini_keys = [k for k in gemini_keys if k]
        groq_keys = settings.get('groq_api_keys', [])
        
        ai_generator = GeminiCaptionGenerator(
            api_keys=gemini_keys,
            groq_api_keys=groq_keys,
            provider=settings.get('ai_provider', 'Gemini'),
            model=settings.get('gemini_model', 'gemini-2.0-flash'),
            groq_model=settings.get('groq_model', 'llama-3.3-70b-versatile'),
            log_cb=lambda m, l='info': update_state(f'{m}')
        )

        output_dir = os.path.join(BASE_DIR, 'output')
        os.makedirs(output_dir, exist_ok=True)

        if mode == 'single':
            url = job.get('url', '').strip()
            update_state('Mengambil data produk Shopee...', 30)
            res = scraper.scrape_product(url, force_browser=True)
            if not res:
                raise ValueError('Gagal mengambil data produk dari Shopee. Pastikan link benar atau selesaikan captcha di browser.')
                
            update_state('Mengunduh media produk...', 50)
            title_sanitized = re.sub(r'[\/*?:"<>|]', '_', res.get('title', 'product'))[:80].strip()
            product_dir = os.path.join(output_dir, title_sanitized)
            os.makedirs(product_dir, exist_ok=True)
            
            downloaded = []
            image_limit = job.get('image_limit', '5')
            img_limit = None if job.get('all_images') else int(image_limit or 5)
            
            image_urls = res.get('image_urls', [])
            to_download = len(image_urls) if img_limit is None else min(len(image_urls), img_limit)
            for idx, img_url in enumerate(image_urls[:to_download], 1):
                save_path = os.path.join(product_dir, f'image_{idx}.jpg')
                update_state(f'Mengunduh gambar {idx}/{to_download}...', 50 + int((idx/to_download)*20))
                if scraper.download_image(img_url, save_path):
                    downloaded.append(save_path)
                    
            update_state('Membuat caption + TTS dengan AI...', 80)
            ai_res = ai_generator.generate_caption(
                title=res.get('title', ''),
                description=res.get('description', ''),
                region=region
            )
            
            caption = ai_res.get('caption', '')
            tts_script = ai_res.get('tts_script', '')
            
            if caption:
                with open(os.path.join(product_dir, 'caption.txt'), 'w', encoding='utf-8') as f:
                    f.write(caption)
            if tts_script:
                with open(os.path.join(product_dir, 'tts_script.txt'), 'w', encoding='utf-8') as f:
                    f.write(tts_script)
                    
            # Simpan detail json di folder produk
            with open(os.path.join(product_dir, 'details.json'), 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=4)
                
            update_state('Scrape selesai!', 100, done=True, success=True, data={
                'title': res.get('title', ''),
                'description': res.get('description', ''),
                'price': res.get('price', ''),
                'save_folder': product_dir,
                'caption': caption,
                'tts_script': tts_script,
                'media': [os.path.basename(p) for p in downloaded]
            })
            
        elif mode == 'bulk':
            urls = job.get('urls', [])
            keyword = job.get('keyword', '')
            limit = int(job.get('limit', 10))
            
            if keyword:
                update_state(f'Mencari produk Shopee untuk keyword "{keyword}"...', 20)
                search_res = scraper.search_products(keyword, region=region, limit=limit)
                urls = [p.get('product_url') for p in search_res if p.get('product_url')]
                
            if not urls:
                raise ValueError('Tidak ada URL produk yang dapat di-scrape.')
                
            rows = []
            total = len(urls)
            csv_path = os.path.join(output_dir, f'scrape_hasil_{int(time.time())}.csv')
            
            for idx, url in enumerate(urls, 1):
                update_state(f'Scraping produk {idx}/{total}...', 20 + int((idx/total)*60))
                res = scraper.scrape_product(url, force_browser=True)
                if not res:
                    continue
                    
                title_sanitized = re.sub(r'[\/*?:"<>|]', '_', res.get('title', 'product'))[:80].strip()
                product_dir = os.path.join(output_dir, title_sanitized)
                os.makedirs(product_dir, exist_ok=True)
                
                primary_image = ''
                if res.get('image_urls'):
                    save_path = os.path.join(product_dir, 'image_1.jpg')
                    if scraper.download_image(res['image_urls'][0], save_path):
                        primary_image = save_path
                
                ai_res = ai_generator.generate_caption(
                    title=res.get('title', ''),
                    description=res.get('description', ''),
                    region=region
                )
                
                caption = ai_res.get('caption', '')
                tts_script = ai_res.get('tts_script', '')
                
                if caption:
                    with open(os.path.join(product_dir, 'caption.txt'), 'w', encoding='utf-8') as f:
                        f.write(caption)
                if tts_script:
                    with open(os.path.join(product_dir, 'tts_script.txt'), 'w', encoding='utf-8') as f:
                        f.write(tts_script)
                
                rows.append({
                    'title': res.get('title', ''),
                    'caption': caption,
                    'video_path': primary_image,
                    'product_url': url,
                    'region': region
                })
                
            import csv
            with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(['video_path', 'caption', 'title', 'product_url', 'region'])
                for r in rows:
                    writer.writerow([r['video_path'], r['caption'], r['title'], r['product_url'], r['region']])
                    
            update_state('Scrape bulk selesai!', 100, done=True, success=True, csv_path=csv_path, rows=rows)
            
    except Exception as e:
        update_state(f'ERROR: {e}', done=True, success=False, error=str(e))

@app.route('/api/open_url', methods=['POST'])
def open_url():
    """Buka URL (https) di browser sistem — dipakai tombol link 'dapatkan API key'
    di Pengaturan (di aplikasi desktop pywebview, <a> biasa tidak membuka browser)."""
    url = ((request.get_json() or {}).get('url') or '').strip()
    if not url.startswith('https://'):
        return jsonify({'error': 'URL tidak valid.'})
    try:
        webbrowser.open(url)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})

#─── Routes: Auto-update ─────────────────────────────────────────────────────
# Sumber rilis: GitHub Releases (repo publik, tanpa token). Lihat gama_update.py.

@app.route('/api/update/check', methods=['GET'])
def update_check():
    """Bandingkan versi yang sedang jalan dengan rilis terbaru."""
    try:
        import gama_update
        return jsonify(gama_update.check())
    except Exception as e:
        # Versi saat ini tetap dikirim supaya UI bisa menampilkannya walau
        # pengecekan gagal (mis. sedang offline).
        try:
            import gama_update
            sekarang = gama_update.APP_VERSION
        except Exception:
            sekarang = '?'
        return jsonify({'error': str(e), 'current': sekarang, 'available': False})


@app.route('/api/update/download', methods=['POST'])
def update_download():
    """Mulai unduh installer versi terbaru di background."""
    try:
        import gama_update
        info = gama_update.check()
        if not info.get('available'):
            return jsonify({'error': 'Sudah versi terbaru.'})
        if not gama_update.start_download(info):
            return jsonify({'error': 'Unduhan sedang berjalan.'})
        return jsonify({'success': True, 'version': info['version'],
                        'size': info['size']})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/update/status', methods=['GET'])
def update_status():
    """Progres unduhan — dipanggil berkala oleh UI."""
    try:
        import gama_update
        return jsonify(gama_update.get_state())
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/update/install', methods=['POST'])
def update_install():
    """Jalankan installer lalu tutup GAMA.

    GAMA WAJIB ditutup: installer menimpa file yang sedang dipakai proses ini,
    dan Windows mengunci EXE yang sedang berjalan. Penutupan ditunda ~1,5 detik
    supaya respons HTTP ini sempat sampai ke UI lebih dulu.
    """
    try:
        import gama_update
        st = gama_update.get_state()
        berkas = st.get('file')
        if not berkas or not os.path.isfile(berkas):
            return jsonify({'error': 'Installer belum selesai diunduh.'})
        gama_update.run_installer(berkas)

        def tutup():
            time.sleep(1.5)
            try:
                import webview
                if webview.windows:
                    webview.windows[0].destroy()
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=tutup, daemon=True).start()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(load_omnivora_settings())

@app.route('/api/settings/save', methods=['POST'])
def save_settings():
    data = request.get_json() or {}
    try:
        # MERGE dengan setting lama — UI Pengaturan hanya mengirim field API key,
        # field lain (shopee_cookies, gemini_model, dll) tidak boleh ikut terhapus.
        current = load_omnivora_settings()
        current.update(data)
        with open(OMNIVORA_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(current, f, ensure_ascii=False, indent=4)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/riset/search', methods=['POST'])
def riset_search():
    data = request.get_json() or {}
    platform = (data.get('platform') or 'shopee').strip().lower()
    profile = (data.get('profile') or '').strip()
    keyword = (data.get('keyword') or '').strip()
    region = (data.get('region') or 'id').strip()

    if not profile or profile not in list_profile_dirs():
        return jsonify({'error': 'Pilih profile GAM yang sudah login terlebih dahulu.'})
    if not keyword:
        return jsonify({'error': 'Keyword wajib diisi.'})
    if platform not in RISET_PLATFORMS:
        return jsonify({'error': f'Platform riset "{platform}" belum dikenali.'})

    return jsonify({
        'success': True,
        'platform': platform,
        'profile': profile,
        'mode': 'links',
        'results': _build_riset_links(platform, keyword, region=region),
    })
# Platform yang mengunci akses tanpa login (Instagram & Threads sekarang minta
# sesi login bahkan utk 1 post publik) — mode Dari Link-nya lewat browser profil
# GAM yang sudah login, BUKAN yt-dlp lagi. Facebook/X/Pinterest link langsung
# masih jalan normal via yt-dlp jadi tidak perlu profil.
BROWSER_ONLY_LINK_PLATFORMS = {'instagram', 'threads'}
BROWSER_ONLY_ACCOUNT_PLATFORMS = {'facebook', 'instagram', 'threads', 'x', 'twitter', 'pinterest'}


@app.route('/api/downloader/download', methods=['POST'])
def downloader_download():
    data = request.get_json() or {}
    urls = [str(u).strip() for u in (data.get('urls') or []) if str(u).strip()]
    if not urls:
        return jsonify({'error': 'Masukkan minimal satu URL media atau link produk.'})
    platform = (data.get('platform') or 'auto').strip().lower()

    if platform in BROWSER_ONLY_LINK_PLATFORMS:
        profile = (data.get('profile') or '').strip()
        if not profile or profile not in list_profile_dirs():
            return jsonify({'error': f'Pilih profil GAM yang sudah login {platform} dulu.'})
        job = {
            'platform':     'x' if platform == 'twitter' else platform,
            'mode':         'link',
            'profile_path': os.path.join(PROFILES_DIR, profile),
            'profile':      profile,
            'urls':         urls,
            'out_dir':      _download_target_dir(platform),
            'title':        f'{platform} (link) via {profile}',
        }
        job_id = _start_worker('social_downloader_bridge.py', job)
        return jsonify({'success': True, 'job_id': job_id})

    # Kalau minta link affiliate via HP, resolve adb + validasi device
    if data.get('with_links') and data.get('device'):
        adb = _resolve_adb()
        if not adb:
            return jsonify({'error': 'adb.exe tidak ditemukan untuk ambil link affiliate.'})
        devs = [d['serial'] for d in _adb_devices(adb) if d.get('state') == 'device']
        if data['device'] not in devs:
            return jsonify({'error': f'HP {data["device"]} tidak terhubung.'})
        data['adb_path'] = adb
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_id = 'job_downloader_' + datetime.now().strftime('%Y%m%d%H%M%S') + '_' + os.urandom(3).hex()
    status_path = os.path.join(JOBS_DIR, job_id + '.status.json')
    with open(status_path, 'w', encoding='utf-8') as f:
        json.dump({'message': 'Antre...', 'done': False, 'success': False, 'error': '', 'pct': 0}, f)
    data['status_path'] = status_path
    _stop_ev = threading.Event()
    data['stop_event'] = _stop_ev
    t = threading.Thread(target=_run_downloader_job_thread, args=(job_id, data), daemon=True)
    t.start()
    with _yt_jobs_lock:
        _yt_jobs[job_id] = {
            'thread': t,
            'stop_event': _stop_ev,
            'status_path': status_path,
            'profile': '',
            'title': data.get('platform', 'downloader'),
        }
    return jsonify({'success': True, 'job_id': job_id})


# Platform mode "Per Akun" yang diunduh via yt-dlp (TANPA ADB). Tiap platform
# punya cara membentuk URL profil dari username. Shopee TIDAK di sini (pakai ADB).
ACCOUNT_YTDLP_PLATFORMS = {
    'tiktok':    lambda u: f'https://www.tiktok.com/@{u}',
    'youtube':   lambda u: f'https://www.youtube.com/@{u}/videos',
    'instagram': lambda u: f'https://www.instagram.com/{u}/',
    'facebook':  lambda u: f'https://www.facebook.com/{u}/videos',
    'twitter':   lambda u: f'https://twitter.com/{u}',
    'pinterest': lambda u: f'https://www.pinterest.com/{u}/',
}


def _account_profile_url(platform: str, target: str):
    """Kembalikan (url_profil, username) untuk mode Per Akun. Kalau target sudah
    berupa URL penuh, dipakai apa adanya."""
    target = (target or '').strip()
    if target.startswith('http'):
        # Utamakan @handle bila ada; kalau tidak, ambil segmen path pertama yang
        # bukan tab standar (videos/shorts/reels/featured/streams).
        m = re.search(r'@([\w.\-]+)', target)
        if m:
            return target, m.group(1)
        segs = [s for s in urlparse(target).path.split('/') if s
                and s.lower() not in ('videos', 'shorts', 'reels', 'featured', 'streams', 'user', 'c', 'channel')]
        return target, (segs[0] if segs else 'akun')
    username = re.sub(r'^@', '', target.rstrip('/').split('/')[-1])
    builder = ACCOUNT_YTDLP_PLATFORMS.get(platform)
    url = builder(username) if builder else f'https://www.tiktok.com/@{username}'
    return url, username


def _run_tiktok_account_job_thread(job_id, job):
    """Download N video terbaru dari satu akun (TikTok/YouTube/Instagram/Facebook/
    dll) via yt-dlp — TANPA ADB — lengkap caption + hashtag (sidecar .txt + CSV
    siap dikonversi). Shopee dikecualikan (butuh HP/ADB)."""
    status_path = job['status_path']
    platform = (job.get('platform') or 'tiktok').lower()
    state = {'message': 'Menyiapkan yt-dlp...', 'pct': 0, 'done': False, 'success': False, 'error': ''}

    def update_state(msg, pct=None, done=False, success=False, error='', **extra):
        state['message'] = msg
        if pct is not None:
            state['pct'] = pct
        state.update(done=done, success=success, error=error, **extra)
        try:
            with open(status_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    # Dideklarasikan sebelum try supaya blok finally di bawah tetap bisa
    # menutupnya walau kesalahan terjadi sebelum browser sempat dibuka.
    pembaca = None

    try:
        import yt_dlp
        target = (job.get('target') or '').strip()
        raw_limit = int(job.get('limit') or 0)
        download_all = raw_limit <= 0            # 0 / kosong = semua video akun
        limit = 500 if download_all else raw_limit   # TANPA cap 100 lagi
        url, username = _account_profile_url(platform, target)

        out_dir = os.path.join(DOWNLOADS_DIR, f'{platform}_akun', re.sub(r'[^\w\-.]+', '_', username)[:40])
        os.makedirs(out_dir, exist_ok=True)

        # Pembaca halaman TikTok — HANYA dibuka kalau user meminta lokasi &
        # produk. Video, caption, hashtag, dan musik tidak membutuhkannya
        # (diambil lewat tikwm tanpa browser, jadi tanpa verifikasi puzzle).
        if platform == 'tiktok' and job.get('ambil_meta') and job.get('profile'):
            try:
                import tiktok_downloader as _ttdl
                jalur_profil = os.path.join(PROFILES_DIR, job['profile'])
                chrome = get_chrome_path()
                if chrome and os.path.isdir(jalur_profil):
                    pembaca = _ttdl.PembacaHalaman(
                        chrome, jalur_profil,
                        log=lambda m: update_state(m, state.get('pct')))
                    pembaca.__enter__()
                    update_state('Browser disiapkan untuk membaca lokasi & produk...', 8)
            except Exception as pe:
                print(f'[TIKTOK ACCOUNT] Pembaca halaman gagal disiapkan: {pe}')
                pembaca = None

        update_state(f'Mengambil daftar video {platform} @{username}...', 5)
        flat_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                     'skip_download': True}
        if not download_all:
            flat_opts['playlistend'] = limit
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            # yt-dlp gagal membaca sebagian akun TikTok ("Unable to extract
            # secondary user ID"). Kalau browser pembaca sudah terbuka, daftar
            # videonya diambil dari halaman profil — sumber yang sama yang
            # dilihat manusia, jadi jauh lebih jarang gagal.
            dari_browser = pembaca.daftar_video(username, limit) if pembaca else []
            if not dari_browser:
                raise ValueError(f'Gagal membuka profil {platform} "{username}". '
                                 f'Cek username/link. Untuk TikTok, centang '
                                 f'"Ambil tag lokasi & produk" agar daftar video '
                                 f'bisa dibaca lewat browser. Detail: {str(e)[:120]}')
            update_state(f'{len(dari_browser)} video dibaca lewat browser.', 10)
            info = {'entries': [{'url': u} for u in dari_browser]}
        # Bisa channel dengan banyak tab (entries berisi playlist) → ratakan
        entries = []
        for e in (info.get('entries') or [info]):
            if not e:
                continue
            if e.get('entries'):
                entries.extend([x for x in e['entries'] if x])
            else:
                entries.append(e)
        entries = entries[:limit]
        if not entries:
            raise ValueError(f'Tidak ada video ditemukan untuk {platform} "{username}". Cek username-nya.')

        # media_type: 'video' (default) | 'image' (cover/gambar tiap post via
        # thumbnail, tanpa video) | 'both'. Untuk platform gambar (IG/Pinterest)
        # thumbnail = gambar aslinya; untuk platform video = cover post-nya.
        media_type = (job.get('media_type') or 'video').lower()
        want_video = media_type in ('video', 'both')
        want_image = media_type in ('image', 'both')
        IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
        VID_EXTS = ('.mp4', '.mkv', '.webm', '.mov')
        label = {'video': 'video', 'image': 'gambar', 'both': 'media'}.get(media_type, 'video')

        saved, csv_rows, errors = [], [], []
        # Lokasi per berkas hasil, dipakai mengisi kolom `location` di CSV
        # siap-upload TikTok supaya video bisa diposting ulang di tempat yang
        # sama tanpa mengetik ulang alamatnya.
        lokasi_per_file = {}

        total = len(entries)
        _stop = job.get('stop_event')
        for i, ent in enumerate(entries, 1):
            if _stop is not None and _stop.is_set():
                update_state(f'Dihentikan. {len(saved)} {label} terunduh.',
                             done=True, success=bool(saved), files=saved, stopped=True)
                return
            vid_url = ent.get('url') or ent.get('webpage_url')
            update_state(f'Download {label} {i}/{total} dari {platform} @{username}...',
                         5 + int((i - 1) / total * 90))
            base = f'{i:02d}_{ent.get("id") or i}'
            outtmpl = os.path.join(out_dir, base + '.%(ext)s')
            dl_opts = {'quiet': True, 'no_warnings': True, 'outtmpl': outtmpl}
            if want_video:
                dl_opts['format'] = ('bestvideo*[vcodec^=avc1]+bestaudio[acodec^=mp4a]/'
                                     'best[vcodec^=avc1][acodec^=mp4a]/mp4/bestvideo*+bestaudio/best')
            else:
                # Mode gambar saja: jangan unduh video, cukup thumbnail/cover.
                dl_opts['skip_download'] = True
            if want_image:
                dl_opts['writethumbnail'] = True

            try:
                vinfo = {}
                used_tikwm = False
                tt_detail = None
                if platform == 'tiktok':
                    try:
                        path, title, _tk = _download_tiktok_via_tikwm(vid_url, media_type, os.path.join(out_dir, base))
                        vinfo = {'description': title, 'title': title}
                        produced = [path]
                        vids = [path] if os.path.splitext(path)[1].lower() in VID_EXTS else []
                        imgs = [path] if os.path.splitext(path)[1].lower() in IMG_EXTS else []
                        used_tikwm = True
                        # Data mentah tikwm sudah di tangan — diurai di sini juga
                        # supaya musik, lokasi, dan statistik ikut terekam tanpa
                        # memanggil API-nya dua kali.
                        try:
                            import tiktok_downloader as _ttdl
                            tt_detail = _ttdl.detail_dari_data(vid_url, _tk)
                            # Lokasi & produk tidak ada di tikwm — hanya di
                            # halaman web. Dibaca lewat jendela browser yang
                            # sama untuk seluruh antrean, supaya verifikasi
                            # puzzle (kalau muncul) cukup sekali di awal.
                            if pembaca is not None:
                                update_state(f'Baca lokasi & produk {i}/{total}...',
                                             int(((i - 1) / max(total, 1)) * 100))
                                tambahan = pembaca.baca(vid_url)
                                if tambahan:
                                    if tambahan.get('lokasi'):
                                        tt_detail['lokasi'] = tambahan['lokasi']
                                    if tambahan.get('produk'):
                                        tt_detail['produk'] = tambahan['produk']
                                    for k in ('lokasi_nama', 'lokasi_alamat',
                                              'lokasi_kota', 'lokasi_id'):
                                        tt_detail[k] = tambahan.get(k, '')
                        except Exception as de:
                            print(f"[TIKTOK ACCOUNT] Metadata lengkap gagal: {de}")
                    except Exception as te:
                        print(f"[TIKTOK ACCOUNT] TikWM failed for {vid_url}: {te}. Fallback to yt-dlp.")

                if not used_tikwm:
                    with yt_dlp.YoutubeDL(dl_opts) as ydl:
                        vinfo = ydl.extract_info(vid_url, download=True)
                    # Sengaja exclude audio-only — TikTok slideshow kadang menghasilkan
                    # mp3/m4a saja dari yt-dlp; path audio tidak boleh masuk CSV upload.
                    _AUD_EXTS = ('.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.flac')
                    produced = [os.path.join(out_dir, fn) for fn in os.listdir(out_dir)
                                if fn.startswith(base + '.') and not fn.endswith('.txt')
                                and os.path.splitext(fn)[1].lower() not in _AUD_EXTS]
                    vids = [p for p in produced if os.path.splitext(p)[1].lower() in VID_EXTS]
                    imgs = [p for p in produced if os.path.splitext(p)[1].lower() in IMG_EXTS]

                # File utama yang dicatat di CSV: gambar kalau mode image, video kalau tidak.
                # Untuk TikTok mode video: mainfiles WAJIB berisi file video (.mp4) —
                # tidak pernah audio. Kalau tidak ada file video, item ini dihitung gagal.
                if media_type == 'image':
                    mainfiles = imgs
                elif media_type == 'both':
                    mainfiles = vids + imgs
                else:
                    # vids sudah di-filter hanya .mp4/.mkv/.webm/.mov
                    # produced (yt-dlp path) juga sudah exclude audio di atas
                    mainfiles = vids if vids else produced
                if not mainfiles:
                    raise RuntimeError(
                        'Tidak ada file video yang dihasilkan. '
                        'Konten mungkin slideshow yang gagal dirender atau format tidak didukung.'
                    )

                caption_full = (vinfo.get('description') or vinfo.get('title') or '').strip()
                tags = re.findall(r'#[\w\d_]+', caption_full)
                hashtags = ','.join(t.lstrip('#') for t in tags)
                caption = re.sub(r'\s{2,}', ' ', re.sub(r'#[\w\d_]+', '', caption_full)).strip()

                # TikTok: sidecar .txt berisi rekap LENGKAP (musik, lokasi,
                # produk, statistik), bukan cuma caption.
                # unduh_musik TIDAK dipanggil — mp3 musik hanya dipakai secara
                # temporary saat render slideshow (sudah dihapus otomatis), tidak
                # disimpan permanen di folder output agar folder tetap bersih (.mp4 saja).
                lokasi_video = ''
                if tt_detail:
                    try:
                        import tiktok_downloader as _ttdl
                        _ttdl.tulis_metadata(tt_detail, os.path.join(out_dir, base + '.txt'))
                        lokasi_video = tt_detail.get('lokasi') or ''
                    except Exception as me:
                        print(f"[TIKTOK ACCOUNT] Tulis metadata gagal: {me}")
                        with open(os.path.join(out_dir, base + '.txt'), 'w', encoding='utf-8') as f:
                            f.write(caption_full + '\n')
                else:
                    with open(os.path.join(out_dir, base + '.txt'), 'w', encoding='utf-8') as f:
                        f.write(caption_full + '\n')

                # Kolom rekap: satu baris = satu video, berisi semua yang
                # diminta — path, deskripsi, tag, lokasi, produk, dan musik.
                produk_nama, produk_link, musik_nama = '', '', ''
                if tt_detail:
                    p = (tt_detail.get('produk') or [])
                    produk_nama = ' | '.join(x.get('nama', '') for x in p if x.get('nama'))
                    produk_link = ' | '.join(x.get('link', '') for x in p if x.get('link'))
                    judul_musik = tt_detail.get('musik_judul') or ''
                    artis = tt_detail.get('musik_artis') or ''
                    musik_nama = (f'{judul_musik} - {artis}'
                                  if judul_musik and artis and artis not in judul_musik
                                  else judul_musik)

                for mf in mainfiles:
                    saved.append(mf)
                    csv_rows.append([mf, caption, hashtags, lokasi_video,
                                     produk_nama, produk_link, musik_nama])
                    lokasi_per_file[mf] = lokasi_video
            except Exception as e:
                errors.append(f'{label} {i}: {str(e)[:120]}')

        if not saved:
            raise ValueError('Semua download gagal: ' + '; '.join(errors[:3]))

        def _bersih(nilai):
            """Titik koma diganti koma karena itu pemisah kolom CSV-nya."""
            return str(nilai or '').replace(';', ',').replace('\n', ' ').strip()

        # CSV rekap: satu baris per video, memuat semua metadata yang diambil.
        csv_path = os.path.join(out_dir, 'rekap_video.csv')
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            f.write('video_path;caption;hashtags;location;produk;product_link;musik\n')
            for row in csv_rows:
                f.write(';'.join(_bersih(c) for c in row) + '\n')

        # TikTok video: CSV siap-upload TikTok.
        # Kolom sesuai template resmi: video_path | caption | hashtags | music |
        # location | ai_generate_content | schedule_date | schedule_time.
        # Kolom musik diisi dari metadata (judul - artis).
        # Kolom location diisi dari tag lokasi video aslinya.
        # Kolom ai_generate_content, schedule_date, schedule_time dikosongkan
        # supaya user hanya perlu mengisi jadwal sebelum upload.
        if platform == 'tiktok' and media_type == 'video':
            tt_csv = os.path.join(out_dir, 'upload_tiktok.csv')
            with open(tt_csv, 'w', encoding='utf-8-sig', newline='') as f:
                f.write('video_path;caption;hashtags;music;location;'
                        'ai_generate_content;schedule_date;schedule_time\n')
                for row in csv_rows:
                    # row: [0]=video_path [1]=caption [2]=hashtags [3]=lokasi
                    #       [4]=produk_nama [5]=produk_link [6]=musik_nama
                    f.write(
                        f'{_bersih(row[0])};'   # video_path
                        f'{_bersih(row[1])};'   # caption
                        f'{_bersih(row[2])};'   # hashtags
                        f'{_bersih(row[6])};'   # music  (judul - artis)
                        f'{_bersih(row[3])};'   # location
                        f';;\n'                 # ai_generate_content;schedule_date;schedule_time
                    )
            csv_path = tt_csv

        msg = f'Selesai! {len(saved)} {label} {platform} @{username} + caption tersimpan di {out_dir}'
        if errors:
            msg += f' ({len(errors)} gagal)'
        update_state(msg, 100, done=True, success=True,
                     files=saved, folder=out_dir, csv=csv_path, error='; '.join(errors[:3]))
    except Exception as e:
        update_state(f'ERROR: {e}', done=True, success=False, error=str(e))
    finally:
        # Jendela browser pembaca lokasi/produk harus ditutup apa pun hasilnya,
        # kalau tidak Chrome menggantung dan profilnya terkunci untuk pemakaian
        # berikutnya.
        try:
            if pembaca is not None:
                pembaca.__exit__(None, None, None)
        except Exception:
            pass


@app.route('/api/downloader/account', methods=['POST'])
def downloader_account():
    """Download semua/N video dari satu akun (TikTok via yt-dlp, Shopee Video
    via browser profil GAM yang sudah login)."""
    data = request.get_json() or {}
    platform = (data.get('platform') or 'tiktok').lower()
    target = (data.get('target') or '').strip()
    if not target:
        return jsonify({'error': 'Isi username atau link profil dulu.'})

    if platform in BROWSER_ONLY_ACCOUNT_PLATFORMS:
        profile = (data.get('profile') or '').strip()
        if not profile or profile not in list_profile_dirs():
            return jsonify({'error': f'Pilih profil GAM yang sudah login {platform} dulu.'})
        norm_platform = 'x' if platform == 'twitter' else platform
        job = {
            'platform':     norm_platform,
            'mode':         'account',
            'profile_path': os.path.join(PROFILES_DIR, profile),
            'profile':      profile,
            'target':       target,
            'limit':        int(data.get('limit') or 0),
            'media_type':   (data.get('media_type') or 'video').lower(),
            'out_dir':      os.path.join(DOWNLOADS_DIR, f'{norm_platform}_akun'),
            'title':        f'{platform} @{target[:30]} via {profile}',
        }
        job_id = _start_worker('social_downloader_bridge.py', job)
        return jsonify({'success': True, 'job_id': job_id})

    if platform == 'shopee':
        # Shopee Video hanya bisa via HP asli (ADB) — feed diblokir di browser.
        adb = _resolve_adb()
        if not adb:
            return jsonify({'error': 'adb.exe tidak ditemukan (bundel GAMA hilang).'})
        device = (data.get('device') or '').strip()
        devs = [d for d in _adb_devices(adb) if d.get('state') == 'device']
        if not devs:
            return jsonify({'error': 'Tidak ada HP terhubung. Sambungkan HP (USB debugging ON) lalu coba lagi.'})
        if not device:
            device = devs[0]['serial']
        elif device not in [d['serial'] for d in devs]:
            return jsonify({'error': f'HP {device} tidak terhubung.'})
        region = (data.get('region') or 'id').lower()
        job = {
            'platform':  'shopee',
            'device':    device,
            'adb_path':  adb,
            'target':    target,
            'region':    region,
            'package':   PKG_BY_REGION.get(region, 'com.shopee.id'),
            'limit':     int(data.get('limit') or 0),   # 0 = SEMUA video (semua region)
            'with_links': bool(data.get('with_links')),
            'out_dir':   os.path.join(DOWNLOADS_DIR, 'shopee_akun'),
            'title':     f'Shopee Video (HP {device})',
        }
        job_id = _start_worker('shopee_adb_bridge.py', job, console=True, graceful_stop=True)
        return jsonify({'success': True, 'job_id': job_id})

    # TikTok (default): in-process thread via yt-dlp
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_id = 'job_dlacc_' + datetime.now().strftime('%Y%m%d%H%M%S') + '_' + os.urandom(3).hex()
    status_path = os.path.join(JOBS_DIR, job_id + '.status.json')
    with open(status_path, 'w', encoding='utf-8') as f:
        json.dump({'message': 'Antre...', 'done': False, 'success': False, 'error': '', 'pct': 0}, f)
    data['status_path'] = status_path
    _stop_ev = threading.Event()
    data['stop_event'] = _stop_ev
    t = threading.Thread(target=_run_tiktok_account_job_thread, args=(job_id, data), daemon=True)
    t.start()
    with _yt_jobs_lock:
        _yt_jobs[job_id] = {
            'thread':      t,
            'stop_event':  _stop_ev,
            'status_path': status_path,
            'profile':     '',
            'title':       f'{platform} @{target[:40]}',
        }
    return jsonify({'success': True, 'job_id': job_id})


# Header CSV per platform (siap dipakai di tab Upload masing-masing)
CONVERT_HEADERS = {
    'facebook':  'video_path;caption',
    'instagram': 'video_path;caption',
    'threads':   'video_path;caption',
    'x':         'video_path;caption;schedule_date;schedule_time',
    'tiktok':    'video_path;caption;hashtags;schedule_date;schedule_time',
    'youtube':   'VIDEO PATH;TITLE;DESCRIPTION;TAGS;THUMBNAIL;DATE;TIME;AI CONTENT;MADE FOR KIDS',
    'pinterest': 'video_path;title;caption;link;schedule_date;schedule_time',
    'shopee':    'video_path;title;caption;hashtags;product_link',
}


def _clean_csv_text(s: str) -> str:
    """Bersihkan zero-width char + HTML entity yang mungkin terbawa dari sumber."""
    import html as _html
    zw = dict.fromkeys(map(ord, '​‌‍⁠﻿'), None)
    return _html.unescape(str(s or '').translate(zw)).strip()


def _merge_caption(caption: str, hashtags: str) -> str:
    """Gabung caption + hashtag jadi satu caption (untuk FB/IG/Threads/X)."""
    caption = _clean_csv_text(caption)
    hashtags = _clean_csv_text(hashtags)
    tags = [t.strip().lstrip('#') for t in re.split(r'[,\s]+', hashtags or '') if t.strip()]
    tagstr = ' '.join('#' + t for t in tags)
    if tagstr:
        return (caption + ' ' + tagstr).strip()
    return caption


def _convert_rekap_to_uploads(csv_path, platforms, out_dir=None):
    """Inti konversi rekap CSV → upload_<platform>.csv. Return (created_files, count).
    Dipakai route konverter Downloader DAN auto-convert Clipper."""
    rows = _read_csv_rows(csv_path)
    if not rows:
        return [], 0
    parsed = []
    for r in rows:
        parsed.append({
            'video_path': _pick_csv_value(r, 'video_path', 'video', 'file', 'VIDEO PATH'),
            'judul':      _clean_csv_text(_pick_csv_value(r, 'title', 'judul', 'TITLE')),
            'caption':    _clean_csv_text(_pick_csv_value(r, 'caption', 'title', 'description', 'TITLE')),
            'hashtags':   _clean_csv_text(_pick_csv_value(r, 'hashtags', 'tags', 'hashtag', 'TAGS')),
            'link':       _clean_csv_text(_pick_csv_value(r, 'product_links', 'product_link', 'link', 'links', 'produk')),
        })
    out_dir = out_dir or os.path.dirname(csv_path)
    created = []
    for p in platforms:
        header = CONVERT_HEADERS.get(p)
        if not header:
            continue
        ncols = header.count(';') + 1
        lines = [header]
        for row in parsed:
            vp = row['video_path']
            if not vp:
                continue
            cap = _merge_caption(row['caption'], row['hashtags'])
            judul = (row.get('judul') or '').strip() or (row['caption'] or '')
            if p == 'pinterest':
                cells = [vp, judul[:100], cap, row['link'] or '', '', '']
            elif p == 'x':
                cells = [vp, cap, '', '']
            elif p in ('facebook', 'instagram', 'threads'):
                cells = [vp, cap]
            elif p == 'tiktok':
                cells = [vp, row['caption'] or '', row['hashtags'] or '', '', '']
            elif p == 'youtube':
                yt_tags = ', '.join(h.lstrip('#') for h in re.split(r'[\s,]+', row['hashtags'] or '') if h.strip())
                cells = [vp, judul[:95], cap, yt_tags, '', '', '', '0', '0']
            elif p == 'shopee':
                cells = [vp, judul[:80], row['caption'] or '', row['hashtags'] or '', row['link'] or '']
            else:
                cells = [vp, cap]
            cells = (cells + [''] * ncols)[:ncols]
            lines.append(';'.join(str(c).replace(';', ',').replace('\n', ' ') for c in cells))
        out_path = os.path.join(out_dir, f'upload_{p}.csv')
        with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
            f.write('\n'.join(lines) + '\n')
        created.append(out_path)
    return created, len(parsed)


ALL_UPLOAD_PLATFORMS = ['youtube', 'tiktok', 'facebook', 'instagram', 'threads', 'x', 'pinterest', 'shopee']


# Alamat halaman pencarian + bentuk tautan hasil, per platform.
# `pola` adalah regex JavaScript (dipakai apa adanya di dalam halaman), jadi
# ditulis lengkap dengan garis miring pembatasnya.
PENCARIAN_BROWSER = {
    'tiktok': {
        'nama': 'TikTok',
        'url':  'https://www.tiktok.com/search/video?q={q}',
        'pola': r'/\/@[^/]+\/video\/\d+/',
    },
    'instagram': {
        # Instagram tidak punya pencarian konten terbuka lewat URL, tapi
        # halaman tagar berfungsi dan hasilnya paling dekat dengan "kata kunci".
        'nama': 'Instagram',
        'url':  'https://www.instagram.com/explore/tags/{tag}/',
        'pola': r'/instagram\.com\/(p|reel)\/[A-Za-z0-9_-]+/',
    },
    'facebook': {
        'nama': 'Facebook',
        'url':  'https://www.facebook.com/search/videos/?q={q}',
        # Facebook punya tiga bentuk tautan video, dan dua di antaranya TIDAK
        # punya segmen sebelum katanya (facebook.com/watch/?v=..., /reel/...),
        # jadi pola ".*/(videos|watch)" meleset untuk keduanya.
        'pola': r'/facebook\.com\/(watch\/|reel\/|[^/]+\/videos\/)/',
    },
    'threads': {
        'nama': 'Threads',
        'url':  'https://www.threads.net/search?q={q}&serp_type=default',
        'pola': r'/threads\.(net|com)\/@[^/]+\/post\//',
    },
    'x': {
        'nama': 'X',
        'url':  'https://x.com/search?q={q}&f=video',
        'pola': r'/(x|twitter)\.com\/[^/]+\/status\/\d+/',
    },
    'pinterest': {
        'nama': 'Pinterest',
        'url':  'https://www.pinterest.com/search/pins/?q={q}',
        'pola': r'/pinterest\.[a-z.]+\/pin\/\d+/',
    },
}


@app.route('/api/downloader/cari_keyword', methods=['POST'])
def downloader_cari_keyword():
    """Cari video berdasarkan KATA KUNCI lalu kembalikan daftar URL-nya.

    Sengaja TIDAK langsung mengunduh: hasilnya dikirim balik sebagai daftar
    URL untuk diisikan ke kotak URL (satu per baris). Dengan begitu user bisa
    membuang yang tidak relevan dulu — hasil pencarian selalu bercampur — dan
    pengunduhannya memakai jalur link yang sudah terbukti, bukan jalur baru.
    """
    data = request.get_json() or {}
    platform = (data.get('platform') or 'youtube').strip().lower()
    keyword = (data.get('keyword') or '').strip()
    try:
        limit = max(1, min(200, int(data.get('limit') or 20)))
    except (TypeError, ValueError):
        limit = 20
    if not keyword:
        return jsonify({'error': 'Kata kunci wajib diisi.'})

    # ── YouTube: yt-dlp punya pencarian bawaan (ytsearchN:) ──────────────
    if platform in ('youtube', 'yt'):
        try:
            import yt_dlp
            opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                    'skip_download': True, 'ignoreerrors': True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                hasil = ydl.extract_info(f'ytsearch{limit}:{keyword}', download=False)
            urls = []
            for e in (hasil or {}).get('entries') or []:
                if not e:
                    continue
                u = e.get('url') or e.get('webpage_url') or ''
                if u and not u.startswith('http'):
                    u = f'https://www.youtube.com/watch?v={u}'
                if u:
                    urls.append(u)
            urls = list(dict.fromkeys(urls))[:limit]
            if not urls:
                return jsonify({'error': f'Tidak ada hasil YouTube untuk "{keyword}".'})
            return jsonify({'success': True, 'platform': 'youtube',
                            'keyword': keyword, 'urls': urls, 'count': len(urls)})
        except Exception as e:
            return jsonify({'error': f'Pencarian YouTube gagal: {e}'})

    # ── Platform lain: lewat browser profil yang sudah login ─────────────
    #
    # Semuanya mengunci hasil pencarian untuk pengunjung anonim, dan yt-dlp
    # tidak punya pencarian untuk satu pun di antaranya. Jadi jalurnya sama:
    # buka halaman pencarian di profil Chrome yang sudah login, lalu kumpulkan
    # tautan yang bentuknya sesuai.
    if platform in PENCARIAN_BROWSER:
        info = PENCARIAN_BROWSER[platform]
        profile = (data.get('profile') or '').strip()
        if not profile or profile not in list_profile_dirs():
            return jsonify({'error': (f'Pencarian {info["nama"]} butuh profil Chrome '
                                      f'yang sudah login. Pilih profilnya dulu.')})
        chrome = get_chrome_path()
        jalur_profil = os.path.join(PROFILES_DIR, profile)
        if not chrome or not os.path.isdir(jalur_profil):
            return jsonify({'error': 'Chrome atau folder profil tidak ditemukan.'})
        pembaca = None
        try:
            import tiktok_downloader as _ttdl
            from urllib.parse import quote
            pembaca = _ttdl.PembacaHalaman(chrome, jalur_profil, log=print)
            pembaca.__enter__()
            if platform == 'tiktok':
                urls = pembaca.daftar_dari_pencarian(keyword, batas=limit)
            else:
                urls = pembaca.daftar_tautan(
                    info['url'].format(q=quote(keyword),
                                       tag=quote(keyword.replace(' ', ''))),
                    info['pola'], batas=limit)
        except Exception as e:
            return jsonify({'error': f'Pencarian {info["nama"]} gagal: {e}'})
        finally:
            if pembaca:
                try:
                    pembaca.__exit__()
                except Exception:
                    pass
        if not urls:
            return jsonify({'error': (f'Tidak ada hasil {info["nama"]} untuk '
                                      f'"{keyword}". Bisa jadi profilnya belum '
                                      'login, halaman meminta verifikasi, atau '
                                      'kata kuncinya terlalu sempit.')})
        return jsonify({'success': True, 'platform': platform,
                        'keyword': keyword, 'urls': urls, 'count': len(urls)})

    if platform == 'shopee':
        return jsonify({'error': ('Shopee mengunci feed videonya hanya di aplikasi, '
                                  'jadi pencarian kata kunci tidak bisa lewat '
                                  'browser. Pakai mode Per Akun (Username) yang '
                                  'berjalan lewat HP/ADB.')})

    return jsonify({'error': f'Platform "{platform}" belum dikenali.'})


@app.route('/api/downloader/convert_csv', methods=['POST'])
def downloader_convert_csv():
    """Ubah CSV hasil download (rekap_video.csv / upload_tiktok.csv) menjadi CSV
    siap-upload untuk platform terpilih (FB/IG/Threads/X, dll)."""
    data = request.get_json() or {}
    csv_path = (data.get('csv_path') or '').strip()
    platforms = data.get('platforms') or []
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': f'File CSV tidak ditemukan: {csv_path}'})
    if not platforms:
        return jsonify({'error': 'Pilih minimal satu platform tujuan.'})
    try:
        created, count = _convert_rekap_to_uploads(csv_path, platforms)
    except Exception as e:
        return jsonify({'error': f'Gagal konversi CSV: {e}'})
    if not count:
        return jsonify({'error': 'CSV kosong / tak dikenali.'})
    return jsonify({'success': True, 'files': created,
                    'folder': os.path.dirname(csv_path), 'count': count})


@app.route('/api/downloader/phones', methods=['GET'])
def downloader_phones():
    """Daftar HP Android terhubung (untuk download Shopee Video via ADB)."""
    adb = _resolve_adb()
    if not adb:
        return jsonify({'has_adb': False, 'devices': []})
    devs = [d for d in _adb_devices(adb) if d.get('state') == 'device']
    return jsonify({'has_adb': True, 'devices': devs})


@app.route('/api/downloader/open_folder', methods=['POST'])
def downloader_open_folder():
    platform = (request.get_json() or {}).get('platform') or 'generic'
    target = _download_target_dir(platform)
    try:
        os.startfile(target)
        return jsonify({'success': True, 'path': target})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/gmaps/scrape', methods=['POST'])
def gmaps_scrape():
    """Ekstraktor Google Maps: scrape kontak bisnis (nama/telepon/website/dll)."""
    data = request.get_json() or {}
    if not (data.get('keyword') or '').strip():
        return jsonify({'error': 'Kata kunci wajib diisi (mis. "kafe", "toko bangunan").'})
    job = {
        'keyword':  (data.get('keyword') or '').strip(),
        'location': (data.get('location') or '').strip(),
        'limit':    data.get('limit') or 20,
        'out_dir':  os.path.join(DOWNLOADS_DIR, 'gmaps'),
        'title':    'GMaps: ' + (data.get('keyword') or '')[:40],
    }
    job_id = _start_worker('gmaps_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/gmaps/scrape_photos', methods=['POST'])
def gmaps_scrape_photos():
    """Ambil foto yang di-upload PELANGGAN di kolom Ulasan satu bisnis (bukan
    foto resmi bisnisnya) - dipakai sebagai bahan gambar sumber di tab Generator.
    'place' boleh nama tempat biasa (di-search dulu) ATAU link Google Maps langsung."""
    data = request.get_json() or {}
    place = (data.get('place') or data.get('maps_url') or '').strip()
    if not place:
        return jsonify({'error': 'Nama tempat atau link Google Maps wajib diisi.'})
    name = (data.get('name') or '').strip()
    job = {
        'mode':     'photos',
        'place':    place,
        'name':     name,
        'limit':    data.get('limit') or 20,
        'sort_best_quality': bool(data.get('sort_best_quality')),
        'require_people':    bool(data.get('require_people')),
        'require_detail':    bool(data.get('require_detail')),
        'out_dir':  os.path.join(DOWNLOADS_DIR, 'gmaps_photos'),
        'title':    'Foto ulasan: ' + (name or place)[:40],
    }
    job_id = _start_worker('gmaps_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/gmaps/check_wa', methods=['POST'])
def gmaps_check_wa():
    """Cek daftar nomor: terdaftar di WhatsApp atau tidak (pakai sesi WA login)."""
    data = request.get_json() or {}
    session = (data.get('session') or '').strip()
    phones = data.get('phones') or []
    if not session:
        return jsonify({'error': 'Pilih sesi WhatsApp yang sudah login dulu.'})
    if not phones:
        return jsonify({'error': 'Tidak ada nomor untuk dicek.'})
    import wa_manager
    ok, reason = wa_manager.engine_available()
    if not ok:
        return jsonify({'error': reason})
    return jsonify(wa_manager.manager.check_numbers(session, phones))


@app.route('/api/gmaps/send_review_request', methods=['POST'])
def gmaps_send_review_request():
    """Kirim AJAKAN REVIEW (broadcast WA) ke kontak lead yang WA-nya aktif —
    minta pelanggan asli kasih review jujur ke Google Business milik user.
    BUKAN posting review otomatis (itu review palsu & tidak dibangun)."""
    data = request.get_json() or {}
    session = (data.get('session') or '').strip()
    contacts = data.get('contacts') or []          # [{phone, name}]
    message = (data.get('message') or '').strip()
    link = (data.get('link') or '').strip()
    media_path = _blast_media_path((data.get('media_path') or '').strip())
    if not session:
        return jsonify({'error': 'Pilih sesi WhatsApp yang sudah login dulu.'})
    if not contacts:
        return jsonify({'error': 'Tidak ada kontak (nomor WA aktif) untuk dikirimi.'})
    if not message:
        return jsonify({'error': 'Tulis pesan ajakan review dulu.'})

    ok, reason = wa_manager.engine_available()
    if not ok:
        return jsonify({'error': reason})

    delay = int(load_chatgrow_settings().get('blast_delay', 8) or 8)

    def _runner(update_state):
        rows = [{'title': (c.get('name') or c.get('phone') or f'#{i+1}'), 'status': 'antre'}
                for i, c in enumerate(contacts)]
        update_state('Menyiapkan broadcast ajakan review...', 0, rows=rows)
        sent = 0
        total = len(contacts)
        for i, c in enumerate(contacts):
            phone = re.sub(r'\D+', '', str(c.get('phone') or ''))
            if not phone:
                rows[i]['status'] = 'nomor kosong'
                update_state(pct=int((i + 1) / total * 100), rows=rows)
                continue
            text = chatgrow.render_blast_text(
                message, {'nama': c.get('name', ''), 'name': c.get('name', ''),
                          'telepon': phone, 'link': link})
            rows[i]['status'] = 'kirim...'
            update_state(f'Kirim {i+1}/{total} ke {c.get("name") or phone}...',
                         int(i / total * 100), rows=rows)
            try:
                if media_path:
                    res = wa_manager.manager.send_media(session, phone, media_path, text)
                else:
                    res = wa_manager.manager.send_text(session, phone, text)
                if res.get('error'):
                    rows[i]['status'] = 'gagal: ' + str(res['error'])[:40]
                else:
                    sent += 1
                    rows[i]['status'] = 'terkirim'
            except Exception as e:
                rows[i]['status'] = 'gagal: ' + str(e)[:40]
            update_state(pct=int((i + 1) / total * 100), rows=rows)
            if i < total - 1:
                time.sleep(delay)
        update_state(f'Selesai! {sent}/{total} ajakan review terkirim.',
                     100, done=True, success=(sent > 0),
                     error='' if sent else 'Semua pengiriman gagal.', rows=rows)

    job_id = _start_thread_job(_runner, title=f'Ajakan Review ({len(contacts)} kontak)')
    return jsonify({'success': True, 'job_id': job_id})



@app.route('/api/gmaps/open_folder', methods=['POST'])
def gmaps_open_folder():
    target = os.path.join(DOWNLOADS_DIR, 'gmaps')
    os.makedirs(target, exist_ok=True)
    try:
        os.startfile(target)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/scraper/scrape', methods=['POST'])
def scraper_scrape():
    data = request.get_json() or {}
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_id = 'job_scrape_' + datetime.now().strftime('%Y%m%d%H%M%S') + '_' + os.urandom(3).hex()
    status_path = os.path.join(JOBS_DIR, job_id + '.status.json')
    
    with open(status_path, 'w', encoding='utf-8') as f:
        json.dump({'message': 'Antre...', 'done': False, 'success': False, 'error': '', 'pct': 0}, f)
        
    data['status_path'] = status_path
    
    t = threading.Thread(target=_run_scrape_job_thread, args=(job_id, data), daemon=True)
    t.start()
    
    with _yt_jobs_lock:
        _yt_jobs[job_id] = {
            'thread':      t,
            'status_path': status_path,
            'profile':     data.get('profile', ''),
            'title':       data.get('keyword', '') or data.get('url', '')[:60],
        }
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/api/generate/video', methods=['POST'])
def generate_video():
    data = request.get_json() or {}
    prompts = [str(p).strip() for p in (data.get('prompt_variants') or []) if str(p).strip()]
    if not (data.get('prompt') or '').strip() and not prompts:
        return jsonify({'error': 'Teks prompt video wajib diisi.'})
    job_id = _start_worker('video_generator_bridge.py', data)
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/generate/image', methods=['POST'])
def generate_image():
    data = request.get_json() or {}
    prompts = [str(p).strip() for p in (data.get('prompt_variants') or []) if str(p).strip()]
    if not (data.get('prompt') or '').strip() and not prompts:
        return jsonify({'error': 'Teks prompt image wajib diisi.'})
    model = (data.get('model') or '').strip()
    if 'banana' not in model.lower():
        model = 'Nano Banana 2'
    data['model'] = model
    job_id = _start_worker('video_generator_bridge.py', data)
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/api/generate/check_credits', methods=['POST'])
def generate_check_credits():
    """Cek limit/credit Google Labs Flow per akun terpilih (worker terpisah)."""
    data = request.get_json() or {}
    if not data.get('profiles'):
        return jsonify({'error': 'Pilih minimal satu Google Profile.'})
    data['action'] = 'check_credits'
    job_id = _start_worker('video_generator_bridge.py', data)
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/api/generate/analyze_scene_refs', methods=['POST'])
def generate_analyze_scene_refs():
    """Analisis gambar referensi tiap scene via Gemini Vision.

    Mengembalikan deskripsi visual konkret (subjek, warna, komposisi, mood)
    untuk disisipkan ke image prompt Nano Banana sehingga hasil generate
    lebih mengikuti foto referensi dan konsisten antar scene.

    Request: { image_paths: [...] }
    Response: { success: true, descriptions: [{index, summary, subject, colors, composition, mood}, ...] }
    """
    data = request.get_json() or {}
    image_paths = [str(p).strip() for p in (data.get('image_paths') or []) if str(p).strip()]
    if not image_paths:
        return jsonify({'error': 'image_paths wajib diisi.'})

    settings = load_omnivora_settings()
    gemini_keys = settings.get('gemini_api_keys', []) or [settings.get('gemini_api_key', '')]
    gemini_keys = [k for k in gemini_keys if k]
    if not gemini_keys:
        return jsonify({'error': 'Gemini API Key belum diisi. Buka Generate \u2192 Pengaturan lalu isi API Key Gemini.'})

    missing = [p for p in image_paths if not os.path.isfile(p)]
    if missing:
        return jsonify({'error': f'File gambar tidak ditemukan: {missing[0]}'})

    try:
        from omnivora.ai_caption import GeminiCaptionGenerator
        gen = GeminiCaptionGenerator(
            api_keys=gemini_keys,
            model=settings.get('gemini_model') or 'gemini-flash-latest',
            log_cb=lambda m, l='info': None,
        )
        descriptions = []
        for idx, img_path in enumerate(image_paths):
            result = gen.analyze_reference_image(img_path)
            descriptions.append({
                'index': idx,
                'image_path': img_path,
                'subject': result.get('subject', ''),
                'colors': result.get('colors', ''),
                'composition': result.get('composition', ''),
                'mood': result.get('mood', ''),
                'summary': result.get('summary', ''),
                'ok': bool(result.get('summary')),
            })
        success_count = sum(1 for d in descriptions if d['ok'])
        return jsonify({
            'success': True,
            'descriptions': descriptions,
            'message': f'{success_count}/{len(image_paths)} gambar berhasil dianalisis.'
        })
    except Exception as e:
        return jsonify({'error': f'Analisis gambar referensi error: {e}'})

@app.route('/api/generate/prompt', methods=['POST'])
def generate_prompt_from_image():
    """Prompt Generator: analisa gambar via Gemini Vision \u2192 prompt siap pakai
    untuk Video Generator (Flow) atau Image Generator (Nano Banana).
    Gaya prompt mengikuti Prompt Library OMNIVORA + gaya sosmed non-jualan."""
    data = request.get_json() or {}
    image_path = (data.get('image_path') or '').strip()
    if not image_path:
        return jsonify({'error': 'Pilih file gambar dulu.'})
    if not os.path.isfile(image_path):
        return jsonify({'error': f'File gambar tidak ditemukan: {image_path}'})

    settings = load_omnivora_settings()
    gemini_keys = settings.get('gemini_api_keys', []) or [settings.get('gemini_api_key', '')]
    gemini_keys = [k for k in gemini_keys if k]
    if not gemini_keys:
        return jsonify({'error': 'Gemini API Key belum diisi. Buka Generate \u2192 Pengaturan lalu isi API Key Gemini (Prompt Generator butuh Gemini Vision).'})

    try:
        from omnivora.ai_caption import GeminiCaptionGenerator
        gen = GeminiCaptionGenerator(
            api_keys=gemini_keys,
            # Default flash-latest: model lama (2.0-flash) sering habis kuota / tak
            # tersedia untuk key baru; generate_visual_prompt tetap punya fallback
            # multi-model kalau model ini pun gagal.
            model=settings.get('gemini_model') or 'gemini-flash-latest',
            log_cb=lambda m, l='info': None,
        )
        result = gen.generate_visual_prompt(
            image_path=image_path,
            mode=(data.get('mode') or 'video'),
            style=(data.get('style') or 'hard_selling'),
            platform=(data.get('platform') or 'tiktok'),
            lang=(data.get('lang') or 'id'),
            duration=(data.get('duration') or '8s'),
            extra=(data.get('extra') or ''),
        )
        if not result.get('prompt'):
            return jsonify({'error': gen.last_error or 'Gagal generate prompt dari gambar.'})
        return jsonify({'success': True, 'prompt': result['prompt']})
    except Exception as e:
        return jsonify({'error': f'Prompt generator error: {e}'})

@app.route('/api/generate/prompt_from_video', methods=['POST'])
def generate_prompt_from_video():
    """Video → Prompt: Gemini mengamati video contoh lalu membuat prompt foto
    (frame awal) + prompt image-to-video yang menirunya semirip mungkin."""
    data = request.get_json() or {}
    video_path = (data.get('video_path') or '').strip()
    if not video_path:
        return jsonify({'error': 'Pilih file video contoh dulu.'})
    if not os.path.isfile(video_path):
        return jsonify({'error': f'File video tidak ditemukan: {video_path}'})

    settings = load_omnivora_settings()
    gemini_keys = settings.get('gemini_api_keys', []) or [settings.get('gemini_api_key', '')]
    gemini_keys = [k for k in gemini_keys if k]
    if not gemini_keys:
        return jsonify({'error': 'Gemini API Key belum diisi. Buka Generate → Pengaturan lalu isi API Key Gemini.'})

    try:
        from omnivora.ai_caption import GeminiCaptionGenerator
        gen = GeminiCaptionGenerator(
            api_keys=gemini_keys,
            model=settings.get('gemini_model') or 'gemini-flash-latest',
            log_cb=lambda m, l='info': None,
        )
        result = gen.generate_prompts_from_video(
            video_path=video_path,
            platform=(data.get('platform') or 'tiktok'),
            lang=(data.get('lang') or 'id'),
            duration=(data.get('duration') or '8s'),
            extra=(data.get('extra') or ''),
        )
        if not (result.get('video_prompt') or result.get('image_prompt')):
            return jsonify({'error': gen.last_error or 'Gagal menganalisa video.'})
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'error': f'Analisa video error: {e}'})

@app.route('/api/youtube/job/<job_id>/stop', methods=['POST'])
def stop_job(job_id):
    """Hentikan job worker (subprocess) yang sedang berjalan."""
    with _yt_jobs_lock:
        info = _yt_jobs.get(job_id)
    if not info:
        return jsonify({'error': 'Job tidak ditemukan.'})
    proc = info.get('proc')
    if not proc:
        ev = info.get('stop_event')
        if ev is not None:
            ev.set()
            return jsonify({'success': True, 'message': 'Permintaan stop dikirim — job berhenti setelah item berjalan selesai.'})
        return jsonify({'error': 'Job ini tidak bisa dihentikan (berjalan sebagai thread).'})
    if proc.poll() is not None:
        return jsonify({'success': True, 'message': 'Job sudah selesai.'})

    stop_path = info.get('stop_path')
    if stop_path:
        # Stop KOOPERATIF (mis. Shopee Video "limit 0/unlimited"): worker
        # sendiri cek flag ini di tengah loop-nya dan berhenti RAPI (proses
        # hasil yang sudah didapat sampai titik itu, tulis CSV, baru keluar
        # normal) — TIDAK di-kill di sini. proc.terminate() langsung motong
        # proses SEBELUM sempat menyimpan apa pun (video sudah ke-cache di HP
        # tapi belum direkonstruksi jadi mp4), makanya dulu hasil stop = nihil
        # walau sudah setengah jalan. Jaga-jaga kalau worker tidak merespons
        # (macet), thread latar belakang hard-kill setelah tenggat.
        try:
            with open(stop_path, 'w', encoding='utf-8') as f:
                f.write('stop')
        except Exception as e:
            return jsonify({'error': f'Gagal mengirim sinyal stop: {e}'})

        def _force_kill_if_stuck(p, jid, delay=180):
            try:
                p.wait(timeout=delay)
                return  # keluar sendiri dengan rapi, tidak perlu apa-apa lagi
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                return
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            with _yt_jobs_lock:
                inf = _yt_jobs.get(jid)
            sp = inf.get('status_path') if inf else None
            if sp and os.path.isfile(sp):
                try:
                    with open(sp, 'r', encoding='utf-8') as f:
                        st = json.load(f)
                except Exception:
                    st = {}
                if not st.get('done'):
                    st.update({'done': True, 'success': False,
                               'error': 'Dihentikan paksa — worker tidak merespons permintaan stop.',
                               'message': 'Job dihentikan paksa.'})
                    try:
                        with open(sp, 'w', encoding='utf-8') as f:
                            json.dump(st, f, ensure_ascii=False)
                    except Exception:
                        pass

        threading.Thread(target=_force_kill_if_stuck, args=(proc, job_id), daemon=True).start()
        return jsonify({'success': True,
                        'message': 'Stop diminta — video yang sudah diambil sampai sekarang akan tetap diproses & disimpan, mohon tunggu sebentar...'})

    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        return jsonify({'error': f'Gagal menghentikan job: {e}'})
    # Tandai status file sebagai dihentikan agar UI berhenti polling dengan rapi
    status_path = info.get('status_path')
    if status_path and os.path.isfile(status_path):
        try:
            with open(status_path, 'r', encoding='utf-8') as f:
                status = json.load(f)
        except Exception:
            status = {}
        status.update({'done': True, 'success': False,
                       'error': 'Dihentikan oleh pengguna.',
                       'message': 'Job dihentikan oleh pengguna.'})
        try:
            with open(status_path, 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False)
        except Exception:
            pass
    return jsonify({'success': True})


@app.route('/api/youtube/job/<job_id>/resume', methods=['POST'])
def resume_job(job_id):
    """Kirim sinyal resume ke job yang sedang pause karena unusual activity.
    Menulis file flag resume per-slot agar semua thread paralel bisa resume,
    bukan hanya satu thread yang berhasil membaca file global."""
    with _yt_jobs_lock:
        info = _yt_jobs.get(job_id)
    if not info:
        return jsonify({'error': 'Job tidak ditemukan.'})
    status_path = info.get('status_path', '')
    if not status_path:
        return jsonify({'error': 'Status path tidak ditemukan.'})
    try:
        # Hitung jumlah slot aktif dari job file
        total_slots = 1
        job_path = info.get('job_path', '')
        if job_path and os.path.isfile(job_path):
            try:
                with open(job_path, 'r', encoding='utf-8') as f:
                    job_data = json.load(f)
                variant_count = int(job_data.get('variant_count', 0) or 0)
                runs = int(job_data.get('runs', 1) or 1)
                profiles = job_data.get('profiles', [])
                if not profiles and job_data.get('profile'):
                    profiles = [job_data['profile']]
                total_slots = max(variant_count, runs * max(len(profiles), 1), 1)
                total_slots = min(total_slots, 20)  # batas wajar
            except Exception:
                total_slots = 1

        # Tulis file resume global (backward-compat)
        resume_path = status_path.replace('.json', '_resume.flag')
        with open(resume_path, 'w', encoding='utf-8') as f:
            f.write('resume')

        # Tulis file resume per-slot agar semua thread paralel bisa resume
        for slot in range(total_slots):
            slot_path = status_path.replace('.json', f'_resume_slot{slot}.flag')
            try:
                with open(slot_path, 'w', encoding='utf-8') as f:
                    f.write('resume')
            except Exception:
                pass

        return jsonify({'success': True, 'message': 'Resume signal dikirim — GAMA akan melanjutkan download.'})
    except Exception as e:
        return jsonify({'error': f'Gagal menulis resume flag: {e}'})


# ─── Routes: file hasil scrape/generate (folder output) ─────────────────────

OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

@app.route('/output/<path:filename>')
def serve_output(filename):
    """Serve gambar/file hasil scrape agar bisa dipreview di UI."""
    return send_from_directory(OUTPUT_DIR, filename)

@app.route('/api/reveal_folder', methods=['POST'])
def reveal_folder():
    """Buka folder tempat sebuah file hasil generate berada (aplikasi desktop
    lokal). Berbeda dari /api/open_folder yang dibatasi ke OUTPUT_DIR — hasil
    generate bisa mendarat di folder asal gambar mana pun."""
    data = request.get_json() or {}
    raw = (data.get('path') or '').strip()
    if not raw:
        return jsonify({'error': 'Path kosong.'})
    target = os.path.abspath(raw)
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    if not os.path.isdir(folder):
        return jsonify({'error': 'Folder tidak ditemukan.'})
    try:
        os.startfile(folder)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


def cleanup_gmaps_and_generated_images(termasuk_gmaps: bool = True):
    """Bersihkan sampah foto hasil generate, dan OPSIONAL foto download GMaps.

    `termasuk_gmaps=False` dipakai saat GAMA dibuka. Sebelumnya foto hasil
    download Google Maps ikut dihapus di setiap start, jadi hasil scrape
    kemarin lenyap begitu aplikasi dibuka lagi — padahal foto itu bahan konten
    yang masih dipakai, bukan sampah. Pembersihannya sekarang hanya lewat
    tombol manual, yaitu ketika user memang meminta.
    """
    lingkup = ("foto GMaps & hasil generate" if termasuk_gmaps
               else "hasil generate saja (foto GMaps dipertahankan)")
    print(f"[Cleanup] Memulai pembersihan: {lingkup}...")
    cleaned_count = 0
    try:
        # 1. Folder download GMaps (downloads/gmaps, downloads/gmaps_photos)
        #
        # PENJAGA GANDA. Selain parameter, ada pemeriksaan pemanggil di sini:
        # foto GMaps hanya boleh dihapus lewat permintaan MANUAL user. Kalau
        # suatu saat ada kode baru memanggil fungsi ini tanpa sadar (mis. di
        # startup lagi), penghapusannya tetap tidak jalan.
        import inspect
        dari_manual = any(
            f.function == 'api_clean_temp_files' for f in inspect.stack()[1:6])
        if termasuk_gmaps and not dari_manual:
            print("[Cleanup] Penghapusan foto GMaps DILEWATI — hanya boleh "
                  "lewat permintaan manual user.")
            termasuk_gmaps = False

        downloads_base = os.path.join(BASE_DIR, 'downloads')
        if termasuk_gmaps and os.path.exists(downloads_base):
            for item in os.listdir(downloads_base):
                if item.lower().startswith('gmaps'):
                    target_path = os.path.join(downloads_base, item)
                    try:
                        if os.path.isdir(target_path):
                            for sub in os.listdir(target_path):
                                sub_p = os.path.join(target_path, sub)
                                if os.path.isdir(sub_p):
                                    shutil.rmtree(sub_p, ignore_errors=True)
                                else:
                                    os.remove(sub_p)
                                cleaned_count += 1
                        else:
                            os.remove(target_path)
                            cleaned_count += 1
                    except Exception as e:
                        print(f"[Cleanup] Gagal membersihkan {target_path}: {e}")

        # 2. Bersihkan staging downloads & foto hasil generate Nano Banana
        from omnivora.environment import runtime_data_root
        output_dirs = {
            os.path.join(BASE_DIR, 'output'),
            str(runtime_data_root() / 'output'),
        }

        for out_dir in output_dirs:
            if not os.path.exists(out_dir):
                continue
            # Bersihkan staging _downloads
            staging = os.path.join(out_dir, '_downloads')
            if os.path.exists(staging):
                try:
                    for sub in os.listdir(staging):
                        sub_p = os.path.join(staging, sub)
                        if os.path.isdir(sub_p):
                            shutil.rmtree(sub_p, ignore_errors=True)
                        else:
                            os.remove(sub_p)
                        cleaned_count += 1
                except Exception:
                    pass

            # Bersihkan _debug_flow
            dbg = os.path.join(out_dir, '_debug_flow')
            if os.path.exists(dbg):
                try:
                    shutil.rmtree(dbg, ignore_errors=True)
                except Exception:
                    pass

            # Bersihkan foto sisa hasil generate (Nano Banana).
            #
            # Syaratnya SEKARANG dua-duanya harus benar: berekstensi GAMBAR
            # DAN berawalan scene_/nano_/img_gen_. Aturan lama memakai "atau",
            # sehingga:
            #   - scene_01_..._.txt  (metadata caption/hashtag) ikut terhapus
            #   - scene_01_..._.mp4  (VIDEO hasil generate!) ikut terhapus,
            #     karena video disimpan di sebelah foto sumbernya — dan foto
            #     sumbernya sering berada di folder output ini juga
            #   - foto apa pun milik user yang kebetulan ditaruh di output/
            # Menghapus hasil kerja jauh lebih mahal daripada menyisakan
            # beberapa berkas sampah, jadi syaratnya diperketat.
            GAMBAR = ('.jpg', '.jpeg', '.png', '.webp')
            for root, dirs, files in os.walk(out_dir, topdown=False):
                if os.path.basename(root) == '_downloads':
                    continue
                for f in files:
                    lower = f.lower()
                    if (lower.startswith(('scene_', 'nano_', 'img_gen_'))
                            and lower.endswith(GAMBAR)):
                        file_p = os.path.join(root, f)
                        try:
                            os.remove(file_p)
                            cleaned_count += 1
                        except Exception:
                            pass
                # Hapus folder kosong di bawah output/ (kecuali _downloads)
                if root != out_dir and os.path.basename(root) != '_downloads':
                    try:
                        if not os.listdir(root):
                            os.rmdir(root)
                    except Exception:
                        pass
        print(f"[Cleanup] Pembersihan selesai ({cleaned_count} item/folder dibersihkan).")
        return cleaned_count
    except Exception as e:
        print(f"[Cleanup] Error saat pembersihan: {e}")
        return 0


@app.route('/api/system/clean_temp_files', methods=['POST'])
def api_clean_temp_files():
    """Pembersihan sampah secara MANUAL.

    `termasuk_gmaps` harus diminta EKSPLISIT. Bawaannya False supaya foto hasil
    download Google Maps tidak ikut terhapus tanpa diminta — foto itu bahan
    konten, dan menghapusnya tidak bisa dibatalkan.
    """
    data = request.get_json(silent=True) or {}
    termasuk_gmaps = bool(data.get('termasuk_gmaps', False))
    count = cleanup_gmaps_and_generated_images(termasuk_gmaps=termasuk_gmaps)
    pesan = f'Berhasil membersihkan {count} item/folder sampah.'
    if not termasuk_gmaps:
        pesan += ' Foto hasil download Google Maps dipertahankan.'
    return jsonify({'success': True, 'cleaned_count': count,
                    'gmaps_ikut_dihapus': termasuk_gmaps, 'message': pesan})


@app.route('/api/preview_image')
def preview_image():
    """Serve satu file gambar lokal untuk preview thumbnail di UI (aplikasi
    desktop lokal). Dibatasi ke ekstensi gambar & file yang benar-benar ada."""
    raw = (request.args.get('path') or '').strip()
    if not raw:
        return ('', 400)
    target = os.path.abspath(raw)
    if os.path.splitext(target)[1].lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        return ('', 400)
    if not os.path.isfile(target):
        return ('', 404)
    return send_from_directory(os.path.dirname(target), os.path.basename(target))


@app.route('/api/download_file')
def download_file():
    """Download file gambar hasil generate langsung ke browser sebagai attachment.
    Dipakai tombol Download di panel hasil Image Generator."""
    raw = (request.args.get('path') or '').strip()
    if not raw:
        return ('', 400)
    target = os.path.abspath(raw)
    ext = os.path.splitext(target)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        return ('', 400)
    if not os.path.isfile(target):
        return ('File tidak ditemukan — mungkin sudah dibersihkan. Klik "Buka Folder" sebelum restart GAMA.', 404)
    return send_from_directory(
        os.path.dirname(target),
        os.path.basename(target),
        as_attachment=True,
        download_name=os.path.basename(target),
    )

@app.route('/api/open_folder', methods=['POST'])
def open_folder():
    """Buka folder hasil di Windows Explorer (dibatasi ke folder output GAM)."""
    data = request.get_json() or {}
    raw = (data.get('path') or '').strip()
    if not raw:
        return jsonify({'error': 'Path kosong.'})
    target = os.path.abspath(raw)
    if os.path.isfile(target):
        target = os.path.dirname(target)
    if not target.lower().startswith(OUTPUT_DIR.lower()):
        return jsonify({'error': 'Hanya folder output GAM yang boleh dibuka.'})
    if not os.path.isdir(target):
        return jsonify({'error': 'Folder tidak ditemukan.'})
    try:
        os.startfile(target)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})

# ─── Scrape & Blast + Klien WhatsApp (tab Chat) ─────────────────────────────
# Semua data hasil scrape disimpan di folder khusus: data/chatgrow/ (lihat chatgrow.py)

import wa_manager
import tg_manager
import chatgrow

CHATGROW_SETTINGS_FILE = os.path.join(DATA_DIR, 'chatgrow_settings.json')


def _blast_media_path(raw_path: str) -> str:
    path = os.path.abspath((raw_path or '').strip()) if (raw_path or '').strip() else ''
    if path and not os.path.isfile(path):
        raise FileNotFoundError(f'File media tidak ditemukan: {path}')
    return path


def _manual_target_rows(platform: str, targets: list) -> list[dict]:
    rows = []
    for raw in targets or []:
        raw_target = str(raw or '').strip()
        target = raw_target
        if not target:
            continue
        if platform == 'whatsapp' and not (target.endswith('@g.us') or target.endswith('@s.whatsapp.net')):
            target = ''.join(ch for ch in target if ch.isdigit())
        target_kind = 'group' if (platform == 'whatsapp' and raw_target.endswith('@g.us')) else 'personal'
        rows.append({'target': target, 'target_kind': target_kind})
    return rows


def _group_target_rows(groups: list) -> list[dict]:
    rows = []
    for item in groups or []:
        if isinstance(item, dict):
            target = str(item.get('target') or '').strip()
            label = str(item.get('label') or target).strip()
        else:
            target = str(item or '').strip()
            label = target
        if target:
            rows.append({'target': target, 'name': label, 'source': 'group_list', 'target_kind': 'group'})
    return rows


def _apply_personal_blast_range(rows: list[dict], start_value=None, end_value=None) -> tuple[list[dict], dict]:
    personal_positions = [
        idx for idx, row in enumerate(rows)
        if str(row.get('target_kind') or 'personal') != 'group'
    ]
    total_personal = len(personal_positions)
    if not total_personal:
        return rows, {'enabled': False, 'total_personal': 0, 'start': 0, 'end': 0}

    if start_value in (None, '') and end_value in (None, ''):
        return rows, {'enabled': False, 'total_personal': total_personal, 'start': 1, 'end': total_personal}

    try:
        start = max(1, int(start_value or 1))
        end = min(total_personal, int(end_value or total_personal))
    except (TypeError, ValueError):
        raise ValueError('Range blast personal harus berupa angka.')
    if start > total_personal:
        raise ValueError(f'Nomor mulai personal melebihi jumlah target ({total_personal}).')
    if start > end:
        raise ValueError('Nomor mulai personal tidak boleh lebih besar dari nomor akhir.')

    keep_personal_positions = set(personal_positions[start - 1:end])
    filtered = [
        row for idx, row in enumerate(rows)
        if str(row.get('target_kind') or 'personal') == 'group' or idx in keep_personal_positions
    ]
    return filtered, {'enabled': True, 'total_personal': total_personal, 'start': start, 'end': end}


def _merge_blast_rows(platform: str, *, manual_targets=None, csv_file='', csv_path='', group_targets=None, group_list_name='') -> list[dict]:
    rows = []
    if csv_file:
        rows.extend(chatgrow.read_blast_rows(os.path.join(chatgrow.SCRAPE_DIR, csv_file), platform))
    if csv_path:
        rows.extend(chatgrow.read_blast_rows(csv_path, platform))
    rows.extend(_manual_target_rows(platform, manual_targets or []))
    rows.extend(_group_target_rows(group_targets or []))
    if group_list_name:
        saved = chatgrow.get_group_list(group_list_name, platform)
        if saved:
            rows.extend(_group_target_rows(saved.get('groups', [])))
    seen = set()
    uniq = []
    for row in rows:
        target = str(row.get('target') or '').strip()
        if not target or target in seen:
            continue
        seen.add(target)
        uniq.append(row)
    return uniq


def _render_blast_message(message: str, row: dict) -> str:
    base = str(message or '').strip() or str(row.get('message') or '').strip()
    return chatgrow.render_blast_text(base, row).strip()


def _send_blast(platform: str, session: str, target: str, message: str, media_path: str):
    if platform == 'telegram':
        if media_path:
            return tg_manager.manager.send_media(session, target, media_path, message)
        tg_manager.manager.send_message(session, target, message)
        return {'success': True}
    if media_path:
        return wa_manager.manager.send_media(session, target, media_path, message)
    return wa_manager.manager.send_text(session, target, message)


def load_chatgrow_settings() -> dict:
    default = {
        'tg_api_id': tg_manager.DEFAULT_API_ID,
        'tg_api_hash': tg_manager.DEFAULT_API_HASH,
        'blast_delay': 8,          # jeda antar pesan blast (detik) — hindari ban
    }
    if os.path.isfile(CHATGROW_SETTINGS_FILE):
        try:
            with open(CHATGROW_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return default


def _apply_tg_api():
    cfg = load_chatgrow_settings()
    tg_manager.manager.set_api(cfg.get('tg_api_id'), cfg.get('tg_api_hash'))
    return cfg


def _start_thread_job(target, title=''):
    """Jalankan fungsi target(update_state) sebagai job thread; kompatibel dengan
    polling /api/youtube/job/<id>. update_state(msg, pct, done, success, error, **extra)."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_id = 'job_cg_' + datetime.now().strftime('%Y%m%d%H%M%S') + '_' + os.urandom(3).hex()
    status_path = os.path.join(JOBS_DIR, job_id + '.status.json')
    state = {'message': 'Antre...', 'done': False, 'success': False, 'error': '', 'pct': 0}

    def _write():
        try:
            with open(status_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    def update_state(msg=None, pct=None, done=None, success=None, error=None, **extra):
        if msg is not None:
            state['message'] = msg
        if pct is not None:
            state['pct'] = pct
        if done is not None:
            state['done'] = done
        if success is not None:
            state['success'] = success
        if error is not None:
            state['error'] = error
        state.update(extra)
        _write()

    _write()

    # Event stop: worker bisa cek `update.stop_event` untuk cancel mid-process
    stop_event = threading.Event()
    update_state.stop_event = stop_event

    def _runner():
        try:
            target(update_state)
        except Exception as e:
            update_state(f'ERROR: {e}', done=True, success=False, error=str(e))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    with _yt_jobs_lock:
        _yt_jobs[job_id] = {'thread': t, 'status_path': status_path, 'profile': '', 'title': title,
                            'stop_event': stop_event}
    return job_id


# ── Settings Chat-Grow (API Telegram + jeda blast) ──────────────────────────

@app.route('/api/chatgrow/settings', methods=['GET', 'POST'])
def chatgrow_settings():
    if request.method == 'GET':
        return jsonify(load_chatgrow_settings())
    data = request.get_json() or {}
    cfg = load_chatgrow_settings()
    if 'tg_api_id' in data:
        try:
            cfg['tg_api_id'] = int(data['tg_api_id']) or tg_manager.DEFAULT_API_ID
        except (TypeError, ValueError):
            cfg['tg_api_id'] = tg_manager.DEFAULT_API_ID
    if 'tg_api_hash' in data:
        cfg['tg_api_hash'] = (data['tg_api_hash'] or '').strip() or tg_manager.DEFAULT_API_HASH
    if 'blast_delay' in data:
        try:
            cfg['blast_delay'] = max(1, int(data['blast_delay']))
        except (TypeError, ValueError):
            cfg['blast_delay'] = 8
    try:
        with open(CHATGROW_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        tg_manager.manager.set_api(cfg['tg_api_id'], cfg['tg_api_hash'])
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/chatgrow/files', methods=['GET', 'DELETE'])
def chatgrow_files():
    if request.method == 'DELETE':
        data = request.get_json() or {}
        ok = chatgrow.delete_scrape_file(data.get('file') or '')
        return jsonify({'success': ok})
    return jsonify({'files': chatgrow.list_scrape_files()})


@app.route('/api/chatgrow/csv_template')
def chatgrow_csv_template():
    platform = (request.args.get('platform') or 'whatsapp').strip().lower()
    csv_text = chatgrow.csv_template_text(platform)
    return Response(
        csv_text,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=template_blast_{platform}.csv'},
    )


@app.route('/api/chatgrow/blast_templates', methods=['GET', 'POST', 'DELETE'])
def chatgrow_blast_templates():
    if request.method == 'GET':
        platform = (request.args.get('platform') or '').strip().lower()
        return jsonify({'templates': chatgrow.list_blast_templates(platform)})
    data = request.get_json() or {}
    if request.method == 'DELETE':
        ok = chatgrow.delete_blast_template(data.get('name') or '', data.get('platform') or '')
        return jsonify({'success': ok})
    try:
        item = chatgrow.save_blast_template(
            data.get('name') or '',
            data.get('platform') or '',
            data.get('message') or '',
            data.get('media_path') or '',
        )
        return jsonify({'success': True, 'template': item})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/chatgrow/group_lists', methods=['GET', 'POST', 'DELETE'])
def chatgrow_group_lists():
    if request.method == 'GET':
        platform = (request.args.get('platform') or '').strip().lower()
        return jsonify({'group_lists': chatgrow.list_group_lists(platform)})
    data = request.get_json() or {}
    if request.method == 'DELETE':
        ok = chatgrow.delete_group_list(data.get('name') or '', data.get('platform') or '')
        return jsonify({'success': ok})
    try:
        item = chatgrow.save_group_list(
            data.get('name') or '',
            data.get('platform') or '',
            data.get('session') or '',
            data.get('groups') or [],
        )
        return jsonify({'success': True, 'group_list': item})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/chatgrow/open_folder', methods=['POST'])
def chatgrow_open_folder():
    chatgrow.ensure_dirs()
    try:
        os.startfile(chatgrow.SCRAPE_DIR)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/chatgrow/open_url', methods=['POST'])
def chatgrow_open_url():
    """Buka URL bantuan (mis. my.telegram.org) di browser sistem.
    Dibatasi ke host yang diizinkan agar aman."""
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    allowed = ('https://my.telegram.org', 'https://core.telegram.org')
    if not url.startswith(allowed):
        return jsonify({'error': 'URL tidak diizinkan.'})
    try:
        webbrowser.open(url)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


# ── WhatsApp: klien chat ────────────────────────────────────────────────────

@app.route('/api/wa/status')
def wa_status():
    ok, reason = wa_manager.engine_available()
    if ok:
        wa_manager.manager.startup()
    return jsonify({'available': ok, 'reason': reason,
                    'sessions': wa_manager.manager.list_sessions() if ok else []})


@app.route('/api/wa/sessions', methods=['POST'])
def wa_add_session():
    ok, reason = wa_manager.engine_available()
    if not ok:
        return jsonify({'error': reason})
    data = request.get_json() or {}
    try:
        s = wa_manager.manager.add_session(data.get('name') or '')
        return jsonify({'success': True, 'session': s.info()})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/wa/sessions/<name>/qr')
def wa_session_qr(name):
    return jsonify(wa_manager.manager.qr_data_uri(name))


@app.route('/api/wa/sessions/<name>/disconnect', methods=['POST'])
def wa_session_disconnect(name):
    return jsonify(wa_manager.manager.disconnect(name))


@app.route('/api/wa/sessions/<name>/logout', methods=['POST'])
def wa_session_logout(name):
    return jsonify(wa_manager.manager.logout(name))


@app.route('/api/wa/chats/<name>')
def wa_chats(name):
    return jsonify({'chats': wa_manager.manager.chats(name)})


@app.route('/api/wa/messages/<name>')
def wa_messages(name):
    jid = (request.args.get('jid') or '').strip()
    if not jid:
        return jsonify({'error': 'jid wajib diisi.'})
    try:
        limit = min(200, int(request.args.get('limit') or 50))
    except (ValueError, TypeError):
        limit = 50
    return jsonify({'messages': wa_manager.manager.messages(name, jid, limit)})


@app.route('/api/wa/read', methods=['POST'])
def wa_read():
    data = request.get_json() or {}
    wa_manager.manager.mark_read(data.get('session') or '', data.get('jid') or '')
    return jsonify({'success': True})


@app.route('/api/wa/send', methods=['POST'])
def wa_send():
    data = request.get_json() or {}
    session = data.get('session') or ''
    jid = data.get('jid') or ''
    text = data.get('text') or ''
    media = (data.get('media_path') or '').strip()
    if media:
        return jsonify(wa_manager.manager.send_media(session, jid, media, text))
    return jsonify(wa_manager.manager.send_text(session, jid, text))


@app.route('/api/wa/newchat', methods=['POST'])
def wa_newchat():
    data = request.get_json() or {}
    return jsonify(wa_manager.manager.new_chat(
        data.get('session') or '', data.get('phone') or ''))


@app.route('/api/wa/delete_chat', methods=['POST'])
def wa_delete_chat():
    data = request.get_json() or {}
    return jsonify(wa_manager.manager.delete_chat_synced(
        data.get('session') or '', data.get('jid') or ''))


@app.route('/api/wa/sync/<name>', methods=['POST'])
def wa_sync(name):
    return jsonify(wa_manager.manager.force_sync(name))


@app.route('/api/wa/resync/<name>', methods=['POST'])
def wa_resync(name):
    return jsonify(wa_manager.manager.resync_history(name))


# ── WhatsApp: grup, scrape member, blast ────────────────────────────────────

@app.route('/api/wa/groups/<name>')
def wa_groups(name):
    return jsonify(wa_manager.manager.list_groups(name))


@app.route('/api/wa/scrape', methods=['POST'])
def wa_scrape():
    data = request.get_json() or {}
    session = data.get('session') or ''
    group_jid = data.get('group_jid') or ''
    mode = (data.get('mode') or 'members').strip()   # 'members' | 'channel' | 'history'
    if not session:
        return jsonify({'error': 'Pilih sesi WA dulu.'})
    if mode == 'members' and not group_jid:
        return jsonify({'error': 'Pilih grup dulu.'})

    def _job(update):
        if mode == 'history':
            update('Membaca histori chat tersimpan...', pct=40)
            res = wa_manager.manager.scrape_chat_history(session, group_jid)
        else:
            update('Mengambil member grup...', pct=30)
            res = wa_manager.manager.scrape_group_members(session, group_jid)
        if res.get('error'):
            update(res['error'], done=True, success=False, error=res['error'])
            return
        members = res.get('members', [])
        save_name = (data.get('save_name') or '').strip()
        path = chatgrow.write_members_csv('whatsapp', res.get('label', group_jid or 'chat'), members, save_name)
        update(f'{len(members)} kontak disimpan.', pct=100, done=True, success=True,
               file=os.path.basename(path), count=len(members))

    return jsonify({'success': True, 'job_id': _start_thread_job(_job, 'Scrape WA')})


@app.route('/api/wa/blast', methods=['POST'])
def wa_blast():
    data = request.get_json() or {}
    session = data.get('session') or ''
    message = (data.get('message') or '').strip()
    csv_file = data.get('csv_file') or ''
    csv_path = (data.get('csv_path') or '').strip()
    group_list_name = (data.get('group_list_name') or '').strip()
    group_targets = data.get('group_targets') or []
    media_path = ''
    if not session:
        return jsonify({'error': 'Pilih sesi WA dulu.'})
    try:
        media_path = _blast_media_path(data.get('media_path') or '')
    except Exception as e:
        return jsonify({'error': str(e)})
    rows = _merge_blast_rows(
        'whatsapp',
        manual_targets=data.get('targets') or [],
        csv_file=csv_file,
        csv_path=csv_path,
        group_targets=group_targets,
        group_list_name=group_list_name,
    )
    try:
        rows, range_info = _apply_personal_blast_range(rows, data.get('personal_start'), data.get('personal_end'))
    except Exception as e:
        return jsonify({'error': str(e)})
    has_row_message = any(str(r.get('message') or '').strip() for r in rows)
    if not message and not media_path and not has_row_message:
        return jsonify({'error': 'Isi pesan blast, isi kolom message di CSV, atau pilih media dulu.'})
    if not rows:
        return jsonify({'error': 'Tidak ada target. Pilih file CSV, isi target manual, atau pilih grup.'})
    if range_info.get('enabled') and not any(str(r.get('target_kind') or 'personal') != 'group' for r in rows):
        return jsonify({'error': 'Range personal tidak menyisakan target personal yang bisa dikirim.'})

    delay = load_chatgrow_settings().get('blast_delay', 8)

    def _job(update):
        total = len(rows)
        ok = 0
        errors = 0
        err_samples = []
        for i, row in enumerate(rows, 1):
            target = str(row.get('target') or '').strip()
            jid = target if target.endswith('@g.us') or target.endswith('@s.whatsapp.net') else (''.join(ch for ch in target if ch.isdigit()) + '@s.whatsapp.net')
            final_message = _render_blast_message(message, row)
            res = _send_blast('whatsapp', session, jid, final_message, media_path)
            if res.get('success'):
                ok += 1
            else:
                errors += 1
                if res.get('error') and len(err_samples) < 3:
                    label = row.get('name') or target
                    err_samples.append(f'{label}: {res["error"]}')
            note = ('\n' + '\n'.join(err_samples)) if err_samples else ''
            update(f'Blast {i}/{total} - berhasil {ok} - gagal {errors}{note}', pct=int(i / total * 100))
            if i < total:
                time.sleep(delay)
        final = f'Blast selesai: {ok}/{total} terkirim.'
        if err_samples:
            final += '\nContoh gagal:\n' + '\n'.join(err_samples)
        update(final, pct=100, done=True, success=True,
               sent=ok, failed=errors, total=total, error='\n'.join(err_samples))

    return jsonify({'success': True, 'job_id': _start_thread_job(_job, 'Blast WA')})


@app.route('/api/tg/status')
def tg_status():
    ok, reason = tg_manager.engine_available()
    if ok:
        _apply_tg_api()
    return jsonify({'available': ok, 'reason': reason,
                    'sessions': tg_manager.manager.list_sessions() if ok else []})


@app.route('/api/tg/login', methods=['POST'])
def tg_login():
    ok, reason = tg_manager.engine_available()
    if not ok:
        return jsonify({'error': reason})
    _apply_tg_api()
    data = request.get_json() or {}
    return jsonify(tg_manager.manager.start_login(data.get('name') or '', data.get('phone') or ''))


@app.route('/api/tg/verify_code', methods=['POST'])
def tg_verify_code():
    data = request.get_json() or {}
    return jsonify(tg_manager.manager.verify_code(data.get('name') or '', data.get('code') or ''))


@app.route('/api/tg/verify_2fa', methods=['POST'])
def tg_verify_2fa():
    data = request.get_json() or {}
    return jsonify(tg_manager.manager.verify_2fa(data.get('name') or '', data.get('password') or ''))


@app.route('/api/tg/logout', methods=['POST'])
def tg_logout():
    data = request.get_json() or {}
    return jsonify(tg_manager.manager.logout(data.get('name') or ''))


@app.route('/api/tg/dialogs/<name>')
def tg_dialogs(name):
    _apply_tg_api()
    return jsonify(tg_manager.manager.list_dialogs(name))


@app.route('/api/tg/chats/<name>')
def tg_chats(name):
    _apply_tg_api()
    return jsonify(tg_manager.manager.chat_dialogs(name))


@app.route('/api/tg/topics/<name>')
def tg_topics(name):
    _apply_tg_api()
    target = (request.args.get('target') or '').strip()
    if not target:
        return jsonify({'error': 'target grup/channel wajib diisi.'})
    return jsonify(tg_manager.manager.list_topics(name, target))


@app.route('/api/tg/messages/<name>')
def tg_messages(name):
    peer = (request.args.get('peer') or '').strip()
    if not peer:
        return jsonify({'error': 'peer wajib diisi.'})
    try:
        limit = min(200, int(request.args.get('limit') or 50))
    except (ValueError, TypeError):
        limit = 50
    topic_id = (request.args.get('topic_id') or '').strip() or None
    return jsonify(tg_manager.manager.get_history(name, peer, limit, topic_id=topic_id))


@app.route('/api/tg/read', methods=['POST'])
def tg_read():
    data = request.get_json() or {}
    return jsonify(tg_manager.manager.mark_read(data.get('name') or '', data.get('peer') or ''))


@app.route('/api/tg/delete_chat', methods=['POST'])
def tg_delete_chat():
    data = request.get_json() or {}
    return jsonify(tg_manager.manager.delete_dialog(data.get('name') or '', data.get('peer') or ''))


@app.route('/api/tg/send', methods=['POST'])
def tg_send():
    data = request.get_json() or {}
    session = data.get('name') or ''
    peer = (data.get('peer') or '').strip()
    text = (data.get('text') or '').strip()
    media = (data.get('media_path') or '').strip()
    topic_id = data.get('topic_id') or None
    if not session or not peer or (not text and not media):
        return jsonify({'error': 'Sesi, tujuan, dan teks/media wajib diisi.'})
    try:
        if media:
            tg_manager.manager.send_media(session, peer, media, text, topic_id=topic_id)
        else:
            tg_manager.manager.send_message(session, peer, text, topic_id=topic_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/tg/scrape', methods=['POST'])
def tg_scrape():
    _apply_tg_api()
    data = request.get_json() or {}
    session = data.get('name') or ''
    target = (data.get('target') or '').strip()
    mode = (data.get('mode') or 'members').strip()   # 'members' | 'history'
    topic_id = data.get('topic_id') or None  # opsional: scrape histori 1 topic forum saja
    if not session or not target:
        return jsonify({'error': 'Pilih sesi Telegram dan target grup/channel dulu.'})

    def _job(update):
        if mode == 'history':
            hint = f' (topic #{topic_id})' if topic_id else ''
            update(f'Menghubungkan & membaca histori chat{hint}...', pct=20)
            def prog(n, label):
                update(f'{n} kontak unik dari histori "{label}"{hint}...', pct=60)
            label, members = tg_manager.manager.scrape_history(session, target, progress=prog, topic_id=topic_id)
        elif mode == 'channel':
            update('Menghubungkan & membaca target channel...', pct=20)
            def prog(n, label):
                update(f'{n} data channel terbaca dari "{label}"...', pct=60)
            label, members = tg_manager.manager.scrape_channel(session, target, progress=prog)
        else:
            update('Menghubungkan & mengambil member...', pct=20)
            def prog(n, label):
                update(f'{n} member terbaca dari "{label}"...', pct=60)
            label, members = tg_manager.manager.scrape_members(session, target, progress=prog)
        save_name = (data.get('save_name') or '').strip()
        path = chatgrow.write_members_csv('telegram', label, members, save_name)
        update(f'{len(members)} kontak disimpan.', pct=100, done=True, success=True,
               file=os.path.basename(path), count=len(members))

    return jsonify({'success': True, 'job_id': _start_thread_job(_job, 'Scrape TG')})


@app.route('/api/tg/blast', methods=['POST'])
def tg_blast():
    _apply_tg_api()
    data = request.get_json() or {}
    session = data.get('name') or ''
    message = (data.get('message') or '').strip()
    csv_file = data.get('csv_file') or ''
    csv_path = (data.get('csv_path') or '').strip()
    group_list_name = (data.get('group_list_name') or '').strip()
    group_targets = data.get('group_targets') or []
    if not session:
        return jsonify({'error': 'Pilih sesi Telegram dulu.'})
    try:
        media_path = _blast_media_path(data.get('media_path') or '')
    except Exception as e:
        return jsonify({'error': str(e)})
    rows = _merge_blast_rows(
        'telegram',
        manual_targets=data.get('targets') or [],
        csv_file=csv_file,
        csv_path=csv_path,
        group_targets=group_targets,
        group_list_name=group_list_name,
    )
    try:
        rows, range_info = _apply_personal_blast_range(rows, data.get('personal_start'), data.get('personal_end'))
    except Exception as e:
        return jsonify({'error': str(e)})
    has_row_message = any(str(r.get('message') or '').strip() for r in rows)
    if not message and not media_path and not has_row_message:
        return jsonify({'error': 'Isi pesan blast, isi kolom message di CSV, atau pilih media dulu.'})
    if not rows:
        return jsonify({'error': 'Tidak ada target. Pilih file CSV, isi target manual, atau pilih grup.'})
    if range_info.get('enabled') and not any(str(r.get('target_kind') or 'personal') != 'group' for r in rows):
        return jsonify({'error': 'Range personal tidak menyisakan target personal yang bisa dikirim.'})

    delay = load_chatgrow_settings().get('blast_delay', 8)

    def _job(update):
        total = len(rows)
        ok = 0
        errors = 0
        for i, row in enumerate(rows, 1):
            target = str(row.get('target') or '').strip()
            final_message = _render_blast_message(message, row)
            try:
                res = _send_blast('telegram', session, target, final_message, media_path)
            except Exception as e:
                res = {'error': str(e)}
            if res.get('success'):
                ok += 1
            else:
                errors += 1
            update(f'Blast {i}/{total} - berhasil {ok} - gagal {errors}', pct=int(i / total * 100))
            if i < total:
                time.sleep(delay)
        update(f'Blast selesai: {ok}/{total} terkirim.', pct=100, done=True, success=True,
               sent=ok, failed=errors, total=total)

    return jsonify({'success': True, 'job_id': _start_thread_job(_job, 'Blast TG')})


# ─── Repurpose (port inti LUMINA: download + reframe + split + frames) ───────

import repurpose


@app.route('/api/repurpose/status')
def repurpose_status():
    ok, reason = repurpose.tools_available()
    ft_ok, ft_reason = repurpose.face_tracker_available()
    return jsonify({
        'available': ok,
        'reason': reason,
        'face_tracker_available': ft_ok,
        'face_tracker_reason': ft_reason,
    })


@app.route('/api/repurpose/run', methods=['POST'])
def repurpose_run():
    data = request.get_json() or {}
    source = (data.get('source') or '').strip()
    sources = [str(p).strip() for p in (data.get('sources') or []) if str(p).strip()]
    if not source and not sources:
        return jsonify({'error': 'Tambahkan video dulu sebelum memulai split.'})
    ok, reason = repurpose.tools_available()
    if not ok:
        return jsonify({'error': reason})
    job = {
        'source': source,
        'sources': sources,
        'is_url': bool(data.get('is_url')),
        'ratio': data.get('ratio') or '9:16',
        'split_mode': data.get('split_mode') or 'count',
        'seg_duration': data.get('seg_duration') or 60,
        'seg_count': data.get('seg_count') or 5,
        'frames': bool(data.get('frames')),
        'frame_interval': data.get('frame_interval') or 10,
        'face_tracker': bool(data.get('face_tracker')),
        'turbo': bool(data.get('turbo', True)),
        'output_dir': data.get('output_dir') or '',
        'title': data.get('title') or '',
        # Identitas konten untuk penamaan folder & berkas hasil render.
        # Tanpa diteruskan di sini, nilai dari UI berhenti di endpoint dan
        # penamaannya kembali memakai nama berkas sumber.
        'label': data.get('label') or '',
        'niche': data.get('niche') or '',
    }
    job_id = _start_thread_job(lambda update: repurpose.run_repurpose(job, update), 'Repurpose')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/repurpose/open_folder', methods=['POST'])
def repurpose_open_folder():
    data = request.get_json() or {}
    target = (data.get('path') or repurpose.EXPORT_DIR).strip()
    target = os.path.abspath(target)
    os.makedirs(target, exist_ok=True)
    try:
        os.startfile(target)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/repurpose/list_videos', methods=['POST'])
def repurpose_list_videos():
    data = request.get_json() or {}
    folder = os.path.abspath((data.get('folder') or '').strip())
    if not folder or not os.path.isdir(folder):
        return jsonify({'error': 'Folder tidak ditemukan.'})
    files = repurpose.list_videos_in_folder(folder)
    return jsonify({'success': True, 'files': files})

# ─── Repurpose: port fitur LUMINA (capture, img2video, transkrip, mixer) ─────

@app.route('/api/repurpose/effects')
def repurpose_effects():
    return jsonify({'effects': repurpose.CINEMATIC_EFFECTS})


@app.route('/api/repurpose/smart_capture', methods=['POST'])
def repurpose_smart_capture():
    data = request.get_json() or {}
    if not (data.get('source') or '').strip():
        return jsonify({'error': 'Pilih file video atau isi URL dulu.'})
    ok, reason = repurpose.tools_available()
    if not ok:
        return jsonify({'error': reason})
    job = {
        'source': data.get('source'),
        'is_url': bool(data.get('is_url')),
        'interval': data.get('interval') or 10,
        'ratio': data.get('ratio') or '9:16',
        'face_tracker': bool(data.get('face_tracker')),
        'output_dir': data.get('output_dir') or '',
    }
    job_id = _start_thread_job(lambda update: repurpose.smart_capture(job, update), 'Capture Image')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/repurpose/img2video', methods=['POST'])
def repurpose_img2video():
    data = request.get_json() or {}
    images = [str(p).strip() for p in (data.get('images') or []) if str(p).strip()]
    if not images:
        return jsonify({'error': 'Pilih minimal 1 gambar dulu.'})
    ok, reason = repurpose.tools_available()
    if not ok:
        return jsonify({'error': reason})
    job = {
        'images': images,
        'effect': data.get('effect') or 'Zoom In',
        'duration': data.get('duration') or 5,
        'ratio': data.get('ratio') or '9:16',
        'output_dir': data.get('output_dir') or '',
        'crop_x': data.get('crop_x') or 0.5,
        'crop_y': data.get('crop_y') or 0.5,
    }
    job_id = _start_thread_job(lambda update: repurpose.images_to_video(job, update), 'Image to Video')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/repurpose/mix', methods=['POST'])
def repurpose_mix():
    data = request.get_json() or {}
    ok, reason = repurpose.tools_available()
    if not ok:
        return jsonify({'error': reason})
    settings = load_omnivora_settings()
    gemini_keys = settings.get('gemini_api_keys', []) or [settings.get('gemini_api_key', '')]
    gemini_keys = [k for k in gemini_keys if k]
    job = {
        'mode': data.get('mode') or 'acak',
        'folder': data.get('folder') or '',
        'folder1': data.get('folder1') or '',
        'folder2': data.get('folder2') or '',
        'count': data.get('count', 1),  # 0 = auto (habiskan stok clip)
        'ratio': data.get('ratio') or '9:16',
        'resolution': data.get('resolution') or '1080p',
        'shuffle': bool(data.get('shuffle', True)),
        'delete_used': bool(data.get('delete_used')),
        'audios': data.get('audios') or [],
        'audio_vol': data.get('audio_vol', 100),
        'audio_pitch': data.get('audio_pitch', 0),
        'backsound_path': data.get('backsound_path') or '',
        'backsound_vol': data.get('backsound_vol', 50),
        'wm_path': data.get('wm_path') or '',
        'wm_scale': data.get('wm_scale', 0.15),
        'wm_opacity': data.get('wm_opacity', 1.0),
        'wm_x_pct': data.get('wm_x_pct', 0.85),
        'wm_y_pct': data.get('wm_y_pct', 0.05),
        'output_dir': data.get('output_dir') or '',
        'videos': data.get('videos') or [],
        'vo_path': data.get('vo_path') or '',
        'vo_files': data.get('vo_files') or [],
        'generate_caption': data.get('generate_caption', True),
        'caption_platform': data.get('caption_platform') or 'tiktok',
        'caption_content_type': data.get('caption_content_type') or 'auto',
        'caption_extra': data.get('caption_extra') or '',
        'gemini_keys': gemini_keys,
        'gemini_model': settings.get('gemini_model') or 'gemini-flash-latest',
    }
    job_id = _start_thread_job(lambda update: repurpose.run_video_mixer(job, update), 'Video Mixer')
    return jsonify({'success': True, 'job_id': job_id})


def _gemini_keys_or_error():
    settings = load_omnivora_settings()
    keys = settings.get('gemini_api_keys', []) or [settings.get('gemini_api_key', '')]
    keys = [k for k in keys if k]
    return keys, settings


def _gemini_plain_call(keys, parts, model=None, max_tokens=8192):
    """generateContent teks polos (bukan JSON) dengan rotasi key + fallback model."""
    import httpx
    models = [m for m in [model, 'gemini-flash-latest', 'gemini-2.5-flash', 'gemini-2.0-flash'] if m]
    last_err = 'Gemini tidak merespons.'
    for mdl in dict.fromkeys(models):
        for key in keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}'
            payload = {'contents': [{'parts': parts}],
                       'generationConfig': {'temperature': 0.4, 'maxOutputTokens': max_tokens}}
            try:
                with httpx.Client(timeout=120.0) as client:
                    r = client.post(url, json=payload)
                    if r.status_code in (429, 503):
                        last_err = f'Gemini {mdl} sibuk/kuota habis (HTTP {r.status_code}).'
                        continue
                    if r.status_code != 200:
                        last_err = f'Gemini HTTP {r.status_code}: {r.text[:200]}'
                        continue
                    cands = (r.json() or {}).get('candidates', [])
                    text = ''.join(p.get('text', '') for c in cands[:1]
                                   for p in c.get('content', {}).get('parts', []))
                    if text.strip():
                        return text.strip(), ''
                    last_err = 'Gemini mengembalikan konten kosong (safety filter?).'
            except Exception as e:
                last_err = str(e)
    return '', last_err


@app.route('/api/repurpose/transcribe', methods=['POST'])
def repurpose_transcribe():
    """Transkrip video/audio via Gemini (audio diupload ke Files API)."""
    data = request.get_json() or {}
    source = (data.get('source') or '').strip()
    if not source or not os.path.isfile(source):
        return jsonify({'error': 'Pilih file video/audio dulu.'})
    keys, settings = _gemini_keys_or_error()
    if not keys:
        return jsonify({'error': 'Gemini API Key belum diisi. Buka Generate → Pengaturan lalu isi API Key Gemini.'})

    def _runner(update):
        update('Ekstrak audio...', pct=5)
        tmp_audio = os.path.join(repurpose.DOWNLOAD_DIR, '_transcribe_tmp.mp3')
        os.makedirs(repurpose.DOWNLOAD_DIR, exist_ok=True)
        import subprocess as sp
        cmd = [repurpose.FFMPEG, '-y', '-i', source, '-vn', '-acodec', 'libmp3lame',
               '-b:a', '96k', '-ac', '1', tmp_audio]
        sp.run(cmd, capture_output=True, creationflags=repurpose._NOWIN)
        audio_path = tmp_audio if os.path.isfile(tmp_audio) and os.path.getsize(tmp_audio) > 1000 else source

        update('Upload audio ke Gemini...', pct=25)
        from omnivora.ai_caption import GeminiCaptionGenerator
        gen = GeminiCaptionGenerator(api_keys=keys, model=settings.get('gemini_model') or 'gemini-flash-latest',
                                     log_cb=lambda m, l='info': None)
        uri = err = None
        used_key = keys[0] if keys else None
        for key in keys:
            uri, err = gen._upload_media_to_gemini(key, audio_path, 'audio/mpeg')
            if uri:
                used_key = key
                break
        if not uri:
            raise RuntimeError(f'Upload audio gagal: {err}')

        update('Gemini mentranskrip...', pct=55)
        parts = [
            {'file_data': {'file_uri': uri, 'mime_type': 'audio/mpeg'}},
            {'text': 'Transkrip audio ini apa adanya dalam bahasa aslinya. '
                     'Keluarkan HANYA teks transkrip (tanpa timestamp, tanpa penjelasan).'},
        ]
        text, terr = _gemini_plain_call([used_key] + [k for k in keys if k != used_key], parts,
                                        model=settings.get('gemini_model'))
        if not text:
            raise RuntimeError(f'Transkrip gagal: {terr}')
        update('Transkrip selesai.', pct=100, done=True, success=True, text=text)

    job_id = _start_thread_job(_runner, 'Transkrip')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/repurpose/rewrite', methods=['POST'])
def repurpose_rewrite():
    """Rewrite naskah via Gemini dengan gaya + target durasi (port LUMINA)."""
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Teks naskah masih kosong.'})
    keys, settings = _gemini_keys_or_error()
    if not keys:
        return jsonify({'error': 'Gemini API Key belum diisi. Buka Generate → Pengaturan lalu isi API Key Gemini.'})
    style = data.get('style') or 'Professional'
    dur_min = float(data.get('duration_minutes') or 1.0)
    wpm = 150  # kecepatan baca narasi rata-rata
    target_words = max(30, int(dur_min * wpm))
    prompt = (
        f'Tulis ulang naskah narasi berikut dengan gaya "{style}". '
        f'Target durasi baca sekitar {dur_min:.1f} menit (±{target_words} kata). '
        'Pertahankan bahasa aslinya, buat mengalir natural untuk voice over, '
        'tanpa heading/bullet/emoji — keluarkan HANYA naskah hasil rewrite.\n\n'
        f'NASKAH ASLI:\n{text}'
    )
    out, err = _gemini_plain_call(keys, [{'text': prompt}], model=settings.get('gemini_model'))
    if not out:
        return jsonify({'error': f'Rewrite gagal: {err}'})
    return jsonify({'success': True, 'text': out})


@app.route('/api/repurpose/vo_generate', methods=['POST'])
def repurpose_vo_generate():
    """Generate Voice Over otomatis lewat Gemini Canvas (browser real, akun
    Google terpilih) - tempel naskah+setting, kirim, tunggu audio, download.
    Worker subprocess (bukan thread) karena ini otomasi Chrome sungguhan,
    supaya crash/hang di browser tidak menjatuhkan seluruh app (sama seperti
    video_generator_bridge.py / gmaps_bridge.py)."""
    data = request.get_json() or {}
    email = (data.get('email') or '').strip()
    if not email:
        return jsonify({'error': 'Pilih akun Gmail dulu.'})
    if email not in list_profile_dirs():
        return jsonify({'error': 'Profil Gmail tidak ditemukan.'})
    script = (data.get('script') or '').strip()
    if not script:
        return jsonify({'error': 'Naskah voice over masih kosong.'})
    out_dir = (data.get('out_dir') or '').strip() or os.path.join(BASE_DIR, 'exports', 'voice_over')
    job = {
        'email':   email,
        'script':  script,
        'voice':   data.get('voice') or 'Puck',
        'bahasa':  data.get('bahasa') or 'Indonesia',
        'gaya':    data.get('gaya') or 'natural',
        'out_dir': out_dir,
        'title':   'Voice Over: ' + email[:40],
    }
    job_id = _start_worker('voice_over_bridge.py', job)
    return jsonify({'success': True, 'job_id': job_id})


# ─── Clipper (auto potong klip pendek untuk Reels/TikTok/Shopee/IG) ──────────

import clipper


@app.route('/api/clipper/status')
def clipper_status():
    ok, reason = clipper.tools_available()
    try:
        import cv2 as _cv2
        opencv_ver = _cv2.__version__
        opencv_ok = True
    except Exception:
        opencv_ver = None
        opencv_ok = False
    return jsonify({'available': ok, 'reason': reason,
                    'opencv_available': opencv_ok, 'opencv_version': opencv_ver})


@app.route('/api/clipper/face_detect', methods=['POST'])
def clipper_face_detect():
    """Ambil 1 frame dari video pada detik `ts`, jalankan OpenCV face detection,
    kembalikan daftar kotak wajah + thumbnail JPEG base64 untuk preview di canvas."""
    data = request.get_json() or {}
    video = (data.get('video') or '').strip()
    try:
        ts = float(data.get('ts') or 0)
    except (ValueError, TypeError):
        ts = 0.0
    if not video or not os.path.isfile(video):
        return jsonify({'error': 'File video tidak ditemukan.'})
    try:
        import cv2 as _cv2
    except ImportError:
        return jsonify({'error': 'opencv-python belum terinstall. Jalankan: pip install opencv-python'})
    try:
        cap = _cv2.VideoCapture(video)
        if not cap.isOpened():
            return jsonify({'error': 'Gagal membuka file video.'})
        fps = cap.get(_cv2.CAP_PROP_FPS) or 25.0
        total = cap.get(_cv2.CAP_PROP_FRAME_COUNT) or 0
        dur = total / fps if fps else 0
        # kalau ts=0 ambil frame di 10% video biar ada konten
        if ts == 0 and dur > 5:
            ts = dur * 0.1
        cap.set(_cv2.CAP_PROP_POS_MSEC, ts * 1000)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return jsonify({'error': 'Gagal membaca frame dari video.'})
        frame_h, frame_w = frame.shape[:2]
        # Face detection
        gray = _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY)
        gray = _cv2.equalizeHist(gray)
        scale = 1.0
        if max(frame_w, frame_h) > 640:
            scale = 640.0 / max(frame_w, frame_h)
            small = _cv2.resize(gray, (int(frame_w*scale), int(frame_h*scale)))
        else:
            small = gray
        frontal = _cv2.CascadeClassifier(_cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        profile = _cv2.CascadeClassifier(_cv2.data.haarcascades + 'haarcascade_profileface.xml')
        detected = []
        for cascade in (frontal, profile):
            found = cascade.detectMultiScale(small, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))
            if len(found):
                for (x, y, w, h) in found.tolist():
                    inv = 1.0 / scale if scale != 1.0 else 1.0
                    detected.append([int(x*inv), int(y*inv), int(w*inv), int(h*inv)])
        # Draw boxes on frame thumbnail
        thumb = _cv2.resize(frame, (480, int(frame_h * 480 / frame_w)))
        sx = 480 / frame_w
        sy = (frame_h * 480 / frame_w) / frame_h
        for (x, y, w, h) in detected:
            _cv2.rectangle(thumb, (int(x*sx), int(y*sy)), (int((x+w)*sx), int((y+h)*sy)), (99, 102, 241), 2)
        _, buf = _cv2.imencode('.jpg', thumb, [_cv2.IMWRITE_JPEG_QUALITY, 80])
        import base64 as _b64
        b64 = _b64.b64encode(buf.tobytes()).decode()
        return jsonify({
            'faces': detected,
            'frame_w': frame_w,
            'frame_h': frame_h,
            'scale': scale,
            'frame_b64': b64,
            'ts': round(ts, 2),
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/clipper/run', methods=['POST'])
def clipper_run():
    data = request.get_json() or {}
    source = (data.get('source') or '').strip()
    if not source:
        return jsonify({'error': 'Isi URL video atau path file dulu.'})
    ok, reason = clipper.tools_available()
    if not ok:
        return jsonify({'error': reason})
    job = {
        'source': source,
        'is_url': bool(data.get('is_url')),
        'clip_duration': data.get('clip_duration') or 45,
        'clip_count': data.get('clip_count') or 3,
        'mode': data.get('mode') or 'auto',
        'ratio': data.get('ratio') or '9:16',
        'burn_subs': bool(data.get('burn_subs')),
        'sub_lang': data.get('sub_lang') or 'id',
        'title': data.get('title') or '',
    }
    job_id = _start_thread_job(lambda update: clipper.run_clipper(job, update), 'Clipper')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/clipper/analyze', methods=['POST'])
def clipper_analyze():
    """Fase 1 VideoClipperPro-style: siapkan video + cari kandidat klip.
    Hasil (clip_list, video_path, srt_path) diambil dari status job."""
    data = request.get_json() or {}
    source = (data.get('source') or '').strip()
    if not source:
        return jsonify({'error': 'Isi URL video atau path file dulu.'})
    ok, reason = clipper.tools_available()
    if not ok:
        return jsonify({'error': reason})
    settings = load_omnivora_settings()
    keys = settings.get('gemini_api_keys', []) or [settings.get('gemini_api_key', '')]
    job = {
        'source': source,
        'is_url': bool(data.get('is_url')),
        'clip_duration': data.get('clip_duration', 0),  # 0 = otomatis
        'clip_count': data.get('clip_count', 0),        # 0 = otomatis
        'mode': data.get('mode') or 'auto',
        'sub_lang': data.get('sub_lang') or 'id',
        'min_dur': data.get('min_dur') or 0,
        'max_dur': data.get('max_dur') or 0,
        'gemini_keys': [k for k in keys if k],
        'gemini_model': settings.get('gemini_model') or '',
    }
    job_id = _start_thread_job(lambda update: clipper.analyze_clips(job, update), 'Clipper Analisa')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/clipper/export', methods=['POST'])
def clipper_export():
    """Fase 2: export klip terpilih (reframe/face mode, caption, BGM, anti-detect)."""
    data = request.get_json() or {}
    if not (data.get('video') or '').strip():
        return jsonify({'error': 'Jalankan Analisa dulu untuk memilih video.'})
    if not data.get('clips'):
        return jsonify({'error': 'Pilih minimal 1 klip di daftar.'})
    ok, reason = clipper.tools_available()
    if not ok:
        return jsonify({'error': reason})
    job = {
        'video': data.get('video'),
        'srt': data.get('srt') or '',
        'clips': data.get('clips'),
        'ratio': data.get('ratio') or '9:16',
        'crop_pos': data.get('crop_pos') or 'center',
        'crop_custom_pct': data.get('crop_custom_pct', 0.5),
        'letterbox': bool(data.get('letterbox')),
        'face_mode': data.get('face_mode') or 'off',
        'caption_style': data.get('caption_style') or 'off',
        'caption_size': data.get('caption_size') or 15,
        'caption_color': data.get('caption_color') or 'kuning',
        'caption_outline': data.get('caption_outline') or 'hitam',
        'caption_pos': data.get('caption_pos') or 'bawah',
        'smart_meta': bool(data.get('smart_meta', True)),
        'bgm': data.get('bgm') or '',
        'bgm_vol': data.get('bgm_vol', 30),
        'orig_vol': data.get('orig_vol', 100),
        'bgm_fade_in': bool(data.get('bgm_fade_in', True)),
        'bgm_fade_out': bool(data.get('bgm_fade_out', True)),
        'anti_detect': bool(data.get('anti_detect')),
        'turbo': bool(data.get('turbo', True)),
        'name_pattern': data.get('name_pattern') or 'clip_{n}',
        'title': data.get('title') or '',
    }
    # Gemini keys untuk auto-transkrip caption bila video tidak punya subtitle
    _settings = load_omnivora_settings()
    _keys = _settings.get('gemini_api_keys', []) or [_settings.get('gemini_api_key', '')]
    job['gemini_keys'] = [k for k in _keys if k]
    job['gemini_model'] = _settings.get('gemini_model') or ''
    job['auto_upload_csv'] = bool(data.get('auto_upload_csv', True))
    job['upload_platforms'] = data.get('upload_platforms') or ALL_UPLOAD_PLATFORMS
    job_id = _start_thread_job(lambda update: clipper.export_clips(job, update), 'Clipper Export')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/clipper/batch', methods=['POST'])
def clipper_batch():
    """Clipper BATCH: multi-link / per-username / keyword → tiap video otomatis
    di-download, dianalisa, diexport, lalu digabung jadi rekap + CSV siap-upload."""
    data = request.get_json() or {}
    source_type = data.get('source_type') or 'multi_link'
    value = (data.get('source_value') or '').strip()
    if not value:
        return jsonify({'error': 'Isi daftar link / username / keyword dulu.'})
    ok, reason = clipper.tools_available()
    if not ok:
        return jsonify({'error': reason})
    _settings = load_omnivora_settings()
    _keys = [k for k in (_settings.get('gemini_api_keys', []) or [_settings.get('gemini_api_key', '')]) if k]
    job = {
        'source_type': source_type,
        'source_value': value,
        'videos_limit': data.get('videos_limit') or 5,
        'mode': data.get('mode') or 'auto',
        'clip_duration': data.get('clip_duration', 0),
        'clip_count': data.get('clip_count', 0),
        'ratio': data.get('ratio') or '9:16',
        'crop_pos': data.get('crop_pos') or 'center',
        'face_mode': data.get('face_mode') or 'off',
        'caption_style': data.get('caption_style') or 'off',
        'caption_size': data.get('caption_size') or 15,
        'caption_color': data.get('caption_color') or 'kuning',
        'caption_outline': data.get('caption_outline') or 'hitam',
        'caption_pos': data.get('caption_pos') or 'bawah',
        'smart_meta': bool(data.get('smart_meta', True)),
        'anti_detect': bool(data.get('anti_detect')),
        'turbo': bool(data.get('turbo', True)),
        'name_pattern': data.get('name_pattern') or 'clip_{n}',
        'gemini_keys': _keys,
        'gemini_model': _settings.get('gemini_model') or '',
        'upload_platforms': data.get('upload_platforms') or ALL_UPLOAD_PLATFORMS,
    }
    job_id = _start_thread_job(lambda update: clipper.run_batch(job, update), 'Clipper Batch')
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/clipper/open_folder', methods=['POST'])
def clipper_open_folder():
    data = request.get_json() or {}
    target = (data.get('path') or clipper.EXPORT_DIR).strip()
    target = os.path.abspath(target)
    if not target.lower().startswith(os.path.abspath(clipper.EXPORT_DIR).lower()):
        target = clipper.EXPORT_DIR
    os.makedirs(target, exist_ok=True)
    try:
        os.startfile(target)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/clipper/fetch_by_username', methods=['POST'])
def clipper_fetch_by_username():
    """Fetch daftar URL video dari channel/user tertentu via yt-dlp.
    Payload: { platform, username, max_count, sort }
    Return: { success, urls: [...], count } atau { error }"""
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    platform = (data.get('platform') or 'youtube').strip().lower()
    max_count = max(1, min(50, int(data.get('max_count') or 10)))
    sort = (data.get('sort') or 'date').strip()
    if not username:
        return jsonify({'error': 'Username tidak boleh kosong.'})
    try:
        urls = clipper.fetch_urls_by_username(platform, username, max_count, sort)
        return jsonify({'success': True, 'urls': urls, 'count': len(urls)})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/clipper/fetch_by_keyword', methods=['POST'])
def clipper_fetch_by_keyword():
    """Cari video berdasarkan keyword via yt-dlp.
    Payload: { platform, keyword, max_count, sort }
    Return: { success, urls: [...], count } atau { error }"""
    data = request.get_json() or {}
    keyword = (data.get('keyword') or '').strip()
    platform = (data.get('platform') or 'youtube').strip().lower()
    max_count = max(1, min(50, int(data.get('max_count') or 10)))
    sort = (data.get('sort') or 'relevance').strip()
    if not keyword:
        return jsonify({'error': 'Keyword tidak boleh kosong.'})
    try:
        urls = clipper.fetch_urls_by_keyword(platform, keyword, max_count, sort)
        return jsonify({'success': True, 'urls': urls, 'count': len(urls)})
    except Exception as e:
        return jsonify({'error': str(e)})


APP_PORT = 5000


def _pick_free_port(preferred=5000):
    """Cari port kosong mulai dari `preferred`. Pakai SO_EXCLUSIVEADDRUSE supaya
    ghost socket (handle port nyangkut di proses anak chrome/ffmpeg) terdeteksi
    dan GAMA otomatis geser ke port berikutnya, bukan hang."""
    import socket
    for port in range(preferred, preferred + 11):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(('127.0.0.1', port))
            s.close()
            return port
        except OSError:
            s.close()
            continue
    return preferred


def run_flask():
    # threaded=True: request panjang (mis. Prompt Generator memanggil Gemini)
    # tidak boleh memblokir polling status job lain.
    app.run(host='127.0.0.1', port=APP_PORT, debug=False, use_reloader=False, threaded=True)


class NativeApi:
    """Dipanggil dari JS via window.pywebview.api untuk dialog file native."""

    _NO_WINDOW = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW

    def clipboard_get(self):
        """Baca teks dari clipboard sistem (untuk Paste via klik kanan di
        pywebview, yang sering memblokir clipboard web)."""
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 '[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw'],
                capture_output=True, timeout=6, creationflags=self._NO_WINDOW)
            return r.stdout.decode('utf-8', 'replace').rstrip('\r\n')
        except Exception:
            return ''

    def clipboard_set(self, text=''):
        """Tulis teks ke clipboard sistem (untuk Copy/Cut via klik kanan). Teks
        dikirim sebagai base64 supaya aman untuk karakter non-ASCII/emoji."""
        try:
            import base64 as _b64
            b64 = _b64.b64encode(str(text).encode('utf-8')).decode('ascii')
            cmd = ("$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
                   + b64 + "')); Set-Clipboard -Value $t")
            subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                           timeout=6, creationflags=self._NO_WINDOW)
            return True
        except Exception:
            return False

    _PICK_FILE_TYPES = {
        'audio': ('Audio Files (*.mp3;*.wav;*.m4a;*.aac;*.ogg;*.flac)', 'All files (*.*)'),
        'video': ('Video Files (*.mp4;*.mov;*.mkv;*.webm;*.avi)', 'All files (*.*)'),
        'video_audio': ('Video/Audio Files (*.mp4;*.mov;*.mkv;*.webm;*.avi;*.mp3;*.wav;*.m4a;*.aac;*.ogg;*.flac)', 'All files (*.*)'),
        'image': ('Image Files (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif)', 'All files (*.*)'),
        'csv': ('CSV Files (*.csv)', 'All files (*.*)'),
        'media': ('Media Files (*.mp4;*.mov;*.mkv;*.webm;*.jpg;*.jpeg;*.png;*.webp)', 'All files (*.*)'),
        # Lampiran blast: bebas apa saja (image/video/dokumen) - "All files" jadi
        # filter default supaya tidak ada yang keblok/abu-abu, filter media cuma
        # opsi tambahan buat yang mau nyaring.
        'attachment': ('All files (*.*)', 'Media Files (*.mp4;*.mov;*.mkv;*.webm;*.jpg;*.jpeg;*.png;*.webp;*.pdf)'),
    }

    def pick_file(self, kind='video', filters=None):
        import webview
        file_types = tuple(filters) if filters else self._PICK_FILE_TYPES.get(kind, ())
        result = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, file_types=file_types)
        if isinstance(result, (list, tuple)) and result:
            return result[0]
        return None

    def pick_files(self, kind='video', filters=None):
        import webview
        file_types = tuple(filters) if filters else self._PICK_FILE_TYPES.get(kind, ())
        try:
            result = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=file_types)
        except Exception:
            return []
        if isinstance(result, (list, tuple)):
            return [p for p in result if isinstance(p, str)]
        return [result] if isinstance(result, str) and result else []

    def pick_folder(self):
        import webview
        try:
            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            return None
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return result if isinstance(result, str) and result else None
    def save_csv_template(self, kind='youtube', up_type='regular'):
        """Simpan template CSV via dialog Save native. Return path."""
        import webview
        try:
            result = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f'template_upload_{kind}.csv',
                file_types=('CSV (*.csv)',),
            )
        except Exception:
            return None
        # pywebview >= 5 mengembalikan tuple/list path; versi lama string
        if isinstance(result, (list, tuple)):
            result = result[0] if result else None
        if not result or not isinstance(result, str):
            return None
        path = result
        if not path.lower().endswith('.csv'):
            path += '.csv'
        # Pilih template yang sesuai
        if kind == 'tiktok_foto':
            tmpl = CSV_TEMPLATE_TT_FOTO
        elif kind == 'tiktok':
            tmpl = CSV_TEMPLATE_TT_AFFILIATE if up_type in ('affiliate', 'seller') else CSV_TEMPLATE_TT_REGULAR
        elif kind == 'facebook':
            tmpl = CSV_TEMPLATE_FB
        elif kind == 'instagram':
            tmpl = CSV_TEMPLATE_IG
        elif kind in CSV_TEMPLATE_SOCIAL:
            tmpl = CSV_TEMPLATE_SOCIAL[kind]
        else:
            tmpl = CSV_TEMPLATE_YT
        try:
            with open(path, 'w', encoding='utf-8-sig') as f:
                f.write(tmpl)
        except OSError:
            return None
        return path


if __name__ == '__main__':
    # Jalankan cleanup sampah di background thread agar tidak memperlambat startup GAMA
    # Foto GMaps SENGAJA tidak ikut dibersihkan saat start — itu bahan konten
    # user, bukan sampah. Penghapusannya hanya lewat tombol manual.
    threading.Thread(target=cleanup_gmaps_and_generated_images,
                     kwargs={'termasuk_gmaps': False}, daemon=True).start()
    force_web = '--web' in sys.argv
    APP_PORT = _pick_free_port(5000)


    if APP_PORT != 5000:
        print(f'Port 5000 terpakai/nyangkut — GAMA pindah ke port {APP_PORT}.')

    try:
        if force_web:
            raise ImportError
        import webview
        # Mode desktop — pywebview
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        import socket
        for _ in range(50):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.1)
            try:
                s.connect(('127.0.0.1', APP_PORT))
                s.close()
                break
            except Exception:
                time.sleep(0.1)

        icon_path = os.path.join(BASE_DIR, 'assets', 'logo.ico')
        webview.create_window(
            'GAMA — Google Account Manager & Automation',
            f'http://127.0.0.1:{APP_PORT}',
            width=1280, height=820,
            min_size=(900, 600),
            js_api=NativeApi(),
        )
        webview.start(icon=icon_path if os.path.isfile(icon_path) else None)
    except ImportError:
        # Mode web — buka di browser biasa
        print('Membuka GAMA (Google Account Manager & Automation) di browser...')
        print(f'Server: http://127.0.0.1:{APP_PORT}')
        threading.Timer(0.3, lambda: webbrowser.open(f'http://127.0.0.1:{APP_PORT}')).start()
        app.run(host='127.0.0.1', port=APP_PORT, debug=False, use_reloader=False, threaded=True)










