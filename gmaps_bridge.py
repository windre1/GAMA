"""
gmaps_bridge.py — Ekstraktor Google Maps / Google Bisnis untuk GAMA.

Scrape kontak bisnis (nama, kategori, alamat, telepon, website, rating, jumlah
review) dari hasil pencarian Google Maps lewat browser (DrissionPage). Cocok
untuk lead-gen: hasilnya bisa dicek WA aktif-nya lalu dikirimi permintaan review.

Juga bisa mode "photos": ambil foto yang di-upload pelanggan di kolom Ulasan
satu bisnis (bukan foto resmi bisnisnya) - hasilnya dipakai sebagai gambar
sumber di tab Generator (Image/Video Generator) buat di-generate ulang.

Dijalankan sebagai worker subprocess:
    python gmaps_bridge.py <job_json_path>

Job JSON (mode="search", default): {keyword, location, limit, out_dir, status_path}
Job JSON (mode="photos"): {mode, maps_url/place, name, limit, out_dir, status_path,
sort_best_quality, require_people, require_detail}
"""
import os
import re
import sys
import json
import time
import html
import argparse
import shutil
from urllib.parse import quote, unquote

# Frozen (PyInstaller): folder GAMA.exe (persisten). Dev: folder file ini.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
           else os.path.dirname(os.path.abspath(__file__))

CHROME_PATHS = [
    os.path.join(BASE_DIR, "assets", "chrome", "chrome.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]


def get_chrome_path():
    env = os.environ.get("GAMA_BROWSER")
    if env and os.path.isfile(env):
        return env
    for p in CHROME_PATHS:
        if p and os.path.isfile(p):
            return p
    return None


def write_status(status_path, **fields):
    tmp = status_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False)
        os.replace(tmp, status_path)
    except Exception:
        pass


def _connect(profile_dir):
    from DrissionPage import ChromiumPage, ChromiumOptions
    chrome = get_chrome_path()
    if not chrome:
        raise RuntimeError("Chrome tidak ditemukan (butuh Chrome untuk scrape Google Maps).")
    os.makedirs(profile_dir, exist_ok=True)
    co = ChromiumOptions()
    co.set_browser_path(chrome)
    co.set_user_data_path(profile_dir)
    co.set_local_port(9810 + (abs(hash(profile_dir)) % 300))
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--disable-extensions")
    co.set_argument("--lang=id-ID")
    return ChromiumPage(co)


def _dismiss_consent(page):
    """Klik tombol persetujuan cookie Google bila muncul (ID/EN)."""
    for txt in ("Terima semua", "Accept all", "Setuju semua", "I agree", "Terima"):
        try:
            el = page.ele(f"text:{txt}", timeout=1)
            if el:
                el.click()
                time.sleep(1.5)
                return
        except Exception:
            pass


def _place_links(page, want):
    """Kumpulkan href tempat dari feed hasil pencarian sambil scroll sampai cukup."""
    links, seen = [], set()
    stagnant = 0
    for _ in range(60):
        try:
            cards = page.eles("tag:a", timeout=1)
        except Exception:
            cards = []
        for a in cards:
            try:
                href = a.attr("href") or ""
                if "/maps/place/" in href and href not in seen:
                    seen.add(href)
                    name = a.attr("aria-label") or ""
                    links.append({"href": href, "name": name.strip()})
            except Exception:
                pass
        if len(links) >= want:
            break
        # scroll panel feed ke bawah untuk memuat lebih banyak
        try:
            feed = page.ele('css:div[role="feed"]', timeout=1)
            if feed:
                page.run_js("arguments[0].scrollTop = arguments[0].scrollHeight;", feed)
            else:
                page.scroll.to_bottom()
        except Exception:
            pass
        before = len(links)
        time.sleep(1.6)
        if len(links) <= before:
            stagnant += 1
            if stagnant >= 4:
                break
        else:
            stagnant = 0
    return links[:want]


def _text(page, selector):
    try:
        el = page.ele(selector, timeout=0.6)
        return (el.text or "").strip() if el else ""
    except Exception:
        return ""


def _extract_detail(page):
    """Ambil field bisnis dari panel detail tempat yang sedang terbuka."""
    data = {"name": "", "category": "", "address": "", "phone": "",
            "website": "", "rating": "", "reviews": ""}
    # Nama
    data["name"] = _text(page, "css:h1.DUwDvf") or _text(page, "tag:h1")
    # Kategori
    data["category"] = _text(page, "css:button[jsaction*='category']") or _text(page, "css:.DkEaL")
    # Rating + jumlah review
    data["rating"] = _text(page, "css:div.F7nice span[aria-hidden='true']")
    for sel in ("css:button[aria-label*='ulasan']", "css:button[aria-label*='review']",
                "css:div.F7nice span[aria-label]", "css:button[jsaction*='reviewChart']"):
        try:
            el = page.ele(sel, timeout=0.4)
            if el:
                lab = el.attr("aria-label") or el.text or ""
                m = re.search(r"([\d.,]+)\s*(ulasan|review|reviews)?", lab)
                if m and m.group(1):
                    data["reviews"] = m.group(1)
                    break
        except Exception:
            pass
    # Alamat, telepon, website — via data-item-id (paling stabil)
    try:
        for b in page.eles("css:button[data-item-id]", timeout=1):
            iid = (b.attr("data-item-id") or "")
            if iid == "address" and not data["address"]:
                data["address"] = (b.attr("aria-label") or b.text or "").replace("Alamat:", "").replace("Address:", "").strip()
            elif iid.startswith("phone:tel:") and not data["phone"]:
                data["phone"] = iid.split("tel:", 1)[1].strip()
    except Exception:
        pass
    if not data["phone"]:
        # fallback: cari nomor di aria-label tombol telepon
        try:
            el = page.ele("css:button[aria-label*='Telepon'], button[aria-label*='Phone']", timeout=0.6)
            if el:
                m = re.search(r"[\d+][\d\s().-]{6,}", el.attr("aria-label") or "")
                if m:
                    data["phone"] = m.group(0).strip()
        except Exception:
            pass
    try:
        w = page.ele("css:a[data-item-id='authority']", timeout=0.6)
        if w:
            data["website"] = (w.attr("href") or "").strip()
    except Exception:
        pass
    return data


def _norm_phone_id(phone):
    """Normalisasi nomor Indonesia ke format 62xxxx (untuk cek WA nanti)."""
    d = re.sub(r"\D+", "", phone or "")
    if not d:
        return ""
    if d.startswith("0"):
        d = "62" + d[1:]
    elif d.startswith("620"):
        d = "62" + d[3:]
    return d


def _safe_slug(name):
    s = re.sub(r"[^\w\-]+", "_", (name or "bisnis").strip())
    return (s.strip("_") or "bisnis")[:60]


def _open_reviews_tab(page):
    """Klik tab 'Ulasan'/'Reviews' di panel detail bisnis. True kalau berhasil."""
    try:
        tabs = page.eles('css:button[role="tab"]', timeout=3)
    except Exception:
        tabs = []
    for t in tabs:
        try:
            lab = ((t.attr("aria-label") or "") + " " + (t.text or "")).lower()
            if "ulasan" in lab or "review" in lab:
                t.click()
                return True
        except Exception:
            pass
    return False


def _scroll_review_pane(page):
    """Scroll panel ulasan ke bawah - cari ancestor scrollable dari kartu
    ulasan pertama (bukan hardcode nama class, supaya tahan kalau Google
    ganti nama class pane-nya)."""
    moved = False
    try:
        moved = bool(page.run_js(
            """
            const cards = [...document.querySelectorAll('div.jftiEf')];
            const card = cards[cards.length - 1] || cards[0];
            let scroller = null;
            let el = card;
            for (let i = 0; i < 10 && el; i++) {
                if (el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 100) {
                    scroller = el;
                    break;
                }
                el = el.parentElement;
            }
            scroller = scroller || document.querySelector('[role="main"]') || document.scrollingElement;
            if (!scroller) return false;
            const before = scroller.scrollTop || 0;
            scroller.scrollBy(0, Math.max(900, scroller.clientHeight * 1.8));
            if (cards.length) cards[cards.length - 1].scrollIntoView({block: 'end'});
            return (scroller.scrollTop || 0) !== before;
            """
        ))
    except Exception:
        pass
    try:
        page.actions.scroll(delta_y=900)
    except Exception:
        pass
    return moved


def _normalise_photo_url(url):
    url = html.unescape(str(url or "").strip().strip("\"'").rstrip(","))
    url = url.replace("\\u003d", "=").replace("\\u0026", "&")
    url = url.replace("\\x3d", "=").replace("\\x26", "&")
    if url.startswith("//"):
        url = "https:" + url
    return url


def _is_maps_photo_url(url):
    """True untuk foto tempat Google Maps, false untuk avatar/icon/map tile."""
    u = (url or "").lower()
    if not u.startswith("http"):
        return False
    if not any(host in u for host in ("googleusercontent.com", "ggpht.com")):
        return False
    bad_bits = (
        "/a-/",
        "/a/",
        "streetviewpixels",
        "staticmap",
        "maps/vt",
        "marker",
        "mapfiles",
        "gstatic",
    )
    if any(bit in u for bit in bad_bits):
        return False
    strong_photo_bits = ("/p/", "/gps-cs", "/geougc")
    if any(bit in u for bit in strong_photo_bits):
        return True
    return bool(re.search(r"=(?:w\d+-h\d+|s\d+)", u))


def _photo_key(url):
    """Kunci dedupe yang mengabaikan varian ukuran (=w300-h225, =s1600, dll)."""
    u = _normalise_photo_url(url)
    base = u.split("?", 1)[0]
    return re.sub(r"=(?:w\d+-h\d+|s\d+|h\d+)[^/]*$", "", base)


def _photo_url_candidates(value):
    text = _normalise_photo_url(value)
    if not text:
        return []
    variants = [text]
    decoded = unquote(text)
    if decoded != text:
        variants.append(decoded)

    out = []
    for variant in variants:
        matches = re.findall(r"https?:/+[^\"'\s)<>!]+", variant)
        if not matches and variant.startswith("http"):
            matches = [variant]
        for url in matches:
            url = _normalise_photo_url(url).rstrip(",;!")
            if url.startswith("https:/") and not url.startswith("https://"):
                url = "https://" + url[len("https:/"):].lstrip("/")
            if url.startswith("http:/") and not url.startswith("http://"):
                url = "http://" + url[len("http:/"):].lstrip("/")
            out.append(url)
    return out


def _append_photo(photos, seen, url, reviewer="", source="review"):
    added = False
    for candidate in _photo_url_candidates(url):
        candidate = _normalise_photo_url(candidate)
        if not _is_maps_photo_url(candidate):
            continue
        key = _photo_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        photos.append({
            "url": _upsize_photo_url(candidate),
            "reviewer": reviewer,
            "source": source,
        })
        added = True
    return added


def _upsize_photo_url(url):
    """Google Maps nyimpen foto ulasan dalam ukuran thumbnail (mis. =w300-h225-p-k-no)
    di URL-nya sendiri - ganti jadi =s1600 supaya yang diunduh resolusi asli/besar,
    bukan thumbnail kecil (penting buat bahan generate ulang gambar)."""
    url = _normalise_photo_url(url)
    if not url:
        return url
    base, sep, query = url.partition("?")
    base = re.sub(r"=(?:w\d+-h\d+|s\d+|h\d+)[^\"'\s)]*$", "", base)
    sized = base + "=s1600"
    return sized + (sep + query if sep else "")


def _dom_photo_urls(page):
    try:
        urls = page.run_js(
            """
            const out = [];
            const push = (value) => {
                if (!value) return;
                const text = String(value);
                const matches = text.match(/https?:\\/\\/[^"'\\s)]+/g) || [];
                for (const url of matches) out.push(url);
            };
            document.querySelectorAll('img').forEach((img) => {
                push(img.currentSrc);
                push(img.src);
                push(img.getAttribute('data-src'));
                push(img.getAttribute('srcset'));
            });
            document.querySelectorAll('[style*="url("]').forEach((el) => {
                push(el.getAttribute('style'));
                try { push(getComputedStyle(el).backgroundImage); } catch (e) {}
            });
            document.querySelectorAll('a[href], area[href]').forEach((el) => {
                push(el.href);
                push(el.getAttribute('href'));
            });
            document.querySelectorAll('[data-photo-url], [data-src], [data-thumbnail-url]').forEach((el) => {
                push(el.getAttribute('data-photo-url'));
                push(el.getAttribute('data-src'));
                push(el.getAttribute('data-thumbnail-url'));
            });
            return out;
            """
        )
        if isinstance(urls, list):
            return [str(u) for u in urls if u]
    except Exception:
        pass
    return []


def _open_photos_tab(page):
    """Buka tab/galeri Foto di detail Maps sebagai fallback kalau foto ulasan kurang."""
    good_words = ("foto", "photo", "photos", "gambar")
    bad_words = ("tambahkan", "add photo", "upload", "unggah", "laporkan", "report")
    selectors = (
        'css:button[role="tab"]',
        'css:a[role="tab"]',
        'css:button',
        'css:a[href]',
    )
    candidates = []
    for selector in selectors:
        try:
            els = page.eles(selector, timeout=1)
        except Exception:
            els = []
        for el in els:
            try:
                label = " ".join([
                    el.attr("aria-label") or "",
                    el.attr("title") or "",
                    el.attr("href") or "",
                    el.text or "",
                ]).strip().lower()
                if not label:
                    continue
                if any(bad in label for bad in bad_words):
                    continue
                if any(good in label for good in good_words):
                    score = 30
                    if any(x in label for x in (
                        "lihat foto",
                        "lihat semua foto",
                        "view photos",
                        "view all photos",
                        "see photos",
                        "see all photos",
                        "semua foto",
                        "all photos",
                    )):
                        score = 100
                    elif (el.attr("role") or "").lower() == "tab":
                        score = 85
                    elif label.strip() in ("semua", "all"):
                        score = 75
                    elif "foto ke-" in label or "photo " in label:
                        score = 20
                    elif label.startswith("foto ") or label.startswith("photo "):
                        score = 40
                    candidates.append((score, el))
            except Exception:
                pass
    for _, el in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            el.click()
            time.sleep(2)
            return True
        except Exception:
            pass

    try:
        clicked = bool(page.run_js(
            """
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 80 && r.height > 80 && r.bottom > 0 && r.right > 0;
            };
            const hasPlacePhoto = (el) => {
                const style = getComputedStyle(el).backgroundImage || '';
                return /(googleusercontent|ggpht)/i.test(style) && /(\\/p\\/|\\/gps-cs)/i.test(style);
            };
            const items = [...document.querySelectorAll('button, a')].filter((el) => {
                const label = [
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.textContent || '',
                    el.href || ''
                ].join(' ').toLowerCase();
                if (/tambahkan|add photo|upload|unggah|laporkan|report/.test(label)) return false;
                return visible(el) && (/foto|photo|gambar/.test(label) || hasPlacePhoto(el));
            });
            if (!items.length) return false;
            items[0].click();
            return true;
            """
        ))
        if clicked:
            time.sleep(2)
            return True
    except Exception:
        pass
    return False


def _scroll_photo_gallery(page):
    moved = False
    try:
        moved = bool(page.run_js(
            """
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 120 && r.height > 120 && r.bottom > 0 && r.right > 0;
            };
            const scrollers = [...document.querySelectorAll('div, main, section')]
                .filter((el) => visible(el) && el.scrollHeight > el.clientHeight + 120)
                .sort((a, b) => (b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth));
            let didMove = false;
            for (const scroller of scrollers.slice(0, 12)) {
                const before = scroller.scrollTop || 0;
                scroller.scrollBy(0, Math.max(900, scroller.clientHeight * 1.45));
                if ((scroller.scrollTop || 0) !== before) didMove = true;
            }
            const doc = document.scrollingElement;
            if (doc) {
                const before = doc.scrollTop || 0;
                doc.scrollBy(0, Math.max(900, window.innerHeight * 1.3));
                if ((doc.scrollTop || 0) !== before) didMove = true;
            }
            return didMove;
            """
        ))
    except Exception:
        pass
    try:
        page.actions.scroll(delta_y=1200)
    except Exception:
        pass
    return moved


def _collect_gallery_photos(page, limit, exclude_urls=None):
    """Kumpulkan foto tambahan dari galeri Maps sampai target kandidat cukup."""
    photos = []
    seen = {_photo_key(u) for u in (exclude_urls or []) if _photo_key(u)}
    stagnant, last_count = 0, 0

    for _ in range(120):
        for url in _dom_photo_urls(page):
            _append_photo(photos, seen, url, source="gallery")
            if len(photos) >= limit:
                break
        if len(photos) >= limit:
            break
        if len(photos) > last_count:
            stagnant = 0
            last_count = len(photos)
        else:
            stagnant += 1
            if stagnant >= 14:
                break
        _scroll_photo_gallery(page)
        time.sleep(1.05)
    return photos[:limit]


def _collect_review_photos(page, limit):
    """Kumpulkan {url, reviewer} dari foto yang di-attach pelanggan di ulasan,
    sambil scroll sampai cukup / stagnan (pola sama seperti _place_links)."""
    photos, seen = [], set()
    stagnant, last_count = 0, 0

    def scan_once():
        added = 0
        try:
            cards = page.eles("css:div.jftiEf[data-review-id]", timeout=1)
        except Exception:
            cards = []
        for card in cards:
            reviewer = (card.attr("aria-label") or "").strip()
            try:
                photo_btns = card.eles("css:button.Tya61d", timeout=0.5)
            except Exception:
                photo_btns = []
            for b in photo_btns:
                try:
                    style = b.attr("style") or ""
                    m = re.search(r'url\((["\']?)(.*?)\1\)', style)
                    if not m:
                        continue
                    if _append_photo(photos, seen, m.group(2), reviewer=reviewer, source="review"):
                        added += 1
                except Exception:
                    pass
        try:
            buttons = page.eles("css:button.Tya61d", timeout=0.5)
        except Exception:
            buttons = []
        for b in buttons:
            try:
                style = b.attr("style") or ""
                m = re.search(r'url\((["\']?)(.*?)\1\)', style)
                if not m:
                    continue
                if _append_photo(photos, seen, m.group(2), source="review"):
                    added += 1
            except Exception:
                pass
        return added

    for _ in range(140):
        scan_once()
        if len(photos) >= limit:
            break
        if len(photos) > last_count:
            stagnant = 0
            last_count = len(photos)
        else:
            stagnant += 1
            if stagnant >= 12:
                break
        _scroll_review_pane(page)
        time.sleep(1.35)
    return photos[:limit]


def _download_url(url, out_path, referer=None):
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    if referer:
        headers["Referer"] = referer
    try:
        with requests.get(url, headers=headers, timeout=20, stream=True) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return False


_CV2_CASCADE_CACHE = {}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _cv2_read_image(path):
    """Baca gambar dengan OpenCV. np.fromfile lebih aman untuk path Windows
    yang mengandung karakter non-ASCII."""
    try:
        import cv2
        import numpy as np
        data = np.fromfile(path, dtype=np.uint8)
        if data.size:
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is not None:
                return cv2, img
        return cv2, cv2.imread(path)
    except Exception:
        return None, None


def _get_cv2_cascade(cv2, filename):
    if filename in _CV2_CASCADE_CACHE:
        return _CV2_CASCADE_CACHE[filename]
    try:
        path = os.path.join(cv2.data.haarcascades, filename)
        if not os.path.isfile(path):
            _CV2_CASCADE_CACHE[filename] = None
            return None
        cascade = cv2.CascadeClassifier(path)
        if cascade.empty():
            cascade = None
        _CV2_CASCADE_CACHE[filename] = cascade
        return cascade
    except Exception:
        _CV2_CASCADE_CACHE[filename] = None
        return None


def _detect_people(gray, cv2):
    detections = 0
    cascade_specs = (
        ("haarcascade_frontalface_default.xml", 1.1, 4, (36, 36)),
        ("haarcascade_profileface.xml", 1.1, 4, (36, 36)),
        ("haarcascade_upperbody.xml", 1.05, 5, (48, 48)),
        ("haarcascade_fullbody.xml", 1.05, 4, (48, 80)),
    )
    for filename, scale, neighbors, min_size in cascade_specs:
        cascade = _get_cv2_cascade(cv2, filename)
        if cascade is None:
            continue
        try:
            found = cascade.detectMultiScale(
                gray, scaleFactor=scale, minNeighbors=neighbors, minSize=min_size
            )
            detections += len(found)
            if detections:
                break
        except Exception:
            pass
    return detections


def _analyze_photo(path):
    meta = {
        "width": 0,
        "height": 0,
        "file_size": 0,
        "quality_score": 0.0,
        "detail_score": 0.0,
        "sharpness": 0.0,
        "has_people": False,
    }
    try:
        meta["file_size"] = os.path.getsize(path)
    except Exception:
        pass

    try:
        from PIL import Image
        with Image.open(path) as img:
            meta["width"], meta["height"] = img.size
    except Exception:
        pass

    cv2, img = _cv2_read_image(path)
    if cv2 is not None and img is not None:
        try:
            h, w = img.shape[:2]
            meta["width"] = meta["width"] or int(w)
            meta["height"] = meta["height"] or int(h)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            max_side = max(gray.shape[:2])
            if max_side > 900:
                scale = 900.0 / max_side
                gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            edges = cv2.Canny(gray, 70, 160)
            edge_density = float((edges > 0).mean())
            contrast = float(gray.std())
            brightness = float(gray.mean())
            people_count = _detect_people(gray, cv2)

            area = max(1, int(meta["width"]) * int(meta["height"]))
            area_score = min(area / float(1600 * 1200), 2.0)
            size_score = min(meta["file_size"] / float(1_200_000), 1.5)
            sharp_score = min(sharpness / 450.0, 1.5)
            contrast_score = min(contrast / 64.0, 1.0)
            exposure_score = 1.0 - min(abs(brightness - 128.0) / 128.0, 1.0)

            meta["sharpness"] = round(sharpness, 2)
            meta["has_people"] = bool(people_count)
            meta["detail_score"] = round(
                min(edge_density * 3.5, 1.0) * 0.55
                + min(sharp_score / 1.5, 1.0) * 0.25
                + contrast_score * 0.20,
                4,
            )
            meta["quality_score"] = round(
                area_score * 0.45
                + sharp_score * 0.25
                + size_score * 0.15
                + contrast_score * 0.10
                + exposure_score * 0.05,
                4,
            )
            return meta
        except Exception:
            pass

    area = max(1, int(meta["width"] or 0) * int(meta["height"] or 0))
    meta["quality_score"] = round(
        min(area / float(1600 * 1200), 2.0) * 0.75
        + min(meta["file_size"] / float(1_200_000), 1.5) * 0.25,
        4,
    )
    return meta


def _rank_photo_candidates(candidates, limit, sort_best_quality=False,
                           require_people=False, require_detail=False):
    pool = list(candidates or [])
    notes = []
    if not pool:
        return [], notes

    if require_people and not any(c.get("has_people") for c in pool):
        notes.append("Foto manusia tidak terdeteksi; memakai ranking kualitas terbaik yang tersedia.")

    def score(c):
        quality = float(c.get("quality_score") or 0)
        detail = float(c.get("detail_score") or 0)
        area = int(c.get("width") or 0) * int(c.get("height") or 0)
        size = int(c.get("file_size") or 0)
        people_boost = 1.25 if c.get("has_people") else 0.0

        value = quality * (1.25 if sort_best_quality else 0.85)
        value += min(area / float(1600 * 1200), 2.0) * 0.20
        value += min(size / float(1_200_000), 1.5) * 0.08
        if require_people:
            value += people_boost
        if require_detail:
            value += detail * 1.10
        return value

    pool = sorted(
        pool,
        key=lambda c: (
            score(c),
            float(c.get("quality_score") or 0),
            float(c.get("detail_score") or 0),
        ),
        reverse=True,
    )
    target = min(max(int(limit or 3), 3), len(pool))
    return pool[:target], notes


def _resolve_place_to_url(page, place):
    """'place' boleh link Google Maps langsung, atau nama tempat biasa yang
    perlu di-search dulu (ambil hasil pertama, sama seperti _place_links tapi
    cukup satu tempat saja)."""
    if re.match(r"^https?://", place) and ("maps.app.goo.gl" in place or "goo.gl/maps" in place):
        page.get(place)
        time.sleep(4)
        _dismiss_consent(page)
        for _ in range(10):
            cur = page.url or ""
            if "google.com/maps" in cur:
                return cur
            time.sleep(1)
        return page.url or place
    if re.match(r"^https?://", place) or "google.com/maps" in place:
        return place
    page.get("https://www.google.com/maps/search/" + quote(place))
    time.sleep(3)
    _dismiss_consent(page)
    time.sleep(1.5)
    if "/maps/place/" in page.url:
        return page.url
    try:
        first = page.ele('css:a[href*="/maps/place/"]', timeout=5)
        if first:
            href = first.attr("href") or ""
            if href:
                return href
    except Exception:
        pass
    return None


def run_job_photos(job):
    """Ambil foto yang di-upload pelanggan di kolom Ulasan satu bisnis."""
    status_path = job["status_path"]
    place = (job.get("place") or job.get("maps_url") or "").strip()
    name = (job.get("name") or "").strip()
    limit = max(3, min(int(job.get("limit") or 20), 100))
    sort_best_quality = _as_bool(job.get("sort_best_quality"))
    require_people = _as_bool(job.get("require_people"))
    require_detail = _as_bool(job.get("require_detail"))
    out_dir = job.get("out_dir") or os.path.join(BASE_DIR, "downloads", "gmaps_photos")
    profile_dir = os.path.join(BASE_DIR, "data", "gmaps_profile")

    state = {"pct": 0, "message": "Menyiapkan browser...", "done": False,
             "success": False, "error": "", "photos": [], "files": []}

    def flush():
        write_status(status_path, **state)

    def log(pct, msg):
        state["pct"] = pct
        state["message"] = msg
        flush()

    flush()
    if not place:
        state.update(done=True, error="Nama tempat atau link Google Maps wajib diisi.")
        flush(); return 1

    page = None
    candidate_dir = None
    try:
        page = _connect(profile_dir)
        log(5, f"Mencari '{place}' di Google Maps...")
        maps_url = _resolve_place_to_url(page, place)
        if not maps_url:
            raise RuntimeError(f"Tempat '{place}' tidak ditemukan di Google Maps.")

        log(12, "Membuka listing...")
        page.get(maps_url)
        time.sleep(2.5)
        _dismiss_consent(page)
        time.sleep(1)

        if not name:
            name = _text(page, "css:h1.DUwDvf") or place
        state["place_name"] = name
        flush()
        slug = _safe_slug(name)
        sub_out = os.path.join(out_dir, slug)
        # Hasil scrape sebelumnya TIDAK PERNAH ditimpa atau dihapus. Kalau
        # tempat yang sama di-scrape lagi, hasilnya masuk ke folder baru
        # ber-akhiran angka. Sebelumnya foto lama dihapus lebih dulu supaya
        # penomoran 01..NN tidak bentrok — tapi itu berarti hasil kemarin
        # lenyap hanya karena user mengulang scrape, tanpa pernah diminta.
        if os.path.isdir(sub_out) and any(
            re.match(r"^\d{2,3}\.(jpe?g|png|webp)$", f, re.I)
            for f in os.listdir(sub_out)
        ):
            n = 2
            while os.path.isdir(f"{sub_out}_{n}"):
                n += 1
            sub_out = f"{sub_out}_{n}"
            log(0, f"Hasil scrape sebelumnya dipertahankan — foto baru "
                   f"disimpan di folder {os.path.basename(sub_out)}.")
        os.makedirs(sub_out, exist_ok=True)

        screening = sort_best_quality or require_people or require_detail
        if screening:
            collect_limit = min(100, max(limit, limit * (4 if require_people else 3 if require_detail else 2)))
        else:
            collect_limit = min(100, max(limit * 5, limit + 20))

        filter_bits = []
        if sort_best_quality:
            filter_bits.append("kualitas terbaik")
        if require_people:
            filter_bits.append("manusia")
        if require_detail:
            filter_bits.append("detail")
        filter_label = (" filter: " + ", ".join(filter_bits)) if filter_bits else ""

        photos = []
        log(18, "Membuka tab Ulasan...")
        if _open_reviews_tab(page):
            time.sleep(2)
            log(25, f"Mengumpulkan foto dari ulasan (target {limit}, kandidat {collect_limit}){filter_label}...")
            photos = _collect_review_photos(page, collect_limit)
        else:
            log(22, "Tab Ulasan tidak ditemukan, mencoba galeri Foto...")

        review_photo_count = len(photos)
        gallery_photo_count = 0
        if len(photos) < collect_limit and (len(photos) < limit or screening):
            remaining = collect_limit - len(photos)
            log(34, f"Foto ulasan baru {len(photos)}/{limit}; mencari tambahan dari galeri Maps...")
            try:
                page.get(maps_url)
                time.sleep(2.5)
                _dismiss_consent(page)
                time.sleep(1)
                _open_photos_tab(page)
                extra = _collect_gallery_photos(
                    page,
                    remaining,
                    exclude_urls=[p.get("url", "") for p in photos],
                )
                gallery_photo_count = len(extra)
                photos.extend(extra)
            except Exception:
                pass
        if not photos:
            raise RuntimeError("Tidak ada foto yang bisa dibaca dari ulasan atau galeri bisnis ini.")

        candidate_dir = os.path.join(sub_out, f"_candidates_{int(time.time())}")
        os.makedirs(candidate_dir, exist_ok=True)
        candidates = []
        total = len(photos)
        for i, p in enumerate(photos, 1):
            pct = 40 + int((i - 1) / total * 30)
            log(pct, f"Mengunduh kandidat foto {i}/{total}...")
            fname = f"cand_{i:03d}.jpg"
            fpath = os.path.join(candidate_dir, fname)
            if _download_url(p["url"], fpath, referer=maps_url):
                item = {
                    "path": fpath,
                    "reviewer": p.get("reviewer", ""),
                    "source_url": p.get("url", ""),
                    "source": p.get("source", "review"),
                }
                if screening:
                    log(pct, f"Menganalisa kualitas foto {i}/{total}...")
                    item.update(_analyze_photo(fpath))
                candidates.append(item)

        if not candidates:
            raise RuntimeError("Gagal mengunduh semua foto (jaringan/URL bermasalah).")

        if not screening:
            for item in candidates:
                item.update(_analyze_photo(item["path"]))

        log(75, "Menyortir dan memilih foto terbaik...")
        selected, notes = _rank_photo_candidates(
            candidates,
            limit,
            sort_best_quality=sort_best_quality,
            require_people=require_people,
            require_detail=require_detail,
        )
        if not selected:
            raise RuntimeError("Tidak ada foto yang lolos filter.")
        if len(selected) < limit:
            notes.append(f"Google Maps hanya membuka {len(selected)} foto yang bisa diunduh untuk listing ini.")

        # Tidak ada penghapusan foto lama di sini lagi — folder tujuan sudah
        # dipastikan kosong dari penomoran lama saat sub_out ditentukan.

        files, saved_photos = [], []
        total_selected = len(selected)
        for i, item in enumerate(selected, 1):
            log(78 + int((i - 1) / total_selected * 17), f"Menyimpan foto terpilih {i}/{total_selected}...")
            fpath = os.path.join(sub_out, f"{i:02d}.jpg")
            shutil.copyfile(item["path"], fpath)
            saved = {
                "path": fpath,
                "reviewer": item.get("reviewer", ""),
                "width": item.get("width", 0),
                "height": item.get("height", 0),
                "quality_score": item.get("quality_score", 0),
                "detail_score": item.get("detail_score", 0),
                "has_people": bool(item.get("has_people")),
                "place_name": name,
                "source": item.get("source", "review"),
            }
            files.append(fpath)
            saved_photos.append(saved)
            state["files"] = files
            state["photos"] = saved_photos
            flush()

        try:
            shutil.rmtree(candidate_dir, ignore_errors=True)
        except Exception:
            pass

        csv_path = os.path.join(sub_out, "foto_ulasan.csv")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("photo_path;business_name;reviewer;maps_url;width;height;quality_score;has_people;detail_score;source\n")
            for sp in saved_photos:
                cells = [
                    sp["path"], name, sp.get("reviewer", ""), maps_url,
                    sp.get("width", ""), sp.get("height", ""), sp.get("quality_score", ""),
                    "yes" if sp.get("has_people") else "no", sp.get("detail_score", ""),
                    sp.get("source", ""),
                ]
                f.write(";".join(str(c).replace(";", ",").replace("\n", " ") for c in cells) + "\n")

        final_note = (" " + " ".join(notes)) if notes else ""
        source_note = f" (ulasan {review_photo_count}, galeri {gallery_photo_count})" if gallery_photo_count else ""
        state.update(pct=100, done=True, success=True,
                     message=f"Selesai! {len(files)} foto Maps tersimpan di {sub_out}.{source_note}{final_note}",
                     folder=sub_out, csv=csv_path, place_name=name)
        flush()
        return 0
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
        flush()
        return 1
    finally:
        try:
            if page is not None:
                page.quit()
        except Exception:
            pass
        try:
            if candidate_dir and os.path.isdir(candidate_dir):
                shutil.rmtree(candidate_dir, ignore_errors=True)
        except Exception:
            pass


def run_job(job):
    status_path = job["status_path"]
    keyword = (job.get("keyword") or "").strip()
    location = (job.get("location") or "").strip()
    limit = max(1, min(int(job.get("limit") or 20), 120))
    out_dir = job.get("out_dir") or os.path.join(BASE_DIR, "downloads", "gmaps")
    os.makedirs(out_dir, exist_ok=True)
    profile_dir = os.path.join(BASE_DIR, "data", "gmaps_profile")

    state = {"pct": 0, "message": "Menyiapkan browser...", "done": False,
             "success": False, "error": "", "rows": [], "results": []}

    def flush():
        write_status(status_path, **state)

    def log(pct, msg):
        state["pct"] = pct
        state["message"] = msg
        flush()

    flush()
    if not keyword:
        state.update(done=True, error="Kata kunci wajib diisi.")
        flush(); return 1

    query = (keyword + " " + location).strip()
    page = None
    try:
        page = _connect(profile_dir)
        log(5, "Membuka Google Maps...")
        page.get("https://www.google.com/maps/search/" + quote(query))
        time.sleep(3)
        _dismiss_consent(page)
        time.sleep(2)

        log(12, f"Mengumpulkan daftar bisnis untuk '{query}'...")
        links = _place_links(page, limit)
        if not links:
            raise RuntimeError("Tidak ada hasil bisnis ditemukan. Coba kata kunci/lokasi lain.")

        rows, results = [], []
        total = len(links)
        for i, lk in enumerate(links, 1):
            log(12 + int((i - 1) / total * 82),
                f"Mengambil detail {i}/{total}: {lk.get('name','')[:40]}...")
            try:
                page.get(lk["href"])
                time.sleep(2.2)
                d = _extract_detail(page)
                if not d["name"]:
                    d["name"] = lk.get("name", "")
                d["phone_wa"] = _norm_phone_id(d["phone"])
                d["maps_url"] = lk["href"]
                results.append(d)
                rows.append({"title": d["name"] or f"Bisnis {i}",
                             "status": "telepon: " + (d["phone"] or "-")})
                state["rows"] = rows
                state["results"] = results
                flush()
            except Exception as e:
                rows.append({"title": lk.get("name", f"Bisnis {i}"), "status": f"gagal: {str(e)[:40]}"})
                state["rows"] = rows
                flush()

        # CSV
        csv_path = os.path.join(out_dir, re.sub(r"[^\w\-]+", "_", query)[:50] + ".csv")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("nama;kategori;alamat;telepon;telepon_wa;website;rating;review;maps_url\n")
            for d in results:
                cells = [d.get(k, "") for k in
                         ("name", "category", "address", "phone", "phone_wa", "website", "rating", "reviews", "maps_url")]
                f.write(";".join(str(c).replace(";", ",").replace("\n", " ") for c in cells) + "\n")

        n_phone = sum(1 for d in results if d.get("phone"))
        state.update(pct=100, done=True, success=True,
                     message=f"Selesai! {len(results)} bisnis ({n_phone} punya telepon) tersimpan di {csv_path}",
                     folder=out_dir, csv=csv_path)
        flush()
        return 0
    except Exception as e:
        state.update(done=True, success=False, error=str(e))
        flush()
        return 1
    finally:
        try:
            if page is not None:
                page.quit()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job", nargs="?")
    args = parser.parse_args()
    if not args.job or not os.path.isfile(args.job):
        print("Job file tidak ditemukan", file=sys.stderr)
        return 2
    with open(args.job, "r", encoding="utf-8-sig") as f:
        job = json.load(f)
    if job.get("mode") == "photos":
        return run_job_photos(job)
    return run_job(job)


if __name__ == "__main__":
    raise SystemExit(main())
