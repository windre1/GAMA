"""
tg_manager.py — Engine Telegram (akun user via MTProto/Telethon) untuk GAM.

Fitur: login akun (HP → kode → 2FA), scrape member grup/channel, dan blast
pesan ke daftar target. BUKAN bot — pakai akun user seperti Chat-Grow.

Semua client berjalan di satu event-loop asyncio pada thread khusus; Flask
memanggilnya sinkron lewat run_coroutine_threadsafe.
"""
import os
import time
import asyncio
import threading

import chatgrow

# Kredensial API default (sama seperti fallback Chat-Grow — Telegram Desktop).
# User sebaiknya isi punya sendiri dari https://my.telegram.org (Settings).
DEFAULT_API_ID = 6
DEFAULT_API_HASH = 'eb06d4abfb4903de849291bc74098590'

_avail_cache = None


def engine_available():
    global _avail_cache
    if _avail_cache is None:
        try:
            import telethon  # noqa: F401
            _avail_cache = (True, '')
        except Exception as e:
            _avail_cache = (False,
                            f'Engine Telegram (telethon) tidak bisa dimuat: {e}. '
                            'Install dengan "pip install telethon" lalu restart GAM.')
    return _avail_cache


_AUTH_CACHE_TTL = 20  # detik


class TGManager:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.clients = {}          # name -> TelegramClient
        self.pending = {}          # name -> {'phone', 'hash'} saat menunggu kode
        self.lock = threading.Lock()
        self.api_id = DEFAULT_API_ID
        self.api_hash = DEFAULT_API_HASH
        self._auth_cache = {}      # name -> (authorized: bool, checked_at: float)

    # ── loop asyncio ────────────────────────────────────────────────────────

    def _ensure_loop(self):
        if self.loop and self.loop.is_running():
            return
        chatgrow.ensure_dirs()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name='tg-loop')
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, coro, timeout=120):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def set_api(self, api_id, api_hash):
        try:
            self.api_id = int(api_id) or DEFAULT_API_ID
        except (TypeError, ValueError):
            self.api_id = DEFAULT_API_ID
        self.api_hash = (api_hash or '').strip() or DEFAULT_API_HASH

    def _session_path(self, name):
        return os.path.join(chatgrow.TG_DIR, chatgrow.safe_name(name))

    async def _new_client(self, name):
        from telethon import TelegramClient
        client = TelegramClient(self._session_path(name), self.api_id, self.api_hash)
        await client.connect()
        return client

    # ── auth ────────────────────────────────────────────────────────────────

    def list_sessions(self):
        chatgrow.ensure_dirs()
        names = set(self.clients.keys())
        for fn in os.listdir(chatgrow.TG_DIR):
            if fn.endswith('.session'):
                names.add(fn[:-8])
        out = []
        for n in sorted(names):
            out.append({'name': n, 'authorized': self._is_authorized(n),
                        'pending': n in self.pending})
        return out

    def _is_authorized(self, name, use_cache=True):
        # PENTING (27 Jul): frontend poll /api/tg/status tiap 2.5 detik (near
        # real-time chat) -- kalau tiap poll harus connect+cek jaringan ke
        # Telegram utk SETIAP akun tersimpan, satu-satunya event loop/thread TG
        # jadi macet ketumpuk (makin parah dgn makin banyak akun), dan operasi
        # LAIN (mis. login akun baru) ikut ke-antre lama di belakangnya --
        # gejalanya kelihatan seperti "tidak bisa nambah akun ke-2". Cache hasil
        # tiap sesi selama _AUTH_CACHE_TTL detik supaya poll tidak selalu hit
        # jaringan.
        if use_cache:
            cached = self._auth_cache.get(name)
            if cached and (time.time() - cached[1]) < _AUTH_CACHE_TTL:
                return cached[0]

        async def _check():
            client = self.clients.get(name)
            close = False
            if client is None:
                client = await self._new_client(name)
                close = True
            try:
                return await client.is_user_authorized()
            finally:
                if close:
                    await client.disconnect()
        # is_user_authorized() panggil jaringan ke Telegram -- retry sampai 3x
        # dulu (exception ATAUPUN hasil False yang "bersih") sebelum disimpulkan
        # "belum login". Dibuktikan 26 Jul: sesi yang SUDAH login bisa kena
        # false-negative sesaat kalau koneksi/server Telegram lelet di percobaan
        # pertama -- retry mandiri berikutnya balik True. TIDAK boleh dianggap
        # "belum login beneran" dari SATU kali cek gagal saja.
        ok = False
        for attempt in range(3):
            try:
                ok = bool(self._call(_check(), timeout=30))
            except Exception:
                ok = False
            if ok:
                break
            if attempt < 2:
                time.sleep(2)
        self._auth_cache[name] = (ok, time.time())
        return ok

    def start_login(self, name, phone):
        name = chatgrow.safe_name(name)
        phone = (phone or '').strip()
        if not name or not phone:
            return {'error': 'Nama sesi dan nomor HP wajib diisi.'}

        async def _send():
            client = await self._new_client(name)
            self.clients[name] = client
            if await client.is_user_authorized():
                return {'success': True, 'already': True}
            sent = await client.send_code_request(phone)
            self.pending[name] = {'phone': phone, 'hash': sent.phone_code_hash}
            return {'success': True, 'code_sent': True}
        try:
            return self._call(_send(), timeout=60)
        except Exception as e:
            return {'error': f'Gagal kirim kode: {e}'}

    def verify_code(self, name, code):
        name = chatgrow.safe_name(name)
        pend = self.pending.get(name)
        client = self.clients.get(name)
        if not pend or not client:
            return {'error': 'Tidak ada proses login untuk sesi ini. Mulai login dulu.'}

        async def _verify():
            from telethon.errors import SessionPasswordNeededError
            try:
                await client.sign_in(phone=pend['phone'], code=(code or '').strip(),
                                     phone_code_hash=pend['hash'])
            except SessionPasswordNeededError:
                return {'need_2fa': True}
            me = await client.get_me()
            self.pending.pop(name, None)
            return {'success': True, 'me': _me_label(me)}
        try:
            result = self._call(_verify(), timeout=60)
            if result.get('success'):
                self._auth_cache[name] = (True, time.time())
            return result
        except Exception as e:
            return {'error': f'Kode salah / gagal: {e}'}

    def verify_2fa(self, name, password):
        name = chatgrow.safe_name(name)
        client = self.clients.get(name)
        if not client:
            return {'error': 'Tidak ada proses login untuk sesi ini.'}

        async def _pw():
            await client.sign_in(password=(password or ''))
            me = await client.get_me()
            self.pending.pop(name, None)
            return {'success': True, 'me': _me_label(me)}
        try:
            result = self._call(_pw(), timeout=60)
            if result.get('success'):
                self._auth_cache[name] = (True, time.time())
            return result
        except Exception as e:
            return {'error': f'Password 2FA salah / gagal: {e}'}

    def logout(self, name):
        name = chatgrow.safe_name(name)

        async def _out():
            client = self.clients.get(name) or await self._new_client(name)
            try:
                await client.log_out()
            finally:
                self.clients.pop(name, None)
                self.pending.pop(name, None)
            return {'success': True}
        try:
            res = self._call(_out(), timeout=30)
        except Exception as e:
            res = {'error': str(e)}
        # Hapus file session sisa
        for suf in ('.session', '.session-journal'):
            p = self._session_path(name) + suf
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        self._auth_cache.pop(name, None)
        return res

    async def _get_client(self, name):
        client = self.clients.get(name)
        if client is None:
            client = await self._new_client(name)
            self.clients[name] = client
        if not await client.is_user_authorized():
            raise RuntimeError(f'Sesi Telegram "{name}" belum login.')
        return client

    # ── dialogs (grup/channel yang diikuti) ─────────────────────────────────

    def list_dialogs(self, name):
        async def _list():
            client = await self._get_client(name)
            out = []
            async for d in client.iter_dialogs():
                if d.is_group or d.is_channel:
                    ent = d.entity
                    out.append({
                        'id': str(d.id),
                        'title': d.name or '',
                        'is_channel': bool(d.is_channel and not d.is_group),
                        'username': getattr(ent, 'username', '') or '',
                        'participants': getattr(ent, 'participants_count', 0) or 0,
                    })
            return out
        try:
            return {'success': True, 'dialogs': self._call(_list(), timeout=90)}
        except Exception as e:
            return {'error': str(e)}

    def list_topics(self, name, target):
        """Daftar topic (forum) dari grup/channel yang punya fitur Topics
        Telegram aktif (mis. grup diskusi besar spt "TYM"). Grup biasa (bukan
        forum) akan balik list kosong -- bukan error, biar caller bisa fallback
        ke tampilan biasa."""
        async def _list():
            from telethon.tl.functions.messages import GetForumTopicsRequest
            client = await self._get_client(name)
            entity = await client.get_entity(_peer(target))
            if not getattr(entity, 'forum', False):
                return {'is_forum': False, 'topics': []}
            res = await client(GetForumTopicsRequest(
                peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100,
            ))
            topics = []
            for t in res.topics:
                # ForumTopicDeleted tidak punya title/closed -- skip
                if not hasattr(t, 'title'):
                    continue
                # icon_color = RGB integer (mis. 0x6FB9F0) persis kayak dipakai
                # Telegram app/web sendiri utk ikon bulat topic -- convert ke hex CSS.
                icon_color = getattr(t, 'icon_color', None)
                topics.append({
                    'id': t.id,
                    'title': t.title or '',
                    'closed': bool(getattr(t, 'closed', False)),
                    'pinned': bool(getattr(t, 'pinned', False)),
                    'icon_color': f'#{icon_color:06x}' if icon_color is not None else '#6FB9F0',
                })
            return {'is_forum': True, 'topics': topics}
        # Retry spt _is_authorized() -- "database is locked" bisa muncul sesaat
        # kalau instance GAMA lain lagi pegang file session yang sama.
        last_err = None
        for attempt in range(3):
            try:
                return {'success': True, **self._call(_list(), timeout=60)}
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2)
        return {'error': str(last_err)}

    # ── chat client (daftar chat, riwayat, kirim) ───────────────────────────

    def chat_dialogs(self, name, limit=80):
        """Daftar semua chat (user, grup, channel) untuk klien chat Telegram."""
        async def _list():
            client = await self._get_client(name)
            out = []
            async for d in client.iter_dialogs(limit=limit):
                ent = d.entity
                last = d.message
                out.append({
                    'id': str(d.id),
                    'name': d.name or '',
                    'kind': ('channel' if (d.is_channel and not d.is_group)
                             else 'group' if d.is_group else 'user'),
                    'username': getattr(ent, 'username', '') or '',
                    'last_text': (getattr(last, 'message', '') or '')[:120] if last else '',
                    'last_ts': int(last.date.timestamp()) if last and last.date else 0,
                    'unread': d.unread_count or 0,
                    'is_forum': bool(getattr(ent, 'forum', False)),
                })
            return out
        try:
            return {'success': True, 'chats': self._call(_list(), timeout=90)}
        except Exception as e:
            return {'error': str(e)}

    def get_history(self, name, peer, limit=50, topic_id=None):
        async def _hist():
            client = await self._get_client(name)
            ent = await client.get_entity(_peer(peer))
            msgs = []
            kwargs = {'reply_to': int(topic_id)} if topic_id else {}
            for m in await client.get_messages(ent, limit=limit, **kwargs):
                if not (m.message or ''):
                    continue
                sender = ''
                try:
                    s = m.sender
                    sender = _me_label(s) if s else ''
                except Exception:
                    pass
                msgs.append({
                    'from_me': bool(m.out),
                    'sender': 'saya' if m.out else (sender or ''),
                    'text': m.message or '',
                    'ts': int(m.date.timestamp()) if m.date else 0,
                })
            msgs.reverse()
            return msgs
        try:
            return {'success': True, 'messages': self._call(_hist(), timeout=60)}
        except Exception as e:
            return {'error': str(e)}

    def mark_read(self, name, peer):
        async def _read():
            client = await self._get_client(name)
            ent = await client.get_entity(_peer(peer))
            await client.send_read_acknowledge(ent)
            return True
        try:
            self._call(_read(), timeout=30)
        except Exception:
            pass
        return {'success': True}

    def delete_dialog(self, name, peer):
        """Hapus chat dari daftar Telegram (sama seperti 'Delete chat' di app:
        DM = hapus untuk diri sendiri; grup/channel = keluar)."""
        async def _del():
            client = await self._get_client(name)
            ent = await client.get_entity(_peer(peer))
            await client.delete_dialog(ent)
            return True
        last_err = None
        # File sesi (.session) SQLite bisa kena "database is locked" sesaat
        # kalau ada window GAMA lain yang baru saja/lagi pakai sesi Telegram
        # yang sama (mis. polling status chat tiap beberapa detik) - ini
        # transien, jadi coba lagi beberapa kali dengan jeda sebelum benar2
        # dilaporkan gagal ke user.
        for attempt in range(4):
            try:
                self._call(_del(), timeout=45)
                return {'success': True}
            except Exception as e:
                last_err = e
                if 'database is locked' in str(e).lower() and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        return {'error': str(last_err)}

    # ── scrape member ───────────────────────────────────────────────────────

    async def _iter_participants_safe(self, client, entity, label, progress=None):
        """Coba ambil daftar participant asli. Return None (bukan raise) kalau
        Telegram diam-diam menyembunyikan sebagian besar list-nya (fitur privasi
        grup "Hide Members" / channel besar) - dideteksi dari GetFullChannelRequest
        (participants_hidden), BUKAN dari error, karena Telegram tidak selalu
        melempar ChatAdminRequiredError untuk kasus ini - kadang cuma diam-diam
        balikin daftar yang sangat pendek (mis. cuma admin)."""
        from telethon.tl.types import Channel

        hidden = False
        if isinstance(entity, Channel):
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await client(GetFullChannelRequest(entity))
                hidden = bool(getattr(full.full_chat, 'participants_hidden', False))
            except Exception:
                pass
        if hidden:
            return None

        members = []
        n = 0
        async for u in client.iter_participants(entity):
            members.append({
                'name': _me_label(u),
                'phone': (u.phone or '') if getattr(u, 'phone', None) else '',
                'username': u.username or '',
                'user_id': str(u.id),
                'is_admin': '',
            })
            n += 1
            if progress and n % 50 == 0:
                progress(n, label)
        return members

    def scrape_members(self, name, target, progress=None):
        """Scrape member grup/channel. target: username, id, atau link undangan.
        Kalau daftar member disembunyikan Telegram dari non-admin (fitur privasi
        "Hide Members", atau ChatAdminRequiredError), otomatis alihkan ke scrape
        histori chat (pengirim pesan asli) di grup yang sama - biasanya tetap
        dapat kontak aktif meski daftar member resminya tertutup."""
        from telethon.errors import ChatAdminRequiredError, ChannelPrivateError
        async def _scrape():
            client = await self._get_client(name)
            entity = await client.get_entity(_peer(target))
            label = getattr(entity, 'title', None) or getattr(entity, 'username', '') or str(target)
            try:
                members = await self._iter_participants_safe(client, entity, label, progress)
            except ChannelPrivateError:
                raise RuntimeError(
                    f'Tidak bisa akses "{label}" — akun ini belum join, atau channel/grup '
                    'private dan butuh invite link yang masih valid.'
                )
            except ChatAdminRequiredError:
                members = None
            if members is not None:
                return label, members
            # Daftar member disembunyikan - fallback ke histori chat (pengirim
            # pesan asli), masih di grup yang sama.
            fallback = await _scrape_message_senders(client, entity, label, progress)
            return f'{label} (dari histori chat — daftar member disembunyikan admin)', fallback
        label, members = self._call(_scrape(), timeout=600)
        return label, members

    def scrape_channel(self, name, target, progress=None):
        """Scrape KHUSUS untuk channel broadcast (satu arah, cuma admin yang
        posting). Telegram TIDAK PERNAH mengizinkan non-admin lihat daftar
        subscriber channel murni — ini batasan platform, semua tool (termasuk
        Telegram Desktop resmi) kena batas yang sama, bukan sesuatu yang bisa
        di-bypass GAMA. Jadi di sini kita:
        1. Coba iter_participants dulu — kalau targetnya sebenarnya supergroup
           (bukan channel murni) atau kebetulan daftar membernya publik, ini
           langsung berhasil dan selesai di sini.
        2. Kalau ditolak/disembunyikan DAN channel itu punya grup
           diskusi/komentar yang ditautkan, otomatis alihkan scrape ke grup
           diskusi itu (isinya identitas user asli yang komentar di channel).
        3. Kalau tidak ada grup diskusi sama sekali, kasih tahu jujur bahwa
           channel ini memang tidak bisa di-scrape member-nya oleh siapa pun
           selain admin."""
        from telethon.errors import ChatAdminRequiredError, ChannelPrivateError
        from telethon.tl.functions.channels import GetFullChannelRequest

        async def _scrape():
            client = await self._get_client(name)
            entity = await client.get_entity(_peer(target))
            label = getattr(entity, 'title', None) or getattr(entity, 'username', '') or str(target)
            try:
                members = await self._iter_participants_safe(client, entity, label, progress)
                if members is not None:
                    return label, members
            except ChannelPrivateError:
                raise RuntimeError(
                    f'Tidak bisa akses "{label}" — akun ini belum join, atau channel '
                    'private dan butuh invite link yang masih valid.'
                )
            except ChatAdminRequiredError:
                pass  # channel broadcast murni - coba fallback grup diskusi di bawah

            full = await client(GetFullChannelRequest(entity))
            linked_id = getattr(full.full_chat, 'linked_chat_id', None)
            if not linked_id:
                raise RuntimeError(
                    f'"{label}" adalah channel broadcast tanpa grup diskusi/komentar. '
                    'Telegram tidak mengizinkan siapa pun selain admin melihat daftar '
                    'subscriber channel murni (ini batasan platform, bukan GAMA) — '
                    'dan channel ini tidak punya kolom komentar untuk dijadikan sumber '
                    'kontak alternatif. Coba mode "Histori Chat" kalau channel-nya '
                    'sebenarnya sebuah grup, atau pilih target lain.'
                )
            linked_entity = next((c for c in full.chats if getattr(c, 'id', None) == linked_id), None)
            if linked_entity is None:
                linked_entity = await client.get_entity(linked_id)
            linked_label = getattr(linked_entity, 'title', None) or getattr(linked_entity, 'username', '') or str(linked_id)

            fallback = await _scrape_message_senders(
                client, linked_entity, f'{label} (komentar @ {linked_label})', progress)
            return f'{label} (komentar)', fallback

        label, members = self._call(_scrape(), timeout=900)
        return label, members

    def scrape_history(self, name, target, limit=2000, progress=None, topic_id=None):
        """Scrape kontak dari HISTORI CHAT: kumpulkan pengirim unik dari pesan
        terakhir sebuah grup/channel/chat. Berguna untuk grup yang tidak
        mengizinkan lihat daftar member, atau untuk channel diskusi.

        topic_id (opsional): kalau grup-nya forum (punya Topics, mis. "TYM"),
        scrape cuma pesan di topic itu (reply_to=topic_id di Telethon) --
        lebih presisi drpd nyampur semua topic sekaligus."""
        async def _scrape():
            client = await self._get_client(name)
            entity = await client.get_entity(_peer(target))
            label = getattr(entity, 'title', None) or getattr(entity, 'username', '') or str(target)
            members = await _scrape_message_senders(client, entity, label, progress, limit=limit, topic_id=topic_id)
            return label, members
        label, members = self._call(_scrape(), timeout=900)
        return label, members

    # ── blast ───────────────────────────────────────────────────────────────

    def send_message(self, name, target, text, topic_id=None):
        async def _send():
            client = await self._get_client(name)
            tgt = _norm_target(target)
            try:
                tgt = int(tgt)   # user_id numerik
            except (TypeError, ValueError):
                pass
            kwargs = {'reply_to': int(topic_id)} if topic_id else {}
            await client.send_message(tgt, text, **kwargs)
            return True
        return self._call(_send(), timeout=60)

    def send_media(self, name, target, file_path, caption='', topic_id=None):
        async def _send():
            client = await self._get_client(name)
            tgt = _norm_target(target)
            try:
                tgt = int(tgt)
            except (TypeError, ValueError):
                pass
            kwargs = {'reply_to': int(topic_id)} if topic_id else {}
            await client.send_file(tgt, file_path, caption=caption or None,
                                    **_send_file_kwargs(file_path), **kwargs)
            return True
        return self._call(_send(), timeout=180)


def _send_file_kwargs(file_path):
    """Kwargs tambahan utk Telethon client.send_file() berdasar tipe file --
    supaya video kepreview sbg video (streaming) di chat, bukan turun sbg
    dokumen mentah. Sebelumnya fungsi ini dipanggil tapi TIDAK PERNAH
    didefinisikan sama sekali -> kirim media Telegram selalu NameError crash."""
    ext = os.path.splitext(file_path or '')[1].lower()
    if ext in ('.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'):
        return {'supports_streaming': True}
    return {}


async def _scrape_message_senders(client, entity, label, progress=None, limit=2000, topic_id=None):
    """Kumpulkan pengirim UNIK (user asli, bukan bot/channel) dari histori
    pesan sebuah entity - dipakai untuk mode "Histori Chat" dan sebagai
    fallback kalau daftar member/subscriber resmi disembunyikan/ditolak."""
    from telethon.tl.types import User
    seen = {}
    n = 0
    kwargs = {'reply_to': int(topic_id)} if topic_id else {}
    async for m in client.iter_messages(entity, limit=limit, **kwargs):
        try:
            s = m.sender
        except Exception:
            s = None
        # Post channel broadcast dikirim ATAS NAMA channel itu sendiri
        # (m.sender jadi entity Channel, bukan User asli) - buang, supaya
        # tidak keikut sebagai "kontak" palsu.
        if not isinstance(s, User) or s.bot:
            continue
        uid = str(getattr(s, 'id', '') or '')
        if not uid or uid in seen:
            continue
        seen[uid] = {
            'name': _me_label(s),
            'phone': (getattr(s, 'phone', '') or ''),
            'username': getattr(s, 'username', '') or '',
            'user_id': uid,
            'is_admin': '',
        }
        n += 1
        if progress and n % 25 == 0:
            progress(n, label)
    return list(seen.values())


def _me_label(u):
    parts = [getattr(u, 'first_name', '') or '', getattr(u, 'last_name', '') or '']
    label = ' '.join(p for p in parts if p).strip()
    return label or (getattr(u, 'username', '') or str(getattr(u, 'id', '')))


def _norm_target(t):
    t = str(t or '').strip()
    return t.lstrip('@') if t.startswith('@') else t


def _peer(t):
    """Untuk get_entity: id numerik → int, selain itu username/link string."""
    t = _norm_target(t)
    try:
        return int(t)
    except (TypeError, ValueError):
        return t


manager = TGManager()
