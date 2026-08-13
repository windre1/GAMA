import base64
import httpx
import json
import mimetypes
import os
import re
import random
import time

from omnivora import regions as _regions

# ── Prompt Generator dari gambar (Gemini Vision) ─────────────────────────────
# Gaya prompt mengikuti Prompt Library OMNIVORA: baris-baris instruksi konsep di
# atas, lalu paragraf deskripsi sinematik detail (kamera, cahaya, materi, 8k).

PROMPT_LANGS = {
    "indonesia": "Bahasa Indonesia",
    "english (us)": "English",
    "jepang": "Bahasa Jepang (Japanese)",
    "korea": "Bahasa Korea (Korean)",
    "prancis": "Bahasa Prancis (French)",
    "jerman": "Bahasa Jerman (German)",
    "spanyol (as)": "Bahasa Spanyol (Spanish)",
    "italia": "Bahasa Italia (Italian)",
    "portugis (br)": "Bahasa Portugis (Portuguese)",
    "rusia": "Bahasa Rusia (Russian)",
    "id": "Bahasa Indonesia",
    "en": "English",
    "ja": "Bahasa Jepang (Japanese)",
    "ko": "Bahasa Korea (Korean)",
    "fr": "Bahasa Prancis (French)",
    "de": "Bahasa Jerman (German)",
    "es": "Bahasa Spanyol (Spanish)",
    "it": "Bahasa Italia (Italian)",
    "pt": "Bahasa Portugis (Portuguese)",
    "ru": "Bahasa Rusia (Russian)",
    "ms": "Bahasa Melayu (Malaysia)",
    "fil": "Filipino (Tagalog)",
    "th": "Thai (ภาษาไทย)",
}

# CTA per platform — hanya dipakai untuk style jualan.
PROMPT_PLATFORMS = {
    "tiktok": {
        "label": "TikTok",
        "cta": "Akhiri dengan arahan klik keranjang kuning",
    },
    "shopee": {
        "label": "Shopee Video",
        "cta": "Akhiri dengan arahan klik keranjang oranye dan cek produk di Shopee",
    },
    "instagram": {
        "label": "Instagram Reels",
        "cta": "Akhiri dengan ajakan cek link di bio",
    },
    "youtube": {
        "label": "YouTube Shorts",
        "cta": "Akhiri dengan ajakan cek link di deskripsi",
    },
    "umum": {
        "label": "sosial media umum",
        "cta": "Akhiri dengan ajakan halus untuk membeli",
    },
    # ── Platform ber-NICHE ────────────────────────────────────────────────
    #
    # Kenapa niche dipisah jadi platform sendiri, bukan cuma gaya prompt:
    # kalau user memakai gaya umum (mis. "Storyboard") sementara platformnya
    # hanya "TikTok", AI tidak punya petunjuk niche sama sekali. Hasilnya
    # promptnya melenceng — konten rekomendasi hotel bisa berubah jadi seolah
    # MENJUAL hotel/apartemen, atau tercampur gaya affiliate produk.
    # Dengan platform ber-niche, arah konten terkunci dari dua sisi sekaligus.
    "tiktok_aff": {
        "label": "TikTok Affiliate",
        "cta": "Akhiri dengan arahan klik keranjang kuning",
        "niche": "affiliate",
        "fokus": (
            "Konten AFFILIATE PRODUK: subjek utamanya adalah PRODUK yang dijual. "
            "Tonjolkan bentuk, bahan, fitur, dan cara pakai produk."
        ),
        "larangan": (
            "JANGAN membuatnya jadi review tempat/hotel/kuliner/wisata. "
            "Fokus tetap pada produk, bukan lokasi."
        ),
    },
    "tiktok_go_wisata": {
        "label": "TikTok Go - Tempat Wisata",
        "cta": "Akhiri dengan ajakan simpan lokasi dan cek tag lokasi",
        "niche": "wisata",
        "fokus": (
            "Konten REKOMENDASI TEMPAT WISATA: subjek utamanya adalah TEMPAT. "
            "Tonjolkan suasana, pemandangan, aktivitas, dan spot menarik."
        ),
        "larangan": (
            "JANGAN menjual produk dan JANGAN terdengar seperti iklan properti. "
            "Ini rekomendasi tempat untuk dikunjungi, bukan barang untuk dibeli. "
            "Tanpa harga, diskon, atau keranjang belanja."
        ),
    },
    "tiktok_go_kuliner": {
        "label": "TikTok Go - Kuliner",
        "cta": "Akhiri dengan ajakan simpan tempat makan dan cek tag lokasi",
        "niche": "kuliner",
        "fokus": (
            "Konten REKOMENDASI KULINER: subjek utamanya MAKANAN/MINUMAN dan "
            "tempat makannya. Tonjolkan tekstur, penyajian, dan suasana tempat."
        ),
        "larangan": (
            "JANGAN menjual produk kemasan seperti konten affiliate. "
            "Ini rekomendasi tempat makan. Tanpa harga, diskon, atau keranjang."
        ),
    },
    "tiktok_go_hotel": {
        "label": "TikTok Go - Hotel / Penginapan",
        "cta": "Akhiri dengan ajakan simpan rekomendasi dan cek tag lokasi",
        "niche": "hotel",
        "fokus": (
            "Konten REKOMENDASI HOTEL/PENGINAPAN untuk calon tamu menginap: "
            "subjek utamanya kamar, fasilitas, view, dan ambience menginap."
        ),
        "larangan": (
            "JANGAN menulisnya seolah MENJUAL atau MENYEWAKAN properti "
            "(hotel/apartemen/rumah) kepada pembeli — ini rekomendasi tempat "
            "MENGINAP untuk traveler. Jangan pula memakai gaya affiliate "
            "produk. Tanpa harga, diskon, atau keranjang belanja."
        ),
    },
}

PROMPT_STYLES = {
    "hard_selling": {
        "label": "Hard Selling (Iklan TV)",
        "selling": True,
        "brief": (
            "Konsep video hard-selling dengan efek video produk seperti iklan TV. "
            "Tambahkan stiker dan teks setelah setiap kata agar lebih menarik. "
            "Sorot kamera pada detail produk dan kegunaannya. "
            "Tempo cepat dan energik, fokus penuh ke produk."
        ),
        "image_brief": (
            "Foto iklan hero-shot komersial: produk jadi bintang utama di tengah komposisi, "
            "latar podium/permukaan premium yang dramatis, pencahayaan studio tajam yang menonjolkan "
            "material produk, dan sisakan area kosong di bagian atas untuk teks promosi/diskon."
        ),
    },
    "soft_selling": {
        "label": "Soft Selling / Storytelling",
        "selling": True,
        "brief": (
            "Konsep storytelling pendek yang relatable: mulai dari masalah atau momen sehari-hari, "
            "produk muncul secara natural sebagai solusinya. Tone hangat dan jujur seperti rekomendasi teman, "
            "tanpa kesan iklan keras. Sorot produk saat dipakai dalam kehidupan nyata."
        ),
        "image_brief": (
            "Foto lifestyle natural: produk tampil sedang dipakai/dipajang dalam suasana keseharian yang "
            "hangat dan believable (bukan studio kaku), cahaya alami, terasa seperti foto rekomendasi teman "
            "yang estetik namun tetap jelas menampilkan produk."
        ),
    },
    "storyboard": {
        "label": "Storyboard (Multi-Adegan)",
        "selling": True,
        "brief": (
            "Pecah video menjadi 3-4 adegan berurutan (storyboard). Untuk tiap adegan sebutkan: "
            "detik mulai-selesai, gerakan kamera, aksi subjek, dan teks overlay singkat. "
            "Adegan pembuka harus hook kuat, adegan penutup berisi call-to-action."
        ),
        "image_brief": (
            "Foto katalog multi-angle dalam satu frame: kolase rapi 3 panel yang menampilkan produk dari "
            "sudut berbeda (keseluruhan, detail close-up material, dan saat dipakai), gaya katalog e-commerce "
            "premium dengan latar konsisten."
        ),
    },
    "aesthetic": {
        "label": "Aesthetic / Cinematic (Non-Jualan)",
        "selling": False,
        "brief": (
            "Konsep cinematic b-roll aesthetic untuk feed sosial media, BUKAN konten jualan. "
            "Mood dreamy dan tenang, slow motion, komposisi artistik, permainan cahaya alami, "
            "cocok untuk video moodboard atau vibes."
        ),
        "image_brief": (
            "Foto aesthetic moody untuk feed: komposisi artistik dengan negative space, permainan cahaya dan "
            "bayangan alami (golden hour / cahaya jendela), palet warna senada yang tenang, terasa seperti "
            "moodboard Pinterest, BUKAN foto jualan."
        ),
    },
    "edukasi": {
        "label": "Edukasi / Tips (Non-Jualan)",
        "selling": False,
        "brief": (
            "Konsep konten edukasi singkat terkait objek di gambar, BUKAN konten jualan. "
            "Buka dengan hook pertanyaan yang bikin penasaran, tampilkan poin tips sebagai teks di layar, "
            "gaya penyampaian jelas dan to the point seperti kreator edukasi."
        ),
        "image_brief": (
            "Foto latar konten edukasi: objek ditata rapi dan informatif (flat-lay atau sudut jelas), "
            "pencahayaan terang merata, dan sisakan area kosong yang lega untuk teks poin/tips ditambahkan "
            "nanti, BUKAN foto jualan."
        ),
    },
    "pov": {
        "label": "POV / Relatable (Non-Jualan)",
        "selling": False,
        "brief": (
            "Konsep konten POV/relatable ala meme sosial media, BUKAN konten jualan. "
            "Humor ringan yang bikin penonton merasa 'ini gue banget', teks POV di bagian atas layar, "
            "ekspresi dan situasi yang menggelitik."
        ),
        "image_brief": (
            "Foto adegan POV/relatable ala meme: objek difoto dari sudut pandang orang pertama dalam situasi "
            "keseharian yang lucu/menggelitik, terasa candid seperti jepretan HP, sisakan area kosong di atas "
            "untuk teks 'POV:', BUKAN foto jualan."
        ),
    },
    "asmr": {
        "label": "ASMR / Satisfying (Non-Jualan)",
        "selling": False,
        "brief": (
            "Konsep konten ASMR/satisfying, BUKAN konten jualan. Fokus pada tekstur permukaan, "
            "suara halus, dan gerakan pelan yang memuaskan (memencet, mengusap, menuang, membuka kemasan). "
            "Close-up ekstrem, pencahayaan lembut, tempo santai."
        ),
        "image_brief": (
            "Foto makro satisfying: close-up ekstrem pada tekstur dan detail material objek, pencahayaan "
            "lembut yang memanjakan mata, komposisi simetris/rapi yang memuaskan, fokus tajam pada tekstur, "
            "BUKAN foto jualan."
        ),
    },
    "tiktok_go_wisata": {
        "label": "TikTok Go - Tempat Wisata",
        "selling": False,
        "brief": (
            "Konsep konten rekomendasi tempat wisata untuk TikTok Go. Tonjolkan alasan tempat ini layak "
            "dikunjungi: suasana, pemandangan, aktivitas, spot foto, dan rasa penasaran penonton. "
            "Gaya travel creator yang natural, bukan iklan keras."
        ),
        "image_brief": (
            "Foto travel vertical-ready untuk TikTok Go: tempat wisata menjadi subjek utama, suasana lokasi "
            "terasa nyata dan mengundang, pemandangan/spot menarik terlihat jelas, cahaya natural sinematik, "
            "komposisi memberi ruang untuk teks rekomendasi singkat, tanpa harga atau klaim berlebihan."
        ),
    },
    "tiktok_go_kuliner": {
        "label": "TikTok Go - Kuliner",
        "selling": False,
        "brief": (
            "Konsep konten kuliner untuk TikTok Go. Fokus pada makanan/minuman, tekstur, penyajian, suasana "
            "tempat makan, dan hook rekomendasi yang bikin penonton ingin mencoba. Gaya food creator lokal "
            "yang jujur dan menggugah selera."
        ),
        "image_brief": (
            "Foto kuliner vertical-ready untuk TikTok Go: makanan atau minuman tampil menggugah selera dengan "
            "tekstur, detail topping, uap/saus/permukaan terlihat jelas, latar tempat makan tetap terasa natural, "
            "pencahayaan hangat bersih, komposisi cocok untuk rekomendasi kuliner tanpa teks harga atau diskon."
        ),
    },
    "tiktok_go_hotel": {
        "label": "TikTok Go - Hotel / Penginapan",
        "selling": False,
        "brief": (
            "Konsep konten rekomendasi hotel/penginapan untuk TikTok Go. Tonjolkan kenyamanan kamar, view, "
            "fasilitas, ambience, dan momen menginap yang terasa realistis untuk calon traveler. "
            "Gaya review singkat yang premium tapi tetap relatable."
        ),
        "image_brief": (
            "Foto hotel/penginapan vertical-ready untuk TikTok Go: kamar, fasilitas, lobby, kolam, view, atau "
            "detail interior terlihat bersih dan nyaman, nuansa menginap terasa premium-natural, pencahayaan "
            "rapi, komposisi memberi ruang untuk teks rekomendasi singkat, tanpa harga, diskon, atau klaim palsu."
        ),
    },
}

# Default hashtag TikTok per region (dipakai fallback & normalisasi hashtag)
_TIKTOK_TAGS = {
    "id": "#fyp #foryou #racuntiktok #tiktokmademebuyit #viral #fypシ",
    "sg": "#fyp #foryou #tiktokmademebuyit #tiktoksg #viral",
    "en": "#fyp #foryou #tiktokmademebuyit #tiktokfinds #viral",
    "my": "#fyp #foryou #tiktokmalaysia #racuntiktok #viral",
    "ph": "#fyp #foryou #tiktokph #tiktokfinds #viral",
    "th": "#fyp #foryou #tiktokthailand #ของมันต้องมี #viral",
}


class GeminiCaptionGenerator:
    def __init__(
        self,
        api_key: str = "",
        log_cb=None,
        api_keys=None,
        model: str = "gemini-2.0-flash",
        provider: str = "Gemini",
        groq_api_keys=None,
        groq_model: str = "llama-3.3-70b-versatile",
    ):

        keys = api_keys if api_keys is not None else [api_key]
        self.api_keys = [str(key).strip() for key in keys if str(key or "").strip()]
        self.api_key = self.api_keys[0] if self.api_keys else ""
        self.model = model or "gemini-2.0-flash"
        self.provider = provider or "Gemini"
        self.groq_api_keys = [str(key).strip() for key in (groq_api_keys or []) if str(key or "").strip()]
        self.groq_model = groq_model or "llama-3.3-70b-versatile"
        self.log = log_cb or (lambda msg, level="info": print(f"[{level.upper()}] {msg}"))
        self.last_error = ""

    def _active_keys(self):
        if self.provider.lower() == "groq":
            return self.groq_api_keys
        return self.api_keys

    def _keys_for_provider(self, provider):
        return self.groq_api_keys if provider.lower() == "groq" else self.api_keys

    def _provider_key_sets(self):
        primary = "groq" if self.provider.lower() == "groq" else "gemini"
        secondary = "gemini" if primary == "groq" else "groq"
        providers = []
        for provider in (primary, secondary):
            keys = self._keys_for_provider(provider)
            if keys:
                providers.append((provider, keys))
        return providers

    def generate_caption(self, title: str, description: str, region: str = "id", target_duration: float = 0.0) -> dict:
        """
        Generate promo caption and clean TTS script from product description using Gemini API.
        Returns a dict containing:
          - "caption": Instagram/TikTok style caption with emojis and hashtags.
          - "tts_script": Clean text without emojis, ready for TTS voiceover.
        """
        if not self._active_keys():
            self.last_error = f"{self.provider} API Key kosong."
            self.log(f"{self.last_error} Tidak bisa men-generate caption.", "error")
            return {}

        # Profil bahasa per region dari core.regions — PH/TH/MY punya bahasanya
        # sendiri, BUKAN English (lihat LANGUAGE_PROFILES di regions.py).
        profile = _regions.language_profile(region)
        language = profile["name"]
        cta = profile["cta"]
        default_tags = profile["tags"]
        words_per_sec = profile["wps"]
        if target_duration and target_duration > 0:
            target_words = int(target_duration * words_per_sec)
            tts_length_hint = f"Target durasi TTS {int(target_duration)} detik — buat sekitar {target_words} kata."
        else:
            tts_length_hint = "Target durasi TTS sekitar 15-25 detik (35-60 kata)."

        prompt = f"""Tugas Anda adalah membuat caption promosi dan naskah voiceover (TTS) yang sangat menarik dan persuasif untuk video promosi Shopee Affiliate {region.upper()}.

Detail Produk:
- Nama Produk: {title}
- Informasi Produk: {description[:1000]} (Diringkas)

Aturan compliance penting:
- Jangan overclaim. Hindari klaim mutlak seperti "paling bagus", "nomor satu", "pasti", "100% ampuh", "menyembuhkan", "garansi hasil", kecuali eksplisit ada di data produk.
- Jangan membuat klaim medis/kesehatan/keuangan yang tidak ada di deskripsi.
- Pakai bahasa aman: "bisa membantu", "cocok untuk", "praktis untuk", "terlihat", "dirancang untuk".
- Fokus ke fitur yang benar-benar muncul di data produk.

Anda harus merespon dalam format JSON murni dengan skema berikut:
{{
  "caption": "Teks copywriting untuk caption promosi di media sosial. Berikan 3-4 hashtag relevan seperti {default_tags}. Tambahkan juga CTA seperti {cta} dan emoji menarik.",
  "tts_script": "Naskah voiceover gaya hard-selling atau storytelling yang natural dalam bahasa {language}. {tts_length_hint} WAJIB: JANGAN sebut angka, ukuran, berat, harga, kode produk, tanggal, expired, atau spesifikasi teknis apapun. Fokus ke manfaat nyata, suasana pakai, dan ajakan beli yang terasa alami. Tidak ada emoji, hashtag, atau simbol."
}}

Kembalikan HANYA format JSON murni di atas. Jangan tambahkan teks penjelasan apa pun."""

        for provider, keys in self._provider_key_sets():
            provider_label = provider.title()
            self.log(f"Menghubungi {provider_label} untuk generate caption & TTS...", "info")
            if provider != self.provider.lower():
                self.log(f"Fallback ke {provider_label} karena provider utama gagal atau tidak tersedia.", "warn")
            max_tokens = max(500, int((target_duration or 0) * 12))
            for key_idx, api_key in enumerate(keys, 1):
                result = self._call_provider(api_key, prompt, max_tokens=max_tokens, provider=provider)
                if result:
                    return result
                if len(keys) > 1:
                    self.log(f"{provider_label} key #{key_idx} gagal, mencoba key berikutnya...", "warn")
        self.log("Semua API Key AI gagal dipakai.", "error")
        return {}

    def generate_safe_product_copy(self, title: str, description: str, region: str = "id", target_duration: float = 0.0, platform: str = "shopee") -> dict:
        """Rewrite raw product text into safe affiliate copy without overclaim.

        platform: 'shopee' (SEO marketplace) atau 'tiktok' (viral/FYP)."""
        if not self._active_keys():
            self.last_error = f"{self.provider} API Key kosong."
            self.log(f"{self.last_error} Tidak bisa rewrite deskripsi.", "error")
            return {}

        lang_map = {
            "id": "Bahasa Indonesia",
            "sg": "English",
            "en": "English",
            "my": "Bahasa Melayu (Malay)",
            "ph": "Filipino (Tagalog)",
            "th": "Thai (ภาษาไทย)",
        }
        language = lang_map.get((region or "id").lower(), "Bahasa Indonesia")
        words_per_sec = _regions.language_profile(region)["wps"]
        if target_duration and target_duration > 0:
            target_words = int(target_duration * words_per_sec)
            tts_length_hint = f"Target durasi TTS {int(target_duration)} detik — tulis sekitar {target_words} kata."
        else:
            tts_length_hint = "Target durasi TTS sekitar 15-25 detik (35-60 kata)."

        platform = (platform or "shopee").lower()
        if platform == "tiktok":
            role = "copywriter TikTok Shop & TikTok Affiliate yang jago bikin konten viral (FYP)"
            desc_style = "gaya storytelling singkat, hook kuat di kalimat pertama, bahasa gaul/kekinian, memancing rasa penasaran"
            tag_style = (
                "8-12 hashtag TikTok viral, SEMUA diawali '#', dipisah spasi. WAJIB sertakan campuran hashtag tren "
                "(#fyp #foryou #fypシ #racuntiktok #tiktokmademebuyit #viral) dan hashtag spesifik produk. Tanpa spasi di dalam satu hashtag."
            )
            tag_example = "#fyp #foryou #racuntiktok #tiktokmademebuyit #viral #namaproduk"
        else:
            role = "copywriter & SEO specialist Shopee Affiliate yang patuh aturan platform"
            desc_style = "SEO-friendly, mengandung kata kunci pencarian marketplace secara natural, informatif dan persuasif"
            tag_style = (
                "8-12 hashtag SEO Shopee, SEMUA diawali '#', dipisah spasi. WAJIB campuran hashtag umum Shopee "
                "(#racunshopee #shopeefinds #shopeehaul #rekomendasiproduk) dan hashtag spesifik produk. Tanpa spasi di dalam satu hashtag."
            )
            tag_example = "#racunshopee #shopeefinds #shopeehaul #rekomendasiproduk #namaproduk"

        prompt = f"""Anda adalah {role}.

Tugas:
Ubah data produk mentah menjadi DESKRIPSI PRODUK ({platform.upper()}) yang menarik dan aman tanpa overclaim, beserta HASHTAG (tagar).

Data produk:
Judul: {title}
Deskripsi mentah:
{description[:3500]}

Aturan wajib:
- Bahasa output: {language} (SEMUA teks output HARUS dalam bahasa ini).
- Gaya deskripsi: {desc_style}. Jangan keyword-stuffing berlebihan.
- Jangan menambah fakta baru yang tidak ada di data produk.
- Jangan overclaim atau klaim absolut: hindari "pasti", "terbaik", "nomor satu", "100% ampuh", "menyembuhkan", "anti gagal", "garansi hasil".
- Jika produk terkait kesehatan/kecantikan, gunakan bahasa aman seperti "membantu", "dapat digunakan", "terasa", "cocok untuk", bukan klaim medis.
- HASHTAG: {tag_style} Contoh format: {tag_example}
- {tts_length_hint} Gaya hard-selling atau storytelling yang natural dan mengalir.
- TTS DILARANG menyebut angka, ukuran, berat, harga, kode produk, tanggal, expired, atau spesifikasi teknis apapun.
- Fokus ke manfaat, suasana pakai, dan ajakan beli yang terasa alami, bukan deskripsi teknis.
- Deskripsi (caption) boleh pakai emoji, TTS tidak boleh pakai emoji/hashtag.

Kembalikan HANYA JSON valid:
{{
  "safe_summary": "ringkasan fitur produk yang netral dan faktual",
  "caption": "deskripsi produk {platform.upper()} yang menarik, aman tanpa overclaim (2-4 kalimat)",
  "hashtags": "{tag_example} ... (8-12 hashtag, SEMUA diawali #, dipisah spasi)",
  "tts_script": "script voiceover bersih tanpa emoji/hashtag",
  "claims_removed": ["daftar klaim berisiko yang dihindari atau dinetralkan"]
}}"""

        for provider, keys in self._provider_key_sets():
            provider_label = provider.title()
            if provider != self.provider.lower():
                self.log(f"Fallback rewrite ke {provider_label} karena provider utama gagal.", "warn")
            for key_idx, api_key in enumerate(keys, 1):
                result = self._call_provider(api_key, prompt, max_tokens=900, provider=provider)
                if result:
                    result["tts_script"] = self._clean_tts(result.get("tts_script", ""))
                    result["hashtags"] = self._normalize_hashtags(result.get("hashtags", ""), platform, region)
                    result.setdefault("safe_summary", "")
                    result.setdefault("claims_removed", [])
                    return result
                if len(keys) > 1:
                    self.log(f"{provider_label} key #{key_idx} gagal untuk rewrite, mencoba key berikutnya...", "warn")
        self.log("Semua API Key AI gagal untuk rewrite deskripsi.", "error")
        return {}

    def analyze_reference_image(self, image_path: str) -> dict:
        """Analisa satu gambar referensi via Gemini Vision dan kembalikan deskripsi
        visual singkat yang bisa disisipkan ke prompt Nano Banana.

        Return:
            {
                "subject": "makanan/produk/tempat utama",
                "colors": "warna dominan",
                "composition": "komposisi/sudut pandang/latar",
                "mood": "suasana/mood",
                "summary": "1-2 kalimat deskripsi visual siap-pakai"
            }
        atau {} bila gagal (lihat self.last_error).
        """
        if not self.api_keys:
            self.last_error = "Gemini API Key kosong."
            return {}
        if not image_path or not os.path.isfile(image_path):
            self.last_error = f"File gambar tidak ditemukan: {image_path}"
            return {}
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            self.last_error = f"Gagal membaca gambar: {e}"
            return {}
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        if not mime.startswith("image/"):
            mime = "image/jpeg"

        meta_prompt = (
            "Kamu adalah asisten analisis gambar untuk prompt generator foto AI.\n\n"
            "Lihat gambar terlampir dengan teliti. Ekstrak informasi berikut secara faktual "
            "(jangan mengarang merek/nama jika tidak terbaca jelas):\n\n"
            "Balas HANYA JSON valid tanpa markdown:\n"
            "{\"subject\": \"objek/subjek utama (mis: ayam goreng crispy di piring hitam)\","
            " \"colors\": \"warna dominan (mis: golden brown, putih, hijau)\","
            " \"composition\": \"komposisi dan latar (mis: close-up, latar meja kayu cafe)\","
            " \"mood\": \"suasana (mis: hangat, fresh, mewah, kasual)\","
            " \"summary\": \"1-2 kalimat deskripsi visual konkret yang siap dipakai sebagai "
            "bagian dari prompt foto AI, deskripsikan apa yang TERLIHAT di gambar secara spesifik\"}"
        )
        parts = [
            {"text": meta_prompt},
            {"inline_data": {"mime_type": mime, "data": img_b64}},
        ]
        import time as _time
        deadline = _time.time() + 60
        vision_models = [
            "gemini-2.0-flash",
            "gemini-2.5-flash",
            "gemini-1.5-flash",
            "gemini-flash-latest",
        ]
        for key_idx, key in enumerate(self.api_keys):
            for model in vision_models:
                if _time.time() > deadline:
                    break
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={key}"
                )
                body = {
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 400,
                        "responseMimeType": "application/json",
                    },
                }
                try:
                    resp = httpx.post(url, json=body, timeout=30)
                    if resp.status_code in (429, 503, 500):
                        continue
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    raw = ""
                    for cand in (data.get("candidates") or []):
                        for part in (cand.get("content", {}).get("parts") or []):
                            raw += part.get("text", "")
                    raw = raw.strip()
                    # Coba parse JSON
                    obj = None
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        m = re.search(r"\{.*\}", raw, re.DOTALL)
                        if m:
                            try:
                                obj = json.loads(m.group())
                            except Exception:
                                pass
                    if obj and obj.get("summary"):
                        return {
                            "subject": str(obj.get("subject") or "").strip(),
                            "colors": str(obj.get("colors") or "").strip(),
                            "composition": str(obj.get("composition") or "").strip(),
                            "mood": str(obj.get("mood") or "").strip(),
                            "summary": str(obj.get("summary") or "").strip(),
                        }
                except Exception:
                    continue
        self.last_error = "Gagal menganalisis gambar referensi via Gemini Vision."
        return {}

    def generate_visual_prompt(self, image_path: str, mode: str = "video",
                               style: str = "hard_selling", platform: str = "tiktok",
                               lang: str = "id", duration: str = "8s",
                               extra: str = "") -> dict:
        """Analisa gambar (Gemini Vision) lalu buat SATU prompt siap-pakai untuk
        Google Flow (mode='video', image-to-video) atau Nano Banana (mode='image').

        Gaya output mengikuti Prompt Library OMNIVORA: baris instruksi konsep di
        atas, ditutup paragraf deskripsi sinematik detail. Return {'prompt': ...}
        atau {} bila gagal (lihat self.last_error)."""
        if not self.api_keys:
            self.last_error = "Gemini API Key kosong. Prompt Generator butuh Gemini (vision)."
            self.log(self.last_error, "error")
            return {}
        if not image_path or not os.path.isfile(image_path):
            self.last_error = f"File gambar tidak ditemukan: {image_path}"
            self.log(self.last_error, "error")
            return {}

        style_key = str(style or "hard_selling").strip()
        platform = str(platform or "tiktok").strip()
        # Gaya tiktok_go_* dulu MEMAKSA platform jadi "tiktok" polos, sehingga
        # niche-nya hilang dari prompt. Sekarang platform diarahkan ke varian
        # ber-niche yang cocok, supaya gaya dan platform saling menguatkan
        # alih-alih saling menghapus.
        if style_key.startswith("tiktok_go_") and platform in ("tiktok", "umum", ""):
            platform = style_key
        style_info = PROMPT_STYLES.get(style_key) or PROMPT_STYLES["hard_selling"]
        plat_info = PROMPT_PLATFORMS.get(platform) or PROMPT_PLATFORMS["umum"]

        # Rambu niche: dipakai kalau platformnya ber-niche TAPI gayanya umum
        # (mis. platform "TikTok Go - Hotel" + gaya "Storyboard"). Tanpa ini
        # AI tidak tahu arah kontennya dan promptnya melenceng.
        niche_block = ""
        if plat_info.get("fokus"):
            niche_block = (
                "\n\nARAH KONTEN (WAJIB DIIKUTI, ini yang menentukan niche):\n"
                f"- {plat_info['fokus']}\n"
                f"- {plat_info.get('larangan', '')}"
            ).rstrip()
        language = PROMPT_LANGS.get(str(lang or "id").strip().lower(), lang or "Bahasa Indonesia")
        mode = (mode or "video").lower()
        selling = bool(style_info.get("selling"))

        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            self.last_error = f"Gagal membaca file gambar: {e}"
            self.log(self.last_error, "error")
            return {}
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        if not mime.startswith("image/"):
            mime = "image/jpeg"

        extra_block = f"\nCatatan tambahan dari user: {extra.strip()}" if (extra or "").strip() else ""

        if mode == "image":
            # ── Mode IMAGE (Nano Banana): prompt foto, BUKAN video ───────────
            if selling:
                gaya_block = (
                    f"Gaya foto: {style_info['label']} untuk konten {plat_info['label']}.\n"
                    f"{style_info['image_brief']}"
                )
            else:
                gaya_block = (
                    f"Gaya foto: {style_info['label']} untuk konten {plat_info['label']}.\n"
                    f"{style_info['image_brief']}\n"
                    "DILARANG ada elemen jualan: tanpa teks diskon, tanpa harga, tanpa keranjang."
                )

            contoh_image = (
                "Foto iklan komersial profesional dari botol kaca serum mewah berdiri di atas "
                "podium marmer putih dengan tetesan air segar di sekitarnya, latar belakang "
                "gradasi pastel lembut, pencahayaan studio tiga titik yang tajam menonjolkan "
                "kilau kaca dan label produk, pantulan lembut di permukaan podium, komposisi "
                "tengah dengan area kosong lega di bagian atas untuk teks promosi, "
                "fotorealistik, resolusi 8k, kelas komersial."
            )

            meta_prompt = f"""Kamu adalah prompt engineer profesional untuk konten sosial media Indonesia.

Lihat dan analisa gambar terlampir dengan teliti: kenali objek utamanya (jenis produk/subjek, warna, bentuk, bahan/tekstur, suasana latar). Jangan menebak merek bila tidak terbaca jelas, dan jangan mengarang fakta atau angka.

Prompt yang kamu tulis adalah prompt GENERATE FOTO untuk model Nano Banana di Google Flow: menghasilkan SATU FOTO baru berbasis gambar terlampir. Ini FOTO DIAM, BUKAN VIDEO.

{gaya_block}{niche_block}{extra_block}

Aturan penulisan prompt final:
- SELURUH prompt final ditulis dalam {language}.
- Bentuk output: SATU paragraf utuh yang mengalir — TANPA baris kosong, TANPA blok terpisah, TANPA daftar berbaris. Satu blok teks saja.
- Deskripsikan hasil akhir FOTO yang diinginkan: subjek persis seperti di gambar (warna, bentuk, material disebut spesifik), penataan/komposisi, latar, pencahayaan, mood, dan kualitas (fotorealistik, 8k, komersial).
- Jika gambar referensi menampilkan manusia, gunakan hanya pose/komposisi dan konteks tempatnya sebagai referensi: wajah model WAJIB dibuat berbeda dari orang asli, identitas tidak disalin, warna pakaian utama dibuat berbeda namun tetap serasi dengan lokasi/produk.
- Jika menambahkan teks, tipografi, sticker teks, label, atau efek tulisan di dalam foto, seluruh teks WAJIB memakai Bahasa Indonesia yang natural dan singkat.
- DILARANG KERAS menyebut kata: video, iklan video, durasi, detik, gerakan kamera, zoom, transisi, slow motion, stiker animasi, voiceover, atau klik keranjang. Ini prompt foto.
- Jangan overclaim (tanpa "terbaik", "nomor satu", "100%"), jangan sebut harga atau angka spesifikasi.

Contoh gaya (referensi struktur saja, jangan disalin mentah):
{contoh_image}

Balas HANYA JSON valid:
{{"prompt": "prompt final siap pakai"}}"""
        else:
            # ── Mode VIDEO (Flow image-to-video) ─────────────────────────────
            dur = str(duration or "8s").replace("s", "")
            if selling:
                gaya_block = (
                    f"Gaya konten: {style_info['label']} untuk platform {plat_info['label']}.\n"
                    f"{style_info['brief']}\n"
                    f"{plat_info['cta']}."
                )
            else:
                gaya_block = (
                    f"Gaya konten: {style_info['label']} untuk platform {plat_info['label']}.\n"
                    f"{style_info['brief']}\n"
                    "DILARANG ada call-to-action jualan (tanpa keranjang, tanpa ajakan beli, "
                    "tanpa harga). Boleh ditutup ajakan ringan seperti follow/simpan video."
                )

            contoh_omnivora = (
                "Buat penawaran video iklan\n"
                "Video dalam bahasa Indonesia\n"
                "Konsep video hard-selling dengan efek video produk seperti iklan TV\n"
                "Tambahkan stiker dan teks setelah setiap kata agar lebih menarik\n"
                "Sorot kamera pada detail produk dan kegunaannya\n"
                "Akhiri dengan arahan klik keranjang kuning\n"
                "Bidikan sinematik komersial dari botol kaca serum mewah di atas permukaan "
                "marmer basah yang memantulkan cahaya. Tetesan air dengan lembut memercik di "
                "sekitarnya dalam gerak lambat. Pencahayaan studio kelas atas, lampu latar "
                "lembut, menciptakan cahaya yang bersih dan elegan. Kamera memperbesar dengan "
                "mulus (lensa makro). Fotorealistik, resolusi 8k, kelas komersial, warna "
                "dengan nada pastel dan emas lembut, direkam secara profesional untuk TV."
            )

            meta_prompt = f"""Kamu adalah prompt engineer profesional untuk konten sosial media Indonesia.

Lihat dan analisa gambar terlampir dengan teliti: kenali objek utamanya (jenis produk/subjek, warna, bentuk, bahan/tekstur, suasana latar). Jangan menebak merek bila tidak terbaca jelas, dan jangan mengarang fakta atau angka.

Prompt yang kamu tulis adalah prompt IMAGE-TO-VIDEO untuk Google Flow (durasi sekitar {dur} detik). Gambar terlampir dipakai sebagai frame awal video, jadi deskripsi adegan harus konsisten dengan isi gambar.

{gaya_block}{niche_block}{extra_block}

Aturan penulisan prompt final:
- SELURUH prompt final ditulis dalam {language}.
- Bentuk output: SATU blok teks berurutan TANPA baris kosong di tengah — beberapa baris pendek instruksi konsep, langsung disambung (baris berikutnya, bukan paragraf terpisah) deskripsi sinematik yang sangat detail (subjek persis seperti di gambar, permukaan/latar, pencahayaan, gerakan kamera, kualitas fotorealistik 8k komersial).
- Deskripsi sinematik WAJIB menyebut objek di gambar secara spesifik (warna, bentuk, material) supaya hasil generate konsisten dengan gambar.
- Jika gambar referensi menampilkan manusia, jaga konteks dan blocking adegan tetapi wajah model WAJIB berbeda dari orang asli, identitas tidak disalin, dan warna pakaian utama dibuat berbeda namun tetap natural.
- Jika ada teks overlay, sticker teks, headline, caption visual, atau efek tulisan di layar, seluruh teks WAJIB Bahasa Indonesia yang natural dan singkat.
- Jangan overclaim (tanpa "terbaik", "nomor satu", "100%"), jangan sebut harga atau angka spesifikasi.

Contoh gaya (referensi struktur saja, jangan disalin mentah — perhatikan: satu blok tanpa baris kosong):
{contoh_omnivora}

Balas HANYA JSON valid:
{{"prompt": "prompt final siap pakai"}}"""

        parts = [
            {"text": meta_prompt},
            {"inline_data": {"mime_type": mime, "data": img_b64}},
        ]
        # Kuota Gemini per-model — kalau model utama kena 429/404, coba model
        # vision lain yang masih tersedia untuk key tersebut. Total dibatasi
        # deadline supaya request UI tidak menggantung bermenit-menit.
        import time as _time
        deadline = _time.time() + 100
        model_candidates = list(dict.fromkeys([
            self.model, "gemini-flash-latest", "gemini-3.5-flash",
            "gemini-3.1-flash-lite", "gemini-3-flash-preview",
            "gemini-2.5-flash", "gemini-2.0-flash",
        ]))
        for model in model_candidates:
            for key_idx, api_key in enumerate(self.api_keys, 1):
                if _time.time() > deadline:
                    self.last_error = ("Server Gemini sedang sibuk/limit — waktu percobaan habis. "
                                       "Coba lagi sebentar lagi. Detail terakhir: " + (self.last_error or "-"))
                    self.log(self.last_error, "error")
                    return {}
                result = self._call_gemini_vision(api_key, parts, max_tokens=4096, model=model)
                if result.get("prompt"):
                    # Paksa SATU blok: rapatkan baris kosong (composer Flow membaca
                    # blok terpisah sebagai 2 prompt).
                    result["prompt"] = re.sub(r"\n\s*\n+", "\n", result["prompt"]).strip()
                    return result
                if len(self.api_keys) > 1:
                    self.log(f"Gemini key #{key_idx} ({model}) gagal untuk prompt generator, coba berikutnya...", "warn")
            self.log(f"Model {model} gagal dipakai, coba model lain...", "warn")
        self.log("Semua Gemini API Key/model gagal dipakai untuk prompt generator.", "error")
        return {}

    def _call_gemini_vision(self, api_key: str, parts: list, max_tokens: int = 4096, model: str = None) -> dict:
        """Panggil Gemini generateContent dengan konten campuran teks + gambar."""
        model = model or self.model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        try:
            with httpx.Client(timeout=45.0) as client:
                response = client.post(url, json=payload)
                if response.status_code == 503:
                    # High demand sementara — tunggu sebentar lalu coba sekali lagi
                    import time as _time
                    _time.sleep(2.5)
                    response = client.post(url, json=payload)
                if response.status_code == 429:
                    self.last_error = ("Kuota Gemini habis (rate limit). Tunggu beberapa saat, "
                                       "atau tambah API Key Gemini lain di Pengaturan (otomatis rotasi).")
                    self.log(self.last_error, "warn")
                    return {}
                if response.status_code != 200:
                    self.last_error = f"Gemini HTTP {response.status_code}: {response.text[:300]}"
                    self.log(self.last_error, "error")
                    return {}
                data = response.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    self.last_error = "Gemini tidak mengembalikan konten (kemungkinan kena safety filter)."
                    self.log(self.last_error, "error")
                    return {}
                raw_text = ""
                for part in candidates[0].get("content", {}).get("parts", []):
                    raw_text += part.get("text", "")
                raw_text = raw_text.strip()
                prompt = ""
                try:
                    start, end = raw_text.find("{"), raw_text.rfind("}")
                    obj = json.loads(raw_text[start:end + 1] if start != -1 and end != -1 else raw_text)
                    prompt = str(obj.get("prompt") or "").strip()
                except Exception:
                    # JSON invalid (mis. output terpotong / newline mentah) —
                    # coba comot isi field "prompt" langsung via regex.
                    m = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)', raw_text, re.S)
                    if m:
                        try:
                            prompt = json.loads('"' + m.group(1) + '"').strip()
                        except Exception:
                            prompt = m.group(1).replace('\\n', '\n').replace('\\"', '"').strip()
                    else:
                        # Bukan JSON sama sekali — pakai teks mentah (masih berguna)
                        prompt = re.sub(r'^[\s`"\'{]+|[\s`"\'}]+$', '', raw_text).strip()
                if prompt:
                    self.last_error = ""
                    return {"prompt": prompt}
                self.last_error = "JSON Gemini tidak berisi field 'prompt'."
                return {}
        except Exception as e:
            self.last_error = str(e)
            self.log(f"Gagal memanggil Gemini Vision: {e}", "error")
            return {}

    # ── Video → Prompt (Gemini analisa video contoh) ─────────────────────────

    def _upload_media_to_gemini(self, api_key, path, mime):
        """Upload file (video) ke Gemini Files API (resumable) lalu tunggu ACTIVE.
        Return (file_uri, '') atau (None, pesan_error)."""
        base = "https://generativelanguage.googleapis.com"
        try:
            size = os.path.getsize(path)
        except OSError as e:
            return None, f"gagal baca file: {e}"
        try:
            with httpx.Client(timeout=180.0) as client:
                start = client.post(
                    f"{base}/upload/v1beta/files?key={api_key}",
                    headers={
                        "X-Goog-Upload-Protocol": "resumable",
                        "X-Goog-Upload-Command": "start",
                        "X-Goog-Upload-Header-Content-Length": str(size),
                        "X-Goog-Upload-Header-Content-Type": mime,
                        "Content-Type": "application/json",
                    },
                    json={"file": {"display_name": os.path.basename(path)}},
                )
                upload_url = start.headers.get("X-Goog-Upload-URL")
                if not upload_url:
                    return None, f"gagal memulai upload video (HTTP {start.status_code}): {start.text[:200]}"
                with open(path, "rb") as f:
                    data = f.read()
                up = client.post(
                    upload_url,
                    headers={
                        "Content-Length": str(size),
                        "X-Goog-Upload-Offset": "0",
                        "X-Goog-Upload-Command": "upload, finalize",
                    },
                    content=data,
                )
                info = (up.json() or {}).get("file", {})
                name = info.get("name")
                uri = info.get("uri")
                state = info.get("state")
                if not name:
                    return None, f"upload video gagal: {up.text[:200]}"
                # Tunggu Gemini selesai memproses video (PROCESSING → ACTIVE)
                for _ in range(40):
                    if state == "ACTIVE":
                        return uri, ""
                    if state == "FAILED":
                        return None, "Gemini gagal memproses video."
                    time.sleep(3)
                    rp = client.get(f"{base}/v1beta/{name}?key={api_key}")
                    info = rp.json() or {}
                    state = info.get("state", state)
                    uri = info.get("uri", uri)
                if state == "ACTIVE":
                    return uri, ""
                return None, "Video masih diproses Gemini (timeout). Coba video lebih pendek."
        except Exception as e:
            return None, f"error upload video: {e}"

    def generate_prompts_from_video(self, video_path, platform="tiktok", lang="id",
                                    duration="8s", extra=""):
        """Amati VIDEO contoh via Gemini lalu hasilkan prompt untuk MENIRU-nya:
        prompt foto (frame awal) + prompt image-to-video (gaya OMNIVORA).
        Return {observation, image_prompt, video_prompt} atau {} (lihat last_error)."""
        if not self.api_keys:
            self.last_error = "Gemini API Key kosong. Fitur ini butuh Gemini (analisa video)."
            self.log(self.last_error, "error")
            return {}
        if not video_path or not os.path.isfile(video_path):
            self.last_error = f"File video tidak ditemukan: {video_path}"
            self.log(self.last_error, "error")
            return {}

        mime = mimetypes.guess_type(video_path)[0] or "video/mp4"
        if not mime.startswith("video/"):
            mime = "video/mp4"
        language = PROMPT_LANGS.get(str(lang or "id").strip().lower(), lang or "Bahasa Indonesia")
        plat_info = PROMPT_PLATFORMS.get(platform) or PROMPT_PLATFORMS["umum"]
        dur = str(duration or "8s").replace("s", "")
        extra_block = f"\nCatatan tambahan dari user: {extra.strip()}" if (extra or "").strip() else ""

        meta_prompt = f"""Kamu adalah prompt engineer profesional untuk konten sosial media Indonesia.

Amati VIDEO contoh terlampir dengan sangat teliti. Perhatikan: subjek/produk utama, latar & suasana, gerakan kamera (pan/zoom/tracking), sudut pengambilan, transisi antar-adegan, tempo/pacing, teks overlay/stiker, efek visual, pencahayaan, warna & color grading, serta gaya keseluruhan.

Tujuan: buat prompt agar bisa MEMBUAT ULANG video ini SEMIRIP mungkin di Google Flow (image-to-video), plus prompt foto untuk frame awalnya. Jangan mengarang merek/teks yang tidak terlihat.

Hasilkan TIGA hal untuk platform {plat_info['label']}:
1. observation: ringkasan gaya video contoh dalam {language} (2-4 kalimat: subjek, gerakan kamera, tempo, efek, mood).
2. image_prompt: prompt FOTO (untuk model Nano Banana) guna membuat FRAME AWAL yang meniru look video. SATU paragraf mengalir dalam {language}. DILARANG menyebut kata video, kamera bergerak, durasi, atau detik. Deskripsikan subjek, komposisi, latar, pencahayaan, mood, kualitas fotorealistik 8k.
3. video_prompt: prompt IMAGE-TO-VIDEO untuk Google Flow (durasi sekitar {dur} detik) dalam {language}, meniru gerakan kamera, tempo, transisi, dan efek video contoh. Bentuk SATU blok teks berurutan TANPA baris kosong (beberapa baris instruksi konsep lalu disambung deskripsi sinematik detail). Konsisten dengan frame awal di image_prompt.{extra_block}

Aturan: seluruh teks dalam {language}. Jangan overclaim (tanpa "terbaik"/"nomor satu"/"100%"), jangan sebut harga/angka spesifikasi. video_prompt WAJIB satu blok tanpa baris kosong.

Balas HANYA JSON valid:
{{"observation": "...", "image_prompt": "...", "video_prompt": "..."}}"""

        self.log("Menyiapkan video contoh untuk dianalisa Gemini...", "info")
        import time as _time
        deadline = _time.time() + 240
        model_candidates = list(dict.fromkeys([
            self.model, "gemini-flash-latest", "gemini-3.5-flash",
            "gemini-3.1-flash-lite", "gemini-2.0-flash",
        ]))

        # Video kecil (< ~15 MB) dikirim INLINE base64 — tidak butuh Files API
        # (banyak key kena 403 'denied access' di Files API). Video besar pakai
        # Files API (upload dulu).
        INLINE_LIMIT = 15 * 1024 * 1024
        try:
            small = os.path.getsize(video_path) <= INLINE_LIMIT
        except OSError:
            small = False

        for api_key in self.api_keys:
            if small:
                try:
                    with open(video_path, "rb") as f:
                        vid_b64 = base64.b64encode(f.read()).decode("ascii")
                except Exception as e:
                    self.last_error = f"gagal baca video: {e}"
                    continue
                parts = [
                    {"inline_data": {"mime_type": mime, "data": vid_b64}},
                    {"text": meta_prompt},
                ]
            else:
                uri, err = self._upload_media_to_gemini(api_key, video_path, mime)
                if not uri:
                    self.last_error = err
                    self.log(f"Upload video gagal untuk satu key: {err}", "warn")
                    continue
                parts = [
                    {"file_data": {"mime_type": mime, "file_uri": uri}},
                    {"text": meta_prompt},
                ]
            for model in model_candidates:
                if _time.time() > deadline:
                    self.last_error = "Waktu analisa video habis. Coba lagi / video lebih pendek."
                    return {}
                obj = self._call_gemini_json(api_key, parts, model=model, max_tokens=4096)
                if obj and (obj.get("video_prompt") or obj.get("image_prompt")):
                    vp = re.sub(r"\n\s*\n+", "\n", str(obj.get("video_prompt") or "")).strip()
                    ip = str(obj.get("image_prompt") or "").strip()
                    self.last_error = ""
                    return {
                        "observation": str(obj.get("observation") or "").strip(),
                        "image_prompt": ip,
                        "video_prompt": vp,
                    }
                # Kuota key ini habis → semua model berbagi kuota per-key, jadi
                # langsung lompat ke API key berikutnya (jangan buang waktu).
                if "429" in (self.last_error or "") or "Kuota" in (self.last_error or ""):
                    self.log("Kuota key ini habis, pindah ke API key berikutnya...", "warn")
                    break
                self.log(f"Model {model} gagal analisa video, coba model lain...", "warn")
        self.log("Semua Gemini key/model gagal menganalisa video.", "error")
        return {}

    def generate_caption_from_video(self, video_path, platform="tiktok", lang="id",
                                    content_type="auto", extra=""):
        """Analisa video final lalu buat caption + hashtag siap upload.
        Return {title, caption, hashtags, seo_keywords, summary} atau {}."""
        if not self.api_keys:
            self.last_error = "Gemini API Key kosong. Generate caption video butuh Gemini."
            self.log(self.last_error, "error")
            return {}
        if not video_path or not os.path.isfile(video_path):
            self.last_error = f"File video tidak ditemukan: {video_path}"
            self.log(self.last_error, "error")
            return {}

        mime = mimetypes.guess_type(video_path)[0] or "video/mp4"
        if not mime.startswith("video/"):
            mime = "video/mp4"
        language = PROMPT_LANGS.get(str(lang or "id").strip().lower(), lang or "Bahasa Indonesia")
        plat_info = PROMPT_PLATFORMS.get(platform) or PROMPT_PLATFORMS["tiktok"]
        extra_block = f"\nCatatan tambahan dari user/sistem: {extra.strip()}" if (extra or "").strip() else ""

        meta_prompt = f"""Kamu adalah SEO social media copywriter Indonesia.

Amati VIDEO FINAL terlampir dengan teliti. Video ini sudah digabung/merge dan siap diupload. Kenali isi visualnya: tempat wisata, kuliner, hotel/penginapan, produk affiliate, suasana, teks overlay yang tampak, CTA, dan alur scene. Jangan mengarang merek/nama tempat/produk jika tidak terlihat jelas.

Tugas: buat caption upload dan hashtag SEO yang sangat relevan dengan isi video untuk platform {plat_info['label']}. Jenis konten: {content_type}.

Aturan:
- Seluruh output dalam {language}.
- Caption 2-4 kalimat, natural, tidak lebay, tidak overclaim, dan cocok untuk upload langsung.
- Jika konten terlihat seperti TikTok Go/tempat/kuliner/hotel, boleh pakai CTA lokasi seperti "klik tag lokasi", "cek tag lokasi", atau "simpan buat itinerary".
- Jika konten terlihat affiliate/produk, boleh pakai CTA aman seperti "cek keranjang kuning", "lihat detail produknya", atau "simpan dulu".
- Jika konten umum/sosmed, pakai CTA like/simpan/bagikan/follow secara natural.
- Hashtag 8-15 tag, campuran SEO spesifik + tag platform umum. Jangan pakai hashtag yang tidak relevan.
- Jangan sebut harga, diskon, klaim nomor satu/terbaik/100%, atau fakta yang tidak terlihat.
{extra_block}

Balas HANYA JSON valid:
{{"title":"judul pendek SEO","summary":"ringkasan isi video","caption":"caption siap upload","hashtags":"#tag1 #tag2 #tag3","seo_keywords":["keyword 1","keyword 2"]}}"""

        self.log("Menyiapkan video final untuk generate caption SEO...", "info")
        import time as _time
        deadline = _time.time() + 240
        model_candidates = list(dict.fromkeys([
            self.model, "gemini-flash-latest", "gemini-3.5-flash",
            "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash",
        ]))

        INLINE_LIMIT = 15 * 1024 * 1024
        try:
            small = os.path.getsize(video_path) <= INLINE_LIMIT
        except OSError:
            small = False

        for api_key in self.api_keys:
            if small:
                try:
                    with open(video_path, "rb") as f:
                        vid_b64 = base64.b64encode(f.read()).decode("ascii")
                except Exception as e:
                    self.last_error = f"gagal baca video: {e}"
                    continue
                parts = [
                    {"inline_data": {"mime_type": mime, "data": vid_b64}},
                    {"text": meta_prompt},
                ]
            else:
                uri, err = self._upload_media_to_gemini(api_key, video_path, mime)
                if not uri:
                    self.last_error = err
                    self.log(f"Upload video caption gagal untuk satu key: {err}", "warn")
                    continue
                parts = [
                    {"file_data": {"mime_type": mime, "file_uri": uri}},
                    {"text": meta_prompt},
                ]

            for model in model_candidates:
                if _time.time() > deadline:
                    self.last_error = "Waktu generate caption video habis. Coba lagi / video lebih pendek."
                    return {}
                obj = self._call_gemini_json(api_key, parts, model=model, max_tokens=3072)
                caption = str(obj.get("caption") or "").strip() if obj else ""
                hashtags = self._normalize_hashtags(obj.get("hashtags", ""), platform, "id") if obj else ""
                if caption:
                    self.last_error = ""
                    return {
                        "title": str(obj.get("title") or "").strip(),
                        "summary": str(obj.get("summary") or "").strip(),
                        "caption": caption,
                        "hashtags": hashtags,
                        "seo_keywords": obj.get("seo_keywords") or [],
                    }
                if "429" in (self.last_error or "") or "Kuota" in (self.last_error or ""):
                    self.log("Kuota key ini habis, pindah ke API key berikutnya...", "warn")
                    break
                self.log(f"Model {model} gagal generate caption video, coba model lain...", "warn")
        self.log("Semua Gemini key/model gagal generate caption video.", "error")
        return {}

    def _call_gemini_json(self, api_key, parts, model=None, max_tokens=4096):
        """Panggil Gemini generateContent (teks+media) → dict JSON hasil parse."""
        model = model or self.model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        try:
            with httpx.Client(timeout=90.0) as client:
                response = client.post(url, json=payload)
                if response.status_code == 503:
                    time.sleep(2.5)
                    response = client.post(url, json=payload)
                if response.status_code == 429:
                    self.last_error = ("Kuota Gemini habis (rate limit). Tunggu beberapa saat, "
                                       "atau tambah API Key Gemini lain di Pengaturan (otomatis rotasi).")
                    self.log(self.last_error, "warn")
                    return {}
                if response.status_code != 200:
                    self.last_error = f"Gemini HTTP {response.status_code}: {response.text[:300]}"
                    self.log(self.last_error, "error")
                    return {}
                candidates = (response.json() or {}).get("candidates", [])
                if not candidates:
                    self.last_error = "Gemini tidak mengembalikan konten (kemungkinan safety filter)."
                    return {}
                raw_text = ""
                for part in candidates[0].get("content", {}).get("parts", []):
                    raw_text += part.get("text", "")
                raw_text = raw_text.strip()
                try:
                    start, end = raw_text.find("{"), raw_text.rfind("}")
                    return json.loads(raw_text[start:end + 1] if start != -1 and end != -1 else raw_text)
                except Exception:
                    # Comot field satu per satu bila JSON rusak/terpotong
                    out = {}
                    for key in ("observation", "image_prompt", "video_prompt",
                                "title", "summary", "caption", "hashtags", "seo_keywords"):
                        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text, re.S)
                        if m:
                            try:
                                out[key] = json.loads('"' + m.group(1) + '"')
                            except Exception:
                                out[key] = m.group(1).replace('\\n', '\n').replace('\\"', '"')
                    return out
        except Exception as e:
            self.last_error = str(e)
            self.log(f"Gagal memanggil Gemini (JSON): {e}", "error")
            return {}

    def _normalize_hashtags(self, raw, platform="shopee", region="id") -> str:
        """Pastikan hasil tag berupa hashtag (#tagar). AI kadang mengembalikan kata
        kunci tanpa '#' atau dalam list — rapikan jadi string hashtag dipisah spasi."""
        if isinstance(raw, (list, tuple)):
            tokens = [str(t) for t in raw]
        else:
            tokens = re.split(r"[\s,]+", str(raw or ""))
        tags = []
        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue
            # Buang '#' berlebih & karakter non-tagar, gabungkan kata jadi satu tagar
            body = re.sub(r"[^0-9A-Za-z฀-๿]+", "", tok.lstrip("#"))
            if not body:
                continue
            tag = "#" + body
            if tag.lower() not in [t.lower() for t in tags]:
                tags.append(tag)
        if not tags:
            # AI tidak memberi tag sama sekali → pakai default per platform/region
            region = (region or "id").lower()
            if platform == "tiktok":
                return _TIKTOK_TAGS.get(region, _TIKTOK_TAGS["id"])
            # Default Shopee: hashtag contoh milik region tersebut dari regions.py
            return _regions.language_profile(region)["tags"]
        return " ".join(tags[:12])

    def fallback_safe_product_copy(self, title: str, description: str, region: str = "id", target_duration: float = 0.0, platform: str = "shopee") -> dict:
        """Local safe-copy fallback when AI provider is unavailable."""
        platform = (platform or "shopee").lower()
        _tiktok_tags = _TIKTOK_TAGS
        text = re.sub(r'\s+', ' ', f"{title}. {description or ''}").strip()
        risky_patterns = [
            (r'\b100%\s*(ampuh|aman|berhasil)\b', "dapat membantu"),
            (r'\b100%\s*(herbal(?:\s+oil|\s+minyak)?)\b', "berbahan herbal"),
            (r'\b100%\s*(original|ori)\b', "original"),
            (r'\b(pasti|terbaik|nomor\s*satu|no\.?\s*1|anti\s*gagal|garansi\s*hasil)\b', "pilihan yang bisa dipertimbangkan"),
            (r'\b(menyembuhkan|mengobati|hilangkan|sembuhkan|redakan|meredakan|mengatasi)\b', "membantu"),
        ]
        claims_removed = []
        safe_text = text
        for pattern, replacement in risky_patterns:
            for match in re.findall(pattern, safe_text, flags=re.IGNORECASE):
                if isinstance(match, tuple):
                    match = " ".join(part for part in match if part)
                claims_removed.append(str(match))
            safe_text = re.sub(pattern, replacement, safe_text, flags=re.IGNORECASE)

        sentences = re.split(r'(?<=[.!?])\s+', safe_text)
        if sentences and sentences[0].strip(" .!?").lower() == (title or "").strip(" .!?").lower():
            sentences = sentences[1:]
        summary = " ".join(sentences[:3]).strip() or title
        if len(summary) > 450:
            summary = summary[:447].rstrip() + "..."

        _tts_id = [
            lambda t, s: f"Kenalin, {t}! Produk ini beneran worth it buat kamu. Yuk langsung cek di halaman produknya.",
            lambda t, s: f"Kalau kamu lagi nyari sesuatu yang praktis dan andal, {t} jawabannya. Jangan sampai kehabisan!",
            lambda t, s: f"Produk ini lagi hits banget. {t} cocok banget buat aktivitas sehari-hari kamu. Buruan cek sekarang.",
            lambda t, s: f"Satu pilihan yang sayang banget dilewatin. {t}. Cek ulasan nyata dari pembeli lain di halamannya.",
            lambda t, s: f"Sudah ribuan orang pilih {t}. Nggak mau ketinggalan? Langsung ke halaman produknya ya.",
            lambda t, s: f"Buat kamu yang pengen upgrade kesehariannya, coba {t}. Klik linknya dan lihat sendiri.",
            lambda t, s: f"Ini dia yang kamu cari. {t} hadir buat bikin hari kamu lebih mudah. Cek sekarang sebelum habis!",
            lambda t, s: f"Jangan scroll dulu! {t} ini beneran layak masuk keranjang kamu. Cek harga terbaiknya sekarang.",
        ]
        _tts_sg = [
            lambda t, s: f"Looking for something reliable? {t} is worth checking out. Grab yours before it sells out!",
            lambda t, s: f"Here's one you don't want to miss. {t} is a crowd favourite on Shopee right now.",
            lambda t, s: f"Stop scrolling for a second. {t} might just be exactly what you need. Check it out today.",
            lambda t, s: f"Thousands of happy shoppers can't be wrong. {t} is trending on Shopee. Tap to see why.",
            lambda t, s: f"Upgrade your everyday with {t}. Simple, practical, and loved by real buyers. Check the link!",
            lambda t, s: f"If you've been on the fence about {t}, now's the time. See the reviews and decide for yourself.",
            lambda t, s: f"Great quality finds on Shopee. {t} is one of them. Don't sleep on this one!",
            lambda t, s: f"Your next favourite pick could be {t}. Head to the product page and see it for yourself.",
        ]
        _caption_tags_sg = [
            "#shopeesg #shopeefinds #affiliate",
            "#shopeesg #affiliatesg #shopeeshopping",
            "#shopeesg #sgdeals #shopeefinds",
        ]
        _caption_tags_id = [
            "#racunshopee #shopeefinds #shopeeaffiliate",
            "#shopee #rekomendasiproduk #shopeeaffiliate",
            "#shopeeindo #racunshopee #affiliateshopee",
        ]

        # For longer target durations, concatenate extra sentences to fill time.
        # ~2.3 words/sec ID, ~2.6 words/sec EN; each template ≈ 20-25 words ≈ ~9-11s.
        extra_sentences_needed = max(0, int((target_duration - 15) / 10)) if target_duration > 15 else 0

        region = (region or "id").lower()
        _fallback = {
            "my": {
                "tags": "#shopeemy #shopeefinds #barangviral #shopeemalaysia",
                "line": "Pilihan praktikal untuk kegunaan harian. Semak butiran produk sebelum membeli.",
                "tts": f"Sedang mencari barang berkualiti? {title} memang berbaloi. Semak halaman produk sekarang.",
            },
            "ph": {
                "tags": "#shopeeph #shopeefinds #budgetfinds #shopeehaul",
                "line": "Praktikal na piliin para sa pang-araw-araw. Tingnan ang detalye bago bumili.",
                "tts": f"Naghahanap ng maganda at sulit? {title} ang sagot. Tingnan ang product page ngayon.",
            },
            "th": {
                "tags": "#shopeeth #ของมันต้องมี #รีวิวสินค้า #ช้อปปี้",
                "line": "ตัวเลือกที่ใช้งานได้จริงในทุกวัน ดูรายละเอียดสินค้าก่อนสั่งซื้อ",
                "tts": f"กำลังมองหาของดีอยู่ใช่ไหม? {title} คุ้มค่าน่าลอง ดูหน้าสินค้าเลยตอนนี้",
            },
        }
        if region in _fallback:
            fb = _fallback[region]
            tags = _tiktok_tags.get(region, _tiktok_tags["id"]) if platform == "tiktok" else fb["tags"]
            caption = f"{title}\n\n{summary}\n\n{fb['line']}\n{tags}"
            tts_script = fb["tts"]
            return {
                "safe_summary": summary,
                "caption": caption,
                "hashtags": tags,
                "tts_script": self._clean_tts(tts_script),
                "claims_removed": sorted(set(claims_removed)),
                "fallback": True,
            }
        if region == "sg":
            tags = _tiktok_tags["sg"] if platform == "tiktok" else random.choice(_caption_tags_sg)
            caption = (
                f"{title}\n\n"
                f"{summary}\n\n"
                f"A practical pick for daily use. Check the product details before buying.\n{tags}"
            )
            pool = _tts_sg[:]
            chosen = random.choice(pool)
            tts_script = chosen(title, summary)
            pool.remove(chosen)
            for _ in range(min(extra_sentences_needed, len(pool))):
                extra = random.choice(pool)
                pool.remove(extra)
                tts_script += " " + extra(title, summary)
        else:
            tags = _tiktok_tags["id"] if platform == "tiktok" else random.choice(_caption_tags_id)
            caption = (
                f"{title}\n\n"
                f"{summary}\n\n"
                f"Cek detail produk dan sesuaikan dengan kebutuhan kamu sebelum checkout.\n{tags}"
            )
            pool = _tts_id[:]
            chosen = random.choice(pool)
            tts_script = chosen(title, summary)
            pool.remove(chosen)
            for _ in range(min(extra_sentences_needed, len(pool))):
                extra = random.choice(pool)
                pool.remove(extra)
                tts_script += " " + extra(title, summary)

        return {
            "safe_summary": summary,
            "caption": caption,
            "hashtags": tags,
            "tts_script": self._clean_tts(tts_script),
            "claims_removed": sorted(set(claims_removed)),
            "fallback": True,
        }

    def _call_provider(self, api_key: str, prompt: str, max_tokens: int = 500, provider: str = None) -> dict:
        provider = provider or self.provider
        if provider.lower() == "groq":
            return self._call_groq(api_key, prompt, max_tokens=max_tokens)
        return self._call_gemini(api_key, prompt, max_tokens=max_tokens)

    def _call_gemini(self, api_key: str, prompt: str, max_tokens: int = 500) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.65,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json"
            }
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, json=payload)
                if response.status_code != 200:
                    detail = response.text[:500]
                    self.last_error = f"Gemini HTTP {response.status_code}: {detail}"
                    self.log(f"Gemini API merespon dengan error status: {response.status_code}", "error")
                    self.log(detail, "error")
                    return {}
                
                data = response.json()
                candidates = data.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    if parts:
                        raw_text = parts[0].get("text", "").strip()
                        
                        # Parsing JSON secara robust
                        json_data = {}
                        try:
                            start_idx = raw_text.find('{')
                            end_idx = raw_text.rfind('}')
                            if start_idx != -1 and end_idx != -1:
                                json_data = json.loads(raw_text[start_idx:end_idx+1])
                            else:
                                json_data = json.loads(raw_text)
                        except Exception as parse_err:
                            self.last_error = f"Parse JSON Gemini gagal: {parse_err}"
                            self.log(f"Gagal memparsing JSON dari Gemini: {parse_err}", "error")
                            self.log(f"Response teks mentah: {raw_text}", "debug")

                        if json_data and ("caption" in json_data or "tts_script" in json_data):
                            caption = json_data.get("caption", "").strip()
                            tts_script = json_data.get("tts_script", "").strip()
                            # Pastikan tts_script dibersihkan secara ketat dari emoji & simbol
                            tts_script = self._clean_tts(tts_script)
                            self.log("Sukses generate JSON dari Gemini.", "success")
                            self.last_error = ""
                            json_data.update({
                                "caption": caption,
                                "tts_script": tts_script
                            })
                            return json_data
                        else:
                            # Fallback: jika gagal parse JSON, kembalikan teks mentah
                            self.log("Menggunakan fallback karena format JSON tidak sesuai.", "warn")
                            clean_caption = re.sub(r'[\`\{\}\"\[\]]', '', raw_text).strip()
                            clean_tts = self._clean_tts(raw_text)
                            return {
                                "caption": clean_caption,
                                "tts_script": clean_tts
                            }
                
                self.last_error = "Tidak ada konten yang dihasilkan dari Gemini API."
                self.log(self.last_error, "error")
                return {}

        except Exception as e:
            self.last_error = str(e)
            self.log(f"Gagal generate caption via Gemini: {e}", "error")
            return {}

    def _call_groq(self, api_key: str, prompt: str, max_tokens: int = 500) -> dict:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.groq_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.65,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, json=payload, headers=headers)
                if response.status_code != 200:
                    detail = response.text[:500]
                    self.last_error = f"Groq HTTP {response.status_code}: {detail}"
                    self.log(f"Groq API merespon error status: {response.status_code}", "error")
                    self.log(detail, "error")
                    return {}

                raw_text = (
                    response.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                return self._parse_ai_json(raw_text)
        except Exception as e:
            self.last_error = str(e)
            self.log(f"Gagal generate via Groq: {e}", "error")
            return {}

    def _parse_ai_json(self, raw_text: str) -> dict:
        json_data = {}
        try:
            start_idx = raw_text.find('{')
            end_idx = raw_text.rfind('}')
            if start_idx != -1 and end_idx != -1:
                json_data = json.loads(raw_text[start_idx:end_idx+1])
            else:
                json_data = json.loads(raw_text)
        except Exception as parse_err:
            self.last_error = f"Parse JSON AI gagal: {parse_err}"
            self.log(f"Gagal memparsing JSON AI: {parse_err}", "error")
            self.log(f"Response teks mentah: {raw_text[:500]}", "debug")

        if json_data and ("caption" in json_data or "tts_script" in json_data or "safe_summary" in json_data):
            caption = json_data.get("caption", "").strip()
            tts_script = self._clean_tts(json_data.get("tts_script", "").strip())
            json_data.update({"caption": caption, "tts_script": tts_script})
            self.log("Sukses generate JSON dari AI.", "success")
            self.last_error = ""
            return json_data

        clean_caption = re.sub(r'[\`\{\}\"\[\]]', '', raw_text or '').strip()
        clean_tts = self._clean_tts(raw_text or '')
        if clean_caption or clean_tts:
            return {"caption": clean_caption, "tts_script": clean_tts}
        return {}

    def _clean_tts(self, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'[–—−]', ' ', text)
        # strip measurements: 500ml, 100gr, 2kg, 30cm, 10x20, 120x60x30
        text = re.sub(r'\b\d+(?:[.,]\d+)?\s*(?:ml|gr|g|kg|mg|cm|mm|m|liter|l|oz|lb|pcs|pc|unit|buah|lembar|dus|pack|sachet|kapsul|tablet|botol|kaleng)\b', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\b\d+\s*[xX×]\s*\d+(?:\s*[xX×]\s*\d+)?\s*(?:cm|mm|m)?\b', '', text, flags=re.IGNORECASE)
        # strip prices: Rp 50.000, SGD 9.90, $12
        text = re.sub(r'(?:Rp\.?\s*|SGD\s*|USD?\s*|\$)\d[\d.,]*', '', text, flags=re.IGNORECASE)
        # strip percentages
        text = re.sub(r'\b\d+(?:[.,]\d+)?\s*%', '', text)
        # strip expiry/date patterns: 12/2026, 2026-12-01, best before xx
        text = re.sub(r'\b(?:exp(?:iry|ired)?|best\s*before|kadaluarsa)[^\.\n]{0,30}', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b', '', text)
        # strip SKU/code patterns: ABC-1234, #XYZ-99
        text = re.sub(r'#?\b[A-Z]{1,5}-\d{3,}\b', '', text)
        # strip standalone numbers (leftover)
        text = re.sub(r'(?<!\w)\d+(?:[.,]\d+)?(?!\w)', '', text)
        # strip symbols not useful in speech
        text = re.sub(r'[*#@|\\/\[\]{}<>^~`]', ' ', text)
        text = re.sub(r'-{2,}', ' ', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n+', '\n', text)
        # collapse leftover stray punctuation
        text = re.sub(r'([.,!?])\s*([.,!?])+', r'\1', text)
        text = re.sub(r'^\s*[.,!?:;]+', '', text, flags=re.MULTILINE)
        return text.strip()
