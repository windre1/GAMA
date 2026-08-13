"""
video_generator_bridge.py — Worker video generator Google Labs Flow untuk GAM.

Dijalankan sebagai subprocess:
    python video_generator_bridge.py <job_json_path>
"""
import os
import sys
import json
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor

# Label UI Google Flow memuat emoji (mis. model "Nano Banana 🍌"). Di Windows,
# stdout default cp1252 → print(teks_emoji) melempar UnicodeEncodeError dan
# menggagalkan generate secara senyap. Paksa stdout/stderr ke UTF-8 toleran.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Pastikan import dari folder omnivora
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from omnivora.video_generator import GoogleVideoGenerator, GoogleAccountSession, sanitize_email
from gama_helpers import load_meta, generate_user_agent

def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(fields, f, ensure_ascii=False)
        os.replace(tmp, status_path)
    except Exception:
        pass

def run_check_credits(job):
    """Cek limit/credit Google Labs Flow untuk tiap akun terpilih (sekuensial,
    satu browser per akun agar hasilnya akurat)."""
    status_path = job['status_path']
    profiles = job.get('profiles', [])
    model = job.get('model', 'Omni Flash')

    state = {
        'pct': 0,
        'message': 'Memulai cek limit...',
        'done': False,
        'success': False,
        'error': '',
        'rows': []
    }

    def flush():
        write_status(status_path, **state)

    if not profiles:
        state.update(done=True, error='Tidak ada Google profile yang dipilih.')
        flush()
        return 1

    rows = [{'title': email, 'status': 'waiting'} for email in profiles]
    state['rows'] = rows
    flush()

    meta = load_meta()
    ready_count = 0
    for idx, email in enumerate(profiles):
        rows[idx]['status'] = 'memeriksa...'
        state['message'] = f'Cek limit {email} ({idx + 1}/{len(profiles)})...'
        state['pct'] = int((idx / len(profiles)) * 100)
        flush()

        ua = (meta.get(email, {}) or {}).get('user_agent') or generate_user_agent()
        try:
            session = GoogleAccountSession(email, user_agent=ua,
                                           log_cb=lambda m, l='info': None)
            res = session.check_generate_ready(model=model) or {}
            label = res.get('label') or res.get('state') or '?'
            reason = res.get('reason') or ''
            if res.get('state') == 'ready':
                ready_count += 1
            rows[idx]['status'] = label + ((' — ' + reason) if reason else '')
        except Exception as e:
            rows[idx]['status'] = f'Error: {e}'
        flush()

    state.update(
        pct=100, done=True, success=True,
        message=f'Cek limit selesai: {ready_count}/{len(profiles)} akun siap generate.'
    )
    flush()
    return 0


def run_job(job):
    if job.get('action') == 'check_credits':
        return run_check_credits(job)

    status_path = job['status_path']
    mode = job.get('mode', 'single')
    
    # Payload parameters
    prompt = job.get('prompt', '')
    image_path = job.get('image_path', '')
    prompt_variants = [str(p).strip() for p in (job.get('prompt_variants') or []) if str(p).strip()]
    image_variants = [str(p).strip() for p in (job.get('image_variants') or []) if str(p).strip()]
    scene_titles = [str(t).strip() for t in (job.get('scene_titles') or []) if str(t).strip()]
    output_names = [str(t).strip() for t in (job.get('output_names') or []) if str(t).strip()]
    ratio = job.get('ratio', '9:16')
    count = int(job.get('count', 1))
    duration = job.get('duration', '8s')
    model = job.get('model', 'Omni Flash')
    hidden = bool(job.get('hidden', False))

    # Path file sinyal resume (ditulis main.py saat user klik "Lanjutkan")
    resume_path = status_path.replace('.json', '_resume.flag')

    # Concurrency and accounts
    profiles = job.get('profiles', [])
    if not profiles and job.get('profile'):
        profiles = [job['profile']]
        
    concurrency = int(job.get('concurrency', 1))
    runs = int(job.get('runs', 1))
    
    state = {
        'pct': 0,
        'message': 'Memulai video generator...',
        'done': False,
        'success': False,
        'error': '',
        'rows': [],
        'files': [],
        'paused': False,         # True saat unusual activity terdeteksi
        'unusual_activity': False,
    }
    
    def flush():
        write_status(status_path, **state)

    flush()
    
    if not profiles:
        state.update(done=True, error='Tidak ada Google profile yang dipilih.')
        flush()
        return 1
        
    if image_variants:
        missing = [p for p in image_variants if not os.path.isfile(p)]
        if missing:
            state.update(done=True, error=f'File gambar tidak ditemukan: {missing[0]}')
            flush()
            return 1
    elif not os.path.isfile(image_path):
        state.update(done=True, error=f'File gambar tidak ditemukan: {image_path}')
        flush()
        return 1
        
    # Buat daftar baris (rows) status per email untuk UI
    row_states = []
    # Jika ada variants, tiap variant menjadi satu task. Dipakai untuk storyboard:
    # scene 1/2/3 punya prompt dan/atau gambar berbeda tetapi tetap lewat worker
    # yang sama.
    tasks = []
    variant_count = max(len(prompt_variants), len(image_variants), len(scene_titles), len(output_names))

    # Storyboard menuntut pemetaan 1:1 — scene N WAJIB memakai foto N.
    #
    # Dulu tiap daftar diambil dengan modulo (`daftar[idx % len(daftar)]`), jadi
    # kalau panjangnya berbeda indeksnya BERPUTAR diam-diam: dengan 5 scene tapi
    # 3 foto, scene 4 memakai foto 1 dan scene 5 memakai foto 2. Hasilnya
    # "salah ambil foto" tanpa satu pun pesan error. Sekarang ketidakcocokan
    # dilaporkan dan job dihentikan, bukan ditutupi.
    if variant_count:
        beda = [
            f'{nama}={len(daftar)}'
            for nama, daftar in (('prompt_variants', prompt_variants),
                                 ('image_variants', image_variants),
                                 ('scene_titles', scene_titles),
                                 ('output_names', output_names))
            if daftar and len(daftar) != variant_count
        ]
        if beda:
            state.update(done=True, success=False, error=(
                f'Jumlah scene tidak sinkron (seharusnya {variant_count} semua): '
                + ', '.join(beda)
                + '. Scene ke-N harus memakai foto ke-N, jadi job dihentikan '
                  'daripada memakai foto yang salah.'))
            flush()
            return 1

    if variant_count:
        for idx in range(variant_count):
            email = profiles[idx % len(profiles)]
            title = scene_titles[idx] if idx < len(scene_titles) else f'{email} (Scene {idx + 1})'
            row_states.append({
                'title': title,
                'status': 'waiting',
                'profile': email,
                'variant_index': idx,
                'scene_index': idx,
                'success': False,
                'files': [],
                'error': '',
            })
            tasks.append((email, idx, idx))
    elif mode == 'single' or len(profiles) == 1:
        email = profiles[0]
        for idx in range(runs):
            row_states.append({
                'title': f'{email} (Run {idx+1})',
                'status': 'waiting',
                'profile': email,
                'variant_index': None,
                'scene_index': idx,
                'success': False,
                'files': [],
                'error': '',
            })
            tasks.append((email, idx, None))
    else:
        # Batch mode: jalankan untuk semua profile
        for idx, email in enumerate(profiles):
            row_states.append({
                'title': email,
                'status': 'waiting',
                'profile': email,
                'variant_index': None,
                'scene_index': idx,
                'success': False,
                'files': [],
                'error': '',
            })
            tasks.append((email, idx, None))
            
    state['rows'] = row_states
    flush()

    total_tasks = len(tasks)
    completed_tasks = 0
    success_tasks = 0
    ordered_files = [[] for _ in tasks]
    
    lock = threading.Lock()
    stop_event = threading.Event()

    # Satu akun = satu browser pada satu waktu.
    #
    # Folder profil Chrome dan port debug ditentukan dari EMAIL saja
    # (get_profile_dir/get_debug_port), jadi dua slot berakun sama akan memakai
    # --user-data-dir dan --remote-debugging-port yang PERSIS SAMA. Chrome
    # tidak membuka instance kedua; ketiganya menempel ke browser yang sama
    # lalu saling menimpa: page.set.download_path() milik slot terakhir menang,
    # sehingga unduhan slot lain mendarat di folder yang salah dan slot itu
    # menunggu selamanya. Gejala di log user: satu scene sukses, satu
    # menggantung, satu melapor "Akun belum login" padahal jelas sudah login.
    #
    # Paralelisme tetap penuh ANTAR akun — yang diserikan hanya slot yang
    # kebetulan berbagi akun yang sama.
    kunci_akun = {}
    kunci_akun_lock = threading.Lock()

    def kunci_untuk(email):
        with kunci_akun_lock:
            if email not in kunci_akun:
                kunci_akun[email] = threading.Lock()
            return kunci_akun[email]
    
    def write_log_line(msg, level='info'):
        """Cetak defensif: encoding console tak boleh menggagalkan generate
        (emoji di label Flow, dll)."""
        try:
            print(f'[{level.upper()}] {msg}')
        except Exception:
            try:
                safe = str(msg).encode('ascii', 'replace').decode('ascii')
                print(f'[{level.upper()}] {safe}')
            except Exception:
                pass

    # Logger callback global untuk status job umum.
    def log_cb(msg, level='info'):
        write_log_line(msg, level)
        # Update message status global
        clean_msg = str(msg).strip()
        if clean_msg:
            with lock:
                state['message'] = clean_msg
                flush()

    def run_single_task(task_args, slot):
        """Bungkus: pastikan hanya SATU tugas per akun yang jalan pada satu waktu."""
        email = task_args[0]
        k = kunci_untuk(email)
        if not k.acquire(blocking=False):
            with lock:
                state['message'] = (f'Menunggu giliran akun {email} '
                                    f'(satu akun dipakai satu browser)...')
                flush()
            k.acquire()
        try:
            return _run_single_task(task_args, slot)
        finally:
            k.release()

    def _run_single_task(task_args, slot):
        nonlocal completed_tasks, success_tasks
        email, row_idx, variant_idx = task_args
        # Resume path UNIK per slot agar tiap thread punya sinyal resume sendiri.
        # resume_path global (level job) tidak dipakai di sini — jika dipakai bersama,
        # thread pertama yang membaca file langsung os.remove()-nya sehingga thread
        # lain tidak pernah mendapat sinyal dan timeout setelah 10 menit.
        slot_resume_path = status_path.replace('.json', f'_resume_slot{slot}.flag')
        # Indeks LANGSUNG, tanpa modulo. Panjang tiap daftar sudah dipastikan
        # sama dengan variant_count di atas, jadi scene ke-N pasti memakai
        # prompt, foto, dan nama berkas ke-N — tidak ada lagi indeks berputar
        # yang membuat scene mengambil foto milik scene lain.
        def _ambil(daftar, bawaan=''):
            if variant_idx is None or not daftar:
                return bawaan
            if variant_idx >= len(daftar):
                raise IndexError(
                    f'scene ke-{variant_idx + 1} tidak punya pasangan di daftar '
                    f'(panjang {len(daftar)}) — pemetaan scene ke foto tidak utuh.')
            return daftar[variant_idx]

        task_prompt = _ambil(prompt_variants, prompt)
        task_image_path = _ambil(image_variants, image_path)
        task_output_name = _ambil(output_names, '')
        
        with lock:
            row_states[row_idx]['status'] = 'processing'
            row_states[row_idx]['profile'] = email
            flush()
            
        # Dapatkan User Agent profil
        meta = load_meta()
        profile_meta = meta.get(email, {})
        ua = profile_meta.get('user_agent') or generate_user_agent()
        
        ok = False
        last_line = {'text': ''}

        def task_log_cb(msg, level='info'):
            clean_msg = str(msg).strip()
            write_log_line(clean_msg, level)
            if clean_msg:
                with lock:
                    state['message'] = clean_msg
                    compact = clean_msg
                    prefix = f'[{email}]'
                    if compact.startswith(prefix):
                        compact = compact[len(prefix):].strip()
                    if len(compact) > 160:
                        compact = compact[:157] + '...'
                    if 'ERROR' in clean_msg.upper():
                        last_line['text'] = compact
                    elif not last_line['text']:
                        last_line['text'] = compact
                    row_states[row_idx]['status'] = compact or 'processing'
                    # Deteksi unusual activity dari pesan log
                    if 'UNUSUAL_ACTIVITY' in clean_msg:
                        state['paused'] = True
                        state['unusual_activity'] = True
                    flush()

        def pause_cb():
            """Dipanggil oleh generator saat unusual activity terdeteksi."""
            with lock:
                state['paused'] = True
                state['unusual_activity'] = True
                flush()

        def resume_check():
            """Return True jika user sudah klik Lanjutkan (resume flag ditulis main.py).
            Memakai slot_resume_path yang unik per slot agar thread paralel tidak
            saling rebut satu file yang sama — thread pertama yang os.remove() akan
            membuat thread lain tidak pernah mendapat sinyal resume."""
            if os.path.isfile(slot_resume_path):
                try:
                    os.remove(slot_resume_path)
                except Exception:
                    pass
                with lock:
                    state['paused'] = False
                    state['unusual_activity'] = False
                    flush()
                return True
            # Fallback: cek juga resume_path global (backward-compat saat user klik
            # Lanjutkan dari UI yang belum sadar soal per-slot path)
            if os.path.isfile(resume_path):
                try:
                    # Salin isinya ke slot lain yang masih menunggu sebelum dihapus,
                    # supaya hanya slot INI yang consume — slot lain tetap menunggu
                    # file masing-masing. Hapus HANYA jika tidak ada slot lain paused.
                    pass
                except Exception:
                    pass
                # Anggap resume berlaku untuk semua slot yang sedang paused
                try:
                    os.remove(resume_path)
                except Exception:
                    pass
                with lock:
                    state['paused'] = False
                    state['unusual_activity'] = False
                    flush()
                return True
            return False

        try:
            generator = GoogleVideoGenerator(
                email=email,
                image_path=task_image_path,
                prompt=task_prompt,
                ratio=ratio,
                count=count,
                duration=duration,
                model=model,
                user_agent=ua,
                log_cb=task_log_cb,
                stop_event=stop_event,
                window_index=slot,
                hidden=hidden,
                output_name=task_output_name,
                pause_cb=pause_cb,
                resume_check=resume_check,
            )
            ok = generator.run_generate(status_cb=lambda m: task_log_cb(m, 'info'))
        except Exception as e:
            last_line['text'] = f'ERROR pada akun {email}: {e}'
            task_log_cb(last_line['text'], 'error')

        with lock:
            completed_tasks += 1
            if ok:
                success_tasks += 1
                row_states[row_idx]['status'] = 'success'
                row_states[row_idx]['success'] = True
                row_states[row_idx]['error'] = ''
                # Kumpulkan path file hasil untuk auto-lanjut (foto → video, dll)
                try:
                    for p in (generator.last_output_paths or []):
                        if p and p not in ordered_files[row_idx]:
                            ordered_files[row_idx].append(p)
                except Exception:
                    pass
                row_states[row_idx]['files'] = ordered_files[row_idx]
                state['files'] = [p for group in ordered_files for p in group]
            else:
                err = last_line['text'] or 'failed'
                row_states[row_idx]['status'] = err
                row_states[row_idx]['success'] = False
                row_states[row_idx]['error'] = err
                row_states[row_idx]['files'] = []
                
            state['pct'] = int((completed_tasks / total_tasks) * 100)
            state['message'] = f'Proses selesai {completed_tasks}/{total_tasks}...'
            flush()

    # Eksekusi dengan ThreadPoolExecutor agar slot browser paralel rapi
    try:
        max_workers = min(concurrency, total_tasks)
        if variant_count:
            max_workers = min(max_workers, max(1, len(profiles)))
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            # Berikan window_index/slot yang unik untuk penempatan window browser di layar
            futures = []
            for i, task in enumerate(tasks):
                # Gunakan indeks absolut (bukan modulo concurrency) agar setiap
                # task mendapat slot unik → folder staging download tidak bentrok
                # saat 3 browser paralel berjalan bersamaan (scene 1/2/3).
                slot_idx = i
                futures.append(executor.submit(run_single_task, task, slot_idx))
            
            # Tunggu semua selesai
            for f in futures:
                f.result()
                
        state.update(
            pct=100,
            done=True,
            success=(success_tasks > 0),
            message=f'Selesai: {success_tasks}/{total_tasks} berhasil.'
        )
        if success_tasks == 0:
            reasons = [
                (r.get('status') or '').strip()
                for r in row_states
                if (r.get('status') or '').strip() and (r.get('status') or '').strip() != 'success'
            ]
            state['error'] = reasons[-1] if reasons else 'Semua proses video generator gagal.'
            
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
        
    flush()
    return 0 if state['success'] else 1

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job', nargs='?')
    args = parser.parse_args()
    if not args.job or not os.path.isfile(args.job):
        print('Job file tidak ditemukan', file=sys.stderr)
        return 2
    with open(args.job, 'r', encoding='utf-8') as f:
        job = json.load(f)
    return run_job(job)

if __name__ == '__main__':
    raise SystemExit(main())
