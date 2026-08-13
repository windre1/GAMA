import time
import os
import json
import random
import subprocess
from app.core.path_helper import get_writable_path
from app.core.browser_helper import get_browser_exe, get_browser_label
from DrissionPage import ChromiumPage, ChromiumOptions

TIKTOK_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"

# ── Chrome helper ─────────────────────────────────────────────────────────────

def _launch_chrome(profile_path: str, port: int, headless: bool = False) -> bool:
    chrome = get_browser_exe()
    if not chrome:
        print("[ERROR] Browser tidak ditemukan. Isi folder browser portable atau install Chrome/Edge.")
        return False
    print(f"[BROWSER] Menggunakan {get_browser_label(chrome)}: {chrome}")
    profile_path = get_writable_path(profile_path)
    os.makedirs(profile_path, exist_ok=True)

    # Flag berbeda untuk headless vs normal
    # Ukuran window Chrome yang fixed — konsisten di semua perangkat
    # 1280x800 cukup untuk TikTok Studio dan muat di laptop layar kecil
    CHROME_W = 1280
    CHROME_H = 800
    CHROME_X = 0   # posisi kiri layar
    CHROME_Y = 0   # posisi atas layar

    if headless:
        extra = (
            "--headless=new "
            f"--window-size={CHROME_W},{CHROME_H} "
            "--disable-gpu "
            "--disable-software-rasterizer "
            "--disable-dev-shm-usage "
            "--no-sandbox "
            "--remote-allow-origins=* "
        )
    else:
        extra = (
            f"--window-size={CHROME_W},{CHROME_H} "
            f"--window-position={CHROME_X},{CHROME_Y} "
            "--force-device-scale-factor=1 "
        )

    cmd = (
        f'"{chrome}" '
        f'--remote-debugging-port={port} '
        f'--user-data-dir="{profile_path}" '
        f'--no-first-run '
        f'--disable-notifications '
        f'--disable-popup-blocking '
        f'{extra}'
    )
    print(f"[CHROME] Launch: headless={headless}, port={port}")
    subprocess.Popen(cmd, shell=True)

    # Tunggu Chrome siap — cek endpoint /json/version (lebih reliable dari /json)
    print(f"[CHROME] Menunggu Chrome siap di port {port}...")
    import urllib.request, urllib.error
    for i in range(20):
        time.sleep(1)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2)
            print(f"[CHROME] Chrome siap di port {port} (detik ke-{i+1}).")
            return True
        except Exception:
            continue
    print(f"[ERROR] Chrome tidak merespons di port {port} setelah 20 detik.")
    return False


def _is_chrome_running(port: int) -> bool:
    """Cek apakah Chrome sudah running di port ini."""
    import urllib.request
    for endpoint in ["/json/version", "/json"]:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}{endpoint}", timeout=1)
            return True
        except Exception:
            continue
    return False


def _kill_chrome_on_port(port: int):
    """Kill Chrome process yang pakai port debugging ini (Windows)."""
    try:
        import subprocess
        # Cari PID yang pakai port ini
        result = subprocess.run(
            f'netstat -ano | findstr ":{port}"',
            shell=True, capture_output=True, text=True)
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and f":{port}" in parts[1]:
                pid = parts[-1]
                subprocess.run(f"taskkill /F /PID {pid}", shell=True,
                                capture_output=True)
                print(f"[CHROME] Kill Chrome PID {pid} di port {port}")
        time.sleep(1)
    except Exception as e:
        print(f"[CHROME] Gagal kill Chrome: {e}")


def _connect(profile_path: str, port: int, headless: bool) -> ChromiumPage:
    profile_path = os.path.abspath(profile_path)
    os.makedirs(profile_path, exist_ok=True)

    if headless:
        # ── Mode invisible: biarkan DrissionPage launch Chrome sendiri ────────
        # Ini JAUH lebih reliable daripada manual launch + attach
        if _is_chrome_running(port):
            print(f"[CHROME] Kill Chrome lama di port {port} untuk headless...")
            _kill_chrome_on_port(port)
            time.sleep(2)

        chrome = get_browser_exe()
        if not chrome:
            print("[ERROR] Browser tidak ditemukan. Isi folder browser portable atau install Chrome/Edge.")
            return None

        print(f"[CHROME] Launch headless via DrissionPage options...")
        co = ChromiumOptions()
        co.set_local_port(port)
        co.set_browser_path(chrome)
        co.set_user_data_path(profile_path)
        co.headless(True)
        co.set_argument("--window-size=1920,1080")
        co.set_argument("--disable-gpu")
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--no-first-run")
        co.set_argument("--disable-notifications")

        try:
            page = ChromiumPage(co)
            print(f"[CHROME] Headless browser siap.")
            return page
        except Exception as e:
            print(f"[ERROR] Gagal launch headless: {e}")
            return None

    else:
        # ── Mode normal: launch manual + attach (sudah terbukti jalan) ────────
        if _is_chrome_running(port):
            print(f"[CHROME] Reuse Chrome yang sudah berjalan di port {port}.")
        else:
            if not _launch_chrome(profile_path, port, headless=False):
                return None
            time.sleep(2)

        co = ChromiumOptions()
        co.set_local_port(port)
        browser_path = get_browser_exe()
        if browser_path:
            co.set_browser_path(browser_path)
        try:
            page = ChromiumPage(co)
            print(f"[CHROME] Browser normal siap di port {port}.")
            return page
        except Exception as e:
            print(f"[ERROR] Gagal connect browser normal: {e}")
            return None


# ── Step helpers ──────────────────────────────────────────────────────────────

def _wait_upload_complete(page, timeout: int = 120) -> bool:
    """Tunggu sampai progress upload di TikTok Studio selesai (tidak ada progress bar)."""
    print("[WAIT] Menunggu video selesai diproses TikTok...")
    start = time.time()
    while time.time() - start < timeout:
        # Cek apakah progress bar / loading sudah hilang
        # dan caption box sudah bisa diklik
        try:
            # Jika caption box sudah muncul = upload selesai
            cap = page.ele('@contenteditable=true', timeout=2)
            if cap:
                # Pastikan bukan loading state
                loading = page.ele('.upload-progress', timeout=1)
                if not loading:
                    print("[WAIT] Video selesai diproses.")
                    return True
        except Exception:
            pass

        # Cek popup saat sedang menunggu (misal: "Turn on automatic content checks?")
        _dismiss_popups(page)

        time.sleep(3)
    print("[WARN] Timeout menunggu video selesai, lanjut...")
    return False


def _dismiss_popups(page, allow_cancel: bool = True):
    """Tutup popup/dialog yang mungkin muncul setelah video diupload."""
    print("[STEP] Cek dan tutup popup yang muncul...")
    closed = 0
    # Selector popup yang umum muncul di TikTok Studio
    popup_close_sels = [
        # 1. Tombol X / Close (paling umum)
        'xpath://button[@aria-label="Close"]',
        'xpath://button[@aria-label="close"]',
        'xpath://button[contains(@class,"close")]',
        'xpath://div[contains(@class,"modal")]//button[contains(@class,"close")]',
        '@data-e2e=modal-close-inner-button',
        '@data-e2e=close-btn',

        # 2. Tombol konfirmasi positif (Got it, OK, Allow)
        'xpath://button[normalize-space()="Got it"]',
        'xpath://button[normalize-space()="OK"]',
        'xpath://button[normalize-space()="Dismiss"]',
        'xpath://button[normalize-space()="Allow"]',
        'xpath://button[normalize-space()="Izinkan"]',
        'xpath://button[contains(text(),"Allow")]',
        'xpath://button[contains(text(),"Izinkan")]',
        '@data-e2e=modal-allow-button',
        '@data-e2e=accept-btn',

        # 3. Handle popup "Sure you want to cancel your upload?" -> Klik NO
        'xpath://div[contains(.,"Sure you want to cancel")]//button[normalize-space()="No"]',
        'xpath://div[contains(.,"Yakin ingin membatalkan")]//button[normalize-space()="Tidak"]',
        'xpath://div[contains(@class,"modal")]//button[normalize-space()="No"]',

        # 4. Tombol Cancel pada popup tertentu (misal: auto content checks).
        # Jangan dipakai di flow Schedule karena bisa membatalkan pemilihan radio
        # schedule dan membuat field tanggal/jam tidak muncul.
        'xpath://div[contains(text(),"automatic content checks")]/following-sibling::div//button[contains(normalize-space(),"Cancel")]',
    ]
    if allow_cancel:
        popup_close_sels.extend([
            'xpath://div[contains(@class,"modal")]//button[normalize-space()="Cancel"]',
            'xpath://div[contains(@class,"modal")]//button[normalize-space()="Batal"]',
            'xpath://div[contains(@class,"modal")]//button[normalize-space()="Not now"]',
        ])
    for sel in popup_close_sels:
        try:
            btn = page.ele(sel, timeout=1)
            if btn:
                btn.click()
                closed += 1
                print(f"[STEP] Popup ditutup via: {sel}")
                time.sleep(0.5)
        except Exception:
            pass

    if closed > 0:
        print(f"[STEP] {closed} popup berhasil ditutup.")
    else:
        print("[STEP] Tidak ada popup yang perlu ditutup.")


def _close_product_modal(page):
    """Tutup modal Add link/Add product yang tersisa agar flow schedule tidak terhalang."""
    try:
        closed = page.run_js("""
            function visible(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                var st = window.getComputedStyle(el);
                return r.width > 20 && r.height > 20 && st.display !== 'none' &&
                    st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.05;
            }
            var modals = Array.from(document.querySelectorAll(
                '[class*=modal],[class*=Modal],[role=dialog]'
            )).filter(visible);
            for (var modal of modals.reverse()) {
                var txt = (modal.innerText || modal.textContent || '').toLowerCase();
                if (!(txt.includes('add link') || txt.includes('add product') ||
                      txt.includes('product links') || txt.includes('produk'))) {
                    continue;
                }
                var buttons = Array.from(modal.querySelectorAll('button')).filter(visible);
                var btn = buttons.find(function(b) {
                    var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    var a = (b.getAttribute('aria-label') || '').trim().toLowerCase();
                    return t === 'cancel' || t === 'batal' || a === 'close' || a === 'tutup' ||
                        t === 'close' || t === 'tutup';
                }) || buttons[0];
                if (btn) {
                    btn.click();
                    return true;
                }
            }
            return false;
        """)
        if closed:
            print("[PRODUCT] Modal produk ditutup sebelum lanjut schedule.")
            time.sleep(1)
            return True
    except Exception as e:
        print(f"[PRODUCT] Gagal menutup modal produk: {e}")
    return False


def _clear_draftjs(page, cap_box):
    """Clear Draft.js contenteditable dengan CDP keyEvent — satu-satunya cara yang reliable."""

    # Fokus dulu ke elemen
    cap_box.click()
    time.sleep(0.5)

    # Gunakan CDP Input.dispatchKeyEvent untuk Ctrl+A lalu Backspace
    # Draft.js hanya merespons keyEvent yang melalui CDP, bukan JS biasa
    try:
        # Ctrl+A — select all
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyDown",
            "modifiers": 2,   # Ctrl
            "key": "a",
            "code": "KeyA",
            "windowsVirtualKeyCode": 65
        })
        time.sleep(0.2)
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyUp",
            "modifiers": 2,
            "key": "a",
            "code": "KeyA",
            "windowsVirtualKeyCode": 65
        })
        time.sleep(0.2)

        # Backspace — hapus semua yang terseleksi
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyDown",
            "key": "Backspace",
            "code": "Backspace",
            "windowsVirtualKeyCode": 8
        })
        time.sleep(0.2)
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyUp",
            "key": "Backspace",
            "code": "Backspace",
            "windowsVirtualKeyCode": 8
        })
        time.sleep(0.3)
        print("[CLEAR] Draft.js cleared via CDP Ctrl+A + Backspace.")
        return
    except Exception as e:
        print(f"[CLEAR] CDP keyEvent gagal: {e}, coba fallback...")

    # Fallback: Actions API jika CDP tidak tersedia
    try:
        actions = page.actions
        actions.key_down("ctrl").key_down("a").key_up("a").key_up("ctrl")
        time.sleep(0.2)
        actions.key_down("Backspace").key_up("Backspace")
        time.sleep(0.2)
        print("[CLEAR] Draft.js cleared via Actions API.")
        return
    except Exception as e2:
        print(f"[CLEAR] Actions API gagal: {e2}")

    # Fallback terakhir: JS execCommand
    try:
        page.run_js(
            "arguments[0].focus(); "
            "document.execCommand('selectAll', false, null); "
            "document.execCommand('delete', false, null);",
            cap_box)
        time.sleep(0.3)
        print("[CLEAR] Draft.js cleared via execCommand fallback.")
    except Exception as e3:
        print(f"[CLEAR] execCommand gagal: {e3}")


# Field lokasi TikTok Studio dibungkus komponen "poi-search" (POI = point of
# interest). Jangkar ini yang paling stabil: placeholder input-nya KOSONG, jadi
# mencari lewat placeholder atau lewat //input[@type="text"] gampang nyasar ke
# kotak teks lain di halaman yang sama.
_LOC_INPUT = 'div.poi-search input.Select__searchInput'

# Hasil pencarian lokasi berada di div.Select__content, satu anak = satu baris.
# CATATAN: div[contains(@class,"suggestion")] TIDAK boleh dipakai — itu cuma
# SATU div pembungkus seluruh daftar. Mengkliknya berarti mengklik titik tengah
# pembungkus, yang mendarat di baris mana pun yang kebetulan ada di tengah;
# inilah penyebab lokasi sering meleset.
_LOC_DROPDOWN = 'div.Select__content'


def _loc_buka_dropdown(page) -> bool:
    """Klik trigger Select lokasi supaya panel hasilnya muncul.

    WAJIB dan tidak bisa diganti focus(): di halaman edit yang baru, komponen
    Select masih tertutup dan div.Select__content BELUM ADA di DOM. Hanya klik
    sungguhan yang membukanya — memberi focus saja membuat teks masuk ke kotak
    tapi dropdown tidak pernah muncul.
    """
    pos = page.run_js(
        "var i=document.querySelector(%s);"
        "if(!i)return null;"
        "var t=i.closest('button')||i.parentElement;"
        "t.scrollIntoView({block:'center'});"
        "var r=t.getBoundingClientRect();"
        "return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};"
        % json.dumps(_LOC_INPUT))
    if not pos:
        return False
    for tipe in ("mousePressed", "mouseReleased"):
        page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                     x=pos['x'], y=pos['y'], button="left", clickCount=1)
    time.sleep(1.2)
    return True


def _loc_isi_kotak(page, teks: str) -> bool:
    """Buka dropdown, kosongkan kotak lokasi, lalu isi teksnya.

    Dikosongkan lewat native setter + event 'input' karena komponennya React:
    menulis .value langsung tidak terbaca, dan .input() bawaan malah MENAMBAH
    di belakang teks lama (terbukti saat pengujian: isinya jadi dobel).
    """
    if not _loc_buka_dropdown(page):
        return False

    kosong = page.run_js(
        "var i=document.querySelector(%s);"
        "if(!i)return false;"
        "i.focus();"
        "var s=Object.getOwnPropertyDescriptor("
        "  window.HTMLInputElement.prototype,'value').set;"
        "s.call(i,'');"
        "i.dispatchEvent(new Event('input',{bubbles:true}));"
        "return true;" % json.dumps(_LOC_INPUT))
    if not kosong:
        return False
    time.sleep(1.0)
    # insertText meniru ketikan sungguhan; React menerimanya seperti user.
    page.run_cdp("Input.insertText", text=teks)
    return True


def _loc_baris_dropdown(page):
    """Daftar teks tiap baris dropdown, berurutan dari atas."""
    try:
        return page.run_js(
            "var k=document.querySelector(%s);"
            "if(!k)return [];"
            "var o=[];"
            "for(var i=0;i<k.children.length;i++){"
            "  o.push((k.children[i].innerText||'').replace(/\\n/g,' ').trim());"
            "}"
            "return o;" % json.dumps(_LOC_DROPDOWN)) or []
    except Exception:
        return []


def _do_location(page, location: str) -> bool:
    """Isi lokasi di TikTok Studio (hanya mode regular).

    Ketik PERSIS teks lokasi dari CSV, lalu pilih BARIS PALING ATAS dropdown.

    Kenapa teks penuh dan baris pertama: nilai location di CSV memang disalin
    dari dropdown TikTok sendiri (format "Nama Tempat - Alamat lengkap"), jadi
    mengetiknya utuh membuat TikTok menaruh tempat yang persis sama di urutan
    teratas. Sudah diuji langsung ke TikTok Studio: baik teks penuh maupun teks
    yang dipotong di koma pertama sama-sama menaruh lokasi yang benar di baris
    0 — jadi memotong teks tidak ada untungnya, sementara memilih baris selain
    yang teratas justru membuat hasilnya meleset.

    Return True jika berhasil, False jika gagal (non-fatal, upload tetap lanjut).
    """
    import re
    if not location or not location.strip():
        return False
    loc = location.strip()
    print(f"[LOCATION] Mengisi lokasi (persis dari CSV): {loc}")

    try:
        ada = page.run_js("return !!document.querySelector(%s);" % json.dumps(_LOC_INPUT))
        if not ada:
            print("[LOCATION] Field lokasi tidak ditemukan - skip.")
            return False

        # Kata kunci dipakai HANYA untuk memastikan dropdown sudah ter-refresh
        # untuk query ini (bukan sisa daftar "lokasi baru-baru ini"). Pemilihan
        # barisnya tetap selalu baris teratas.
        kata = [w.lower() for w in re.findall(r'\w+', loc.split(',')[0]) if len(w) > 2]
        # Minimal DUA kata harus cocok. Satu kata saja terlalu longgar: hampir
        # semua lokasi Indonesia mengandung kata "Indonesia", jadi daftar basi
        # pun bisa lolos dan post tertandai di tempat yang salah.
        minimal = min(2, len(kata)) if kata else 0

        if not _loc_isi_kotak(page, loc):
            print("[LOCATION] Gagal mengisi kotak lokasi - skip.")
            return False

        def _cocok(teks: str) -> int:
            rendah = teks.lower()
            return sum(1 for w in kata if w in rendah)

        # Tunggu dropdown benar-benar berisi hasil untuk query INI.
        baris = []
        segar = False
        for _ in range(30):                     # maksimal ~15 detik
            time.sleep(0.5)
            baris = _loc_baris_dropdown(page)
            if baris and (not minimal or _cocok(baris[0]) >= minimal):
                segar = True
                break

        if not baris:
            print("[LOCATION] Dropdown tidak memunculkan hasil - skip.")
            return False
        if not segar:
            # Sengaja TIDAK diklik. Sebelum ada penjagaan ini, baris teratas
            # tetap diklik walau daftarnya masih berisi "lokasi baru-baru ini"
            # milik akun — hasilnya post tertandai di tempat acak yang sama
            # sekali tidak ada di CSV (pernah terjadi: "Warga +62 Coffee Shop").
            # Lebih baik post tanpa lokasi daripada salah lokasi.
            print(f"[LOCATION] GAGAL: daftar belum ter-refresh untuk lokasi CSV ini. "
                  f"Hasil teratas '{baris[0][:60]}' tidak nyambung - "
                  f"lokasi SENGAJA dikosongkan daripada salah tandai.")
            return False

        print(f"[LOCATION] {len(baris)} hasil muncul, memilih BARIS TERATAS:")
        for i, b in enumerate(baris[:3]):
            print(f"[LOCATION]   {'>' if i == 0 else ' '} {i}. {b[:90]}")

        # Klik anak pertama tepat di tengahnya sendiri (bukan tengah pembungkus).
        hasil = page.run_js(
            "var k=document.querySelector(%s);"
            "if(!k||!k.children.length)return null;"
            "var e=k.children[0];"
            "e.scrollIntoView({block:'center'});"
            "var b=e.getBoundingClientRect();"
            "return {x:Math.round(b.x+b.width/2),y:Math.round(b.y+b.height/2),"
            "        teks:(e.innerText||'').replace(/\\n/g,' ').trim()};"
            % json.dumps(_LOC_DROPDOWN))
        if not hasil:
            print("[LOCATION] Baris teratas hilang saat hendak diklik - skip.")
            return False

        for tipe in ("mousePressed", "mouseReleased"):
            page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                         x=hasil['x'], y=hasil['y'], button="left", clickCount=1)
        time.sleep(1.5)

        # Pastikan benar-benar terpilih, bukan sekadar diklik.
        terpilih = page.run_js(
            "var i=document.querySelector(%s);return i?i.value:'';" % json.dumps(_LOC_INPUT)) or ''
        if terpilih.strip():
            print(f"[LOCATION] Terpilih: {terpilih[:90]}")
            return True

        print(f"[LOCATION] WARNING: kotak lokasi kosong setelah diklik "
              f"(target: '{hasil['teks'][:60]}'). Lokasi mungkin tidak tersimpan.")
        return False
    except Exception as e:
        print(f"[LOCATION] Error saat mengisi lokasi: {e}")
        return False


def _klik_tombol_terlihat(page, teks: str) -> bool:
    """Klik tombol yang teksnya PERSIS `teks` dan benar-benar terlihat.

    Dicocokkan persis, bukan "mengandung", supaya "Turn on" tidak keliru kena
    tombol lain yang kebetulan memuat kata itu.
    """
    try:
        pos = page.run_js(
            "var cari=%s;"
            "var b=document.querySelectorAll('button');"
            "for(var i=0;i<b.length;i++){"
            "  if(!b[i].offsetParent)continue;"
            "  if((b[i].innerText||'').trim()!==cari)continue;"
            "  b[i].scrollIntoView({block:'center'});"
            "  var r=b[i].getBoundingClientRect();"
            "  return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};"
            "}"
            "return null;" % json.dumps(teks))
        if not pos:
            return False
        for tipe in ("mousePressed", "mouseReleased"):
            page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                         x=pos['x'], y=pos['y'], button="left", clickCount=1)
        return True
    except Exception:
        return False


def _buka_show_more(page) -> bool:
    """Buka panel "Show more" di Settings TikTok Studio.

    WAJIB dipanggil sebelum menyentuh AI-generated content. Panelnya
    (data-e2e="advanced_settings_container") default-nya punya kelas
    "collapsed", dan isinya TETAP ADA di DOM meski tersembunyi — ukurannya
    0x0. Itu sebabnya kode lama seolah "menemukan" tombol AI lalu mengkliknya
    tanpa efek apa pun: yang diklik elemen tak terlihat.
    """
    try:
        keadaan = page.run_js(
            "var k=document.querySelector('[data-e2e=\"advanced_settings_container\"]');"
            "if(!k)return null;"
            "var terlipat=String(k.className||'').indexOf('collapsed')>=0;"
            "if(!terlipat)return {sudah:true};"
            "var b=k.querySelector('.more-btn');"
            "if(!b)return {sudah:false,tombol:false};"
            "b.scrollIntoView({block:'center'});"
            "var r=b.getBoundingClientRect();"
            "return {sudah:false,tombol:true,"
            "        x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};")

        if keadaan is None:
            print("[AI_GENERATE] Panel Settings tidak ditemukan.")
            return False
        if keadaan.get('sudah'):
            return True
        if not keadaan.get('tombol'):
            print("[AI_GENERATE] Tombol 'Show more' tidak ditemukan.")
            return False

        print("[AI_GENERATE] Membuka 'Show more'...")
        for tipe in ("mousePressed", "mouseReleased"):
            page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                         x=keadaan['x'], y=keadaan['y'], button="left", clickCount=1)

        for _ in range(12):                     # maksimal ~6 detik
            time.sleep(0.5)
            terbuka = page.run_js(
                "var k=document.querySelector('[data-e2e=\"aigc_container\"]');"
                "return !!(k && k.offsetParent);")
            if terbuka:
                return True

        print("[AI_GENERATE] 'Show more' diklik tapi panel tetap tertutup.")
        return False
    except Exception as e:
        print(f"[AI_GENERATE] Error saat membuka 'Show more': {e}")
        return False


def _do_ai_generate(page) -> bool:
    """Nyalakan switch 'AI-generated content' di TikTok Studio.

    Return True jika switch benar-benar menyala (diverifikasi), False kalau
    tidak (non-fatal, upload tetap lanjut).
    """
    print("[AI_GENERATE] Menandai konten sebagai AI-generated...")
    try:
        if not _buka_show_more(page):
            return False

        keadaan = page.run_js(
            "var k=document.querySelector('[data-e2e=\"aigc_container\"]');"
            "if(!k)return null;"
            "var s=k.querySelector('input[type=checkbox]');"
            "if(!s)return {switch:false};"
            "if(s.checked)return {switch:true,sudah:true};"
            "s.scrollIntoView({block:'center'});"
            "var t=s.closest('label')||s.parentElement;"
            "var r=t.getBoundingClientRect();"
            "return {switch:true,sudah:false,"
            "        x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};")

        if keadaan is None:
            print("[AI_GENERATE] Bagian AI-generated content tidak ditemukan.")
            return False
        if not keadaan.get('switch'):
            print("[AI_GENERATE] Switch AI-generated content tidak ditemukan.")
            return False
        if keadaan.get('sudah'):
            print("[AI_GENERATE] Sudah menyala sejak awal.")
            return True

        for tipe in ("mousePressed", "mouseReleased"):
            page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                         x=keadaan['x'], y=keadaan['y'], button="left", clickCount=1)
        time.sleep(1.5)

        # TikTok memunculkan modal konfirmasi "Labeling AI-generated content"
        # berisi tombol "Turn on". Selama modal ini belum ditekan, switch-nya
        # TETAP MATI — dan dulu itu tidak ketahuan karena hasilnya tidak pernah
        # diperiksa. Modal ini juga menutupi seluruh halaman, jadi kalau
        # dibiarkan terbuka, langkah-langkah berikutnya ikut gagal diklik.
        if _klik_tombol_terlihat(page, "Turn on"):
            print("[AI_GENERATE] Modal konfirmasi muncul - tombol 'Turn on' ditekan.")
            time.sleep(1.0)

        # Diverifikasi, bukan diasumsikan: klik yang mendarat di tempat salah
        # tidak akan mengubah apa pun, dan dulu itu lolos tanpa ketahuan.
        for _ in range(10):                     # maksimal ~5 detik
            time.sleep(0.5)
            nyala = page.run_js(
                "var k=document.querySelector('[data-e2e=\"aigc_container\"]');"
                "if(!k)return null;"
                "var s=k.querySelector('input[type=checkbox]');"
                "return s?s.checked:null;")
            if nyala:
                print("[AI_GENERATE] Switch AI-generated content NYALA (terverifikasi).")
                return True

        print("[AI_GENERATE] WARNING: switch diklik tapi tetap mati.")
        return False
    except Exception as e:
        print(f"[AI_GENERATE] Error: {e}")
        return False


def _fill_caption_hashtag(page, caption: str, hashtags: str = None):
    """Isi caption dan hashtag di TikTok Studio."""
    print("[STEP] Mengisi caption...")

    # Cari caption box — TikTok Studio pakai Draft.js contenteditable div
    cap_box = None
    selectors = [
        '@data-e2e=caption-input',
        '.public-DraftEditor-content',
        '@contenteditable=true',
    ]
    for sel in selectors:
        try:
            cap_box = page.ele(sel, timeout=5)
            if cap_box:
                print(f"[STEP] Caption box ditemukan: {sel}")
                break
        except Exception:
            continue

    if not cap_box:
        try:
            print(f"[CAPTION DEBUG] URL saat ini: {page.url}")
            print(f"[CAPTION DEBUG] Title halaman: {page.title}")
            body_text = page.run_js("return document.body.innerText.slice(0, 500);")
            print(f"[CAPTION DEBUG] Text halaman (500 char awal): {body_text}")
        except Exception as ed:
            print(f"[CAPTION DEBUG] Gagal dump info: {ed}")
        raise Exception("Caption box tidak ditemukan.")

    # Bangun teks lengkap: caption + hashtag sekaligus
    full_text = caption.strip()
    if hashtags:
        tags = [t.strip().lstrip('#') for t in hashtags.split(',') if t.strip()]
        full_text = full_text + ' ' + ' '.join(f'#{t}' for t in tags)

    # ── Clear Draft.js via CDP (satu-satunya cara reliable) ───────────────────
    _clear_draftjs(page, cap_box)
    time.sleep(0.5)

    # Verifikasi sudah kosong — kalau belum, clear sekali lagi
    try:
        current = (cap_box.text or "").strip()
        if current:
            print(f"[WARN] Masih ada teks: '{current[:50]}' — clear ulang...")
            _clear_draftjs(page, cap_box)
            time.sleep(0.5)
        else:
            print("[CLEAR] Caption box kosong, siap diisi.")
    except Exception:
        pass

    # ── Input caption + hashtag ────────────────────────────────────────────────
    cap_box.click()
    time.sleep(0.3)
    cap_box.input(full_text)
    time.sleep(random.uniform(1.5, 2.0))

    # Dismiss dropdown suggestion jika muncul
    try:
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyDown",
            "key": "Escape",
            "code": "Escape",
            "windowsVirtualKeyCode": 27
        })
        time.sleep(0.2)
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyUp",
            "key": "Escape",
            "code": "Escape",
            "windowsVirtualKeyCode": 27
        })
    except Exception:
        pass

    time.sleep(0.5)
    print(f"[STEP] Caption selesai diisi: {full_text[:80]}...")


def _do_schedule(page, schedule_time: str) -> bool:
    import re as _re
    from datetime import datetime
    print(f"[SCHED] Memproses jadwal: {schedule_time}")

    # ── Parse jadwal ──────────────────────────────────────────────────────────
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(schedule_time.strip(), fmt)
            break
        except ValueError:
            continue
    if not dt:
        print(f"[SCHED] Format jadwal tidak dikenali: {schedule_time}")
        return False

    # Bulatkan menit ke kelipatan 5 (TikTok hanya support kelipatan 5)
    rounded = round(dt.minute / 5) * 5
    if rounded == 60:
        rounded = 55
    dt = dt.replace(minute=rounded, second=0)
    hour_int   = dt.hour
    minute_int = rounded
    day_int    = dt.day   # FIX: pakai int bukan str untuk perbandingan exact
    day_str    = str(dt.day)
    print(f"[SCHED] Target: {dt.strftime('%Y-%m-%d')} {hour_int:02d}:{minute_int:02d}")

    # ── Helper: CDP click ─────────────────────────────────────────────────────
    def cdp_click(x, y):
        page.run_cdp("Input.dispatchMouseEvent",
                     type="mousePressed", x=x, y=y, button="left", clickCount=1)
        time.sleep(0.15)
        page.run_cdp("Input.dispatchMouseEvent",
                     type="mouseReleased", x=x, y=y, button="left", clickCount=1)

    def escape_key():
        try:
            page.run_cdp("Input.dispatchKeyEvent",
                         type="rawKeyDown", windowsVirtualKeyCode=27, code="Escape", key="Escape")
            page.run_cdp("Input.dispatchKeyEvent",
                         type="keyUp", windowsVirtualKeyCode=27, code="Escape", key="Escape")
        except Exception:
            pass

    # ── A: Klik radio Schedule ────────────────────────────────────────────────
    page.run_js("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)

    rc = page.run_js(
        "var r=document.querySelector('[value=schedule][type=radio]');"
        "if(!r)return null;"
        "r.scrollIntoView({block:'center'});"
        "var b=r.getBoundingClientRect();"
        "return {x:Math.round(b.x+b.width/2),y:Math.round(b.y+b.height/2)};")
    if not rc:
        print("[SCHED] Radio Schedule tidak ditemukan.")
        return False

    print(f"[SCHED] Klik radio Schedule ({rc['x']},{rc['y']})...")
    try:
        sched_el = page.ele('[value=schedule][type=radio]', timeout=2)
        if sched_el:
            sched_el.click()
        else:
            cdp_click(rc['x'], rc['y'])
    except Exception:
        cdp_click(rc['x'], rc['y'])

    # FIX: Tunggu sebentar lalu cek popup — kalau tidak ada langsung lanjut.
    # Tidak perlu tunggu lama seperti nunggu gebetan yang kabur.
    time.sleep(1)
    has_popup = page.run_js("""
        var modalSels = ['[class*=common-modal]','[role=dialog]','[class*=Modal]'];
        for (var s of modalSels) {
            var m = document.querySelector(s);
            if (!m) continue;
            var r = m.getBoundingClientRect();
            if (r.width > 50 && r.height > 50) {
                var st = window.getComputedStyle(m);
                if (st.display !== 'none' && st.visibility !== 'hidden'
                        && parseFloat(st.opacity) > 0.1)
                    return true;
            }
        }
        return false;
    """)
    if has_popup:
        print("[SCHED] Popup terdeteksi, menutup...")
        _dismiss_popups(page, allow_cancel=False)
        time.sleep(0.8)
    else:
        print("[SCHED] Tidak ada popup — langsung lanjut.")

    # ── B: Deteksi koordinat field Date dan Time ──────────────────────────────
    # Dari log: TikTok Studio taruh Time (x=221) di KIRI, Date (x=413) di KANAN.
    # Jangan pakai teks untuk deteksi — teks timepicker scroll bocor ke parent.
    # Pakai posisi X: field paling KANAN = Date, paling KIRI = Time.
    picker_ready = page.run_js("""
        return !!document.querySelector('[class*=scheduled-picker]')
            || !!document.querySelector('input[type=date]')
            || !!document.querySelector('input[type=time]');
    """)
    if not picker_ready:
        print("[SCHED] Picker belum muncul setelah popup, klik radio Schedule ulang...")
        try:
            sched_el = page.ele('[value=schedule][type=radio]', timeout=2)
            if sched_el:
                sched_el.click()
            else:
                cdp_click(rc['x'], rc['y'])
        except Exception:
            cdp_click(rc['x'], rc['y'])
        time.sleep(1.2)

    fields = page.run_js("""
        var sp = document.querySelector('[class*=scheduled-picker]');
        if (!sp) {
            var nativeInputs = Array.from(document.querySelectorAll('input[type=date],input[type=time]'))
                .filter(function(el) {
                    var r = el.getBoundingClientRect();
                    return r.width > 10 && r.height > 10 && el.offsetParent !== null;
                });
            return nativeInputs.map(function(el, i) {
                var r = el.getBoundingClientRect();
                return {
                    index: i,
                    text: (el.getAttribute('type') || '') + ' ' + (el.value || ''),
                    x: Math.round(r.left + r.width / 2),
                    y: Math.round(r.top + r.height / 2),
                    left: Math.round(r.left),
                    type: el.getAttribute('type')
                };
            });
        }
        return Array.from(sp.children)
            .filter(function(c) {
                var r = c.getBoundingClientRect();
                return r.width > 10 && r.height > 10;
            })
            .map(function(c, i) {
                var r = c.getBoundingClientRect();
                return {
                    index: i,
                    text: c.textContent.trim().substring(0, 60),
                    x: Math.round(r.left + r.width / 2),
                    y: Math.round(r.top + r.height / 2),
                    left: Math.round(r.left)
                };
            });
    """) or []

    print(f"[SCHED] Fields ditemukan: {len(fields)}")
    for f in fields:
        print(f"[SCHED]   index={f['index']} x={f['x']} text='{f['text'][:50]}'")

    date_coord = None
    time_coord = None

    typed_date = next((f for f in fields if f.get('type') == 'date'), None)
    typed_time = next((f for f in fields if f.get('type') == 'time'), None)
    if typed_date or typed_time:
        date_coord = typed_date
        time_coord = typed_time
        print(f"[SCHED] By input type: date={bool(date_coord)}, time={bool(time_coord)}")
    elif len(fields) >= 2:
        # Sort by X: KIRI = time picker, KANAN = date picker
        sorted_x = sorted(fields, key=lambda f: f['x'])
        time_coord = sorted_x[0]   # x terkecil = kiri = timepicker
        date_coord = sorted_x[-1]  # x terbesar = kanan = date picker
        print(f"[SCHED] By X-position: time=x{time_coord['x']}, date=x{date_coord['x']}")
    elif len(fields) == 1:
        # Satu field saja — pakai sebagai date, time akan skip
        date_coord = fields[0]
        print(f"[SCHED] Hanya 1 field ditemukan, dipakai sebagai date.")
    else:
        print("[SCHED] Tidak ada field ditemukan di scheduled-picker!")

    print(f"[SCHED] FINAL date=({date_coord['x'] if date_coord else 'N/A'},{date_coord['y'] if date_coord else 'N/A'}) "
          f"time=({time_coord['x'] if time_coord else 'N/A'},{time_coord['y'] if time_coord else 'N/A'})")

    # ── Helper: buka picker ───────────────────────────────────────────────────
    def open_picker(coord, label, visible_js):
        if page.run_js(visible_js):
            print(f"[SCHED] {label} picker sudah visible.")
            return True
        if not coord:
            print(f"[SCHED] {label} koordinat tidak ditemukan.")
            return False
        escape_key()
        time.sleep(0.3)
        for attempt in range(3):
            print(f"[SCHED] Klik {label} field di ({coord['x']},{coord['y']}) (attempt {attempt+1})...")
            cdp_click(coord['x'], coord['y'])
            for _ in range(6):
                time.sleep(0.4)
                if page.run_js(visible_js):
                    print(f"[SCHED] {label} picker terbuka!")
                    return True
            print(f"[SCHED] {label} picker belum muncul, retry...")
        print(f"[SCHED] {label} picker tidak muncul.")
        return False

    # ── C: PILIH TANGGAL ──────────────────────────────────────────────────────
    print(f"[SCHED] Pilih tanggal {dt.strftime('%Y-%m-%d')}...")

    cal_vis_js = (
        "var sels=['[class*=day-header]','[class*=month-title]',"
        "  '[class*=calendar-container]','[class*=day-span]'];"
        "for(var s of sels){"
        "  var c=document.querySelector(s);"
        "  if(c){var r=c.getBoundingClientRect();"
        "  if(r.height>5&&r.width>5)return true;}}"
        "return false;"
    )

    if not open_picker(date_coord, "Date", cal_vis_js):
        print("[SCHED] Kalender tidak bisa dibuka.")
        return False

    # Navigasi bulan jika perlu
    month_map = {
        'jan':1,'january':1,'januari':1,'feb':2,'february':2,'februari':2,
        'mar':3,'march':3,'maret':3,'apr':4,'april':4,'may':5,'mei':5,
        'jun':6,'june':6,'juni':6,'jul':7,'july':7,'juli':7,
        'aug':8,'august':8,'agustus':8,'sep':9,'september':9,
        'oct':10,'october':10,'oktober':10,'nov':11,'november':11,
        'dec':12,'december':12,'desember':12,
    }

    def _get_cal_month_year():
        raw = page.run_js(
            "var sels=['[class*=month-title]','[class*=year-title]',"
            "  '[class*=calendar-header]','[class*=month-header]'];"
            "var texts=[];"
            "for(var s of sels){"
            "  var els=document.querySelectorAll(s);"
            "  for(var e of els){var t=e.textContent.trim();if(t)texts.push(t);}}"
            "return texts.join(' | ');") or ''
        y = _re.search(r'\b(20\d{2})\b', raw)
        cal_year = int(y.group(1)) if y else None
        cal_month = None
        raw_lower = raw.lower()
        for name, val in month_map.items():
            if _re.search(rf'\b{name}\b', raw_lower):
                if cal_month is None or len(name) > 3:
                    cal_month = val
        return cal_month, cal_year

    for nav_attempt in range(13):
        cal_m, cal_y = _get_cal_month_year()
        need_m, need_y = dt.month, dt.year
        print(f"[SCHED] Kalender: bulan={cal_m}, tahun={cal_y} | Target: {need_m}/{need_y}")
        if cal_m == need_m and cal_y == need_y:
            print("[SCHED] Bulan/tahun sesuai!")
            break
        if cal_m is None or cal_y is None:
            print("[SCHED] Tidak bisa baca bulan/tahun, lanjut...")
            break
        go_forward = (cal_y < need_y) or (cal_y == need_y and cal_m < need_m)
        direction  = "NEXT" if go_forward else "PREV"
        print(f"[SCHED] Navigasi {direction} (attempt {nav_attempt+1})...")
        if go_forward:
            nc = page.run_js(
                "var area=document.querySelector('[class*=day-header],[class*=calendar-header]');"
                "var btns=Array.from((area||document).querySelectorAll('button,[role=button]'));"
                "btns=btns.filter(function(b){var r=b.getBoundingClientRect();return r.width>0&&r.height>0;});"
                "if(!btns.length)return null;"
                "var last=btns[btns.length-1];var r=last.getBoundingClientRect();"
                "return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};")
        else:
            nc = page.run_js(
                "var area=document.querySelector('[class*=day-header],[class*=calendar-header]');"
                "var btns=Array.from((area||document).querySelectorAll('button,[role=button]'));"
                "btns=btns.filter(function(b){var r=b.getBoundingClientRect();return r.width>0&&r.height>0;});"
                "if(!btns.length)return null;"
                "var first=btns[0];var r=first.getBoundingClientRect();"
                "return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};")
        if nc:
            cdp_click(nc['x'], nc['y'])
            time.sleep(0.8)
        else:
            print("[SCHED] Tombol navigasi bulan tidak ditemukan.")
            break

    # FIX: Klik tanggal — verifikasi dengan membaca selected state dari DOM,
    # bukan dari teks kalender yang panjang (yang sebelumnya selalu match palsu)
    date_selected = False
    for day_attempt in range(3):
        day_info = page.run_js(f"""
            var dayStr = '{day_str}';
            var dayInt = {day_int};
            var targetIso = '{dt.strftime('%Y-%m-%d')}';
            var targetYear = '{dt.year}';
            var targetMonth = {dt.month};
            var monthNames = {{
                1:['jan','january','januari'],
                2:['feb','february','februari'],
                3:['mar','march','maret'],
                4:['apr','april'],
                5:['may','mei'],
                6:['jun','june','juni'],
                7:['jul','july','juli'],
                8:['aug','august','agustus'],
                9:['sep','september'],
                10:['oct','october','oktober'],
                11:['nov','november'],
                12:['dec','december','desember']
            }};

            // Cari container kalender yang visible
            var dayHeaders = ['sun','mon','tue','wed','thu','fri','sat',
                              'min','sen','sel','rab','kam','jum','sab'];
            var container = Array.from(document.querySelectorAll(
                '[class*=popup],[class*=modal],[class*=calendar],[class*=popover]'
            )).find(function(el) {{
                var txt = el.textContent.toLowerCase();
                return dayHeaders.some(function(h) {{ return txt.includes(h); }})
                    && el.offsetParent !== null;
            }}) || document;

            function tryFind(sel) {{
                var items = Array.from(container.querySelectorAll(sel));
                return items.filter(function(el) {{
                    // FIX: exact match angka saja (bukan substring)
                    var txt = el.textContent.trim();
                    if (txt !== dayStr) return false;
                    if (el.offsetParent === null) return false;
                    var cn = (el.className || '').toLowerCase();
                    if (cn.indexOf('not-current') >= 0) return false;
                    if (cn.indexOf('outside') >= 0) return false;
                    if (cn.indexOf('disabled') >= 0) return false;
                    if (el.getAttribute('aria-disabled') === 'true') return false;
                    // Pastikan ada di viewport
                    var r = el.getBoundingClientRect();
                    if (r.top < 0 || r.bottom > window.innerHeight) return false;
                    if (r.left < 0 || r.right > window.innerWidth) return false;

                    // Jika elemen punya atribut tanggal lengkap, pakai itu untuk
                    // menghindari memilih tanggal sama dari bulan sebelah.
                    var attr = [
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('datetime'),
                        el.getAttribute('data-date'),
                        el.getAttribute('data-value'),
                        el.getAttribute('value')
                    ].filter(Boolean).join(' ').toLowerCase();
                    var iso = attr.match(/20\\d{{2}}[-/]\\d{{1,2}}[-/]\\d{{1,2}}/);
                    if (iso) {{
                        var parts = iso[0].replace(/\\//g, '-').split('-');
                        var normIso = parts[0] + '-' + parts[1].padStart(2, '0') + '-' + parts[2].padStart(2, '0');
                        if (normIso !== targetIso) return false;
                    }}
                    if (attr.includes(targetYear)) {{
                        var names = monthNames[targetMonth] || [];
                        var hasMonth = names.some(function(m) {{ return attr.includes(m); }});
                        var hasOtherMonth = Object.keys(monthNames).some(function(k) {{
                            if (parseInt(k) === targetMonth) return false;
                            return monthNames[k].some(function(m) {{ return attr.includes(m); }});
                        }});
                        if (hasOtherMonth && !hasMonth) return false;
                    }}
                    return true;
                }});
            }}

            var cands = tryFind('[class*=day-span]');
            if (!cands.length) cands = tryFind('[class*=day-item]');
            if (!cands.length) cands = tryFind('td');
            if (!cands.length) cands = tryFind('[class*=day]');
            if (!cands.length) cands = tryFind('[class*=date]');
            if (!cands.length) return null;

            // Pilih kandidat yang paling di tengah kalender (hindari overflow bulan lain)
            var calCenterX = window.innerWidth / 2;
            cands.sort(function(a, b) {{
                var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                var ax = ra.left + ra.width / 2, bx = rb.left + rb.width / 2;
                return Math.abs(ax - calCenterX) - Math.abs(bx - calCenterX);
            }});
            var best = cands[0];
            var r = best.getBoundingClientRect();

            // Klik via JS + React fiber + MouseEvent
            best.click();
            var fk = Object.keys(best).find(function(k) {{
                return k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance');
            }});
            if (fk) {{
                var nd = best[fk];
                for (var i = 0; i < 15; i++) {{
                    if (!nd) break;
                    var pr = nd.memoizedProps || nd.pendingProps;
                    if (pr && pr.onClick) {{
                        try {{ pr.onClick({{ bubbles:true, preventDefault:function(){{}}, stopPropagation:function(){{}} }}); }} catch(e) {{}}
                        break;
                    }}
                    nd = nd.return;
                }}
            }}
            best.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true,cancelable:true}}));
            best.dispatchEvent(new MouseEvent('mouseup',   {{bubbles:true,cancelable:true}}));
            best.dispatchEvent(new MouseEvent('click',     {{bubbles:true,cancelable:true}}));

            return {{
                x: Math.round(r.left + r.width / 2),
                y: Math.round(r.top  + r.height / 2),
                text: best.textContent.trim(),
                cn: (best.className || '').substring(0, 60)
            }};
        """)

        if not day_info:
            print(f"[SCHED] Elemen tanggal {day_str} tidak ditemukan (attempt {day_attempt+1}).")
            if day_attempt < 2:
                time.sleep(1)
            continue

        print(f"[SCHED] Klik tanggal '{day_info['text']}' di ({day_info['x']},{day_info['y']}) (attempt {day_attempt+1})")
        cdp_click(day_info['x'], day_info['y'])
        time.sleep(1.0)

        # FIX: Verifikasi dengan membaca elemen yang punya class 'selected'/'active'
        # bukan dari teks kalender yang panjang (dulu selalu false positive)
        verified = page.run_js(f"""
            var dayStr = '{day_str}';
            // Cari elemen yang marked as selected/active dan teksnya match
            var selSels = [
                '[class*=selected]', '[class*=active]',
                '[aria-selected=true]', '[class*=current-day]'
            ];
            for (var ss of selSels) {{
                var els = document.querySelectorAll(ss);
                for (var el of els) {{
                    if (el.textContent.trim() === dayStr && el.offsetParent !== null) {{
                        return true;
                    }}
                }}
            }}
            return false;
        """)

        # Cek apakah kalender masih terbuka setelah klik
        cal_still_open = page.run_js(cal_vis_js)

        if verified:
            # Kalender masih terbuka + selected state terbaca = verifikasi sempurna
            print(f"[SCHED] Tanggal {day_str} TERVERIFIKASI via selected state!")
            date_selected = True
            break
        elif not cal_still_open:
            # Kalender tertutup otomatis setelah klik = TikTok menerima klik = SUKSES
            # Ini behavior normal TikTok Studio — tidak perlu buka lagi
            print(f"[SCHED] Kalender tertutup otomatis setelah klik — tanggal {day_str} DITERIMA!")
            date_selected = True
            break
        else:
            # Kalender masih terbuka + selected tidak terbaca = klik belum kena
            print(f"[SCHED] Kalender masih terbuka, selected state tidak terbaca — retry...")
            if day_attempt < 2:
                time.sleep(0.5)

    if not date_selected:
        # KRITIS: JANGAN lanjut submit kalau tanggal tidak terverifikasi -
        # sebelumnya kode ini cuma warning lalu tetap lanjut klik tombol
        # Schedule, dan _do_schedule return True asal ada redirect (tidak
        # pernah cek tanggal beneran kepencet apa nggak). Akibatnya post
        # bisa kejadwal di TANGGAL YANG SALAH (mis. hari ini) tanpa ada
        # error sama sekali - baru ketahuan user setelah post-nya tayang.
        # Lebih baik gagal jelas drpd diam-diam salah jadwal.
        print(f"[SCHED] GAGAL: Tanggal {day_str} tidak bisa dipastikan terpilih "
              f"setelah 3 percobaan klik. Kalender masih terbuka & tidak ada "
              f"state 'selected' yang terbaca — berhenti drpd berisiko submit "
              f"dengan tanggal yang salah.")
        escape_key()
        return False

    # Tutup kalender sebelum buka time picker
    escape_key()
    time.sleep(0.5)

    # ── D: PILIH WAKTU (TIME) ─────────────────────────────────────────────────
    print(f"[SCHED] Pilih waktu {hour_int:02d}:{minute_int:02d}...")

    tp_vis_js = (
        "var sels=['[class*=tiktok-timepicker-time-picker-container]',"
        "  '[class*=time-picker-container]','[class*=timepicker]'];"
        "for(var s of sels){"
        "  var c=document.querySelector(s);"
        "  if(!c)continue;"
        "  var r=c.getBoundingClientRect();"
        "  if(r.height>5&&c.className.indexOf('invisible')<0)return true;}"
        "return false;"
    )

    tp_visible = open_picker(time_coord, "Time", tp_vis_js)
    if tp_visible:
        def click_tp_item(col_idx, item_idx, label):
            # Scroll item ke tengah kolom
            page.run_js(
                f"var cols=document.querySelectorAll('[class*=tiktok-timepicker-time-scroll-container]');"
                f"if(cols.length<={col_idx})return;"
                f"var col=cols[{col_idx}];"
                f"var items=col.querySelectorAll('[class*=tiktok-timepicker-option-item]');"
                f"if(items.length<={item_idx})return;"
                f"var t=items[{item_idx}];"
                f"col.scrollTop=t.offsetTop-col.clientHeight/2+t.clientHeight/2;")
            time.sleep(0.8)
            coord = page.run_js(
                f"var cols=document.querySelectorAll('[class*=tiktok-timepicker-time-scroll-container]');"
                f"if(cols.length<={col_idx})return null;"
                f"var col=cols[{col_idx}];"
                f"var items=col.querySelectorAll('[class*=tiktok-timepicker-option-item]');"
                f"if(items.length<={item_idx})return null;"
                f"var t=items[{item_idx}];var r=t.getBoundingClientRect();"
                f"if(r.height<1)return null;"
                f"return {{x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}};")
            if coord:
                cdp_click(coord['x'], coord['y'])
                time.sleep(0.8)
                print(f"[SCHED] {label} dipilih.")
                return True
            print(f"[SCHED] {label} item tidak ditemukan.")
            return False

        num_cols = page.run_js(
            "return document.querySelectorAll('[class*=tiktok-timepicker-time-scroll-container]').length;") or 0

        if num_cols >= 3:
            # Format 12 jam (AM/PM)
            is_pm = hour_int >= 12
            h12   = hour_int % 12
            if h12 == 0:
                h12 = 12
            click_tp_item(0, h12 - 1,        f"Jam {h12}")
            click_tp_item(1, minute_int // 5, f"Menit {minute_int}")
            click_tp_item(2, 1 if is_pm else 0, "PM" if is_pm else "AM")
        elif num_cols >= 2:
            # Format 24 jam
            click_tp_item(0, hour_int,        f"Jam {hour_int}")
            click_tp_item(1, minute_int // 5, f"Menit {minute_int}")
        else:
            print(f"[SCHED] Timepicker kolom tidak ditemukan (num_cols={num_cols}).")

        # Tutup time picker sebelum klik tombol Schedule
        escape_key()
        time.sleep(0.5)
    else:
        print("[SCHED] Timepicker tidak visible — lanjut tanpa set waktu.")

    time.sleep(1)

    # ── E: KLIK TOMBOL SCHEDULE ───────────────────────────────────────────────
    # FIX: Tidak ada popup check yang lama di sini.
    # Langsung cari tombol Schedule, klik, tunggu redirect/error.
    # Kalau ada popup "Content restricted" baru tangani — tidak nunggu duluan.
    page.run_js("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.5)

    for attempt in range(3):
        cur_url = page.url.lower()
        if "manage" in cur_url or "content" in cur_url:
            print("[SCHED] Redirect terdeteksi — schedule berhasil!")
            return True

        # Cari tombol konfirmasi jadwal dengan MEMINDAI tombol yang terlihat,
        # bukan lewat daftar selector tetap.
        #
        # Versi lama memakai //button[normalize-space()="Schedule"|"Jadwalkan"].
        # Terbukti gagal di UI berbahasa Indonesia (log user 11 Agu: tanggal &
        # jam sudah terisi benar, lalu "Tombol Schedule tidak ditemukan" 3x):
        # tombolnya bukan <button> polos berteks persis itu. Karena labelnya
        # bisa berbeda per bahasa/rollout, saat tidak ketemu SELURUH label
        # tombol yang terlihat ikut dicetak — jadi label aslinya langsung
        # kelihatan di log tanpa perlu menebak lagi.
        pindai = page.run_js(r"""
        return (function(){
            function vis(el){
                var r = el.getBoundingClientRect();
                var s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                       s.display !== 'none' && s.visibility !== 'hidden';
            }
            var kandidat = [], semua = [], seen = new Set();
            var sel = 'button,[role="button"],[data-e2e*="post"],[data-e2e*="schedule"]';
            document.querySelectorAll(sel).forEach(function(b){
                if (!vis(b) || seen.has(b)) return;
                seen.add(b);
                if (b.disabled || b.getAttribute('aria-disabled') === 'true') return;
                var t = (b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim();
                if (!t || t.length > 40) return;
                semua.push(t);
                // HANYA kata yang berarti "jadwalkan". Kata seperti Post/Unggah
                // SENGAJA tidak dicocokkan — menekannya akan memposting
                // SEKARANG, bukan menjadwalkan.
                if (/^(schedule|jadwalkan|jadwal|atur jadwal|schedule post)$/i.test(t)) {
                    // WAJIB digulir dulu. Tombol ini ada di dasar halaman dan
                    // koordinatnya bisa jatuh di LUAR viewport (terbukti:
                    // y=1140 pada jendela ~1000px), sehingga klik CDP mendarat
                    // di ruang kosong dan tidak terjadi apa-apa.
                    try { b.scrollIntoView({block: 'center'}); } catch(e) {}
                    var r = b.getBoundingClientRect();
                    kandidat.push({teks: t,
                                   e2e: b.getAttribute('data-e2e') || '',
                                   tag: (b.tagName || '').toLowerCase(),
                                   dalam_layar: r.top >= 0 && r.bottom <= window.innerHeight,
                                   x: Math.round(r.left + r.width / 2),
                                   y: Math.round(r.top + r.height / 2)});
                }
            });
            return {kandidat: kandidat, semua: semua.slice(0, 30),
                    tinggi: window.innerHeight};
        })()
        """) or {}

        kandidat = (pindai or {}).get('kandidat') or []
        if not kandidat:
            time.sleep(2)
            if "manage" in page.url.lower() or "content" in page.url.lower():
                return True
            print(f"[SCHED] Attempt {attempt+1}: Tombol jadwal tidak ditemukan. "
                  f"Tombol yang terlihat: {(pindai or {}).get('semua')}")
            continue

        tombol = kandidat[0]
        print(f"[SCHED] Attempt {attempt+1}: Klik tombol '{tombol['teks']}' "
              f"<{tombol.get('tag')}> e2e='{tombol.get('e2e')}' "
              f"di ({tombol['x']},{tombol['y']}) "
              f"dalam_layar={tombol.get('dalam_layar')} "
              f"tinggi_jendela={(pindai or {}).get('tinggi')}...")
        cdp_click(tombol['x'], tombol['y'])
        time.sleep(2)

        # Cadangan: klik langsung lewat JS. Klik koordinat bisa meleset kalau
        # ada elemen lain menimpa titik itu, atau kalau tombolnya bergeser
        # sesudah digulir.
        if "manage" not in page.url.lower() and "content" not in page.url.lower():
            diklik = page.run_js(r"""
            return (function(){
                function vis(el){
                    var r = el.getBoundingClientRect();
                    var s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.display !== 'none' && s.visibility !== 'hidden';
                }
                var hasil = '';
                document.querySelectorAll('button,[role="button"]').forEach(function(b){
                    if (hasil || !vis(b)) return;
                    if (b.disabled || b.getAttribute('aria-disabled') === 'true') return;
                    var t = (b.innerText || b.textContent || '')
                            .replace(/\s+/g, ' ').trim();
                    if (/^(schedule|jadwalkan|jadwal|atur jadwal|schedule post)$/i.test(t)) {
                        try { b.click(); hasil = t; } catch(e) {}
                    }
                });
                return hasil;
            })()
            """)
            if diklik:
                print(f"[SCHED] Klik cadangan lewat JS pada '{diklik}'.")
        time.sleep(2)

        # Cek apakah ada popup "Content restricted" — kalau ada, dismiss lalu klik ulang
        had_popup = _dismiss_content_restricted(page)
        if had_popup:
            print("[SCHED] Popup dismissed, klik Schedule ulang...")
            time.sleep(1)
            continue

        # Tunggu redirect — cek setiap 1 detik max 10 detik
        for _ in range(10):
            time.sleep(1)
            if "manage" in page.url.lower() or "content" in page.url.lower():
                print("[SCHED] Redirect terdeteksi — schedule berhasil!")
                return True

        print(f"[SCHED] Attempt {attempt+1}: Belum redirect setelah klik Schedule.")

    # Cek terakhir
    if "manage" in page.url.lower() or "content" in page.url.lower():
        return True

    # Dulu di sini `return True` tanpa syarat, dengan alasan biar verifikasi
    # diserahkan ke _wait_post_success. Akibatnya jadwal yang jelas-jelas gagal
    # tetap dilaporkan berhasil, lalu alurnya menggantung lama di verifikasi
    # dan berakhir dengan browser ditutup paksa. Sekarang keadaan halaman
    # dibaca sekali lagi: kalau memang ada tanda sukses, akui; kalau tidak,
    # laporkan gagal apa adanya.
    if _wait_post_success(page, timeout=20):
        print("[SCHED] Konfirmasi ditemukan pada pemeriksaan terakhir.")
        return True

    print("[SCHED] Semua attempt habis dan tidak ada konfirmasi — schedule GAGAL.")
    return False


def _fill_date_time_input(page, input_type: str, value: str):
    try:
        inp = page.ele(f'xpath://input[@type="{input_type}"]', timeout=3)
        if inp:
            inp.click()
            time.sleep(0.3)
            inp.input(value)
            time.sleep(0.2)
            page.run_js(
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                inp)
            print(f"[SCHED] {input_type} diisi: {value}")
            return
    except Exception as e:
        print(f"[SCHED] {input_type} input gagal: {e}")

    # Fallback: native value setter via JS (untuk React)
    try:
        js = (
            f"var el=document.querySelector('[type={input_type}]');"
            f"if(el){{"
            f"var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
            f"s.call(el,'{value}');"
            f"el.dispatchEvent(new Event('input',{{bubbles:true}}));"
            f"el.dispatchEvent(new Event('change',{{bubbles:true}}));}}"
        )
        page.run_js(js)
        print(f"[SCHED] {input_type} diisi via JS: {value}")
    except Exception as e:
        print(f"[SCHED] {input_type} JS gagal: {e}")


def _click_post(page) -> bool:
    """Scroll ke bawah lalu klik tombol Post di TikTok Studio."""
    print("[STEP] Scroll ke bawah untuk cari tombol Post...")

    # Scroll ke bawah halaman agar tombol Post terlihat
    try:
        page.run_js("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
    except Exception:
        pass

    print("[STEP] Mencari tombol Post...")
    selectors = [
        '@data-e2e=post_video_button',
        'xpath://button[@data-e2e="post_video_button"]',
        'xpath://button[text()="Post"]',
        'xpath://button[normalize-space()="Post"]',
    ]
    for sel in selectors:
        try:
            btn = page.ele(sel, timeout=3)
            if btn:
                print(f"[STEP] Tombol Post ditemukan: {sel}")
                btn.click()
                print("[STEP] Tombol Post diklik.")
                return True
        except Exception:
            continue

    # Fallback: scan semua button
    try:
        buttons = page.eles('tag:button', timeout=3)
        for btn in buttons:
            txt = (btn.text or '').strip().lower()
            de2 = (btn.attr('data-e2e') or '').lower()
            if txt == 'post' or de2 == 'post_video_button':
                print(f"[STEP] Tombol Post via scan: '{txt}'")
                btn.click()
                return True
    except Exception as e:
        print(f"[STEP] Scan gagal: {e}")

    print("[ERROR] Tombol Post tidak ditemukan.")
    return False


def _dismiss_content_restricted(page) -> bool:
    """
    Tunggu popup 'Content may be restricted' dan tutup dengan klik X atau Post now.
    Dipanggil setelah klik Post/Schedule pertama.
    Return True jika popup ditemukan dan ditutup, False jika tidak ada popup.
    PENTING: Hanya mendeteksi MODAL NYATA yang visible — bukan teks biasa di halaman.
    """
    print("[RESTRICT] Menunggu popup 'Content may be restricted'...")
    # Wait singkat, lalu cek max 4x (total ~8 detik)
    time.sleep(1.5)
    for attempt in range(4):
        # JIKA URL sudah berubah ke manage/content, berarti post sukses
        try:
            cur_url = page.url.lower()
            if "manage" in cur_url or "content" in cur_url:
                print("[RESTRICT] Post berhasil terdeteksi (redirect), stop mencari popup.")
                return False
        except:
            pass

        try:
            # Deteksi HANYA dalam modal/dialog yang benar-benar visible
            # Tidak cek document.body.innerText (terlalu luas, false positive)
            modal_info = page.run_js("""
                function isVisible(el) {
                    if (!el) return false;
                    var r = el.getBoundingClientRect();
                    if (r.width < 50 || r.height < 50) return false;
                    var style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' && parseFloat(style.opacity) > 0.1;
                }
                // Cari modal container yang benar-benar muncul (bukan halaman biasa)
                var modalSels = [
                    '[class*="common-modal-container"]',
                    '[class*="CommonModal"]',
                    '[data-e2e="modal-container"]',
                    '[class*="restriction"]',
                    '[class*="Restriction"]'
                ];
                for (var ms of modalSels) {
                    var modal = document.querySelector(ms);
                    if (modal && isVisible(modal)) {
                        var txt = (modal.innerText || '').toLowerCase();
                        var keywords = ['restricted', 'continue to post', 'checking your video',
                                        'lanjutkan', 'cek hak cipta', 'copyright', 'potensi masalah',
                                        'may be restricted', 'community guidelines'];
                        for (var k of keywords) {
                            if (txt.includes(k)) return {found: true, text: txt.substring(0, 100)};
                        }
                    }
                }
                // Cari [role=dialog] yang visible dan berisi keyword
                var dialogs = document.querySelectorAll('[role="dialog"]');
                for (var d of dialogs) {
                    if (!isVisible(d)) continue;
                    var txt2 = (d.innerText || '').toLowerCase();
                    var kws = ['restricted', 'continue to post', 'copyright', 'community guidelines',
                               'lanjutkan', 'cek hak cipta'];
                    for (var k2 of kws) {
                        if (txt2.includes(k2)) return {found: true, text: txt2.substring(0, 100)};
                    }
                }
                return {found: false};
            """)

            if modal_info and modal_info.get('found'):
                print(f"[RESTRICT] Popup NYATA ditemukan: '{modal_info.get('text', '')[:60]}'")
                time.sleep(0.3)

                # Cari koordinat tombol penutup DALAM modal
                coords = page.run_js("""
                    function getCoord(el) {
                        var r = el.getBoundingClientRect();
                        return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
                    }
                    function isVisible(el) {
                        if (!el) return false;
                        var r = el.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5) return false;
                        var s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }

                    // 1. Tombol X di modal (common-modal-close)
                    var closeBtn = document.querySelector('[class*="common-modal-close"]');
                    if (closeBtn && isVisible(closeBtn)) return getCoord(closeBtn);

                    // 2. data-e2e confirm button
                    var e2e = document.querySelector('[data-e2e="modal-confirm-button"],[data-e2e="ok-btn"]');
                    if (e2e && isVisible(e2e)) return getCoord(e2e);

                    // 3. Scan teks dalam modal/dialog saja
                    var targets = ['post now', 'post sekarang', 'lanjutkan', 'posting sekarang',
                                   'tetap posting', 'continue to post'];
                    var containers = document.querySelectorAll('[class*="common-modal"],[role="dialog"],[class*="restriction"]');
                    for (var c of containers) {
                        var btns = c.querySelectorAll('button, [role="button"]');
                        for (var btn of btns) {
                            if (!isVisible(btn)) continue;
                            var t = (btn.textContent || '').trim().toLowerCase();
                            for (var tgt of targets) {
                                if (t.includes(tgt)) return getCoord(btn);
                            }
                        }
                    }
                    return null;
                """)

                if coords:
                    print(f"[RESTRICT] Klik tombol penutup di ({coords['x']}, {coords['y']})...")
                    page.run_cdp('Input.dispatchMouseEvent', type='mousePressed',
                                 x=coords['x'], y=coords['y'], button='left', clickCount=1)
                    time.sleep(0.05)
                    page.run_cdp('Input.dispatchMouseEvent', type='mouseReleased',
                                 x=coords['x'], y=coords['y'], button='left', clickCount=1)
                    time.sleep(2.0)
                    return True
                else:
                    # Popup ada tapi tombol tidak ketemu — jangan Escape (bisa reset form)
                    # Coba klik di luar modal untuk dismiss
                    print("[RESTRICT] Tombol penutup tidak ditemukan dalam modal, skip Escape (agar form tidak reset).")
                    time.sleep(1)
                    return True
            else:
                print(f"[RESTRICT] Tidak ada popup nyata (attempt {attempt+1}/4).")
        except Exception as ex:
            print(f"[RESTRICT] Error cek popup: {ex}")

        time.sleep(2)
    print("[RESTRICT] Tidak ada popup 'Content restricted' yang terdeteksi — dianggap bersih.")
    return False


def _wait_post_success(page, timeout: int = 45) -> bool:
    """Tunggu konfirmasi posting/penjadwalan berhasil.

    KENAPA DITULIS ULANG (gejala yang dilaporkan user: "tiba-tiba stag lalu
    browser force closed"):

    Versi lama memanggil page.ele(...) dengan timeout=1 sebanyak SEPULUH kali
    tiap putaran (4 pola sukses + 6 pola error). Kalau tidak ada yang cocok —
    keadaan yang justru paling sering — satu putaran menghabiskan ~10 detik
    HANYA untuk menunggu selector meleset, dan itu diulang sampai timeout 60
    detik. Selama itu browser terlihat membeku total padahal yang terblokir
    adalah kodenya. Sesudah timeout, exception dilempar, page.quit() menutup
    browser, dan bridge lanjut ke video berikutnya.

    Sekarang seluruh pemeriksaan dikerjakan dalam SATU evaluasi JS per putaran,
    jadi tiap putaran hanya sepersekian detik dan sisa waktu benar-benar
    dipakai untuk menunggu TikTok, bukan menunggu selector gagal.

    Pencocokan error juga diperketat. Pola lama `contains(text(),"gagal")`
    cocok dengan teks apa pun yang memuat kata itu — termasuk tulisan UI biasa
    yang tidak ada hubungannya — sehingga posting yang sebenarnya sukses bisa
    dinyatakan gagal.
    """
    print("[WAIT] Menunggu konfirmasi post...")

    CEK = r"""
    return (function(){
        var t = (document.body ? document.body.innerText : '');
        var u = (location.href || '').toLowerCase();
        function ada(daftar){
            for (var i = 0; i < daftar.length; i++)
                if (t.toLowerCase().indexOf(daftar[i]) >= 0) return daftar[i];
            return '';
        }
        return {
            url: u,
            sukses: ada([
                'posted successfully', 'berhasil diposting', 'berhasil diunggah',
                'scheduled successfully', 'berhasil dijadwalkan',
                'your video is being uploaded', 'video anda sedang diunggah',
                'your post is scheduled', 'postingan dijadwalkan'
            ]),
            gagal: ada([
                'something went wrong', 'terjadi kesalahan',
                'copyright infringement', 'pelanggaran hak cipta',
                'violates our community guidelines', 'melanggar pedoman komunitas',
                'gagal mengunggah', 'upload failed', 'failed to upload'
            ]),
            cuplikan: t.replace(/\s+/g, ' ').slice(0, 160)
        };
    })()
    """

    start = time.time()
    terakhir = None
    while time.time() - start < timeout:
        try:
            info = page.run_js(CEK)
        except Exception:
            info = None

        if isinstance(info, dict):
            terakhir = info
            url = info.get('url') or ''
            # Redirect ke daftar konten = TikTok sudah menerima postingannya.
            if 'manage' in url or '/content' in url or 'tiktokstudio/content' in url:
                print("[SUCCESS] Redirect ke halaman konten — post/schedule berhasil!")
                return True
            if info.get('sukses'):
                print(f"[SUCCESS] Konfirmasi terdeteksi: '{info['sukses']}'")
                return True
            if info.get('gagal'):
                print(f"[ERROR] TikTok menolak: '{info['gagal']}'")
                return False

        time.sleep(2)

    print(f"[WARN] Timeout {timeout}s menunggu konfirmasi post. "
          f"Keadaan terakhir: {terakhir}")
    return False


# ── Main upload function ──────────────────────────────────────────────────────

def upload_video(video_path: str, caption: str, account_id: int,
                 upload_type: str, is_invisible: int = 0,
                 hashtags: str = None,
                 product_name: str = None, product_id: str = None,
                 schedule_time: str = None,
                 location: str = None,
                 music: str = None,
                 ai_generate_content: bool = False,
                 progress_callback=None) -> bool:

    def set_p(v):
        if progress_callback:
            progress_callback(v)

    # Ambil data akun dari DB
    from app.db.database import get_db
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT username, current_session FROM accounts WHERE id=?", (account_id,))
    acc  = c.fetchone()
    conn.close()

    if not acc:
        print(f"[ERROR] Akun ID={account_id} tidak ditemukan di DB.")
        return False

    username     = acc["username"]
    profile_path = acc["current_session"]

    if not profile_path:
        print(f"[ERROR] Profile path kosong untuk akun '{username}'.")
        return False

    if not os.path.exists(profile_path):
        print(f"[ERROR] Profile path tidak ada: {profile_path}")
        return False

    if not os.path.exists(video_path):
        print(f"[ERROR] File video tidak ada: {video_path}")
        return False

    port     = 9322 + (hash(username) % 1000)
    headless = bool(is_invisible)

    print(f"\n{'='*50}")
    print(f"[UPLOAD] Mulai upload untuk akun: {username}")
    print(f"[UPLOAD] Video : {os.path.basename(video_path)}")
    print(f"[UPLOAD] Tipe  : {upload_type} | Headless: {headless}")
    print(f"{'='*50}")

    page = _connect(profile_path, port, headless)
    set_p(20)
    if not page:
        print("[ERROR] Gagal membuka browser.")
        return False

    try:
        # ── STEP 1: Buka TikTok Studio ────────────────────────────────────────
        # Halaman edit TikTok memasang beforeunload. Begitu ada perubahan yang
        # belum disimpan (mis. video kedua dan seterusnya dalam satu batch),
        # navigasi memunculkan dialog native "Leave site?". Dialog itu MEMBEKUKAN
        # seluruh perintah CDP — browser terlihat menggantung tanpa pesan error
        # apa pun. Terbukti saat pengujian: satu dialog yang tidak ditutup
        # membuat seluruh sisa batch mati.
        try:
            page.set.auto_handle_alert(True, accept=True)
        except Exception:
            pass
        try:
            page.handle_alert(accept=True, timeout=1)
        except Exception:
            pass

        print(f"[STEP 1] Navigasi ke TikTok Studio...")
        page.get(TIKTOK_UPLOAD_URL)
        set_p(30)

        # Tunggu dan cek login — TikTok kadang redirect sementara.
        #
        # PENTING: jangan mengandalkan "tiktokstudio ada di URL". Halaman login
        # TikTok berbentuk
        #   /login?redirect_url=https%3A%2F%2F...%2Ftiktokstudio%2Fupload
        # sehingga kata "tiktokstudio" IKUT ADA di URL login. Pengecekan lama
        # karena itu melaporkan "Sesi valid" untuk akun yang justru belum login,
        # lalu upload gagal beberapa langkah kemudian dengan pesan yang
        # menyesatkan ("input file tidak ditemukan"). Yang dipakai sekarang:
        # path URL-nya benar-benar bukan /login, DAN elemen upload sungguh ada.
        print(f"[STEP 1] Menunggu halaman load dan verifikasi sesi...")

        def _sudah_login():
            try:
                jalur = (page.run_js("return location.pathname;") or '').lower()
            except Exception:
                jalur = ''
            if jalur.startswith('/login') or jalur.startswith('/signup'):
                return False
            try:
                return bool(page.run_js(
                    "return !!(document.querySelector('input[type=file]')"
                    " || document.querySelector('div.poi-search'));"))
            except Exception:
                return False

        login_ok = False
        for _ in range(12):                      # maksimal ~24 detik
            time.sleep(2)
            if _sudah_login():
                login_ok = True
                break

        if not login_ok:
            print(f"[AUTH] Sesi '{username}' TIDAK VALID - URL: {page.url}")
            print(f"[AUTH] Akun ini belum login di TikTok. Buka profilnya di browser, "
                  f"login ke TikTok, lalu ulangi upload.")
            page.quit()
            return False

        print(f"[STEP 1] Sesi valid, halaman terbuka: {page.url}")

        # ── STEP 2: Pilih file video ──────────────────────────────────────────
        print(f"[STEP 2] Mencari input file upload...")

        # TikTok Studio punya input file tersembunyi — cari dengan berbagai selector
        file_input = None
        file_selectors = [
            'xpath://input[@type="file"]',
            'xpath://input[@accept]',
        ]
        for sel in file_selectors:
            try:
                file_input = page.ele(sel, timeout=8)
                if file_input:
                    print(f"[STEP 2] Input file ditemukan dengan selector: {sel}")
                    break
            except Exception:
                continue

        if not file_input:
            # Coba klik area upload dulu untuk memunculkan input
            print("[STEP 2] Input langsung tidak ada, coba klik area upload...")
            upload_area_selectors = [
                '@data-e2e=upload-drag-area',
                'xpath://div[contains(@class,"upload-wrapper")]',
                'xpath://div[contains(@class,"drag")]',
                'xpath://*[contains(text(),"Select files")]',
                'xpath://*[contains(text(),"Upload")]',
            ]
            for sel in upload_area_selectors:
                try:
                    area = page.ele(sel, timeout=3)
                    if area:
                        area.click()
                        time.sleep(2)
                        # Coba cari input file lagi setelah klik
                        file_input = page.ele('xpath://input[@type="file"]', timeout=5)
                        if file_input:
                            break
                except Exception:
                    continue

        if not file_input:
            raise Exception("Input file upload tidak ditemukan di halaman TikTok Studio.")

        # Set file ke input — coba berbagai API DrissionPage sesuai versi
        print(f"[STEP 2] Memasukkan file: {os.path.basename(video_path)}")
        set_p(40)
        abs_path = os.path.abspath(video_path)
        upload_ok = False

        # Cara 1: DrissionPage 4.x — set.files() dengan list
        try:
            file_input.set.files(abs_path)
            upload_ok = True
            print(f"[STEP 2] File diset via set.files() (str)")
        except Exception as e1:
            print(f"[STEP 2] set.files(str) gagal: {e1}")

        # Cara 2: set.files() dengan list
        if not upload_ok:
            try:
                file_input.set.files([abs_path])
                upload_ok = True
                print(f"[STEP 2] File diset via set.files([list])")
            except Exception as e2:
                print(f"[STEP 2] set.files([list]) gagal: {e2}")

        # Cara 3: DrissionPage versi lama — set_value()
        if not upload_ok:
            try:
                file_input.set_value(abs_path)
                upload_ok = True
                print(f"[STEP 2] File diset via set_value()")
            except Exception as e3:
                print(f"[STEP 2] set_value() gagal: {e3}")

        # Cara 4: CDP sendKeys via JavaScript
        if not upload_ok:
            try:
                file_input.input(abs_path)
                upload_ok = True
                print(f"[STEP 2] File diset via input()")
            except Exception as e4:
                print(f"[STEP 2] input() gagal: {e4}")

        # Cara 5: CDP setFiles langsung
        if not upload_ok:
            try:
                node_id = file_input.node_id
                page.run_cdp("DOM.setFileInputFiles",
                             files=[abs_path],
                             nodeId=node_id)
                upload_ok = True
                print(f"[STEP 2] File diset via CDP DOM.setFileInputFiles")
            except Exception as e5:
                print(f"[STEP 2] CDP setFileInputFiles gagal: {e5}")

        if not upload_ok:
            raise Exception(
                "Semua metode set file gagal. "
                "Cek versi DrissionPage: pip show DrissionPage")

        print(f"[STEP 2] File berhasil dimasukkan, menunggu upload selesai...")
        set_p(50)

        # ── STEP 3: Tunggu video selesai diproses ─────────────────────────────
        # Tunggu minimal 10 detik lalu cek apakah sudah siap
        time.sleep(10)
        _wait_upload_complete(page, timeout=180)
        set_p(70)
        time.sleep(random.uniform(3, 5))

        # ── STEP 3.5: Tutup popup yang muncul setelah upload ─────────────────
        _dismiss_popups(page)
        time.sleep(random.uniform(1, 2))

        # ── STEP 4: Isi caption dan hashtag ───────────────────────────────────
        _fill_caption_hashtag(page, caption, hashtags)
        set_p(80)
        time.sleep(random.uniform(2, 3))

        # ── STEP 4.5: Lokasi dan AI Generate Content ──────────────────────────
        if location and location.strip() and upload_type == 'regular':
            _do_location(page, location)
            time.sleep(random.uniform(1, 2))

        # Musik untuk VIDEO. Tombol "Add sound" saat ini hanya muncul di
        # halaman foto; di halaman video belum ada (diperiksa langsung, dua
        # kali, dengan unggahan sudah 100%). _do_sound menanganinya dengan
        # aman: kalau tombolnya tidak ada, dicatat lalu dilewati — jadi begitu
        # TikTok memunculkannya di halaman video, fitur ini langsung jalan
        # tanpa perlu diubah lagi.
        if music and str(music).strip():
            _do_sound(page, str(music).strip())
            time.sleep(random.uniform(1, 2))

        if ai_generate_content:
            _do_ai_generate(page)
            time.sleep(random.uniform(1, 2))

        # ── STEP 5: Tambah produk (affiliate/seller) ──────────────────────────
        if upload_type in ("affiliate", "seller"):
            if not product_id:
                print(f"[WARN] product_id kosong untuk akun {upload_type} — skip tambah produk.")
            else:
                print(f"[STEP 5] Menambahkan produk: {product_name or product_id}")
                ok = _add_product(page, product_id, upload_type, product_name, headless)
                if not ok:
                    print(f"[WARN] Gagal tambah produk '{product_id}' — lanjut tanpa produk.")
                    print(f"[WARN] Cek log [PRODUCT DEBUG] untuk class element yang tersedia.")
                    _close_product_modal(page)
                time.sleep(random.uniform(2, 3))

        # ── STEP 6: Post atau Schedule di TikTok Studio ─────────────────────
        time.sleep(random.uniform(1, 2))



        if schedule_time:
            # Ada schedule_time → klik tombol Schedule di TikTok Studio,
            # isi tanggal/waktu, lalu klik Schedule untuk konfirmasi
            print(f"[STEP 6] Mode SCHEDULE — jadwal: {schedule_time}")
            ok = _do_schedule(page, schedule_time)
            if not ok:
                raise Exception("Gagal mengatur schedule di TikTok Studio.")

            # _do_schedule kini sudah memverifikasi sendiri sebelum mengaku
            # berhasil, jadi di sini cukup penegasan singkat. Dulu nilainya 60
            # detik dan dijalankan bahkan setelah jadwal jelas gagal — itulah
            # sumber "stag" panjang sebelum browser tertutup.
            success = _wait_post_success(page, timeout=25)
            if not success:
                raise Exception("Gagal konfirmasi schedule. TikTok mungkin menolak jadwal ini atau terjadi error.")
                
            print(f"[SUCCESS] Video dijadwalkan: {os.path.basename(video_path)} @ {schedule_time}")
        else:
            # Tidak ada schedule_time → Post langsung
            print(f"[STEP 6] Mode INSTANT — klik Post...")
            
            for attempt in range(3):
                # Jika sudah sukses (URL berubah), berhenti
                if "manage" in page.url.lower() or "content" in page.url.lower():
                    break

                posted = _click_post(page)
                if not posted:
                    # Jika tombol tidak ketemu, mungkin sudah terproses atau ada popup
                    if "manage" in page.url.lower() or "content" in page.url.lower(): break
                    if _dismiss_content_restricted(page): continue
                    time.sleep(2)
                    continue

                # Cek popup "Content may be restricted"
                had_popup = _dismiss_content_restricted(page)
                if had_popup:
                    print("[STEP 6] Popup terdeteksi & ditutup, klik Post ulang...")
                    continue
                
                # Tunggu sebentar dan cek apakah masih di halaman yang sama
                time.sleep(3)
                if "manage" in page.url.lower() or "content" in page.url.lower():
                    break

            # CRITICAL: Cek apakah benar-benar berhasil dipost
            success = _wait_post_success(page, timeout=60)
            if not success:
                raise Exception("Gagal konfirmasi upload. TikTok mungkin menolak video ini atau terjadi error.")
            
            set_p(95)
            print(f"[SUCCESS] Upload selesai: {os.path.basename(video_path)}")

        time.sleep(5)
        page.quit()
        return True

    except Exception as e:
        print(f"[UPLOAD ERROR] {e}")
        try:
            print(f"[UPLOAD ERROR DEBUG] URL saat ini: {page.url}")
        except Exception:
            pass
        try:
            page.quit()
        except Exception:
            pass
        return False



# ── Product helper ────────────────────────────────────────────────────────────

def _add_product(page, product_id: str, upload_type: str, product_name: str = None, headless: bool = False) -> bool:
    """
    Flow tambah produk keranjang kuning untuk seller & affiliate:
    1. Klik tombol "+ Add" di halaman upload
    2. Muncul dialog "Add link" dengan dropdown Products → klik Next (merah)
    3. Muncul panel "Add product links" → ketik ID di search box
    4. Klik radio button produk yang muncul
    5. Klik Next
    6. Muncul dialog "Product name" → klik Add
    """
    print(f"[PRODUCT] Mulai tambah produk: {product_id} ({upload_type})")

    # ── STEP A: Klik tombol "+ Add" di halaman upload ─────────────────────────
    add_btn = None
    try:
        all_btns = page.eles('tag:button', timeout=5)
        add_candidates = []
        for btn in all_btns:
            txt = (btn.text or '').strip()
            de2 = (btn.attr('data-e2e') or '').lower()
            if de2 in ('post_video_button', 'save_draft_button', 'discard_post_button'):
                continue
            if txt.lower() in ('add', '+ add', '+add', 'add products', 'tambah produk'):
                add_candidates.append(btn)
                print(f"[PRODUCT] Kandidat Add: '{txt}' | data-e2e='{de2}'")
        if add_candidates:
            add_btn = add_candidates[0]
            print(f"[PRODUCT] Tombol +Add dipilih dari {len(add_candidates)} kandidat.")
    except Exception as e:
        print(f"[PRODUCT] Scan Add gagal: {e}")

    if not add_btn:
        print("[PRODUCT] Tombol +Add tidak ditemukan.")
        return False

    add_btn.click()
    print("[PRODUCT] Tombol +Add diklik.")
    time.sleep(random.uniform(2, 3))

    # ── STEP B: Dialog "Add link" muncul → klik Next (tombol merah) ──────────
    # Dari screenshot: dialog punya tombol "Cancel" dan "Next" (merah)
    # Next di sini untuk masuk ke panel "Add product links"
    print("[PRODUCT] Mencari tombol Next di dialog Add link...")
    next_clicked = False
    try:
        # Cari tombol Next yang ada di dalam dialog/modal
        for sel in [
            'xpath://div[contains(@class,"modal") or contains(@class,"dialog") or contains(@class,"Modal")]//button[contains(.,"Next")]',
            'xpath://div[contains(@role,"dialog")]//button[contains(.,"Next")]',
            'xpath://button[text()="Next"]',
            'xpath://button[normalize-space()="Next"]',
        ]:
            btn = page.ele(sel, timeout=3)
            if btn:
                btn.click()
                next_clicked = True
                print(f"[PRODUCT] Next (dialog Add link) diklik via: {sel}")
                break
    except Exception:
        pass

    if not next_clicked:
        # Fallback scan
        _click_btn_by_text(page, ['Next', 'Lanjut'], "[PRODUCT] Next (dialog Add link)")
    time.sleep(random.uniform(2, 3))

    # ── STEP C: Panel "Add product links" — ketik ID di search box ──────────────
    # Cari search input secara global di page terlebih dahulu
    search_inp = None
    search_selectors = [
        'xpath://input[@placeholder="Search products"]',
        'xpath://input[contains(@placeholder,"Search products")]',
        'xpath://input[contains(@placeholder,"Search")]',
        'xpath://input[@placeholder="Cari produk"]',
        'xpath://input[contains(@placeholder,"Cari")]',
        'xpath://input[@type="search"]',
        'xpath://input[@type="text"]',
    ]
    print("[PRODUCT] Menunggu panel Add product links muncul...")
    for attempt in range(8):  # max 16 detik
        for sel in search_selectors:
            try:
                el = page.ele(sel, timeout=1)
                if el:
                    search_inp = el
                    print(f"[PRODUCT] Search input ditemukan secara global (attempt {attempt+1}): {sel}")
                    break
            except Exception:
                continue
        if search_inp:
            break
        print(f"[PRODUCT] Menunggu panel... ({attempt+1}/8)")
        time.sleep(2)

    if not search_inp:
        print("[PRODUCT] Search input tidak ditemukan.")
        return False

    # Dapatkan container modal/dialog/drawer produk dengan menelusuri parent dari search_inp
    dialog_container = None
    try:
        dialog_container = page.run_js("""
            var el = arguments[0];
            while(el){
                if(el.className && (
                    el.className.includes('modal') || 
                    el.className.includes('dialog') || 
                    el.className.includes('drawer') || 
                    el.className.includes('Modal') || 
                    el.className.includes('Drawer') ||
                    el.className.includes('panel') ||
                    el.className.includes('Panel')
                )) {
                    return el;
                }
                el = el.parentElement;
            }
            return null;
        """, search_inp)
    except Exception as e_parent:
        print(f"[PRODUCT] Gagal mencari parent dialog: {e_parent}")

    if dialog_container:
        print("[PRODUCT] Parent dialog/drawer container berhasil ditemukan.")
    else:
        print("[PRODUCT] Parent dialog/drawer container tidak ditemukan, menggunakan root page.")
        dialog_container = page

    # Klik search input dan ketik ID produk via CDP
    search_inp.click()
    time.sleep(0.8)

    # Clear dulu
    try:
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyDown", "modifiers": 2,
            "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65})
        time.sleep(0.1)
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyUp", "modifiers": 2,
            "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65})
        time.sleep(0.1)
        page.run_cdp("Input.dispatchKeyEvent", **{
            "type": "keyDown",
            "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        time.sleep(0.2)
    except Exception:
        pass

    # Ketik ID via CDP insertText (paling reliable untuk React)
    try:
        page.run_cdp("Input.insertText", text=product_id)
        print(f"[PRODUCT] ID diketik via insertText: {product_id}")
    except Exception as e1:
        print(f"[PRODUCT] insertText gagal: {e1}, fallback char-by-char...")
        for char in product_id:
            try:
                page.run_cdp("Input.dispatchKeyEvent",
                             type="char", text=char, unmodifiedText=char)
                time.sleep(0.04)
            except Exception:
                pass

    time.sleep(0.5)
    # Trigger React onChange
    page.run_js(
        "var el=document.activeElement;"
        "if(el){"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));}")
    time.sleep(0.5)

    # Klik icon search (🔍) untuk trigger search
    try:
        search_icon = dialog_container.ele(
            'xpath:.//input[contains(@placeholder,"Search") or contains(@placeholder,"Cari")]'
            '/following-sibling::*[1]', timeout=2)
        if search_icon:
            search_icon.click()
            print("[PRODUCT] Icon search diklik.")
    except Exception:
        pass

    # Tekan Enter sebagai backup trigger search
    try:
        page.run_cdp("Input.dispatchKeyEvent",
                     type="keyDown", key="Return",
                     code="Enter", windowsVirtualKeyCode=13)
        time.sleep(0.1)
        page.run_cdp("Input.dispatchKeyEvent",
                     type="keyUp", key="Return",
                     code="Enter", windowsVirtualKeyCode=13)
        print("[PRODUCT] Enter ditekan untuk trigger search.")
    except Exception:
        pass
    print(f"[PRODUCT] ID/Link dimasukkan: {product_id}")

    # ── STEP D: Tunggu hasil search + klik radio/checkbox atau tombol Add ───────────────────
    print("[PRODUCT] Menunggu hasil search muncul...")
    product_found = False

    for wait in range(10):  # max 20 detik
        time.sleep(2)

        # Cek no result
        try:
            no_res = dialog_container.ele(
                'xpath:.//*[contains(text(),"No result") or '
                'contains(text(),"No products found")]', timeout=1)
            if no_res:
                print("[PRODUCT] TikTok: tidak ada hasil search.")
                return False
        except Exception:
            pass

        # Cari radio/checkbox atau tombol "Add" / "Tambah" baris produk di dalam dialog_container
        try:
            radio = None
            try:
                radio = dialog_container.ele(
                    'xpath:.//input[@type="radio" or @type="checkbox"][contains(@class,"TUXRadio") or contains(@class,"TUXCheckbox") or contains(@class,"Checkbox")]',
                    timeout=1)
            except Exception:
                pass

            # Fallback 1: cari semua radio/checkbox di dalam dialog, skip yang name=postSchedule
            if not radio:
                inputs = dialog_container.eles('xpath:.//input[@type="radio" or @type="checkbox"]', timeout=1)
                for r in inputs:
                    if (r.attr('name') or '') != 'postSchedule':
                        radio = r
                        break

            # Fallback 2: cari element dengan role="checkbox" atau role="radio" di dalam dialog
            if not radio:
                try:
                    radio = dialog_container.ele('xpath:.//*[@role="radio" or @role="checkbox"]', timeout=1)
                except Exception:
                    pass

            # Fallback 3: cari generic element dengan class yang merepresentasikan selection
            if not radio:
                try:
                    for el in dialog_container.eles('xpath:.//*[contains(@class,"TUXRadio") or contains(@class,"TUXCheckbox") or contains(@class,"Checkbox") or contains(@class,"radio")]'):
                        cl = (el.attr('class') or '').lower()
                        nm = (el.attr('name') or '').lower()
                        if 'schedule' not in cl and 'schedule' not in nm:
                            radio = el
                            break
                except Exception:
                    pass

            # Fallback 4: Jika tidak ada radio/checkbox sama sekali, cari tombol "Add" atau "Tambah" baris produk
            add_row_btn = None
            if not radio:
                try:
                    # Ambil semua tombol di dialog_container
                    btns = dialog_container.eles('tag:button')
                    for b in btns:
                        txt = (b.text or '').strip()
                        aria = (b.attr('aria-label') or '').strip()
                        label = (txt or aria).lower()
                        if label in ('add', 'tambah') or 'add product' in label or 'tambah produk' in label:
                            add_row_btn = b
                            break
                except Exception:
                    pass

            # Fallback 5: cari card/baris hasil yang memuat product_id/product_name,
            # lalu klik kontrol pilihan di dalamnya.
            dom_pick = None
            if not radio and not add_row_btn:
                try:
                    dom_pick = page.run_js("""
                        var productId = String(arguments[0] || '').trim();
                        var productName = String(arguments[1] || '').trim().toLowerCase();

                        function visible(el) {
                            if (!el) return false;
                            var r = el.getBoundingClientRect();
                            var st = window.getComputedStyle(el);
                            return r.width > 2 && r.height > 2 && st.display !== 'none' &&
                                st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.05;
                        }
                        function textOf(el) {
                            return (el.innerText || el.textContent || '').trim();
                        }
                        function isBadButton(btn) {
                            var t = textOf(btn).toLowerCase();
                            var a = (btn.getAttribute('aria-label') || '').toLowerCase();
                            return ['cancel','batal','next','lanjut','back','kembali','close','tutup']
                                .some(function(x) { return t === x || a === x; });
                        }

                        var roots = Array.from(document.querySelectorAll(
                            '[class*=modal],[class*=dialog],[class*=drawer],[class*=panel],[role=dialog]'
                        )).filter(visible);
                        if (!roots.length) roots = [document.body];

                        var matched = null;
                        for (var root of roots) {
                            var nodes = Array.from(root.querySelectorAll('*')).filter(visible);
                            for (var node of nodes) {
                                var txt = textOf(node);
                                var low = txt.toLowerCase();
                                if ((productId && txt.includes(productId)) ||
                                    (productName && low.includes(productName))) {
                                    matched = node;
                                    break;
                                }
                            }
                            if (matched) break;
                        }
                        if (!matched) return null;

                        var card = matched;
                        for (var i = 0; i < 8 && card; i++) {
                            var controls = Array.from(card.querySelectorAll(
                                'input[type=radio],input[type=checkbox],[role=radio],[role=checkbox],button'
                            )).filter(function(el) { return visible(el) && !isBadButton(el); });
                            if (controls.length) {
                                var ctrl = controls[0];
                                ctrl.scrollIntoView({block:'center', inline:'center'});
                                ctrl.click();
                                ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true,cancelable:true}));
                                ctrl.dispatchEvent(new MouseEvent('mouseup', {bubbles:true,cancelable:true}));
                                ctrl.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true}));
                                return 'control:' + ctrl.tagName + ':' + textOf(ctrl).slice(0, 30);
                            }
                            card = card.parentElement;
                        }

                        matched.scrollIntoView({block:'center', inline:'center'});
                        matched.click();
                        return 'matched-node';
                    """, product_id, product_name or "")
                except Exception as e_dom:
                    print(f"[PRODUCT] DOM fallback gagal: {e_dom}")

            # Aksi klik sesuai apa yang ditemukan
            if radio:
                print(f"[PRODUCT] Radio/Checkbox button ditemukan (detik ke-{(wait+1)*2})")
                clicked = page.run_js("""
                    var input = arguments[0];
                    var fiberKey = Object.keys(input).find(function(k){
                        return k.startsWith('__reactFiber') ||
                               k.startsWith('__reactInternalInstance');
                    });
                    if (fiberKey) {
                        var node = input[fiberKey];
                        for (var i = 0; i < 15; i++) {
                            if (!node) break;
                            var props = node.memoizedProps || node.pendingProps;
                            if (props) {
                                if (props.onClick) {
                                    props.onClick({bubbles:true,preventDefault:function(){}});
                                    return 'fiber-onClick';
                                }
                                if (props.onChange) {
                                    props.onChange({target:input,currentTarget:input,
                                        bubbles:true,preventDefault:function(){}});
                                    return 'fiber-onChange';
                                }
                            }
                            node = node.return;
                        }
                    }

                    var el = input.parentElement;
                    for (var j = 0; j < 6; j++) {
                        if (!el) break;
                        if (el.tagName === 'LABEL') {
                            el.click();
                            return 'label-click';
                        }
                        if (el.tagName === 'DIV' && (
                            el.className.indexOf('TUXRadio') >= 0 ||
                            el.className.indexOf('Radio__root') >= 0 ||
                            el.className.indexOf('radio') >= 0 ||
                            el.className.indexOf('TUXCheckbox') >= 0 ||
                            el.className.indexOf('Checkbox__root') >= 0 ||
                            el.className.indexOf('checkbox') >= 0)) {
                            el.click();
                            return 'div-click';
                        }
                        el = el.parentElement;
                    }

                    var nativeSet = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'checked').set;
                    if (nativeSet) {
                        nativeSet.call(input, true);
                    } else {
                        input.checked = true;
                    }
                    input.dispatchEvent(new MouseEvent('click',
                        {bubbles:true, cancelable:true}));
                    input.dispatchEvent(new Event('change', {bubbles:true}));
                    return 'native';
                """, radio)

                print(f"[PRODUCT] Radio/Checkbox klik method: {clicked}")
                time.sleep(1.0)

                CHECK_JS = (
                    "return !!(document.querySelector('[class*=checked]')"
                    "|| document.querySelector('[class*=Radio__root--checked-true]')"
                    "|| document.querySelector('[class*=Radio__input--checked-true]')"
                    "|| document.querySelector('[class*=Checkbox__root--checked-true]')"
                    "|| document.querySelector('[class*=Checkbox__input--checked-true]')"
                    "|| document.querySelector('[class*=TUXRadioStandalone--checked]')"
                    "|| document.querySelector('[class*=TUXCheckbox--checked]'));"
                )
                pre_check = page.run_js(CHECK_JS)

                if not pre_check:
                    print("[PRODUCT] Fiber tidak trigger visual, klik langsung...")
                    try:
                        radio.click()
                        time.sleep(1.0)
                    except Exception as e:
                        print(f"[PRODUCT] Direct click gagal: {e}")

                is_checked = page.run_js(CHECK_JS)
                print(f"[PRODUCT] Radio/Checkbox checked: {is_checked}")

                if not is_checked:
                    print(f"[PRODUCT] Radio/Checkbox belum checked, coba lagi...")
                    continue

                product_found = True
                break

            elif add_row_btn:
                print(f"[PRODUCT] Tombol Add baris produk ditemukan, klik langsung (detik ke-{(wait+1)*2})")
                add_row_btn.click()
                time.sleep(1.5)
                product_found = True
                break

            elif dom_pick:
                print(f"[PRODUCT] DOM fallback memilih produk: {dom_pick}")
                time.sleep(1.5)
                product_found = True
                break

        except Exception as e:
            print(f"[PRODUCT] Error klik radio/checkbox atau Add button (attempt {wait+1}): {e}")

        if not radio and not add_row_btn:
            print(f"[PRODUCT] Menunggu hasil... ({(wait+1)*2}/20 detik)")

    if not product_found:
        print("[PRODUCT] Radio/Checkbox atau Add button tidak ditemukan setelah 20 detik.")
        print("[PRODUCT DEBUG] Gagal menemukan elemen pemilihan produk. Mari print element classes di dialog:")
        try:
            dialog_classes = page.run_js("""
                var dialog = document.querySelector('[class*=modal],[class*=dialog],[class*=Modal]');
                if (!dialog) return 'No dialog found';
                var els = dialog.querySelectorAll('*');
                var info = [];
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    if (el.className || el.tagName === 'INPUT' || el.tagName === 'BUTTON') {
                        var text = (el.innerText || el.textContent || '').trim().substring(0, 60);
                        info.push(el.tagName + ' (class=' + el.className + ', type=' + el.getAttribute('type') + ', role=' + el.getAttribute('role') + ', aria=' + el.getAttribute('aria-label') + ', text=' + text + ')');
                    }
                }
                return info.slice(0, 100).join('\\n');
            """)
            print(dialog_classes)
        except Exception as e_dbg:
            print(f"[PRODUCT DEBUG] Gagal dump dialog: {e_dbg}")
        return False

    time.sleep(2)
    print("[PRODUCT] Produk dipilih.")

    # ── STEP E: Klik Next / Add untuk masuk ke konfirmasi Nama Produk ────────
    # Jika kita mengklik radio/checkbox, kita harus mengklik Next/Add untuk memicu popup ganti nama
    # Tapi jika kita mengklik tombol Add di baris produk secara langsung, popup ganti nama sudah terbuka
    has_name_input = page.run_js("""
        var input = document.querySelector('[class*=modal] input, [class*=dialog] input, .TUXTextInputCore-input');
        if (!input) return false;
        if (input.placeholder && (input.placeholder.includes('Search') || input.placeholder.includes('Cari'))) return false;
        var r = input.getBoundingClientRect();
        return r.height > 5;
    """)

    if not has_name_input:
        print("[PRODUCT] Mencari tombol untuk lanjut (Next/Add) ke dialog ganti nama...")
        proc_btn = None
        for _ in range(15):
            try:
                all_btns = dialog_container.eles('tag:button', timeout=1)
                candidates = [b for b in all_btns if (b.text or '').strip() in ('Next', 'Add', 'Tambah')]
                if candidates:
                    proc_btn = candidates[-1]
                    print(f"[PRODUCT] Tombol lanjut ditemukan: '{proc_btn.text}'")
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if not proc_btn:
            try:
                all_btns = page.eles('tag:button', timeout=1)
                candidates = [b for b in all_btns if (b.text or '').strip() in ('Next', 'Add', 'Tambah')]
                if candidates:
                    proc_btn = candidates[-1]
                    print(f"[PRODUCT] Tombol lanjut ditemukan via page fallback: '{proc_btn.text}'")
            except Exception:
                pass

        if proc_btn:
            proc_btn.click()
            print(f"[PRODUCT] Tombol '{proc_btn.text}' diklik, menunggu popup konfirmasi ganti nama...")
            time.sleep(1.5)
        else:
            print("[PRODUCT] WARNING: Tombol lanjut tidak ditemukan, coba lanjut...")

    time.sleep(1.5)

    # ── STEP E.1: Ganti nama produk via JS langsung (bypass DrissionPage ele) ──
    if product_name:
        print(f"[PRODUCT] Mencoba mengganti nama produk menjadi: {product_name}")
        try:
            replaced = page.run_js("""
                var dialogs = document.querySelectorAll('[class*=modal],[class*=dialog],[class*=Modal]');
                var input = null;
                if (dialogs.length > 0) {
                    for (var i = dialogs.length - 1; i >= 0; i--) {
                        var inp = dialogs[i].querySelector('.TUXTextInputCore-input, input[type=text], input');
                        if (inp && (!inp.placeholder || (!inp.placeholder.includes('Search') && !inp.placeholder.includes('Cari')))) {
                            input = inp;
                            break;
                        }
                    }
                }
                if (!input) {
                    var inputs = document.querySelectorAll('.TUXTextInputCore-input, input');
                    for (var j = 0; j < inputs.length; j++) {
                        var inp2 = inputs[j];
                        if (inp2 && (!inp2.placeholder || (!inp2.placeholder.includes('Search') && !inp2.placeholder.includes('Cari')))) {
                            input = inp2;
                            break;
                        }
                    }
                }
                if (!input) return 'NOT_FOUND';

                input.focus();

                var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(input, '');
                input.dispatchEvent(new Event('input', { bubbles: true }));

                nativeSetter.call(input, arguments[0]);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));

                return input.value;
            """, product_name)

            print(f"[PRODUCT] JS replace result: {replaced}")

            if replaced == 'NOT_FOUND':
                print("[PRODUCT] ERROR: Input ganti nama tidak ditemukan via JS.")
            else:
                time.sleep(0.3)
                page.run_cdp("Input.dispatchKeyEvent", type="keyDown", modifiers=2, key="a", code="KeyA", windowsVirtualKeyCode=65)
                time.sleep(0.05)
                page.run_cdp("Input.dispatchKeyEvent", type="keyUp", modifiers=2, key="a", code="KeyA", windowsVirtualKeyCode=65)
                time.sleep(0.05)
                page.run_cdp("Input.insertText", text=product_name)
                time.sleep(0.5)
                print(f"[PRODUCT] Nama produk diisi: {product_name}")

        except Exception as e_name:
            print(f"[PRODUCT] Gagal ganti nama produk: {e_name}")

    # ── STEP F: Popup baru → klik Add ────────────────────────────────────────
    print("[PRODUCT] Menunggu tombol Add final di popup...")
    add_btn = None
    for _ in range(20):
        time.sleep(0.5)
        try:
            all_btns = page.eles('tag:button', timeout=1)
            for btn in all_btns:
                txt = (btn.text or '').strip()
                if txt in ('Add', 'Tambah'):
                    is_in_popup = page.run_js("""
                        var el = arguments[0];
                        var dialogs = document.querySelectorAll('[class*=modal],[class*=dialog],[class*=Modal]');
                        if (dialogs.length > 0) {
                            var lastDialog = dialogs[dialogs.length - 1];
                            if (lastDialog.contains(el)) return true;
                        }
                        while(el){
                            if(el.className && (el.className.includes('modal') || el.className.includes('dialog'))) return true;
                            el = el.parentElement;
                        }
                        return false;
                    """, btn)
                    if is_in_popup:
                        add_btn = btn
                        print(f"[PRODUCT] Tombol Add final ditemukan di popup!")
                        break
            if add_btn:
                break
        except Exception:
            pass

    if not add_btn:
        try:
            candidates = [b for b in page.eles('tag:button') if (b.text or '').strip() in ('Add', 'Tambah')]
            if candidates:
                add_btn = candidates[-1]
                print("[PRODUCT] Tombol Add final dipilih via fallback (button terakhir).")
        except Exception: pass

    if not add_btn:
        print("[PRODUCT] Tombol Add final tidak ditemukan.")
        return False

    add_btn.click()
    print(f"[PRODUCT] Produk berhasil ditambahkan: {product_id}")
    time.sleep(random.uniform(2, 3))
    return True


def _click_btn_by_text(page, texts: list, label: str = "") -> bool:
    """Klik tombol berdasarkan teks, dengan multi-selector fallback."""
    texts_lower = [t.lower() for t in texts]
    for txt in texts:
        for sel in [
            f'xpath://button[text()="{txt}"]',
            f'xpath://button[normalize-space()="{txt}"]',
        ]:
            try:
                btn = page.ele(sel, timeout=2)
                if btn:
                    btn.click()
                    print(f"{label} diklik: '{txt}'")
                    return True
            except Exception:
                continue
    # Fallback scan
    try:
        for btn in page.eles('tag:button', timeout=3):
            if (btn.text or '').strip().lower() in texts_lower:
                btn.click()
                print(f"{label} diklik via scan: '{btn.text}'")
                return True
    except Exception:
        pass
    print(f"{label} tidak ditemukan.")
    return False


# ═══════════════════════════════════════════════════════════════════════════
#  UNGGAH FOTO (carousel) LEWAT WEB
#
#  TikTok Studio punya HALAMAN TERPISAH untuk foto:
#      /tiktokstudio/upload?from=webapp&tab=photo
#  Halaman upload video menolak gambar (accept="video/*", multiple=false),
#  sedangkan halaman ini menerima banyak gambar sekaligus:
#      accept="image/jpg,image/jpeg,image/png,image/webp"  multiple=true
#      "Upload up to 35 photos per post"
#
#  CATATAN PENTING: menu "Photos" hanya muncul di sebagian akun — TikTok
#  merilisnya bertahap. Akun yang belum mendapatkannya diarahkan balik ke
#  halaman video; itu diperiksa di bawah supaya kegagalannya jelas, bukan
#  diam-diam terunggah sebagai video.
#
#  Komponen caption, lokasi, dan jadwal di halaman ini SAMA PERSIS dengan
#  halaman video (sudah diperiksa: div.poi-search input.Select__searchInput
#  dan radio schedule/post_now dua-duanya ada), jadi _fill_caption_hashtag,
#  _do_location, dan _do_schedule dipakai ulang apa adanya.
# ═══════════════════════════════════════════════════════════════════════════

TIKTOK_PHOTO_URL = 'https://www.tiktok.com/tiktokstudio/upload?from=webapp&tab=photo'

EKSTENSI_FOTO = ('.jpg', '.jpeg', '.png', '.webp')


def foto_dari_kolom(nilai: str) -> list:
    """Terjemahkan kolom `photos` CSV jadi daftar berkas.

    Menerima path FOLDER (semua gambar di dalamnya, urut nama) atau beberapa
    berkas dipisah '|'. Pemisahnya '|' dan bukan ';' karena titik koma sudah
    dipakai sebagai pemisah kolom CSV.
    """
    teks = (nilai or '').strip().strip('"')
    if not teks:
        return []
    if os.path.isdir(teks):
        return [os.path.join(teks, f) for f in sorted(os.listdir(teks))
                if f.lower().endswith(EKSTENSI_FOTO)]
    hasil = []
    for bagian in teks.split('|'):
        p = bagian.strip().strip('"')
        if p and os.path.isfile(p) and p.lower().endswith(EKSTENSI_FOTO):
            hasil.append(p)
    return hasil


def _halaman_foto_siap(page) -> bool:
    """Pastikan yang terbuka benar-benar halaman FOTO, bukan halaman video."""
    try:
        return bool(page.run_js(
            "var i=document.querySelector('input[type=file]');"
            "if(!i)return false;"
            "return (i.accept||'').indexOf('image')>=0 && i.multiple===true;"))
    except Exception:
        return False


def _tutup_onboarding(page):
    """Tutup popup pengenalan ("Got it", "Phone mode") yang menutupi panel."""
    try:
        page.run_js(
            "document.querySelectorAll('button,div').forEach(function(b){"
            "  if(b.children.length>1)return;"
            "  var t=(b.innerText||'').trim();"
            "  if(/^(Got it|Mengerti|OK|Oke|Skip|Lewati)$/i.test(t) && b.offsetParent)"
            "    b.dispatchEvent(new MouseEvent('click',{bubbles:true}));"
            "});")
        time.sleep(2)
    except Exception:
        pass


def _do_sound(page, nama_musik: str) -> bool:
    """Pasang musik. Menangani DUA bentuk panel yang berbeda.

    LETAK DAN NAMA TOMBOLNYA BERBEDA per halaman:
      - halaman FOTO  : "Add sound" di kolom form kiri. Membuka MODAL sederhana
                        berisi kotak "Search sounds" dan tombol "Use" per baris.
      - halaman VIDEO : "Sounds" di panel KANAN bawah pratinjau. Membuka
                        EDITOR VIDEO WEB penuh (timeline, rail Text/Sounds/
                        Templates, tombol Cancel/Save di atas). Barisnya TIDAK
                        punya tombol "Use" - lagunya ditambahkan dengan
                        mengklik barisnya, lalu ditutup lewat tombol "Save".

    Versi sebelumnya hanya mengenal bentuk pertama, dan sempat salah
    menyimpulkan halaman video tidak punya fitur musik sama sekali.
    """
    if not (nama_musik or '').strip():
        return True
    print("[SOUND] Mencari musik: " + nama_musik)
    try:
        pos = page.run_js(
            "var t=null;"
            "document.querySelectorAll('button,div,span').forEach(function(e){"
            "  if(t)return; if(e.children.length>2)return;"
            "  var s=(e.innerText||'').trim();"
            "  if(/^[+]?\\s*(Add sound|Tambah suara|Sounds|Suara)$/i.test(s)) t=e;"
            "});"
            "if(!t)return null;"
            "t.scrollIntoView({block:'center'});"
            "var r=t.getBoundingClientRect();"
            "return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};")
        if not pos:
            print("[SOUND] Tombol musik tidak ada di halaman ini - dilewati.")
            return False
        for tipe in ("mousePressed", "mouseReleased"):
            page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                         x=pos['x'], y=pos['y'], button="left", clickCount=1)
        time.sleep(6)
        _tutup_onboarding(page)

        # Kotak pencarian. Kotak lokasi ("Search locations") sengaja
        # dikecualikan: letaknya lebih dulu di DOM dan pernah membuat nama lagu
        # terketik ke sana, merusak langkah lokasi.
        fokus = page.run_js(
            "var k=null;"
            "document.querySelectorAll('input').forEach(function(i){"
            "  if(k||!i.offsetParent)return;"
            "  var p=(i.placeholder||'').toLowerCase();"
            "  if(p.indexOf('location')>=0||p.indexOf('lokasi')>=0)return;"
            "  if(p.indexOf('sound')>=0||p.indexOf('music')>=0"
            "     ||p.indexOf('suara')>=0||p.indexOf('lagu')>=0) k=i;"
            "});"
            "if(!k)return null;"
            "k.focus();"
            "var s=Object.getOwnPropertyDescriptor("
            "  window.HTMLInputElement.prototype,'value').set;"
            "s.call(k, " + json.dumps(nama_musik) + ");"
            "k.dispatchEvent(new Event('input',{bubbles:true}));"
            "return (k.placeholder||'');")
        if not fokus:
            print("[SOUND] Kotak pencarian musik tidak ditemukan - dilewati.")
            _tutup_panel_sound(page)
            return False
        print("[SOUND] Mengetik di kotak: " + str(fokus))
        page.run_cdp("Input.dispatchKeyEvent", type="keyDown", key="Enter",
                     code="Enter", windowsVirtualKeyCode=13)
        page.run_cdp("Input.dispatchKeyEvent", type="keyUp", key="Enter",
                     code="Enter", windowsVirtualKeyCode=13)
        time.sleep(8)
        _tutup_onboarding(page)

        import re as _re
        kata = [w.lower() for w in _re.findall(r'\w+', nama_musik) if len(w) > 2]
        minimal = max(1, len(kata) // 2)

        # BENTUK 1 (halaman foto): tiap baris punya tombol "Use".
        baris = page.run_js(
            "var o=[];"
            "document.querySelectorAll('button,div').forEach(function(b){"
            "  if(b.children.length>1)return;"
            "  var t=(b.innerText||'').trim();"
            "  if(!/^(Use|Pakai|Gunakan)$/i.test(t))return;"
            "  if(!b.offsetParent)return;"
            "  var row=b.parentElement;"
            "  for(var i=0;i<4 && row && row.innerText.trim().length<8;i++) row=row.parentElement;"
            "  var r=b.getBoundingClientRect();"
            "  o.push({judul:(row?row.innerText:'').replace(/\\n/g,' ').trim().slice(0,90),"
            "          x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2), pakai:1});"
            "});"
            "return o.slice(0,20);") or []

        # BENTUK 2 (editor video): tidak ada tombol Use — barisnya sendiri
        # yang diklik. Baris dikenali dari pola "judul + mm:ss + artis".
        if not baris:
            baris = page.run_js(
                "var o=[];"
                "document.querySelectorAll('div,li').forEach(function(e){"
                "  if(!e.offsetParent)return;"
                "  if(e.querySelectorAll('div,li').length>4)return;"
                "  var t=(e.innerText||'').replace(/\\n/g,' ').trim();"
                "  if(t.length<6||t.length>90)return;"
                "  if(!/\\d{1,2}:\\d{2}/.test(t))return;"
                "  var r=e.getBoundingClientRect();"
                "  if(r.width<150||r.height<30||r.height>140)return;"
                "  o.push({judul:t.slice(0,90),"
                "          x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2), pakai:0});"
                "});"
                "return o.slice(0,20);") or []

        if not baris:
            print("[SOUND] Tidak ada hasil pencarian musik - dilewati.")
            _tutup_panel_sound(page)
            return False

        pilih = None
        for b in baris:
            rendah = (b.get('judul') or '').lower()
            if sum(1 for w in kata if w in rendah) >= minimal:
                pilih = b
                break
        if not pilih:
            print("[SOUND] Tidak ada hasil yang cocok dengan '" + nama_musik
                  + "'. Musik SENGAJA dilewati daripada memasang lagu yang salah.")
            print("[SOUND] Yang muncul: "
                  + ' | '.join((b.get('judul') or '')[:40] for b in baris[:3]))
            _tutup_panel_sound(page)
            return False

        print("[SOUND] Dipilih: " + pilih['judul'][:70])
        for tipe in ("mousePressed", "mouseReleased"):
            page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                         x=pilih['x'], y=pilih['y'], button="left", clickCount=1)
        time.sleep(6)

        # Editor video harus disimpan supaya musiknya ikut terbawa kembali ke
        # form upload. Modal halaman foto tidak punya tombol ini.
        if _klik_tombol_terlihat_uploader(page, ['Save', 'Simpan']):
            print("[SOUND] Editor disimpan (tombol Save).")
            time.sleep(8)
        else:
            _tutup_panel_sound(page)
        return True
    except Exception as e:
        print("[SOUND] Error: " + str(e))
        try:
            _tutup_panel_sound(page)
        except Exception:
            pass
        return False


def _klik_tombol_terlihat_uploader(page, teks_list) -> bool:
    """Klik tombol yang teksnya PERSIS salah satu dari `teks_list`."""
    try:
        pos = page.run_js(
            "var cari=" + json.dumps([t.lower() for t in teks_list]) + ";"
            "var t=null;"
            "document.querySelectorAll('button,div').forEach(function(b){"
            "  if(t||b.children.length>1)return;"
            "  if(!b.offsetParent)return;"
            "  var s=(b.innerText||'').trim().toLowerCase();"
            "  if(cari.indexOf(s)>=0) t=b;"
            "});"
            "if(!t)return null;"
            "var r=t.getBoundingClientRect();"
            "return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};")
        if not pos:
            return False
        for tipe in ("mousePressed", "mouseReleased"):
            page.run_cdp("Input.dispatchMouseEvent", type=tipe,
                         x=pos['x'], y=pos['y'], button="left", clickCount=1)
        return True
    except Exception:
        return False


def _tutup_panel_sound(page):
    """Tutup panel Sounds kalau masih terbuka.

    Panel ini menutupi kolom Location di bawahnya; kalau dibiarkan terbuka,
    langkah lokasi berikutnya akan mengklik ke panel ini.
    """
    try:
        page.run_js(
            "var judul=null;"
            "document.querySelectorAll('div,span,h1,h2,h3').forEach(function(e){"
            "  if(judul)return; if(e.children.length>1)return;"
            "  if(/^(Sounds|Suara)$/i.test((e.innerText||'').trim())) judul=e;"
            "});"
            "if(!judul)return false;"
            "var kotak=judul.parentElement;"
            "for(var i=0;i<4 && kotak;i++){"
            "  var x=kotak.querySelector('svg,button');"
            "  if(x && kotak.getBoundingClientRect().width>300){"
            "    var b=kotak.querySelectorAll('button,svg');"
            "    if(b.length){ b[b.length-1].dispatchEvent("
            "        new MouseEvent('click',{bubbles:true})); return true; }"
            "  }"
            "  kotak=kotak.parentElement;"
            "}"
            "return false;")
        time.sleep(2)
        # Cadangan: tekan Escape
        page.run_cdp("Input.dispatchKeyEvent", type="keyDown", key="Escape",
                     code="Escape", windowsVirtualKeyCode=27)
        page.run_cdp("Input.dispatchKeyEvent", type="keyUp", key="Escape",
                     code="Escape", windowsVirtualKeyCode=27)
        time.sleep(2)
    except Exception:
        pass


def upload_photos(photo_paths, caption: str, account_id: int,
                  hashtags: str = None, music: str = None,
                  location: str = None, schedule_time: str = None,
                  is_invisible: int = 0, simpan_draf: bool = False,
                  progress_callback=None) -> bool:
    """Unggah satu postingan MULTI-FOTO lewat TikTok Studio web."""

    def set_p(v):
        if progress_callback:
            try:
                progress_callback(v)
            except Exception:
                pass

    foto = [p for p in (photo_paths or []) if os.path.isfile(p)]
    if not foto:
        print("[ERROR] Tidak ada berkas foto yang valid.")
        return False

    from app.db.database import get_db
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, current_session FROM accounts WHERE id=?", (account_id,))
    acc = c.fetchone()
    conn.close()
    if not acc:
        print("[ERROR] Akun ID=" + str(account_id) + " tidak ditemukan di DB.")
        return False
    username = acc["username"]
    profile_path = acc["current_session"]
    if not profile_path or not os.path.exists(profile_path):
        print("[ERROR] Profile path tidak ada: " + str(profile_path))
        return False

    port = 9322 + (hash(username) % 1000)
    page = _connect(profile_path, port, bool(is_invisible))
    if not page:
        print("[ERROR] Gagal membuka browser.")
        return False
    set_p(15)

    try:
        try:
            page.set.auto_handle_alert(True, accept=True)
        except Exception:
            pass

        print("[FOTO] Membuka halaman upload foto (" + str(len(foto)) + " foto)...")
        page.get(TIKTOK_PHOTO_URL)
        time.sleep(8)
        _dismiss_popups(page)

        if not _halaman_foto_siap(page):
            print("[ERROR] Halaman upload FOTO tidak tersedia untuk akun ini. "
                  "TikTok merilis fitur ini bertahap - buka tiktok.com, arahkan "
                  "kursor ke tombol Unggah; kalau tidak ada pilihan 'Foto', akun "
                  "ini memang belum mendapatkannya.")
            page.quit()
            return False
        set_p(30)

        inp = page.ele('tag:input@type=file', timeout=15)
        inp.input('\n'.join(foto))
        print("[FOTO] " + str(len(foto)) + " foto dikirim, menunggu editor...")

        siap = False
        for _ in range(60):
            time.sleep(2)
            if page.run_js("return !!document.querySelector('[contenteditable=\"true\"]');"):
                siap = True
                break
        if not siap:
            print("[ERROR] Editor foto tidak muncul.")
            page.quit()
            return False
        time.sleep(4)
        set_p(55)

        _fill_caption_hashtag(page, caption, hashtags)
        time.sleep(2)

        # LOKASI DULU, BARU MUSIK.
        #
        # Panel "Sounds" adalah modal yang menutupi kolom Location di bawahnya.
        # Saat musik dikerjakan lebih dulu, penutupan modal itu meninggalkan
        # dropdown lokasi dalam keadaan menampilkan daftar "lokasi baru-baru
        # ini" yang basi — terbukti saat pengujian: lokasi yang diminta
        # "Trans Studio Bandung" tapi yang muncul "Warga +62 Coffee Shop".
        # Dengan lokasi dikerjakan lebih dulu, keduanya sama-sama bersih.
        if location:
            _do_location(page, location)
            time.sleep(2)
        set_p(70)

        if music:
            _do_sound(page, music)
            time.sleep(3)
        set_p(80)

        if schedule_time:
            print("[FOTO] Mode SCHEDULE: " + str(schedule_time))
            _do_schedule(page, schedule_time)
        set_p(90)

        if simpan_draf:
            # Dipakai untuk mencoba tanpa benar-benar memposting.
            if not _click_btn_by_text(page, ['Save draft', 'Simpan draf'], 'Save draft'):
                print("[ERROR] Tombol 'Save draft' tidak ditemukan.")
                page.quit()
                return False
            time.sleep(8)
            print("[SUCCESS] Tersimpan sebagai draf.")
            try:
                page.quit()
            except Exception:
                pass
            return True

        if not _click_post(page):
            print("[ERROR] Tombol Post tidak ditemukan.")
            page.quit()
            return False

        _wait_post_success(page, timeout=60)
        set_p(100)
        print("[SUCCESS] Postingan foto terkirim: " + str(len(foto)) + " foto")
        try:
            page.quit()
        except Exception:
            pass
        return True
    except Exception as e:
        print("[ERROR] Gagal unggah foto: " + str(e))
        try:
            page.quit()
        except Exception:
            pass
        return False
