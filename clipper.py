"""
clipper.py — Auto Clipper untuk GAMA.

Bikin potongan video pendek (short/reels) otomatis dari video panjang
(YouTube dll) untuk upload ke Reels, TikTok, Shopee Video, dan Instagram.

Pipeline (ffmpeg/ffprobe + yt-dlp, tanpa dependency tambahan):
  1. Download video dari URL via yt-dlp (sekalian ambil subtitle/auto-caption)
  2. Analisis loudness audio (ebur128) untuk menemukan momen paling "hidup"
  3. Pilih N window highlight yang tidak saling tumpang tindih
     (mode 'auto') ATAU sebar merata sepanjang video (mode 'spread')
  4. Potong + reframe ke rasio target (default 9:16) per klip
  5. (opsional) Burn subtitle otomatis ke tiap klip

Hasil disimpan ke exports/clipper/<judul>_<timestamp>/.
"""
import glob
import math
import os
import re
import subprocess
import sys
import time

from repurpose import (FFMPEG, FFPROBE, DOWNLOAD_DIR, RATIOS, _NOWIN,
                       _reframe_filter, _safe, probe_duration, tools_available)

# Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini.
BASE_DIR   = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
             else os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(BASE_DIR, 'exports', 'clipper')

# Batas durasi aman per platform (detik) — dipakai untuk clamp input UI.
PLATFORM_LIMITS = {
    'reels':     90,   # Instagram Reels
    'tiktok':    180,
    'shopee':    60,   # Shopee Video
    'instagram': 90,
}


# ─── Fetch URL by Username / Keyword ─────────────────────────────────────────

# Template URL channel per platform
_CHANNEL_URL = {
    'youtube':   'https://www.youtube.com/@{username}/videos',
    'tiktok':    'https://www.tiktok.com/@{username}',
    'instagram': 'https://www.instagram.com/{username}/',
    'twitter':   'https://twitter.com/{username}/media',
    'facebook':  'https://www.facebook.com/{username}/videos',
}

# Format pencarian per platform
_SEARCH_URL = {
    'youtube':   'ytsearch{n}:{query}',
    'tiktok':    'https://www.tiktok.com/search?q={query_enc}',
    'instagram': 'https://www.instagram.com/explore/tags/{query_enc}/',
    'twitter':   'twitsearch{n}:{query}',
    'facebook':  'https://www.facebook.com/search/videos/?q={query_enc}',
}


def fetch_urls_by_username(platform, username, max_count=10, sort='date'):
    """Ambil daftar URL video dari channel/user via yt-dlp.
    Return list URL (string) atau raise RuntimeError."""
    from urllib.parse import quote_plus
    try:
        from yt_dlp import YoutubeDL
    except Exception as e:
        raise RuntimeError(f'yt-dlp tidak tersedia: {e}')

    username = username.strip().lstrip('@')
    if not username:
        raise RuntimeError('Username tidak boleh kosong.')

    platform = (platform or 'youtube').lower()
    url_tmpl = _CHANNEL_URL.get(platform)
    if not url_tmpl:
        raise RuntimeError(f'Platform "{platform}" tidak didukung untuk fetch by username.')
    channel_url = url_tmpl.format(username=username)

    # Opsi sort: 'date' = terbaru, 'views' = terpopuler
    playlist_items = f'1:{max_count}'
    extra = {}
    if platform == 'youtube' and sort == 'views':
        channel_url = f'https://www.youtube.com/@{username}/videos?view=0&sort=p'

    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
        'playlistend': max_count,
        **extra,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
    except Exception as e:
        raise RuntimeError(f'Gagal fetch video dari @{username}: {e}')

    urls = []
    if info:
        entries = info.get('entries') or ([info] if info.get('url') or info.get('webpage_url') else [])
        for entry in entries:
            u = entry.get('url') or entry.get('webpage_url') or entry.get('id')
            if u:
                # kalau hanya ID, bangun URL penuh
                if not u.startswith('http') and platform == 'youtube':
                    u = f'https://www.youtube.com/watch?v={u}'
                elif not u.startswith('http') and platform == 'tiktok':
                    u = f'https://www.tiktok.com/@{username}/video/{u}'
                urls.append(u)
            if len(urls) >= max_count:
                break
    if not urls:
        raise RuntimeError(f'Tidak ada video ditemukan di @{username} ({platform}).')
    return urls[:max_count]


def fetch_urls_by_keyword(platform, keyword, max_count=10, sort='relevance'):
    """Cari video berdasarkan keyword via yt-dlp.
    Return list URL atau raise RuntimeError."""
    from urllib.parse import quote_plus
    try:
        from yt_dlp import YoutubeDL
    except Exception as e:
        raise RuntimeError(f'yt-dlp tidak tersedia: {e}')

    keyword = keyword.strip()
    if not keyword:
        raise RuntimeError('Keyword tidak boleh kosong.')

    platform = (platform or 'youtube').lower()
    query_enc = quote_plus(keyword)

    # Bangun URL pencarian
    if platform == 'youtube':
        if sort == 'date':
            search_url = f'ytsearch{max_count}:{keyword}'
            # Gunakan filter upload_date melalui parameter sp
            search_url = f'https://www.youtube.com/results?search_query={query_enc}&sp=CAI%253D'
        elif sort == 'views':
            search_url = f'https://www.youtube.com/results?search_query={query_enc}&sp=CAM%253D'
        else:
            search_url = f'ytsearch{max_count}:{keyword}'
    elif platform == 'tiktok':
        search_url = f'https://www.tiktok.com/search?q={query_enc}'
    elif platform == 'instagram':
        search_url = f'https://www.instagram.com/explore/tags/{query_enc}/'
    else:
        # Fallback ke yt-dlp generic search
        search_url = f'ytsearch{max_count}:{keyword}'

    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
        'playlistend': max_count,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
    except Exception as e:
        raise RuntimeError(f'Gagal mencari video "{keyword}": {e}')

    urls = []
    if info:
        entries = info.get('entries') or ([info] if info.get('url') or info.get('webpage_url') else [])
        for entry in entries:
            u = entry.get('url') or entry.get('webpage_url') or entry.get('id')
            if u:
                if not u.startswith('http') and platform == 'youtube':
                    u = f'https://www.youtube.com/watch?v={u}'
                urls.append(u)
            if len(urls) >= max_count:
                break
    if not urls:
        raise RuntimeError(f'Tidak ada video ditemukan untuk keyword "{keyword}" di {platform}.')
    return urls[:max_count]


def probe_video_info(path):
    """Probe video file → dict(duration, width, height, fps, size_mb, codec)."""
    if not os.path.isfile(path):
        return {}
    info = {'size_mb': round(os.path.getsize(path) / 1048576, 1)}
    try:
        import json as _j
        cmd = [FFPROBE, '-v', 'quiet', '-print_format', 'json',
               '-show_format', '-show_streams', path]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             errors='replace', creationflags=_NOWIN)
        data = _j.loads(out.stdout or '{}')
    except Exception:
        return info
    fmt = data.get('format', {})
    info['duration'] = round(float(fmt.get('duration', 0)), 2)
    for s in data.get('streams', []):
        if s.get('codec_type') == 'video':
            info['width'] = s.get('width', 0)
            info['height'] = s.get('height', 0)
            info['codec'] = s.get('codec_name', '')
            r = s.get('r_frame_rate', '0/1')
            try:
                num, den = r.split('/')
                info['fps'] = round(int(num) / max(1, int(den)), 1)
            except Exception:
                info['fps'] = 0
            break
    return info


# ─── Download (video + subtitle) ─────────────────────────────────────────────

def download_with_subs(url, out_dir, sub_langs=('id', 'en'), progress=None):
    """Download video via yt-dlp + subtitle/auto-caption (dikonversi ke .srt).
    Return (video_path, srt_path_or_None)."""
    os.makedirs(out_dir, exist_ok=True)
    try:
        from yt_dlp import YoutubeDL
    except Exception as e:
        raise RuntimeError(f'yt-dlp tidak tersedia: {e}')

    def hook(d):
        if progress and d.get('status') == 'downloading':
            tot = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            got = d.get('downloaded_bytes') or 0
            if tot:
                progress(int(got / tot * 100))

    outtmpl = os.path.join(out_dir, '%(title).80s.%(ext)s')
    # TAHAP 1 — unduh VIDEO saja dulu. Subtitle sengaja dipisah supaya error
    # subtitle (mis. HTTP 429 Too Many Requests dari YouTube) tidak
    # menggagalkan seluruh proses.
    opts = {
        'outtmpl': outtmpl,
        'format': 'bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1][acodec^=mp4a]/bv*+ba/b',
        'merge_output_format': 'mp4',
        'ffmpeg_location': os.path.dirname(FFMPEG) if os.path.isfile(FFMPEG) else None,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'progress_hooks': [hook],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not os.path.isfile(path):
        base = os.path.splitext(path)[0]
        for ext in ('.mp4', '.mkv', '.webm'):
            if os.path.isfile(base + ext):
                path = base + ext
                break
    if not os.path.isfile(path):
        raise RuntimeError('Download selesai tapi file tidak ditemukan.')

    # TAHAP 2 — coba ambil subtitle terpisah; gagal = lanjut tanpa subtitle
    # (caption masih bisa dibuat via transkrip Gemini saat export).
    try:
        sub_opts = {
            'outtmpl': os.path.splitext(path)[0] + '.%(ext)s',
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': list(sub_langs),
            'postprocessors': [{'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'}],
            'ffmpeg_location': os.path.dirname(FFMPEG) if os.path.isfile(FFMPEG) else None,
            'sleep_interval_subtitles': 2,
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
        }
        with YoutubeDL(sub_opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        print(f'[CLIPPER] Subtitle gagal diambil (dilewati): {e}')

    base = os.path.splitext(path)[0]
    srt = None
    candidates = sorted(glob.glob(glob.escape(base) + '*.srt'))
    for lang in sub_langs:
        for c in candidates:
            if f'.{lang}.' in os.path.basename(c) or c.endswith(f'.{lang}.srt'):
                srt = c
                break
        if srt:
            break
    if not srt and candidates:
        srt = candidates[0]
    return path, srt


# ─── Analisis loudness (cari momen highlight) ────────────────────────────────

_EBUR_RE = re.compile(r't:\s*([\d.]+)\s+.*?M:\s*(-?[\d.]+|-inf)')


def _loudness_series(video, progress=None):
    """Scan loudness momentary (ebur128) → list (t, energy_linear).
    Sampel tiap ~100 ms. Return [] kalau video tanpa audio / gagal analisis."""
    cmd = [FFMPEG, '-hide_banner', '-nostats', '-i', video,
           '-vn', '-af', 'ebur128', '-f', 'null', '-']
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             errors='replace', creationflags=_NOWIN)
    except Exception:
        return []
    series = []
    for line in (out.stderr or '').splitlines():
        m = _EBUR_RE.search(line)
        if not m:
            continue
        t = float(m.group(1))
        db = m.group(2)
        energy = 0.0 if db == '-inf' else 10 ** (float(db) / 10.0)
        series.append((t, energy))
    return series


def pick_highlights(video, duration, clip_dur, count, progress=None):
    """Pilih `count` window sepanjang `clip_dur` dengan energi audio tertinggi,
    tanpa tumpang tindih. Fallback: sebar merata bila analisis gagal."""
    series = _loudness_series(video, progress)
    if len(series) < 10 or duration <= clip_dur:
        return spread_windows(duration, clip_dur, count)

    step = max(0.05, (series[-1][0] - series[0][0]) / max(1, len(series) - 1))
    k = max(1, int(round(clip_dur / step)))          # sampel per window
    energies = [e for _, e in series]
    times = [t for t, _ in series]

    # Sliding-window sum (prefix sum) → skor tiap kandidat start
    prefix = [0.0]
    for e in energies:
        prefix.append(prefix[-1] + e)
    scores = []
    for i in range(0, len(energies) - k + 1):
        start_t = times[i]
        if start_t + clip_dur > duration + 0.5:
            break
        scores.append((prefix[i + k] - prefix[i], start_t))
    if not scores:
        return spread_windows(duration, clip_dur, count)

    scores.sort(reverse=True)
    chosen = []
    min_gap = max(2.0, clip_dur * 0.25)              # jarak antar klip
    for score, start_t in scores:
        if len(chosen) >= count:
            break
        if all(abs(start_t - c) >= clip_dur + min_gap for c in chosen):
            chosen.append(start_t)
    # Kalau kurang (video pendek), isi sisanya dengan sebaran merata
    if len(chosen) < count:
        for start_t in spread_windows(duration, clip_dur, count):
            if len(chosen) >= count:
                break
            if all(abs(start_t - c) >= clip_dur for c in chosen):
                chosen.append(start_t)
    chosen.sort()
    return chosen


def spread_windows(duration, clip_dur, count):
    """Sebar `count` window merata sepanjang durasi video."""
    if duration <= clip_dur:
        return [0.0]
    count = max(1, count)
    usable = duration - clip_dur
    if count == 1:
        return [usable / 2]
    step = usable / (count - 1)
    return [round(i * step, 3) for i in range(count)]


# ─── Subtitle (potong + geser SRT per klip) ──────────────────────────────────

_SRT_TIME = re.compile(
    r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})')


def _parse_srt(path):
    """Parse file .srt → list (start_s, end_s, text)."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except Exception:
        return []
    entries = []
    for block in re.split(r'\r?\n\r?\n', raw):
        m = _SRT_TIME.search(block)
        if not m:
            continue
        g = m.groups()
        h1, m1, s1 = int(g[0]), int(g[1]), int(g[2])
        h2, m2, s2 = int(g[4]), int(g[5]), int(g[6])
        ms1 = int(g[3].ljust(3, '0'))
        ms2 = int(g[7].ljust(3, '0'))
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end   = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        text  = block[m.end():].strip()
        if text:
            entries.append((start, end, text))
    return entries


def _fmt_srt_time(sec):
    sec = max(0.0, sec)
    ms = int(round((sec - int(sec)) * 1000))
    s = int(sec)
    return f'{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}'


def _slice_srt(entries, clip_start, clip_dur, out_path):
    """Ambil subtitle yang jatuh di window klip, geser waktunya ke 0.
    Return True bila ada minimal 1 baris."""
    clip_end = clip_start + clip_dur
    lines, idx = [], 0
    for start, end, text in entries:
        if end <= clip_start or start >= clip_end:
            continue
        idx += 1
        s = max(0.0, start - clip_start)
        e = min(clip_dur, end - clip_start)
        lines.append(f'{idx}\n{_fmt_srt_time(s)} --> {_fmt_srt_time(e)}\n{text}\n')
    if not lines:
        return False
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return True


def _sub_filter(srt_path):
    """Filter subtitles ffmpeg dengan escaping path Windows + styling caption."""
    p = os.path.abspath(srt_path).replace('\\', '/').replace(':', r'\:')
    style = ('FontName=Arial,FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,'
             'OutlineColour=&H00000000,Outline=2,BorderStyle=1,'
             'Alignment=2,MarginV=60')
    return f"subtitles='{p}':force_style='{style}'"


# ─── Job utama ────────────────────────────────────────────────────────────────

def run_clipper(job, update):
    """`job` dict: source(path/url), is_url, clip_duration, clip_count,
    mode ('auto'|'spread'), ratio, burn_subs(bool), sub_lang, title."""
    source = (job.get('source') or '').strip()
    if not source:
        update('Sumber video kosong.', done=True, success=False, error='Sumber kosong.')
        return

    ok, reason = tools_available()
    if not ok:
        update(reason, done=True, success=False, error=reason)
        return

    clip_dur  = min(600.0, max(5.0, float(job.get('clip_duration') or 45)))
    count     = min(20, max(1, int(job.get('clip_count') or 3)))
    mode      = job.get('mode') or 'auto'
    ratio     = job.get('ratio') or '9:16'
    burn_subs = bool(job.get('burn_subs'))
    sub_lang  = (job.get('sub_lang') or 'id').strip() or 'id'

    # 1. Dapatkan video lokal (+ subtitle bila diminta)
    srt_path = None
    if job.get('is_url'):
        update('Mengunduh video...', pct=3)
        try:
            langs = (sub_lang, 'id', 'en') if burn_subs else ()
            if burn_subs:
                video, srt_path = download_with_subs(
                    source, DOWNLOAD_DIR, sub_langs=langs,
                    progress=lambda p: update(f'Mengunduh... {p}%', pct=3 + int(p * 0.17)))
            else:
                from repurpose import download_video
                video = download_video(
                    source, DOWNLOAD_DIR,
                    progress=lambda p: update(f'Mengunduh... {p}%', pct=3 + int(p * 0.17)))
        except Exception as e:
            update(f'Gagal unduh: {e}', done=True, success=False, error=str(e))
            return
    else:
        if not os.path.isfile(source):
            update('File video tidak ditemukan.', done=True, success=False,
                   error='File tidak ditemukan.')
            return
        video = source
        if burn_subs:
            base = os.path.splitext(video)[0]
            for c in sorted(glob.glob(glob.escape(base) + '*.srt')):
                srt_path = c
                break

    duration = probe_duration(video)
    if duration <= 0:
        update('Gagal membaca durasi video.', done=True, success=False,
               error='Durasi tidak terbaca (file rusak?).')
        return

    title = _safe(job.get('title') or os.path.splitext(os.path.basename(video))[0])
    out_dir = os.path.join(EXPORT_DIR, f'{title}_{time.strftime("%Y%m%d_%H%M%S")}')
    os.makedirs(out_dir, exist_ok=True)

    # 2 & 3. Tentukan titik potong
    if mode == 'auto':
        update('Menganalisis audio untuk mencari momen highlight...', pct=22)
        starts = pick_highlights(video, duration, clip_dur, count)
    else:
        starts = spread_windows(duration, clip_dur, count)
    starts = [s for s in starts if s < duration - 1][:count]
    if not starts:
        starts = [0.0]

    sub_entries = _parse_srt(srt_path) if (burn_subs and srt_path) else []
    subs_missing = burn_subs and not sub_entries

    # 4 & 5. Potong + reframe (+ subtitle)
    vf_base = _reframe_filter(ratio)
    outputs = []
    n = len(starts)
    update(f'Memproses {n} klip (rasio {ratio})...', pct=35)
    for i, start in enumerate(starts):
        cur = min(clip_dur, duration - start)
        if cur < 3:
            continue
        out_path = os.path.join(out_dir, f'clip_{i+1:02d}_{int(start)}s.mp4')
        vf = vf_base
        if sub_entries:
            clip_srt = os.path.join(out_dir, f'clip_{i+1:02d}.srt')
            if _slice_srt(sub_entries, start, cur, clip_srt):
                vf = vf_base + ',' + _sub_filter(clip_srt)
        cmd = [FFMPEG, '-y', '-ss', f'{start:.3f}', '-t', f'{cur:.3f}', '-i', video,
               '-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
               '-movflags', '+faststart', out_path]
        subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            outputs.append(os.path.basename(out_path))
        update(f'Klip {i+1}/{n} selesai...', pct=35 + int((i + 1) / n * 60))

    if not outputs:
        update('Tidak ada klip dihasilkan (cek ffmpeg/format video).',
               done=True, success=False, error='Output kosong.')
        return

    msg = f'Selesai: {len(outputs)} klip pendek siap upload'
    if subs_missing:
        msg += ' (subtitle tidak tersedia, klip dibuat tanpa caption)'
    update(msg + '.', pct=100, done=True, success=True,
           out_dir=out_dir, clips=len(outputs))


# ─── Upgrade gaya VideoClipperPro: analisa 2-fase + clip manager + export pro ─

import json as _json
import random as _random

from repurpose import (_center_crop_filter, _face_crop_filter_for_segment,
                       _get_encoder_args, detect_best_encoder)


def detect_silences(video, noise_db=-35, min_silence=0.5):
    """Deteksi jeda hening via ffmpeg silencedetect → list (start, end)."""
    cmd = [FFMPEG, '-hide_banner', '-nostats', '-i', video, '-vn',
           '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}', '-f', 'null', '-']
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             errors='replace', creationflags=_NOWIN)
    except Exception:
        return []
    sil, cur = [], None
    for line in (out.stderr or '').splitlines():
        m = re.search(r'silence_start:\s*([\d.]+)', line)
        if m:
            cur = float(m.group(1))
            continue
        m = re.search(r'silence_end:\s*([\d.]+)', line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1))))
            cur = None
    return sil


def _silence_segments(video, duration, clip_dur, count, min_dur=None, max_dur=None):
    """Bangun segmen yang batasnya jatuh di jeda hening (kalimat tidak kepotong).
    min_dur/max_dur membatasi durasi setiap klip."""
    lo = max(3.0, float(min_dur or clip_dur * 0.6))
    hi = max(lo + 1, float(max_dur or clip_dur * 1.4))
    sil = detect_silences(video)
    cuts = [0.0] + [round((a + b) / 2, 2) for a, b in sil] + [duration]
    cuts = sorted(set(cuts))
    segs = []
    seg_start = cuts[0]
    for c in cuts[1:]:
        seg_len = c - seg_start
        if seg_len >= lo:
            segs.append((seg_start, min(c, seg_start + hi)))
            seg_start = c
        if len(segs) >= count:
            break
    if not segs:
        return [(s, min(s + clip_dur, duration)) for s in spread_windows(duration, clip_dur, count)]
    return segs


def _gemini_call_text(keys, parts, model=None, max_tokens=4096):
    """generateContent teks polos dengan rotasi key + fallback model (lokal,
    tanpa import main agar tidak circular)."""
    import httpx
    models = [m for m in [model, 'gemini-flash-latest', 'gemini-2.5-flash', 'gemini-2.0-flash'] if m]
    last_err = 'Gemini tidak merespons.'
    for mdl in dict.fromkeys(models):
        for key in keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}'
            payload = {'contents': [{'parts': parts}],
                       'generationConfig': {'temperature': 0.3, 'maxOutputTokens': max_tokens}}
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
                    last_err = 'Gemini mengembalikan konten kosong.'
            except Exception as e:
                last_err = str(e)
    return '', last_err


def _gemini_segments(video, duration, clip_dur, count, update, keys, model=None):
    """AI Segment Finder: Gemini dengar audionya lalu pilih momen paling viral."""
    from omnivora.ai_caption import GeminiCaptionGenerator
    keys = [k for k in (keys or []) if k]
    if not keys:
        raise RuntimeError('AI Segment Finder butuh Gemini API Key (Generate → Pengaturan).')

    update('Ekstrak audio untuk analisa AI...', pct=22)
    tmp_audio = os.path.join(DOWNLOAD_DIR, '_clipper_ai_audio.mp3')
    subprocess.run([FFMPEG, '-y', '-i', video, '-vn', '-acodec', 'libmp3lame',
                    '-b:a', '64k', '-ac', '1', tmp_audio],
                   capture_output=True, creationflags=_NOWIN)
    if not os.path.isfile(tmp_audio) or os.path.getsize(tmp_audio) < 1000:
        raise RuntimeError('Video tidak punya audio — AI Finder butuh audio.')

    gen = GeminiCaptionGenerator(api_keys=keys,
                                 model=model or 'gemini-flash-latest',
                                 log_cb=lambda m, l='info': None)
    update('Upload audio ke Gemini...', pct=30)
    uri = err = None
    used_key = keys[0]
    for key in keys:
        uri, err = gen._upload_media_to_gemini(key, tmp_audio, 'audio/mpeg')
        if uri:
            used_key = key
            break
    if not uri:
        raise RuntimeError(f'Upload audio gagal: {err}')

    update('Gemini mencari momen viral...', pct=45)
    prompt = (
        f'Audio ini dari video berdurasi {int(duration)} detik. '
        f'Pilih maksimal {count} momen PALING menarik/viral untuk klip pendek '
        '(kalau momen bagus lebih sedikit, kembalikan yang benar-benar bagus saja). '
        f'(hook kuat, punchline, insight, emosi). Durasi tiap momen sekitar {int(clip_dur)} detik '
        f'(boleh {int(clip_dur * 0.6)}-{int(clip_dur * 1.3)} detik), tidak boleh tumpang tindih. '
        'Jawab HANYA JSON array: '
        '[{"start": detik_mulai, "end": detik_selesai, "title": "judul singkat momen"}]'
    )
    parts = [{'file_data': {'file_uri': uri, 'mime_type': 'audio/mpeg'}}, {'text': prompt}]
    text, terr = _gemini_call_text([used_key] + [k for k in keys if k != used_key],
                                   parts, model=model)
    if not text:
        raise RuntimeError(f'Gemini tidak merespons: {terr}')
    m = re.search(r'\[.*\]', text, re.S)
    if not m:
        raise RuntimeError('Jawaban Gemini bukan JSON daftar segmen.')
    try:
        raw = _json.loads(m.group(0))
    except Exception:
        raise RuntimeError('JSON segmen dari Gemini tidak valid.')
    segs = []
    for item in raw:
        try:
            s = max(0.0, float(item.get('start', 0)))
            e = min(duration, float(item.get('end', 0)))
            if e - s >= 3:
                segs.append({'start': round(s, 2), 'end': round(e, 2),
                             'label': str(item.get('title') or f'AI moment {len(segs) + 1}')[:80]})
        except Exception:
            continue
    if not segs:
        raise RuntimeError('Gemini tidak menemukan segmen valid.')
    return segs[:count]


def resolve_video_urls(source_type, value, limit=5):
    """Kembalikan list URL video dari sumber batch.
    source_type: 'multi_link' | 'username' | 'keyword'."""
    limit = max(1, min(50, int(limit or 5)))
    value = (value or '').strip()
    if source_type == 'multi_link':
        urls = [u.strip() for u in re.split(r'[\r\n]+', value) if u.strip().startswith('http')]
        return urls[:limit]

    from yt_dlp import YoutubeDL
    if source_type == 'keyword':
        target = f'ytsearch{limit}:{value}'
    else:  # username / channel
        if value.startswith('http'):
            target = value
        else:
            handle = value.lstrip('@')
            target = f'https://www.youtube.com/@{handle}/videos'

    flat_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                 'playlistend': limit, 'skip_download': True}
    with YoutubeDL(flat_opts) as ydl:
        info = ydl.extract_info(target, download=False)
    entries = []
    for e in (info.get('entries') or [info]):
        if not e:
            continue
        if e.get('entries'):
            entries.extend([x for x in e['entries'] if x])
        else:
            entries.append(e)
    urls = []
    for e in entries[:limit]:
        u = e.get('url') or e.get('webpage_url') or e.get('id')
        if u and not str(u).startswith('http'):
            u = f'https://www.youtube.com/watch?v={u}'
        if u:
            urls.append(u)
    return urls


def run_batch(job, update):
    """Clipper BATCH otomatis: resolusi banyak video (multi-link / username /
    keyword) → tiap video di-download, dianalisa (auto pilih klip), diexport
    dengan setting yang sama → semua klip masuk 1 folder + rekap CSV gabungan +
    auto CSV siap-upload semua platform."""
    source_type = job.get('source_type') or 'multi_link'
    value = job.get('source_value') or ''
    per_video = int(job.get('videos_limit') or 5)

    update('Menyusun daftar video...', pct=2)
    try:
        urls = resolve_video_urls(source_type, value, per_video)
    except Exception as e:
        raise RuntimeError(f'Gagal ambil daftar video: {e}')
    if not urls:
        raise RuntimeError('Tidak ada video ditemukan dari sumber itu.')

    stamp = time.strftime('%Y%m%d_%H%M%S')
    out_dir = (job.get('output_dir') or '').strip() or os.path.join(EXPORT_DIR, f'batch_{stamp}')
    os.makedirs(out_dir, exist_ok=True)

    _stop = getattr(update, 'stop_event', None)
    total = len(urls)
    all_outputs = []
    all_rows = []
    fail = 0

    for vi, url in enumerate(urls, 1):
        if _stop is not None and _stop.is_set():
            update(f'Dihentikan di video {vi}/{total}.', pct=None)
            break
        base_pct = int((vi - 1) / total * 100)

        def _sub_update(msg=None, **kw):
            if msg:
                update(f'[Video {vi}/{total}] {msg}',
                       pct=min(99, base_pct + int((kw.get('pct') or 0) / total)))
        _sub_update.stop_event = _stop

        # 1. Analisa (download + auto pilih klip)
        astate = {}
        sub_an = dict(job, source=url, is_url=True,
                      clip_duration=job.get('clip_duration') or 0,
                      clip_count=job.get('clip_count') or 0,
                      mode=job.get('mode') or 'auto')
        try:
            analyze_clips(sub_an, lambda msg=None, _a=astate, **kw: (
                _a.update({k: v for k, v in kw.items() if v is not None}),
                _sub_update(msg, **kw)))
        except Exception as e:
            fail += 1
            update(f'[Video {vi}/{total}] gagal analisa: {str(e)[:80]}', pct=None)
            continue
        clips = astate.get('clip_list') or []
        if not clips:
            fail += 1
            continue

        # 2. Export SEMUA klip video ini ke folder gabungan (tanpa auto-convert
        #    per video; konversi dilakukan sekali di akhir untuk rekap gabungan)
        estate = {}
        sub_ex = dict(job, video=astate.get('video_path'), srt=astate.get('srt_path', ''),
                      clips=clips, output_dir=out_dir, auto_upload_csv=False,
                      name_pattern=job.get('name_pattern') or 'clip_{n}')
        try:
            export_clips(sub_ex, lambda msg=None, _e=estate, **kw: (
                _e.update({k: v for k, v in kw.items() if v is not None}),
                _sub_update(msg, **kw)))
        except Exception as e:
            fail += 1
            update(f'[Video {vi}/{total}] gagal export: {str(e)[:80]}', pct=None)
            continue
        all_outputs.extend(estate.get('files') or [])
        all_rows.extend(estate.get('rekap_rows') or [])

    if not all_outputs:
        raise RuntimeError('Batch selesai tapi tidak ada klip dihasilkan.')

    # Rekap gabungan + auto CSV siap-upload semua platform
    import csv as _csv
    csv_path = os.path.join(out_dir, 'rekap_clipper.csv')
    try:
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as cf:
            w = _csv.writer(cf)
            w.writerow(['video_path', 'title', 'caption', 'hashtags', 'link'])
            for vp, judul, cap, tags in all_rows:
                w.writerow([vp, judul, cap, tags, ''])
    except OSError:
        csv_path = ''

    upload_files = []
    if csv_path:
        try:
            import main as _m
            platforms = job.get('upload_platforms') or _m.ALL_UPLOAD_PLATFORMS
            upload_files, _ = _m._convert_rekap_to_uploads(csv_path, platforms, out_dir)
        except Exception:
            upload_files = []

    msg = f'Batch selesai: {len(all_outputs)} klip dari {total - fail}/{total} video'
    if upload_files:
        msg += f' + {len(upload_files)} CSV upload'
    update(msg + '.', pct=100, done=True, success=True, out_dir=out_dir,
           clips=len(all_outputs), files=sorted(all_outputs), csv_path=csv_path,
           upload_csv_files=upload_files)



def analyze_clips(job, update):
    """FASE 1 — siapkan video + cari kandidat klip. Hasil dikirim lewat
    update(clip_list=..., video_path=..., srt_path=..., video_duration=...)."""
    source = (job.get('source') or '').strip()
    if not source:
        raise RuntimeError('Sumber video kosong.')
    ok, reason = tools_available()
    if not ok:
        raise RuntimeError(reason)

    mode = job.get('mode') or 'auto'
    raw_dur = float(job.get('clip_duration') or 0)
    raw_count = int(job.get('clip_count') or 0)
    auto_dur = raw_dur <= 0
    auto_count = raw_count <= 0
    clip_dur = min(600.0, max(5.0, raw_dur)) if not auto_dur else 45.0
    count = min(40, max(1, raw_count)) if not auto_count else 5
    sub_lang = (job.get('sub_lang') or 'id').strip() or 'id'

    srt_path = None
    if job.get('is_url'):
        update('Mengunduh video + subtitle...', pct=3)
        video, srt_path = download_with_subs(
            source, DOWNLOAD_DIR, sub_langs=(sub_lang, 'id', 'en'),
            progress=lambda p: update(f'Mengunduh... {p}%', pct=3 + int(p * 0.15)))
    else:
        if not os.path.isfile(source):
            raise RuntimeError('File video tidak ditemukan.')
        video = source
        base = os.path.splitext(video)[0]
        for c in sorted(glob.glob(glob.escape(base) + '*.srt')):
            srt_path = c
            break

    duration = probe_duration(video)
    if duration <= 0:
        raise RuntimeError('Durasi video tidak terbaca (file rusak?).')

    # Nilai otomatis menyesuaikan panjang video
    if auto_dur:
        clip_dur = 30.0 if duration <= 180 else (45.0 if duration <= 1200 else 60.0)
    if auto_count:
        if mode in ('duration', 'count'):
            count = max(1, min(40, int(duration // clip_dur) or 1))
        else:
            # 1 momen tiap ~2.5x durasi klip, min 3 maks 20
            count = max(3, min(20, int(duration // (clip_dur * 2.5)) or 3))
        count = max(1, min(40, count))
    if auto_dur or auto_count:
        update(f'Otomatis: durasi klip ~{int(clip_dur)}s, target {count} klip.', pct=25)

    if mode == 'ai':
        segs = _gemini_segments(video, duration, clip_dur, count, update,
                                job.get('gemini_keys') or [], job.get('gemini_model'))
    elif mode == 'silence':
        update('Deteksi jeda hening...', pct=30)
        segs = [{'start': round(s, 2), 'end': round(e, 2), 'label': f'Segmen {i + 1}'}
                for i, (s, e) in enumerate(_silence_segments(video, duration, clip_dur, count,
                                                             job.get('min_dur'), job.get('max_dur')))]
    elif mode == 'auto':
        update('Analisis audio untuk momen highlight...', pct=30)
        starts = pick_highlights(video, duration, clip_dur, count)
        segs = [{'start': round(s, 2), 'end': round(min(s + clip_dur, duration), 2),
                 'label': f'Highlight {i + 1}'} for i, s in enumerate(starts)]
    elif mode == 'duration':
        segs = []
        t = 0.0
        while t < duration - 1 and len(segs) < 60:
            segs.append({'start': round(t, 2), 'end': round(min(t + clip_dur, duration), 2),
                         'label': f'Bagian {len(segs) + 1}'})
            t += clip_dur
    elif mode == 'count':
        per = duration / count
        segs = [{'start': round(i * per, 2), 'end': round(min((i + 1) * per, duration), 2),
                 'label': f'Bagian {i + 1}'} for i in range(count)]
    else:  # spread
        starts = spread_windows(duration, clip_dur, count)
        segs = [{'start': round(s, 2), 'end': round(min(s + clip_dur, duration), 2),
                 'label': f'Klip {i + 1}'} for i, s in enumerate(starts)]

    # Clamp min/max durasi klip: kepanjangan dipotong, kependekan dibuang
    min_dur = float(job.get('min_dur') or 0)
    max_dur = float(job.get('max_dur') or 0)
    if min_dur or max_dur:
        clean = []
        for s in segs:
            length = s['end'] - s['start']
            if max_dur and length > max_dur:
                s = dict(s, end=round(s['start'] + max_dur, 2))
                length = max_dur
            if min_dur and length < min_dur:
                continue
            clean.append(s)
        if clean:
            segs = clean

    vinfo = probe_video_info(video)
    update(f'Analisa selesai: {len(segs)} kandidat klip.', pct=100, done=True, success=True,
           clip_list=segs, video_path=video, srt_path=srt_path or '',
           video_duration=round(duration, 2), has_subs=bool(srt_path),
           video_info=vinfo)


# ── Caption word-by-word (ASS karaoke dari SRT) ──────────────────────────────

_ASS_COLORS = {
    'kuning': '&H0000FFFF', 'hijau': '&H0000FF00', 'merah': '&H000000FF',
    'biru': '&H00FF8888', 'putih': '&H00FFFFFF', 'ungu': '&H00FF00CC',
}


def _ass_time(sec):
    sec = max(0.0, sec)
    cs = int(round((sec - int(sec)) * 100))
    s = int(sec)
    return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}.{cs:02d}'


def _build_ass(entries, clip_start, clip_dur, out_path, style='karaoke',
               font_size=15, accent='kuning', outline='hitam', pos='bawah',
               play_res=(1080, 1920)):
    """Bangun subtitle .ass dari cue SRT (timing kata dibagi rata per cue).

    Gaya: karaoke (sapuan warna), highlight (kata aktif berwarna),
    pop (kata muncul membesar), beast (kata besar warna selang-seling ala
    Hormozi/MrBeast), box (kata aktif diberi kotak warna), clean (baris penuh
    minimal), blocks (subtitle biasa per kalimat).
    pos: bawah | tengah | atas."""
    clip_end = clip_start + clip_dur
    accent_c = _ASS_COLORS.get(accent, _ASS_COLORS['kuning'])
    rw, rh = play_res
    fs = int(font_size * rh / 240)  # skala kasar -> resolusi ASS
    if style == 'beast':
        fs = int(fs * 1.35)

    if style == 'karaoke':
        primary, secondary = accent_c, '&H00FFFFFF'
    else:
        primary, secondary = '&H00FFFFFF', accent_c

    _OUTLINE_MAP = {'hitam': '&H00000000', 'putih': '&H00FFFFFF', 'none': '&H00000000'}
    outline_c = _OUTLINE_MAP.get(outline, '&H00000000')
    outline_w = 0 if outline == 'none' else 4
    shadow = 1
    if style == 'beast':
        outline_w = max(outline_w, 6)
        shadow = 3
    elif style == 'clean':
        outline_w = 2 if outline != 'none' else 0
        shadow = 0

    _POS = {'bawah': (2, 140), 'tengah': (5, 0), 'atas': (8, 120)}
    align, margin_v = _POS.get(pos, _POS['bawah'])

    header = (
        '[Script Info]\nScriptType: v4.00+\n'
        f'PlayResX: {rw}\nPlayResY: {rh}\nWrapStyle: 0\n\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
        'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: Cap,Arial,{fs},{primary},{secondary},{outline_c},&H7F000000,'
        f'1,0,0,0,100,100,0,0,1,{outline_w},{shadow},{align},40,40,{margin_v},1\n\n'
        '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )
    # Warna selang-seling untuk gaya beast (mulai dari warna aksen pilihan)
    beast_cycle = [accent_c] + [c for c in (_ASS_COLORS['hijau'], _ASS_COLORS['merah'],
                                            _ASS_COLORS['kuning']) if c != accent_c][:2]
    events = []
    widx_global = 0
    for start_t, end_t, text in entries:
        if end_t <= clip_start or start_t >= clip_end:
            continue
        s = max(0.0, start_t - clip_start)
        e = min(clip_dur, end_t - clip_start)
        if e - s <= 0.05:
            continue
        words = [w for w in re.sub(r'\s+', ' ', text.replace('\n', ' ')).split(' ') if w]
        if not words:
            continue
        per = (e - s) / len(words)

        if style in ('blocks', 'clean'):
            # Subtitle biasa: satu kalimat tampil selama durasi cue-nya
            events.append(f'Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{" ".join(words)}')
        elif style == 'pop':
            for i, w in enumerate(words):
                ws = s + i * per
                we = min(e, ws + per)
                anim = r'{\fscx60\fscy60\t(0,90,\fscx115\fscy115)\t(90,160,\fscx100\fscy100)}'
                events.append(f'Dialogue: 0,{_ass_time(ws)},{_ass_time(we)},Cap,,0,0,0,,{anim}{w.upper()}')
        elif style == 'beast':
            # Kata besar satu-satu, warna bergilir, bounce + outline tebal
            for i, w in enumerate(words):
                ws = s + i * per
                we = min(e, ws + per)
                color = beast_cycle[widx_global % len(beast_cycle)]
                widx_global += 1
                anim = ('{\\c' + color + '\\fscx70\\fscy70'
                        '\\t(0,80,\\fscx120\\fscy120)\\t(80,150,\\fscx100\\fscy100)}')
                events.append(f'Dialogue: 0,{_ass_time(ws)},{_ass_time(we)},Cap,,0,0,0,,{anim}{w.upper()}')
        elif style == 'box':
            # Baris penuh; kata aktif diberi blok warna (outline tebal aksen + teks gelap)
            for i in range(len(words)):
                ws = s + i * per
                we = min(e, ws + per)
                line = []
                for j, w in enumerate(words):
                    if j == i:
                        line.append('{\\bord10\\3c' + accent_c + '\\c&H00202020&}' + w
                                    + '{\\bord' + str(outline_w) + '\\3c' + outline_c + '\\c&H00FFFFFF&}')
                    else:
                        line.append(w)
                events.append(f'Dialogue: 0,{_ass_time(ws)},{_ass_time(we)},Cap,,0,0,0,,{" ".join(line)}')
        elif style == 'highlight':
            for i in range(len(words)):
                ws = s + i * per
                we = min(e, ws + per)
                line = []
                for j, w in enumerate(words):
                    if j == i:
                        line.append('{\\c' + accent_c + '}' + w + '{\\c&H00FFFFFF&}')
                    else:
                        line.append(w)
                events.append(f'Dialogue: 0,{_ass_time(ws)},{_ass_time(we)},Cap,,0,0,0,,{" ".join(line)}')
        else:  # karaoke
            ktags = ''.join('{\\kf' + str(max(1, int(per * 100))) + '}' + w + ' ' for w in words)
            events.append(f'Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{ktags.strip()}')
    if not events:
        return False
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(header + '\n'.join(events) + '\n')
    return True


def _ass_filter(ass_path):
    p = os.path.abspath(ass_path).replace('\\', '/').replace(':', r'\:')
    return f"subtitles='{p}'"


# ── Reframe + face mode ──────────────────────────────────────────────────────

def _crop_pos_filter(ratio, pos='center', custom_pct=0.5, letterbox=False):
    """Center/left/right/custom crop ke rasio target. letterbox=True → pad hitam."""
    if ratio == 'original' or ratio not in RATIOS:
        return 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1'
    rw, rh = RATIOS[ratio]
    aspect = rw / rh
    if letterbox:
        # Scale to fit + pad hitam (letterbox / pillarbox)
        return (f'scale=trunc(min(iw\\,ih*{aspect:.6f})/2)*2:'
                f'trunc(min(ih\\,iw/{aspect:.6f})/2)*2:'
                f'force_original_aspect_ratio=decrease,'
                f'pad=trunc(ih*{aspect:.6f}/2)*2:trunc(iw/{aspect:.6f}/2)*2:'
                f'(ow-iw)/2:(oh-ih)/2:black,setsar=1')
    cw = f'min(iw\\,ih*{aspect:.6f})'
    ch = f'min(ih\\,iw/{aspect:.6f})'
    if pos == 'left':
        x = '0'
    elif pos == 'right':
        x = f'(iw-{cw})'
    elif pos == 'custom':
        pct = max(0.0, min(1.0, float(custom_pct)))
        x = f'(iw-{cw})*{pct:.4f}'
    else:
        x = f'(iw-{cw})/2'
    return f'crop={cw}:{ch}:{x}:(ih-{ch})/2,setsar=1'


def _face_mode_complex(face_mode, out_w=1080, out_h=1920):
    """filter_complex untuk mode split-screen. Return string atau None (pakai -vf biasa)."""
    if face_mode == 'split':
        # 2 pembicara: kiri atas, kanan bawah (50:50)
        half = out_h // 2
        return (f'[0:v]crop=iw/2:ih:0:0,scale={out_w}:{half}:force_original_aspect_ratio=increase,'
                f'crop={out_w}:{half},setsar=1[top];'
                f'[0:v]crop=iw/2:ih:iw/2:0,scale={out_w}:{half}:force_original_aspect_ratio=increase,'
                f'crop={out_w}:{half},setsar=1[bot];[top][bot]vstack[vout]')
    if face_mode == 'gaming':
        # 60% gameplay (tengah) + 40% face cam (kuadran kanan-bawah sumber)
        top_h = int(out_h * 0.6) // 2 * 2
        bot_h = out_h - top_h
        return (f'[0:v]crop=iw:ih*0.75:0:0,scale={out_w}:{top_h}:force_original_aspect_ratio=increase,'
                f'crop={out_w}:{top_h},setsar=1[game];'
                f'[0:v]crop=iw/2:ih/2:iw/2:ih/2,scale={out_w}:{bot_h}:force_original_aspect_ratio=increase,'
                f'crop={out_w}:{bot_h},setsar=1[cam];[game][cam]vstack[vout]')
    return None


# ── FASE 2: export klip terpilih ─────────────────────────────────────────────


def _gemini_srt(video, keys, model, update):
    """Buat subtitle .srt otomatis via Gemini (untuk video lokal tanpa subtitle).
    Return path srt atau None."""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return None
    out_srt = os.path.splitext(video)[0] + '.gama.srt'
    if os.path.isfile(out_srt) and os.path.getsize(out_srt) > 50:
        return out_srt
    try:
        from omnivora.ai_caption import GeminiCaptionGenerator
        update('Transkrip Gemini untuk caption...', pct=4)
        tmp_audio = os.path.join(DOWNLOAD_DIR, '_clipper_cap_audio.mp3')
        subprocess.run([FFMPEG, '-y', '-i', video, '-vn', '-acodec', 'libmp3lame',
                        '-b:a', '64k', '-ac', '1', tmp_audio],
                       capture_output=True, creationflags=_NOWIN)
        if not os.path.isfile(tmp_audio) or os.path.getsize(tmp_audio) < 1000:
            return None
        gen = GeminiCaptionGenerator(api_keys=keys, model=model or 'gemini-flash-latest',
                                     log_cb=lambda m, l='info': None)
        uri = None
        used_key = keys[0]
        for key in keys:
            uri, err = gen._upload_media_to_gemini(key, tmp_audio, 'audio/mpeg')
            if uri:
                used_key = key
                break
        if not uri:
            return None
        prompt = ('Transkrip audio ini menjadi subtitle format SRT dalam bahasa aslinya. '
                  'Potong per kalimat pendek (maks ~8 kata per cue) dengan timestamp seakurat mungkin. '
                  'Keluarkan HANYA isi file SRT (nomor, timestamp HH:MM:SS,mmm --> HH:MM:SS,mmm, teks).')
        parts = [{'file_data': {'file_uri': uri, 'mime_type': 'audio/mpeg'}}, {'text': prompt}]
        text, _terr = _gemini_call_text([used_key] + [k for k in keys if k != used_key],
                                        parts, model=model, max_tokens=8192)
        if not text or '-->' not in text:
            return None
        text = re.sub(r'^```(srt)?|```$', '', text.strip(), flags=re.M).strip()
        with open(out_srt, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
        return out_srt if _parse_srt(out_srt) else None
    except Exception:
        return None


def _parse_srt_text(text):
    """Parse teks SRT langsung (tanpa file) → list (start, end, teks)."""
    entries = []
    for block in re.split(r'\r?\n\r?\n', text or ''):
        m = _SRT_TIME.search(block)
        if not m:
            continue
        g = m.groups()
        start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, '0')) / 1000
        end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, '0')) / 1000
        txt = block[m.end():].strip()
        if txt:
            entries.append((start, end, txt))
    return entries


_CLIP_PACK_CACHE = {}


def _gemini_clip_pack(video, start, dur, keys, model, update=None):
    """SATU panggilan Gemini per klip → judul + caption upload + hashtag + SRT.

    Audio klip diekstrak dulu (pendek → transkrip akurat & murah), timestamp SRT
    relatif ke awal klip. Return dict {judul, caption, hashtags, entries} atau None."""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return None
    cache_key = (video, round(start, 1), round(dur, 1))
    if cache_key in _CLIP_PACK_CACHE:
        return _CLIP_PACK_CACHE[cache_key]
    try:
        from omnivora.ai_caption import GeminiCaptionGenerator
        tmp_audio = os.path.join(DOWNLOAD_DIR, f'_clip_pack_{os.getpid()}_{int(start)}.mp3')
        subprocess.run([FFMPEG, '-y', '-ss', f'{start:.2f}', '-t', f'{dur:.2f}', '-i', video,
                        '-vn', '-acodec', 'libmp3lame', '-b:a', '64k', '-ac', '1', tmp_audio],
                       capture_output=True, creationflags=_NOWIN)
        if not os.path.isfile(tmp_audio) or os.path.getsize(tmp_audio) < 1000:
            return None
        gen = GeminiCaptionGenerator(api_keys=keys, model=model or 'gemini-flash-latest',
                                     log_cb=lambda m, l='info': None)
        uri = None
        used_key = keys[0]
        for key in keys:
            uri, err = gen._upload_media_to_gemini(key, tmp_audio, 'audio/mpeg')
            if uri:
                used_key = key
                break
        try:
            os.remove(tmp_audio)
        except OSError:
            pass
        if not uri:
            return None
        prompt = (
            f'Audio ini potongan klip pendek berdurasi {int(dur)} detik untuk konten '
            'TikTok/Reels/Shorts. Kerjakan 4 hal sekaligus, jawab HANYA JSON valid:\n'
            '{"judul": "judul singkat menarik maks 8 kata sesuai isi klip",\n'
            ' "caption": "caption upload 1-2 kalimat yang hook, bahasa sama dengan audio, tanpa hashtag",\n'
            ' "hashtags": ["5-8 hashtag relevan termasuk yang lagi tren, diawali #"],\n'
            ' "srt": "transkrip format SRT, potong per frasa pendek maks 7 kata, '
            'timestamp mulai 00:00:00 relatif awal audio ini"}'
        )
        parts = [{'file_data': {'file_uri': uri, 'mime_type': 'audio/mpeg'}}, {'text': prompt}]
        text, terr = _gemini_call_text([used_key] + [k for k in keys if k != used_key],
                                       parts, model=model, max_tokens=8192)
        if not text:
            if update:
                update(f'Gemini meta klip gagal: {terr}', pct=None)
            return None
        m = re.search(r'\{.*\}', text, re.S)
        if not m:
            return None
        raw = m.group(0)
        try:
            obj = _json.loads(raw)
        except Exception:
            # JSON kadang berisi newline mentah di field srt — perbaiki kasar
            try:
                obj = _json.loads(raw.replace('\r', '').replace('\n', '\\n'))
            except Exception:
                return None
        srt_text = str(obj.get('srt') or '').replace('\\n', '\n')
        entries = _parse_srt_text(srt_text)
        pack = {
            'judul': str(obj.get('judul') or '').strip()[:90],
            'caption': str(obj.get('caption') or '').strip(),
            'hashtags': ' '.join(str(h).strip() for h in (obj.get('hashtags') or [])
                                 if str(h).strip().startswith('#')),
            'entries': entries,
        }
        _CLIP_PACK_CACHE[cache_key] = pack
        if len(_CLIP_PACK_CACHE) > 512:
            _CLIP_PACK_CACHE.clear()
        return pack
    except Exception:
        return None



def export_clips(job, update):
    """Export klip terpilih dengan reframe/face mode, caption word-by-word,
    BGM (loop, fade in/out), anti-detection, GPU encoder + paralel.

    Mendukung multi-video: tiap klip bisa punya field 'video' sendiri yang
    meng-override job['video'] default. Berguna untuk mode Multi Link / Batch."""
    video = (job.get('video') or '').strip()
    clips = job.get('clips') or []
    # Validasi: video default wajib ada KECUALI semua klip punya video sendiri
    clips_need_default = [c for c in clips if not (c.get('video') or '').strip()]
    if clips_need_default and not os.path.isfile(video):
        raise RuntimeError('File video sumber hilang — jalankan Analisa lagi.')
    if not clips:
        raise RuntimeError('Tidak ada klip terpilih.')

    ratio = job.get('ratio') or '9:16'
    crop_pos = job.get('crop_pos') or 'center'
    crop_custom_pct = float(job.get('crop_custom_pct', 0.5))
    letterbox = bool(job.get('letterbox'))
    face_mode = job.get('face_mode') or 'off'
    cap_style = job.get('caption_style') or 'off'
    cap_size = int(job.get('caption_size') or 15)
    cap_color = job.get('caption_color') or 'kuning'
    cap_outline = job.get('caption_outline') or 'hitam'
    cap_pos = job.get('caption_pos') or 'bawah'
    bgm = (job.get('bgm') or '').strip()
    bgm_vol = float(job.get('bgm_vol', 30)) / 100
    orig_vol = float(job.get('orig_vol', 100)) / 100
    bgm_fade_in = bool(job.get('bgm_fade_in', True))
    bgm_fade_out = bool(job.get('bgm_fade_out', True))
    anti = bool(job.get('anti_detect'))
    turbo = bool(job.get('turbo', True))
    pattern = (job.get('name_pattern') or 'clip_{n}').strip() or 'clip_{n}'
    smart_meta = bool(job.get('smart_meta', True)) and bool(job.get('gemini_keys'))
    csv_rows = []  # (video_path, judul, caption, hashtags) untuk rekap CSV
    _cancelled = [False]  # mutable for nested closure
    job['_cancelled'] = _cancelled
    _stop_ev = getattr(update, 'stop_event', None)

    srt = (job.get('srt') or '').strip()
    if cap_style != 'off' and not (srt and os.path.isfile(srt)):
        # Tidak ada subtitle (file lokal) — coba transkrip otomatis via Gemini
        auto = _gemini_srt(video, job.get('gemini_keys'), job.get('gemini_model'), update)
        if auto:
            srt = auto
    sub_entries = _parse_srt(srt) if (cap_style != 'off' and srt and os.path.isfile(srt)) else []
    if cap_style != 'off' and not sub_entries:
        update('Subtitle tidak tersedia — klip diexport tanpa caption.', pct=3)

    if ratio in RATIOS:
        rw, rh = RATIOS[ratio]
        out_w, out_h = (1080, 1920) if rw < rh else ((1080, 1080) if rw == rh else (1920, 1080))
    else:
        out_w, out_h = 1080, 1920

    title = _safe(job.get('title') or os.path.splitext(os.path.basename(video))[0])
    out_dir = (job.get('output_dir') or '').strip() or os.path.join(
        EXPORT_DIR, f'{title}_{time.strftime("%Y%m%d_%H%M%S")}')
    os.makedirs(out_dir, exist_ok=True)

    encoder = detect_best_encoder(FFMPEG)
    outputs = []
    _lock = __import__('threading').Lock()
    _done = [0]
    n = len(clips)

    def _export_one(idx_clip):
        if _cancelled[0] or (_stop_ev is not None and _stop_ev.is_set()):
            _cancelled[0] = True
            return None
        i, clip = idx_clip
        start = max(0.0, float(clip.get('start') or 0))
        end = float(clip.get('end') or 0)
        dur = end - start
        if dur < 2:
            return None
        # Mendukung multi-video: pakai video per-klip jika tersedia
        clip_video = (clip.get('video') or '').strip() or video
        if not os.path.isfile(clip_video):
            update(f'Klip {i+1}: video tidak ditemukan ({os.path.basename(clip_video)})', pct=None)
            return None
        # Paket Gemini per klip: judul + caption + hashtag + SRT (untuk penamaan,
        # sidecar, CSV, dan fallback caption saat video tak punya subtitle)
        meta = None
        if smart_meta and (cap_style != 'off' or True):
            update(f'Gemini menganalisa klip {i + 1}/{n} (judul/caption/transkrip)...', pct=None)
            meta = _gemini_clip_pack(clip_video, start, dur, job.get('gemini_keys'),
                                     job.get('gemini_model'), update)
        judul_slug = _safe((meta or {}).get('judul') or clip.get('label') or f'clip')
        name = pattern.replace('{judul}', judul_slug).replace('{n}', f'{i + 1:02d}')
        if meta and '{judul}' not in pattern and pattern.strip() in ('clip_{n}', ''):
            # Pola default → otomatis pakai judul isi konten
            name = f'{judul_slug}_{i + 1:02d}'
        out_path = os.path.join(out_dir, _safe(name)[:80] + '.mp4')

        # Caption ASS untuk window ini (SRT penuh → slice; tanpa SRT → transkrip
        # Gemini per-klip yang timestamp-nya sudah relatif ke awal klip)
        sub_f = ''
        if cap_style != 'off':
            ass_path = os.path.join(out_dir, f'_cap_{i + 1:02d}.ass')
            built = False
            # SRT bawaan hanya valid untuk video utama; klip dari video lain
            # (mode batch) pakai transkrip Gemini per-klip.
            if sub_entries and clip_video == video:
                built = _build_ass(sub_entries, start, dur, ass_path, style=cap_style,
                                   font_size=cap_size, accent=cap_color, outline=cap_outline,
                                   pos=cap_pos, play_res=(out_w, out_h))
            if not built and meta and meta.get('entries'):
                built = _build_ass(meta['entries'], 0, dur, ass_path, style=cap_style,
                                   font_size=cap_size, accent=cap_color, outline=cap_outline,
                                   pos=cap_pos, play_res=(out_w, out_h))
            if built:
                sub_f = _ass_filter(ass_path)

        # Anti-detection: variasi ringan unik per klip
        anti_f = ''
        if anti:
            rnd = _random.Random(f'{video}|{i}')
            b = rnd.uniform(-0.02, 0.02)
            c = rnd.uniform(0.97, 1.03)
            s = rnd.uniform(0.95, 1.05)
            anti_f = f'eq=brightness={b:.4f}:contrast={c:.4f}:saturation={s:.4f}'

        complex_v = _face_mode_complex(face_mode, out_w, out_h) if ratio != 'original' else None
        cmd = [FFMPEG, '-y', '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', clip_video]

        has_bgm = bgm and os.path.isfile(bgm)
        if has_bgm:
            cmd += ['-stream_loop', '-1', '-i', bgm]

        fparts = []
        if complex_v:
            fparts.append(complex_v)
            vlabel = '[vout]'
        else:
            if face_mode == 'smart' and ratio != 'original':
                vf = (_face_crop_filter_for_segment(clip_video, start, dur, ratio)
                      or _crop_pos_filter(ratio, crop_pos, crop_custom_pct, letterbox))
            else:
                vf = _crop_pos_filter(ratio, crop_pos, crop_custom_pct, letterbox)
            fparts.append(f'[0:v]{vf}[vout]')
            vlabel = '[vout]'
        extra = ','.join(x for x in (anti_f, sub_f) if x)
        if extra:
            fparts.append(f'{vlabel}{extra}[vfin]')
            vlabel = '[vfin]'

        if has_bgm:
            fparts.append(f'[0:a]volume={orig_vol:.3f}[a0]')
            bgm_chain = f'volume={bgm_vol:.3f}'
            if bgm_fade_in:
                bgm_chain += ',afade=t=in:st=0:d=1.5'
            if bgm_fade_out:
                bgm_chain += f',afade=t=out:st={max(0, dur - 1.5):.2f}:d=1.5'
            fparts.append(f'[1:a]{bgm_chain}[a1]')
            fparts.append('[a0][a1]amix=inputs=2:duration=first:normalize=0[aout]')
            amap = '[aout]'
        else:
            fparts.append(f'[0:a]volume={orig_vol:.3f}[aout]')
            amap = '[aout]'

        cmd += ['-filter_complex', ';'.join(fparts), '-map', vlabel, '-map', amap]
        cmd += _get_encoder_args(encoder, 21)
        cmd += ['-c:a', 'aac', '-b:a', '128k', '-map_metadata', '-1', '-map_chapters', '-1']
        if anti:
            import hashlib as _hl
            fp = _hl.md5(f'{video}|{i}|{time.time_ns()}'.encode()).hexdigest()[:16]
            cmd += ['-metadata', f'comment=vc-{fp}']
        cmd += ['-movflags', '+faststart', out_path]
        res = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
        ok = res.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        note = ' selesai'
        if not ok and sub_f:
            # Caption bermasalah (font/ass/filter) — ulangi tanpa caption supaya
            # export tidak pernah gagal hanya gara-gara subtitle.
            cmd_nc = [a for a in cmd]
            for k, a in enumerate(cmd_nc):
                if a == '-filter_complex':
                    cmd_nc[k + 1] = cmd_nc[k + 1].replace(',' + sub_f, '').replace(sub_f + ',', '').replace(sub_f, 'null')
            res = subprocess.run(cmd_nc, capture_output=True, creationflags=_NOWIN)
            ok = res.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
            if ok:
                note = ' selesai (tanpa caption — caption gagal dirender)'
        with _lock:
            _done[0] += 1
            update(f'Klip {_done[0]}/{n}' + (note if ok else ' GAGAL'),
                   pct=5 + int(_done[0] / n * 92))
            if ok:
                outputs.append(out_path)
                if meta:
                    try:
                        with open(os.path.splitext(out_path)[0] + '.txt', 'w', encoding='utf-8') as sf:
                            sf.write('Judul: ' + (meta.get('judul') or '') + '\n\n'
                                     + 'Caption:\n' + (meta.get('caption') or '') + '\n\n'
                                     + 'Hashtags:\n' + (meta.get('hashtags') or '') + '\n')
                    except OSError:
                        pass
                    csv_rows.append((out_path, meta.get('judul') or '',
                                     meta.get('caption') or '', meta.get('hashtags') or ''))
                else:
                    csv_rows.append((out_path, clip.get('label') or '', '', ''))
        return out_path if ok else None

    # Estimasi waktu & size
    total_dur_s = sum(max(0, float((c.get('end') or 0)) - float((c.get('start') or 0))) for c in clips)
    est_size_mb = round(total_dur_s * 0.8, 1)  # ~0.8 MB/detik CRF21 1080p
    est_time_s = int(total_dur_s * 1.2) if encoder == 'libx264' else int(total_dur_s * 0.6)
    est_time_m = max(1, est_time_s // 60)
    update(f'Export {n} klip (encoder {encoder}) — est. ~{est_size_mb} MB, ~{est_time_m} menit', pct=3,
           est_size_mb=est_size_mb, est_time_min=est_time_m)

    tasks = list(enumerate(clips))
    if turbo and n > 1:
        import concurrent.futures as _fut
        workers = 3 if encoder != 'libx264' else max(2, (os.cpu_count() or 4) // 2)
        with _fut.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_export_one, tasks))
    else:
        for t in tasks:
            if _cancelled[0]:
                break
            _export_one(t)

    # Bersihkan file .ass sementara
    for f in glob.glob(os.path.join(out_dir, '_cap_*.ass')):
        try:
            os.remove(f)
        except OSError:
            pass

    if not outputs:
        if _cancelled[0]:
            raise RuntimeError('Export dibatalkan.')
        raise RuntimeError('Semua klip gagal diexport — cek ffmpeg/format video.')
    # Rekap CSV — format kompatibel konverter siap-upload GAMA (semua platform)
    csv_path = ''
    if csv_rows:
        import csv as _csv
        csv_path = os.path.join(out_dir, 'rekap_clipper.csv')
        try:
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as cf:
                wcsv = _csv.writer(cf)
                wcsv.writerow(['video_path', 'title', 'caption', 'hashtags', 'link'])
                for vp, judul, cap, tags in csv_rows:
                    wcsv.writerow([vp, judul, cap, tags, ''])
        except OSError:
            csv_path = ''

    # Auto-generate CSV siap-upload semua platform (tanpa klik manual)
    upload_files = []
    if csv_path and job.get('auto_upload_csv', True):
        try:
            import main as _m
            platforms = job.get('upload_platforms') or _m.ALL_UPLOAD_PLATFORMS
            upload_files, _ = _m._convert_rekap_to_uploads(csv_path, platforms, out_dir)
        except Exception:
            upload_files = []

    note = ' (dihentikan sebagian)' if _cancelled[0] else ''
    up_note = f' + {len(upload_files)} CSV upload' if upload_files else ''
    update(f'Selesai: {len(outputs)} klip siap upload{note}{up_note}.', pct=100, done=True, success=True,
           out_dir=out_dir, clips=len(outputs), files=sorted(outputs), csv_path=csv_path,
           upload_csv_files=upload_files, rekap_rows=csv_rows)
