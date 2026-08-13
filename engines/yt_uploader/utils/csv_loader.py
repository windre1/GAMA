"""
utils/csv_loader.py
Improved CSV Importer for Batch Uploads

Robustly reads CSV files with auto-delimiter detection, encoding fallback,
and header normalization.
"""

import csv
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


def _normalize_csv_date(d_str, line_no):
    """Normalize date to YYYY-MM-DD or raise ValueError."""
    if not d_str:
        return None
    d_str = str(d_str).strip()
    if not d_str:
        return None

    fmts = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]
    for f in fmts:
        try:
            dt = datetime.strptime(d_str, f)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Invalid date format in CSV (Row {line_no}): '{d_str}'")


def _normalize_csv_time(t_str, line_no):
    """Normalize time to HH:MM or raise ValueError. Accepts ':' or '.' as the
    hour/minute separator — Indonesian users commonly write '08.00' instead
    of '08:00', so both are treated the same."""
    if not t_str:
        return None
    t_str = str(t_str).strip()
    if not t_str:
        return None

    fmts = ["%H:%M:%S", "%H:%M", "%H.%M.%S", "%H.%M"]
    for f in fmts:
        try:
            dt = datetime.strptime(t_str, f)
            return dt.strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError(f"Invalid time format in CSV (Row {line_no}): '{t_str}'")


INTERNAL_KEYS = [
    'video_path', 'title', 'description', 'tags',
    'thumbnail_path', 'visibility', 'schedule_time', 'account_id',
    'date', 'time', 'ai_content', 'made_for_kids'
]


HEADER_ALIASES = {
    'video_path': 'video_path',
    'video': 'video_path',
    'file_path': 'video_path',
    'path': 'video_path',
    'title': 'title',
    'judul': 'title',
    'name': 'title',
    'description': 'description',
    'deskripsi': 'description',
    'desc': 'description',
    'tags': 'tags',
    'tag': 'tags',
    'keywords': 'tags',
    'thumbnail_path': 'thumbnail_path',
    'thumbnail': 'thumbnail_path',
    'thumb': 'thumbnail_path',
    'image': 'thumbnail_path',
    'visibility': 'visibility',
    'privacy': 'visibility',
    'status': 'visibility',
    'schedule_time': 'schedule_time',
    'publish_at': 'schedule_time',
    'publish_time': 'schedule_time',
    'date': 'date',
    'tanggal': 'date',
    'time': 'time',
    'jam': 'time',
    'ai_content': 'ai_content',
    'ai': 'ai_content',
    'konten_ai': 'ai_content',
    'altered_content': 'ai_content',
    'synthetic_content': 'ai_content',
    'made_for_kids': 'made_for_kids',
    'kids': 'made_for_kids',
    'audience': 'made_for_kids',
    'for_kids': 'made_for_kids',
    'untuk_anak': 'made_for_kids',
    'konten_anak': 'made_for_kids',
    'account_id': 'account_id',
    'channel_id': 'account_id',
    'account': 'account_id'
}


def normalize_header(h):
    """Normalize a header string to lower snake-ish case."""
    if not h:
        return ""
    return str(h).strip().lower().replace(" ", "_")


def clean_path_value(path):
    """Clean paths copied from CSV/Excel, including wrapped quotes."""
    value = str(path or "").strip().strip('"').strip("'")
    for quote in ('"', "'"):
        if quote in value:
            parts = [p.strip() for p in value.split(quote) if p.strip()]
            drive_parts = [p for p in parts if len(p) > 2 and p[1:3] in (":\\", ":/")]
            if drive_parts:
                value = drive_parts[-1]
                break
    return value


def parse_bool_value(value):
    """Parse CSV-ish boolean values untuk ai_content / made_for_kids.

    Return:
      True  → nilai eksplisit positif (1, yes, true, x, dll)
      False → nilai eksplisit negatif (0, false, no, tidak, dll)
      None  → kosong/tidak diisi = ikuti checkbox di GAMA (default)
    """
    text = str(value or "").strip().lower()
    if not text:
        return None   # kosong → ikuti checkbox GAMA
    if text in ("1", "true", "yes", "y", "ya", "iya", "checked", "x"):
        return True
    if text in ("0", "false", "no", "n", "tidak"):
        return False   # FIX: 0 = eksplisit False (tidak dibuat untuk anak)
    return None   # nilai tidak dikenal → ikuti checkbox GAMA


def load_video_csv(file_path):
    """Read a CSV file and return a list of metadata dictionaries."""
    if not os.path.exists(file_path):
        logger.error(f"CSV file not found: {file_path}")
        return []

    delimiters = [',', ';']
    encodings = ['utf-8-sig', 'cp1252', 'latin-1']

    for sep in delimiters:
        for encoding in encodings:
            try:
                videos = []

                with open(file_path, mode='r', encoding=encoding, newline='') as f:
                    first_line = f.readline()
                    if sep not in first_line and sep != delimiters[-1]:
                        break

                    f.seek(0)
                    reader = csv.DictReader(f, delimiter=sep)
                    if not reader.fieldnames:
                        break

                    logger.info(f"Detected delimiter: {sep}")
                    logger.info(f"Detected encoding: {encoding}")
                    logger.info(f"Raw headers: {list(reader.fieldnames)}")

                    header_map = {}
                    for csv_h in reader.fieldnames:
                        norm_h = normalize_header(csv_h)
                        internal_k = HEADER_ALIASES.get(norm_h)
                        if internal_k:
                            header_map[csv_h] = internal_k
                        elif norm_h in INTERNAL_KEYS:
                            header_map[csv_h] = norm_h

                    logger.info(f"Mapped headers: {header_map}")

                    if not header_map:
                        logger.warning(
                            f"No matching headers found with delimiter '{sep}'. "
                            f"Detected: {reader.fieldnames}")
                        if sep == delimiters[-1]:
                            logger.error(f"Failed to detect headers for CSV: {file_path}")
                        continue

                    for line_no, row in enumerate(reader, start=2):
                        logger.info(f"Row {line_no} data: {row}")

                        data = {k: "" for k in INTERNAL_KEYS}
                        for csv_h, val in row.items():
                            internal_k = header_map.get(csv_h)
                            if internal_k:
                                data[internal_k] = (val.strip() if val else '')

                        data['video_path'] = clean_path_value(data.get('video_path', ''))
                        data['thumbnail_path'] = clean_path_value(data.get('thumbnail_path', ''))
                        data['ai_content'] = parse_bool_value(data.get('ai_content', ''))
                        data['made_for_kids'] = parse_bool_value(data.get('made_for_kids', ''))

                        if not data.get('video_path', '').strip():
                            logger.warning(f"Skipping Row {line_no} - video_path is empty.")
                            continue

                        if data['video_path'] and not os.path.isabs(data['video_path']):
                            csv_dir = os.path.dirname(os.path.abspath(file_path))
                            data['video_path'] = os.path.abspath(os.path.join(csv_dir, data['video_path']))

                        raw_date = data.get('date', '')
                        raw_time = data.get('time', '')

                        try:
                            data['date'] = _normalize_csv_date(raw_date, line_no)
                            data['time'] = _normalize_csv_time(raw_time, line_no)
                            if data['date']:
                                logger.info(f"CSV Date parsed: {data['date']} (Row {line_no})")
                            if data['time']:
                                logger.info(f"CSV Time parsed: {data['time']} (Row {line_no})")
                        except ValueError as ve:
                            logger.error(ve)
                            raise

                        if data['date'] and data['time']:
                            try:
                                data['schedule_time'] = datetime.strptime(
                                    f"{data['date']} {data['time']}", "%Y-%m-%d %H:%M")
                            except ValueError:
                                data['schedule_time'] = None
                        else:
                            data['schedule_time'] = None

                        videos.append(data)

                if videos:
                    logger.info(
                        f"Successfully loaded {len(videos)} videos using delimiter "
                        f"'{sep}' and encoding '{encoding}'.")
                    return videos

            except UnicodeDecodeError as e:
                logger.debug(
                    f"Failed decoding CSV with encoding '{encoding}' and delimiter "
                    f"'{sep}': {e}")
                continue
            except ValueError:
                raise
            except Exception as e:
                logger.debug(
                    f"Failed loading CSV with delimiter '{sep}' and encoding "
                    f"'{encoding}': {e}")
                continue

    return []
