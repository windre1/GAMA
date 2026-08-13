"""
voice_over_bridge.py — Worker Voice Over (Gemini Canvas) untuk GAMA.

Dijalankan sebagai subprocess:
    python voice_over_bridge.py <job_json_path>

Job JSON: {email, script, voice, bahasa, gaya, out_dir, status_path}
"""
import os
import sys
import json
import argparse

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from omnivora.voice_over import GeminiVoiceOverGenerator
from gama_helpers import load_meta, generate_user_agent


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(fields, f, ensure_ascii=False)
        os.replace(tmp, status_path)
    except Exception:
        pass


def run_job(job):
    status_path = job['status_path']
    email = (job.get('email') or '').strip()
    script = (job.get('script') or '').strip()
    voice = (job.get('voice') or '').strip() or 'Puck'
    bahasa = (job.get('bahasa') or '').strip() or 'Indonesia'
    gaya = (job.get('gaya') or '').strip() or 'natural'
    out_dir = job.get('out_dir') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'voice_over')

    state = {'pct': 0, 'message': 'Memulai...', 'done': False, 'success': False, 'error': '', 'files': []}

    def flush():
        write_status(status_path, **state)

    flush()
    if not email:
        state.update(done=True, error='Pilih akun Gmail dulu.')
        flush(); return 1
    if not script:
        state.update(done=True, error='Naskah voice over kosong.')
        flush(); return 1

    def status_cb(msg):
        try:
            print(msg)
        except Exception:
            pass
        state['message'] = str(msg).strip()
        flush()

    meta = load_meta()
    ua = (meta.get(email, {}) or {}).get('user_agent') or generate_user_agent()

    try:
        gen = GeminiVoiceOverGenerator(email, user_agent=ua, log_cb=lambda m, l='info': status_cb(m))
        ok = gen.generate(script, voice, bahasa, gaya, out_dir, status_cb=status_cb)
        if ok and gen.last_output_path:
            state.update(pct=100, done=True, success=True, files=[gen.last_output_path],
                         message=f'Voice over selesai: {gen.last_output_path}', folder=out_dir)
        else:
            state.update(done=True, success=False, error='Voice over gagal dibuat.')
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
