"""
wa_manager.py — Engine WhatsApp multi-sesi untuk GAM (via neonize/whatsmeow).

Dipakai oleh tab Chat di GAM: login QR multi-akun, terima/kirim pesan,
daftar chat, dan hook auto-reply. Semua berjalan in-process (thread per sesi).
"""
import os
import re
import sys
import time
import sqlite3
import threading
import mimetypes

# Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
           else os.path.dirname(os.path.abspath(__file__))
WA_DIR   = os.path.join(BASE_DIR, 'data', 'whatsapp')
WA_DB    = os.path.join(WA_DIR, 'sessions.db')    # store login neonize/whatsmeow
MSG_DB   = os.path.join(WA_DIR, 'messages.db')    # store pesan & chat GAM

_avail_cache = None


def engine_available():
    """Cek engine neonize bisa dimuat (sekali saja, hasil dicache)."""
    global _avail_cache
    if _avail_cache is None:
        try:
            import neonize  # noqa: F401
            _avail_cache = (True, '')
        except Exception as e:
            _avail_cache = (False,
                            f'Engine WhatsApp (neonize) tidak bisa dimuat: {e}. '
                            'Install dengan "pip install neonize" lalu restart GAM.')
    return _avail_cache


# ─── Store pesan (sqlite; koneksi per operasi supaya aman lintas thread) ─────

def _conn():
    os.makedirs(WA_DIR, exist_ok=True)
    conn = sqlite3.connect(MSG_DB, timeout=10)
    conn.execute('''CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session TEXT, chat_jid TEXT, sender_jid TEXT, sender_name TEXT,
        from_me INTEGER, msg_id TEXT, text TEXT, media_type TEXT, ts INTEGER)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(session, chat_jid, ts)')
    conn.execute('''CREATE TABLE IF NOT EXISTS chats(
        session TEXT, jid TEXT, name TEXT, is_group INTEGER DEFAULT 0,
        last_ts INTEGER, last_text TEXT, unread INTEGER DEFAULT 0,
        PRIMARY KEY(session, jid))''')
    return conn


def _extract_text(message) -> tuple[str, str]:
    """Ambil teks & jenis media dari proto Message WhatsApp."""
    try:
        if message.conversation:
            return message.conversation, ''
        if message.extendedTextMessage.text:
            return message.extendedTextMessage.text, ''
        if message.imageMessage.ByteSize():
            return message.imageMessage.caption or '', 'image'
        if message.videoMessage.ByteSize():
            return message.videoMessage.caption or '', 'video'
        if message.documentMessage.ByteSize():
            return message.documentMessage.fileName or '', 'document'
        if message.audioMessage.ByteSize():
            return '', 'audio'
        if message.stickerMessage.ByteSize():
            return '', 'sticker'
    except Exception:
        pass
    return '', ''


def _norm_ts(ts) -> int:
    ts = int(ts or 0)
    if ts > 10**12:          # milidetik → detik
        ts //= 1000
    return ts or int(time.time())


def _media_kind(path: str) -> str:
    """Tentukan jenis media dari ekstensi/mime → 'image' | 'video' | 'document'."""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'):
        return 'image'
    if ext in ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.3gp', '.m4v'):
        return 'video'
    mime = (mimetypes.guess_type(path)[0] or '').lower()
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('video/'):
        return 'video'
    return 'document'


def _is_real_chat(jid: str) -> bool:
    """Bukan pseudo-chat: Status WhatsApp (status@broadcast) & broadcast list
    (@broadcast) TIDAK ditampilkan sebagai chat biasa."""
    j = (jid or '').strip().lower()
    if not j:
        return False
    return not j.endswith('@broadcast')


def _looks_like_phone_name(value: str) -> bool:
    raw = (value or '').strip()
    if not raw:
        return True
    compact = re.sub(r'[^\d+]+', '', raw)
    normalized = re.sub(r'[\s().-]+', '', raw)
    return bool(compact) and len(compact) >= 5 and compact == normalized


def _pick_chat_name(stored_name: str, msg_name: str, jid: str) -> str:
    stored = (stored_name or '').strip()
    latest = (msg_name or '').strip()
    fallback = (jid or '').split('@')[0]
    if latest and (_looks_like_phone_name(stored) or not stored):
        return latest
    if stored:
        return stored
    if latest:
        return latest
    return fallback


class WASession:
    def __init__(self, name):
        self.name = name
        self.aliases = {name}
        self.status = 'starting'   # starting | qr | connected | disconnected | logged_out | error
        self.qr_code = ''          # kode QR aktif (string mentah)
        self.qr_ts = 0.0
        self.me = ''
        self.error = ''
        self.client = None
        self.thread = None
        self.jid = None            # JID device (untuk reconnect/resync)
        self.display = name        # nama tampilan (PushName); name = kunci stabil (nomor)
        self.synced = False        # sudah tarik daftar grup dari server?
        self.name_refreshing = False
        self.name_refresh_ts = 0.0
        self.db_path = WA_DB       # file DB whatsmeow milik akun ini (per-akun)

    def info(self):
        return {'name': self.name, 'display': self.display or self.name,
                'status': self.status, 'me': self.me, 'error': self.error,
                'aliases': sorted(a for a in self.aliases if a)}


class WAManager:
    def __init__(self):
        self.sessions: dict[str, WASession] = {}
        self.lock = threading.Lock()
        self.factories: dict[str, object] = {}   # db_path -> ClientFactory (1 per akun)
        self.started = False
        # Hook auto-reply: fn(session_name, chat_jid, sender_name, text) -> None
        self.on_message = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _session_for_client(self, client) -> 'WASession | None':
        for s in list(self.sessions.values()):
            if s.client is client:
                return s
        return None

    def _resolve_session_name(self, name: str) -> str:
        raw = str(name or '').strip()
        if not raw:
            return ''
        if raw in self.sessions:
            return raw
        raw_l = raw.lower()
        raw_digits = re.sub(r'\D+', '', raw)
        for s in list(self.sessions.values()):
            if raw in s.aliases:
                return s.name
            if raw_l in {str(a).lower() for a in s.aliases if a}:
                return s.name
            probes = [s.name, s.display, s.me]
            if any((p or '').strip().lower() == raw_l for p in probes):
                return s.name
            if raw_digits and any(re.sub(r'\D+', '', (p or '')) == raw_digits for p in probes if p):
                return s.name
        return raw

    def _session(self, name: str):
        return self.sessions.get(self._resolve_session_name(name))

    def _factory_for(self, db_path: str):
        """Satu ClientFactory per FILE DB. KUNCI multi-akun whatsmeow: tiap akun
        WA WAJIB punya database sendiri. Kalau berbagi 1 DB, akun baru malah
        nyambung ke device lama (QR tak muncul)."""
        from neonize.client import ClientFactory
        f = self.factories.get(db_path)
        if f is None:
            os.makedirs(WA_DIR, exist_ok=True)
            f = ClientFactory(db_path)
            self.factories[db_path] = f
            self._bind_events(f)
        return f

    def _bind_events(self, factory):
        """Daftarkan handler event pada factory (dispatch ke sesi yang benar
        lewat identitas objek client — lintas semua factory/akun)."""
        from neonize.events import (ConnectedEv, PairStatusEv, MessageEv,
                                    LoggedOutEv, DisconnectedEv)
        from neonize.utils import Jid2String, JIDToNonAD

        @factory.event(ConnectedEv)
        def _on_conn(client, ev):
            s = self._session_for_client(client)
            if not s:
                return
            s.qr_code = ''
            s.status = 'connected'
            s.synced = False   # picu tarik ulang daftar grup di poll berikutnya
            try:
                me = client.get_me()
                s.jid = me.JID
                s.me = Jid2String(JIDToNonAD(me.JID))
                phone = me.JID.User
                s.display = (getattr(me, 'PushName', '') or '').strip() or phone
                # Kanonikalkan nama sesi ke NOMOR (stabil lintas restart). Nama
                # awal dari add_session (mis. "Windre") atau PushName ("WinzDigital")
                # bisa beda-beda → data terpecah. Migrasikan ke nomor.
                if phone and s.name != phone:
                    old = s.name
                    s.aliases.update({old, phone, s.display, s.me})
                    self._migrate_session(old, phone)
                    with self.lock:
                        self.sessions.pop(old, None)
                        s.name = phone
                        self.sessions[phone] = s
                else:
                    s.aliases.update({s.name, phone, s.display, s.me})
                threading.Thread(target=lambda: (time.sleep(8), self._refresh_personal_names(s, limit=400)), daemon=True).start()
            except Exception:
                pass

        @factory.event(PairStatusEv)
        def _on_pair(client, ev):
            s = self._session_for_client(client)
            if s:
                s.qr_code = ''
                s.status = 'connected'

        @factory.event(LoggedOutEv)
        def _on_logout(client, ev):
            s = self._session_for_client(client)
            if s:
                s.status = 'logged_out'

        @factory.event(DisconnectedEv)
        def _on_disc(client, ev):
            s = self._session_for_client(client)
            if s and s.status == 'connected':
                s.status = 'disconnected'

        @factory.event(MessageEv)
        def _on_message(client, ev):
            s = self._session_for_client(client)
            if s:
                try:
                    self._store_incoming(s, ev)
                except Exception:
                    pass

        from neonize.events import HistorySyncEv

        @factory.event(HistorySyncEv)
        def _on_history(client, ev):
            s = self._session_for_client(client)
            if s:
                try:
                    self._store_history(s, ev)
                except Exception:
                    pass

    def _migrate_session(self, old: str, new: str):
        """Pindahkan data chat/pesan dari nama sesi lama ke baru (kanonikalisasi
        ke nomor telepon supaya tidak terpecah antar restart)."""
        if not old or not new or old == new:
            return
        conn = _conn()
        try:
            conn.execute('UPDATE OR IGNORE chats SET session=? WHERE session=?', (new, old))
            conn.execute('DELETE FROM chats WHERE session=?', (old,))
            conn.execute('UPDATE messages SET session=? WHERE session=?', (new, old))
            conn.commit()
        finally:
            conn.close()

    def _account_dbs(self) -> list[str]:
        """Semua file DB whatsmeow: legacy sessions.db + acc_*.db (bukan messages.db)."""
        os.makedirs(WA_DIR, exist_ok=True)
        dbs = []
        if os.path.isfile(WA_DB):
            dbs.append(WA_DB)
        for fn in sorted(os.listdir(WA_DIR)):
            if fn.startswith('acc_') and fn.endswith('.db'):
                dbs.append(os.path.join(WA_DIR, fn))
        return dbs

    def startup(self):
        """Sambungkan ulang semua device yang pernah login (dipanggil lazy).
        Tiap file DB = 1 akun. Nama sesi = NOMOR telepon (stabil lintas restart)."""
        ok, _ = engine_available()
        if not ok or self.started:
            return
        self.started = True
        for db_path in self._account_dbs():
            try:
                devices = self._factory_for(db_path).get_all_devices()
            except Exception:
                devices = []
            for d in devices:
                phone = (d.JID.User or '').strip() or 'wa'
                if phone not in self.sessions:
                    s = self._start(phone, jid=d.JID, db_path=db_path)
                    s.display = (d.PushName or '').strip() or phone
                    s.aliases.update({phone, s.display})

    def add_session(self, name: str) -> WASession:
        """Mulai sesi baru (login via QR). Akun baru = FILE DB baru sendiri,
        supaya QR benar-benar muncul (tidak nyangkut ke device lama)."""
        name = re.sub(r'[^\w\-. ]+', '', name).strip()
        if not name:
            raise ValueError('Nama sesi tidak valid.')
        with self.lock:
            s = self._session(name)
            if s and s.status in ('starting', 'qr', 'connected'):
                return s
        safe = re.sub(r'[^\w\-.]+', '_', name).strip('_') or 'wa'
        db_path = os.path.join(WA_DIR, f'acc_{safe}.db')
        return self._start(name, uuid=name, db_path=db_path)

    def _start(self, name, jid=None, uuid=None, db_path=None) -> WASession:
        s = WASession(name)
        s.db_path = db_path or WA_DB
        with self.lock:
            self.sessions[name] = s
        s.thread = threading.Thread(target=self._run, args=(s, jid, uuid),
                                    daemon=True, name=f'wa-{name}')
        s.thread.start()
        return s

    def _device_props(self):
        """DeviceProps supaya pairing companion mulus & HP tidak "loading terus".

        Kunci masalah: default whatsmeow minta FULL history sync → HP sibuk
        mengunggah riwayat berbulan-bulan sehingga tampak loading dan perangkat
        tertaut tak kunjung terverifikasi. Kita matikan full sync & batasi
        history seminimal mungkin (GAMA hanya perlu pesan baru, bukan arsip)."""
        try:
            from neonize.client import DeviceProps
            return DeviceProps(
                os='GAMA',
                platformType=DeviceProps.CHROME,
                requireFullSync=True,
                historySyncConfig=DeviceProps.HistorySyncConfig(
                    # Prioritaskan histori personal/grup ~30 hari agar panel chat GAMA
                    # benar-benar terisi. Ini lebih berat, tapi sinkronisasi jadi nyata.
                    fullSyncDaysLimit=30,
                    fullSyncSizeMbLimit=512,
                    storageQuotaMb=1024,
                    recentSyncDaysLimit=30,
                ),
            )
        except Exception:
            return None

    def _run(self, s: WASession, jid, uuid):
        try:
            factory = self._factory_for(s.db_path)
            client = factory.new_client(jid=jid, uuid=uuid or s.name,
                                        props=self._device_props())
            s.client = client

            # QR dikirim lewat callback khusus (bukan event bus). Simpan kode
            # mentah + tandai status 'qr' supaya UI bisa render gambarnya.
            def _on_qr(c, data_qr):
                try:
                    s.qr_code = data_qr.decode() if isinstance(data_qr, (bytes, bytearray)) else str(data_qr)
                except Exception:
                    s.qr_code = ''
                s.qr_ts = time.time()
                if s.status != 'connected':
                    s.status = 'qr'
            client.qr(_on_qr)

            client.connect()   # blocking sampai stop/logout
        except Exception as e:
            s.status = 'error'
            s.error = str(e)
        finally:
            if s.status in ('starting', 'qr', 'connected'):
                s.status = 'disconnected'

    # ── pesan masuk ────────────────────────────────────────────────────────

    def _store_incoming(self, s: WASession, ev):
        from neonize.utils import Jid2String, JIDToNonAD
        info = ev.Info
        src = info.MessageSource
        chat_jid = Jid2String(JIDToNonAD(src.Chat))
        if not _is_real_chat(chat_jid):   # abaikan Status/broadcast
            return
        sender_jid = Jid2String(JIDToNonAD(src.Sender))
        text, media = _extract_text(ev.Message)
        if not text and not media:
            return
        ts = _norm_ts(info.Timestamp)
        sender_name = info.Pushname or sender_jid.split('@')[0]
        from_me = 1 if src.IsFromMe else 0
        preview = text or f'[{media}]'

        conn = _conn()
        try:
            conn.execute(
                'INSERT INTO messages(session, chat_jid, sender_jid, sender_name,'
                ' from_me, msg_id, text, media_type, ts) VALUES(?,?,?,?,?,?,?,?,?)',
                (s.name, chat_jid, sender_jid, sender_name, from_me,
                 info.ID, text, media, ts))
            # Nama chat: untuk DM pakai pushname pengirim; grup pakai jid dulu
            chat_name = sender_name if (not src.IsGroup and not from_me) else ''
            conn.execute(
                'INSERT INTO chats(session, jid, name, is_group, last_ts, last_text, unread)'
                ' VALUES(?,?,?,?,?,?,?)'
                ' ON CONFLICT(session, jid) DO UPDATE SET'
                '  last_ts=excluded.last_ts, last_text=excluded.last_text,'
                '  is_group=excluded.is_group,'
                '  name=CASE WHEN excluded.name != "" THEN excluded.name ELSE chats.name END,'
                '  unread=chats.unread + ?',
                (s.name, chat_jid, chat_name, 1 if src.IsGroup else 0,
                 ts, preview[:120], 0 if from_me else 1, 0 if from_me else 1))
            conn.commit()
        finally:
            conn.close()

        # Hook auto-reply GAM: hanya DM masuk (bukan grup, bukan pesan sendiri)
        if not from_me and not src.IsGroup and self.on_message and text:
            try:
                self.on_message(s.name, chat_jid, sender_name, text)
            except Exception:
                pass

    def _store_history(self, s: WASession, ev):
        """Simpan riwayat chat yang dikirim WhatsApp setelah link (HistorySyncEv).
        Ini yang mengisi daftar chat + pesan lama sehingga UI seperti Chat-Grow."""
        conversations = list(getattr(ev.Data, 'conversations', []) or [])
        if not conversations:
            return
        conn = _conn()
        try:
            for conv in conversations:
                chat_jid = str(conv.ID or '').strip()
                if not chat_jid or not _is_real_chat(chat_jid):
                    continue
                is_group = 1 if chat_jid.endswith('@g.us') else 0
                conv_name = (conv.name or '').strip()
                last_ts = 0
                last_text = ''
                for hsm in (conv.messages or []):
                    wmi = getattr(hsm, 'message', None)
                    if wmi is None:
                        continue
                    key = wmi.key
                    msg_id = key.ID or ''
                    if not msg_id:
                        continue
                    exists = conn.execute(
                        'SELECT 1 FROM messages WHERE session=? AND msg_id=? LIMIT 1',
                        (s.name, msg_id)).fetchone()
                    if exists:
                        continue
                    text, media = _extract_text(wmi.message)
                    if not text and not media:
                        continue
                    from_me = 1 if key.fromMe else 0
                    ts = _norm_ts(wmi.messageTimestamp)
                    sender_name = wmi.pushName or ''
                    if from_me:
                        sender_jid = s.me
                    elif is_group:
                        sender_jid = str(wmi.participant or key.participant or '')
                    else:
                        sender_jid = chat_jid
                    conn.execute(
                        'INSERT INTO messages(session, chat_jid, sender_jid, sender_name,'
                        ' from_me, msg_id, text, media_type, ts) VALUES(?,?,?,?,?,?,?,?,?)',
                        (s.name, chat_jid, sender_jid,
                         sender_name or sender_jid.split('@')[0], from_me,
                         msg_id, text, media, ts))
                    if ts >= last_ts:
                        last_ts = ts
                        last_text = text or f'[{media}]'
                # Nama chat DM: utamakan pushName manusiawi, terutama bila nama chat masih angka.
                if (not conv_name or _looks_like_phone_name(conv_name)) and not is_group:
                    row = conn.execute(
                        'SELECT sender_name FROM messages WHERE session=? AND chat_jid=?'
                        ' AND from_me=0 AND sender_name!="" ORDER BY ts DESC LIMIT 1',
                        (s.name, chat_jid)).fetchone()
                    if row and row[0]:
                        conv_name = row[0]
                if last_ts == 0:
                    last_ts = _norm_ts(getattr(conv, 'lastMsgTimestamp', 0))
                conn.execute(
                    'INSERT INTO chats(session, jid, name, is_group, last_ts, last_text, unread)'
                    ' VALUES(?,?,?,?,?,?,?)'
                    ' ON CONFLICT(session, jid) DO UPDATE SET'
                    '  last_ts=MAX(chats.last_ts, excluded.last_ts),'
                    '  last_text=CASE WHEN excluded.last_ts >= chats.last_ts AND excluded.last_text!=""'
                    '            THEN excluded.last_text ELSE chats.last_text END,'
                    '  is_group=excluded.is_group,'
                    '  name=CASE WHEN excluded.name != "" THEN excluded.name ELSE chats.name END',
                    (s.name, chat_jid, conv_name, is_group, last_ts, last_text,
                     int(getattr(conv, 'unreadCount', 0) or 0)))
            conn.commit()
        finally:
            conn.close()

    def _best_user_info_name(self, info) -> str:
        if info is None:
            return ''
        try:
            for dev in list(getattr(info, 'Devices', []) or []):
                cand = (getattr(dev, 'PushName', '') or '').strip()
                if cand:
                    return cand
                cand = (getattr(dev, 'BussinessName', '') or '').strip()
                if cand:
                    return cand
        except Exception:
            pass
        return ''

    def _refresh_personal_names(self, s: WASession, limit: int = 120, force: bool = False):
        if s.name_refreshing or not s.client or s.status != 'connected':
            return
        if not force and s.name_refresh_ts and (time.time() - s.name_refresh_ts) < 60:
            return
        s.name_refreshing = True
        s.name_refresh_ts = time.time()

        def _job():
            from neonize.utils import Jid2String
            try:
                targets = []
                conn = _conn()
                try:
                    rows = conn.execute(
                        'SELECT jid, name FROM chats WHERE session=? AND jid LIKE "%@lid" ORDER BY last_ts DESC LIMIT ?',
                        (s.name, limit)).fetchall()
                finally:
                    conn.close()
                for jid, name in rows:
                    nm = (name or '').strip()
                    if nm and not _looks_like_phone_name(nm):
                        continue
                    targets.append(jid)
                if not targets:
                    return
                infos = s.client.get_user_info(*[self._jid_obj(j) for j in targets])
                updates = []
                for item in list(infos or []):
                    try:
                        jid = Jid2String(getattr(item, 'JID', None))
                    except Exception:
                        jid = ''
                    name = self._best_user_info_name(getattr(item, 'UserInfo', None))
                    if jid and name:
                        updates.append((name, s.name, jid))
                if updates:
                    conn = _conn()
                    try:
                        conn.executemany(
                            'UPDATE chats SET name=? WHERE session=? AND jid=? AND (name="" OR name IS NULL OR name GLOB "[0-9]*")',
                            updates,
                        )
                        conn.commit()
                    finally:
                        conn.close()
            except Exception:
                pass
            finally:
                s.name_refreshing = False

        threading.Thread(target=_job, daemon=True).start()

    def _sync_groups(self, s: WASession):
        """Tarik AKTIF semua grup yang diikuti (get_joined_groups) → isi daftar
        chat. Ini yang bikin WA ikut 'sinkron' seperti Telegram, tidak cuma
        menunggu HistorySyncEv yang hanya dikirim sekali saat pairing."""
        try:
            from neonize.utils import Jid2String
            groups = s.client.get_joined_groups() or []
        except Exception:
            s.synced = False   # biar dicoba lagi di poll berikutnya
            return
        if not groups:
            return
        conn = _conn()
        try:
            for g in groups:
                try:
                    jid = Jid2String(g.JID)
                except Exception:
                    continue
                gname = (g.GroupName.Name or '').strip() or jid.split('@')[0]
                # last_ts=0 → grup tanpa pesan tampil di bawah; begitu ada pesan
                # (live/history) last_ts terupdate. Hindari asumsi tipe GroupCreated.
                conn.execute(
                    'INSERT INTO chats(session, jid, name, is_group, last_ts, last_text, unread)'
                    ' VALUES(?,?,?,1,0,"",0)'
                    ' ON CONFLICT(session, jid) DO UPDATE SET is_group=1,'
                    '  name=CASE WHEN excluded.name != "" THEN excluded.name ELSE chats.name END',
                    (s.name, jid, gname))
            conn.commit()
        finally:
            conn.close()

    def force_sync(self, name: str) -> dict:
        """Paksa tarik ulang daftar grup dari server (tombol Sync)."""
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        s.synced = True
        threading.Thread(target=self._sync_groups, args=(s,), daemon=True).start()
        self._refresh_personal_names(s, limit=400, force=True)
        return {'success': True}

    def resync_history(self, name: str) -> dict:
        """Putus & sambung ulang sesi (pakai JID tersimpan, TANPA scan QR) supaya
        WhatsApp mengirim ulang history sync → riwayat chat (personal & grup)
        masuk lewat HistorySyncEv. Ini yang menarik riwayat tanpa re-pair."""
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        jid = s.jid
        if jid is None:
            return {'error': 'JID sesi belum siap, coba lagi sebentar.'}
        db_path = s.db_path
        resolved_name = s.name

        def _cycle():
            try:
                s.client.disconnect()
            except Exception:
                pass
            time.sleep(2)
            with self.lock:
                self.sessions.pop(resolved_name, None)
            # Sambung ulang device yang sama (jid) - WhatsApp kirim ulang history
            ns = self._start(resolved_name, jid=jid, db_path=db_path)
            ns.aliases.update(s.aliases)
            ns.display = s.display

        threading.Thread(target=_cycle, daemon=True).start()
        return {'success': True}

    def delete_chat_synced(self, name: str, jid: str) -> dict:
        """Hapus chat DAN sinkron ke HP/akun WhatsApp (app-state deleteChatAction).
        Jika sinkron ke HP gagal, tetap hapus lokal & laporkan alasannya."""
        if not jid:
            return {'error': 'jid kosong.'}
        s = self._session(name)
        synced = False
        sync_error = ''
        if s and s.status == 'connected' and s.client:
            try:
                from neonize.proto.Neonize_pb2 import PatchInfo, MutationInfo
                from waSyncAction.WAWebProtobufSyncAction_pb2 import (
                    SyncActionValue, DeleteChatAction, SyncActionMessageRange,
                    SyncActionMessage)
                from waCommon.WACommon_pb2 import MessageKey
                conn = _conn()
                try:
                    row = conn.execute(
                        'SELECT msg_id, from_me, ts, sender_jid FROM messages'
                        ' WHERE session=? AND chat_jid=? ORDER BY ts DESC, id DESC LIMIT 1',
                        (self._resolve_session_name(name), jid)).fetchone()
                finally:
                    conn.close()
                now_ms = int(time.time() * 1000)
                last_ts = int(row[2]) if row and row[2] else int(time.time())
                rng = SyncActionMessageRange(lastMessageTimestamp=last_ts)
                if row and row[0]:
                    key = MessageKey(remoteJID=jid, fromMe=bool(row[1]), ID=row[0])
                    if jid.endswith('@g.us') and row[3]:
                        key.participant = row[3]
                    rng.messages.append(SyncActionMessage(key=key, timestamp=last_ts))
                val = SyncActionValue(
                    timestamp=now_ms,
                    deleteChatAction=DeleteChatAction(messageRange=rng))
                patch = PatchInfo(
                    Type=PatchInfo.WAPatchName.Value('REGULAR_HIGH'),
                    Timestamp=now_ms,
                    Mutations=[MutationInfo(Index=['deleteChat', jid], Version=6, Value=val)])
                # send_app_state() adalah panggilan native BLOCKING tanpa timeout
                # sendiri - kalau WhatsApp tidak merespons IQ-nya (pernah diamati
                # ~70 detik lalu error "info query timed out"), satu permintaan
                # hapus bisa menggantung lama dan bikin hapus massal (puluhan
                # chat sekaligus) terasa macet total. Bungkus di thread + batasi
                # tunggu 12 detik - kalau lewat, anggap gagal sinkron (chat tetap
                # dihapus lokal) drpd user nunggu tanpa kepastian.
                _res = {}
                def _do_send():
                    try:
                        s.client.send_app_state(patch)
                        _res['ok'] = True
                    except Exception as _e:
                        _res['err'] = str(_e)
                th = threading.Thread(target=_do_send, daemon=True)
                th.start()
                th.join(timeout=12)
                if th.is_alive():
                    sync_error = ('Sinkron ke HP timeout (WhatsApp tidak merespons dalam 12 detik) - '
                                  'chat tetap dihapus di GAMA.')
                elif _res.get('err'):
                    sync_error = _res['err']
                else:
                    synced = True
            except Exception as e:
                sync_error = str(e)
        self.delete_chat(name, jid)   # hapus lokal juga
        out = {'success': True, 'synced': synced}
        if not synced:
            out['sync_error'] = sync_error or 'Sesi WA belum terhubung.'
        return out

    def delete_chat(self, name: str, jid: str) -> dict:
        """Hapus chat dari tampilan GAMA (store lokal). Tidak menghapus di HP."""
        if not jid:
            return {'error': 'jid kosong.'}
        conn = _conn()
        try:
            conn.execute('DELETE FROM messages WHERE session=? AND chat_jid=?', (self._resolve_session_name(name), jid))
            conn.execute('DELETE FROM chats WHERE session=? AND jid=?', (self._resolve_session_name(name), jid))
            conn.commit()
        finally:
            conn.close()
        return {'success': True}

    # ── API untuk Flask ────────────────────────────────────────────────────

    def list_sessions(self):
        with self.lock:
            return [s.info() for s in self.sessions.values()]

    def qr_data_uri(self, name: str) -> dict:
        s = self._session(name)
        if not s:
            return {'error': 'Sesi tidak ditemukan.'}
        out = {'status': s.status, 'me': s.me, 'error': s.error}
        if s.status == 'qr' and s.qr_code:
            try:
                import segno
                out['qr'] = segno.make_qr(s.qr_code).png_data_uri(scale=6, border=2)
                out['age'] = int(time.time() - s.qr_ts)
            except Exception as e:
                out['error'] = f'Gagal membuat gambar QR: {e}'
        return out

    def chats(self, name: str):
        # Sekali per koneksi: tarik daftar grup dari server di background
        # (non-blocking) supaya daftar chat langsung terisi seperti Telegram.
        s = self._session(name)
        if s and s.status == 'connected' and s.client:
            if not s.synced:
                s.synced = True
                threading.Thread(target=self._sync_groups, args=(s,), daemon=True).start()
            self._refresh_personal_names(s)
        conn = _conn()
        try:
            # Resolusi nama: pakai chats.name; kalau kosong, ambil sender_name
            # terbaru dari pesan masuk (untuk chat @lid yang namanya angka).
            rows = conn.execute(
                'SELECT c.jid, c.name, c.is_group, c.last_ts, c.last_text, c.unread,'
                ' (SELECT m.sender_name FROM messages m'
                '   WHERE m.session=c.session AND m.chat_jid=c.jid AND m.from_me=0'
                '     AND m.sender_name!="" ORDER BY m.ts DESC LIMIT 1) AS msg_name'
                ' FROM chats c WHERE c.session=? AND c.jid NOT LIKE "%@broadcast"'
                ' ORDER BY c.last_ts DESC LIMIT 1000',
                (self._resolve_session_name(name),)).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            nm = _pick_chat_name(r[1], r[6], r[0])
            last_text = r[4] or ''
            is_group = bool(r[2])
            is_placeholder_personal = (not is_group and r[0].endswith('@lid') and not last_text and _looks_like_phone_name(nm))
            if is_placeholder_personal:
                continue
            out.append({'jid': r[0], 'name': nm, 'is_group': is_group,
                        'last_ts': r[3], 'last_text': last_text, 'unread': r[5]})
        return out

    def messages(self, name: str, jid: str, limit: int = 50):
        conn = _conn()
        try:
            rows = conn.execute(
                'SELECT sender_name, from_me, text, media_type, ts FROM messages'
                ' WHERE session=? AND chat_jid=? ORDER BY ts DESC, id DESC LIMIT ?',
                (self._resolve_session_name(name), jid, limit)).fetchall()
        finally:
            conn.close()
        rows.reverse()
        return [{'sender': r[0], 'from_me': bool(r[1]), 'text': r[2],
                 'media': r[3], 'ts': r[4]} for r in rows]

    def mark_read(self, name: str, jid: str):
        conn = _conn()
        try:
            conn.execute('UPDATE chats SET unread=0 WHERE session=? AND jid=?', (self._resolve_session_name(name), jid))
            conn.commit()
        finally:
            conn.close()

    def _jid_obj(self, jid_str: str):
        from neonize.utils import build_jid
        user, _, server = jid_str.partition('@')
        return build_jid(user, server or 's.whatsapp.net')

    def _store_outgoing(self, name: str, jid: str, sender_jid: str, text: str, media_type: str = '') -> dict:
        name = self._resolve_session_name(name)
        ts = int(time.time())
        preview = text or (f'[{media_type}]' if media_type else '')
        conn = _conn()
        try:
            conn.execute(
                'INSERT INTO messages(session, chat_jid, sender_jid, sender_name,'
                ' from_me, msg_id, text, media_type, ts) VALUES(?,?,?,?,1,"",?,?,?)',
                (name, jid, sender_jid, 'saya', text, media_type, ts))
            conn.execute(
                'INSERT INTO chats(session, jid, name, last_ts, last_text, unread)'
                ' VALUES(?,?,?,?,?,0)'
                ' ON CONFLICT(session, jid) DO UPDATE SET last_ts=excluded.last_ts,'
                ' last_text=excluded.last_text',
                (name, jid, '', ts, preview[:120]))
            conn.commit()
        finally:
            conn.close()
        return {'success': True, 'ts': ts}

    def send_text(self, name: str, jid: str, text: str) -> dict:
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        text = (text or '').strip()
        if not text:
            return {'error': 'Pesan kosong.'}
        try:
            s.client.send_message(self._jid_obj(jid), text)
        except Exception as e:
            return {'error': f'Gagal mengirim: {e}'}
        return self._store_outgoing(self._resolve_session_name(name), jid, s.me, text)

    def send_media(self, name: str, jid: str, file_path: str, caption: str = '') -> dict:
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        file_path = (file_path or '').strip()
        if not file_path or not os.path.isfile(file_path):
            return {'error': f'File media tidak ditemukan: {file_path}'}
        media_type = _media_kind(file_path)
        try:
            if media_type == 'image':
                s.client.send_image(self._jid_obj(jid), file_path, caption=caption or None)
            elif media_type == 'video':
                s.client.send_video(self._jid_obj(jid), file_path, caption=caption or None)
            else:
                mime = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
                s.client.send_document(
                    self._jid_obj(jid), file_path, caption=caption or None,
                    filename=os.path.basename(file_path), mimetype=mime
                )
        except Exception as e:
            return {'error': f'Gagal mengirim media: {e}'}
        return self._store_outgoing(self._resolve_session_name(name), jid, s.me, caption or os.path.basename(file_path), media_type)

    def new_chat(self, name: str, phone: str) -> dict:
        """Validasi nomor via WhatsApp lalu buat entri chat kosong."""
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        phone = re.sub(r'\D+', '', phone or '')
        if not phone:
            return {'error': 'Nomor tidak valid.'}
        try:
            res = s.client.is_on_whatsapp(phone)
            hit = next((r for r in res if r.IsIn), None)
            if hit is None:
                return {'error': f'Nomor {phone} tidak terdaftar di WhatsApp.'}
            from neonize.utils import Jid2String, JIDToNonAD
            jid = Jid2String(JIDToNonAD(hit.JID))
        except Exception as e:
            return {'error': f'Gagal cek nomor: {e}'}
        conn = _conn()
        try:
            conn.execute(
                'INSERT OR IGNORE INTO chats(session, jid, name, last_ts, last_text, unread)'
                ' VALUES(?,?,?,?, "", 0)', (self._resolve_session_name(name), jid, phone, int(time.time())))
            conn.commit()
        finally:
            conn.close()
        return {'success': True, 'jid': jid}

    def check_numbers(self, name: str, phones: list) -> dict:
        """Cek daftar nomor sekaligus: apakah terdaftar di WhatsApp. Return
        {'success': True, 'results': [{'phone','on_wa','jid'}]}. Dipakai fitur
        Ekstraktor Prospek untuk menandai lead yang punya WA aktif."""
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        from neonize.utils import Jid2String, JIDToNonAD
        results = []
        for raw in (phones or []):
            phone = re.sub(r'\D+', '', str(raw or ''))
            if not phone:
                results.append({'phone': raw, 'on_wa': False, 'jid': ''})
                continue
            on_wa, jid = False, ''
            try:
                res = s.client.is_on_whatsapp(phone)
                hit = next((r for r in res if r.IsIn), None)
                if hit is not None:
                    on_wa = True
                    jid = Jid2String(JIDToNonAD(hit.JID))
            except Exception:
                pass
            results.append({'phone': phone, 'on_wa': on_wa, 'jid': jid})
            time.sleep(0.4)   # jeda kecil supaya tidak dianggap spam oleh WhatsApp
        return {'success': True, 'results': results}

    # ── grup & scrape member (untuk Scrape & Blast) ─────────────────────────

    def list_groups(self, name: str) -> dict:
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        try:
            from neonize.utils import Jid2String
            groups = s.client.get_joined_groups() or []
            out = []
            for g in groups:
                # GroupAnnounce.IsAnnounce=True → hanya admin yang boleh kirim
                # (grup pengumuman). 'open' = grup umum bebas dikirimi member biasa.
                # GroupAnnounce objek bersarang {IsAnnounce, AnnounceVersionID} — BUKAN bool.
                announce = bool(getattr(g.GroupAnnounce, 'IsAnnounce', False))
                out.append({
                    'jid': Jid2String(g.JID),
                    'name': (g.GroupName.Name or '').strip() or Jid2String(g.JID).split('@')[0],
                    'participants': len(g.Participants) if g.Participants else 0,
                    'announce': announce,
                    'open': not announce,
                })
            out.sort(key=lambda x: x['name'].lower())
            return {'success': True, 'groups': out}
        except Exception as e:
            return {'error': f'Gagal mengambil daftar grup: {e}'}

    def scrape_group_members(self, name: str, group_jid: str) -> dict:
        s = self._session(name)
        if not s or s.status != 'connected' or not s.client:
            return {'error': f'Sesi "{name}" belum terhubung.'}
        try:
            from neonize.utils import Jid2String, JIDToNonAD
            info = s.client.get_group_info(self._jid_obj(group_jid))
            label = (info.GroupName.Name or '').strip() or group_jid.split('@')[0]
            members = []
            for p in (info.Participants or []):
                jid = Jid2String(JIDToNonAD(p.JID)) if p.JID.User else ''
                phone = p.PhoneNumber or (jid.split('@')[0] if jid.endswith('s.whatsapp.net') else '')
                phone = ''.join(ch for ch in phone if ch.isdigit())
                members.append({
                    'name': p.DisplayName or '',
                    'phone': phone,
                    'username': '',
                    'user_id': jid,
                    'is_admin': 'yes' if (p.IsAdmin or p.IsSuperAdmin) else '',
                })
            return {'success': True, 'label': label, 'members': members}
        except Exception as e:
            return {'error': f'Gagal scrape member grup: {e}'}

    def scrape_chat_history(self, name: str, chat_jid: str = '') -> dict:
        """Scrape kontak dari HISTORI CHAT tersimpan (messages.db): pengirim unik
        dari pesan yang sudah masuk. chat_jid kosong = semua chat sesi ini."""
        conn = _conn()
        try:
            if chat_jid:
                rows = conn.execute(
                    'SELECT DISTINCT sender_jid, sender_name FROM messages'
                    ' WHERE session=? AND chat_jid=? AND from_me=0', (self._resolve_session_name(name), chat_jid)).fetchall()
                label = chat_jid.split('@')[0]
            else:
                rows = conn.execute(
                    'SELECT DISTINCT sender_jid, sender_name FROM messages'
                    ' WHERE session=? AND from_me=0', (self._resolve_session_name(name),)).fetchall()
                label = 'semua_chat'
        finally:
            conn.close()
        members = []
        for sender_jid, sender_name in rows:
            if not sender_jid:
                continue
            phone = sender_jid.split('@')[0] if sender_jid.endswith('s.whatsapp.net') else ''
            phone = ''.join(ch for ch in phone if ch.isdigit())
            members.append({
                'name': sender_name or '',
                'phone': phone,
                'username': '',
                'user_id': sender_jid,
                'is_admin': '',
            })
        return {'success': True, 'label': label, 'members': members}

    def disconnect(self, name: str) -> dict:
        s = self._session(name)
        if not s or not s.client:
            return {'error': 'Sesi tidak ditemukan.'}
        try:
            s.client.disconnect()
            s.status = 'disconnected'
            return {'success': True}
        except Exception as e:
            return {'error': str(e)}

    def logout(self, name: str) -> dict:
        s = self._session(name)
        if not s or not s.client:
            return {'error': 'Sesi tidak ditemukan.'}
        db_path = s.db_path
        try:
            s.client.logout()
        except Exception:
            pass
        s.status = 'logged_out'
        with self.lock:
            self.sessions.pop(s.name, None)
        self.factories.pop(db_path, None)
        # Hapus file DB akun (khusus acc_*.db) supaya bersih & bisa pasang ulang.
        if db_path and os.path.basename(db_path).startswith('acc_'):
            for suf in ('', '-shm', '-wal'):
                try:
                    p = db_path + suf
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
        return {'success': True}


manager = WAManager()
