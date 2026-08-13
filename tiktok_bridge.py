"""
tiktok_bridge.py - Jembatan GAM -> TEKOTOK untuk upload TikTok.

Dijalankan sebagai worker subprocess (cwd = folder TEKOTOK):
    python tiktok_bridge.py <job_json_path>
    python tiktok_bridge.py --selftest
"""
import os
import sys
import json
import argparse

# TEKOTOK ngeprint macam-macam teks hasil scrape TikTok (nama lokasi, caption,
# dst) yang bisa berisi karakter non-ASCII - tanpa ini, console Windows
# (cp1252) crash dgn UnicodeEncodeError dan bikin seluruh langkah (mis. isi
# lokasi) gagal cuma gara-gara print(), bukan karena aksinya sendiri gagal.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def _cari_tekotok() -> str:
    """Tentukan folder engine TEKOTOK yang dipakai.

    Urutannya SENGAJA menaruh engine bundel di posisi pertama, bukan env var.
    Sebelumnya nilainya hanya dibaca dari os.environ['TEKOTOK_DIR'] yang di-set
    main.py sebelum menjalankan worker. Ketergantungan diam-diam itu bikin
    bridge memakai instalasi lama D:\\TOOLS\\TEKOTOK begitu ia dijalankan tanpa
    lewat main.py — dan uploader di sana TIDAK punya fitur baru, sehingga
    muncul galat membingungkan seperti
    "upload_video() got an unexpected keyword argument 'music'".
    """
    dasar = os.path.dirname(os.path.abspath(__file__))
    bundel = os.path.join(dasar, 'engines', 'tekotok')
    if os.path.isfile(os.path.join(bundel, 'app', 'core', 'uploader.py')):
        return bundel
    dari_env = os.environ.get('TEKOTOK_DIR')
    if dari_env and os.path.isdir(dari_env):
        return dari_env
    return dari_env or r'D:\TOOLS\TEKOTOK'


TEKOTOK_DIR = _cari_tekotok()


def _prepare_tekotok():
    if TEKOTOK_DIR not in sys.path:
        sys.path.insert(0, TEKOTOK_DIR)
    try:
        os.chdir(TEKOTOK_DIR)
    except OSError:
        pass


def _register_gam_account(profile_path):
    from app.db.database import get_db, init_db
    init_db()
    username = 'gam__' + os.path.basename(profile_path.rstrip('\\/'))
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM accounts WHERE username=?", (username,))
    row = c.fetchone()
    if row:
        acc_id = row['id']
        c.execute("UPDATE accounts SET current_session=? WHERE id=?", (profile_path, acc_id))
    else:
        c.execute(
            "INSERT INTO accounts (username, current_session, account_type) VALUES (?, ?, 'regular')",
            (username, profile_path),
        )
        acc_id = c.lastrowid
    conn.commit()
    conn.close()
    return acc_id, username


def _delete_gam_account(acc_id):
    try:
        from app.db.database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


# Sama seperti worker TEKOTOK (app/core/worker.py)
MAX_ATTEMPTS = 3


def _patch_uploader_port(uploader_module):
    """TEKOTOK menghitung port debug Chrome dari hash(username). hash() string
    Python diacak per proses (PYTHONHASHSEED), aman di GUI TEKOTOK yang satu
    proses, tapi GAMA menjalankan satu subprocess per akun sehingga port bisa
    tabrakan antar akun yang upload bersamaan. Override hash di namespace modul
    dengan CRC32 yang deterministik: akun sama selalu dapat port sama, akun
    beda dapat port beda."""
    import zlib
    uploader_module.hash = lambda s: zlib.crc32(str(s).encode('utf-8'))


def _delete_video_file(path):
    """Hapus file video setelah sukses posting (fitur delete_after, seperti
    worker TEKOTOK)."""
    try:
        p = os.path.normpath(os.path.abspath(path or ''))
        if os.path.isfile(p):
            os.remove(p)
            print(f'[DELETE] File dihapus setelah posting: {p}')
            return True
    except Exception as e:
        print(f'[DELETE] Gagal hapus file: {e}')
    return False


def _combine_schedule(item):
    """Gabungkan schedule_date + schedule_time jadi 'YYYY-MM-DD HH:MM',
    persis seperti upload_view TEKOTOK sebelum add_job. _do_schedule TikTok
    Studio hanya mengerti format gabungan ini."""
    d = (item.get('schedule_date') or item.get('date') or '').strip()
    t = (item.get('schedule_time') or item.get('time') or '').strip()
    if d and t:
        return f'{d} {t}'
    return None


def _single_upload(upload_video, acc_id, item, invisible, progress_callback,
                   default_type='regular'):
    return upload_video(
        video_path=item.get('video_path') or '',
        caption=item.get('caption') or '',
        account_id=acc_id,
        upload_type=(item.get('upload_type') or default_type or 'regular'),
        is_invisible=1 if invisible else 0,
        hashtags=item.get('hashtags') or None,
        product_name=item.get('product_name') or None,
        product_id=item.get('product_id') or None,
        schedule_time=_combine_schedule(item),
        location=(item.get('location') or '').strip() or None,
        music=(item.get('music') or '').strip() or None,
        ai_generate_content=bool(item.get('ai_generate_content', False)),
        progress_callback=progress_callback,
    )



def _single_upload_foto(upload_photos, acc_id, item, invisible, progress_callback):
    """Unggah satu postingan multi-foto lewat web.

    Kolom `photos` diisi path FOLDER (semua gambar di dalamnya, urut nama)
    atau beberapa berkas dipisah '|'.
    """
    from app.core.uploader import foto_dari_kolom
    foto = foto_dari_kolom(item.get('photos') or '')
    if not foto:
        return False
    return upload_photos(
        foto,
        caption=item.get('caption') or '',
        account_id=acc_id,
        hashtags=item.get('hashtags') or None,
        music=(item.get('music') or '').strip() or None,
        location=(item.get('location') or '').strip() or None,
        schedule_time=_combine_schedule(item),
        is_invisible=1 if invisible else 0,
        simpan_draf=bool(item.get('simpan_draf', False)),
        progress_callback=progress_callback,
    )

def _upload_with_retry(upload_video, acc_id, item, invisible, progress_callback,
                       default_type='regular', upload_photos=None):
    """Coba upload sampai MAX_ATTEMPTS kali dengan jeda, seperti worker TEKOTOK.

    Kalau `upload_photos` diisi, barisnya diperlakukan sebagai postingan FOTO
    (kolom `photos`), bukan video.
    """
    import time
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if upload_photos is not None:
                ok = _single_upload_foto(upload_photos, acc_id, item, invisible,
                                         progress_callback)
            else:
                ok = _single_upload(upload_video, acc_id, item, invisible,
                                    progress_callback, default_type)
            if ok:
                return True, None
            last_err = 'Upload gagal.'
        except Exception as e:
            import traceback
            last_err = str(e) + '\n' + traceback.format_exc()
        if attempt < MAX_ATTEMPTS:
            print(f'[BRIDGE] Percobaan {attempt} gagal ({last_err}), coba lagi...')
            time.sleep(2)
    return False, last_err


def run_upload(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    state = {
        'pct': 0,
        'message': 'Menyiapkan TikTok...',
        'done': False,
        'success': False,
        'error': None,
    }

    def flush():
        write_status(status_path, **state)

    flush()

    if not os.path.isdir(profile_path):
        state.update(done=True, error='Folder profil tidak ditemukan.')
        flush()
        return 1

    try:
        _prepare_tekotok()
    except Exception as e:
        state.update(done=True, error=f'Gagal menyiapkan TEKOTOK: {e}')
        flush()
        return 1

    # Registrasi akun ke DB TEKOTOK dengan retry - beberapa worker paralel
    # (mode multi akun) bisa kena lock sqlite sesaat.
    import time as _time
    acc_id = None
    reg_err = None
    for _ in range(5):
        try:
            acc_id, username = _register_gam_account(profile_path)
            break
        except Exception as e:
            reg_err = e
            _time.sleep(1)
    if acc_id is None:
        state.update(done=True, error=f'Gagal daftar akun ke TEKOTOK: {reg_err}')
        flush()
        return 1

    if job.get('dry_run'):
        state.update(pct=100, done=True, success=True,
                     message=f'Dry run OK - engine TikTok siap (acc={username}), tidak ada upload.')
        flush()
        _delete_gam_account(acc_id)
        return 0

    try:
        from app.core import uploader
        _patch_uploader_port(uploader)
        upload_video = uploader.upload_video
        mode = (job.get('mode') or 'single').lower()
        delete_after = bool(job.get('delete_after'))
        # Postingan foto memakai halaman TikTok Studio yang BERBEDA
        # (?tab=photo) dengan input file dan panel musik sendiri.
        is_foto = (job.get('content') or '').lower() == 'foto'
        upload_photos = uploader.upload_photos if is_foto else None

        if mode == 'batch':
            rows = job.get('rows') or []
            if not rows:
                state.update(done=True, success=False, error='CSV batch TikTok kosong.')
                flush()
                return 1

            row_states = []
            failures = []
            skipped_msgs = list(job.get('skipped_rows') or [])
            total = len(rows)
            invisible = bool(job.get('invisible'))
            default_type = (job.get('upload_type') or 'regular').lower()
            state['rows'] = row_states

            for idx, item in enumerate(rows, 1):
                bawaan = (item.get('photos') if is_foto else item.get('video_path')) or ''
                title = item.get('caption') or os.path.basename(bawaan) or f'baris_{idx}'
                row_state = {'title': title[:80], 'status': 'processing'}
                row_states.append(row_state)
                state['message'] = f'Upload TikTok batch {idx}/{total}...'
                flush()

                if is_foto:
                    from app.core.uploader import foto_dari_kolom
                    if not foto_dari_kolom(item.get('photos') or ''):
                        row_state['status'] = 'failed'
                        failures.append(f'Baris {idx}: tidak ada file foto yang bisa dibaca.')
                        state['pct'] = int((idx / total) * 100)
                        flush()
                        continue
                else:
                    video_path = item.get('video_path') or ''
                    if not video_path or not os.path.isfile(video_path):
                        row_state['status'] = 'failed'
                        failures.append(f'Baris {idx}: file video tidak ditemukan.')
                        state['pct'] = int((idx / total) * 100)
                        flush()
                        continue

                def progress(v, row_idx=idx, row=row_state):
                    try:
                        row_pct = max(0, min(100, int(v)))
                    except (TypeError, ValueError):
                        row_pct = 0
                    row['status'] = f'{row_pct}%'
                    state['pct'] = int((((row_idx - 1) + (row_pct / 100.0)) / total) * 100)
                    state['message'] = f'Upload TikTok batch {row_idx}/{total}...'
                    flush()

                ok, err = _upload_with_retry(upload_video, acc_id, item, invisible,
                                             progress, default_type, upload_photos)
                if ok:
                    row_state['status'] = 'completed'
                    if delete_after and not is_foto:
                        _delete_video_file(item.get('video_path'))
                else:
                    row_state['status'] = 'failed'
                    failures.append(f'Baris {idx}: {err or "upload gagal."}')
                state['pct'] = int((idx / total) * 100)
                flush()
                if idx < total:
                    import time
                    time.sleep(2)  # jeda antar video, sama seperti worker TEKOTOK

            # Baris di-skip (CSV invalid: path salah/caption kosong/dst) itu
            # KATEGORI BEDA dari upload yang benar2 dicoba lalu gagal - kalau
            # semua baris valid berhasil diupload, job tetap dianggap SUKSES
            # (bukan seolah semuanya gagal) tapi info skip tetap ditampilkan
            # supaya user tahu ada baris yang tidak ikut diproses & kenapa.
            if failures:
                notes = skipped_msgs + failures
                state.update(done=True, success=False,
                             error='; '.join(notes[:5]) + (f' (+{len(notes)-5} lainnya)' if len(notes) > 5 else ''))
            elif skipped_msgs:
                state.update(done=True, success=True, pct=100,
                             message=(f'Upload TikTok batch selesai ({total} video). '
                                      f'{len(skipped_msgs)} baris CSV dilewati (invalid): ' +
                                      '; '.join(skipped_msgs[:5])))
            else:
                state.update(done=True, success=True, message='Upload TikTok batch selesai.', pct=100)
            flush()
            return 0 if not failures else 1

        if is_foto:
            from app.core.uploader import foto_dari_kolom
            if not foto_dari_kolom(job.get('photos') or ''):
                state.update(done=True, error='Tidak ada file foto yang bisa dibaca.')
                flush()
                return 1
        else:
            video_path = job.get('video_path') or ''
            if not video_path or not os.path.isfile(video_path):
                state.update(done=True, error=f'File video tidak ditemukan: {video_path}')
                flush()
                return 1

        def progress(v):
            try:
                state['pct'] = int(v)
            except (TypeError, ValueError):
                pass
            state['message'] = f'Upload TikTok... {state["pct"]}%'
            flush()

        ok, err = _upload_with_retry(upload_video, acc_id, job, bool(job.get('invisible')),
                                     progress, (job.get('upload_type') or 'regular').lower(),
                                     upload_photos)
        if ok:
            state.update(pct=100, done=True, success=True, message='Upload TikTok selesai.')
            if delete_after and not is_foto:
                _delete_video_file(job.get('video_path'))
        else:
            state.update(done=True, success=False,
                         error=(err or 'Upload TikTok gagal.') +
                               ' Pastikan profil sudah login TikTok (buka shortcut TikTok lalu Continue with Google).')
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        _delete_gam_account(acc_id)
    flush()
    return 0 if state['success'] else 1


def run_selftest():
    result = {'tekotok_import': False, 'uploader_import': False, 'db_ok': False, 'error': None}
    try:
        _prepare_tekotok()
        import app.db.database as _db  # noqa
        result['tekotok_import'] = True
        from app.core import uploader  # noqa
        result['uploader_import'] = hasattr(uploader, 'upload_video')
        acc_id, _ = _register_gam_account(os.getcwd())
        result['db_ok'] = bool(acc_id)
        _delete_gam_account(acc_id)
    except Exception as e:
        result['error'] = str(e)
    print(json.dumps(result))
    return 0 if result['db_ok'] and result['uploader_import'] else 1


def main():
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
    return run_upload(job)


if __name__ == '__main__':
    raise SystemExit(main())
