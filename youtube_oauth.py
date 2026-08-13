"""
youtube_oauth.py — Upload YouTube via YouTube Data API v3 resmi (OAuth),
alternatif dari browser automation (yt_bridge.py / browser_upload_service.py).

Kenapa ada ini: upload via klik-DOM di YouTube Studio terbukti rapuh untuk
thumbnail custom (Studio versinya berubah-ubah, komponennya di shadow DOM,
banyak lapis event internal yang susah ditiru automation). YouTube Data API
(`videos().insert()` + `thumbnails().set()`) adalah jalur RESMI yang didukung
Google — begitu terautentikasi, hasilnya pasti konsisten.

Alur:
  1. Sekali per aplikasi: admin buat OAuth Client ID (Desktop app) di Google
     Cloud Console, download client_secrets.json, taruh di assets/.
  2. Sekali per akun/channel GAMA: `oauth_login(profile)` — buka browser,
     user login + izinkan, token hasil disimpan di data/youtube_oauth/<profile>.json.
  3. Tiap upload: `get_youtube_service(profile)` pakai token tersimpan
     (auto-refresh kalau kadaluarsa), tidak perlu login ulang.
"""
import os
import json
import sys

# Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
           else os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS_PATH = os.path.join(BASE_DIR, 'assets', 'youtube_client_secret.json')
TOKENS_DIR = os.path.join(BASE_DIR, 'data', 'youtube_oauth')

SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
]


def has_client_secrets() -> bool:
    return os.path.isfile(CLIENT_SECRETS_PATH)


def _token_path(profile: str) -> str:
    safe = ''.join(c for c in profile if c.isalnum() or c in '-_.')
    return os.path.join(TOKENS_DIR, safe + '.json')


def is_authorized(profile: str) -> bool:
    return os.path.isfile(_token_path(profile))


def list_authorized_profiles() -> list:
    if not os.path.isdir(TOKENS_DIR):
        return []
    return [f[:-5] for f in os.listdir(TOKENS_DIR) if f.endswith('.json')]


def oauth_login(profile: str):
    """Jalankan alur OAuth (buka browser, minta login+izinkan), simpan token.
    Return (channel_title, channel_id) kalau sukses. Blocking sampai user
    selesai di browser (run_local_server)."""
    if not has_client_secrets():
        raise RuntimeError(
            f'File client_secrets.json belum ada di {CLIENT_SECRETS_PATH}. '
            'Buat OAuth Client ID (Desktop app) di Google Cloud Console dulu.')
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_PATH, SCOPES)
    creds = flow.run_local_server(port=0, prompt='consent')

    youtube = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    resp = youtube.channels().list(mine=True, part='snippet,id').execute()
    items = resp.get('items') or []
    channel_title = items[0]['snippet']['title'] if items else ''
    channel_id = items[0]['id'] if items else ''

    os.makedirs(TOKENS_DIR, exist_ok=True)
    with open(_token_path(profile), 'w', encoding='utf-8') as f:
        f.write(creds.to_json())
    return channel_title, channel_id


def get_youtube_service(profile: str):
    """Bangun service YouTube API terautentikasi dari token tersimpan,
    refresh otomatis kalau access token kadaluarsa."""
    path = _token_path(profile)
    if not os.path.isfile(path):
        raise RuntimeError(f'Akun "{profile}" belum login OAuth YouTube. Login dulu.')
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    with open(path, encoding='utf-8') as f:
        creds_data = json.load(f)
    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(path, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)


def upload_video(profile: str, video_path: str, metadata: dict, progress_cb=None) -> dict:
    """Upload video + (opsional) thumbnail via YouTube Data API resmi.
    metadata: title, description, tags(str/list), privacy, category_id,
              thumbnail_path, publish_at (ISO 8601 UTC string, opsional),
              made_for_kids (bool).
    Return {'video_id':..., 'url':..., 'thumbnail_set': bool}."""
    from googleapiclient.http import MediaFileUpload

    youtube = get_youtube_service(profile)

    tags = metadata.get('tags') or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]

    body = {
        'snippet': {
            'title': (metadata.get('title') or 'Untitled')[:100],
            'description': metadata.get('description') or '',
            'tags': tags,
            'categoryId': metadata.get('category_id') or '22',
        },
        'status': {
            'privacyStatus': metadata.get('privacy') or 'public',
            'selfDeclaredMadeForKids': bool(metadata.get('made_for_kids', False)),
        },
    }
    publish_at = metadata.get('publish_at')
    if publish_at:
        body['status']['publishAt'] = publish_at
        body['status']['privacyStatus'] = 'private'  # wajib private saat schedule

    media = MediaFileUpload(video_path, chunksize=1024 * 1024 * 5, resumable=True)
    request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

    file_size = os.path.getsize(video_path) or 1
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progress_cb:
            pct = min(int(status.resumable_progress / file_size * 85) + 5, 90)
            try:
                progress_cb(pct, f'Mengupload video via API... {status.resumable_progress/1048576:.1f}/{file_size/1048576:.1f} MB')
            except Exception:
                pass

    video_id = response['id']
    thumbnail_set = False
    thumb_path = metadata.get('thumbnail_path')
    if thumb_path and os.path.isfile(thumb_path):
        if progress_cb:
            try:
                progress_cb(92, 'Mengunggah thumbnail via API...')
            except Exception:
                pass
        thumb_media = MediaFileUpload(thumb_path, resumable=False)
        youtube.thumbnails().set(videoId=video_id, media_body=thumb_media).execute()
        thumbnail_set = True

    if progress_cb:
        try:
            progress_cb(100, 'Selesai (via YouTube API resmi).')
        except Exception:
            pass

    return {
        'video_id': video_id,
        'url': f'https://youtu.be/{video_id}',
        'thumbnail_set': thumbnail_set,
    }
