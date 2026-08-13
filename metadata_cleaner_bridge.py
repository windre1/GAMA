"""metadata_cleaner_bridge.py — Worker pembersih/pemalsu metadata untuk GAMA.

Dijalankan sebagai subprocess:
    python metadata_cleaner_bridge.py <job_json_path>

Diadopsi dari D:\\TOOLS\\METADATA CLEANER (Tkinter GUI standalone) — logika inti
(ffmpeg untuk video, Pillow untuk gambar) dipindah ke sini, memakai ffmpeg
bundel GAMA (assets/bin/ffmpeg.exe, lihat repurpose.py) alih-alih PyInstaller
resource path. Fitur lisensi/trial pada tool asal SENGAJA tidak diikutkan.

Perbaikan dibanding tool asal:
  - "Clean" untuk gambar sekarang benar-benar menghapus EXIF asli (tool asal
    cuma menimpa beberapa tag di atas EXIF lama — GPS/lensa/dll masih ikut).
  - Randomisasi creation_time sekarang mengikuti pilihan user (opsi
    'randomize_date'), bukan selalu jalan otomatis seperti tool asal.
"""
import json
import logging
import os
import random
import string
import struct
import subprocess
import sys
import time
from datetime import datetime, timedelta

from repurpose import FFMPEG

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

_NOWIN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.avi')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
MEDIA_EXTS = VIDEO_EXTS + IMAGE_EXTS

RATIO_MAP = {
    "9:16 (TikTok)": 9 / 16,
    "1:1 (Square)": 1 / 1,
    "4:5 (Portrait)": 4 / 5,
    "16:9 (Wide)": 16 / 9,
}

# ─── Preset perangkat (diadopsi verbatim dari presets.py tool asal) ──────────

DEVICE_PRESETS = {
    "Apple": {
        "iPhone 15 Pro Max": {"make": "Apple", "model": "iPhone 15 Pro Max", "software": "iOS 17.5"},
        "iPhone 14 Pro": {"make": "Apple", "model": "iPhone 14 Pro", "software": "iOS 16.6"},
        "iPhone 13 mini": {"make": "Apple", "model": "iPhone 13 mini", "software": "iOS 15.4"},
        "iPhone 12": {"make": "Apple", "model": "iPhone 12", "software": "iOS 14.1"},
    },
    "Samsung": {
        "Galaxy S24 Ultra": {"make": "Samsung", "model": "SM-S928B", "software": "One UI 6.1 (Android 14)"},
        "Galaxy S23 FE": {"make": "Samsung", "model": "SM-S711B", "software": "Android 13"},
        "Galaxy Z Fold 5": {"make": "Samsung", "model": "SM-F946B", "software": "Android 13"},
        "Galaxy A55 5G": {"make": "Samsung", "model": "SM-A556B", "software": "Android 14"},
    },
    "Xiaomi": {
        "Xiaomi 14 Ultra": {"make": "Xiaomi", "model": "24030PN60G", "software": "HyperOS 1.0"},
        "Redmi Note 13 Pro": {"make": "Xiaomi", "model": "23090RA98G", "software": "MIUI 14"},
        "POCO F5": {"make": "Xiaomi", "model": "23049PCD8G", "software": "Android 13"},
    },
    "Oppo": {
        "Find X7 Ultra": {"make": "Oppo", "model": "PHY110", "software": "ColorOS 14"},
        "Reno 11 Pro": {"make": "Oppo", "model": "CPH2593", "software": "Android 14"},
        "Oppo A78": {"make": "Oppo", "model": "CPH2483", "software": "Android 13"},
    },
    "Vivo": {
        "Vivo X100 Pro": {"make": "Vivo", "model": "V2309", "software": "Funtouch OS 14"},
        "V30 Pro": {"make": "Vivo", "model": "V2319", "software": "Android 14"},
        "iQOO 12": {"make": "Vivo", "model": "I2220", "software": "Android 14"},
    },
    "Google": {
        "Pixel 8 Pro": {"make": "Google", "model": "Pixel 8 Pro", "software": "Android 14"},
        "Pixel 7a": {"make": "Google", "model": "Pixel 7a", "software": "Android 13"},
        "Pixel 6": {"make": "Google", "model": "Pixel 6", "software": "Android 12"},
    },
    "Sony": {
        "Xperia 1 V": {"make": "Sony", "model": "XQ-DQ72", "software": "Android 13"},
        "Xperia 5 IV": {"make": "Sony", "model": "XQ-CQ72", "software": "Android 12"},
    },
    "Realme": {
        "Realme GT5 Pro": {"make": "Realme", "model": "RMX3888", "software": "Realme UI 5.0"},
        "Realme 12 Pro+": {"make": "Realme", "model": "RMX3840", "software": "Android 14"},
    },
    "Infinix": {
        "Infinix GT 20 Pro": {"make": "Infinix", "model": "X6720", "software": "XOS 14"},
        "Note 40 Pro": {"make": "Infinix", "model": "X6850", "software": "Android 14"},
    },
}


def get_brands():
    return sorted(DEVICE_PRESETS.keys())


def get_models(brand):
    return sorted(DEVICE_PRESETS.get(brand, {}).keys())


def get_preset(brand, model):
    return DEVICE_PRESETS.get(brand, {}).get(model)


def get_random_preset():
    brand = random.choice(get_brands())
    model = random.choice(get_models(brand))
    return brand, model, get_preset(brand, model)


def generate_random_string(length=8):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def is_media_file(path):
    return os.path.splitext(path)[1].lower() in MEDIA_EXTS


def scan_folder(folder):
    """Scan satu folder (non-rekursif) untuk file media yang didukung."""
    out = []
    try:
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if os.path.isfile(full) and is_media_file(full):
                out.append(full)
    except OSError:
        pass
    return out


def _resolve_preset(options):
    """Preset perangkat untuk SATU file — 'random_device' pilih preset baru
    tiap kali dipanggil (dipakai per-file), else pakai preset tetap dari opsi."""
    if options.get('random_device'):
        _, _, preset = get_random_preset()
        return preset
    device = options.get('device') or {}
    brand, model = device.get('brand'), device.get('model')
    if brand and model:
        return get_preset(brand, model)
    return None


def _random_creation_time_video():
    d = datetime.now() - timedelta(days=random.randint(0, 10), hours=random.randint(0, 23))
    return d.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _random_creation_time_image():
    d = datetime.now() - timedelta(days=random.randint(0, 10), hours=random.randint(0, 23))
    return d.strftime("%Y:%m:%d %H:%M:%S")


def process_video(input_path, output_path, options):
    cmd = [FFMPEG, '-y', '-i', input_path]
    filters = []
    ar_opt = options.get('aspect_ratio', 'Original')

    if ar_opt != 'Original' and ar_opt in RATIO_MAP:
        R = RATIO_MAP[ar_opt]
        filters.append(f"crop=w='min(iw,ih*{R})':h='min(ih,iw/{R})'")

    if options.get('upscale'):
        filters.append('scale=iw*2:-2:flags=lanczos')
    elif options.get('tiktok_hd') or ar_opt != 'Original':
        filters.append('scale=trunc(iw/2)*2:trunc(ih/2)*2')

    if options.get('tiktok_hd'):
        seed = random.randint(1, 1000000)
        filters.append(f'noise=all_strength=1:all_flags=t:all_seed={seed}')

    if filters or options.get('tiktok_hd') or options.get('upscale'):
        cmd.extend([
            '-c:v', 'libx264', '-preset', 'medium',
            '-crf', '18' if (options.get('tiktok_hd') or options.get('upscale')) else '23',
            '-vf', ','.join(filters),
            '-c:a', 'aac', '-b:a', '192k', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart+use_metadata_tags',
        ])
    else:
        cmd.extend(['-c', 'copy', '-movflags', '+use_metadata_tags'])

    if options.get('clean'):
        cmd.extend(['-map_metadata', '-1'])
        cmd.extend(['-fflags', '+bitexact', '-flags:v', '+bitexact', '-flags:a', '+bitexact'])

    metadata = {}
    preset = _resolve_preset(options)
    if preset:
        metadata.update(preset)
    if options.get('randomize'):
        metadata['title'] = f"VID_{generate_random_string(10)}"
        metadata['comment'] = f"Processed_{generate_random_string(5)}"
    if options.get('randomize_date', True):
        metadata['creation_time'] = _random_creation_time_video()

    for key, value in metadata.items():
        cmd.extend(['-metadata', f'{key}={value}'])
        if key in ('make', 'model', 'software'):
            cmd.extend(['-metadata', f'com.apple.quicktime.{key}={value}'])
        cmd.extend(['-metadata:s:v:0', f'{key}={value}'])

    cmd.append(output_path)

    startupinfo = None
    creationflags = 0
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = 0x00004000 | 0x08000000  # BELOW_NORMAL_PRIORITY | CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, startupinfo=startupinfo, creationflags=creationflags)
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            logging.error('ffmpeg error on %s: %s', os.path.basename(input_path), stderr)
            return False, (stderr or '')[-400:]
        return True, None
    except Exception as e:
        return False, str(e)


def process_image(input_path, output_path, options):
    try:
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo
    except ImportError:
        return False, 'Pillow tidak ter-install (pip install Pillow).'

    try:
        img = Image.open(input_path)

        ar_opt = options.get('aspect_ratio', 'Original')
        if ar_opt != 'Original' and ar_opt in RATIO_MAP:
            R = RATIO_MAP[ar_opt]
            iw, ih = img.size
            if iw / ih > R:
                new_w = int(ih * R)
                left = (iw - new_w) // 2
                img = img.crop((left, 0, left + new_w, ih))
            else:
                new_h = int(iw / R)
                top = (ih - new_h) // 2
                img = img.crop((0, top, iw, top + new_h))

        if options.get('upscale'):
            img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)

        metadata = {}
        preset = _resolve_preset(options)
        if preset:
            metadata.update(preset)
        if options.get('randomize'):
            metadata['title'] = f"IMG_{generate_random_string(10)}"
            metadata['comment'] = f"Processed_{generate_random_string(5)}"
        creation_time = _random_creation_time_image() if options.get('randomize_date', True) else None

        make_val = metadata.get('make', '')
        model_val = metadata.get('model', '')
        sw_val = metadata.get('software', '')

        # 'clean' -> mulai dari EXIF KOSONG (bukan img.getexif(), yang membawa
        # EXIF asli termasuk GPS/lensa dsb — ini bug di tool asal).
        exif = Image.Exif() if options.get('clean') else img.getexif()
        if make_val: exif[0x010f] = make_val
        if model_val: exif[0x0110] = model_val
        if make_val or model_val or sw_val:
            exif[0x0131] = f"{make_val} {model_val} ({sw_val})".strip()
        if creation_time:
            exif[0x0132] = creation_time

        ext = os.path.splitext(output_path)[1].lower()
        if ext in ('.jpg', '.jpeg'):
            img.convert('RGB').save(output_path, 'JPEG', quality=95, exif=exif)
        elif ext == '.png':
            png_info = PngInfo()
            if make_val or model_val:
                png_info.add_text('Software', f'{make_val} {model_val}'.strip())
                png_info.add_text('Comment', f'Device:{make_val}|Model:{model_val}')
            img.save(output_path, 'PNG', exif=exif, pnginfo=png_info)
        elif ext == '.webp':
            img.save(output_path, 'WEBP', exif=exif, quality=95)
        else:
            img.save(output_path)

        img.close()
        return True, None
    except Exception as e:
        logging.error('Error processing image %s: %s', input_path, str(e))
        return False, str(e)


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


def run_clean(job):
    """Job handler batch clean/spoof metadata — video (ffmpeg) & gambar (Pillow)."""
    status_path = job['status_path']
    files = job.get('files') or []
    output_dir = job.get('output_dir') or ''
    options = job.get('options') or {}

    state = {'pct': 0, 'message': 'Menyiapkan...', 'done': False, 'success': False,
              'error': None, 'rows': []}
    write_status(status_path, **state)

    if not files:
        state.update(done=True, error='Tidak ada file untuk diproses.')
        write_status(status_path, **state)
        return 1
    if not output_dir:
        state.update(done=True, error='Folder output belum ditentukan.')
        write_status(status_path, **state)
        return 1

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        state.update(done=True, error=f'Gagal membuat folder output: {e}')
        write_status(status_path, **state)
        return 1

    total = len(files)
    state['rows'] = [{'title': os.path.basename(f), 'status': 'antre'} for f in files]
    write_status(status_path, **state)

    success = 0
    for i, src in enumerate(files):
        state['rows'][i]['status'] = 'proses'
        state['pct'] = int(i / total * 100)
        state['message'] = f'[{i + 1}/{total}] {os.path.basename(src)}...'
        write_status(status_path, **state)

        if not os.path.isfile(src):
            state['rows'][i]['status'] = 'gagal: file tidak ditemukan'
            write_status(status_path, **state)
            continue

        ext = os.path.splitext(src)[1].lower()
        basename = os.path.basename(src)
        if options.get('rename', True):
            stamp = time.strftime('%Y%m%d_%H%M%S')
            out_name = f'PROCESSED_{stamp}_{generate_random_string(4)}{ext}'
        else:
            out_name = basename
        out_path = os.path.join(output_dir, out_name)

        if ext in VIDEO_EXTS:
            ok, err = process_video(src, out_path, options)
        elif ext in IMAGE_EXTS:
            ok, err = process_image(src, out_path, options)
        else:
            ok, err = False, 'Format tidak didukung'

        state['rows'][i]['status'] = 'sukses' if ok else f'gagal: {str(err or "")[:80]}'
        if ok:
            success += 1
        write_status(status_path, **state)

    state.update(pct=100, done=True, success=(success > 0),
                 message=f'Selesai: {success}/{total} file berhasil diproses.')
    if success == 0:
        state['error'] = 'Semua file gagal diproses.'
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_selftest():
    result = {'ffmpeg_ok': os.path.isfile(FFMPEG) or FFMPEG == 'ffmpeg', 'ffmpeg_path': FFMPEG}
    try:
        import PIL  # noqa
        result['pillow_ok'] = True
    except ImportError:
        result['pillow_ok'] = False
    print(json.dumps(result))
    return 0 if result['pillow_ok'] else 1


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('job', nargs='?')
    parser.add_argument('--selftest', action='store_true')
    args = parser.parse_args()
    if args.selftest:
        return run_selftest()
    if not args.job or not os.path.isfile(args.job):
        print('Job file tidak ditemukan', file=sys.stderr)
        return 2
    with open(args.job, 'r', encoding='utf-8') as f:
        job = json.load(f)
    return run_clean(job)


if __name__ == '__main__':
    sys.exit(main())
