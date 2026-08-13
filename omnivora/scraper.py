import re
import os
import time
import json
import httpx
from html import unescape
from urllib.parse import quote_plus, quote, unquote, urljoin, urlparse

from omnivora import regions as _regions


class ShopeeScraper:
    def __init__(self, log_cb=None, cookies=None, region_cookies=None):
        self.log = log_cb or (lambda msg, level="info": print(f"[{level.upper()}] {msg}"))
        self.cookies = cookies or ""
        self.region_cookies = region_cookies or {}
        self._first_product_verification_pending = True
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "X-Requested-With": "XMLHttpRequest"
        }

    def _cookie_for_region(self, region: str = "") -> str:
        region = _regions.normalize(region)
        return (
            self.region_cookies.get(region)
            or self.region_cookies.get(_regions.DEFAULT_REGION)
            or self.cookies
            or ""
        )

    def _headers_for_region(self, region: str = "", base_headers=None):
        headers = (base_headers or self.headers).copy()
        cookie = self._cookie_for_region(region)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    @staticmethod
    def _ensure_scheme(url: str) -> str:
        """Tambahkan https:// bila user paste URL tanpa scheme, supaya urlparse
        membaca host dengan benar (tanpa ini 'shopee.sg/...' dikira path dan
        region jatuh salah ke default Indonesia)."""
        url = (url or "").strip()
        if url and "://" not in url:
            url = "https://" + url.lstrip("/")
        return url

    def resolve_url(self, url: str) -> str:
        """Resolve short link shope.ee or s.shopee to long link"""
        url = self._ensure_scheme(url)
        if "shope.ee" in url or "shp.ee" in url or "s.shopee" in url:
            self.log(f"Mendeteksi short link, mencoba resolve: {url}", "info")
            try:
                # Resolve TANPA header Cookie: redirect chain shortlink bisa
                # lewat domain tracking pihak ketiga — cookie session Shopee
                # tidak boleh ikut terkirim ke sana.
                with httpx.Client(follow_redirects=True, headers=self.headers, timeout=10.0) as client:
                    response = client.get(url)
                    resolved = str(response.url)
                    self.log(f"Berhasil di-resolve ke: {resolved}", "success")
                    return resolved
            except Exception as e:
                self.log(f"Gagal melakukan resolve url: {e}", "error")
                return url
        return url

    def extract_ids(self, url: str):
        """Extract domain, shopid, and itemid from Shopee URL"""
        parsed = urlparse(self._ensure_scheme(url))
        domain = parsed.netloc or "shopee.co.id"
        # Support shopee.co.id, shopee.sg, play.shopee.co.id etc.
        if "shopee" not in domain:
            domain = "shopee.co.id"

        path = parsed.path
        query = parsed.query

        shop_id = None
        item_id = None

        # Pattern 1: i.SHOPID.ITEMID
        match1 = re.search(r"i\.(\d+)\.(\d+)", url)
        if match1:
            shop_id = match1.group(1)
            item_id = match1.group(2)

        # Pattern 2: product/SHOPID/ITEMID
        if not (shop_id and item_id):
            match2 = re.search(r"product/(\d+)/(\d+)", path)
            if match2:
                shop_id = match2.group(1)
                item_id = match2.group(2)

        # Pattern 3: query params itemid=X&shopid=Y
        if not (shop_id and item_id):
            match_shop = re.search(r"shopid=(\d+)", query)
            match_item = re.search(r"itemid=(\d+)", query)
            if match_shop and match_item:
                shop_id = match_shop.group(1)
                item_id = match_item.group(1)

        # Pattern 4: /username/SHOPID/ITEMID
        if not (shop_id and item_id):
            match4 = re.search(r"/([^/]+)/(\d{5,})/(\d{5,})", path)
            if match4:
                shop_id = match4.group(2)
                item_id = match4.group(3)

        return domain, shop_id, item_id

    def _collect_video_urls(self, data) -> list:
        """Best-effort recursive extraction of direct product video URLs."""
        urls = []
        seen = set()

        def add(value):
            if not isinstance(value, str):
                return
            value = value.strip()
            if not value:
                return
            if value.startswith("//"):
                value = "https:" + value
            if not value.startswith("http"):
                return
            lower = value.lower()
            if not (".mp4" in lower or ".m3u8" in lower or "video" in lower or "livestream" in lower):
                return
            if value not in seen:
                seen.add(value)
                urls.append(value)

        def walk(obj, parent_key=""):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    key_l = str(key).lower()
                    if isinstance(value, str) and (
                        "video" in key_l or key_l in {"url", "video_url", "play_url", "download_url"}
                    ):
                        add(value)
                    else:
                        walk(value, key_l)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, parent_key)
            else:
                add(obj)

        walk(data)
        return urls

    def _build_image_urls(self, image_hashes, region):
        image_urls = []
        for img_hash in image_hashes or []:
            if not img_hash:
                continue
            if isinstance(img_hash, str) and img_hash.startswith("http"):
                image_urls.append(img_hash)
            else:
                image_urls.append(f"https://down-{region}.img.susercontent.com/file/{img_hash}")
        return image_urls

    def _title_from_product_url(self, url: str) -> str:
        path = unquote(urlparse(url).path or "").strip("/")
        if not path:
            return "Produk Shopee"
        slug = path.rsplit("/", 1)[-1]
        slug = re.sub(r"-i\.\d+\.\d+$", "", slug)
        slug = re.sub(r"[-_]+", " ", slug).strip()
        return slug[:120] or "Produk Shopee"

    def _search_result_from_ids(self, domain, region, shop_id, item_id, title="", image=""):
        url = f"https://{domain}/product/{shop_id}/{item_id}"
        return {
            "title": title or f"Produk Shopee {item_id}",
            "shop_id": str(shop_id),
            "item_id": str(item_id),
            "domain": domain,
            "region": region,
            "url": url,
            "image_url": self._build_image_urls([image], region)[0] if image else "",
        }

    def _extract_search_results_from_html(self, html_text: str, domain: str, region: str, limit: int) -> list:
        """Extract product links from a rendered Shopee search page."""
        results = []
        seen = set()

        def add(shop_id, item_id, title=""):
            if not shop_id or not item_id:
                return
            key = f"{shop_id}:{item_id}"
            if key in seen:
                return
            seen.add(key)
            results.append(self._search_result_from_ids(domain, region, shop_id, item_id, title=title))

        html_text = unescape(html_text or "")

        # Pattern 1: slug-style URL  e.g. /product-name-i.SHOPID.ITEMID
        for match in re.finditer(r'href=["\']([^"\']*?i\.(\d+)\.(\d+)[^"\']*)["\']', html_text):
            if len(results) >= limit:
                break
            href, shop_id, item_id = match.groups()
            full_url = urljoin(f"https://{domain}", href)
            add(shop_id, item_id, self._title_from_product_url(full_url))

        # Pattern 2: explicit /product/SHOPID/ITEMID path
        if len(results) < limit:
            for shop_id, item_id in re.findall(r"/product/(\d+)/(\d+)", html_text):
                if len(results) >= limit:
                    break
                add(shop_id, item_id)

        # Pattern 3: inline JSON — "shopid":123,"itemid":456 or "item_id":456
        if len(results) < limit:
            for match in re.finditer(
                r'"shopid"\s*:\s*(\d+)[^}]{0,120}?"itemid"\s*:\s*(\d+)',
                html_text,
            ):
                if len(results) >= limit:
                    break
                add(match.group(1), match.group(2))

        return results

    # fe_filter_options values taken from Shopee's own URL format
    _SHOP_FILTER_OPTIONS = {
        "mall":      [{"group_name": "SHOP_TYPE", "values": ["OFFICIAL_MALL"]}],
        "star_plus": [{"group_name": "SHOP_TYPE", "values": ["PREFERRED_PLUS_SELLER"]}],
        "star":      [{"group_name": "SHOP_TYPE", "values": ["SHOPEE_VERIFIED"]}],
    }

    def _build_search_url(self, domain: str, keyword: str, sort_by: str, shop_type: str) -> str:
        """Build a Shopee search URL with sort + filter baked in from the start."""
        sort_map = {"sales": "by=sales&order=desc", "newest": "by=ctime&order=desc"}
        sort_qs = sort_map.get(sort_by, "by=relevancy&order=desc")
        base = f"https://{domain}/search?keyword={quote_plus(keyword)}&{sort_qs}"
        if shop_type in self._SHOP_FILTER_OPTIONS:
            filter_json = json.dumps(self._SHOP_FILTER_OPTIONS[shop_type], separators=(",", ":"))
            base += f"&fe_filter_options={quote(filter_json)}"
        return base

    def _search_products_browser(self, keyword: str, region: str, limit: int,
                                  sort_by: str = "relevancy", shop_type: str = "all") -> list:
        """Fallback search using the Shopee Chrome session when JSON API is blocked.

        Sort + filter are embedded directly in the URL (fe_filter_options format) so
        clicking sidebar checkboxes — which would reset the sort — is avoided entirely.
        """
        domain = _regions.web_domain(region)
        search_url = self._build_search_url(domain, keyword, sort_by, shop_type)

        filter_label = {"mall": "Mall", "star_plus": "Star+", "star": "Star"}.get(shop_type, "")
        sort_label   = {"sales": "Terlaris", "newest": "Terbaru"}.get(sort_by, "Relevansi")
        self.log(
            f"Browser fallback: keyword='{keyword}' | sort={sort_label} | filter={filter_label or 'Semua'}",
            "info",
        )

        try:
            from omnivora.shopee_session import ShopeeLoginSession

            session = ShopeeLoginSession(region, log_cb=self.log)
            session._launch_chrome(search_url)
            page = session._connect_page()
            try:
                page.get(search_url, timeout=20)
                self.log("Halaman search dimuat, menunggu render produk...", "info")
                time.sleep(3)

                # Guard: login / captcha
                html_text = getattr(page, "html", "") or ""
                page_url  = getattr(page, "url", "") or ""
                if "verify/captcha" in page_url or "Verifikasi untuk melanjutkan" in html_text:
                    self.log("Shopee meminta captcha. Selesaikan manual lalu ulangi search.", "error")
                    return []
                if "/buyer/login" in page_url or "Log in dengan QR" in html_text:
                    self.log("Chrome session belum login Shopee. Login manual lalu ulangi.", "error")
                    return []

                # Scroll untuk memicu lazy-load lebih banyak produk
                self.log(f"Scroll halaman untuk memuat lebih banyak produk (target {limit})...", "info")
                for _ in range(6):
                    try:
                        page.run_js("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(1.2)
                    except Exception:
                        time.sleep(1)

                # Ambil hasil dari HTML yang sudah ter-scroll
                for attempt in range(8):
                    html_text = getattr(page, "html", "") or ""
                    results = self._extract_search_results_from_html(html_text, domain, region, limit)
                    if results:
                        self.log(f"Fallback Chrome berhasil menemukan {len(results)} produk.", "success")
                        return results
                    if attempt < 7:
                        try:
                            page.run_js("window.scrollTo(0, document.body.scrollHeight)")
                        except Exception:
                            pass
                        time.sleep(1.5)

                self.log("Fallback Chrome tidak menemukan produk di halaman search.", "warning")
            finally:
                self.log("Fallback Chrome selesai, session browser tetap dibuka.", "info")
        except Exception as exc:
            self.log(f"Fallback Chrome search gagal: {exc}", "error")
        return []

    def _wait_for_manual_product_verification(self, tab, max_wait: int = 15) -> bool:
        """Beri user waktu untuk selesaikan puzzle/captcha sebelum scraping lanjut."""
        self.log(
            f"Mode aman scrape: memberi jeda verifikasi manual hingga {max_wait} detik jika ada puzzle/captcha.",
            "info",
        )
        waited = 0
        step = 1
        while waited < max_wait:
            try:
                html_text = getattr(tab, "html", "") or ""
                page_url = getattr(tab, "url", "") or ""
            except Exception:
                html_text = ""
                page_url = ""

            blocked = (
                "verify/captcha" in page_url
                or "verify/puzzle" in page_url
                or "verify/traffic" in page_url
                or "Verifikasi untuk melanjutkan" in html_text
                or "captcha" in html_text.lower()
                or "puzzle" in html_text.lower()
                or "geser" in html_text.lower()
            )
            if not blocked:
                if waited > 0:
                    self.log("Puzzle/captcha sudah hilang, scrape dilanjutkan.", "success")
                return True
            if waited == 0:
                self.log(
                    "Puzzle/captcha terdeteksi. Selesaikan manual di browser, bot menunggu...",
                    "warn",
                )
            time.sleep(step)
            waited += step
        return False

    def _scrape_product_browser(self, clean_url: str, domain: str, region: str, shop_id: str, item_id: str):
        import random
        initial_verification = self._first_product_verification_pending

        self.log(f"Mencoba fallback browser untuk scrape produk: {clean_url}", "info")
        if initial_verification:
            self.log("Produk pertama: jeda awal aman 15 detik untuk verifikasi puzzle/captcha jika muncul...", "info")

        try:
            from omnivora.shopee_session import ShopeeLoginSession
            session = ShopeeLoginSession(region, log_cb=self.log)
            session._launch_chrome(clean_url)
            page = session._connect_page()

            try:
                page.get(clean_url, timeout=20)
                self.log("Menunggu halaman produk dimuat...", "info")
                time.sleep(2)

                if initial_verification:
                    self.log("Halaman dimuat. Jika puzzle/captcha muncul, selesaikan dalam 15 detik berikutnya...", "info")
                    self._first_product_verification_pending = False
                    if not self._wait_for_manual_product_verification(page, max_wait=15):
                        self.log("Puzzle/captcha belum selesai dalam waktu tunggu. Ulangi scrape setelah verifikasi.", "error")
                        return None
                else:
                    time.sleep(1)
                
                # Coba eksekusi API fetch langsung dari console browser 
                # (karena browser memiliki token dan cookie lengkap dari JS Shopee)
                api_url = f"https://{domain}/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
                js_fetch = f"""
                return fetch('{api_url}', {{
                    headers: {{
                        'accept': 'application/json'
                    }}
                }}).then(r => r.json()).catch(e => null);
                """
                api_data = page.run_js(js_fetch)
                if isinstance(api_data, dict):
                    item = api_data.get("data") or api_data.get("item")
                    if item:
                        title = item.get("name", "")
                        description = item.get("description", "")
                        image_hashes = item.get("images", [])
                        image_urls = self._build_image_urls(image_hashes, region)
                        video_urls = self._collect_video_urls(item)

                        self.log(f"Sukses mengambil data via Browser API: '{title[:40]}...'", "success")
                        return {
                            "title": title,
                            "description": description,
                            "image_urls": image_urls,
                            "video_urls": video_urls,
                            "shop_id": shop_id,
                            "item_id": item_id,
                            "domain": domain,
                            "region": region
                        }
                
                # Jika fetch API gagal, coba ambil data dari state HTML window.__INITIAL_STATE__
                html_text = getattr(page, "html", "") or ""
                import json
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_text, "html.parser")
                scripts = soup.find_all("script")
                
                for s in scripts:
                    txt = s.string or ""
                    if 'window.__INITIAL_STATE__=' in txt:
                        try:
                            json_str = txt.split('window.__INITIAL_STATE__=')[1].split(';\n')[0]
                            data = json.loads(json_str)
                            initial_state = data.get("initialState", data)
                            item_section = initial_state.get("item", {})
                            items_map = item_section.get("items", {})
                            
                            if items_map:
                                resolved_item_id = item_id if item_id in items_map else list(items_map.keys())[0]
                                item_data = items_map[resolved_item_id]
                                
                                title = item_data.get("name") or item_data.get("title") or ""
                                description = item_data.get("description") or ""
                                image_hashes = item_data.get("images", [])
                                image_urls = self._build_image_urls(image_hashes, region)
                                video_urls = self._collect_video_urls(item_data)
                                
                                shop_id_val = item_data.get("shopid") or item_data.get("shop_id") or shop_id
                                
                                self.log(f"Sukses mengambil data produk via Browser State: '{title[:40]}...'", "success")
                                return {
                                    "title": title,
                                    "description": description,
                                    "image_urls": image_urls,
                                    "video_urls": video_urls,
                                    "shop_id": str(shop_id_val),
                                    "item_id": str(resolved_item_id),
                                    "domain": domain,
                                    "region": region
                                }
                        except Exception as e:
                            self.log(f"Gagal parse window.__INITIAL_STATE__: {e}", "warning")
                
            finally:
                pass
        except Exception as exc:
            self.log(f"Browser fallback gagal: {exc}", "error")
        return None

    def _affiliate_domain(self, region: str) -> str:
        return _regions.affiliate_domain(region)

    def _affiliate_page(self, session, aff_domain: str):
        page = session._connect_page()
        try:
            for tab in page.get_tabs():
                tab_url = getattr(tab, "url", "") or ""
                if aff_domain in tab_url:
                    page.activate_tab(tab)
                    return tab
        except Exception:
            pass
        return page

    def _is_affiliate_blocked_or_logged_out(self, page) -> str:
        url = getattr(page, "url", "") or ""
        try:
            body_text = page.run_js("return document.body ? document.body.innerText : ''") or ""
        except Exception:
            body_text = ""
        if "verify/captcha" in url or "Verifikasi untuk melanjutkan" in body_text:
            return "Shopee minta verifikasi captcha/slider di Chrome session Omnivora. Selesaikan manual lalu ulangi search."
        if "verify/traffic" in url:
            return "Shopee menampilkan traffic verification. Selesaikan manual di Chrome session Omnivora lalu ulangi search."
        if "/buyer/login" in url or "Log in dengan QR" in body_text or "LOG IN" in body_text:
            return "Chrome session Omnivora belum login Shopee. Login dulu di browser yang dibuka tombol Affiliate ID/Login Session."
        return ""

    def _extract_affiliate_results_from_html(self, html_text: str, region: str, limit: int) -> list:
        """Extract product candidates from Shopee Affiliate dashboard HTML."""
        domain = _regions.web_domain(region)
        results = []
        seen = set()
        html_text = unescape(html_text or "")

        def nearby_text(pos):
            start = max(0, pos - 900)
            end = min(len(html_text), pos + 900)
            chunk = re.sub(r"<[^>]+>", " ", html_text[start:end])
            chunk = re.sub(r"\s+", " ", chunk).strip()
            return chunk

        def add(shop_id, item_id, pos=0):
            if not shop_id or not item_id:
                return
            key = f"{shop_id}:{item_id}"
            if key in seen:
                return
            seen.add(key)
            context = nearby_text(pos)
            title = ""
            title_match = re.search(r"([A-Z0-9][^|]{12,140}?)(?:Rp|Komisi|Sampel Gratis|KOMISI|Star|Mall)", context)
            if title_match:
                title = title_match.group(1).strip(" -|")
            results.append({
                **self._search_result_from_ids(domain, region, shop_id, item_id, title=title),
                "source": "affiliate_dashboard",
                "affiliate_context": context[:500],
            })

        # Semua domain region yang didukung (co.id, sg, ph, co.th, com.my)
        _dom = r"(?:co\.id|sg|ph|co\.th|com\.my)"
        patterns = [
            rf"https?://(?:www\.)?shopee\.{_dom}/[^\"'\s<>]*?-i\.(\d+)\.(\d+)",
            rf"shopee\.{_dom}/product/(\d+)/(\d+)",
            r"i\.(\d+)\.(\d+)",
            r'"shopid"\s*:\s*(\d+).*?"itemid"\s*:\s*(\d+)',
            r'"shop_id"\s*:\s*(\d+).*?"item_id"\s*:\s*(\d+)',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, html_text, flags=re.IGNORECASE | re.DOTALL):
                if len(results) >= limit:
                    return results
                add(match.group(1), match.group(2), match.start())
        return results

    def _click_affiliate_filters(self, page, filters: dict):
        labels = []
        seller = (filters or {}).get("seller_type", "")
        if seller and seller != "Semua":
            labels.append(seller)
        if (filters or {}).get("with_video"):
            labels.append("Dengan Video")
        if (filters or {}).get("ready_stock"):
            labels.append("Ready Stock")
        if (filters or {}).get("xtra"):
            labels.append("Promo XTRA")
        if (filters or {}).get("xtra_plus"):
            labels.append("Promo XTRA+")

        for label in labels:
            try:
                ele = page.ele(f"text={label}", timeout=1)
                if ele:
                    ele.click()
                    time.sleep(0.4)
                    self.log(f"Filter affiliate dicentang: {label}", "info")
            except Exception:
                self.log(f"Filter affiliate tidak ditemukan/terlewati: {label}", "warning")

    def search_affiliate_products(self, keyword: str, region: str = "id", limit: int = 10, filters=None) -> list:
        """Read candidates from the currently open Shopee Affiliate product-offer dashboard."""
        keyword = (keyword or "").strip()
        region = _regions.normalize(region)
        limit = max(1, min(int(limit or 10), 100))
        aff_domain = self._affiliate_domain(region)
        try:
            from omnivora.shopee_session import ShopeeLoginSession

            self.log(f"Membaca dashboard Affiliate {region.upper()} yang sedang terbuka...", "info")
            session = ShopeeLoginSession(region, log_cb=self.log)
            try:
                page = self._affiliate_page(session, aff_domain)
            except Exception:
                session.open_affiliate(f"https://{aff_domain}/offer/product_offer")
                page = self._affiliate_page(session, aff_domain)

            html_text = getattr(page, "html", "") or ""
            current_url = getattr(page, "url", "") or ""
            if aff_domain not in current_url and "Penawaran Produk" not in html_text:
                self.log("Tab Affiliate belum ditemukan di Chrome session Omnivora, membuka Penawaran Produk...", "info")
                try:
                    session.open_affiliate(f"https://{aff_domain}/offer/product_offer")
                    page = self._affiliate_page(session, aff_domain)
                    time.sleep(2)
                    html_text = getattr(page, "html", "") or ""
                    current_url = getattr(page, "url", "") or ""
                except Exception:
                    pass
            self.log(f"Tab yang dibaca: {current_url or getattr(page, 'url', '')}", "info")

            blocked_reason = self._is_affiliate_blocked_or_logged_out(page)
            if blocked_reason:
                self.log(blocked_reason, "error")
                return []

            self.log("Mode aman: bot membaca produk yang sudah tampil di dashboard tanpa klik filter otomatis.", "info")
            if keyword:
                self.log(f"Keyword '{keyword}' dipakai untuk memfilter hasil yang terbaca dari halaman.", "info")

            results = []
            for attempt in range(18):
                html_text = getattr(page, "html", "") or ""
                results = self._extract_affiliate_results_from_html(html_text, region, limit)
                if keyword:
                    key_l = keyword.lower()
                    filtered = [item for item in results if key_l in (item.get("title", "") + " " + item.get("affiliate_context", "")).lower()]
                    results = filtered or results
                if results:
                    self.log(f"Dashboard Affiliate menemukan {len(results)} kandidat produk.", "success")
                    return results
                if attempt in (5, 11):
                    self.log("Menunggu card produk Affiliate yang tampil selesai terbaca...", "warning")
                time.sleep(1)
            self.log("Dashboard Affiliate belum menghasilkan kandidat. Pastikan halaman Penawaran Produk sudah terbuka dan produk terlihat.", "warning")
        except Exception as exc:
            self.log(f"Search dashboard Affiliate gagal: {exc}", "error")
        return []

    def scrape_product(self, url: str, force_browser: bool = False):
        """Scrape product info from Shopee (HTML/API).

        force_browser: langsung pakai jalur browser (terlihat) — dipakai untuk produk
        PERTAMA agar user bisa menyelesaikan puzzle/captcha Shopee secara manual."""
        long_url = self.resolve_url(url)
        domain, shop_id, item_id = self.extract_ids(long_url)

        if not shop_id or not item_id:
            self.log(f"Gagal mengekstrak ID produk dari URL: {long_url}", "error")
            return None

        region = _regions.detect_region_from_url(domain)

        # Construct a clean URL without tracking params to avoid redirection and auth errors
        clean_url = f"https://{domain}/product/{shop_id}/{item_id}"

        # Produk pertama: buka browser langsung supaya puzzle bisa diselesaikan manual
        # (jeda 15 detik ditangani di _scrape_product_browser lewat verification pending).
        if force_browser:
            return self._scrape_product_browser(clean_url, domain, region, shop_id, item_id)
        self.log(f"Menghubungi Shopee via HTML Parser di clean URL: {clean_url}", "info")
        mobile_headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": clean_url
        }
        mobile_headers = self._headers_for_region(region, mobile_headers)

        try:
            import json
            from bs4 import BeautifulSoup
            
            with httpx.Client(headers=mobile_headers, follow_redirects=True, timeout=15.0) as client:
                response = client.get(clean_url)
                if response.status_code == 200:
                    html = response.text
                    soup = BeautifulSoup(html, "html.parser")
                    scripts = soup.find_all("script")
                    
                    target_script = None
                    for s in scripts:
                        txt = s.string or ""
                        if '{"initialState":' in txt:
                            target_script = txt
                            break
                            
                    if target_script:
                        data = json.loads(target_script)
                        initial_state = data.get("initialState", {})
                        item_section = initial_state.get("item", {})
                        items_map = item_section.get("items", {})
                        
                        if items_map:
                            resolved_item_id = item_id if item_id in items_map else list(items_map.keys())[0]
                            item_data = items_map[resolved_item_id]
                            
                            title = item_data.get("name") or item_data.get("title") or ""
                            if not title.strip():
                                raise ValueError("Nama produk kosong pada HTML (kemungkinan dialihkan ke halaman utama / captcha)")
                            description = item_data.get("description") or ""
                            image_hashes = item_data.get("images", [])
                            image_urls = self._build_image_urls(image_hashes, region)
                            video_urls = self._collect_video_urls(item_data)
                                
                            shop_id_val = item_data.get("shopid") or item_data.get("shop_id") or shop_id
                            
                            self.log(f"Sukses mengambil data produk via HTML Parser: '{title[:40]}...'. Ditemukan {len(image_urls)} gambar, {len(video_urls)} kandidat video.", "success")
                            return {
                                "title": title,
                                "description": description,
                                "image_urls": image_urls,
                                "video_urls": video_urls,
                                "shop_id": str(shop_id_val),
                                "item_id": str(resolved_item_id),
                                "domain": domain,
                                "region": region
                            }
        except Exception as e:
            self.log(f"Gagal mengambil data via HTML Parser: {e}. Mencoba fallback ke API...", "warning")

        # 2. Fallback to API V4
        self.log(f"Menghubungi API Shopee fallback untuk ShopID: {shop_id}, ItemID: {item_id}", "info")
        api_url = f"https://{domain}/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
        custom_headers = self._headers_for_region(region)
        custom_headers["Referer"] = long_url

        try:
            with httpx.Client(headers=custom_headers, timeout=12.0) as client:
                response = client.get(api_url)
                if response.status_code != 200:
                    self.log(f"API Shopee merespon dengan status code {response.status_code}", "error")
                    return self._scrape_product_browser(clean_url, domain, region, shop_id, item_id)
                
                data = response.json()
                item = data.get("data") or data.get("item")
                if not item:
                    error_msg = data.get("error_msg") or "Format data tidak dikenal"
                    self.log(f"Gagal memuat detail item dari API Shopee: {error_msg}", "error")
                    return None

                title = item.get("name", "")
                description = item.get("description", "")
                image_hashes = item.get("images", [])
                image_urls = self._build_image_urls(image_hashes, region)
                video_urls = self._collect_video_urls(item)

                self.log(f"Sukses mengambil data produk via API: '{title[:40]}...'. Ditemukan {len(image_urls)} gambar, {len(video_urls)} kandidat video.", "success")
                return {
                    "title": title,
                    "description": description,
                    "image_urls": image_urls,
                    "video_urls": video_urls,
                    "shop_id": shop_id,
                    "item_id": item_id,
                    "domain": domain,
                    "region": region
                }

        except Exception as e:
            self.log(f"Error saat menghubungi API Shopee: {e}", "error")
            return None

    def search_products(self, keyword: str, region: str = "id", limit: int = 10,
                        sort_by: str = "relevancy", shop_type: str = "all") -> list:
        """Search Shopee products by keyword and return scrapeable product candidates."""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        region = _regions.normalize(region)
        domain = _regions.web_domain(region)
        limit = max(1, min(int(limit or 10), 50))
        encoded = quote_plus(keyword)

        sort_map = {"relevancy": "relevancy", "sales": "sales", "newest": "ctime"}
        by_param = sort_map.get(sort_by, "relevancy")

        extra_params = ""
        if shop_type == "mall":
            extra_params = "&official_shop=1"
        elif shop_type == "star_plus":
            extra_params = "&preferred_plus_seller=1"
        elif shop_type == "star":
            extra_params = "&shopee_verified=1"

        filter_desc = {"all": "Semua", "mall": "Mall", "star_plus": "Star+", "star": "Star"}.get(shop_type, "Semua")
        sort_desc = {"relevancy": "Relevansi", "sales": "Terlaris", "newest": "Terbaru"}.get(sort_by, "Relevansi")
        self.log(f"Mencari produk Shopee {region.upper()} | Sort: {sort_desc} | Filter: {filter_desc} | Keyword: {keyword}", "info")

        api_url = (
            f"https://{domain}/api/v4/search/search_items"
            f"?by={by_param}&keyword={encoded}&limit={limit}&newest=0&order=desc"
            f"&page_type=search&scenario=PAGE_GLOBAL_SEARCH{extra_params}"
        )
        self.log(f"Mencari produk Shopee {region.upper()} untuk keyword: {keyword}", "info")
        try:
            with httpx.Client(headers=self._headers_for_region(region), follow_redirects=True, timeout=15.0) as client:
                response = client.get(api_url)
                if response.status_code != 200:
                    self.log(f"Search API merespon status {response.status_code}", "warning")
                    return self._search_products_browser(keyword, region, limit, sort_by, shop_type)
                data = response.json()
        except Exception as exc:
            self.log(f"Gagal search produk via API: {exc}", "warning")
            return self._search_products_browser(keyword, region, limit, sort_by, shop_type)

        raw_items = data.get("items") or data.get("data", {}).get("items") or []
        results = []
        seen = set()
        for raw in raw_items:
            item_basic = raw.get("item_basic") if isinstance(raw, dict) else None
            item = item_basic or raw
            if not isinstance(item, dict):
                continue
            shop_id = item.get("shopid") or item.get("shop_id")
            item_id = item.get("itemid") or item.get("item_id")
            if not shop_id or not item_id:
                continue
            key = f"{shop_id}:{item_id}"
            if key in seen:
                continue
            seen.add(key)
            title = item.get("name") or item.get("title") or ""
            image = item.get("image") or next(iter(item.get("images") or []), "")
            url = f"https://{domain}/product/{shop_id}/{item_id}"
            results.append({
                "title": title,
                "shop_id": str(shop_id),
                "item_id": str(item_id),
                "domain": domain,
                "region": region,
                "url": url,
                "image_url": self._build_image_urls([image], region)[0] if image else "",
            })
            if len(results) >= limit:
                break
        if results:
            self.log(f"Search selesai: {len(results)} kandidat produk ditemukan.", "success")
            return results

        self.log("Search API tidak mengembalikan produk, mencoba fallback Chrome session...", "warning")
        results = self._search_products_browser(keyword, region, limit, sort_by, shop_type)
        self.log(f"Search selesai: {len(results)} kandidat produk ditemukan.", "success" if results else "warn")
        return results

    def download_image(self, img_url: str, save_path: str) -> bool:
        """Download image to path"""
        return self.download_file(img_url, save_path)

    def download_file(self, url: str, save_path: str) -> bool:
        """Download image/video/media to path."""
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            parsed = urlparse(url)
            region = _regions.detect_region_from_url(parsed.netloc)
            with httpx.Client(headers=self._headers_for_region(region), follow_redirects=True, timeout=30.0) as client:
                response = client.get(url)
                if response.status_code == 200:
                    with open(save_path, "wb") as f:
                        f.write(response.content)
                    return True
                self.log(f"Gagal mendownload media {url}: status {response.status_code}", "warn")
        except Exception as e:
            self.log(f"Gagal mendownload media {url}: {e}", "error")
        return False
