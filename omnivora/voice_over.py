"""
omnivora/voice_over.py — Otomasi Voice Over via Gemini Canvas untuk tab
Repurpose > Voice Over.

Share link GAMA (VO_SHARE_URL) berisi Canvas app kustom "PROMPTIFY VOICE
OVER" (bukan chat biasa) - sebuah mini web-app yang dirender Gemini di
DALAM iframe terpisah (domain usercontent.goog), dengan form sendiri:
    #text-input       - textarea naskah
    #language-select  - <select> bahasa (value = kode locale, mis. id-ID)
    #voice-select     - <select> aktor suara (value = nama voice, mis. Puck)
    #style-select     - <select> gaya bicara (value = slug, mis. semangat)
    #generate-btn     - mulai generate (app pecah naskah jadi beberapa
                        "bagian"/part kalau panjang, generate audio tiap
                        bagian satu-satu)
    #merge-section / #merge-btn - muncul setelah SEMUA bagian selesai;
                        klik utk gabung semua bagian jadi satu file WAV
    #final-audio-result - div (class "hidden" sampai merge selesai)
    #final-download-link - <a href="blob:..." download="....wav"> hasil akhir

Alur otomasi: buka share link -> fork ke sesi live ("Try Gemini Canvas")
-> tunggu auto-continue selesai -> cari iframe Canvas app -> isi form ->
klik generate -> tunggu bagian selesai (merge-section muncul) -> klik
merge -> tunggu final-audio-result muncul -> klik final-download-link
(attribute "download" memicu Chrome save-as beneran walau href-nya blob
URL) -> deteksi file baru di folder staging (disk-diff, sama pola dengan
GoogleVideoGenerator di video_generator.py) -> pindah ke folder tujuan.
"""
import os
import time
import shutil
from pathlib import Path
from datetime import datetime

from omnivora.environment import runtime_data_root
from omnivora.video_generator import GoogleAccountSession, sanitize_email

VO_SHARE_URL = "https://gemini.google.com/share/e51f41ebc6ee"

_AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".m4a", ".webm", ".flac")


class GeminiVoiceOverGenerator:
    def __init__(self, email, user_agent=None, log_cb=None, hidden=False):
        self.email = email
        self.log_cb = log_cb or (lambda msg, lvl="info": None)
        self.session = GoogleAccountSession(email, user_agent=user_agent, log_cb=self.log_cb, hidden=hidden)
        self.last_output_path = None

    def log(self, msg, lvl="info"):
        self.log_cb(msg, lvl)

    def _snapshot_dir(self, d):
        try:
            return {f.name for f in Path(d).glob("*")}
        except Exception:
            return set()

    def _wait_new_download_file(self, d, before, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            now = self._snapshot_dir(d)
            new = {n for n in now - before if not n.endswith((".crdownload", ".tmp"))}
            new = {n for n in new if n.lower().endswith(_AUDIO_EXTS)}
            if new:
                time.sleep(2)
                return list(new)
            time.sleep(2)
        return []

    def _unique_output_path(self, out_dir, filename):
        base = os.path.basename(str(filename or "")).strip() or "voice_over.wav"
        suffix = Path(base).suffix or ".wav"
        stem = Path(base).stem or "voice_over"
        tag = f"{sanitize_email(self.email)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        candidate = out_dir / f"{stem}_{tag}{suffix}"
        idx = 1
        while candidate.exists():
            candidate = out_dir / f"{stem}_{tag}_{idx}{suffix}"
            idx += 1
        return candidate

    def _find_by_text(self, page, labels, timeout=2):
        """Cari elemen berdasarkan beberapa kemungkinan teks (ID/EN) - akun
        Google beda-beda preferensi bahasa Gemini-nya, jadi label tombol bisa
        'Try Gemini Canvas'/'Continue' (EN) atau 'Coba Gemini Canvas'/
        'Lanjutkan' (ID) tergantung akun yang login."""
        for lbl in labels:
            el = page.ele(f"text:{lbl}", timeout=timeout)
            if el:
                return el
        return None

    def _dismiss_app_warning(self, page):
        """Dialog peringatan 'Aplikasi ini dibuat oleh orang lain'/'This app
        was created by someone else' yang muncul utk Canvas app pihak ketiga -
        klik Lanjutkan/Continue (BUKAN 'Coba Gemini'/'Try Gemini' yang malah
        pindah ke Gemini biasa tanpa Canvas)."""
        for sel in ('css:button[aria-label="Continue"]', 'css:button[aria-label="Lanjutkan"]'):
            btn = page.ele(sel, timeout=1.5)
            if btn:
                btn.click()
                time.sleep(1.5)
                return True
        btn = self._find_by_text(page, ("Lanjutkan", "Continue"), timeout=1.5)
        if btn:
            btn.click()
            time.sleep(1.5)
            return True
        return False

    def _dismiss_signin_banner(self, page):
        """Banner 'You need to sign in with your Google Account to see some
        features' (Google Identity Services) kadang muncul di pojok - bukan
        berarti akun belum login, dan form Canvas tetap berfungsi walau
        banner ini ada. Best-effort tutup saja (klik X) supaya tidak
        menghalangi area klik; TIDAK fatal kalau tidak ketemu/gagal ditutup."""
        try:
            for lbl in ("Close", "Tutup", "Dismiss"):
                btn = page.ele(f'css:button[aria-label="{lbl}"]', timeout=1)
                if btn:
                    btn.click()
                    return True
        except Exception:
            pass
        return False

    def _find_canvas_frame(self, page, timeout=20):
        """Canvas app dirender di iframe terpisah (usercontent.goog) - cari
        iframe yang isinya benar-benar punya form #text-input."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in page.eles("tag:iframe", timeout=1):
                try:
                    fr = page.get_frame(f)
                    if fr.ele("#text-input", timeout=0.5):
                        return fr
                except Exception:
                    continue
            time.sleep(1)
        return None

    def generate(self, script, voice, bahasa, gaya, out_dir, status_cb=None):
        status = status_cb or (lambda m: None)
        page = None
        try:
            status("Membuka browser & login akun...")
            self.session.launch_browser(url=VO_SHARE_URL, headful=True)
            page = self.session.get_page()
            time.sleep(3)

            status("Membuka Gemini Canvas...")
            # Dialog "Aplikasi ini dibuat oleh orang lain" bisa muncul di sini
            # (di halaman share, SEBELUM sempat klik apa pun) - akun dgn
            # preferensi bahasa Indonesia lihat ini duluan.
            self._dismiss_app_warning(page)

            # Akun yang PERNAH klik "Try/Coba Gemini Canvas" di link ini
            # sebelumnya langsung diarahkan Gemini ke form Canvas-nya sendiri
            # tanpa perlu klik apa pun lagi - jadi cek dulu apakah form SUDAH
            # ada sebelum asumsi harus klik tombolnya (yang cuma perlu utk
            # akun yang baru pertama kali membuka link share ini).
            frame = self._find_canvas_frame(page, timeout=8)
            if not frame:
                tryit = self._find_by_text(page, ("Try Gemini Canvas", "Coba Gemini Canvas"), timeout=8)
                if not tryit:
                    raise RuntimeError("Tombol 'Try/Coba Gemini Canvas' tidak ditemukan di halaman share.")
                tryit.click()
                time.sleep(3)

                # Dialog disclaimer "lanjutkan sebagai akun ini" bisa juga
                # muncul SETELAH klik (bukan sebelum) - cek lagi di sini.
                self._dismiss_app_warning(page)

                # Tunggu auto-lanjut percakapan contoh selesai.
                for _ in range(45):
                    if not page.ele('css:button[aria-label="Stop response"]', timeout=1):
                        break
                    time.sleep(2)

                status("Menunggu form Voice Over termuat...")
                frame = self._find_canvas_frame(page, timeout=30)
            if not frame:
                raise RuntimeError("Form Canvas Voice Over (PROMPTIFY VOICE OVER) tidak ditemukan.")

            # Best-effort saja - banner sign-in (kalau muncul) tidak menghalangi
            # form Canvas tetap dipakai, jadi kegagalan di sini tidak fatal.
            self._dismiss_signin_banner(page)

            status("Mengisi naskah & pengaturan suara...")
            text_input = frame.ele("#text-input", timeout=5)
            text_input.click()
            try:
                frame.run_js("arguments[0].value = '';", text_input)
            except Exception:
                pass
            text_input.input(script)
            time.sleep(0.3)

            lang_sel = frame.ele("#language-select", timeout=3)
            if lang_sel:
                try:
                    lang_sel.select.by_value(bahasa)
                except Exception:
                    self.log(f"Gagal set bahasa '{bahasa}', pakai default form.", "warning")

            voice_sel = frame.ele("#voice-select", timeout=3)
            if voice_sel:
                try:
                    voice_sel.select.by_value(voice)
                except Exception:
                    self.log(f"Suara '{voice}' tidak ada di form Canvas ini, pakai default form.", "warning")

            style_sel = frame.ele("#style-select", timeout=3)
            if style_sel:
                try:
                    style_sel.select.by_value(gaya)
                except Exception:
                    self.log(f"Gaya bicara '{gaya}' tidak ada di form Canvas ini, pakai default form.", "warning")

            status("Klik generate...")
            gen_btn = frame.ele("#generate-btn", timeout=5)
            if not gen_btn:
                raise RuntimeError("Tombol generate (#generate-btn) tidak ditemukan.")
            gen_btn.click()

            # Tunggu semua "bagian" naskah selesai di-generate.
            #
            # SALAH SEBELUMNYA: sempat mengecek atribut/properti "disabled"
            # pada tombol #merge-btn itu sendiri - ternyata tombolnya TIDAK
            # PERNAH ditandai disabled sejak awal (defaultnya HTML biasa,
            # tidak disabled). Yang disembunyikan/ditampilkan itu KONTAINER-
            # nya (#merge-section, box hijau "Semua Bagian Selesai!") lewat
            # class "hidden" - persis pola yang sudah benar dipakai utk
            # #final-audio-result di bawah. Jadi cek disabled selalu "siap"
            # dalam hitungan detik (jauh sebelum semua bagian benar2 selesai),
            # lalu klik gagal karena section masih hidden/belum dirender.
            status("Menunggu Gemini membuat audio tiap bagian naskah...")
            ready = False
            deadline = time.time() + 600  # naskah panjang bisa banyak bagian
            while time.time() < deadline:
                merge_section = frame.ele("#merge-section", timeout=2)
                if merge_section and "hidden" not in (merge_section.attr("class") or ""):
                    ready = True
                    break
                time.sleep(3)
            if not ready:
                raise RuntimeError("Timeout menunggu semua bagian audio selesai dibuat.")

            status("Menggabungkan semua bagian jadi satu file...")
            staging_dir = runtime_data_root() / "output" / "_downloads" / sanitize_email(self.email)
            staging_dir.mkdir(parents=True, exist_ok=True)
            page.set.download_path(str(staging_dir))
            before_files = self._snapshot_dir(staging_dir)

            # Ambil elemen SEGAR tepat sebelum klik (bukan reference dari loop
            # di atas) - app ini bisa re-render DOM-nya, jadi reference lama
            # bisa "basi" (NoRectError: tidak punya lokasi/ukuran lagi).
            clicked = False
            for _ in range(5):
                merge_btn = frame.ele("#merge-btn", timeout=2)
                if not merge_btn:
                    break
                try:
                    merge_btn.click()
                    clicked = True
                    break
                except Exception:
                    time.sleep(1)
            if not clicked:
                raise RuntimeError("Tombol gabung (#merge-btn) tidak bisa diklik.")

            ready = False
            deadline = time.time() + 60
            while time.time() < deadline:
                result_div = frame.ele("#final-audio-result", timeout=2)
                if result_div and "hidden" not in (result_div.attr("class") or ""):
                    ready = True
                    break
                time.sleep(2)
            if not ready:
                raise RuntimeError("Hasil akhir (final-audio-result) tidak kunjung muncul.")

            status("Mengunduh audio...")
            clicked = False
            for _ in range(5):
                final_link = frame.ele("#final-download-link", timeout=2)
                if not final_link:
                    break
                try:
                    final_link.click()
                    clicked = True
                    break
                except Exception:
                    time.sleep(1)
            if not clicked:
                raise RuntimeError("Link download (#final-download-link) tidak bisa diklik.")

            new_files = self._wait_new_download_file(staging_dir, before_files, 60)
            if not new_files:
                raise RuntimeError("File audio tidak kunjung terunduh (timeout).")

            src_path = staging_dir / new_files[0]
            final_path = self._unique_output_path(out_dir, new_files[0])
            shutil.move(str(src_path), str(final_path))
            self.last_output_path = str(final_path)
            status(f"Selesai: {final_path.name}")
            return True
        finally:
            try:
                self.session.close_browser(page)
            except Exception:
                pass
