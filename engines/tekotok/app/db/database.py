import sqlite3
import os
from app.core.path_helper import resource_path, get_writable_path

DB_PATH = get_writable_path("data/app.db")

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL,
        account_type    TEXT DEFAULT 'regular',
        current_session TEXT,
        proxy           TEXT,
        last_upload_at  TIMESTAMP,
        followers_count INTEGER DEFAULT 0,
        total_likes     INTEGER DEFAULT 0,
        total_views     INTEGER DEFAULT 0,
        tiktok_username TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id      INTEGER NOT NULL,
        upload_type     TEXT DEFAULT 'regular',
        video_path      TEXT NOT NULL,
        caption         TEXT,
        hashtags        TEXT,
        product_name    TEXT,
        product_id      TEXT,
        privacy         TEXT DEFAULT 'public',
        schedule_time   TEXT,
        is_invisible    INTEGER DEFAULT 0,
        delete_after    INTEGER DEFAULT 0,
        auto_shutdown   INTEGER DEFAULT 0,
        job_type        TEXT DEFAULT 'upload',
        status          TEXT DEFAULT 'pending',
        progress        INTEGER DEFAULT 0,
        attempts        INTEGER DEFAULT 0,
        error_message   TEXT,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES accounts(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS global_settings (
        key     TEXT PRIMARY KEY,
        value   TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS analytics (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id    INTEGER NOT NULL,
        period_days   INTEGER DEFAULT 28,
        views         TEXT,
        likes         TEXT,
        comments      TEXT,
        shares        TEXT,
        followers     TEXT,
        profile_views TEXT,
        total_videos  TEXT,
        scraped_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES accounts(id)
    )
    """)

    # Migration: tambah kolom baru jika belum ada
    migrations = [
        ("jobs",     "hashtags",      "TEXT"),
        ("jobs",     "product_name",  "TEXT"),
        ("jobs",     "product_id",    "TEXT"),
        ("jobs",     "privacy",       "TEXT DEFAULT 'public'"),
        ("jobs",     "schedule_time", "TEXT"),
        ("jobs",     "is_invisible",  "INTEGER DEFAULT 0"),
        ("jobs",     "delete_after",  "INTEGER DEFAULT 0"),
        ("jobs",     "auto_shutdown", "INTEGER DEFAULT 0"),
        ("jobs",     "job_type",      "TEXT DEFAULT 'upload'"),
        ("jobs",     "progress",      "INTEGER DEFAULT 0"),
        ("jobs",     "attempts",      "INTEGER DEFAULT 0"),
        ("jobs",     "error_message", "TEXT"),
        ("accounts", "proxy",             "TEXT"),
        ("accounts", "last_upload_at",    "TIMESTAMP"),
        ("accounts", "current_session",   "TEXT"),
        ("accounts", "followers_count",   "INTEGER DEFAULT 0"),
        ("accounts", "total_likes",       "INTEGER DEFAULT 0"),
        ("accounts", "total_views",       "INTEGER DEFAULT 0"),
        ("accounts", "tiktok_username",   "TEXT"),
        ("analytics", "period_days",      "INTEGER DEFAULT 28"),
        ("analytics", "profile_views",    "TEXT"),
        ("analytics", "total_videos",     "TEXT"),
        ("jobs",      "batch_id",         "TEXT"),
    ]
    for table, col, col_type in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Kolom sudah ada — aman diabaikan

    # Normalisasi account_type yang mungkin tersimpan dengan kapitalisasi berbeda
    c.execute("UPDATE accounts SET account_type = LOWER(account_type)")
    c.execute("UPDATE jobs SET upload_type = LOWER(upload_type)")

    conn.commit()
    conn.close()

    # Sinkronisasi folder profil yang dicopas manual
    auto_sync_profiles()

    print("[DB] Database initialized.")


def auto_sync_profiles():
    """
    Sinkronisasi folder data/profiles dengan database:
    1. Daftarkan folder baru yang ada di disk tapi belum ada di DB.
    2. FIX: Akun yang foldernya tidak ada di disk TIDAK langsung dihapus.
       current_session di-set NULL sebagai penanda, data job tetap aman.
       Ini mencegah data loss jika user pindah folder EXE atau drive.
    """
    profiles_dir = get_writable_path(os.path.join("data", "profiles"))
    if not os.path.exists(profiles_dir):
        os.makedirs(profiles_dir, exist_ok=True)
        return

    conn = get_db()
    c = conn.cursor()

    try:
        # Ambil semua folder yang ada di data/profiles
        found_folders = {
            f for f in os.listdir(profiles_dir)
            if os.path.isdir(os.path.join(profiles_dir, f))
        }

        # Ambil semua data akun dari DB
        c.execute("SELECT id, username FROM accounts")
        accounts_in_db = c.fetchall()
        registered_usernames = {row['username'] for row in accounts_in_db}

        changes_made = False

        # 1. FIX: Jangan hapus akun — tandai current_session=NULL jika folder tidak ada.
        #    Data job tetap tersimpan, akun tetap ada di list, hanya sesi yang invalid.
        flagged_count = 0
        for row in accounts_in_db:
            if row['username'] not in found_folders:
                c.execute(
                    "UPDATE accounts SET current_session=NULL WHERE id=? AND current_session IS NOT NULL",
                    (row['id'],)
                )
                if c.rowcount > 0:
                    flagged_count += 1
                    changes_made = True

        # 2. Tambah akun ke DB jika ada folder baru yang belum terdaftar
        new_count = 0
        for folder_name in found_folders:
            if folder_name not in registered_usernames:
                session_path = os.path.join(profiles_dir, folder_name)
                c.execute("""
                    INSERT INTO accounts (username, current_session, account_type)
                    VALUES (?, ?, ?)
                """, (folder_name, session_path, "regular"))
                new_count += 1
                changes_made = True

        if changes_made:
            conn.commit()
            if flagged_count > 0:
                print(f"[DB] Auto-sync: {flagged_count} akun ditandai (folder tidak ditemukan, sesi di-reset).")
            if new_count > 0:
                print(f"[DB] Auto-sync: Menambahkan {new_count} akun baru dari folder.")

    except Exception as e:
        print(f"[DB] Auto-sync error: {e}")
    finally:
        # FIX: conn.close() hanya di sini — tidak ada penutupan ganda
        try:
            conn.close()
        except Exception:
            pass


def reindex_accounts():
    """
    Mengurutkan ulang ID akun agar selalu berurutan (1, 2, 3...).
    FIX: Dibatalkan jika ada job yang sedang berjalan (status='processing')
    untuk mencegah account_id berubah di tengah eksekusi worker.
    """
    conn = get_db()
    c = conn.cursor()
    try:
        # GUARD: Jangan reindex kalau ada job yang sedang diproses worker
        c.execute("SELECT COUNT(*) FROM jobs WHERE status='processing'")
        active_count = c.fetchone()[0]
        if active_count > 0:
            print(f"[DB] Reindex dibatalkan: ada {active_count} job yang sedang berjalan.")
            return

        # Ambil semua akun urut berdasarkan ID lama
        c.execute("SELECT id FROM accounts ORDER BY id ASC")
        rows = c.fetchall()

        if not rows:
            print("[DB] Reindex: tidak ada akun.")
            return

        # Pakai ID sementara sangat besar agar tidak konflik saat proses update
        temp_offset = 1_000_000

        # Tahap A: Geser ke temporary ID
        for i, row in enumerate(rows):
            old_id = row['id']
            new_id_temp = (i + 1) + temp_offset
            if old_id != new_id_temp:
                c.execute("UPDATE accounts SET id=? WHERE id=?", (new_id_temp, old_id))
                c.execute("UPDATE jobs SET account_id=? WHERE account_id=?", (new_id_temp, old_id))
                c.execute("UPDATE analytics SET account_id=? WHERE account_id=?", (new_id_temp, old_id))

        # Tahap B: Kembalikan dari temporary ID ke ID final (1, 2, 3...)
        for i, _ in enumerate(rows):
            new_id_temp = (i + 1) + temp_offset
            real_new_id = i + 1
            c.execute("UPDATE accounts SET id=? WHERE id=?", (real_new_id, new_id_temp))
            c.execute("UPDATE jobs SET account_id=? WHERE account_id=?", (real_new_id, new_id_temp))
            c.execute("UPDATE analytics SET account_id=? WHERE account_id=?", (real_new_id, new_id_temp))

        # Reset sqlite_sequence — INSERT IGNORE jika baris belum ada
        c.execute("""
            INSERT INTO sqlite_sequence (name, seq) VALUES ('accounts', ?)
            ON CONFLICT(name) DO UPDATE SET seq=excluded.seq
        """, (len(rows),))

        conn.commit()
        print(f"[DB] Reindex selesai. Total akun: {len(rows)}")

    except Exception as e:
        print(f"[DB] Reindex error: {e}")
    finally:
        conn.close()
