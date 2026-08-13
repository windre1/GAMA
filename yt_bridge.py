"""
yt_bridge.py — Jembatan antara Google Account Manager dan YT Uploader.

Dijalankan sebagai worker subprocess oleh main.py:
    python yt_bridge.py <job_json_path>
    python yt_bridge.py --selftest

Job JSON punya field 'mode':
    - "single"        : upload satu video (video_data).
    - "batch"         : upload banyak video dari CSV (csv_path).
    - "list_channels" : deteksi daftar channel YouTube pada satu profil gmail.

Worker memakai ulang engine BrowserUploadService milik YT Uploader, tapi
menyuntikkan profil Chrome milik GAM (data/google_profiles/<email>) langsung
ke engine — gmail yang sudah login di GAM bisa dipakai upload tanpa login ulang.

Karena satu gmail bisa punya banyak channel (brand account), worker bisa
mengganti channel aktif sebelum upload dan memverifikasinya lewat URL
YouTube Studio (studio.youtube.com/channel/UCxxxx).
"""
import os
import re
import sys
import json
import time
import argparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
YT_UPLOADER_DIR = os.environ.get('YT_UPLOADER_DIR', os.path.join(BASE_DIR, 'engines', 'yt_uploader'))

_CHANNEL_ID_RE = re.compile(r'/channel/(UC[\w-]+)')


def load_engine_class():
    if YT_UPLOADER_DIR not in sys.path:
        sys.path.insert(0, YT_UPLOADER_DIR)
    from services.browser_upload_service import BrowserUploadService
    return BrowserUploadService


def _read_active_channel_id(page):
    """Baca channel_id yang sedang aktif via URL YouTube Studio."""
    try:
        page.get('https://studio.youtube.com/?hl=en')
        time.sleep(4)
        m = _CHANNEL_ID_RE.search(page.url or '')
        if m:
            return m.group(1)
        html = page.html or ''
        if 'externalId":"' in html:
            cid = html.split('externalId":"')[1].split('"')[0]
            if cid.startswith('UC'):
                return cid
    except Exception:
        pass
    return None


def _channel_display(page, channel_id):
    """Baca nama channel + @handle dari halaman channel publik."""
    try:
        page.get(f'https://www.youtube.com/channel/{channel_id}?hl=en')
        time.sleep(2.2)
        info = page.run_js('''
            const d = window.ytInitialData || {};
            const meta = (d.metadata && d.metadata.channelMetadataRenderer) || {};
            let name = meta.title
                || (document.querySelector('meta[property="og:title"]') || {}).content
                || (document.title || '').replace(/ - YouTube$/, '');
            let vanity = meta.vanityChannelUrl || '';
            let handle = '';
            const hm = (vanity || '').match(/@[\\w.-]+/);
            if (hm) handle = hm[0];

            // Jumlah subscriber: cari di ytInitialData (beberapa layout)
            let subs = '';
            try {
                const raw = JSON.stringify(d);
                let sm = raw.match(/"subscriberCountText":\\{[^}]*?"simpleText":"([^"]+)"/);
                if (!sm) sm = raw.match(/"([\\d.,]+\\s*[KMB]?\\s*subscribers?)"/i);
                if (!sm) sm = raw.match(/"(No subscribers)"/i);
                if (sm) subs = sm[1];
                if (!subs) {
                    const bm = (document.body.innerText || '').match(/([\\d.,]+\\s*[KMB]?)\\s+subscribers?/i);
                    if (bm) subs = bm[0];
                }
            } catch (e) {}
            return { name: (name || '').trim(), handle: handle, subscribers: (subs || '').trim() };
        ''') or {}
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def _studio_current_subscribers(page):
    """Baca 'Current subscribers' dari dashboard YouTube Studio yang sedang terbuka."""
    try:
        return page.run_js(r'''
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            let node;
            while (node = walker.nextNode()) {
                if ((node.innerText || '').trim().toLowerCase() === 'current subscribers') {
                    const p = node.parentElement;
                    const t = ((p && p.innerText) || '').replace(/\n/g, ' ');
                    const m = t.match(/current subscribers\s*([\d.,]+\s*[KMB]?)/i);
                    return m ? m[1].trim() : '0';
                }
            }
            return '';
        ''') or ''
    except Exception:
        return ''


def _open_account_menu(page):
    """Buka avatar menu YouTube. Return teks header akun aktif."""
    page.get('https://www.youtube.com/?hl=en')
    time.sleep(4)
    page.run_js("(document.querySelector('#avatar-btn')||{click(){}}).click();")
    time.sleep(1.6)
    return page.run_js('''
        const h = document.querySelector('ytd-active-account-header-renderer');
        return h ? h.innerText : '';
    ''') or ''


def _active_handle(page):
    """@handle channel yang sedang aktif (dari avatar menu)."""
    txt = _open_account_menu(page)
    m = re.search(r'@[\w.-]+', txt or '')
    return m.group(0) if m else ''


def _accounts_menu_channels(page):
    """Semua channel dari menu 'Switch account': nama, handle, subscriber. + handle aktif."""
    active_txt = _open_account_menu(page)
    active = ''
    m = re.search(r'@[\w.-]+', active_txt or '')
    if m:
        active = m.group(0)

    # klik "Switch account" untuk memunculkan daftar semua channel
    page.run_js('''
        const els = document.querySelectorAll(
            'ytd-compact-link-renderer, tp-yt-paper-item, yt-formatted-string, a');
        for (const el of els) {
            const t = (el.innerText || '').trim().toLowerCase();
            if (t === 'switch account' || t === 'ganti akun') { el.click(); return true; }
        }
        return false;
    ''')
    time.sleep(2.5)

    items = page.run_js(r'''
        const out = [];
        document.querySelectorAll('ytd-account-item-renderer').forEach(el => {
            const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
            const name = lines[0] || '';
            const handle = lines.find(l => l.startsWith('@')) || '';
            const subs = lines.find(l => /subscriber/i.test(l)) || '';
            out.push({ name: name, handle: handle, subscribers: subs });
        });
        return out;
    ''') or []
    return items, active


def _scrape_channels(page):
    """Daftar SEMUA channel di satu gmail (nama, handle, subscriber) + handle aktif.

    Sumber utama: menu 'Switch account' (memuat subscriber semua channel sekaligus).
    Fallback (akun 1 channel): header akun aktif + 'Current subscribers' dari Studio.
    """
    try:
        items, active = _accounts_menu_channels(page)
    except Exception:
        items, active = [], ''

    channels = []
    for it in items:
        subs = (it.get('subscribers') or '').strip()
        if re.match(r'(?i)^no subscribers', subs):
            subs = '0'
        channels.append({
            'id': '',
            'name': it.get('name') or (it.get('handle') or 'Channel'),
            'handle': it.get('handle') or '',
            'subscribers': subs,
        })

    if channels:
        return channels, active

    # Fallback: akun dengan satu channel (tidak ada menu Switch account)
    try:
        active_id = _read_active_channel_id(page)  # mendarat di Studio channel aktif
        subs = _studio_current_subscribers(page) or '0'
        info = _channel_display(page, active_id) if active_id else {}
        handle = info.get('handle') or active
        channels.append({
            'id': active_id or '',
            'name': info.get('name') or handle or 'Channel',
            'handle': handle or '',
            'subscribers': subs,
        })
        return channels, (handle or active)
    except Exception:
        return channels, active


def build_bridge_service(profile_path, target_channel=None):
    BrowserUploadService = load_engine_class()

    class _StubAccount:
        auth_method = 'REAL_CHROME'
        channel_name = 'GAM Profile'
        name = 'GAM Profile'
        session_path = profile_path
        token_path = None
        cookies_json = None
        credentials_json = None

    class GamBridgeUploadService(BrowserUploadService):
        def __init__(self, gam_profile, target=None, **kw):
            super().__init__(**kw)
            self._gam_profile = os.path.abspath(gam_profile)
            self._target_channel = (target or '').strip() or None

        def _get_account(self, account_id):
            return _StubAccount()

        def _get_session_path(self, account_id):
            return self._gam_profile

        def _switch_channel(self, page):
            """Pindah ke channel target (handle/nama) & verifikasi. Return (ok, active_handle)."""
            target = (self._target_channel or '').strip()
            if not target:
                return True, _active_handle(page)

            active = _active_handle(page)
            if active and active.lower() == target.lower():
                return True, active

            # Buka avatar → Switch account, klik item yang cocok handle/nama
            try:
                _open_account_menu(page)
                page.run_js('''
                    const els = document.querySelectorAll(
                        'ytd-compact-link-renderer, tp-yt-paper-item, yt-formatted-string, a');
                    for (const el of els) {
                        const t = (el.innerText || '').trim().toLowerCase();
                        if (t === 'switch account' || t === 'ganti akun') { el.click(); return true; }
                    }
                    return false;
                ''')
                time.sleep(2.2)
                page.run_js('''
                    const target = (arguments[0] || '').toLowerCase();
                    const items = document.querySelectorAll('ytd-account-item-renderer');
                    for (const el of items) {
                        if ((el.innerText || '').toLowerCase().includes(target)) {
                            el.click(); return true;
                        }
                    }
                    return false;
                ''', target)
                time.sleep(4)
            except Exception:
                pass

            active = _active_handle(page)
            ok = bool(active) and (active.lower() == target.lower()
                                   or target.lower() in active.lower())
            return ok, active

        def _get_page(self, session_path, invisible=False):
            from services.browser_upload_service import acquire_port
            profile_path = os.path.abspath(self._gam_profile)
            os.makedirs(profile_path, exist_ok=True)

            port = self._port or acquire_port()
            self._port = port
            self.kill_chrome_on_port(port)
            time.sleep(1.2)

            co = self._build_options(profile_path, port, invisible)
            self.page = self._launch_chrome(co, port)
            self._inject_stealth(self.page)

            if self._target_channel:
                ok, active = self._switch_channel(self.page)
                if not ok:
                    raise Exception(
                        f"Gagal beralih ke channel {self._target_channel} "
                        f"(channel aktif: {active or 'tidak diketahui'}). "
                        "Upload dibatalkan agar tidak salah channel.")
            return self.page

    return GamBridgeUploadService(profile_path, target=target_channel)


def write_status(status_path, **fields):
    tmp = status_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(fields, f, ensure_ascii=False)
    os.replace(tmp, status_path)


def _map_row(row):
    """Samakan kunci hasil csv_loader dengan yang dibaca engine (_do_upload)."""
    item = dict(row)
    if 'privacy' not in item and 'visibility' in item:
        item['privacy'] = str(item.get('visibility') or 'public').lower()
    return item


def run_list_channels(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    state = {'pct': 10, 'message': 'Membuka profil...', 'done': False,
             'success': False, 'error': None, 'channels': []}
    write_status(status_path, **state)

    if not os.path.isdir(profile_path):
        state.update(done=True, error='Folder profil tidak ditemukan.')
        write_status(status_path, **state)
        return 1

    svc = build_bridge_service(profile_path)
    try:
        page = svc._get_page(profile_path, invisible=True)
        state.update(pct=60, message='Membaca daftar channel...')
        write_status(status_path, **state)
        channels, active = _scrape_channels(page)
        # tandai channel aktif + gunakan handle sebagai nilai pilihan
        for c in channels:
            c['active'] = bool(c.get('handle')) and c['handle'].lower() == (active or '').lower()
        state.update(pct=100, done=True, success=True,
                     message=f'{len(channels)} channel ditemukan.',
                     channels=channels, active=active)
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            svc.stop_browser()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_single(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    state = {'pct': 0, 'message': 'Menyiapkan...', 'done': False,
             'success': False, 'error': None, 'video_id': None}

    def progress(pct, message):
        try:
            state['pct'] = int(pct)
        except (TypeError, ValueError):
            pass
        if message:
            state['message'] = str(message)
        write_status(status_path, **state)

    progress(0, 'Menyiapkan...')

    if not os.path.isdir(profile_path):
        state.update(done=True, error='Folder profil tidak ditemukan.')
        write_status(status_path, **state)
        return 1

    vd = _map_row(job['video_data'])
    if not vd.get('video_path') or not os.path.isfile(vd['video_path']):
        state.update(done=True, error=f"File video tidak ditemukan: {vd.get('video_path')}")
        write_status(status_path, **state)
        return 1

    # Kumpulkan log per-step dari safe_log engine untuk ditampilkan di UI
    session_log = []

    try:
        svc = build_bridge_service(profile_path, job.get('channel_id'))
        # Patch safe_log agar setiap step engine terlihat di UI
        original_safe_log = svc.safe_log
        def patched_safe_log(msg):
            original_safe_log(msg)
            clean = str(msg).strip()
            if clean:
                session_log.append(clean)
                # Update message ke log terbaru yang bermakna
                if any(kw in clean.upper() for kw in
                       ['THUMBNAIL', 'AUDIENCE', 'AI]', 'NEXT', 'UPLOAD', 'ERROR',
                        'NAVIGATE', 'SUCCESS', 'GAGAL', 'BERHASIL', 'PUBLISH']):
                    state['message'] = clean
                state['log'] = session_log[-150:]
                write_status(status_path, **state)
        svc.safe_log = patched_safe_log
    except Exception as e:
        state.update(done=True, error=f'Gagal memuat engine: {e}')
        write_status(status_path, **state)
        return 1

    if job.get('dry_run'):
        state.update(pct=100, done=True, success=True,
                     message='Dry run OK — engine siap, tidak ada upload dilakukan.')
        write_status(status_path, **state)
        return 0

    # `ai_content` dari main.py ditaruh di level job (bukan job['options']) —
    # sebelumnya TIDAK PERNAH sampai ke engine karena upload_single cuma baca
    # `job.get('options', {})`, jadi checkbox AI-content di mode single
    # diam-diam tidak berefek.
    options = dict(job.get('options') or {})
    if 'ai_content' in job:
        options['ai_content'] = bool(job.get('ai_content'))
    # Audience dikunci: GAMA selalu upload sebagai "Not made for kids".
    options['made_for_kids'] = False
    vd['made_for_kids'] = False
    # Teruskan invisible/headless dari job ke options dengan nama yang benar
    if 'invisible' in job:
        options.setdefault('invisible_mode', bool(job.get('invisible')))

    try:
        ok = svc.upload_single('gam', vd, options, progress)
        if ok:
            state.update(pct=100, done=True, success=True, message='Upload selesai.')
        else:
            state.update(done=True, success=False,
                         error=state.get('error') or 'Upload gagal.')
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            svc.stop_browser()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_batch(job):
    status_path = job['status_path']
    profile_path = job['profile_path']
    csv_path = job.get('csv_path')
    state = {'pct': 0, 'message': 'Memuat CSV...', 'done': False,
             'success': False, 'error': None, 'rows': []}
    write_status(status_path, **state)

    if not os.path.isdir(profile_path):
        state.update(done=True, error='Folder profil tidak ditemukan.')
        write_status(status_path, **state)
        return 1
    if not csv_path or not os.path.isfile(csv_path):
        state.update(done=True, error=f'File CSV tidak ditemukan: {csv_path}')
        write_status(status_path, **state)
        return 1

    try:
        if YT_UPLOADER_DIR not in sys.path:
            sys.path.insert(0, YT_UPLOADER_DIR)
        from utils.csv_loader import load_video_csv
        rows = load_video_csv(csv_path)
    except Exception as e:
        state.update(done=True, error=f'Gagal membaca CSV: {e}')
        write_status(status_path, **state)
        return 1

    if not rows:
        state.update(done=True, error='CSV kosong atau format tidak dikenali.')
        write_status(status_path, **state)
        return 1

    # Hanya baris yang dicentang user (indeks 0-based dari pratinjau)
    selected = job.get('selected_rows')
    if isinstance(selected, list) and selected:
        idx_set = set()
        for i in selected:
            try:
                idx_set.add(int(i))
            except (TypeError, ValueError):
                pass
        rows = [r for i, r in enumerate(rows) if i in idx_set]
        if not rows:
            state.update(done=True, error='Tidak ada baris CSV terpilih.')
            write_status(status_path, **state)
            return 1

    total = len(rows)
    state['rows'] = [{'title': r.get('title') or r.get('video_path') or f'Baris {i+1}',
                      'status': 'antre'} for i, r in enumerate(rows)]
    state.update(message=f'{total} video akan diproses.')
    write_status(status_path, **state)

    if job.get('dry_run'):
        for r in state['rows']:
            r['status'] = 'dry-ok'
        state.update(pct=100, done=True, success=True,
                     message=f'Dry run OK — {total} baris valid, tidak ada upload.')
        write_status(status_path, **state)
        return 0

    try:
        svc = build_bridge_service(profile_path, job.get('channel_id'))
    except Exception as e:
        state.update(done=True, error=f'Gagal memuat engine: {e}')
        write_status(status_path, **state)
        return 1

    # Teruskan log per-step engine ke UI, sama seperti mode single.
    # SEBELUMNYA batch tidak melakukan ini: pesan di UI hanya berubah saat
    # progress callback dipanggil, sehingga selama langkah panjang (mis.
    # thumbnail) UI diam di satu baris & persentase yang sama — kelihatan
    # "nge-stuck" padahal worker jalan terus.
    cursor = {'i': 0, 'total': len(rows)}
    session_log = []
    _original_safe_log = svc.safe_log

    def _patched_safe_log(msg):
        _original_safe_log(msg)
        clean = str(msg).strip()
        if not clean:
            return
        session_log.append(clean)
        if any(kw in clean.upper() for kw in
               ('THUMBNAIL', 'AUDIENCE', 'AI]', 'READY', 'NEXT', 'UPLOAD', 'ERROR',
                'NAVIGATE', 'SUCCESS', 'GAGAL', 'BERHASIL', 'PUBLISH', 'SCHED')):
            state['message'] = f"[{cursor['i'] + 1}/{cursor['total']}] {clean}"
        state['log'] = session_log[-150:]
        write_status(status_path, **state)

    svc.safe_log = _patched_safe_log

    # Opsi level-batch: short/long form + privasi default (dipakai bila baris CSV
    # tidak menyetel sendiri). Diteruskan ke engine YT Uploader.
    options = dict(job.get('options', {}) or {})
    if job.get('yt_type'):
        options.setdefault('yt_type', job['yt_type'])
        options.setdefault('is_short', job['yt_type'] == 'short')
    if job.get('privacy'):
        options.setdefault('privacy', job['privacy'])
    # Audience dikunci: GAMA selalu upload sebagai "Not made for kids".
    options['made_for_kids'] = False
    success = 0
    try:
        for i, row in enumerate(rows):
            cursor['i'] = i
            item = _map_row(row)
            if not item.get('privacy') and job.get('privacy'):
                item['privacy'] = job['privacy']
            # Logika ai_content:
            # - CSV isi 1/yes/true → override paksa True (per-baris eksplisit)
            # - CSV isi 0/kosong/tidak diisi → ikuti checkbox GAMA (force_* dari job)
            # - CSV isi false/no → override paksa False
            # parse_bool_value return None untuk 0/kosong = ikuti GAMA
            ai_val = item.get('ai_content')
            if ai_val is None:
                # Tidak diisi/0 di CSV → ikuti checkbox GAMA
                if job.get('force_ai_content'):
                    item['ai_content'] = True
                else:
                    item.pop('ai_content', None)  # biarkan engine pakai default
            # Jika True/False eksplisit dari CSV → biarkan apa adanya

            # made_for_kids: dikunci FALSE untuk semua baris. Kolom
            # MADE FOR KIDS di CSV maupun checkbox GAMA sengaja tidak dipakai —
            # video "made for kids" kehilangan komentar, notifikasi, playlist,
            # dan personalisasi iklan, jadi GAMA tidak pernah menyetelnya.
            if item.get('made_for_kids'):
                # print bisa gagal di build windowed (stdout None) — jangan
                # sampai menjatuhkan batch hanya karena baris log.
                try:
                    print(f"[INFO] Baris {i+1}: kolom MADE FOR KIDS di CSV "
                          f"diabaikan, audience selalu 'No'.", flush=True)
                except Exception:
                    pass
            item['made_for_kids'] = False
            base = int(i / total * 100)

            def progress(pct, message, _b=base, _i=i):
                frac = int(_b + (pct / total))
                state['pct'] = min(99, max(state['pct'], frac))
                state['message'] = f"[{_i+1}/{total}] {message}" if message else state['message']
                write_status(status_path, **state)

            if not item.get('video_path') or not os.path.isfile(item['video_path']):
                state['rows'][i]['status'] = 'file hilang'
                write_status(status_path, **state)
                continue

            state['rows'][i]['status'] = 'upload...'
            write_status(status_path, **state)
            try:
                ok = svc.upload_single('gam', item, options, progress)
                if ok:
                    success += 1
                    state['rows'][i]['status'] = 'sukses'
                else:
                    state['rows'][i]['status'] = 'gagal'
            except Exception as e:
                # Bersihkan error message — hapus newline dan batasan karakter
                # agar pesan error lengkap terlihat di UI tanpa terpotong.
                err_clean = ' | '.join(
                    line.strip() for line in str(e).splitlines() if line.strip()
                )
                state['rows'][i]['status'] = f'gagal: {err_clean}'
            write_status(status_path, **state)

            if i < total - 1:
                # Jeda antar video. Sekarang benar-benar berarti: upload_single
                # tidak lagi kembali sebelum unggahan mencapai 100%, jadi jeda
                # ini murni untuk memberi YouTube Studio waktu menutup dialog
                # dan menyiapkan form berikutnya.
                #
                # Dulu jeda 8 detik ini menyesatkan — tampak seperti ada jeda,
                # padahal video sebelumnya baru terunggah 50-70% dan browser
                # sudah ditutup, sehingga unggahannya batal.
                JEDA = 20
                state['message'] = (f'Video {i+1}/{total} selesai. '
                                    f'Jeda {JEDA} detik sebelum video berikutnya...')
                write_status(status_path, **state)
                time.sleep(JEDA)

        state.update(pct=100, done=True, success=(success > 0),
                     message=f'Selesai: {success}/{total} berhasil.')
        if success == 0:
            state['error'] = 'Semua upload gagal.'
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
    finally:
        try:
            svc.stop_browser()
        except Exception:
            pass
    write_status(status_path, **state)
    return 0 if state['success'] else 1


def run_selftest():
    result = {'engine_import': False, 'subclass_ok': False,
              'overrides_ok': False, 'error': None}
    try:
        load_engine_class()
        result['engine_import'] = True
        svc = build_bridge_service(os.getcwd(), target_channel='UCtest')
        result['subclass_ok'] = True
        acc = svc._get_account('gam')
        sp = svc._get_session_path('gam')
        result['overrides_ok'] = (
            getattr(acc, 'auth_method', '') == 'REAL_CHROME'
            and os.path.abspath(sp) == os.path.abspath(os.getcwd())
            and svc._target_channel == 'UCtest'
        )
    except Exception as e:
        result['error'] = str(e)
    print(json.dumps(result))
    return 0 if result['overrides_ok'] else 1


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

    mode = job.get('mode', 'single')
    if mode == 'list_channels':
        return run_list_channels(job)
    if mode == 'batch':
        return run_batch(job)
    return run_single(job)


if __name__ == '__main__':
    raise SystemExit(main())
