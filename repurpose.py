"""
repurpose.py - Repurpose video tools untuk GAMA.

Tahap split video mendukung:
  1. Multi-source local video
  2. Output folder custom
  3. Split by segment count / fixed duration
  4. Face tracker opsional (aktif bila OpenCV tersedia)
  5. Capture frame tetap dipakai untuk action Capture Image
"""
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

# Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
           else os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(BASE_DIR, 'exports', 'repurposed')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.mpeg', '.mpg', '.m4v'}

RATIOS = {
    '9:16': (9.0, 16.0),
    '1:1': (1.0, 1.0),
    '16:9': (16.0, 9.0),
    '4:5': (4.0, 5.0),
}

_NOWIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
_ENCODER_CACHE = None
logger = logging.getLogger(__name__)


def _tool(name):
    local = os.path.join(BASE_DIR, 'assets', 'bin', name + '.exe')
    if os.path.isfile(local):
        return local
    path = shutil.which(name)
    if path:
        return path
    alt = os.path.join(r'D:\TOOLS\LUMINA REPURPOSE', 'assets', 'bin', name + '.exe')
    return alt if os.path.isfile(alt) else name


FFMPEG = _tool('ffmpeg')
FFPROBE = _tool('ffprobe')


def tools_available():
    ok = bool(shutil.which('ffmpeg') or os.path.isfile(FFMPEG))
    if ok:
        return True, ''
    return False, ('ffmpeg tidak ditemukan. Install ffmpeg (mis. "winget install '
                   'Gyan.FFmpeg") lalu restart GAMA.')


def face_tracker_available():
    try:
        import cv2  # noqa: F401
        return True, ''
    except Exception:
        return False, 'Face Tracker butuh OpenCV (`pip install opencv-python`).'


def list_videos_in_folder(folder):
    if not folder or not os.path.isdir(folder):
        return []
    files = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in VIDEO_EXTS:
            files.append(path)
    return files


def _safe(raw):
    safe = re.sub(r'[\\/*?:"<>|]+', '_', str(raw or '')).strip()
    safe = re.sub(r'\s+', '_', safe)
    safe = re.sub(r'_+', '_', safe)          # collapse consecutive underscores
    return (safe[:60] or 'video').strip('_')


_DURATION_CACHE = {}
_AUDIO_STREAM_CACHE = {}


def probe_duration(path):
    """Durasi media via ffprobe — di-cache per (path, mtime, size) supaya
    seleksi clip mixer/split tidak memanggil ffprobe berulang untuk file sama."""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return 0.0
    hit = _DURATION_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        out = subprocess.run(
            [FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, creationflags=_NOWIN, timeout=15)
        dur = float((out.stdout or '0').strip())
    except Exception:
        dur = 0.0
    if len(_DURATION_CACHE) > 4096:
        _DURATION_CACHE.clear()
    _DURATION_CACHE[key] = dur
    return dur


def probe_has_audio(path):
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return False
    hit = _AUDIO_STREAM_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        out = subprocess.run(
            [FFPROBE, '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=codec_type',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, creationflags=_NOWIN, timeout=15)
        has_audio = bool((out.stdout or '').strip())
    except Exception:
        has_audio = False
    if len(_AUDIO_STREAM_CACHE) > 4096:
        _AUDIO_STREAM_CACHE.clear()
    _AUDIO_STREAM_CACHE[key] = has_audio
    return has_audio


def _center_crop_filter(target_ratio='9:16'):
    if target_ratio == 'original' or target_ratio not in RATIOS:
        return 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1'
    rw, rh = RATIOS[target_ratio]
    aspect = rw / rh
    return (
        f'crop=min(iw\\,ih*{aspect:.6f}):min(ih\\,iw/{aspect:.6f}):'
        f'(iw-min(iw\\,ih*{aspect:.6f}))/2:'
        f'(ih-min(ih\\,iw/{aspect:.6f}))/2,setsar=1'
    )


def _reframe_filter(ratio='9:16'):
    """Alias kompatibilitas untuk clipper.py (nama lama sebelum refactor split)."""
    return _center_crop_filter(ratio)


def _get_cv2():
    try:
        import cv2
        return cv2
    except Exception:
        return None


def _get_cascades(cv2):
    frontal = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, 'haarcascade_frontalface_default.xml'))
    profile = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, 'haarcascade_profileface.xml'))
    return frontal, profile


def _detect_faces_in_frame(frame, cv2, frontal, profile, min_face_ratio=0.05):
    fh, fw = frame.shape[:2]
    # Deteksi di frame yang di-downscale (maks 640px) — jauh lebih cepat untuk
    # video HD/4K dan haar cascade justru lebih stabil di ukuran kecil.
    scale = 1.0
    if max(fw, fh) > 640:
        scale = 640.0 / max(fw, fh)
        frame = cv2.resize(frame, (int(fw * scale), int(fh * scale)), interpolation=cv2.INTER_AREA)
    sh, sw = frame.shape[:2]
    min_size = max(20, int(min(sw, sh) * min_face_ratio))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = []
    try:
        found = frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_size, min_size), flags=cv2.CASCADE_SCALE_IMAGE)
        if len(found):
            faces.extend(found.tolist())
    except Exception:
        pass
    try:
        found = profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_size, min_size), flags=cv2.CASCADE_SCALE_IMAGE)
        if len(found):
            faces.extend(found.tolist())
        flipped = cv2.flip(gray, 1)
        found_right = profile.detectMultiScale(flipped, scaleFactor=1.1, minNeighbors=5, minSize=(min_size, min_size), flags=cv2.CASCADE_SCALE_IMAGE)
        if len(found_right):
            for (x, y, w, h) in found_right:
                faces.append([sw - x - w, y, w, h])
    except Exception:
        pass
    faces = [f for f in faces if f[2] >= min_size and f[3] >= min_size]
    if scale != 1.0:
        inv = 1.0 / scale
        faces = [[int(x * inv), int(y * inv), int(w * inv), int(h * inv)] for (x, y, w, h) in faces]
    return faces


def _best_face(faces, frame_w, frame_h):
    if not faces:
        return None
    if len(faces) == 1:
        return tuple(faces[0])

    def score(face):
        fx, fy, fw, fh = face
        area = fw * fh
        cx = fx + fw / 2
        cy = fy + fh / 2
        edge_penalty = (abs(cx - frame_w / 2) / frame_w + abs(cy - frame_h / 2) / frame_h) * 0.3
        return area * (1 - edge_penalty)

    return tuple(max(faces, key=score))


def _detect_face_in_segment(video_path, start_sec, duration_sec, n_samples=5, min_face_ratio=0.05):
    cv2 = _get_cv2()
    if cv2 is None:
        return None
    frontal, profile = _get_cascades(cv2)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        total_dur = total_frames / fps if fps else 0
        duration_sec = min(duration_sec, max(0.0, total_dur - start_sec))
        if duration_sec <= 0:
            return None
        if n_samples <= 1:
            times = [start_sec + duration_sec * 0.5]
        else:
            step = duration_sec / (n_samples - 1)
            times = [start_sec + i * step for i in range(n_samples)]
        detections = []
        for t in times:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
            ok, frame = cap.read()
            if not ok:
                continue
            faces = _detect_faces_in_frame(frame, cv2, frontal, profile, min_face_ratio)
            best = _best_face(faces, frame_w, frame_h)
            if best:
                detections.append(best)
        if len(detections) < max(1, len(times) // 2):
            return None
        cx_vals = sorted(f[0] + f[2] // 2 for f in detections)
        cy_vals = sorted(f[1] + f[3] // 2 for f in detections)
        cx = cx_vals[len(cx_vals) // 2]
        cy = cy_vals[len(cy_vals) // 2]
        max_fw = max(f[2] for f in detections)
        max_fh = max(f[3] for f in detections)
        return cx, cy, max_fw, max_fh, frame_w, frame_h
    finally:
        cap.release()


def _face_to_crop_params(cx, cy, face_w, face_h, frame_w, frame_h, target_ratio, padding_top=0.65, padding_bottom=0.35):
    try:
        rw, rh = RATIOS[target_ratio]
        aspect = rw / rh
    except Exception:
        aspect = 9.0 / 16.0
    frame_aspect = frame_w / frame_h if frame_h else aspect
    if frame_aspect > aspect:
        crop_h = frame_h
        crop_w = int(frame_h * aspect)
    else:
        crop_w = frame_w
        crop_h = int(frame_w / aspect)
    needed_h = int(face_h * (1 + padding_top + padding_bottom))
    needed_w = int(needed_h * aspect)
    if needed_h > crop_h:
        crop_h = min(needed_h, frame_h)
        crop_w = int(crop_h * aspect)
    if needed_w > crop_w:
        crop_w = min(needed_w, frame_w)
        crop_h = int(crop_w / aspect)
    crop_w = (min(crop_w, frame_w) // 2) * 2
    crop_h = (min(crop_h, frame_h) // 2) * 2
    top_frac = padding_top / (padding_top + 1.0 + padding_bottom)
    crop_y = cy - int(crop_h * top_frac)
    crop_x = cx - crop_w // 2
    crop_x = max(0, min(crop_x, frame_w - crop_w))
    crop_y = max(0, min(crop_y, frame_h - crop_h))
    return crop_x, crop_y, crop_w, crop_h


def _face_crop_filter_for_segment(video_path, start_sec, duration_sec, target_ratio='9:16', require_face=False):
    if target_ratio == 'original':
        return 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1'
    face = _detect_face_in_segment(video_path, start_sec, duration_sec)
    if face is None:
        return None if require_face else _center_crop_filter(target_ratio)
    cx, cy, face_w, face_h, frame_w, frame_h = face
    crop_x, crop_y, crop_w, crop_h = _face_to_crop_params(cx, cy, face_w, face_h, frame_w, frame_h, target_ratio)
    return f'crop={crop_w}:{crop_h}:{crop_x}:{crop_y},setsar=1'


def detect_best_encoder(ffmpeg_path):
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    candidates = [
        # NVENC: preset p1 = absolute fastest, zerolatency tune, no lookahead
        ('h264_nvenc', ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'ull',
                        '-rc', 'vbr', '-cq', '23', '-rc-lookahead', '0', '-b_adapt', '0']),
        # Intel QSV: veryfast + low latency
        ('h264_qsv',   ['-c:v', 'h264_qsv', '-preset', 'veryfast',
                        '-low_power', '1', '-global_quality', '23']),
        # AMD AMF: speed + no lookahead
        ('h264_amf',   ['-c:v', 'h264_amf', '-quality', 'speed',
                        '-rc', 'cqp', '-qp_i', '23', '-preanalysis', '0']),
    ]
    for name, args in candidates:
        try:
            cmd = [
                ffmpeg_path, '-y', '-f', 'lavfi', '-i', 'color=black:size=128x128:duration=0.1:rate=25'
            ] + args + ['-frames:v', '1', '-f', 'null', '-']
            result = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN, timeout=8)
            if result.returncode == 0:
                _ENCODER_CACHE = name
                return name
        except Exception:
            continue
    _ENCODER_CACHE = 'libx264'
    return 'libx264'


def _get_encoder_args(name, crf=22):
    if name == 'h264_nvenc':
        # p1 = fastest NVENC preset, ull = ultra-low-latency, no lookahead
        return ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'ull',
                '-rc', 'vbr', '-cq', str(crf), '-b:v', '0',
                '-rc-lookahead', '0', '-b_adapt', '0', '-pix_fmt', 'yuv420p']
    if name == 'h264_qsv':
        return ['-c:v', 'h264_qsv', '-preset', 'veryfast',
                '-low_power', '1', '-global_quality', str(crf), '-pix_fmt', 'yuv420p']
    if name == 'h264_amf':
        return ['-c:v', 'h264_amf', '-quality', 'speed',
                '-rc', 'cqp', '-qp_i', str(crf), '-preanalysis', '0', '-pix_fmt', 'yuv420p']
    # libx264 ultrafast + zerolatency = absolute fastest software encode
    return ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-crf', str(crf), '-pix_fmt', 'yuv420p']


def _probe_audio_codec(video_path):
    """Deteksi codec audio di file video — return 'aac', 'mp3', dll atau None."""
    try:
        r = subprocess.run(
            [FFMPEG, '-y', '-i', video_path, '-f', 'null', '-'],
            capture_output=True, creationflags=_NOWIN, timeout=5
        )
        out = (r.stderr or b'').decode('utf-8', errors='ignore')
        for line in out.splitlines():
            if 'Audio:' in line:
                # "Audio: aac, ..." → "aac"
                part = line.split('Audio:')[1].strip().split(',')[0].strip()
                return part.lower()
    except Exception:
        pass
    return None


def _build_split_cmd(video_path, out_path, start_sec, duration_sec, vf_filter, encoder=None, crf=22):
    """Build ffmpeg split command dengan optimasi kecepatan maksimal.

    Strategi:
    - Input seek (-ss sebelum -i) = decode hanya segment yang diperlukan
    - -threads 0 = pakai semua CPU core
    - Audio copy jika source AAC (tidak perlu re-encode audio)
    - Hardware encoder jika tersedia (NVENC > QSV > AMF > libx264 ultrafast)
    - CRF 26 default untuk keseimbangan kecepatan/kualitas split
    """
    encoder = encoder or detect_best_encoder(FFMPEG)

    # Deteksi apakah audio source sudah AAC → copy langsung, jauh lebih cepat
    src_audio = _probe_audio_codec(video_path)
    if src_audio in ('aac', 'aac_latm'):
        audio_args = ['-c:a', 'copy']
    else:
        # encode audio cepat: stereo 44.1kHz, bitrate rendah cukup untuk repurpose
        audio_args = ['-c:a', 'aac', '-b:a', '96k', '-ac', '2', '-ar', '44100']

    cmd = [
        FFMPEG, '-y',
        '-threads', '0',            # pakai semua CPU core untuk decode
        '-ss', str(start_sec),      # input seek (sangat cepat)
        '-t',  str(duration_sec),
        '-i',  video_path,
        '-vf', vf_filter,
    ]
    cmd += _get_encoder_args(encoder, crf)
    cmd += audio_args
    cmd += [
        '-map_metadata', '-1',
        '-map_chapters', '-1',
        '-movflags', '+faststart',
        '-avoid_negative_ts', 'make_zero',
        out_path,
    ]
    return cmd


def download_video(url, out_dir, progress=None):
    os.makedirs(out_dir, exist_ok=True)
    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        raise RuntimeError(f'yt-dlp tidak tersedia: {exc}')

    def hook(data):
        if progress and data.get('status') == 'downloading':
            total = data.get('total_bytes') or data.get('total_bytes_estimate') or 0
            done = data.get('downloaded_bytes') or 0
            if total:
                progress(int(done / total * 100))

    outtmpl = os.path.join(out_dir, '%(title).80s.%(ext)s')
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
    return path


def _is_url(s):
    return str(s).lower().startswith(('http://', 'https://'))


def _resolve_videos(job, update):
    source_list = [str(p).strip() for p in (job.get('sources') or []) if str(p).strip()]
    if source_list:
        missing = [p for p in source_list if not _is_url(p) and not os.path.isfile(p)]
        if missing:
            raise RuntimeError(f'{len(missing)} file video tidak ditemukan.')
        urls = [p for p in source_list if _is_url(p)]
        resolved = []
        done = 0
        for p in source_list:
            if _is_url(p):
                done += 1
                label = f'Mengunduh video {done}/{len(urls)}'
                update(f'{label}...', pct=3)
                resolved.append(download_video(
                    p, DOWNLOAD_DIR,
                    progress=lambda pr, _l=label: update(f'{_l}... {pr}%', pct=3 + int(pr * 0.17))))
            else:
                resolved.append(p)
        return resolved
    source = str(job.get('source') or '').strip()
    if not source:
        raise RuntimeError('Sumber video kosong.')
    if job.get('is_url'):
        update('Mengunduh video...', pct=5)
        video = download_video(source, DOWNLOAD_DIR, progress=lambda p: update(f'Mengunduh... {p}%', pct=5 + int(p * 0.20)))
        return [video]
    if not os.path.isfile(source):
        raise RuntimeError('File video tidak ditemukan.')
    return [source]


# Jenis konten -> awalan folder. Dipakai supaya hasil render langsung bisa
# dilacak: dari niche apa dan tempat/produk mana, tanpa membuka isinya.
_AWALAN_NICHE = {
    'tiktok_go_hotel':   'hotel',
    'tiktok_go_wisata':  'wisata',
    'tiktok_go_kuliner': 'kuliner',
    'tiktok_aff':        'affiliate',
    'shopee':            'affiliate',
    'hotel':             'hotel',
    'wisata':            'wisata',
    'kuliner':           'kuliner',
    'affiliate':         'affiliate',
}


def _label_konten(job):
    """Nama pelacak untuk folder/berkas hasil: (awalan_niche, label).

    Untuk konten TikTok Go, `label` adalah NAMA LOKASI yang diisi user di
    Generator ("Nama konten/lokasi"). Untuk yang lain dipakai jenis kontennya.
    Keduanya opsional — kalau kosong, penamaan lama tetap dipakai.
    """
    niche = str(job.get('niche') or job.get('platform') or '').strip().lower()
    awalan = _AWALAN_NICHE.get(niche, '')
    label = _safe(job.get('label') or job.get('title') or '') if (
        job.get('label') or job.get('title')) else ''
    return awalan, label


def _nama_pelacak(job, cadangan=''):
    """Gabungan awalan + label, mis. 'hotel_Cahaya_Villa_Garut'.

    Mengembalikan '' kalau tidak ada identitas SAMA SEKALI, supaya penamaan
    lama ('clip_001_...') tidak berubah. _safe('') sendiri mengembalikan
    'video', jadi nilainya tidak boleh dipakai mentah-mentah di sini.
    """
    awalan, label = _label_konten(job)
    inti = label or (_safe(cadangan) if str(cadangan).strip() else '')
    bagian = [b for b in (awalan, inti) if b]
    gabung = '_'.join(bagian)
    # Truncate bersih — tidak potong di tengah underscore
    if len(gabung) > 70:
        gabung = gabung[:70].rstrip('_')
    return gabung


def _default_output_dir(videos, job):
    custom = str(job.get('output_dir') or '').strip()
    if custom:
        return os.path.abspath(custom)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    cadangan = (os.path.splitext(os.path.basename(videos[0]))[0]
                if len(videos) == 1 else 'split_batch')
    nama = _nama_pelacak(job, cadangan) or 'split_batch'
    return os.path.join(EXPORT_DIR, f'{nama}_{stamp}')


def _compute_segments(duration, split_mode, seg_duration, seg_count):
    if split_mode == 'fixed':
        segment_duration = max(1.0, float(seg_duration or 60))
        count = max(1, math.ceil(duration / segment_duration))
        return count, segment_duration
    if split_mode == 'count':
        count = max(1, int(seg_count or 5))
        return count, duration / count
    return 1, duration


def _capture_frames(video_path, out_dir, ratio, interval):
    frames_dir = os.path.join(out_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)
    vf = _center_crop_filter(ratio)
    cmd = [FFMPEG, '-y', '-i', video_path, '-vf', f'{vf},fps=1/{max(1, int(interval or 10))}', os.path.join(frames_dir, 'frame_%04d.jpg')]
    subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
    return len([name for name in os.listdir(frames_dir) if name.lower().endswith('.jpg')])


def run_repurpose(job, update):
    ok, reason = tools_available()
    if not ok:
        update(reason, done=True, success=False, error=reason)
        return

    ratio = job.get('ratio') or '9:16'
    split_mode = job.get('split_mode') or 'count'
    frame_mode = bool(job.get('frames'))
    face_tracker = bool(job.get('face_tracker'))

    try:
        videos = _resolve_videos(job, update)
    except Exception as exc:
        update(str(exc), done=True, success=False, error=str(exc))
        return

    if face_tracker:
        ft_ok, ft_reason = face_tracker_available()
        if not ft_ok:
            update(ft_reason + ' Face Tracker dimatikan otomatis.', pct=2)
            face_tracker = False

    out_root = _default_output_dir(videos, job)
    os.makedirs(out_root, exist_ok=True)

    encoder = detect_best_encoder(FFMPEG)
    total_videos = len(videos)
    total_saved = []
    total_skipped = 0
    frame_count = 0

    if frame_mode and total_videos == 1 and split_mode == 'none':
        video_path = videos[0]
        update('Mengekstrak frame...', pct=20)
        frame_count = _capture_frames(video_path, out_root, ratio, job.get('frame_interval') or 10)
        if frame_count == 0:
            update('Tidak ada frame yang dihasilkan.', done=True, success=False, error='Output kosong.')
            return
        update(f'Selesai: {frame_count} frame.', pct=100, done=True, success=True, out_dir=out_root, clips=0, frames=frame_count)
        return

    progress_start = 12
    progress_span = 84

    # ── Mode Ultra Cepat ──
    # Strategi 2-pass untuk file besar:
    # Pass 1: split seluruh video SEKALIGUS dengan segment muxer + -c copy
    #         (baca file 1x, potong di keyframe, tanpa encode → ultra cepat)
    # Pass 2: crop/resize setiap segmen pendek secara paralel
    #         (tiap segmen kecil, encode cepat)
    # Untuk rasio 'original' (tanpa crop): langsung 1-pass copy, selesai.
    turbo = bool(job.get('turbo'))
    copy_mode = (ratio == 'original') and not face_tracker
    # 2-pass hanya untuk turbo + ada crop + tanpa face tracker
    two_pass = turbo and not copy_mode and not face_tracker
    if turbo:
        workers = 6 if copy_mode else (3 if encoder != 'libx264' else max(2, (os.cpu_count() or 4) // 2))
    else:
        workers = 1

    # Kumpulkan semua tugas segmen dulu
    tasks = []
    for video_idx, video_path in enumerate(videos, 1):
        duration = probe_duration(video_path)
        if duration <= 0:
            total_skipped += 1
            update(f'Video tidak bisa dibaca: {os.path.basename(video_path)}', pct=progress_start)
            continue
        seg_count, seg_duration = _compute_segments(duration, split_mode, job.get('seg_duration') or 60, job.get('seg_count') or 5)
        video_label = os.path.basename(video_path)
        base_dir = out_root
        if total_videos > 1:
            base_dir = os.path.join(out_root, _safe(os.path.splitext(video_label)[0]))
            os.makedirs(base_dir, exist_ok=True)
        # Awalan nama berkas ikut identitas konten, supaya klip yang sudah
        # keluar dari foldernya (mis. dipindah ke folder upload) tetap bisa
        # dilacak asalnya. Kosong = penamaan lama 'clip_001_...'.
        jejak = _nama_pelacak(job)
        awalan_klip = f'{jejak}_' if jejak else ''
        for seg_idx in range(seg_count):
            start_sec = seg_idx * seg_duration
            if start_sec >= duration:
                break
            current_duration = min(seg_duration, duration - start_sec)
            if current_duration < 0.5:
                continue
            tasks.append((video_idx, video_label, video_path, base_dir, seg_idx, seg_count, start_sec, current_duration))

    total_tasks = max(1, len(tasks))
    _lock = threading.Lock()
    _done = [0]

    def _process_segment(t):
        video_idx, video_label, video_path, base_dir, seg_idx, seg_count, start_sec, current_duration = t

        # ── 2-pass mode: hanya jika pass-1 berhasil untuk video ini ──
        if two_pass and two_pass_ok.get(video_path, False):
            pass1_path = os.path.join(base_dir, f'_pass1_{seg_idx:03d}.ts')  # .ts dari segment muxer
            if not os.path.isfile(pass1_path):
                err = f'Pass-1 file tidak ditemukan: {pass1_path}'
                return ('fail', t, None, err)
            if os.path.getsize(pass1_path) == 0:
                err = f'Pass-1 file kosong (0 byte): {os.path.basename(pass1_path)}'
                return ('fail', t, None, err)
            vf = _center_crop_filter(ratio)
            out_path = os.path.join(base_dir, f'{awalan_klip}clip_{seg_idx + 1:03d}_t{int(start_sec):05d}s.mp4')
            cmd = _build_split_cmd(pass1_path, out_path, 0, current_duration, vf, encoder=encoder, crf=26)
            result = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
            ok = result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
            try:
                os.remove(pass1_path)
            except Exception:
                pass
            if not ok:
                stderr = (result.stderr or b'').decode('utf-8', errors='replace')[-500:]
                err = f'ffmpeg gagal (rc={result.returncode}): {stderr}'
                return ('fail', t, None, err)
            return ('ok', t, out_path, '')

        if face_tracker:
            vf = _face_crop_filter_for_segment(video_path, start_sec, current_duration, ratio,
                                               require_face=True)
            if vf is None:
                return ('skip_face', t, None, 'Tidak ada wajah terdeteksi')
        else:
            vf = _center_crop_filter(ratio)
        out_path = os.path.join(base_dir, f'{awalan_klip}clip_{seg_idx + 1:03d}_t{int(start_sec):05d}s.mp4')
        if copy_mode:
            cmd = [FFMPEG, '-y', '-threads', '0',
                   '-ss', str(round(start_sec, 3)), '-t', str(round(current_duration, 3)),
                   '-i', video_path, '-c', 'copy',
                   '-map_metadata', '-1', '-map_chapters', '-1',
                   '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', out_path]
        else:
            cmd = _build_split_cmd(video_path, out_path, round(start_sec, 3), round(current_duration, 3),
                                   vf, encoder=encoder, crf=26)
        result = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
        ok = result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        if not ok:
            stderr = (result.stderr or b'').decode('utf-8', errors='replace')[-500:]
            err = f'ffmpeg gagal (rc={result.returncode}): {stderr}'
            return ('fail', t, None, err)
        return ('ok', t, out_path, '')

    error_log = []  # kumpulkan semua error detail untuk ditampilkan di UI

    def _report(status, t, out_path, err_detail=''):
        nonlocal total_skipped
        video_idx, video_label, _vp, _bd, seg_idx, seg_count, _ss, _cd = t
        with _lock:
            _done[0] += 1
            pct = min(99, progress_start + int(_done[0] / total_tasks * progress_span))
            if status == 'ok':
                total_saved.append(out_path)
                update(f'[{video_idx}/{total_videos}] {video_label} segmen {seg_idx + 1}/{seg_count} selesai.', pct=pct)
            elif status == 'skip_face':
                total_skipped += 1
                update(f'Lewati {video_label} segmen {seg_idx + 1}/{seg_count}: tidak ada wajah.', pct=pct)
            else:
                total_skipped += 1
                msg = f'[{video_idx}/{total_videos}] {video_label} segmen {seg_idx + 1} gagal.'
                if err_detail:
                    error_log.append(f'Segmen {seg_idx + 1}: {err_detail}')
                    msg += f' {err_detail[:120]}'
                update(msg, pct=pct)

    # ── Pass 1 (2-pass mode): split semua video sekaligus dengan segment muxer ──
    # two_pass_ok: set per video_path, True jika pass-1 berhasil
    two_pass_ok = {}   # {video_path: bool}
    if two_pass:
        for video_idx, video_path in enumerate(videos, 1):
            duration = probe_duration(video_path)
            seg_count, seg_duration = _compute_segments(duration, split_mode, job.get('seg_duration') or 60, job.get('seg_count') or 5)
            video_label = os.path.basename(video_path)
            base_dir = out_root
            if total_videos > 1:
                base_dir = os.path.join(out_root, _safe(os.path.splitext(video_label)[0]))
            update(f'[{video_idx}/{total_videos}] Split cepat {video_label} → {seg_count} segmen...', pct=progress_start)
            pass1_files = _split_one_shot(video_path, base_dir, seg_duration, seg_count,
                                          prefix='_pass1', update=None)
            if not pass1_files:
                # Segment muxer gagal — fallback ke encode langsung per segmen
                two_pass_ok[video_path] = False
                # Log error ffmpeg untuk debug
                stderr_log = _split_one_shot_stderr(video_path, base_dir, seg_duration, seg_count)
                update(f'[{video_idx}/{total_videos}] Split cepat gagal, pakai metode standar. ({stderr_log[:120]})', pct=progress_start)
            else:
                two_pass_ok[video_path] = True
                update(f'[{video_idx}/{total_videos}] Pass 1 selesai ({len(pass1_files)} segmen), mulai crop paralel...', pct=progress_start + 5)

    if workers <= 1:
        for t in tasks:
            _report(*_process_segment(t))
    else:
        import concurrent.futures as _fut
        update(f'Mode Ultra Cepat: {workers} worker paralel'
               + (' + stream copy (tanpa re-encode)' if copy_mode else
                  ' + 2-pass (split cepat → crop paralel)' if two_pass else '') + '...', pct=progress_start)
        with _fut.ThreadPoolExecutor(max_workers=workers) as ex:
            for result_tuple in ex.map(_process_segment, tasks):
                _report(*result_tuple)

    if not total_saved:
        message = 'Tidak ada clip yang tersimpan.'
        if total_skipped:
            message += f' {total_skipped} segmen di-skip/gagal.'
        if error_log:
            message += '\n\nDetail error:\n' + '\n'.join(error_log[:10])
        update(message, done=True, success=False, error=message,
               log=error_log[:20] if error_log else [])
        return

    message = f'Selesai: {len(total_saved)} klip'
    if total_skipped:
        message += f' | {total_skipped} skip'
    update(message + '.', pct=100, done=True, success=True,
           out_dir=out_root, clips=len(total_saved),
           frames=frame_count, skipped=total_skipped, files=total_saved,
           log=error_log[:20] if error_log else [])

def _split_one_shot(video_path, out_dir, seg_duration_sec, seg_count, prefix='seg', update=None):
    """Split video menjadi segmen dengan ffmpeg segment muxer — 1 perintah, baca file 1x.
    Jauh lebih cepat dari N perintah terpisah karena file dibaca sekali dari awal.
    Return: list path file hasil atau None jika gagal.
    """
    os.makedirs(out_dir, exist_ok=True)
    out_pattern = os.path.join(out_dir, f'{prefix}_%03d.ts')
    cmd = [
        FFMPEG, '-y',
        '-i', video_path,
        '-map', '0:v:0',
        '-map', '0:a:0',
        '-c', 'copy',
        '-f', 'segment',
        '-segment_time', str(int(seg_duration_sec)),  # cukup ini saja, tidak perlu segment_times
        '-reset_timestamps', '1',
        '-map_metadata', '-1',
        '-map_chapters', '-1',
        '-avoid_negative_ts', 'make_zero',
        out_pattern,
    ]
    if update:
        update('Split cepat: memproses 1 perintah ffmpeg untuk semua segmen...', pct=None)
    # PENTING: tidak pakai creationflags=_NOWIN — CREATE_NO_WINDOW menyebabkan
    # segment muxer gagal dengan "Could not write header" di Windows.
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return None
    files = sorted(
        (os.path.join(out_dir, f) for f in os.listdir(out_dir)
         if f.startswith(prefix + '_') and f.endswith('.ts') and not f.startswith(prefix + '_debug_')),
        key=lambda p: p
    )
    return [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0] or None


def _split_one_shot_stderr(video_path, out_dir, seg_duration_sec, seg_count, prefix='_pass1'):
    """Jalankan ulang _split_one_shot dan return stderr untuk debug."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        out_pattern = os.path.join(out_dir, f'{prefix}_debug_%03d.mp4')
        times = ','.join(str(int(i * seg_duration_sec)) for i in range(1, seg_count))
        cmd = [FFMPEG, '-y', '-i', video_path, '-c', 'copy',
               '-f', 'segment', '-segment_time', str(int(seg_duration_sec)),
               '-segment_times', times, '-reset_timestamps', '1',
               '-map_metadata', '-1', '-map_chapters', '-1',
               '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart',
               out_pattern]
        r = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN, timeout=30)
        # Hapus file debug
        for f in os.listdir(out_dir):
            if f.startswith(f'{prefix}_debug_'):
                try:
                    os.remove(os.path.join(out_dir, f))
                except Exception:
                    pass
        stderr = (r.stderr or b'').decode('utf-8', errors='replace')
        # Ambil baris error terakhir yang informatif
        lines = [l.strip() for l in stderr.splitlines() if l.strip() and ('Error' in l or 'Invalid' in l or 'error' in l)]
        return lines[-1] if lines else stderr[-200:] if stderr else 'unknown error'
    except Exception as e:
        return str(e)
    """Split video menjadi segmen dengan ffmpeg segment muxer — 1 perintah, baca file 1x.

    Ini jauh lebih cepat dari N perintah ffmpeg terpisah karena:
    - File dibaca sekali dari awal sampai akhir (sequential read)
    - Tidak ada seek ke tengah file untuk setiap segmen
    - -c copy = tidak ada decode/encode, hanya potong di keyframe
    - Cocok untuk video besar (puluhan menit, berjam-jam)

    Return: list path file hasil segmen yang berhasil dibuat.
    """
    os.makedirs(out_dir, exist_ok=True)
    out_pattern = os.path.join(out_dir, f'{prefix}_%03d.mp4')
    # Buat daftar waktu potongan (segment_times)
    # ffmpeg segment_times = "durasi,durasi*2,durasi*3,..."
    times = ','.join(str(int(i * seg_duration_sec)) for i in range(1, seg_count))
    cmd = [
        FFMPEG, '-y',
        '-i', video_path,
        '-c', 'copy',
        '-bsf:v', 'h264_mp4toannexb',  # wajib untuk H264 dari MP4 ke segment muxer
        '-f', 'segment',
        '-segment_time', str(int(seg_duration_sec)),
        '-segment_times', times,
        '-reset_timestamps', '1',
        '-map_metadata', '-1',
        '-map_chapters', '-1',
        '-avoid_negative_ts', 'make_zero',
        '-movflags', '+faststart',
        out_pattern,
    ]
    if update:
        update('Split cepat: memproses 1 perintah ffmpeg untuk semua segmen...', pct=None)
    result = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
    if result.returncode != 0:
        return None  # fallback ke metode lama
    # Kumpulkan file hasil
    files = sorted(
        (os.path.join(out_dir, f) for f in os.listdir(out_dir)
         if f.startswith(prefix + '_') and f.endswith('.mp4')),
        key=lambda p: p
    )
    return [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0]


# ─── Port inti LUMINA: Smart Capture, Image-to-Video, Video Mixer ────────────

CINEMATIC_EFFECTS = [
    'Zoom In', 'Zoom Out', 'Pull In', 'Pull Out',
    'Slide Left', 'Slide Right', 'Slide Up', 'Slide Down',
    'Clockwise', 'Counter-Clockwise', 'Shake', 'Push', 'Wipe', 'Cover', 'Blur', 'None',
]


def cinematic_zoom_filter(effect='Zoom In', duration=5, target_ratio='9:16', crop_x=0.5, crop_y=0.5):
    """Filter zoompan sinematik untuk image-to-video (port RenderService LUMINA)."""
    try:
        rw, rh = RATIOS.get(target_ratio, (9, 16))
        if rw < rh:
            out_w, out_h = 1080, 1920
        elif rw > rh:
            out_w, out_h = 1920, 1080
        else:
            out_w, out_h = 1080, 1080
        fps = 25
        total_frames = int(duration * fps)
        base = 'scale=w=3840:h=-1,format=yuv420p'
        fx = f'iw*{crop_x}'
        fy = f'ih*{crop_y}'
        x_pos = f'min(max({fx}-(iw/zoom/2),0),iw-(iw/zoom))'
        y_pos = f'min(max({fy}-(ih/zoom/2),0),ih-(ih/zoom))'
        if effect in ('Zoom In', 'Pull In'):
            zp = f"zoompan=z='min(zoom+0.0012,1.5)':d={total_frames}:x='{x_pos}':y='{y_pos}':s={out_w}x{out_h}"
            return f'{base},{zp}'
        if effect in ('Zoom Out', 'Pull Out'):
            zp = f"zoompan=z='max(1.5-0.0012*on,1)':d={total_frames}:x='{x_pos}':y='{y_pos}':s={out_w}x{out_h}"
            return f'{base},{zp}'
        if effect in ('Slide Left', 'Push'):
            zp = f"zoompan=z=1.4:d={total_frames}:x='(iw-iw/zoom)*(1-on/{total_frames})':y='{y_pos}':s={out_w}x{out_h}"
            return f'{base},{zp}'
        if effect in ('Slide Right', 'Wipe'):
            zp = f"zoompan=z=1.4:d={total_frames}:x='(iw-iw/zoom)*(on/{total_frames})':y='{y_pos}':s={out_w}x{out_h}"
            return f'{base},{zp}'
        if effect in ('Slide Up', 'Cover'):
            zp = f"zoompan=z=1.4:d={total_frames}:x='{x_pos}':y='(ih-ih/zoom)*(1-on/{total_frames})':s={out_w}x{out_h}"
            return f'{base},{zp}'
        if effect == 'Slide Down':
            zp = f"zoompan=z=1.4:d={total_frames}:x='{x_pos}':y='(ih-ih/zoom)*(on/{total_frames})':s={out_w}x{out_h}"
            return f'{base},{zp}'
        if effect == 'Clockwise':
            zp = f"zoompan=z=1.2:d={total_frames}:x='{x_pos}':y='{y_pos}':s={out_w}x{out_h}"
            return f"{base},{zp},rotate='2*PI*t/{duration}'"
        if effect == 'Counter-Clockwise':
            zp = f"zoompan=z=1.2:d={total_frames}:x='{x_pos}':y='{y_pos}':s={out_w}x{out_h}"
            return f"{base},{zp},rotate='-2*PI*t/{duration}'"
        if effect == 'Shake':
            zp = f"zoompan=z=1.2:d={total_frames}:x='{x_pos}':y='{y_pos}':s={out_w}x{out_h}"
            shake = f'crop=iw-40:ih-40:20+15*sin(5*PI*t):20+15*cos(5*PI*t),scale={out_w}:{out_h}'
            return f'{base},{zp},{shake}'
        if effect == 'Blur':
            zp = f"zoompan=z=1.1:d={total_frames}:x='{x_pos}':y='{y_pos}':s={out_w}x{out_h}"
            return f"{base},{zp},boxblur='min(t*5, 15)'"
        return (f'scale=w=3000:h=-1,crop={out_w}:{out_h}:iw*{crop_x}-ow/2:ih*{crop_y}-oh/2,'
                f'scale={out_w}:{out_h}')
    except Exception:
        return 'scale=1080:-1,crop=1080:1920'


def smart_capture(job, update):
    """Capture frame per interval ala LUMINA: Face Tracker ON = frame tanpa wajah
    di-skip & crop menyertakan dahi+dagu; OFF = center crop ke rasio target."""
    source = str(job.get('source') or '').strip()
    if not source:
        raise RuntimeError('Sumber video kosong.')
    if job.get('is_url') or _is_url(source):
        update('Mengunduh video...', pct=3)
        source = download_video(source, DOWNLOAD_DIR,
                                progress=lambda p: update(f'Mengunduh... {p}%', pct=3 + int(p * 0.20)))
    if not os.path.isfile(source):
        raise RuntimeError('File video tidak ditemukan.')

    interval = max(1, int(job.get('interval') or 10))
    ratio = job.get('ratio') or '9:16'
    face_track = bool(job.get('face_tracker'))
    out_dir = (job.get('output_dir') or '').strip()
    if not out_dir:
        stamp = time.strftime('%Y%m%d_%H%M%S')
        out_dir = os.path.join(EXPORT_DIR, f'capture_{stamp}')
    os.makedirs(out_dir, exist_ok=True)

    cv2 = _get_cv2()
    if cv2 is None:
        if face_track:
            raise RuntimeError('Face Tracker butuh OpenCV (pip install opencv-python).')
        update('OpenCV tidak ada — capture via ffmpeg (center crop)...', pct=30)
        vf = _center_crop_filter(ratio)
        cmd = [FFMPEG, '-y', '-i', source, '-vf', f'{vf},fps=1/{interval}',
               os.path.join(out_dir, 'frame_%04d.jpg')]
        subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
        n = len([f for f in os.listdir(out_dir) if f.lower().endswith('.jpg')])
        update(f'Selesai: {n} frame.', pct=100, done=True, success=True, out_dir=out_dir, frames=n)
        return

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError('Video tidak bisa dibuka.')
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_f = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        total_dur = total_f / fps if fps else 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frontal, profile = _get_cascades(cv2)

        saved = skipped = 0
        t = 0.0
        while t < total_dur:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
            ok, img = cap.read()
            if not ok:
                break
            final_img = img
            keep = True
            if face_track:
                faces = _detect_faces_in_frame(img, cv2, frontal, profile, min_face_ratio=0.08)
                best = _best_face(faces, width, height)
                if best:
                    fx, fy, fw, fh = best
                    cx, cy = fx + fw // 2, fy + fh // 2
                    x, y, w, h = _face_to_crop_params(cx, cy, fw, fh, width, height, ratio)
                    final_img = img[y:y + h, x:x + w]
                else:
                    keep = False
            elif ratio != 'original':
                x, y, w, h = _face_to_crop_params(width // 2, height // 2, 0, 0, width, height, ratio)
                final_img = img[y:y + h, x:x + w]
            if keep:
                saved += 1
                cv2.imwrite(os.path.join(out_dir, f'frame_{saved:04d}.jpg'), final_img)
            else:
                skipped += 1
            t += interval
            pct = 25 + int(min(t / total_dur, 1.0) * 70) if total_dur else 90
            update(f'Capture {saved} frame' + (f' ({skipped} skip tanpa wajah)' if skipped else '') + '...', pct=pct)
    finally:
        cap.release()

    msg = f'Selesai: {saved} frame'
    if skipped:
        msg += f' | {skipped} skip (tanpa wajah)'
    update(msg + '.', pct=100, done=True, success=True, out_dir=out_dir, frames=saved, skipped=skipped)


def images_to_video(job, update):
    """Ubah gambar jadi klip video sinematik (efek zoompan) — port LUMINA."""
    images = [str(p).strip() for p in (job.get('images') or []) if str(p).strip()]
    if not images:
        raise RuntimeError('Pilih minimal 1 gambar.')
    missing = [p for p in images if not os.path.isfile(p)]
    if missing:
        raise RuntimeError(f'{len(missing)} gambar tidak ditemukan.')

    effect = job.get('effect') or 'Zoom In'
    duration = max(1, int(job.get('duration') or 5))
    ratio = job.get('ratio') or '9:16'
    out_dir = (job.get('output_dir') or '').strip()
    if not out_dir:
        stamp = time.strftime('%Y%m%d_%H%M%S')
        out_dir = os.path.join(EXPORT_DIR, f'img2video_{stamp}')
    os.makedirs(out_dir, exist_ok=True)

    encoder = detect_best_encoder(FFMPEG)
    enc_args = _get_encoder_args(encoder, 22)
    crop_x = float(job.get('crop_x') or 0.5)
    crop_y = float(job.get('crop_y') or 0.5)
    is_random = str(effect).strip().lower() == 'random'
    _rnd_pool = [e for e in CINEMATIC_EFFECTS if e != 'None']

    def _vf_for(idx):
        """Efek per gambar — mode Random memilih efek berbeda tiap gambar."""
        if is_random:
            import random as _r
            eff = _r.choice(_rnd_pool)
        else:
            eff = effect
        return eff, cinematic_zoom_filter(eff, duration, ratio, crop_x, crop_y)

    results = []
    _lock = threading.Lock()
    _done = [0]

    def _render_one(img):
        idx = images.index(img)
        eff, vf = _vf_for(idx)
        base = _safe(os.path.splitext(os.path.basename(img))[0])
        out_path = os.path.join(out_dir, f'vid_{base}.mp4')
        cmd = [FFMPEG, '-y', '-loop', '1', '-i', img, '-vf', vf,
               '-t', str(duration), '-r', '25', '-pix_fmt', 'yuv420p'] + enc_args + ['-an', out_path]
        res = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
        ok = res.returncode == 0 and os.path.isfile(out_path)
        with _lock:
            _done[0] += 1
            label = f' [{eff}]' if is_random else ''
            update(f'Render {_done[0]}/{len(images)}: {os.path.basename(img)}{label}',
                   pct=int(_done[0] / len(images) * 95) + 3)
            if ok:
                results.append(out_path)

    workers = min(3, len(images)) if len(images) > 1 else 1
    if workers > 1:
        import concurrent.futures as _fut
        with _fut.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_render_one, images))
    else:
        for img in images:
            _render_one(img)
    if not results:
        raise RuntimeError('Semua render gagal — cek ffmpeg / file gambar.')
    update(f'Selesai: {len(results)} video.', pct=100, done=True, success=True,
           out_dir=out_dir, clips=len(results), files=results)


# ─── Video Mixer (port MixerWorker LUMINA) ───────────────────────────────────

_MIX_EXTS = ('.mp4', '.mov', '.mkv', '.avi', '.webm', '.mpeg', '.mpg')


def _scan_mix_clips(folder):
    """Subfolder mode (kalau ada subfolder) atau flat mode."""
    if not folder or not os.path.isdir(folder):
        return []
    subs = [os.path.join(folder, d) for d in os.listdir(folder)
            if os.path.isdir(os.path.join(folder, d))]
    roots = subs if subs else [folder]
    clips = []
    for root in roots:
        for name in os.listdir(root):
            if name.lower().endswith(_MIX_EXTS):
                clips.append(os.path.join(root, name))
    return sorted(set(clips))


def _scan_flat_clips(folder):
    if not folder or not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, n) for n in os.listdir(folder)
                  if n.lower().endswith(_MIX_EXTS))


def _select_mix_clips(pool_files, target_duration, shuffle=True):
    import random
    pool = list(pool_files)
    if shuffle:
        random.shuffle(pool)
    selected, total, idx, loops = [], 0.0, 0, 0
    while total < target_duration and len(selected) <= 200:
        if idx >= len(pool):
            if loops >= 5:
                break
            random.shuffle(pool)
            idx, loops = 0, loops + 1
        d = probe_duration(pool[idx]) or 0
        if d > 0:
            selected.append(pool[idx])
            total += d
        idx += 1
    return selected


def _select_mix_alternating(folder1, folder2, target_duration, shuffle=True):
    import random
    p1 = {'clips': _scan_flat_clips(folder1), 'idx': 0, 'loops': 0}
    p2 = {'clips': _scan_flat_clips(folder2), 'idx': 0, 'loops': 0}
    if shuffle:
        random.shuffle(p1['clips'])
        random.shuffle(p2['clips'])

    def nxt(p):
        if not p['clips']:
            return None
        if p['idx'] >= len(p['clips']):
            if p['loops'] >= 50:
                return None
            p['idx'], p['loops'] = 0, p['loops'] + 1
            if shuffle:
                random.shuffle(p['clips'])
        c = p['clips'][p['idx']]
        p['idx'] += 1
        return c

    selected, total, turn = [], 0.0, 0
    while total < target_duration and len(selected) < 300:
        clip = nxt(p1 if turn == 0 else p2)
        if clip is None:
            clip = nxt(p2 if turn == 0 else p1)
        if clip is None:
            break
        d = probe_duration(clip) or 0
        if d > 0:
            selected.append(clip)
            total += d
        turn = 1 - turn
    return selected


def _build_mix_cmd(clips, job_cfg, w, h, encoder):
    audio_path = job_cfg.get('audio_path') or ''
    backsound = job_cfg.get('backsound_path') or ''
    wm = job_cfg.get('wm_path') or ''
    audio_vol = float(job_cfg.get('audio_vol', 100)) / 100
    pitch = float(job_cfg.get('audio_pitch', 0))
    bs_vol = float(job_cfg.get('backsound_vol', 50)) / 100
    fps = 30

    has_audio = bool(audio_path and os.path.isfile(audio_path))
    has_bs = bool(backsound and os.path.isfile(backsound))
    has_wm = bool(wm and os.path.isfile(wm))

    cmd = [FFMPEG, '-y']
    for c in clips:
        cmd += ['-i', c]
    idx = len(clips)
    if has_audio:
        cmd += ['-i', audio_path]
        a_idx = idx
        idx += 1
    if has_bs:
        cmd += ['-i', backsound]
        bs_idx = idx
        idx += 1
    if has_wm:
        cmd += ['-i', wm]
        wm_idx = idx
        idx += 1

    f = []
    for i in range(len(clips)):
        f.append(f'[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,'
                 f'crop={w}:{h},fps={fps},setsar=1[v{i}]')
    f.append(''.join(f'[v{i}]' for i in range(len(clips))) +
             f'concat=n={len(clips)}:v=1:a=0[vraw]')

    if has_wm:
        wm_scale = float(job_cfg.get('wm_scale', 0.15))
        wm_x_pct = float(job_cfg.get('wm_x_pct', 0.85))
        wm_y_pct = float(job_cfg.get('wm_y_pct', 0.05))
        wm_opacity = float(job_cfg.get('wm_opacity', 1.0))
        wm_w = int(w * wm_scale)
        wm_x = max(0, min(int(w * wm_x_pct - wm_w / 2), w - wm_w))
        wm_y = max(0, min(int(h * wm_y_pct), h - 20))
        f.append(f'[{wm_idx}:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa={wm_opacity:.2f}[wm]')
        f.append(f'[vraw][wm]overlay={wm_x}:{wm_y}[vout]')
    else:
        f.append('[vraw]copy[vout]')

    if has_audio:
        if pitch:
            rate = 2 ** (pitch / 12)
            f.append(f'[{a_idx}:a]asetrate=44100*{rate:.6f},aresample=44100,volume={audio_vol:.3f}[amain]')
        else:
            f.append(f'[{a_idx}:a]volume={audio_vol:.3f}[amain]')
        if has_bs:
            f.append(f'[{bs_idx}:a]volume={bs_vol:.3f}[abs]')
            f.append('[amain][abs]amix=inputs=2:duration=first:normalize=0[aout]')
        else:
            f.append('[amain]acopy[aout]')
        cmd += ['-filter_complex', ';'.join(f), '-map', '[vout]', '-map', '[aout]']
    elif has_bs:
        f.append(f'[{bs_idx}:a]volume={bs_vol:.3f}[aout]')
        cmd += ['-filter_complex', ';'.join(f), '-map', '[vout]', '-map', '[aout]']
    else:
        cmd += ['-filter_complex', ';'.join(f), '-map', '[vout]', '-an']

    cmd += _get_encoder_args(encoder, 23)
    if has_audio or has_bs:
        cmd += ['-c:a', 'aac', '-b:a', '192k']
    cmd += ['-t', str(job_cfg['target_duration']), '-map_metadata', '-1', job_cfg['output_path']]
    return cmd


def _build_mix_original_audio_cmd(clips, job_cfg, w, h, encoder):
    backsound = job_cfg.get('backsound_path') or ''
    wm = job_cfg.get('wm_path') or ''
    orig_vol = float(job_cfg.get('audio_vol', 100)) / 100
    bs_vol = float(job_cfg.get('backsound_vol', 50)) / 100
    target_duration = float(job_cfg.get('target_duration') or 0)
    fps = 30

    has_bs = bool(backsound and os.path.isfile(backsound))
    has_wm = bool(wm and os.path.isfile(wm))

    cmd = [FFMPEG, '-y']
    clip_durs = []
    for c in clips:
        clip_durs.append(max(probe_duration(c) or 0.1, 0.1))
        cmd += ['-i', c]

    idx = len(clips)
    if has_bs:
        cmd += ['-stream_loop', '-1', '-i', backsound]
        bs_idx = idx
        idx += 1
    if has_wm:
        cmd += ['-i', wm]
        wm_idx = idx
        idx += 1

    f = []
    for i, dur in enumerate(clip_durs):
        f.append(f'[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,'
                 f'crop={w}:{h},fps={fps},setsar=1,setpts=PTS-STARTPTS[v{i}]')
        if probe_has_audio(clips[i]):
            f.append(f'[{i}:a]volume={orig_vol:.3f},'
                     f'aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,'
                     f'apad,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]')
        else:
            f.append(f'anullsrc=channel_layout=stereo:sample_rate=44100,'
                     f'atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]')

    f.append(''.join(f'[v{i}][a{i}]' for i in range(len(clips))) +
             f'concat=n={len(clips)}:v=1:a=1[vraw][araw]')

    if has_wm:
        wm_scale = float(job_cfg.get('wm_scale', 0.15))
        wm_x_pct = float(job_cfg.get('wm_x_pct', 0.85))
        wm_y_pct = float(job_cfg.get('wm_y_pct', 0.05))
        wm_opacity = float(job_cfg.get('wm_opacity', 1.0))
        wm_w = int(w * wm_scale)
        wm_x = max(0, min(int(w * wm_x_pct - wm_w / 2), w - wm_w))
        wm_y = max(0, min(int(h * wm_y_pct), h - 20))
        f.append(f'[{wm_idx}:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa={wm_opacity:.2f}[wm]')
        f.append(f'[vraw][wm]overlay={wm_x}:{wm_y}[vout]')
    else:
        f.append('[vraw]copy[vout]')

    if has_bs:
        f.append(f'[{bs_idx}:a]volume={bs_vol:.3f},'
                 f'aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,'
                 f'atrim=0:{target_duration:.3f},asetpts=PTS-STARTPTS[abs]')
        f.append('[araw][abs]amix=inputs=2:duration=first:normalize=0[aout]')
    else:
        f.append('[araw]anull[aout]')

    cmd += ['-filter_complex', ';'.join(f), '-map', '[vout]', '-map', '[aout]']
    cmd += _get_encoder_args(encoder, 23)
    cmd += ['-c:a', 'aac', '-b:a', '192k', '-t', f'{target_duration:.3f}',
            '-map_metadata', '-1', job_cfg['output_path']]
    return cmd


def _caption_txt_path(video_path):
    base, _ext = os.path.splitext(str(video_path))
    return base + "_caption_hashtag.txt"


def _write_caption_sidecar(video_path, data):
    txt_path = _caption_txt_path(video_path)
    keywords = data.get('seo_keywords') or []
    if isinstance(keywords, (list, tuple)):
        keyword_text = ', '.join(str(k).strip() for k in keywords if str(k).strip())
    else:
        keyword_text = str(keywords or '').strip()
    lines = [
        'JUDUL SEO:',
        str(data.get('title') or '').strip(),
        '',
        'RINGKASAN VIDEO:',
        str(data.get('summary') or '').strip(),
        '',
        'CAPTION:',
        str(data.get('caption') or '').strip(),
        '',
        'HASHTAG:',
        str(data.get('hashtags') or '').strip(),
        '',
        'KEYWORD SEO:',
        keyword_text,
        '',
        f'VIDEO: {os.path.basename(str(video_path))}',
    ]
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines).strip() + '\n')
    return txt_path


def _fallback_caption_for_video(video_path, job):
    stem = os.path.splitext(os.path.basename(str(video_path)))[0]
    title = re.sub(r'[_-]+', ' ', stem).strip().title() or 'Konten Video'
    mode = (job.get('caption_content_type') or job.get('caption_platform') or 'konten').lower()
    if 'shopee' in mode or 'aff' in mode:
        cta = 'Cek detailnya lewat keranjang kuning dan simpan dulu kalau kamu butuh rekomendasi ini.'
        tags = '#shopeeaffiliate #racunshopee #shopeefinds #rekomendasiproduk #tiktokshop #produkviral #fyp #foryou'
    elif 'go' in mode or 'wisata' in mode or 'kuliner' in mode or 'hotel' in mode:
        cta = 'Simpan dulu dan cek tag lokasi di bawah sebelum kamu mampir.'
        tags = '#tiktokgo #rekomendasitempat #kulinerindonesia #tempatwisata #hotelreview #hidden gem #fyp #foryou'
    else:
        cta = 'Simpan dan bagikan ke teman yang perlu lihat ini.'
        tags = '#rekomendasi #kontenviral #tipsindonesia #inspirasi #fyp #foryou #videoreels #tiktokindonesia'
    return {
        'title': title,
        'summary': 'Caption fallback dibuat dari nama file karena analisa Gemini tidak tersedia.',
        'caption': f'{title}. {cta}',
        'hashtags': tags.replace('#hidden gem', '#hiddengem'),
        'seo_keywords': [title.lower(), 'konten video', 'rekomendasi'],
    }


def _generate_caption_sidecars(job, outputs, update):
    if not bool(job.get('generate_caption', True)):
        return []
    outputs = [str(p) for p in (outputs or []) if os.path.isfile(str(p))]
    if not outputs:
        return []

    keys = [k for k in (job.get('gemini_keys') or []) if k]
    platform = job.get('caption_platform') or 'tiktok'
    content_type = job.get('caption_content_type') or 'auto'
    extra = job.get('caption_extra') or ''
    model = job.get('gemini_model') or 'gemini-flash-latest'
    sidecars = []

    generator = None
    if keys:
        try:
            from omnivora.ai_caption import GeminiCaptionGenerator
            generator = GeminiCaptionGenerator(api_keys=keys, model=model,
                                               log_cb=lambda m, l='info': update(str(m), pct=None))
        except Exception as exc:
            update(f'Generate caption: Gemini tidak siap ({exc}), pakai fallback...', pct=None)
            generator = None
    else:
        update('Generate caption: Gemini API Key kosong, pakai fallback caption.', pct=None)

    total = len(outputs)
    for idx, video_path in enumerate(outputs, 1):
        update(f'Generate caption + hashtag SEO {idx}/{total}...', pct=96)
        data = {}
        if generator:
            try:
                data = generator.generate_caption_from_video(
                    video_path,
                    platform=platform,
                    lang='id',
                    content_type=content_type,
                    extra=extra,
                ) or {}
            except Exception as exc:
                update(f'Caption Gemini gagal untuk {os.path.basename(video_path)}: {exc}', pct=None)
                data = {}
        if not data.get('caption'):
            data = _fallback_caption_for_video(video_path, job)
        try:
            txt_path = _write_caption_sidecar(video_path, data)
            sidecars.append(txt_path)
        except Exception as exc:
            update(f'WARNING: Gagal menulis caption TXT: {exc}', pct=None)
    return sidecars


def _run_video_mixer_urutan(job, update):
    """Mode 'Gabung Berurutan': user pilih video 1, 2, dst secara manual -
    digabung PERSIS sesuai urutan itu (bukan acak) untuk tiap output. Bisa
    lebih dari 1 file Voice Over sekaligus - tiap output dapat 1 VO yang
    dipilih ACAK dari daftar itu (jadi mis. 3 VO bisa dipakai untuk 100
    output). Durasi tiap output mengikuti durasi VO-nya masing-masing: video
    dipotong kalau lebih panjang, urutan videonya diulang dari awal (bukan
    diacak, biar polanya tetap kebaca) kalau totalnya masih lebih pendek."""
    import random
    videos = [str(p) for p in (job.get('videos') or []) if os.path.isfile(str(p))]
    if not videos:
        raise RuntimeError('Tidak ada video yang dipilih.')
    vo_files = [str(p) for p in (job.get('vo_files') or []) if os.path.isfile(str(p))]
    legacy_vo = (job.get('vo_path') or '').strip()
    if not vo_files and legacy_vo and os.path.isfile(legacy_vo):
        vo_files = [legacy_vo]
    ratio = job.get('ratio') or '9:16'
    resolution = job.get('resolution') or '1080p'
    out_dir = (job.get('output_dir') or '').strip()
    if not out_dir:
        stamp = time.strftime('%Y%m%d_%H%M%S')
        out_dir = os.path.join(EXPORT_DIR, f'mixer_{stamp}')
    os.makedirs(out_dir, exist_ok=True)

    res_map = {'1080p': (1920, 1080), '720p': (1280, 720), '480p': (854, 480)}
    w, h = res_map.get(resolution, (1920, 1080))
    try:
        rw, rh = RATIOS.get(ratio, (9, 16))
        if rw < rh:
            w, h = min(w, h), max(w, h)
        elif rw == rh:
            w = h = min(w, h)
    except Exception:
        pass

    encoder = detect_best_encoder(FFMPEG)

    if not vo_files:
        valid = []
        total_dur = 0.0
        for v in videos:
            d = probe_duration(v) or 0
            if d <= 0:
                continue
            valid.append(v)
            total_dur += d
        if not valid:
            raise RuntimeError('Video yang dipilih tidak valid/tidak bisa dibaca durasinya.')

        stamp = time.strftime('%Y%m%d_%H%M%S')
        first_name = _safe(os.path.splitext(os.path.basename(valid[0]))[0]) or 'video'
        out_path = os.path.join(out_dir, f'gabung_berurutan_{first_name}_{stamp}.mp4')
        update(f'Menggabungkan {len(valid)} video berurutan tanpa VO...', pct=5)
        cfg = dict(job, target_duration=total_dur, output_path=out_path, audio_path='')
        cmd = _build_mix_original_audio_cmd(valid, cfg, w, h, encoder)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', creationflags=_NOWIN)
        for line in proc.stdout:
            if 'time=' in line:
                try:
                    ts = line.split('time=')[1].split(' ')[0]
                    hh, mm, ss = ts.split(':')
                    cur = int(hh) * 3600 + int(mm) * 60 + float(ss)
                    frac = min(cur / max(total_dur, 1), 1.0)
                    update(None, pct=5 + int(frac * 93))
                except Exception:
                    pass
        proc.wait()
        if proc.returncode != 0 or not os.path.isfile(out_path):
            raise RuntimeError('Render mixer tanpa VO gagal - cek video/ffmpeg/disk.')

        if bool(job.get('delete_used')):
            for v in set(valid):
                try:
                    os.remove(v)
                except OSError:
                    pass

        sidecars = _generate_caption_sidecars(job, [out_path], update)
        update('Selesai: 1 video tanpa VO.', pct=100, done=True, success=True,
               out_dir=out_dir, clips=1, files=[out_path], captions=sidecars)
        return

    raw_count = int(job.get('count') or 0)
    count = raw_count if raw_count > 0 else len(vo_files)
    count = min(200, count)

    # Jatah VO per output: acak, tapi diratakan (bukan random.choice polos)
    # supaya kalau VO cuma 3 dan output diminta 100, ketiganya tetap kepakai
    # merata bukan condong ke satu file saja.
    assigned = []
    pool = vo_files.copy()
    random.shuffle(pool)
    while len(assigned) < count:
        assigned.extend(pool)
        random.shuffle(pool)
    assigned = assigned[:count]

    ok_count = 0
    outputs = []
    for i in range(count):
        vo_path = assigned[i]
        target_dur = probe_duration(vo_path) or 60.0
        update(f'Job {i + 1}/{count}: durasi VO {int(target_dur)}s — menyusun urutan video...',
               pct=int(i / count * 100))

        # Ulangi seluruh urutan video dari awal (loop, BUKAN acak) kalau
        # totalnya masih kurang dari durasi VO - dibatasi 20 putaran biar
        # tidak jadi command ffmpeg raksasa kalau videonya sangat pendek/VO
        # sangat panjang.
        clips, total, loops = [], 0.0, 0
        while total < target_dur and loops < 20:
            for v in videos:
                d = probe_duration(v) or 0
                if d <= 0:
                    continue
                clips.append(v)
                total += d
                if total >= target_dur:
                    break
            loops += 1
        if not clips:
            raise RuntimeError('Video yang dipilih tidak valid/tidak bisa dibaca durasinya.')

        base = _safe(os.path.splitext(os.path.basename(vo_path))[0]) or 'mix'
        sfx = f'_{i + 1:03d}' if count > 1 else ''
        out_path = os.path.join(out_dir, f'{base}{sfx}.mp4')

        cfg = dict(job, target_duration=target_dur, output_path=out_path, audio_path=vo_path)
        cmd = _build_mix_cmd(clips, cfg, w, h, encoder)
        update(f'Job {i + 1}/{count}: render {len(clips)} video (dipotong ke {int(target_dur)}s sesuai VO)...',
               pct=int(i / count * 100) + 2)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding='utf-8', errors='replace', creationflags=_NOWIN)
        for line in proc.stdout:
            if 'time=' in line:
                try:
                    ts = line.split('time=')[1].split(' ')[0]
                    hh, mm, ss = ts.split(':')
                    cur = int(hh) * 3600 + int(mm) * 60 + float(ss)
                    frac = min(cur / max(target_dur, 1), 1.0)
                    update(None, pct=int((i + frac) / count * 100))
                except Exception:
                    pass
        proc.wait()
        if proc.returncode == 0 and os.path.isfile(out_path):
            ok_count += 1
            outputs.append(out_path)

    if not outputs:
        raise RuntimeError('Render mixer gagal — cek video/ffmpeg/disk.')

    if bool(job.get('delete_used')):
        for v in set(videos):
            try:
                os.remove(v)
            except OSError:
                pass

    sidecars = _generate_caption_sidecars(job, outputs, update)
    update(f'Selesai: {ok_count}/{count} video.', pct=100, done=True, success=True,
           out_dir=out_dir, clips=ok_count, files=outputs, captions=sidecars)


def run_video_mixer(job, update):
    """Video Mixer ala LUMINA: gabung clip acak (atau selang-seling 2 folder)
    sampai durasi audio, plus backsound, pitch, dan watermark."""
    import random
    mode = job.get('mode') or 'acak'
    if mode == 'urutan':
        return _run_video_mixer_urutan(job, update)
    raw_count = int(job.get('count') or 0)
    auto_stock = raw_count <= 0   # 0 = auto: habiskan stok clip, tiap clip dipakai sekali
    count = raw_count if not auto_stock else 0
    count = min(50, count) if count else 0
    ratio = job.get('ratio') or '9:16'
    resolution = job.get('resolution') or '1080p'
    shuffle = bool(job.get('shuffle', True))
    delete_used = bool(job.get('delete_used'))
    audio_pool = [str(p) for p in (job.get('audios') or []) if os.path.isfile(str(p))]
    out_dir = (job.get('output_dir') or '').strip()
    if not out_dir:
        stamp = time.strftime('%Y%m%d_%H%M%S')
        out_dir = os.path.join(EXPORT_DIR, f'mixer_{stamp}')
    os.makedirs(out_dir, exist_ok=True)

    f1 = f2 = folder = ''
    all_clips = []
    if mode == 'gabung':
        f1, f2 = (job.get('folder1') or '').strip(), (job.get('folder2') or '').strip()
        if not (os.path.isdir(f1) or os.path.isdir(f2)):
            raise RuntimeError('Folder Gabung Format tidak valid.')
        n_clips = len(_scan_flat_clips(f1)) + len(_scan_flat_clips(f2))
    else:
        folder = (job.get('folder') or '').strip()
        if not os.path.isdir(folder):
            raise RuntimeError('Folder sumber clip tidak ditemukan.')
        all_clips = _scan_mix_clips(folder)
        n_clips = len(all_clips)
    if not n_clips:
        raise RuntimeError('Tidak ada video clip di folder sumber.')

    # Resolusi + orientasi mengikuti rasio
    res_map = {'1080p': (1920, 1080), '720p': (1280, 720), '480p': (854, 480)}
    w, h = res_map.get(resolution, (1920, 1080))
    try:
        rw, rh = RATIOS.get(ratio, (9, 16))
        if rw < rh:
            w, h = min(w, h), max(w, h)
        elif rw == rh:
            w = h = min(w, h)
    except Exception:
        pass

    # Durasi audio & pembagian audio per job (acak, diulang jika kurang)
    durs = {a: (probe_duration(a) or 60.0) for a in audio_pool}

    # Mode AUTO (jumlah output = 0): hitung berapa video yang bisa dihasilkan
    # dari total durasi stok clip; tiap clip hanya dipakai SEKALI.
    consume_pool = None
    consume_p1 = consume_p2 = None
    if auto_stock:
        update('Menghitung stok clip...', pct=1)
        if mode == 'gabung':
            consume_p1 = _scan_flat_clips(f1)
            consume_p2 = _scan_flat_clips(f2)
            random.shuffle(consume_p1)
            random.shuffle(consume_p2)
            stock_dur = sum(probe_duration(c) or 0 for c in consume_p1 + consume_p2)
        else:
            consume_pool = all_clips.copy()
            random.shuffle(consume_pool)
            stock_dur = sum(probe_duration(c) or 0 for c in consume_pool)
        avg_target = (sum(durs.values()) / len(durs)) if durs else 60.0
        count = max(1, min(50, int(stock_dur // max(avg_target, 1))))
        update(f'Auto: stok {int(stock_dur)}s clip ÷ target ~{int(avg_target)}s '
               f'= {count} video.', pct=2)

    assigned = []
    if audio_pool:
        pool = audio_pool.copy()
        random.shuffle(pool)
        while len(assigned) < count:
            assigned.extend(pool)
            random.shuffle(pool)
        assigned = assigned[:count]

    encoder = detect_best_encoder(FFMPEG)
    ok_count = fail_count = 0
    outputs = []
    for i in range(count):
        job_audio = assigned[i] if audio_pool else ''
        target_dur = durs.get(job_audio, 60.0) if job_audio else 60.0
        base = _safe(os.path.splitext(os.path.basename(job_audio))[0]) if job_audio else 'mix'
        sfx = f'_{i + 1:02d}' if count > 1 else ''
        out_path = os.path.join(out_dir, f'{base}{sfx}.mp4')

        update(f'Job {i + 1}/{count}: memilih clip...', pct=int(i / count * 100))
        if auto_stock:
            # Konsumsi stok: ambil clip dari pool sampai cukup durasi, tanpa reuse
            clips = []
            got = 0.0
            if mode == 'gabung':
                turn = 0
                while got < target_dur and (consume_p1 or consume_p2):
                    src = consume_p1 if (turn == 0 and consume_p1) or not consume_p2 else consume_p2
                    c = src.pop(0)
                    d = probe_duration(c) or 0
                    if d > 0:
                        clips.append(c)
                        got += d
                    turn = 1 - turn
            else:
                while got < target_dur and consume_pool:
                    c = consume_pool.pop(0)
                    d = probe_duration(c) or 0
                    if d > 0:
                        clips.append(c)
                        got += d
            if not clips:
                update(f'Stok clip habis — berhenti di {ok_count} video.', pct=int(i / count * 100))
                break
            if got < target_dur * 0.5 and i > 0:
                # Sisa stok terlalu sedikit untuk 1 video utuh — jangan dipaksakan
                update(f'Sisa stok tinggal {int(got)}s (< setengah target) — berhenti di {ok_count} video.',
                       pct=int(i / count * 100))
                break
        elif mode == 'gabung':
            clips = _select_mix_alternating(f1, f2, target_dur, shuffle)
        else:
            clips = _select_mix_clips(all_clips, target_dur, shuffle)
        if not clips:
            fail_count += 1
            continue

        cfg = dict(job, target_duration=target_dur, output_path=out_path, audio_path=job_audio)
        cmd = _build_mix_cmd(clips, cfg, w, h, encoder)
        update(f'Job {i + 1}/{count}: render {len(clips)} clip (~{int(target_dur)}s)...',
               pct=int(i / count * 100) + 2)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', creationflags=_NOWIN)
        for line in proc.stdout:
            if 'time=' in line:
                try:
                    ts = line.split('time=')[1].split(' ')[0]
                    hh, mm, ss = ts.split(':')
                    cur = int(hh) * 3600 + int(mm) * 60 + float(ss)
                    frac = min(cur / max(target_dur, 1), 1.0)
                    update(None, pct=int((i + frac) / count * 100))
                except Exception:
                    pass
        proc.wait()
        if proc.returncode == 0 and os.path.isfile(out_path):
            ok_count += 1
            outputs.append(out_path)
            if delete_used:
                for c in clips:
                    try:
                        os.remove(c)
                    except OSError:
                        pass
                if mode != 'gabung':
                    all_clips = [c for c in all_clips if os.path.isfile(c)]
        else:
            fail_count += 1

    if not ok_count:
        raise RuntimeError('Semua job mixer gagal — cek clip/ffmpeg/disk.')
    msg = f'Selesai: {ok_count} video'
    if fail_count:
        msg += f' | {fail_count} gagal'
    sidecars = _generate_caption_sidecars(job, outputs, update)
    update(msg + '.', pct=100, done=True, success=True, out_dir=out_dir,
           clips=ok_count, files=outputs, captions=sidecars)
