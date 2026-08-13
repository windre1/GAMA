"""
chatgrow.py â€” Util bersama fitur Scrape & Blast (WhatsApp + Telegram) GAM.

Semua data hasil scrape disimpan di folder khusus:  data/chatgrow/
  â”œâ”€ scrape/     â†’ CSV hasil scrape member grup/channel
  â”œâ”€ tg/         â†’ file session Telethon (login akun Telegram)
  â””â”€ wa/         â†’ dipakai wa_manager (session & pesan WhatsApp)
"""
import os
import re
import csv
import sys
import time
import json

# Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini.
BASE_DIR    = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
              else os.path.dirname(os.path.abspath(__file__))
CG_DIR      = os.path.join(BASE_DIR, 'data', 'chatgrow')
SCRAPE_DIR  = os.path.join(CG_DIR, 'scrape')
TG_DIR      = os.path.join(CG_DIR, 'tg')
WA_DIR      = os.path.join(CG_DIR, 'wa')
BLAST_DIR   = os.path.join(CG_DIR, 'blast')
TEMPLATE_DB = os.path.join(BLAST_DIR, 'blast_templates.json')
GROUP_DB    = os.path.join(BLAST_DIR, 'group_lists.json')


def ensure_dirs():
    for d in (CG_DIR, SCRAPE_DIR, TG_DIR, WA_DIR, BLAST_DIR):
        os.makedirs(d, exist_ok=True)


def safe_name(raw: str) -> str:
    """Nama file aman dari label grup/channel."""
    s = re.sub(r'[\\/*?:"<>|]+', '_', str(raw or '')).strip()
    s = re.sub(r'\s+', '_', s)
    return (s[:60] or 'scrape').strip('_')


# Kolom CSV standar member hasil scrape (WA & Telegram diseragamkan)
MEMBER_FIELDS = ['platform', 'source', 'name', 'phone', 'username', 'user_id', 'is_admin']

BLAST_TEMPLATE_FIELDS = ['target', 'name', 'phone', 'username', 'user_id', 'message']


def _read_json(path: str, default):
    ensure_dirs()
    if not os.path.isfile(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data):
    ensure_dirs()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def csv_template_text(platform: str) -> str:
    platform = (platform or 'whatsapp').strip().lower()
    base = ','.join(BLAST_TEMPLATE_FIELDS)
    if platform == 'telegram':
        sample = '@username_target,Nama Target,,username_target,123456789,Halo {name}, ini pesan promo untuk {username}.'
    else:
        sample = '628123456789,Nama Target,628123456789,,628123456789,Halo {name}, ini pesan promo untuk Anda.'
    return base + '\n' + sample + '\n'


def _norm_row(row: dict) -> dict:
    out = {}
    for k, v in (row or {}).items():
        key = str(k or '').strip().lower()
        out[key] = str(v or '').strip()
    return out


def _pick_value(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(str(key).strip().lower(), '')
        if value:
            return value
    return ''


def _dedupe_targets(rows: list[dict]) -> list[dict]:
    seen = set()
    uniq = []
    for row in rows:
        target = str(row.get('target') or '').strip()
        if not target or target in seen:
            continue
        seen.add(target)
        uniq.append(row)
    return uniq



def write_members_csv(platform: str, source_label: str, members: list, save_name: str = '') -> str:
    """Tulis daftar member ke CSV di folder scrape. Return path file."""
    ensure_dirs()
    file_label = safe_name(save_name or source_label)
    fname = f'{platform}_{file_label}_{time.strftime("%Y%m%d_%H%M%S")}.csv'
    path = os.path.join(SCRAPE_DIR, fname)
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=MEMBER_FIELDS)
        w.writeheader()
        for m in members:
            row = {k: m.get(k, '') for k in MEMBER_FIELDS}
            row['platform'] = platform
            row['source'] = source_label
            w.writerow(row)
    return path


def list_scrape_files() -> list:
    """Daftar file CSV hasil scrape (terbaru dulu)."""
    ensure_dirs()
    out = []
    for fn in os.listdir(SCRAPE_DIR):
        if not fn.lower().endswith('.csv'):
            continue
        p = os.path.join(SCRAPE_DIR, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        # Hitung baris (tanpa header) tanpa memuat semua ke memori
        try:
            with open(p, encoding='utf-8-sig') as f:
                rows = max(0, sum(1 for _ in f) - 1)
        except OSError:
            rows = 0
        platform = ''
        source = ''
        columns = []
        try:
            with open(p, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                columns = list(reader.fieldnames or [])
                first = next(reader, None)
            if first:
                first = _norm_row(first)
                platform = first.get('platform', '')
                source = first.get('source', '')
        except Exception:
            pass
        out.append({'file': fn, 'path': p, 'rows': rows,
                    'size': st.st_size, 'mtime': int(st.st_mtime),
                    'platform': platform, 'source': source, 'columns': columns})
    out.sort(key=lambda x: x['mtime'], reverse=True)
    return out


def delete_scrape_file(file_name: str) -> bool:
    ensure_dirs()
    file_name = os.path.basename(str(file_name or '').strip())
    if not file_name or not file_name.lower().endswith('.csv'):
        return False
    path = os.path.join(SCRAPE_DIR, file_name)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def read_targets_csv(path: str, platform: str) -> list:
    """Baca file CSV scrape â†’ daftar target untuk blast.
    WA butuh 'phone'; Telegram butuh 'username' atau 'user_id'."""
    targets = []
    if not os.path.isfile(path):
        return targets
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if platform == 'whatsapp':
                v = (row.get('phone') or '').strip()
                if v:
                    targets.append(v)
            else:
                v = (row.get('username') or '').strip() or (row.get('user_id') or '').strip()
                if v:
                    targets.append(v)
    # Buang duplikat, jaga urutan
    seen, uniq = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def read_blast_rows(path: str, platform: str) -> list[dict]:
    platform = (platform or '').strip().lower()
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, newline='', encoding='utf-8-sig') as f:
        for raw in csv.DictReader(f):
            row = _norm_row(raw)
            if platform == 'telegram':
                target = (_pick_value(row, 'target', 'username', 'user_id', 'id') or '').strip()
                if target and row.get('username') and not target.startswith('@') and not target.lstrip('-').isdigit():
                    target = '@' + target
            else:
                target = (_pick_value(row, 'target', 'phone', 'user_id') or '').strip()
                if not (target.endswith('@g.us') or target.endswith('@s.whatsapp.net')):
                    target = ''.join(ch for ch in target if ch.isdigit())
            if not target:
                continue
            target_kind = 'group' if (platform == 'whatsapp' and target.endswith('@g.us')) else 'personal'
            out.append({
                'target': target,
                'name': _pick_value(row, 'name'),
                'phone': _pick_value(row, 'phone'),
                'username': _pick_value(row, 'username'),
                'user_id': _pick_value(row, 'user_id', 'id'),
                'message': _pick_value(row, 'message'),
                'source': _pick_value(row, 'source'),
                'platform': _pick_value(row, 'platform') or platform,
                'target_kind': target_kind,
            })
    return _dedupe_targets(out)


def render_blast_text(template: str, row: dict) -> str:
    text = str(template or '')
    values = {str(k).strip().lower(): str(v or '') for k, v in (row or {}).items()}

    def _replace(match):
        return values.get(match.group(1).strip().lower(), '')

    return re.sub(r'\{([^{}]+)\}', _replace, text)


def list_blast_templates(platform: str = '') -> list[dict]:
    platform = (platform or '').strip().lower()
    items = _read_json(TEMPLATE_DB, [])
    out = []
    for item in items:
        item_platform = str(item.get('platform') or '').strip().lower()
        if platform and item_platform and item_platform != platform:
            continue
        out.append(item)
    out.sort(key=lambda x: (x.get('platform') or '', x.get('name') or ''))
    return out


def save_blast_template(name: str, platform: str, message: str, media_path: str = '') -> dict:
    name = safe_name(name).replace('_', ' ')
    if not name:
        raise ValueError('Nama template blast wajib diisi.')
    items = _read_json(TEMPLATE_DB, [])
    record = {
        'name': name,
        'platform': (platform or '').strip().lower(),
        'message': str(message or ''),
        'media_path': str(media_path or '').strip(),
        'updated_at': int(time.time()),
    }
    items = [x for x in items if not (
        str(x.get('name') or '').lower() == name.lower()
        and str(x.get('platform') or '').lower() == record['platform']
    )]
    items.append(record)
    _write_json(TEMPLATE_DB, items)
    return record


def delete_blast_template(name: str, platform: str = '') -> bool:
    items = _read_json(TEMPLATE_DB, [])
    name_l = str(name or '').strip().lower()
    platform_l = str(platform or '').strip().lower()
    new_items = [x for x in items if not (
        str(x.get('name') or '').lower() == name_l
        and (not platform_l or str(x.get('platform') or '').lower() == platform_l)
    )]
    changed = len(new_items) != len(items)
    if changed:
        _write_json(TEMPLATE_DB, new_items)
    return changed


def list_group_lists(platform: str = '') -> list[dict]:
    platform = (platform or '').strip().lower()
    items = _read_json(GROUP_DB, [])
    out = []
    for item in items:
        item_platform = str(item.get('platform') or '').strip().lower()
        if platform and item_platform and item_platform != platform:
            continue
        out.append(item)
    out.sort(key=lambda x: (x.get('platform') or '', x.get('name') or ''))
    return out


def get_group_list(name: str, platform: str = '') -> dict | None:
    name_l = str(name or '').strip().lower()
    platform_l = str(platform or '').strip().lower()
    for item in _read_json(GROUP_DB, []):
        if str(item.get('name') or '').strip().lower() != name_l:
            continue
        if platform_l and str(item.get('platform') or '').strip().lower() != platform_l:
            continue
        return item
    return None


def save_group_list(name: str, platform: str, session: str, groups: list[dict]) -> dict:
    name = safe_name(name).replace('_', ' ')
    if not name:
        raise ValueError('Nama list grup wajib diisi.')
    clean_groups = []
    for group in groups or []:
        target = str(group.get('target') or '').strip()
        if not target:
            continue
        clean_groups.append({
            'target': target,
            'label': str(group.get('label') or target).strip(),
        })
    clean_groups = _dedupe_targets(clean_groups)
    if not clean_groups:
        raise ValueError('Belum ada grup yang dipilih.')
    items = _read_json(GROUP_DB, [])
    record = {
        'name': name,
        'platform': (platform or '').strip().lower(),
        'session': str(session or '').strip(),
        'groups': clean_groups,
        'updated_at': int(time.time()),
    }
    items = [x for x in items if not (
        str(x.get('name') or '').lower() == name.lower()
        and str(x.get('platform') or '').lower() == record['platform']
    )]
    items.append(record)
    _write_json(GROUP_DB, items)
    return record


def delete_group_list(name: str, platform: str = '') -> bool:
    items = _read_json(GROUP_DB, [])
    name_l = str(name or '').strip().lower()
    platform_l = str(platform or '').strip().lower()
    new_items = [x for x in items if not (
        str(x.get('name') or '').lower() == name_l
        and (not platform_l or str(x.get('platform') or '').lower() == platform_l)
    )]
    changed = len(new_items) != len(items)
    if changed:
        _write_json(GROUP_DB, new_items)
    return changed

