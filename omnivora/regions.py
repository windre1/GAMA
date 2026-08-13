"""Central registry of Shopee regions supported by OMNIVORA.

Single source of truth for everything that varies per marketplace: the Android
package name, web/affiliate domains, the Gemini Canvas TTS language, and whether
the region needs a premium (Enterprise/Lifetime) license.

Indonesia is the "home" market. Every other region uses Shopee's international
app/web flow, so automation code should branch on ``is_local(region)`` rather
than hard-coding ``region == "sg"``.
"""

# Display / iteration order used across the UI.
REGION_ORDER = ["id", "sg", "ph", "th", "my"]

DEFAULT_REGION = "id"

REGIONS = {
    "id": {
        "label": "Indonesia",
        "short": "ID",
        "package": "com.shopee.id",
        "web_domain": "shopee.co.id",
        "affiliate_domain": "affiliate.shopee.co.id",
        "tts_language": "Indonesia",
        "premium": False,
    },
    "sg": {
        "label": "Singapore",
        "short": "SG",
        "package": "com.shopee.sg",
        "web_domain": "shopee.sg",
        "affiliate_domain": "affiliate.shopee.sg",
        "tts_language": "English (US)",
        "premium": True,
    },
    "ph": {
        "label": "Philippines",
        "short": "PH",
        "package": "com.shopee.ph",
        "web_domain": "shopee.ph",
        "affiliate_domain": "affiliate.shopee.ph",
        "tts_language": "Filipina (Tagalog)",
        "premium": True,
    },
    "th": {
        "label": "Thailand",
        "short": "TH",
        "package": "com.shopee.th",
        "web_domain": "shopee.co.th",
        "affiliate_domain": "affiliate.shopee.co.th",
        "tts_language": "Thailand",
        "premium": True,
    },
    "my": {
        "label": "Malaysia",
        "short": "MY",
        "package": "com.shopee.my",
        "web_domain": "shopee.com.my",
        "affiliate_domain": "affiliate.shopee.com.my",
        "tts_language": "Melayu",
        "premium": True,
    },
}


def normalize(region):
    """Coerce any input to a known region code, defaulting to Indonesia."""
    region = (region or "").lower()
    return region if region in REGIONS else DEFAULT_REGION


def region_meta(region):
    return REGIONS[normalize(region)]


def package_for(region):
    return region_meta(region)["package"]


def web_domain(region):
    return region_meta(region)["web_domain"]


def web_home(region):
    return f"https://{web_domain(region)}"


def affiliate_domain(region):
    return region_meta(region)["affiliate_domain"]


def affiliate_home(region):
    return f"https://{affiliate_domain(region)}/"


def tts_language(region):
    return region_meta(region)["tts_language"]


def is_premium(region):
    return region_meta(region)["premium"]


def label(region):
    return region_meta(region)["label"]


def short_label(region):
    return region_meta(region)["short"]


def is_local(region):
    """True only for the home market (Indonesia). Every other region uses the
    international Shopee app/web flow (originally built for Singapore)."""
    return normalize(region) == DEFAULT_REGION


def premium_regions():
    return [code for code in REGION_ORDER if REGIONS[code]["premium"]]


# Kebijakan paket lisensi → region yang dibuka. Selaras dengan paket di LP web
# (lib/plans.ts). Region "id" selalu terbuka untuk paket aktif apa pun; region
# premium mengikuti tabel ini:
#   enterprise -> ID + SG
#   global     -> semua region (ID, SG, PH, TH, MY)
#   lifetime   -> semua region
PLAN_REGION_ACCESS = {
    "": {"id"},
    "trial": {"id"},
    "free_trial": {"id"},
    "basic": {"id"},
    "pro": {"id"},
    "enterprise": {"id", "sg"},
    "global": {"id", "sg", "ph", "th", "my"},
    "lifetime": {"id", "sg", "ph", "th", "my"},
}


def _normalize_plan(plan):
    return (plan or "").strip().lower().replace("-", "_").replace(" ", "_")


def plan_allows_region(plan, region):
    """True kalau paket ``plan`` boleh upload ke ``region``.

    Region non-premium (id) selalu diizinkan. Region premium (sg/ph/th/my)
    mengikuti PLAN_REGION_ACCESS: SG butuh Enterprise+, sedangkan PH/TH/MY
    hanya dibuka oleh paket Global atau Lifetime.
    """
    region = normalize(region)
    if not is_premium(region):
        return True
    allowed = PLAN_REGION_ACCESS.get(_normalize_plan(plan), {"id"})
    return region in allowed


def cookie_config_key(region):
    """Key used inside configs/environment.json for a region's saved cookie."""
    return f"shopee_cookie_{normalize(region)}"


# web_domain → region, longest domain first so shopee.co.th wins over a bare .co
_DOMAIN_TO_REGION = sorted(
    ((meta["web_domain"], code) for code, meta in REGIONS.items()),
    key=lambda kv: len(kv[0]),
    reverse=True,
)

# Loose TLD fragments used only when a full domain isn't present in the string.
_FRAGMENT_TO_REGION = [
    (".co.id", "id"),
    (".com.my", "my"),
    (".co.th", "th"),
    (".sg", "sg"),
    (".ph", "ph"),
]


def detect_region_from_url(url):
    """Best-effort region detection from a Shopee URL or domain string."""
    text = (url or "").lower()
    for domain, code in _DOMAIN_TO_REGION:
        if domain in text:
            return code
    for fragment, code in _FRAGMENT_TO_REGION:
        if fragment in text:
            return code
    return DEFAULT_REGION


# Per-region language profile for AI caption/script generation. Each market
# writes copy in its own native language — PH/TH/MY are NOT English.
#   name: output language instruction handed to the AI model
#   cta : example call-to-action in that language (schema hint)
#   tags: example hashtags for that market
#   wps : rough words-per-second used to size the TTS script to a duration
#   fallback_promo: 1-line caption promo used when the AI provider is offline
#   fallback_tts  : native TTS one-liners used when the AI provider is offline
LANGUAGE_PROFILES = {
    "id": {
        "name": "Bahasa Indonesia (gaya santai, persuasif, kekinian)",
        "cta": "klik keranjang oranye / link produk di bio!",
        "tags": "#racunshopee #shopeehaul #shopeeindonesia",
        "wps": 2.3,
        "fallback_promo": "Cobain {title}! Cocok banget buat kamu. Klik keranjang oranye sekarang!",
        "fallback_tts": [
            "Cinematic dan keren! Kenalin {title}, produk berkualitas yang wajib kamu miliki. Detailnya kece dan cara pakainya gampang banget. Yuk langsung klik keranjang oranye sekarang!",
            "Jangan scroll dulu! Perhatikan detail menarik dari {title}. Solusi andal untuk kenyamanan kamu. Klik keranjang oranye sebelum kehabisan!",
        ],
    },
    "sg": {
        "name": "Singaporean English (catchy, promo-focused)",
        "cta": "tap the product link to buy now!",
        "tags": "#shopeesg #shopeefinds #viral",
        "wps": 2.6,
        "fallback_promo": "Check out {title}! A practical pick for daily use. Tap the link!",
        "fallback_tts": [
            "Experience the cinematic details of {title}! Quality designed to make your life easier. Tap the product link to check it out now!",
            "Here is the trending {title} you need! Stunning style and reliable performance. Don't wait, tap the link to grab yours now!",
        ],
    },
    "my": {
        "name": "Bahasa Melayu (santai, memujuk, terkini)",
        "cta": "klik pautan produk untuk beli sekarang!",
        "tags": "#shopeemy #shopeehaul #viral",
        "wps": 2.5,
        "fallback_promo": "Jom tengok {title}! Pilihan praktikal untuk harian anda. Klik pautan!",
        "fallback_tts": [
            "Sinematik dan menarik! Kenali {title}, produk berkualiti yang wajib anda miliki. Butirannya hebat dan mudah digunakan. Jom klik pautan produk sekarang!",
            "Jangan skrol dulu! Lihat butiran menarik {title}. Pilihan yang boleh diharap untuk keselesaan anda. Klik pautan produk sebelum kehabisan!",
        ],
    },
    "th": {
        "name": "Thai / ภาษาไทย (catchy, persuasive, trendy)",
        "cta": "แตะลิงก์สินค้าเพื่อซื้อเลยตอนนี้!",
        "tags": "#shopeeth #shopeehaul #viral",
        "wps": 3.0,
        "fallback_promo": "ดู {title} สิ! ตัวเลือกที่ใช้งานได้จริงในทุกวัน แตะที่ลิงก์เลย!",
        "fallback_tts": [
            "สุดปัง! ขอแนะนำ {title} สินค้าคุณภาพที่คุณต้องมี รายละเอียดดีและใช้งานง่ายมาก แตะลิงก์สินค้าเลยตอนนี้!",
            "อย่าเพิ่งเลื่อนผ่าน! ดูรายละเอียดสุดพิเศษของ {title} ตัวเลือกที่ไว้ใจได้เพื่อความสะดวกของคุณ แตะลิงก์สินค้าก่อนของจะหมด!",
        ],
    },
    "ph": {
        "name": "Filipino / Tagalog (catchy, persuasive, conversational)",
        "cta": "i-tap ang link ng produkto para bumili na!",
        "tags": "#shopeeph #shopeehaul #viral",
        "wps": 2.6,
        "fallback_promo": "Tingnan ang {title}! Praktikal na pili para sa araw-araw. I-tap ang link!",
        "fallback_tts": [
            "Cinematic at astig! Ito ang {title}, isang de-kalidad na produkto na dapat mong makuha. Maganda ang detalye at napakadaling gamitin. I-tap ang link ng produkto ngayon!",
            "Huwag munang mag-scroll! Pansinin ang detalye ng {title}. Maaasahang piniling para sa iyong kaginhawaan. I-tap ang link bago maubos!",
        ],
    },
}


def language_profile(region):
    """Full language profile for AI copy generation in a region's language."""
    return LANGUAGE_PROFILES.get(normalize(region), LANGUAGE_PROFILES[DEFAULT_REGION])


def script_language(region):
    """Human-readable output language handed to the AI model for this region."""
    return language_profile(region)["name"]
