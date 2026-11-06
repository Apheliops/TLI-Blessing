# -*- coding: utf-8 -*-
"""
TLI Blessing Helper - Torchlight: Infinite "Blessings from the Gods" kâr hesaplayıcı.

UE_game.log dosyasını okur:
  * Exchange aramalarından (XchgSearchPrice) fiyatları öğrenir (Divinity Stone dahil)
  * Blessings alımlarını (BuyVendorGoods) ve yenilemeleri (RefreshVendorShop) kaydeder
  * Her teklif için "en fazla kaç DS verilir" eşiğini hesaplar

Sadece log dosyasını okur; oyuna dokunmaz, bellek okumaz.
Gereksinim: Python 3.9+ (yalnızca standart kütüphane).

Kullanım:
  python tli_blessing.py                 -> pencereyi açar
  python tli_blessing.py --replay LOG    -> log'u bir kez işler, özet yazar (test)
"""
import json
import math
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone

import i18n
from i18n import tr

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Kişisel dosyalar (ayarlar, fiyatlar, koşu kaydı, ekran kayıtları) ayrı klasörde: GitHub'a gitmez (.git/info/exclude)
USER_DIR = os.path.join(APP_DIR, "userdata")
DATA_FILE = os.path.join(USER_DIR, "tli_blessing_data.json")
ITEMS_FILE = os.path.join(APP_DIR, "items.json")

FE = "100300"   # Flame Elementium
DS = "5210"     # Divinity Stone

DEFAULT_SETTINGS = {
    "log_path": "",              # boş: ilk açılışta UE_game.log kendiliğinden aranır
    "tax": 0.20,                 # satış vergisi (0.20 = %20)
    "price_mode": "cheap3",      # cheap1 | cheap3 | cheap5 : en ucuz N ilanın ortalaması
    "min_profit_pct": 0.0,       # "AL" demek için gereken minimum kâr %
    "always_on_top": True,
    "auto_ocr": True,            # yenilemede ekranı otomatik oku
    "card_outline": True,        # önerilen (BUY) kartların etrafına oyunun üstünde çerçeve çiz
    "autostart": False,          # Windows açılışında gizli başlat (v6.6: yeni kurulumda kapalı; ayarı olan değişmez)
    "monitor": 1,                # oyunun açık olduğu ekran (1 = ana ekran)
    "tesseract_path": "",
    # Hariç tutulanlar (isim parçası veya ID)
    "no_recommend_ids": ["100200", "5080"],            # Flame Sand, Energy Core: okunur ama "AL" önerilmez
    "exclude_ids": ["101200"],                         # Bound Flame Sand (satılamaz)
    "exclude_name_parts": ["arcane", "bound"],         # Arcane Stone türleri, Bound eşyalar
}

# Blessings'te FE her zaman bu paketlerde görülüyor
DEFAULT_CATALOG = [[FE, 1], [FE, 5], [FE, 10],
                   ["5220", 20], ["5230", 20], ["5250", 1],     # Transcendence Wedge Magic/Rare/Legendary
                   ["5080", 10], ["5080", 20],                  # Energy Cores
                   ["5028", 10], ["5028", 20],                  # Netherrealm Resonance
                   ["200030", 1],                               # Ultimate Ember
                   ["100200", 20], ["100200", 60]]              # Flame Sand
CATALOG_VER = 4
APP_VERSION = "7.1"
# Güncelleme kontrolü: GitHub'daki son Release'in sürüm adı okunur (hiçbir veri gönderilmez)
RELEASES_API = "https://api.github.com/repos/Apheliops/TLI-Blessing/releases/latest"
RELEASES_URL = "https://github.com/Apheliops/TLI-Blessing/releases/latest"
ISSUES_NEW_URL = "https://github.com/Apheliops/TLI-Blessing/issues/new"
# Sonuç paylaşımı: GitHub Discussions'ta "Show and tell" kategorisi (form: .github/DISCUSSION_TEMPLATE)
SHARE_NEW_URL = "https://github.com/Apheliops/TLI-Blessing/discussions/new"
# Topluluk fiyatları (isteğe bağlı, varsayılan kapalı): Cloudflare Worker, kodu server\ klasöründe.
# Gönderilen: rastgele kurulum numarası + eşya numarası + fiyat + zaman. Başka hiçbir şey.
PRICES_API = "https://tli-prices.tli-blessing.workers.dev/v1/prices"
PRICE_SHARE_EVERY = 120          # sn: öğrenilen yeni fiyatlar en fazla bu sıklıkta gönderilir
# Geliştiricinin kendi kurulumu: bu dosya varsa içindeki anahtar gönderilir, sunucu bu fiyatları beklemeden yayınlar.
# Dosya sadece geliştiricinin bilgisayarında (kurulum klasörü değil, zip'e / GitHub'a girmez).
OWNER_KEY_FILE = os.path.join(os.environ.get("APPDATA", ""), "TLI_Blessing_Helper", "owner_key.txt")


def owner_key():
    try:
        with open(OWNER_KEY_FILE, encoding="utf-8") as f:
            k = f.read().strip()
        return k if re.fullmatch(r"[0-9a-f]{64}", k) else None
    except OSError:
        return None
PRICE_FETCH_EVERY = 30 * 60      # sn: ortak fiyatlar bu sıklıkta indirilir

# Envanter sekmesindeki eşya simgeleri: tlidb.com'daki eşya sayfasından bir kez indirilir, userdata\icons'ta saklanır
ICON_DIR = os.path.join(USER_DIR, "icons")
ICON_PX = 20
TLIDB_ICON_RX = re.compile(r'<img src="(https://cdn\.tlidb\.com/UI/Textures/[^"]+?\.(?:webp|png))"[^>]*class="ui_item_base')
ICON_RETRY_DAYS = 7


def icon_path(cid):
    return os.path.join(ICON_DIR, f"{cid}.png")


def icon_wanted(cid):
    """Simge indirilmeli mi: dosya yoksa ve son 7 günde bulunamamış olarak işaretlenmediyse."""
    if os.path.isfile(icon_path(cid)):
        return False
    miss = os.path.join(ICON_DIR, f"{cid}.none")
    return not (os.path.isfile(miss) and time.time() - os.path.getmtime(miss) < ICON_RETRY_DAYS * 86400)


def fetch_icon(cid, name, px=ICON_PX, out=None, img_url=None):
    """tlidb.com/en/<Eşya_Adı> sayfasındaki ilk eşya simgesini indirip küçük PNG olarak kaydeder
    (out verilmezse icon_path(cid)). img_url (eşya listesindeki tlidb simge adresi) varsa sayfa aranmaz.
    Sadece eşyanın adı gönderilir. Bulunamazsa <cid>.none bırakılır (bir hafta tekrar denenmez)."""
    out = out or icon_path(cid)
    import io
    import urllib.parse
    import urllib.request
    os.makedirs(ICON_DIR, exist_ok=True)
    head = {"User-Agent": f"TLI-Blessing-Helper/{APP_VERSION}"}
    try:
        src = img_url if img_url and TLIDB_IMG_RX.fullmatch(img_url) else None
        # oyundaki ad bazen küçük harfli ("Netherrealm resonance"); tlidb sayfası kelimeleri büyük harfle başlatıyor
        for cand in () if src else dict.fromkeys((name, " ".join(w[:1].upper() + w[1:] for w in name.split()))):
            slug = urllib.parse.quote(cand.replace(" ", "_"), safe="_'-(),.")
            try:
                with urllib.request.urlopen(urllib.request.Request("https://tlidb.com/en/" + slug, headers=head),
                                            timeout=10) as r:
                    m = TLIDB_ICON_RX.search(r.read().decode("utf-8", "replace"))
            except OSError:
                m = None
            if m:
                src = m.group(1)
                break
        if not src:
            raise ValueError("icon not found")
        with urllib.request.urlopen(urllib.request.Request(src, headers=head), timeout=10) as r:
            raw = r.read(2_000_000)
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGBA").resize((px, px), Image.LANCZOS)
        tmp = out + ".tmp"
        img.save(tmp, "PNG")
        os.replace(tmp, out)
        return True
    except Exception:
        try:
            open(os.path.join(ICON_DIR, f"{cid}.none"), "w").close()
        except OSError:
            pass
        return False


def tint_icon(img, color, px):
    """Renkli simgeyi sekme işaretine çevirir: tek renk (sekme yazısının rengi), şekil ve iç detaylar
    parlaklıktan (koyu yerler daha saydam). Kenardaki boşluk kırpılır."""
    from PIL import Image, ImageChops
    img = img.convert("RGBA")
    box = img.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox()
    if box:
        img = img.crop(box)
    w, h = img.size
    side = max(w, h)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(img, ((side - w) // 2, (side - h) // 2))
    sq = sq.resize((px, px), Image.LANCZOS)
    r, g, b, a = sq.split()
    lum = Image.merge("RGB", (r, g, b)).convert("L").point(lambda v: int(90 + 165 * min(1.0, v / 200)))
    alpha = ImageChops.multiply(lum, a)          # saydamlık × parlaklık
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    out = Image.new("RGBA", sq.size, rgb + (0,))
    out.putalpha(alpha)
    return out


def version_tuple(s):
    """"v6.1" / "6.1" -> (6, 1); karşılaştırma için."""
    return tuple(int(x) for x in re.findall(r"\d+", str(s))[:3])


RELEASE_ZIP_PREFIX = "https://github.com/Apheliops/TLI-Blessing/releases/download/"


def latest_release():
    """GitHub'daki son yayınlanmış sürüm: (etiket, sayfa adresi, zip adresi|None) ya da hata/bağlantı yoksa None."""
    import urllib.request
    try:
        req = urllib.request.Request(RELEASES_API, headers={"User-Agent": f"TLI-Blessing-Helper/{APP_VERSION}",
                                                            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.load(r)
        if rel.get("draft") or rel.get("prerelease") or not rel.get("tag_name"):
            return None
        zips = [a.get("browser_download_url", "") for a in rel.get("assets", [])
                if str(a.get("name", "")).lower().endswith(".zip")]
        zips = [u for u in zips if u.startswith(RELEASE_ZIP_PREFIX)]   # sadece bu reponun Release dosyası
        return rel["tag_name"], rel.get("html_url") or RELEASES_URL, (zips[0] if zips else None)
    except Exception:
        return None


def install_update(zip_url, status=lambda s: None):
    """Release zip'ini indirip program dosyalarının üstüne yazar. userdata\\, _yedek\\ ve backup\\'a dokunmaz;
    değişen her dosyanın eski hâli backup\\update-from-v<sürüm>\\ içine alınır (kullanıcı görüyor: İngilizce ad).
    -> requirements.txt değiştiyse True (yeni paket kurulmalı). İndirme/kontrol hatasında istisna fırlatır
    ve hiçbir dosyaya dokunulmaz; yazma ancak zip'in tamamı doğrulandıktan sonra başlar."""
    import io, shutil, urllib.request, zipfile
    if not zip_url or not zip_url.startswith(RELEASE_ZIP_PREFIX):
        raise ValueError("no download file in the release")
    status("Downloading…")
    req = urllib.request.Request(zip_url, headers={"User-Agent": f"TLI-Blessing-Helper/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        blob = r.read(20 * 1024 * 1024 + 1)
    if len(blob) > 20 * 1024 * 1024:
        raise ValueError("download is too large")
    status("Checking…")
    files = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            _, _, rel = name.partition("/")            # zip'te tek üst klasör var (TLI-Blessing/)
            parts = rel.split("/")
            if (not rel or name.startswith("/") or ":" in rel or ".." in parts
                    or parts[0].lower() in ("userdata", "_yedek", "backup", ".git")):
                raise ValueError(f"unexpected file in the update: {name}")
            files[rel] = z.read(info)
    if "tli_blessing.py" not in files:
        raise ValueError("the update does not contain tli_blessing.py")
    for rel, b in files.items():                       # bozuk/yarım dosya yazılmasın
        if rel.endswith(".py"):
            compile(b, rel, "exec")
    status("Installing…")
    bak = os.path.join(APP_DIR, "backup", f"update-from-v{APP_VERSION}")
    reqs_changed = False
    for rel, b in files.items():
        dst = os.path.join(APP_DIR, *rel.split("/"))
        old = None
        if os.path.isfile(dst):
            with open(dst, "rb") as f:
                old = f.read()
        if old == b:
            continue
        if rel == "requirements.txt":
            reqs_changed = True
        if old is not None:
            os.makedirs(os.path.dirname(os.path.join(bak, rel)), exist_ok=True)
            shutil.copyfile(dst, os.path.join(bak, rel))
    for rel, b in files.items():
        dst = os.path.join(APP_DIR, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".update-tmp"
        with open(tmp, "wb") as f:
            f.write(b)
        os.replace(tmp, dst)
    return reqs_changed


def restart_app(after_pip=False):
    """Yeni sürümü başlatır: yeni süreç bu süreç kapanana kadar bekler (tek kopya kuralı)."""
    import subprocess
    exe = sys.executable
    if exe.lower().endswith("python.exe") and os.path.isfile(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"                 # konsol penceresi açılmasın
    args = [exe, os.path.join(APP_DIR, "tli_blessing.py"), "--wait-pid", str(os.getpid())]
    if after_pip:
        args.append("--pip")
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(args, cwd=APP_DIR, creationflags=flags, close_fds=True)


def wait_for_pid(pid, timeout=30):
    """Eski sürüm kapanana kadar bekle (güncellemeden sonra yeniden başlatma)."""
    end = time.time() + timeout
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000, False, int(pid))      # SYNCHRONIZE
        if h:
            k32.WaitForSingleObject(h, int(timeout * 1000))
            k32.CloseHandle(h)
        return
    while time.time() < end:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return
        time.sleep(0.3)


def pip_install_requirements():
    """Güncellemeyle yeni paket geldiyse kur (Setup.bat'in yaptığı gibi)."""
    import subprocess
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe") and os.path.isfile(exe[:-11] + "python.exe"):
        exe = exe[:-11] + "python.exe"
    try:
        subprocess.run([exe, "-m", "pip", "install", "-r", os.path.join(APP_DIR, "requirements.txt"), "--quiet"],
                       cwd=APP_DIR, timeout=600, creationflags=0x08000000 if sys.platform == "win32" else 0)
    except Exception:
        pass

# Başlangıç fiyatları (28.10.2026 log'undan). Exchange'te aratınca güncel fiyatla değişir.
SEED_PRICES = {"5028": {"prices": [0.086957, 0.097015, 0.09902, 0.1, 0.1], "ts": 1793213507},
               "5210": {"prices": [0.01769, 0.018233, 0.018642, 0.018878, 0.018919], "ts": 1793213383},
               "5201": {"prices": [0.001692, 0.001802, 0.001813, 0.001974, 0.002], "ts": 1793213391},
               "5080": {"prices": [0.072835, 0.073171, 0.073846, 0.07392, 0.074179], "ts": 1793213402},
               "5220": {"prices": [0.007007, 0.007007, 0.008008, 0.008008, 0.008008], "ts": 1793213416},
               "5230": {"prices": [0.090535, 0.095095, 0.095095, 0.095095, 0.095238], "ts": 1793213424},
               "5250": {"prices": [6.0, 6.0, 6.0, 6.0, 6.0], "ts": 1793213450},
               "200030": {"prices": [24.0, 24.0, 24.0, 24.0, 24.0], "ts": 1793213499}}


# --------------------------------------------------------------------------- #
# Veri / ayarlar
# --------------------------------------------------------------------------- #
def load_items():
    try:
        with open(ITEMS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


ITEM_NAMES = load_items()

# Eşya tablosu güncel kalsın: items.json sürümle gelir, yeni sezonun eşyaları için TITrack'in (MIT) tlidb'den
# derlediği güncel liste günde bir indirilir (userdata\items_extra.json: ad + tlidb simge adresi).
# Listede hiç olmayan eşyalara oyuncu kendisi ad verebilir (userdata\item_names.json); bu adlar en önce gelir.
ITEMS_SEED_URL = "https://raw.githubusercontent.com/astockman99/TITrack/master/tlidb_items_seed_en.json"
ITEMS_EXTRA_FILE = os.path.join(USER_DIR, "items_extra.json")
ITEM_NAMES_USER_FILE = os.path.join(USER_DIR, "item_names.json")
ITEMS_REFRESH_EVERY = 24 * 3600
ITEM_IMGS = {}           # eşya id -> tlidb simge adresi (varsa simge sayfa aranmadan doğrudan indirilir)
USER_NAMED = set()       # adını oyuncunun verdiği eşyalar (sadece bunların adı değiştirilebilir)
TLIDB_IMG_RX = re.compile(r"https://cdn\.tlidb\.com/UI/Textures/[\w/.-]+\.(?:webp|png)")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def apply_item_extras():
    """İndirilmiş güncel listeyi ve oyuncunun verdiği adları ITEM_NAMES / ITEM_IMGS'e işler."""
    for cid, it in (_read_json(ITEMS_EXTRA_FILE).get("items") or {}).items():
        if isinstance(it, dict):
            if isinstance(it.get("n"), str) and it["n"].strip():
                ITEM_NAMES[str(cid)] = it["n"].strip()
                USER_NAMED.discard(str(cid))
            if isinstance(it.get("img"), str) and TLIDB_IMG_RX.fullmatch(it["img"]):
                ITEM_IMGS[str(cid)] = it["img"]
    for cid, n in _read_json(ITEM_NAMES_USER_FILE).items():
        # eşya artık listedeyse listedeki ad kullanılır
        if isinstance(n, str) and n.strip() and (str(cid) not in ITEM_NAMES or str(cid) in USER_NAMED):
            ITEM_NAMES[str(cid)] = n.strip()[:60]
            USER_NAMED.add(str(cid))


def refresh_item_list(force=False):
    """Güncel eşya listesini indirir (son indirmeden 24 saat geçtiyse). -> True: liste yenilendi.
    Hiçbir bilgi gönderilmez, sadece GitHub'daki dosya okunur."""
    if not force:
        try:
            if time.time() - os.path.getmtime(ITEMS_EXTRA_FILE) < ITEMS_REFRESH_EVERY:
                return False
        except OSError:
            pass
    import urllib.request
    try:
        req = urllib.request.Request(ITEMS_SEED_URL, headers={"User-Agent": f"TLI-Blessing-Helper/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            seed = json.loads(r.read(8_000_000).decode("utf-8"))
        out = {}
        for it in seed.get("items") or []:
            cid = str(it.get("id") or "")
            if not re.fullmatch(r"\d{1,9}", cid):
                continue
            row = {}
            if isinstance(it.get("name_en"), str) and it["name_en"].strip():
                row["n"] = it["name_en"].strip()[:80]
            if isinstance(it.get("img"), str) and TLIDB_IMG_RX.fullmatch(it["img"]):
                row["img"] = it["img"]
            if row:
                out[cid] = row
        if len(out) < 500:                       # bozuk/yarım dosya: eskisi kalsın
            raise ValueError("item list too short")
        os.makedirs(USER_DIR, exist_ok=True)
        tmp = ITEMS_EXTRA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "items": out}, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, ITEMS_EXTRA_FILE)
        return True
    except Exception:
        try:                                      # başarısız deneme de sayılsın: her açılışta tekrar denenmesin
            if os.path.isfile(ITEMS_EXTRA_FILE):
                os.utime(ITEMS_EXTRA_FILE, None)
        except OSError:
            pass
        return False


def save_item_name(cid, name):
    """Oyuncunun listede olmayan bir eşyaya verdiği ad. -> False: ad boş."""
    name = " ".join((name or "").split())[:60]
    if not name:
        return False
    names = _read_json(ITEM_NAMES_USER_FILE)
    names[str(cid)] = name
    ITEM_NAMES[str(cid)] = name
    USER_NAMED.add(str(cid))
    os.makedirs(USER_DIR, exist_ok=True)
    tmp = ITEM_NAMES_USER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ITEM_NAMES_USER_FILE)
    return True
# Blessings dükkânında hiç satılmayan eşyalar: OCR'ın yakın-isim eşleşmesi bunlara düşmesin, listeye girmesin.
# (Elixir of Oblivion okunamayan bir kartın yazısından yanlış eşleşip listeye eklenmişti.)
NOT_IN_BLESSINGS = {"5011", "6011"}          # Elixir of Oblivion, Bound Elixir of Oblivion
BLESSING_NAMES = {k: v for k, v in ITEM_NAMES.items() if k not in NOT_IN_BLESSINGS}
apply_item_extras()        # Blessings eşleşmesi sürümle gelen tabloyla kalır; envanter güncel adları kullanır

USER_FILES = ["tli_blessing_data.json", "rakamlar.json", "voidsea_kayit.json", "voidsea_kayit.csv",
              "voidsea_kayit.yedek.json", "voidsea_model.json", "son_tarama.png", "son_tarama_voidsea.png",
              "hata_log.txt", "ornekler", "simge.png"]


def migrate_user_files():
    """v4.9: kişisel dosyalar uygulama klasöründen userdata klasörüne TAŞINIR (silinmez, kopyalanmaz).
    Hedefte aynı adlı dosya varsa ikisine de dokunulmaz."""
    try:
        os.makedirs(USER_DIR, exist_ok=True)
        names = USER_FILES + [f for f in os.listdir(APP_DIR) if f.startswith("voidsea_kayit")]
    except OSError:
        return
    for name in dict.fromkeys(names):
        src, dst = os.path.join(APP_DIR, name), os.path.join(USER_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    # v5.5: userdata içindeki Türkçe dosya adları İngilizce adlara TAŞINIR (yeniden adlandırma; silinmez).
    # Yeni adda dosya zaten varsa ikisine de dokunulmaz.
    try:
        extra = [(f, "voidsea_runs.json.corrupt-" + f.split(".bozuk-", 1)[1])
                 for f in os.listdir(USER_DIR) if f.startswith("voidsea_kayit.json.bozuk-")]
    except OSError:
        extra = []
    for old, new in list(USER_RENAMES.items()) + extra:
        src, dst = os.path.join(USER_DIR, old), os.path.join(USER_DIR, new)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.replace(src, dst)
            except OSError:
                pass


USER_RENAMES = {"rakamlar.json": "learned_digits.json", "voidsea_kayit.json": "voidsea_runs.json",
                "voidsea_kayit.csv": "voidsea_runs.csv", "voidsea_kayit.yedek.json": "voidsea_runs.backup.json",
                "son_tarama.png": "last_scan.png", "son_tarama_voidsea.png": "last_scan_voidsea.png",
                "hata_log.txt": "error_log.txt", "ornekler": "price_samples", "simge.png": "icon.png"}


def user_file(name):
    """userdata içindeki dosyanın yolu. Yeniden adlandırma bir sebeple yapılamadıysa (dosya kilitli vb.)
    eski adı kullan: kayıt asla ikiye bölünmesin."""
    new = os.path.join(USER_DIR, name)
    old = next((os.path.join(USER_DIR, o) for o, n in USER_RENAMES.items() if n == name), None)
    if old and not os.path.exists(new) and os.path.exists(old):
        return old
    return new


migrate_user_files()


# --------------------------------------------------------------------------- #
# Hata raporu: okuma hataları ve program hataları, ekran görüntüsüyle birlikte
# userdata\error_report.md (en yeni en altta) + userdata\error_screens\*.jpg
# --------------------------------------------------------------------------- #
REPORT_MAX_BYTES = 300_000      # md bundan büyürse eski kayıtların yarısı silinir
REPORT_MAX_SCREENS = 30
_last_report = {"key": None, "t": 0.0}


def report_error(area, message, details=(), img=None):
    """Bir hatayı error_report.md'ye yazar; varsa ekran görüntüsünü error_screens'e kaydedip bağlar.
    Aynı hata 60 sn içinde tekrar gelirse yazılmaz (otomatik tekrar denemeler kaydı şişirmesin)."""
    details = [str(d) for d in details if d]
    key = (area, message, tuple(details))
    now = time.time()
    if key == _last_report["key"] and now - _last_report["t"] < 60:
        return
    _last_report.update(key=key, t=now)
    try:
        stamp = datetime.now()
        lines = [f"## {stamp.strftime('%Y-%m-%d %H:%M:%S')} — {area}", "",
                 f"- **App version:** v{APP_VERSION}"]
        if img is not None:
            lines.append(f"- **Captured area:** {img.size[0]}×{img.size[1]}")
        lines.append(f"- **Error:** {message}")
        if details:
            lines.append("- **Details:**")
            for d in details:
                if "\n" in d:                      # çok satırlı (program hatası): kod bloğu
                    lines += ["", "```text", d.rstrip(), "```"]
                else:
                    lines.append(f"  - {d}")
        if img is not None:
            sdir = os.path.join(USER_DIR, "error_screens")
            os.makedirs(sdir, exist_ok=True)
            name = f"{stamp.strftime('%Y%m%d-%H%M%S')}_{area.split()[0].lower()}.jpg"
            img.convert("RGB").save(os.path.join(sdir, name), quality=90)
            lines += ["", f"![screenshot](error_screens/{name})"]
            old = sorted(f for f in os.listdir(sdir) if f.endswith(".jpg"))
            for f in old[:-REPORT_MAX_SCREENS]:
                os.remove(os.path.join(sdir, f))
        path = os.path.join(USER_DIR, "error_report.md")
        text = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
        if not text:
            text = ("# TLI Blessing Helper — error report\n\n"
                    "Screen-reading problems and program errors, newest at the bottom. "
                    "Attach this file and the matching screenshot from `error_screens` to a bug report.\n")
        text = text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"
        if len(text.encode("utf-8")) > REPORT_MAX_BYTES:        # eski kayıtların yarısını at
            head, _, rest = text.partition("\n## ")
            entries = rest.split("\n## ")
            text = head + "\n## " + "\n## ".join(entries[len(entries) // 2:])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        pass


def item_name(cid):
    return ITEM_NAMES.get(str(cid)) or f"Item #{cid}"


DATA_BACKUP = os.path.join(USER_DIR, "tli_blessing_data.backup.json")   # bir önceki sağlam ayar dosyası


def _read_saved_data():
    """Ayar dosyasını okur. Dosya bir an kilitliyse (güncellemeden sonra yeniden açılış, antivirüs) birkaç kez
    dener. Okunamazsa bozuk dosya kenara alınır ve yedekten okunur: ayarlar hiçbir durumda sessizce
    varsayılana dönmesin (eskiden okunamayınca varsayılanlarla açılıyor, ilk kayıtta dosyanın üstüne yazıyordu)."""
    if not os.path.exists(DATA_FILE):
        return _read_backup()
    for i in range(5):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                return saved
            break
        except ValueError:
            break                                   # bozuk içerik: beklemek düzeltmez
        except OSError:
            time.sleep(0.3 * (i + 1))               # kilitli: biraz bekleyip yeniden dene
    try:
        os.replace(DATA_FILE, DATA_FILE + f".corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    except OSError:
        pass
    return _read_backup()


def _read_backup():
    try:
        with open(DATA_BACKUP, encoding="utf-8") as f:
            saved = json.load(f)
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def load_data():
    data = {"settings": dict(DEFAULT_SETTINGS), "prices": {}, "catalog": list(DEFAULT_CATALOG)}
    try:
        saved = _read_saved_data()
        data["settings"].update(saved.get("settings", {}))
        data["prices"].update(saved.get("prices", {}))
        if "catalog" in saved:
            data["catalog"] = saved["catalog"]
            if saved.get("catalog_ver", 1) < CATALOG_VER:      # eski kayıtlara yeni varsayılanları ekle
                for c in DEFAULT_CATALOG:
                    if c not in data["catalog"]:
                        data["catalog"].append(c)
    except Exception:
        pass
    st_ = data["settings"]            # Flame Sand artık hariç değil (eski ayar dosyalarını güncelle)
    st_["exclude_ids"] = [i for i in st_.get("exclude_ids", []) if i != "100200"]
    st_["exclude_name_parts"] = [p for p in st_.get("exclude_name_parts", []) if p != "flame sand"]
    if "bound" not in st_["exclude_name_parts"]:
        st_["exclude_name_parts"].append("bound")
    data["catalog_ver"] = CATALOG_VER
    data["catalog"] = [c for c in data["catalog"]
                       if isinstance(c, list) and len(c) == 2 and isinstance(c[1], int)
                       and (c[0] == FE or c[1] <= 200) and c[0] not in NOT_IN_BLESSINGS]
    for k, v in SEED_PRICES.items():          # hiç fiyatı olmayanlara başlangıç değeri
        data["prices"].setdefault(k, dict(v))
    return data


def save_data(data):
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(DATA_FILE):             # bir önceki sağlam hâl yedekte kalsın
            try:
                import shutil
                shutil.copyfile(DATA_FILE, DATA_BACKUP)
            except OSError:
                pass
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass


def _steam_libraries():
    libs = []
    try:
        import winreg
        for hive, key, val in [(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")]:
            try:
                with winreg.OpenKey(hive, key) as k:
                    libs.append(winreg.QueryValueEx(k, val)[0])
            except OSError:
                pass
    except ImportError:
        pass
    for base in list(libs):
        vdf = os.path.join(base, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s*"([^"]+)"', f.read()):
                    libs.append(m.group(1).replace("\\\\", "\\"))
        except OSError:
            pass
    return libs


def find_log_candidates():
    rel = os.path.join("UE_game", "TorchLight", "Saved", "Logs", "UE_game.log")
    games = []
    for lib in _steam_libraries():
        games.append(os.path.join(lib, "steamapps", "common", "Torchlight Infinite"))
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        for r in ["Program Files (x86)\\Steam", "Program Files\\Steam", "SteamLibrary", "Steam",
                  "Games\\Steam", "Games\\SteamLibrary", "Oyunlar\\SteamLibrary", "Oyunlar\\Steam"]:
            games.append(f"{d}:\\{r}\\steamapps\\common\\Torchlight Infinite")
        for r in ["Torchlight Infinite", "Games\\Torchlight Infinite", "XDGames\\Torchlight Infinite",
                  "Program Files\\Torchlight Infinite", "Program Files (x86)\\Torchlight Infinite"]:
            games.append(f"{d}:\\{r}")
    out = [os.path.join(g, rel) for g in games]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        out.append(os.path.join(la, "TorchLight", "Saved", "Logs", "UE_game.log"))
    found = []
    for p in out:
        if os.path.isfile(p) and p not in found:
            found.append(p)
    if not found:
        # standart yolda yoksa oyun klasörünün içinde ara (en fazla 6 seviye)
        for g in games:
            if not os.path.isdir(g):
                continue
            base = g.rstrip("\\/").count(os.sep)
            for dirpath, dirnames, files in os.walk(g):
                if "UE_game.log" in files:
                    found.append(os.path.join(dirpath, "UE_game.log"))
                if dirpath.count(os.sep) - base >= 6:
                    dirnames[:] = []
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)   # en son yazılan önce
    return found


# --------------------------------------------------------------------------- #
# Log ayrıştırıcı
# --------------------------------------------------------------------------- #
RX_TS = re.compile(r"^\[(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d):(\d{3})\]")
RX_PROTO = re.compile(r"ItemChange@ ProtoName=(\w+) (start|end)")
RX_CHANGE = re.compile(r"ItemChange@ (Add|Update) Id=(\d+)_\S+ BagNum=(\d+) in PageId=(-?\d+) SlotId=(\d+)")
RX_DELETE = re.compile(r"ItemChange@ Delete Id=(\d+)_\S+ in PageId=(-?\d+) SlotId=(\d+)")
RX_BAG = re.compile(r"BagMgr@:(?:InitBagData|Modfy BagItem) PageId = (-?\d+) SlotId = (\d+) ConfigBaseId = (\d+) Num = (\d+)")
RX_XSEND = re.compile(r"SendMessage STT----XchgSearchPrice----SynId = (\d+)")
RX_XRECV = re.compile(r"RecvMessage STT----XchgSearchPrice----SynId = (\d+)")
RX_REFER = re.compile(r"\+refer \[([^\]]*)\]")
RX_KEY = re.compile(r"\+key \[(\d+)\]")
RX_PRICE = re.compile(r"\+\d+ \[([0-9.eE+-]+)\]\s*$")
RX_CUR = re.compile(r"\+currency \[(\d+)\]")
RX_FUNC = re.compile(r"CheckFuncDownloaded FuncId = (\d+)")
BLESSINGS_FUNC = "2003"     # oyunun "Blessings from Gods" özelliğinin numarası
# Memory Scrap, Memory Thread Magic/Rare/Epic, Void Sea Invitation Craving/Ecstasy/Sorrow (Void Sea FE planı)
PICK_IDS = ("5140", "5142", "5143", "5144", "410201", "410202", "410203")
# Envanter: "Sort Order" sayfayı "Reset PageId=102" + bütün eşyaları yazar, ardından 4 sayfanın tam dökümü
# (InitBagData) gelir; oyuna girişte de aynı döküm var. Sayfalar oyundaki sekmeler:
RX_RESET = re.compile(r"ItemChange@ Reset PageId=(-?\d+)")
# Exchange'de satılamayan eşyalar: fiyatı yok, "fiyat eksik" sayılmaz (Flame Residue: Trader'da Flame Sand'e
# çevrilir; Memory Thread - Normal: Normal hafızaları bozunca çıkar)
NOT_TRADEABLE = {"100400", "5141"}
INV_PAGES = {"102": "Commodity", "103": "Others", "101": "Skill"}   # Gear (100) yok: rastgele özellikli, fiyatlanamıyor


def ts_of(line):
    m = RX_TS.match(line)
    if not m:
        return None
    y, mo, d, h, mi, s, ms = map(int, m.groups())
    # log saatleri UTC
    try:
        return datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


class Parser:
    """Satır satır besle; olayları self.emit(tip, veri) ile yayar."""

    def __init__(self, emit):
        self.emit = emit
        self.slots = {}          # (page, slot) -> [cid, num]
        self.proto = None
        self.proto_changes = []
        self.last_push = None    # (ts, [(cid, delta)])
        self.block = None        # ("send"/"recv", syn, lines)
        self.pending_search = {}  # syn -> cid
        self.last_ts = None
        self.last_func = None    # son açılan oyun özelliğinin numarası (CheckFuncDownloaded FuncId)
        self.in_blessings = False
        self.bag_burst = None    # InitBagData dökümü sürüyorsa: dökümde görülen sayfalar

    def reset(self):
        self.__init__(self.emit)

    def _emit_inv(self):
        """Envanterin 4 sayfası: {sayfa: {eşya: toplam adet}} (aynı eşyanın yığınları toplanır)."""
        pages = {p: {} for p in INV_PAGES}
        for (page, _slot), (cid, num) in self.slots.items():
            if page in pages and num > 0:
                pages[page][cid] = pages[page].get(cid, 0) + num
        self.emit("inv", {"ts": self.last_ts or time.time(), "pages": pages})

    def _clear_page(self, page):
        for key in [k for k in self.slots if k[0] == page]:
            del self.slots[key]

    def _change(self, page, slot, cid, num, op="Update"):
        """Değişim miktarı; bilinemiyorsa None (daha önce görülmemiş bir yığın güncellendiyse)."""
        key = (page, slot)
        old = self.slots.get(key)
        if op == "Add":
            delta = num
        elif old and old[0] == cid:
            delta = num - old[1]
        elif cid == DS and getattr(self, "ds_bal", None) is not None:
            delta = num - self.ds_bal
        else:
            delta = None
        self.slots[key] = [cid, num]
        if cid == DS:
            self.ds_bal = num
            self.emit("ds_balance", num)
        return delta

    def feed(self, line):
        line = line.rstrip("\r\n")
        if self.bag_burst is not None and "InitBagData" not in line:    # envanter dökümü bitti
            self.bag_burst = None
            self._emit_inv()
        t = ts_of(line)
        if t:
            self.last_ts = t

        # --- Exchange arama blokları (çok satırlı) ---
        if self.block is not None:
            if "----Socket SendMessage End----" in line or "----Socket RecvMessage End----" in line:
                kind, syn, lines = self.block
                self.block = None
                self._finish_block(kind, syn, lines)
            else:
                self.block[2].append(line)
            return
        m = RX_XSEND.search(line)
        if m:
            self.block = ("send", m.group(1), [])
            return
        m = RX_XRECV.search(line)
        if m:
            self.block = ("recv", m.group(1), [])
            return

        # --- Blessings ekranı açıldı ---
        # Blessings ile diğer dükkânlar (Sandlord'daki Resource Trade House vb.) aynı GroceryStoreV3 sayfasını
        # kullanıyor; açılmadan hemen önce yazılan özellik numarası ayırt ediyor: Blessings = 2003.
        m = RX_FUNC.search(line)
        if m:
            self.last_func = m.group(1)
        if "GroceryStoreV3 Run" in line:
            self.in_blessings = self.last_func in (None, BLESSINGS_FUNC)
            if self.in_blessings:
                self.emit("shop", {"ts": self.last_ts or time.time(), "why": "open"})
            return
        # --- Blessings ekranı kapandı (iki log biçimi: "BackFlow0! … 20923_GroceryStoreV3Ctrl" ve "Page@ CloseView 18110_GroceryStoreV3Ctrl") ---
        if "_GroceryStoreV3Ctrl" in line and ("BackFlow0!" in line or "CloseView " in line):
            if self.in_blessings:
                self.emit("shop_close", {"ts": self.last_ts or time.time()})
            return

        # --- Envanter değişimleri ---
        if "ItemChange@" in line or "BagMgr@" in line:
            m = RX_PROTO.search(line)
            if m:
                name, se = m.groups()
                if se == "start":
                    self.proto, self.proto_changes = name, []
                else:
                    self._finish_proto(name, self.proto_changes)
                    self.proto, self.proto_changes = None, []
                    self._emit_inv()
                return
            m = RX_RESET.search(line)
            if m:                       # "Sort Order": sayfa baştan yazılacak (eşyalar yer değiştiriyor)
                self._clear_page(m.group(1))
                return
            m = RX_CHANGE.search(line)
            if m:
                op, cid, num, page, slot = m.groups()
                d = self._change(page, slot, cid, int(num), op)
                if self.proto and d is not None:
                    self.proto_changes.append((cid, d))
                elif self.proto:
                    self.proto_changes.append((cid, None))
                return
            m = RX_DELETE.search(line)
            if m:
                cid, page, slot = m.groups()
                old = self.slots.pop((page, slot), None)
                if self.proto and old:
                    self.proto_changes.append((old[0], -old[1]))
                return
            m = RX_BAG.search(line)
            if m:
                page, slot, cid, num = m.groups()
                if "InitBagData" in line:           # tam döküm: o sayfanın eski hâli silinir
                    if self.bag_burst is None:
                        self.bag_burst = set()
                    if page not in self.bag_burst:
                        self.bag_burst.add(page)
                        self._clear_page(page)
                key = (page, slot)
                self.slots[key] = [cid, int(num)]
                if cid == DS:
                    self.ds_bal = int(num)
                    self.emit("ds_balance", int(num))
                return

    def _finish_proto(self, name, changes):
        ts = self.last_ts or time.time()
        if name == "Push2":
            gains = [(c, d) for c, d in changes if d is None or d > 0]
            if gains:
                self.last_push = (ts, gains)
        elif name == "BuyVendorGoods":
            spent = -sum(d for c, d in changes if c == DS and d is not None and d < 0)
            if spent <= 0:
                return
            gains = [(c, d) for c, d in changes if c != DS and (d is None or d > 0)]
            if not gains and self.last_push and abs(ts - self.last_push[0]) < 3:
                gains = self.last_push[1]
            self.last_push = None
            if gains:
                cid, qty = gains[0]
                self.emit("buy", {"ts": ts, "cid": cid, "qty": qty, "ds": spent})
            else:
                self.emit("buy", {"ts": ts, "cid": None, "qty": 0, "ds": spent})
        elif name == "RefreshVendorShop":
            if not self.in_blessings:          # başka bir dükkânın yenilemesi
                return
            spent = -sum(d for c, d in changes if c == DS and d is not None and d < 0)
            self.emit("refresh", {"ts": ts, "ds": spent})
            self.emit("shop", {"ts": ts, "why": "refresh"})
        elif name == "PickItems":
            # Void Sea FE planı: yerden alınan Memory Scrap / Thread (yığın sayısının artışı)
            for cid, d in changes:
                if cid in PICK_IDS and d and d > 0:
                    self.emit("pick", {"ts": ts, "cid": cid, "n": d})

    def _finish_block(self, kind, syn, lines):
        if kind == "send":
            text = "\n".join(lines)
            refers = RX_REFER.findall(text)
            keys = RX_KEY.findall(text)
            # Basit eşya araması (filtresiz)
            if len(refers) == 1 and "params" not in text and refers[0].isdigit():
                self.pending_search[syn] = (refers[0], False)
            # Skill sayfasındaki eşyalar (Price Check): eşya (key 4) + oyunun koyduğu seviye (key 1) / affix (key 5)
            # filtresi. Fiyat o seviyedeki / o affixli eşyanın fiyatı: oyuncunun kendi eşyası için doğru ama
            # başkalarınınki için değil, topluluğa gönderilmez ("filtered").
            elif (keys and keys[0] == "4" and refers and refers[0].isdigit()
                  and all(k in ("1", "5") for k in keys[1:])):
                self.pending_search[syn] = (refers[0], True)
            return
        cid, filtered = self.pending_search.pop(syn, (None, False))
        if not cid:
            return
        # İki biçim: eskisinde önce fiyatlar sonra "+currency [100300]"; Tartarus sezonunda (2026-11-02) önce
        # "+prices+1+currency [100300]" sonra fiyatlar. Fiyatlardan sonra gelen para birimi bloğun sonudur.
        prices, started, cur = [], False, None
        for l in lines:
            mc = RX_CUR.search(l)
            if mc:
                if prices and cur is not None:      # yeni biçim: sıradaki para biriminin fiyatları başlıyor
                    break
                cur = mc.group(1)
                if prices:                          # eski biçim: fiyatlar bitti
                    break
                continue
            if "unitPrices" in l:
                started = True
            if started:
                mp = RX_PRICE.search(l)
                if mp:
                    try:
                        prices.append(float(mp.group(1)))
                    except ValueError:
                        pass
        if prices and cur == FE:
            self.emit("price", {"cid": cid, "prices": sorted(prices), "ts": self.last_ts or time.time(),
                                "filtered": filtered})


# --------------------------------------------------------------------------- #
# Hesap motoru
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, data):
        self.data = data
        self.purchases = []
        self.refreshes = []
        self.ds_balance = None
        self.community = {}      # topluluk fiyatları {eşya: {"p", "n", "t"}}: sadece kendi fiyatı olmayanlarda

    @property
    def s(self):
        return self.data["settings"]

    def is_excluded(self, cid):
        if str(cid) in self.s.get("exclude_ids", []):
            return True
        n = item_name(cid).lower()
        return any(p and p in n for p in self.s.get("exclude_name_parts", []))

    def ref_price(self, cid):
        """FE cinsinden birim fiyat (en ucuz N ilanın ortalaması)."""
        if cid == FE:
            return 1.0
        p = self.data["prices"].get(str(cid))
        if not p:
            c = self.community.get(str(cid))      # kendi aramadığın eşya: topluluk fiyatı (açıksa)
            return c["p"] if c else None
        lst = p["prices"]
        n = {"cheap1": 1, "cheap3": 3, "cheap5": 5}.get(self.s.get("price_mode"), 3)
        lst = lst[:n] if len(lst) >= n else lst
        return sum(lst) / len(lst)

    def ds_net(self):
        """1 DS satılırsa eline geçen FE (vergi sonrası)."""
        p = self.ref_price(DS)
        if p is None:
            return None
        return p * (1 - self.s["tax"])

    def value_fe(self, cid, qty):
        """Teklifteki eşyanın vergi sonrası FE değeri."""
        if qty is None or cid is None:
            return None
        if cid == FE:
            return float(qty)
        p = self.ref_price(cid)
        if p is None:
            return None
        return qty * p * (1 - self.s["tax"])

    def max_ds(self, cid, qty):
        v, d = self.value_fe(cid, qty), self.ds_net()
        if v is None or not d:
            return None
        return v / d / (1 + self.s.get("min_profit_pct", 0) / 100.0)

    def judge(self, cid, qty, ds_cost):
        """(karar, kâr_FE) ; karar: True=AL, False=ALMA, None=bilinmiyor"""
        v, d = self.value_fe(cid, qty), self.ds_net()
        if v is None or not d:
            return None, None
        cost_fe = ds_cost * d
        profit = v - cost_fe
        need = cost_fe * self.s.get("min_profit_pct", 0) / 100.0
        return profit >= need, profit

    def on_event(self, kind, val):
        if kind == "price":
            self.data["prices"][val["cid"]] = {"prices": val["prices"][:20], "ts": val["ts"]}
        elif kind == "ds_balance":
            self.ds_balance = val
        elif kind == "buy":
            self.purchases.append(val)
            if val["cid"] and isinstance(val["qty"], int):
                entry = [val["cid"], val["qty"]]
                if entry not in self.data["catalog"]:
                    self.data["catalog"].append(entry)
        elif kind == "refresh":
            self.refreshes.append(val)
        elif kind == "shop":
            pass

    def catalog_rows(self):
        rows = []
        for cid, qty in self.data["catalog"]:
            if self.is_excluded(cid) or cid == DS:
                continue
            rows.append((cid, qty, self.ref_price(cid), self.max_ds(cid, qty)))
        rows.sort(key=lambda r: (r[0] != FE, item_name(r[0]), r[1] if isinstance(r[1], int) else 0))
        return rows

    def summary(self):
        ds_buy = sum(p["ds"] for p in self.purchases)
        ds_ref = sum(r["ds"] for r in self.refreshes)
        val = 0.0
        unknown = 0
        for p in self.purchases:
            v = self.value_fe(p["cid"], p["qty"]) if p["cid"] else None
            if v is None:
                unknown += 1
            else:
                val += v
        d = self.ds_net()
        cost = (ds_buy + ds_ref) * d if d else None
        return {"ds_buy": ds_buy, "ds_refresh": ds_ref, "n_buy": len(self.purchases),
                "n_refresh": len(self.refreshes), "value": val, "cost": cost,
                "net": (val - cost) if cost is not None else None, "unknown": unknown}


# --------------------------------------------------------------------------- #
# Log takipçisi (arka plan iş parçacığı)
# --------------------------------------------------------------------------- #
# "Enable Log" kapalıyken oyun yine log yazıyor ama sadece temel satırları: bölge geçişi, eşya
# hareketi, terminal fısıltıları yok. Açıkken ilk bölge satırı oyun açıldıktan ~20 sn sonra geliyor.
# Sayfa açılışları iki biçimde yazılıyor ve hiç karışmıyor: açıkken "PageBase@ OpenFlow5! OpenView X",
# kapalıyken "] Page@ OpenView X". Oyun açılınca ilk dakikada birkaç sayfa açıldığı için durum hemen belli olur;
# oyun içinde ayar değişirse en son görülen biçim geçerli.
LOG_DETAIL_MARKERS = (b"EnterLevel(", b"ItemChange@", b"PageBase@")
LOG_NODETAIL_MARKER = b"] Page@ OpenView "
LOG_DETAIL_WAIT = 120          # hiç sayfa satırı yoksa: log'un ilk satırından bu kadar sn sonra hâlâ ayrıntı yoksa kapalı
UPDATE_CHECK_MS = 30 * 60 * 1000   # yeni sürüm kontrolü: açılıştan 3 sn sonra, sonra 30 dk'da bir
AUTO_COLLAPSE_MS = 3000       # Blessings / Void Sea ekranı kapandıktan sonra panel bu kadar bekleyip küçülür
LOG_TS_RX = re.compile(rb"^\[(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d)")


def log_ts(raw):
    """Log satırının zaman damgası (sn); damgasız satırda None."""
    m = LOG_TS_RX.match(raw)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).timestamp()
    except ValueError:
        return None


class Tailer(threading.Thread):
    def __init__(self, path, out_q):
        super().__init__(daemon=True)
        self.path, self.q = path, out_q
        self.stop_flag = False

    def run(self):
        parser = Parser(lambda k, v: self.q.put((k, v)))
        pos, buf, size_prev = 0, b"", 0
        nlines = 0
        first = True
        t_first, detail = None, None
        while not self.stop_flag:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                self.q.put(("logerr", "Log file not found"))
                time.sleep(2)
                continue
            if size < size_prev or size < pos:      # oyun yeniden açıldı -> yeni log
                pos, buf = 0, b""
                parser.reset()
                nlines = 0
                t_first, detail = None, None
                self.q.put(("newlog", None))
            size_prev = size
            if size > pos:
                try:
                    with open(self.path, "rb") as f:
                        f.seek(pos)
                        chunk = f.read(min(size - pos, 8 * 1024 * 1024))
                        pos += len(chunk)
                except OSError as e:
                    self.q.put(("logerr", f"Could not open log: {e}"))
                    time.sleep(2)
                    continue
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop()
                for raw in lines:
                    if any(k in raw for k in LOG_DETAIL_MARKERS):
                        detail = True
                    elif LOG_NODETAIL_MARKER in raw:
                        detail = False
                    try:
                        parser.feed(raw.decode("utf-8", "replace").lstrip("﻿"))
                    except Exception:
                        pass
                if t_first is None:            # log'un başında yüzlerce damgasız satır olabiliyor
                    t_first = next((t for t in map(log_ts, lines) if t), None)
                t_last = next((t for t in map(log_ts, reversed(lines[-200:])) if t), None)
                nlines += len(lines)
                # None: henüz bilinmiyor (oyun yeni açıldı), False: ayrıntı satırı yok
                has = detail
                if has is None and t_first and t_last and t_last - t_first > LOG_DETAIL_WAIT:
                    has = False
                self.q.put(("logstat", {"lines": nlines, "size": size, "detail": has}))
                if pos < size:
                    continue  # ilk taramada hızlı devam et
            if first:
                self.q.put(("status", "Canlı takip"))
                first = False
            time.sleep(0.5)


# --------------------------------------------------------------------------- #
# Arayüz
# --------------------------------------------------------------------------- #
def fmt(x, nd=1):
    if x is None:
        return "—"
    return f"{x:,.{nd}f}"


STARTUP_NAME = "TLI_Blessing_Helper.vbs"
INSTANCE_PORT = 47321


def _startup_path():
    ad = os.environ.get("APPDATA")
    if not ad:
        return None
    return os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", STARTUP_NAME)


def set_autostart(enabled):
    """Başlangıç klasörüne, uygulamayı penceresiz ve gizli (--tray) açan bir .vbs koyar/siler."""
    path = _startup_path()
    if not path or sys.platform != "win32":
        return False
    try:
        if not enabled:
            if os.path.isfile(path):
                os.remove(path)
            return True
        exe = sys.executable
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(cand):
            exe = cand
        script = os.path.abspath(__file__)
        vbs = ('Set sh = CreateObject("WScript.Shell")\r\n'
               f'sh.CurrentDirectory = "{APP_DIR}"\r\n'
               f'sh.Run """{exe}"" ""{script}"" --tray", 0, False\r\n')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-16", newline="") as f:     # WSH Unicode okur (Türkçe yollar için)
            f.write(vbs)
        return True
    except Exception:
        return False


def claim_single_instance(on_open):
    """İlk kopya ise True döner ve 'aç' isteklerini dinler; değilse çalışan kopyaya 'aç' der, False döner."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen(2)
    except OSError:
        try:
            c = socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2)
            c.sendall(b"open")
            c.close()
        except OSError:
            pass
        return False

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                conn.recv(16)
                conn.close()
                on_open()
            except OSError:
                break

    threading.Thread(target=loop, daemon=True).start()
    return True


def run_gui(start_hidden=False):
    import tkinter as tk
    from tkinter import ttk, filedialog
    import tkinter.font as tkfont

    if sys.platform == "win32":      # ekran yakalamada gerçek pikselleri al
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        try:   # bildirimlerde "Python" değil uygulamanın adı (pencereler açılmadan önce)
            import tray as TR
            TR.set_app_id()
            TR.register_app_id()
        except Exception:
            pass
    q = queue.Queue()
    if not claim_single_instance(lambda: q.put(("tray", "open"))):
        return                                   # zaten açık: onun penceresini öne getirdik
    data = load_data()
    i18n.set_lang(data["settings"].get("ui_lang", "en"))
    eng = Engine(data)
    set_autostart(bool(data["settings"].get("autostart", False)))
    st = {"tailer": None, "dirty": True, "status": "Starting…", "last_save": 0}

    root = tk.Tk()
    root.title(f"TLI Blessing Helper v{APP_VERSION}")
    root.geometry("760x940")
    root.minsize(520, 500)
    root.attributes("-topmost", False)

    # Renk sistemi (v6.1): mavi/cyan = sistem ve nötr veri, altın = ekonomik değer (DS/FE),
    # yeşil/sarı/kırmızı = karar. Beyaz çerçeve yok: ince, koyu mavi-gri kenarlar.
    BG, SURF, SURF2, BORDER = "#0D1117", "#111827", "#1A2233", "#2A3447"
    FG, MUTED = "#E5E7EB", "#94A3B8"
    PRI, ACC = "#4CC9F0", "#F4B942"
    GOOD, WARN, BAD = "#22C55E", "#F59E0B", "#EF4444"
    ROW_A, ROW_B, ROW_HOVER = "#0F172A", "#111C2F", "#1C2A42"
    F_UI, F_NUM = "Segoe UI", "Bahnschrift"          # Bahnschrift: rakamlar için dar, okunaklı (Windows'ta hazır)
    TAB_HOVER, TAB_HOVER_FG = "#161F2E", "#CBD5E1"     # seçili olmayan sekmenin üzerine gelince
    # tek renk çizgi simgeler (kamera, böcek, menü simgeleri): Windows 11'de Fluent, 10'da MDL2; yoksa simgesiz
    ICON_FONT = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in tkfont.families(root)), None)

    def hover_tab(widgets, is_active, off_bg, hover_bg, off_fg=None, hover_fg=None):
        """Tarayıcı sekmesi gibi: seçili olmayan sekmenin üzerine gelince zemin (ve yazı) biraz aydınlanır."""
        def paint(on):
            if is_active():
                return
            for w in widgets:
                try:
                    w.config(bg=hover_bg if on else off_bg)
                    if off_fg and isinstance(w, tk.Label):
                        w.config(fg=hover_fg if on else off_fg)
                except tk.TclError:
                    pass
        for w in widgets:
            w.bind("<Enter>", lambda e: paint(True), add="+")
            w.bind("<Leave>", lambda e: paint(False), add="+")

    def icon_button(parent, glyph, text, cmd, bg, fg, size=9):
        """Simge + yazı tek düğme (ikisine de tıklanır)."""
        fr = tk.Frame(parent, bg=bg, cursor="hand2")
        parts = []
        if glyph and ICON_FONT:
            parts.append(tk.Label(fr, text=glyph, bg=bg, fg=fg, font=(ICON_FONT, size), cursor="hand2"))
        parts.append(T(tk.Label(fr, bg=bg, fg=fg, font=("Segoe UI", size, "bold"), cursor="hand2"), text))
        for i, w in enumerate(parts):
            w.pack(side="left", padx=(6 if i == 0 else 3, 6 if i == len(parts) - 1 else 0), pady=2)
        for w in [fr] + parts:
            w.bind("<Button-1>", lambda e: cmd())
        return fr
    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=BG, foreground=FG, fieldbackground=SURF2, font=(F_UI, 10),
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG, focuscolor=BORDER)
    style.configure("Treeview", background=ROW_A, foreground=FG, fieldbackground=ROW_A, rowheight=26,
                    borderwidth=0, bordercolor=BORDER, lightcolor=ROW_A, darkcolor=ROW_A)
    # Başlıklar tıklanmıyor: üzerine gelince renk değişmesin (clam'de beyaza dönüyordu)
    style.configure("Treeview.Heading", background=SURF2, foreground=MUTED, font=(F_UI, 9, "bold"),
                    relief="flat", borderwidth=1, bordercolor=BORDER, lightcolor=SURF2, darkcolor=SURF2, padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", SURF2), ("pressed", SURF2)],
              foreground=[("active", MUTED)], relief=[("active", "flat"), ("pressed", "flat")])
    style.map("Treeview", background=[("selected", "#245A7A")], foreground=[("selected", "#FFFFFF")])
    # Butonlar: birincil (ana aksiyon, cyan) ve ikincil (sakin)
    style.configure("TButton", background=SURF2, foreground=FG, padding=(12, 5), relief="flat",
                    bordercolor=BORDER, lightcolor=SURF2, darkcolor=SURF2, focuscolor=SURF2)
    style.map("TButton", background=[("pressed", "#26324A"), ("active", "#222C40")],
              lightcolor=[("active", "#222C40")], darkcolor=[("active", "#222C40")])
    style.configure("Primary.TButton", background="#10303F", foreground=PRI, bordercolor="#24607A",
                    lightcolor="#10303F", darkcolor="#10303F", focuscolor="#10303F", font=(F_UI, 10, "bold"))
    style.map("Primary.TButton", background=[("pressed", "#1A4A60"), ("active", "#153E52")],
              lightcolor=[("active", "#153E52")], darkcolor=[("active", "#153E52")])
    # Onay kutuları: üzerine gelince beyaza dönmesin
    style.configure("TCheckbutton", background=BG, foreground=FG, focuscolor=BG, indicatorbackground=SURF2,
                    indicatorforeground=PRI, upperbordercolor=BORDER, lowerbordercolor=BORDER, indicatormargin=(0, 0, 6, 0))
    style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)],
              indicatorbackground=[("pressed", SURF2), ("active", SURF2)])
    style.configure("TLabelframe", background=BG, foreground=MUTED)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=(F_UI, 9, "bold"))
    style.configure("Dark.Vertical.TScrollbar", background=SURF2, troughcolor=ROW_A, bordercolor=ROW_A,
                    lightcolor=SURF2, darkcolor=SURF2, arrowcolor=MUTED, arrowsize=11, gripcount=0)
    style.map("Dark.Vertical.TScrollbar", background=[("active", BORDER)])
    style.configure("Dark.TCombobox", fieldbackground=SURF2, background=SURF2, foreground=FG, arrowcolor=MUTED,
                    bordercolor=BORDER, lightcolor=SURF2, darkcolor=SURF2, padding=(6, 2))
    style.map("Dark.TCombobox", fieldbackground=[("readonly", SURF2)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", SURF2)], selectforeground=[("readonly", FG)],
              background=[("active", "#222C40")], arrowcolor=[("active", FG)])
    root.option_add("*TCombobox*Listbox.background", SURF2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", "#245A7A")
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

    def entry(parent, width):
        return tk.Entry(parent, width=width, bg=SURF2, fg=FG, insertbackground=FG, relief="flat",
                        highlightthickness=1, highlightbackground=BORDER, highlightcolor=PRI, font=(F_NUM, 10))

    static_txt = []                    # (widget, seçenek, İngilizce metin): dil değişince yeniden çevrilir

    def T(w, en, opt="text"):
        w.configure(**{opt: tr(en)})
        static_txt.append((w, opt, en))
        return w

    def H(tree, col, en, **kw):
        tree.heading(col, text=tr(en), **kw)
        static_txt.append((tree, ("heading", col), en))

    def trv(values):
        return tuple(tr(v) if isinstance(v, str) else v for v in values)

    def panel(parent, title, sub="", fill="x", expand=False):
        """Başlık barı + kısa açıklama + içerik alanı olan kart. -> (dış çerçeve, içerik)"""
        outer = tk.Frame(parent, bg=SURF, highlightthickness=1, highlightbackground=BORDER)
        outer.pack(fill=fill, expand=expand, padx=0, pady=(0, 10))
        head = tk.Frame(outer, bg=SURF)
        head.pack(fill="x", padx=12, pady=(9, 0))
        T(tk.Label(head, text=tr(title), bg=SURF, fg=FG, font=(F_UI, 10, "bold"), anchor="w"), title).pack(side="left")
        if sub:
            T(tk.Label(outer, text=tr(sub), bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w", justify="left",
                       wraplength=680), sub).pack(fill="x", padx=12)
        body = tk.Frame(outer, bg=SURF)
        body.pack(fill="both", expand=True, padx=12, pady=(6, 10))
        return outer, body

    def zebra(tag, i):
        return (tag, "odd" if i % 2 else "even")

    def tree_colors(tree):
        tree.tag_configure("even", background=ROW_A)
        tree.tag_configure("odd", background=ROW_B)

    # --- Üst alan: solda uygulama adı + log durumu, sağda aksiyon ---
    logbox = tk.Frame(root, bg=BG)
    logbox.pack(fill="x", padx=16, pady=(12, 4))
    logrow = tk.Frame(logbox, bg=BG)
    logrow.pack(fill="x")
    btn_log = T(ttk.Button(logrow, style="Primary.TButton", command=lambda: pick_log()), "📂  Select log")
    btn_log.pack(side="right", anchor="n")
    head_l = tk.Frame(logrow, bg=BG)
    head_l.pack(side="left", fill="x", expand=True)
    tk.Label(head_l, text=tr("TLI Blessing Helper"), bg=BG, fg=FG, font=(F_NUM + " SemiBold", 15), anchor="w").pack(side="top", anchor="w")
    lbl_log = tk.Label(head_l, text="", bg=BG, fg=MUTED, font=(F_UI, 9, "bold"), anchor="w", justify="left")
    lbl_log.pack(fill="x")
    # dosya yolu varsayılan olarak yıldızlı: ekran görüntüsü paylaşılınca klasör/kullanıcı adı görünmesin
    path_row = tk.Frame(head_l, bg=BG)
    path_row.pack(fill="x")
    eye_mdl = "Segoe MDL2 Assets" in tkfont.families()
    btn_eye = tk.Label(path_row, text="", bg=BG, fg=MUTED, cursor="hand2", padx=0,
                       font=("Segoe MDL2 Assets", 9) if eye_mdl else (F_UI, 9))
    btn_eye.pack(side="left", anchor="n", padx=(0, 6))
    lbl_path = tk.Label(path_row, text="", bg=BG, fg=MUTED, font=(F_UI, 8), anchor="w", justify="left", wraplength=500)
    lbl_path.pack(side="left", fill="x", expand=True)
    path_txt = {"v": ""}

    def show_path():
        on = data["settings"].get("show_log_path", False)
        p = path_txt["v"]
        lbl_path.config(text=p if on or not p else "*" * 40)
        if not p:
            btn_eye.config(text="")
        elif eye_mdl:
            btn_eye.config(text="\uE7B3" if on else "\uED1A")     # açık göz / üstü çizili göz
        else:
            btn_eye.config(text="👁" if on else "◌")

    def toggle_path(_e=None):
        data["settings"]["show_log_path"] = not data["settings"].get("show_log_path", False)
        save_data(data)
        show_path()

    btn_eye.bind("<Button-1>", toggle_path)
    btn_eye.bind("<Enter>", lambda e: btn_eye.config(fg=FG))
    btn_eye.bind("<Leave>", lambda e: btn_eye.config(fg=MUTED))
    show_path()
    # log bulunamadı / yazılmıyor: ne yapılacağını anlat (sorun yokken gizli)
    lbl_loghelp = tk.Label(logbox, text="", bg=BG, fg=WARN, font=(F_UI, 9), anchor="w", justify="left", wraplength=600)

    # --- Güncelleme şeridi: GitHub'da daha yeni bir sürüm varsa (yoksa gizli) ---
    upd_bar = tk.Frame(root, bg="#10303F", highlightthickness=1, highlightbackground="#24607A")
    lbl_upd = tk.Label(upd_bar, text="", bg="#10303F", fg=FG, font=(F_UI, 9, "bold"), anchor="w")
    lbl_upd.pack(side="left", padx=(12, 8), pady=7)
    upd_x = tk.Label(upd_bar, text="✕", bg="#10303F", fg=MUTED, font=(F_UI, 10, "bold"), cursor="hand2", padx=10)
    upd_x.pack(side="right")
    upd_web = T(ttk.Button(upd_bar, command=lambda: open_update()), "Release page")
    upd_web.pack(side="right", pady=4, padx=(6, 0))
    upd_go = T(ttk.Button(upd_bar, style="Primary.TButton", command=lambda: do_update()), "⬇  Install update")
    upd_go.pack(side="right", pady=4)

    # --- Sekmeler: DS Calculator | Void Sea Terminal (aktif sekmenin altında cyan çizgi) ---
    tabbar = tk.Frame(root, bg=BG)
    tk.Frame(tabbar, bg=BORDER, height=1).pack(side="bottom", fill="x")
    nb = tk.Frame(root, bg=BG)
    tab_ds = tk.Frame(nb, bg=BG)
    tab_vs = tk.Frame(nb, bg=BG)
    tab_inv = tk.Frame(nb, bg=BG)

    # Sekme işaretleri: DS Calculator'da uygulamanın simgesi, Inventory value'da tlidb'deki Flame Elementium simgesi.
    # İkisi de sekme yazısının rengine boyanır (seçili değilken gri, seçiliyken açık) ki temaya uysun.
    TAB_ICON_PX = 16
    TAB_FE_PATH = os.path.join(ICON_DIR, "tab_flame_elementium.png")
    tab_imgs = {}
    main_tabs = {}

    def set_tab_icon(key, pil):
        try:
            from PIL import ImageTk
            tab_imgs[key] = {c: ImageTk.PhotoImage(tint_icon(pil, c, TAB_ICON_PX), master=root) for c in (MUTED, FG)}
        except Exception:
            return
        if key in main_tabs:
            main_tabs[key][1].config(image=tab_imgs[key][FG if st.get("main_tab") == key else MUTED], compound="left")

    def load_tab_icons():
        try:
            import tray as TR
            img = TR.app_icon(data["settings"].get("log_path", ""))
            if img is not None:
                set_tab_icon("ds", img)
        except Exception:
            pass
        if os.path.isfile(TAB_FE_PATH):
            try:
                from PIL import Image
                set_tab_icon("inv", Image.open(TAB_FE_PATH))
                return
            except Exception:
                pass

        def get():          # ilk açılışta bir kez indirilir (büyük boy: küçültünce net dursun)
            if fetch_icon("tab_fe", "Flame Elementium", px=64, out=TAB_FE_PATH):
                q.put(("tab_icon", "inv"))
        threading.Thread(target=get, daemon=True).start()

    def show_main_tab(key):
        for k, (fr, lb, line_) in main_tabs.items():
            on = k == key
            lb.config(fg=FG if on else MUTED, bg=SURF if on else BG)
            if k in tab_imgs:
                lb.config(image=tab_imgs[k][FG if on else MUTED])
            line_.config(bg=PRI if on else BG)
            fr.pack_forget()
        main_tabs[key][0].pack(fill="both", expand=True)
        st["main_tab"] = key
        if key == "inv":
            render_inv()

    for key, txt, fr in (("ds", " DS Calculator", tab_ds), ("vs", "◎  Void Sea Terminal", tab_vs),
                         ("inv", " Inventory value", tab_inv)):
        cell = tk.Frame(tabbar, bg=BG)
        cell.pack(side="left", padx=(0, 2))
        lb = T(tk.Label(cell, bg=BG, fg=MUTED, font=(F_UI, 10, "bold"), padx=16, pady=7, cursor="hand2",
                        compound="left"), txt)
        lb.pack()
        line_ = tk.Frame(cell, bg=BG, height=2)
        line_.pack(fill="x")
        lb.bind("<Button-1>", lambda e, k=key: show_main_tab(k))
        main_tabs[key] = (fr, lb, line_)
        hover_tab([cell, lb], lambda k=key: st.get("main_tab") == k, BG, TAB_HOVER, MUTED, TAB_HOVER_FG)
    load_tab_icons()

    # --- Void Sea Terminal sekmesi: plan seçimi. Overlay'in Void Sea önerisi de seçili plana göre ---
    plan_bar = tk.Frame(tab_vs, bg=BG)
    plan_bar.pack(fill="x", pady=(12, 0))
    T(tk.Label(plan_bar, bg=BG, fg=MUTED, font=(F_UI, 9)), "Plan").pack(side="left", padx=(0, 8))
    plan_lbls = {}
    for key, txt in (("red", "Red memories · Armament + Wonder"), ("fe", "Memory materials · Wealth or Boss + Wonder")):
        lb = T(tk.Label(plan_bar, bg=SURF, fg=MUTED, font=(F_UI + " Semibold", 9), padx=12, pady=4, cursor="hand2",
                        highlightthickness=1, highlightbackground=BORDER), txt)
        lb.pack(side="left", padx=(0, 6))
        lb.bind("<Button-1>", lambda e, k=key: show_plan(k))
        plan_lbls[key] = lb
        hover_tab([lb], lambda k=key: data["settings"].get("vs_plan", "red") == k, SURF, "#1A2A40", MUTED,
                  TAB_HOVER_FG)
    T(tk.Label(plan_bar, bg=BG, fg=MUTED, font=(F_UI, 8)), "The overlay advises for this plan.").pack(side="left", padx=6)

    # --- Kırmızı planı sayfası: her koşuda seçilen fısıltılar ve kazanılan memory'ler ---
    vs_top = tk.Frame(tab_vs, bg=BG)
    vs_top.pack(fill="x", pady=(12, 10))
    vs_kpis = {}
    for i, (key, cap) in enumerate((("runs", "Runs logged"), ("mem", "Hero Memories"), ("red", "Red memories"),
                                    ("last", "Last run"))):
        card = tk.Frame(vs_top, bg=SURF, highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
        vs_top.columnconfigure(i, weight=1, uniform="vkpi")
        c = T(tk.Label(card, bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w"), cap)
        c.pack(fill="x", padx=10, pady=(8, 0))
        v = tk.Label(card, text="—", bg=SURF, fg=BAD if key == "red" else FG, font=(F_NUM + " SemiBold", 17), anchor="w")
        v.pack(fill="x", padx=10)
        s = tk.Label(card, text="", bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w")
        s.pack(fill="x", padx=10, pady=(0, 8))
        vs_kpis[key] = (v, s, c)
    # Koşu başına Hero Memory grafiği (son 20 koşu, en yeni sağda)
    _chart_card, fr_chart = panel(tab_vs, "Hero Memories per run",
                                  "Last 20 runs, newest on the right. The dashed line is your average. "
                                  "Click a bar to open that run.")
    chart = tk.Canvas(fr_chart, bg=SURF, height=104, highlightthickness=0, bd=0, cursor="hand2")
    chart_geo = {}                     # son çizimin sütun yerleşimi: tıklanan sütun -> koşu
    chart.pack(fill="x")
    chart.bind("<Configure>", lambda e: draw_chart())

    # Alt kısım iki görünüm arasında geçer: koşu geçmişi | fısıltı türleri karşılaştırması
    low = tk.Frame(tab_vs, bg=SURF, highlightthickness=1, highlightbackground=BORDER)
    low.pack(fill="both", expand=True, pady=(0, 8))
    seg = tk.Frame(low, bg=SURF)
    seg.pack(fill="x", padx=12, pady=(9, 0))
    lbl_low_sub = tk.Label(low, text="", bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w", justify="left", wraplength=680)
    lbl_low_sub.pack(fill="x", padx=12)
    fr_runs = tk.Frame(low, bg=SURF)
    fr_cmp = tk.Frame(low, bg=SURF)
    low_views = {"runs": (fr_runs, "Run history", "Newest first. Click a run to see its whispers."),
                 "cmp": (fr_cmp, "Whisper types compared",
                         "Whispers are often picked together, so “with / without” averages can mislead; "
                         "the estimated effect separates them.")}
    seg_lbls = {}

    # İkinci seviye sekmeler: üstteki ana sekmelerden küçük ama gerçek sekme gibi (zemin, kenar, cyan alt çizgi)
    TAB_ON, TAB_OFF, TAB_EDGE_ON = "#16304A", ROW_A, "#24607A"
    low_state = {"key": "runs"}

    def show_low(key):
        low_state["key"] = key
        for k, (fr, title, sub) in low_views.items():
            on = k == key
            cell, lb, line_ = seg_lbls[k]
            cell.config(bg=TAB_ON if on else TAB_OFF, highlightbackground=TAB_EDGE_ON if on else BORDER)
            lb.config(bg=TAB_ON if on else TAB_OFF, fg="#FFFFFF" if on else MUTED)
            line_.config(bg=PRI if on else TAB_OFF)
            fr.pack_forget()
            if on:
                lbl_low_sub.config(text=tr(sub))
        low_views[key][0].pack(fill="both", expand=True, padx=12, pady=(6, 10))

    for k, (fr, title, sub) in low_views.items():
        cell = tk.Frame(seg, bg=TAB_OFF, highlightthickness=1, highlightbackground=BORDER, cursor="hand2")
        cell.pack(side="left", padx=(0, 6))
        lb = T(tk.Label(cell, bg=TAB_OFF, fg=MUTED, font=(F_UI + " Semibold", 10), padx=14, pady=5,
                        cursor="hand2"), title)
        lb.pack()
        line_ = tk.Frame(cell, bg=TAB_OFF, height=2)
        line_.pack(fill="x")
        for w in (cell, lb):
            w.bind("<Button-1>", lambda e, k=k: show_low(k))
        # pasif sekmenin üzerine gelince yazı aydınlansın (tıklanabilir olduğu belli olsun)
        lb.bind("<Enter>", lambda e, k=k: low_state["key"] != k and seg_lbls[k][1].config(fg=FG))
        lb.bind("<Leave>", lambda e, k=k: low_state["key"] != k and seg_lbls[k][1].config(fg=MUTED))
        seg_lbls[k] = (cell, lb, line_)
        hover_tab([cell, lb, line_], lambda k=k: low_state["key"] == k, TAB_OFF, "#16233A")

    tvw = ttk.Treeview(fr_cmp, columns=("k", "n", "with", "wo", "model", "conf", "red"), show="headings", height=5,
                       selectmode="none")
    for c, t, w, a in [("k", "Whisper type", 128, "w"), ("n", "Runs with / w/o", 106, "center"),
                       ("with", "Avg with", 62, "e"), ("wo", "Avg w/o", 60, "e"),
                       ("model", "Estimated effect", 128, "e"), ("conf", "Confidence", 90, "center"),
                       ("red", "Red / run", 98, "e")]:
        H(tvw, c, t, anchor=a)
        tvw.column(c, width=w, anchor=a)
    tree_colors(tvw)
    # güven düzeyi satır rengiyle: Good yeşil, Preliminary normal, Low sarı, Insufficient sönük
    for tag, col in (("Good", GOOD), ("Preliminary", FG), ("Low", WARN), ("Insufficient", MUTED), ("red", BAD)):
        tvw.tag_configure(tag, foreground=col)
    tvw.pack(fill="x")
    lbl_cmp = tk.Label(fr_cmp, text="", bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w", justify="left", wraplength=660)
    lbl_cmp.pack(fill="x", pady=(4, 0))
    # tablonun altı: analiz özeti
    cmp_sum = tk.Frame(fr_cmp, bg=SURF2, highlightthickness=1, highlightbackground=BORDER)
    cmp_sum.pack(fill="both", expand=True, pady=(8, 0))

    # Koşu geçmişi: solda kısa özet tablo, sağda seçilen koşunun fısıltıları alt alta
    run_detail = tk.Frame(fr_runs, bg=SURF2, width=232, highlightthickness=1, highlightbackground=BORDER)
    run_detail.pack(side="right", fill="y", padx=(10, 0))
    run_detail.pack_propagate(False)
    # 6 fısıltı + not kartın boyunu aşabiliyor: içerik fare tekerleğiyle kaydırılan bir alanda
    rd_canvas = tk.Canvas(run_detail, bg=SURF2, highlightthickness=0, bd=0, yscrollincrement=20)
    rd_canvas.pack(fill="both", expand=True)
    rd_body = tk.Frame(rd_canvas, bg=SURF2)
    rd_body_id = rd_canvas.create_window(0, 0, window=rd_body, anchor="nw")
    rd_body.bind("<Configure>", lambda e: rd_canvas.configure(scrollregion=(0, 0, e.width, e.height)))
    rd_canvas.bind("<Configure>", lambda e: rd_canvas.itemconfig(rd_body_id, width=e.width))

    def _rd_wheel(e):
        if rd_body.winfo_reqheight() > rd_canvas.winfo_height() + 2:
            rd_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def _rd_bind_wheel(w):
        w.bind("<MouseWheel>", _rd_wheel)
        for c in w.winfo_children():
            _rd_bind_wheel(c)
    tvr = ttk.Treeview(fr_runs, columns=("t", "wh", "mem", "red"), show="headings", height=6)
    for c, t, w, a, s in [("t", "Date", 104, "w", False), ("wh", "Whispers", 190, "w", True),
                          ("mem", "Memories", 76, "e", False), ("red", "Red", 44, "e", False)]:
        H(tvr, c, t, anchor=a)
        tvr.column(c, width=w, anchor=a, stretch=s)
    tree_colors(tvr)
    tvr.tag_configure("plain", foreground=FG)
    tvr.tag_configure("gotred", foreground=ACC, font=(F_UI, 10, "bold"))
    tvr.tag_configure("live", foreground=PRI)
    sbr = ttk.Scrollbar(fr_runs, orient="vertical", command=tvr.yview, style="Dark.Vertical.TScrollbar")
    tvr.configure(yscrollcommand=sbr.set)
    sbr.pack(side="right", fill="y")
    tvr.pack(fill="both", expand=True)
    run_rows = {}
    show_low("runs")

    def share_results():
        """Seçili planın koşu özetini (Markdown) panoya kopyalar ve GitHub Discussions'ta yeni bir
        "Show and tell" paylaşımı açar. Kişisel bilgi yok: fısıltı türleri, sayılar, gün, fiyatlar."""
        import urllib.parse
        import webbrowser
        plan = data["settings"].get("vs_plan", "red")
        title, body = VS.share_summary(vs_log.runs, plan, data["settings"], fe_prices(), APP_VERSION)
        win = tk.Toplevel(root, bg=BG)
        win.title(tr("Share results"))
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.transient(root)
        try:
            if st.get("win_icon"):
                win.iconphoto(False, st["win_icon"])
        except Exception:
            pass
        pad = dict(padx=20)
        T(tk.Label(win, bg=BG, fg=FG, font=(F_UI, 13, "bold"), anchor="w"), "Share results").pack(
            fill="x", pady=(18, 2), **pad)
        btns = tk.Frame(win, bg=BG)
        if title is None:
            T(tk.Label(win, bg=BG, fg=MUTED, font=(F_UI, 9), anchor="w", justify="left", wraplength=400), body).pack(
                fill="x", **pad)
            T(ttk.Button(btns, command=win.destroy), "Close").pack(side="right")
        else:
            T(tk.Label(win, bg=BG, fg=MUTED, font=(F_UI, 9), anchor="w", justify="left", wraplength=400),
              "Your run summary (whisper types, counts and dates; nothing personal) is copied. GitHub opens in "
              "your browser: paste it with Ctrl+V into the Results box, add screenshots if you like, then press "
              "“Start discussion”. A GitHub account is needed.").pack(fill="x", **pad)
            tk.Label(win, text=title, bg=BG, fg=ACC, font=(F_UI, 9, "bold"), anchor="w").pack(
                fill="x", pady=(10, 0), **pad)

            def go():
                root.clipboard_clear()
                root.clipboard_append(body)
                q_ = {"category": "show-and-tell", "title": title}
                url = SHARE_NEW_URL + "?" + urllib.parse.urlencode({**q_, "results": body})
                if len(url) > 7000:                  # çok uzun adres: özet sadece panodan yapıştırılır
                    url = SHARE_NEW_URL + "?" + urllib.parse.urlencode(q_)
                webbrowser.open(url)
                win.destroy()

            def copy_only():
                root.clipboard_clear()
                root.clipboard_append(body)
                win.destroy()
            T(ttk.Button(btns, style="Primary.TButton", command=go), "Copy and open GitHub").pack(side="right")
            T(ttk.Button(btns, command=copy_only), "Copy only").pack(side="right", padx=8)
        btns.pack(fill="x", pady=(16, 18), **pad)
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.focus_force()

    icon_button(seg, "\uE72D", "Share results", share_results, SURF, PRI).pack(side="right")

    # --- Inventory value sekmesi: oyundaki 4 envanter sekmesi, eşyalar toplam FE değerine göre ---
    inv_head = tk.Frame(tab_inv, bg=BG)
    inv_head.pack(fill="x", pady=(12, 8))
    var_comm = tk.BooleanVar(value=bool(data["settings"].get("community_prices", False)))
    T(tk.Checkbutton(inv_head, variable=var_comm, command=lambda: toggle_community(), bg=BG, fg=FG, selectcolor=SURF2,
                     activebackground=BG, activeforeground=FG, font=(F_UI, 9), highlightthickness=0),
      "Share prices").pack(side="right")
    lbl_inv_total = tk.Label(inv_head, text="—", bg=BG, fg=ACC, font=(F_NUM + " SemiBold", 20), anchor="w")
    lbl_inv_total.pack(side="left")
    lbl_inv_sub = tk.Label(inv_head, text="", bg=BG, fg=MUTED, font=(F_UI, 9), anchor="w", justify="left")
    lbl_inv_sub.pack(side="left", padx=12)
    inv_box = tk.Frame(tab_inv, bg=SURF, highlightthickness=1, highlightbackground=BORDER)
    inv_box.pack(fill="both", expand=True, pady=(0, 8))
    inv_seg = tk.Frame(inv_box, bg=SURF)
    inv_seg.pack(fill="x", padx=12, pady=(9, 0))
    lbl_inv_hint = tk.Label(inv_box, text="", bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w", justify="left",
                            wraplength=680)
    lbl_inv_hint.pack(fill="x", padx=12, pady=(4, 0))
    inv_fr = tk.Frame(inv_box, bg=SURF)
    inv_fr.pack(fill="both", expand=True, padx=12, pady=(6, 10))
    tvinv = ttk.Treeview(inv_fr, columns=("n", "unit", "total"), show="tree headings", height=14, selectmode="none")
    tvinv.heading("#0", text=tr("Item"), anchor="w")
    tvinv.column("#0", width=300, anchor="w", stretch=True)
    for c, t, w in (("n", "Count", 80), ("unit", "Price (FE)", 100), ("total", "Total (FE)", 110)):
        H(tvinv, c, t, anchor="e")
        tvinv.column(c, width=w, anchor="e", stretch=False)
    tree_colors(tvinv)
    tvinv.tag_configure("np", foreground=MUTED)
    tvinv.tag_configure("top", foreground=ACC)
    sbinv = ttk.Scrollbar(inv_fr, orient="vertical", command=tvinv.yview, style="Dark.Vertical.TScrollbar")
    tvinv.configure(yscrollcommand=sbinv.set)
    sbinv.pack(side="right", fill="y")
    tvinv.pack(fill="both", expand=True)
    inv_tabs = {}
    inv_state = {"page": "102"}

    def show_inv_page(page):
        inv_state["page"] = page
        render_inv()

    for page, name in INV_PAGES.items():
        cell = tk.Frame(inv_seg, bg=TAB_OFF, highlightthickness=1, highlightbackground=BORDER, cursor="hand2")
        cell.pack(side="left", padx=(0, 6))
        lb = tk.Label(cell, text=tr(name), bg=TAB_OFF, fg=MUTED, font=(F_UI + " Semibold", 10), padx=14, pady=5,
                      cursor="hand2")
        lb.pack()
        line_ = tk.Frame(cell, bg=TAB_OFF, height=2)
        line_.pack(fill="x")
        for w in (cell, lb):
            w.bind("<Button-1>", lambda e, p=page: show_inv_page(p))
        inv_tabs[page] = (cell, lb, line_)
        hover_tab([cell, lb, line_], lambda p=page: inv_state["page"] == p, TAB_OFF, "#16233A", MUTED, TAB_HOVER_FG)

    # İki plan da aynı sayfayı kullanır (kartlar, grafik, koşu geçmişi); FE planında içerik FE'ye göre değişir.
    def retext(w, en, opt="text"):
        """Plana göre değişen sabit yazı: dil değişince de doğru metin çevrilsin (static_txt güncellenir)."""
        for k, (ww, o, _en) in enumerate(static_txt):
            if ww is w and o == opt:
                static_txt[k] = (ww, o, en)
        if isinstance(opt, tuple):
            w.heading(opt[1], text=tr(en))
        else:
            w.configure(**{opt: tr(en)})

    chart_title = _chart_card.winfo_children()[0].winfo_children()[0]
    chart_sub = _chart_card.winfo_children()[1]
    RED_RUNS_SUB = low_views["runs"][2]
    RED_CMP_SUB = low_views["cmp"][2]
    FE_CMP_SUB = ("FE each whisper type adds to a run, learned from your Memory materials runs. Whispers are often "
                  "picked together, so “with / without” averages can mislead; the estimated effect separates them.")

    def fe_mode():
        return data["settings"].get("vs_plan") == "fe"

    def apply_plan_view():
        fe = fe_mode()
        for key, red_cap, fe_cap in (("mem", "Hero Memories", "FE earned"), ("red", "Red memories", "FE per run")):
            retext(vs_kpis[key][2], fe_cap if fe else red_cap)
            vs_kpis[key][0].config(fg=ACC if fe else (BAD if key == "red" else FG))
        retext(chart_title, "FE per run" if fe else "Hero Memories per run")
        retext(chart_sub, "Last 20 runs of this plan, newest on the right: Memory Scrap and Thread picked up, at your "
                          "Exchange prices after tax. The dashed line is your average. Click a bar to open that run."
               if fe else "Last 20 runs, newest on the right. The dashed line is your average. "
                          "Click a bar to open that run.")
        retext(tvr, "Scrap / Thread" if fe else "Memories", ("heading", "mem"))
        retext(tvr, "FE" if fe else "Red", ("heading", "red"))
        tvr.column("wh", width=150 if fe else 190)
        tvr.column("mem", width=100 if fe else 76)
        tvr.column("red", width=54 if fe else 44)
        # fısıltı türleri karşılaştırması iki planda da: FE planında değerler koşu başına FE
        for col, red_h, fe_h in (("with", "Avg with", "FE with"), ("wo", "Avg w/o", "FE w/o"),
                                 ("model", "Estimated effect", "Effect (FE / run)"), ("red", "Red / run", "Typical")):
            retext(tvw, fe_h if fe else red_h, ("heading", col))
        low_views["cmp"] = (fr_cmp, "Whisper types compared", FE_CMP_SUB if fe else RED_CMP_SUB)
        if low_state["key"] == "cmp":
            lbl_low_sub.config(text=tr(low_views["cmp"][2]))
        if not fe:
            low_views["runs"] = (fr_runs, "Run history", RED_RUNS_SUB)
            if low_state["key"] == "runs":
                lbl_low_sub.config(text=tr(RED_RUNS_SUB))
        st["runs_sig"] = None

    def show_plan(key, save=True):
        """Plan değişince sayfa da değişir; overlay'in önerisi ve alev ekranındaki tür çifti de."""
        data["settings"]["vs_plan"] = key
        VS.FLAME_PAIR = ("wea", "won") if key == "fe" else ("arm", "won")
        for k, lb in plan_lbls.items():
            on = k == key
            lb.config(bg=TAB_ON if on else SURF, fg="#FFFFFF" if on else MUTED,
                      highlightbackground=TAB_EDGE_ON if on else BORDER)
        apply_plan_view()
        if save:
            save_data(data)
            render_vs()

    # --- Üst bilgi: KPI kartları (karar verdiren rakamlar en önde) ---
    top = tk.Frame(tab_ds, bg=BG)
    top.pack(fill="x", pady=(12, 10))
    kpis = {}
    for i, (key, cap) in enumerate((("rate", "1 FE on the market"), ("be", "Break-even"),
                                    ("bal", "DS balance"), ("upd", "DS price updated"))):
        card = tk.Frame(top, bg=SURF, highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
        top.columnconfigure(i, weight=1, uniform="kpi")
        T(tk.Label(card, bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w"), cap).pack(fill="x", padx=10, pady=(8, 0))
        v = tk.Label(card, text="—", bg=SURF, fg=ACC if key == "be" else FG, font=(F_NUM + " SemiBold", 17), anchor="w")
        v.pack(fill="x", padx=10)
        s = tk.Label(card, text="", bg=SURF, fg=MUTED, font=(F_UI, 8), anchor="w")
        s.pack(fill="x", padx=10, pady=(0, 8))
        kpis[key] = (v, s)
    lbl_info = tk.Label(tab_ds, text="", bg=BG, fg=MUTED, font=(F_UI, 8), anchor="w", justify="left", wraplength=700)
    lbl_info.pack(fill="x", pady=(0, 10))

    # --- Ekrandan okunan teklifler ---
    fr_o_card, fr_o = panel(tab_ds, "Offers on screen",
                            "Blessings read from the game screen. Open or refresh Blessings to read them automatically.")
    orow = tk.Frame(fr_o, bg=SURF)
    orow.pack(fill="x", pady=(0, 2))
    btn_scan = T(ttk.Button(orow, style="Primary.TButton", command=lambda: start_scan("manual")), "👁  Read screen")
    btn_scan.pack(side="left")
    lbl_ocr = tk.Label(orow, text="", bg=SURF, fg=MUTED, font=(F_UI, 9), anchor="w", justify="left")
    lbl_ocr.pack(side="left", padx=10, fill="x", expand=True)
    tvo = ttk.Treeview(fr_o, columns=("n", "item", "ds", "max", "res"), show="headings", height=3)
    for c, t, w, a in [("n", "#", 32, "center"), ("item", "Item", 230, "w"), ("ds", "Price DS", 80, "e"),
                       ("max", "Max DS", 80, "e"), ("res", "Verdict", 140, "e")]:
        H(tvo, c, t, anchor=a)
        tvo.column(c, width=w, anchor=a)
    tree_colors(tvo)
    tvo.tag_configure("good", foreground=GOOD, font=(F_UI, 10, "bold"))
    tvo.tag_configure("bad", foreground=BAD)
    tvo.tag_configure("unk", foreground=MUTED)
    # tablo teklif okununca görünür (boşken sadece buton + durum satırı: panel küçük kalsın)

    # --- DS ile alınabilen eşyalar: hangileri önerilsin (en değersizden en değerliye) ---
    fr_it_card, fr_it = panel(tab_ds, "Items sold for DS",
                              "Click a row to recommend it or hide it from the advice.",
                              fill="both", expand=True)      # kalan boşluğu doldurur; teklifler okununca kısalır
    tvi = ttk.Treeview(fr_it, columns=("item", "packs", "val", "rec"), show="headings", height=5, selectmode="none")
    for c, t, w, a in [("item", "Item", 230, "w"), ("packs", "Packs", 110, "center"),
                       ("val", "Value (FE, best pack)", 140, "e"), ("rec", "Recommend", 110, "center")]:
        H(tvi, c, t, anchor=a)
        tvi.column(c, width=w, anchor=a)
    tree_colors(tvi)
    tvi.tag_configure("on", foreground=FG)
    tvi.tag_configure("off", foreground=MUTED)
    tvi.tag_configure("hover", background=ROW_HOVER)
    sbi = ttk.Scrollbar(fr_it, orient="vertical", command=tvi.yview, style="Dark.Vertical.TScrollbar")
    tvi.configure(yscrollcommand=sbi.set)
    sbi.pack(side="right", fill="y")
    tvi.pack(fill="both", expand=True)
    item_rows = {}
    hover = {"iid": None}

    def _hover_set(iid):
        """Ortadaki tabloda üzerine gelinen satır aydınlansın (tıklanabilir olduğu belli olsun)."""
        old = hover["iid"]
        if old == iid:
            return
        if old and tvi.exists(old):
            tvi.item(old, tags=hover["tags"])
        hover["iid"] = iid
        if iid:
            hover["tags"] = tvi.item(iid, "tags")
            tvi.item(iid, tags=(hover["tags"][0], "hover"))

    tvi.bind("<Motion>", lambda e: _hover_set(tvi.identify_row(e.y) or None))
    tvi.bind("<Leave>", lambda e: _hover_set(None))

    def ds_items():
        """DS ile alınabilen eşyalar: [(değer|None, cid, [adetler])], en değersiz önce, fiyatı bilinmeyen sonda."""
        packs = {}
        for cid, qty in data["catalog"]:
            if cid == DS or eng.is_excluded(cid):
                continue
            packs.setdefault(cid, []).append(qty)
        out = []
        for cid, qs in packs.items():
            vals = [v for v in (eng.value_fe(cid, q) for q in qs) if v is not None]
            out.append((max(vals) if vals else None, cid, sorted(qs)))
        return sorted(out, key=lambda r: (r[0] is None, r[0] or 0, item_name(r[1])))

    def render_items():
        norec = data["settings"].get("no_recommend_ids", [])
        top_ = tvi.yview()[0]
        hov_cid = item_rows.get(hover["iid"])
        tvi.delete(*tvi.get_children())
        item_rows.clear()
        hover["iid"] = None
        for i, (val, cid, qs) in enumerate(ds_items()):
            on = cid not in norec
            iid = tvi.insert("", "end", values=trv((item_name(cid), ", ".join(f"×{q}" for q in qs),
                                                "—  (search on Exchange)" if val is None else fmt(val, 2),
                                                "●  Recommend" if on else "○  Hidden")),
                             tags=zebra("on" if on else "off", i))
            item_rows[iid] = cid
            if cid == hov_cid:                 # yeniden çizimde fare hâlâ bu satırdaysa vurgu kalsın
                _hover_set(iid)
        tvi.yview_moveto(top_)

    def toggle_item(e):
        iid = tvi.identify_row(e.y)
        cid = item_rows.get(iid)
        if not cid:
            return
        norec = data["settings"].setdefault("no_recommend_ids", [])
        if cid in norec:
            norec.remove(cid)
        else:
            norec.append(cid)
        save_data(data)
        st["ov_sig"] = None            # paneldeki kârlı teklifler yeniden hesaplansın
        st["dirty"] = True

    tvi.bind("<ButtonRelease-1>", toggle_item)

    # (Void Sea hesap ayarları pencereden kaldırıldı: değerler void_sea.DEFAULTS'ta, kayıttan öğrenilir)

    # --- Eşik tablosu ---
    fr_tab = ttk.LabelFrame(root, text="  MAX DS — BUY if the price is below this  ")
    # (MAKS DS tablosu gizli: hesap için iç tablo olarak duruyor, ekranda gösterilmiyor)
    cols = ("item", "qty", "mkt", "max")
    tv = ttk.Treeview(fr_tab, columns=cols, show="headings", height=6)
    for c, t, w, a in [("item", "Item", 230, "w"), ("qty", "Qty", 50, "center"),
                       ("mkt", "Market (FE/each)", 110, "e"), ("max", "Max DS", 90, "e")]:
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor=a)
    tv.tag_configure("fe", foreground=ACC)
    tv.tag_configure("np", foreground=MUTED)
    tv.pack(fill="both", expand=True, padx=6, pady=6)

    # --- Eşya ekle / sil ---
    arow = tk.Frame(fr_tab, bg=BG)
    arow.pack(fill="x", padx=6, pady=(0, 6))
    tk.Label(arow, text=tr("Add: name/ID"), bg=BG, fg=MUTED).pack(side="left")
    ent_add = tk.Entry(arow, width=22, bg="#20232a", fg=FG, insertbackground=FG, relief="flat")
    ent_add.pack(side="left", padx=4)
    tk.Label(arow, text=tr("qty"), bg=BG, fg=MUTED).pack(side="left")
    ent_aq = tk.Entry(arow, width=4, bg="#20232a", fg=FG, insertbackground=FG, relief="flat")
    ent_aq.insert(0, "1")
    ent_aq.pack(side="left", padx=4)
    lbl_add = tk.Label(arow, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8))

    def add_item():
        txt = ent_add.get().strip()
        try:
            qty = max(1, int(ent_aq.get()))
        except ValueError:
            qty = 1
        cid = txt if txt in ITEM_NAMES else None
        if not cid and txt:
            hits = [k for k, v in ITEM_NAMES.items() if txt.lower() in v.lower()]
            exact = [k for k in hits if ITEM_NAMES[k].lower() == txt.lower()]
            cid = (exact or hits or [None])[0]
            if len(hits) > 1 and not exact:
                lbl_add.config(text=tr(f"{len(hits)} matches, added the first"))
        if txt.isdigit() and not cid:
            cid = txt
        if not cid:
            lbl_add.config(text=tr("not found"))
            return
        entry = [cid, qty]
        if entry not in data["catalog"]:
            data["catalog"].append(entry)
            save_data(data)
        lbl_add.config(text=tr(f"added: {item_name(cid)}"))
        st["dirty"] = True

    def del_item():
        r = selected()
        if r and list(r) in data["catalog"]:
            data["catalog"].remove(list(r))
            save_data(data)
            st["dirty"] = True

    ttk.Button(arow, text="Add", command=add_item).pack(side="left", padx=4)
    ttk.Button(arow, text="Delete selected", command=del_item).pack(side="left", padx=4)
    lbl_add.pack(side="left", padx=4)

    # --- Hızlı kontrol ---
    fr_q = ttk.LabelFrame(root, text="  QUICK CHECK — pick a row, type the offer's DS price  ")
    # (Hızlı kontrol gizli)
    qrow = tk.Frame(fr_q, bg=BG)
    qrow.pack(fill="x", padx=6, pady=6)
    lbl_sel = tk.Label(qrow, text=tr("(no selection)"), bg=BG, fg=FG, width=26, anchor="w")
    lbl_sel.pack(side="left")
    ent = tk.Entry(qrow, width=8, bg="#20232a", fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 12))
    ent.pack(side="left", padx=6)
    tk.Label(qrow, text=tr("DS"), bg=BG, fg=MUTED).pack(side="left")
    lbl_verdict = tk.Label(qrow, text="", bg=BG, fg=FG, font=("Segoe UI", 12, "bold"))
    lbl_verdict.pack(side="left", padx=10)

    # "Recent purchases" paneli v6.5'te kaldırıldı (teklifler okununca sayfayı uzatıyordu, gereksizdi);
    # alımlar yine log'dan okunuyor: teklif eşleştirme (match_buy_to_offer) bunlara bakıyor.

    # --- DS sekmesinin alt çubuğu: hesap ayarları ---
    bot = tk.Frame(tab_ds, bg=BG)
    bot.pack(fill="x", pady=(0, 6), side="bottom", before=fr_o_card)   # her zaman görünsün
    T(tk.Label(bot, bg=BG, fg=MUTED, font=(F_UI, 9)), "Tax %").pack(side="left")
    ent_tax = entry(bot, 4)
    ent_tax.insert(0, f"{data['settings']['tax'] * 100:g}")
    ent_tax.pack(side="left", padx=(6, 14), ipady=2)
    T(tk.Label(bot, bg=BG, fg=MUTED, font=(F_UI, 9)), "Min profit %").pack(side="left")
    ent_mp = entry(bot, 4)
    ent_mp.insert(0, f"{data['settings'].get('min_profit_pct', 0):g}")
    ent_mp.pack(side="left", padx=(6, 14), ipady=2)
    var_top = tk.BooleanVar(value=bool(data["settings"].get("always_on_top", True)))

    def toggle_top():
        data["settings"]["always_on_top"] = var_top.get()
        root.attributes("-topmost", var_top.get())
        save_data(data)

    # Uygulama geneli alt çubuk (sekmelerin altında): açılışta başlat, ekran no, kapat
    bot2 = tk.Frame(root, bg=BG)
    bot2.pack(fill="x", padx=16, pady=(0, 12), side="bottom")
    tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=16, pady=(0, 10), side="bottom")
    T(ttk.Button(bot2, command=lambda: quit_app()), "Quit app").pack(side="right")
    icon_button(bot2, "\uEBE8", "Bug", lambda: open_bug_report(), SURF, FG).pack(side="right", padx=8, ipady=3)
    var_auto = tk.BooleanVar(value=bool(data["settings"].get("auto_ocr", True)))

    var_boot = tk.BooleanVar(value=bool(data["settings"].get("autostart", False)))
    var_upd = tk.BooleanVar(value=bool(data["settings"].get("check_updates", True)))

    def save_flags():
        data["settings"]["auto_ocr"] = var_auto.get()
        data["settings"]["autostart"] = var_boot.get()
        set_autostart(var_boot.get())
        was = data["settings"].get("check_updates", True)
        data["settings"]["check_updates"] = var_upd.get()
        save_data(data)
        if var_upd.get() and not was:
            threading.Thread(target=lambda: q.put(("update", latest_release())), daemon=True).start()
        elif not var_upd.get():
            show_update(None)

    T(ttk.Checkbutton(bot, variable=var_auto, command=save_flags), "Auto-read on refresh").pack(side="left", padx=(8, 0))
    T(ttk.Checkbutton(bot2, variable=var_boot, command=save_flags), "Start with Windows").pack(side="left")
    T(ttk.Checkbutton(bot2, variable=var_upd, command=save_flags), "Check for updates").pack(side="left", padx=(14, 0))
    T(tk.Label(bot2, bg=BG, fg=MUTED, font=(F_UI, 9)), "Screen #").pack(side="left", padx=(14, 0))
    ent_mon = entry(bot2, 3)
    ent_mon.insert(0, str(data["settings"].get("monitor", 1)))
    ent_mon.pack(side="left", padx=6, ipady=2)

    # Arayüz dili: sadece görünen yazılar değişir (ekran okuma, log, CSV, hata raporu İngilizce kalır).
    # Seçince hemen uygulanır: sabit yazılar yerinde çevrilir, değişen bölümler yeniden çizilir.
    # (v6.6) sağ üstte, "Select log"un solunda
    lang_fr = tk.Frame(logrow, bg=BG)
    lang_fr.pack(side="right", anchor="n", padx=(0, 12), pady=3)
    if ICON_FONT:
        tk.Label(lang_fr, text="", bg=BG, fg=MUTED, font=(ICON_FONT, 10)).pack(side="left", padx=(0, 5))  # dünya
    cb_lang = ttk.Combobox(lang_fr, values=[name for _, name in i18n.LANGS], state="readonly", width=9,
                           style="Dark.TCombobox", font=(F_UI, 9))
    cb_lang.set(dict(i18n.LANGS)[i18n.lang()])
    cb_lang.pack(side="left")

    def apply_lang(code):
        cb_lang.selection_clear()
        if code == i18n.lang():
            return
        i18n.set_lang(code)
        data["settings"]["ui_lang"] = code
        save_data(data)
        for w, opt, en in static_txt:
            try:
                if isinstance(opt, tuple):
                    w.heading(opt[1], text=tr(en))
                else:
                    w.configure(**{opt: tr(en)})
            except tk.TclError:
                pass
        st["ov_sig"] = st["runs_sig"] = st["loghelp_txt"] = None
        show_low(low_state["key"])
        refresh_ui()
        render_offers()                  # küçük paneldeki Blessings listesi de
        render_vs()                      # Void Sea paneli + koşu geçmişi, grafik, karşılaştırma
        if st.get("update"):
            show_update(st["update"])

    cb_lang.bind("<<ComboboxSelected>>", lambda e: apply_lang(i18n.LANGS[cb_lang.current()][0]))

    def save_mon(*_):
        try:
            data["settings"]["monitor"] = max(1, int(ent_mon.get()))
            save_data(data)
        except ValueError:
            pass

    ent_mon.bind("<Return>", save_mon)
    ent_mon.bind("<FocusOut>", save_mon)

    def pick_log():
        p = filedialog.askopenfilename(title="Select UE_game.log", filetypes=[("UE_game.log", "UE_game.log"), ("Log", "*.log")])
        if p:
            data["settings"]["log_path"] = p
            save_data(data)
            start_tailer()
            start_vs_watcher()

    tabbar.pack(fill="x", padx=16, pady=(8, 0))
    nb.pack(fill="both", expand=True, padx=16, pady=(0, 4))
    show_main_tab("ds")

    def apply_settings(*_):
        try:
            data["settings"]["tax"] = max(0.0, min(0.9, float(ent_tax.get().replace(",", ".")) / 100))
        except ValueError:
            pass
        try:
            data["settings"]["min_profit_pct"] = max(0.0, float(ent_mp.get().replace(",", ".")))
        except ValueError:
            pass
        save_data(data)
        st["dirty"] = True
        update_verdict()

    for e in (ent_tax, ent_mp):
        e.bind("<Return>", apply_settings)
        e.bind("<FocusOut>", apply_settings)

    row_map = {}

    def selected():
        sel = tv.selection()
        return row_map.get(sel[0]) if sel else None

    def update_verdict(*_):
        r = selected()
        if not r:
            lbl_sel.config(text=tr("(pick a row)"))
            lbl_verdict.config(text="")
            return
        cid, qty = r
        lbl_sel.config(text=tr(f"{item_name(cid)} ×{qty}"))
        try:
            ds = float(ent.get().replace(",", "."))
        except ValueError:
            lbl_verdict.config(text="", fg=FG)
            return
        ok, profit = eng.judge(cid, qty, ds)
        if ok is None:
            lbl_verdict.config(text=tr("No price"), fg=MUTED)
        elif ok:
            lbl_verdict.config(text=tr(f"✔ BUY  +{fmt(profit, 2)} FE"), fg=GOOD)
        else:
            lbl_verdict.config(text=tr(f"✖ SKIP  {fmt(profit, 2)} FE"), fg=BAD)

    tv.bind("<<TreeviewSelect>>", lambda e: (update_verdict(), ent.focus_set()))
    ent.bind("<KeyRelease>", update_verdict)

    def refresh_ui():
        d_ref = eng.ref_price(DS)
        dnet = eng.ds_net()
        tax = data["settings"]["tax"] * 100
        bal = f"{eng.ds_balance:,} DS" if eng.ds_balance is not None else "—"
        stale = False
        if d_ref:
            ts = data["prices"][DS]["ts"]
            age = datetime.fromtimestamp(ts)
            # fiyat belirlenen süreden eskiyse kartta uyar (oyun ekonomisi hızlı değişiyor)
            stale = time.time() - ts > float(data["settings"].get("price_stale_hours", 24)) * 3600
            vals = {"rate": (f"{fmt(1 / d_ref, 1)} DS", "market price"),
                    "be": (f"{fmt(1 / dnet, 1)} DS", f"after {tax:g}% tax"),
                    "bal": (bal, "from the game log"),
                    "upd": (age.strftime("%H:%M"),
                            f"⚠ {age.strftime('%d.%m')} · may be outdated" if stale else age.strftime("%d.%m.%Y"))}
            lbl_info.config(text=tr(f"Divinity Stone costs {fmt(d_ref, 5)} FE each. Prices come from your Exchange searches "
                                 "in the game log."
                                 + ("  Search “Divinity Stone” on the Exchange again to update it." if stale else "")))
        else:
            vals = {"rate": ("—", "no price yet"), "be": ("—", f"after {tax:g}% tax"),
                    "bal": (bal, "from the game log"), "upd": ("—", "")}
            lbl_info.config(text=tr("No Divinity Stone price yet. Open the Exchange in game and search “Divinity Stone”; "
                                 "the price is learned automatically."))
        for k, (v_, s_) in vals.items():
            kpis[k][0].config(text=tr(v_))
            kpis[k][1].config(text=tr(s_))
        kpis["upd"][0].config(fg=WARN if stale else FG)
        kpis["upd"][1].config(fg=WARN if stale else MUTED)

        render_items()
        sel_key = selected()
        tv.delete(*tv.get_children())
        row_map.clear()
        for cid, qty, p, mx in eng.catalog_rows():
            tag = "fe" if cid == FE else ("np" if p is None else "")
            iid = tv.insert("", "end", values=trv((item_name(cid), qty,
                                               "—  (search on Exchange)" if p is None else fmt(p, 4),
                                               "—" if mx is None else f"{int(mx):,}")),
                            tags=(tag,))
            row_map[iid] = (cid, qty)
            if sel_key == (cid, qty):
                tv.selection_set(iid)

        update_verdict()

    def start_tailer():
        if st["tailer"]:
            st["tailer"].stop_flag = True
        path = data["settings"].get("log_path")
        if not path or not os.path.isfile(path):
            c = find_log_candidates()
            if c:
                path = c[0]
                data["settings"]["log_path"] = path
                save_data(data)
        if not path or not os.path.isfile(path):
            st["status"] = "UE_game.log not found — use 'Select log…'"
            st["log"] = ("bad", "✖ LOG NOT FOUND — use 'Select log…' on the right to pick UE_game.log")
            return
        eng.purchases.clear()
        eng.refreshes.clear()
        st["status"] = f"Reading log: {path}"
        st["log"] = ("muted", f"Reading log… {path}")
        t = Tailer(path, q)
        st["tailer"] = t
        t.start()

    # ---------------- Ekran okuma ----------------
    st.update({"offers": None, "scanning": False, "ocr_ts": None, "ocr_msg": "Not read yet.",
               "scan_pending": False})

    def _covers_cards(SR):
        """Panel ya da ana pencere Blessings kartlarının üstüne denk geliyor mu? Kartlar (iki satır olsa da)
        oyun alanının üst yarısında. Oyun penceresi bulunamazsa güvenli taraf: evet (gizlenir)."""
        try:
            g = SR.game_window_rect()
        except Exception:
            g = None
        if g is None:
            return True
        y_end = g["top"] + g["height"] * 0.55
        for w in (root, ov):
            try:
                if w.state() != "normal" or not w.winfo_viewable():
                    continue
                x, y, ww, hh = w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()
            except Exception:
                return True
            if x < g["left"] + g["width"] and x + ww > g["left"] and y < y_end and y + hh > g["top"]:
                return True
        return False

    def start_scan(why):
        if st["scanning"]:
            st["scan_pending"] = True
            return
        try:
            import screen_reader as SR
            SR.setup(data["settings"].get("tesseract_path", ""))
        except Exception as e:
            st["ocr_msg"] = f"⚠ {e}"
            st["pic"] = False
            report_error("Blessings", f"screen reader not available: {e}")
            st["dirty"] = True
            return
        # pencere kartların üstünü kapatmasın: yakalarken görünmez yap (sadece üstüne denk geliyorsa;
        # panel kartlardan uzaktaysa her okumada yanıp sönmesin)
        hide = _covers_cards(SR)
        try:
            outlined = hide_highlight()        # önerilen kart çerçeveleri görüntüye girmesin
            if hide:
                root.attributes("-alpha", 0.0)
                ov.attributes("-alpha", 0.0)
            if hide or outlined:
                root.update()
                time.sleep(0.06)
            grab_t = time.time()
            img = SR.grab_screen(int(data["settings"].get("monitor", 1)))
            # ikinci kare: yenileme animasyonu sürüyorsa fiyatı kayan kartlar okunmaz, tekrar okumada denenir
            time.sleep(0.15)
            try:
                img2 = SR.grab_screen(int(data["settings"].get("monitor", 1)))
            except Exception:
                img2 = None
        except Exception as e:
            st["ocr_msg"] = f"⚠ Screen capture failed: {e}"
            st["pic"] = False
            report_error("Blessings", f"screen capture failed: {e}")
            st["dirty"] = True
            return
        finally:
            if hide:
                root.attributes("-alpha", 1.0)
                ov.attributes("-alpha", OV_ALPHA)
        st["scanning"] = True
        st["ocr_prev"] = st["ocr_msg"] if not st["ocr_msg"].startswith("Reading") else st.get("ocr_prev", "")
        st["ocr_msg"] = "Reading…"
        st["dirty"] = True

        def work():
            try:
                offers = SR.read_offers(img, BLESSING_NAMES, img2)
                ok = SR.looks_like_blessings(offers)
                if ok:
                    try:
                        SR.save_samples(offers, user_file("price_samples"))
                    except Exception:
                        pass
                if not ok and why == "manual":
                    try:
                        img.save(user_file("last_scan.png"))
                    except Exception:
                        pass
                q.put(("offers", {"offers": offers, "ok": ok, "why": why, "img": img, "grab_t": grab_t}))
            except Exception as e:
                q.put(("ocr_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def eval_offer(o):
        """-> (eşya metni, fiyat, maks, karar metni, etiket, AL mı)"""
        if o["sold"]:
            return ("Sold out", "", "", "", "unk", False)
        if o["excluded"]:
            lbl = "Bound (unsellable)" if "bound" in o["text"].lower() else "Arcane Stone"
            return (f"{lbl}  (excluded)", "", "", "", "unk", False)
        if not o["cid"]:
            return ("not read: " + (o["text"][-24:] or "—"), "" if o["price"] is None else o["price"], "", "→ check manually", "unk", False)
        name = f"{item_name(o['cid'])} ×{o['qty']}"
        if o["cid"] in data["settings"].get("no_recommend_ids", []):
            return (name, "" if o["price"] is None else o["price"], "", "read · not recommended", "unk", False)
        mx = eng.max_ds(o["cid"], o["qty"])
        mxs = "—" if mx is None else str(int(mx))
        if o["price"] is None:
            return (name, "?", mxs, "price not read", "unk", False)
        ok, profit = eng.judge(o["cid"], o["qty"], o["price"])
        if ok is None:
            return (name, o["price"], mxs, "no market price", "unk", False)  # Exchange'te aratılmalı
        if ok:
            return (name, o["price"], mxs, f"✔ BUY  +{fmt(profit, 2)} FE", "good", True)
        return (name, o["price"], mxs, f"✖ SKIP  {fmt(profit, 2)}", "bad", False)

    def render_offers():
        tvo.delete(*tvo.get_children())
        if st["offers"]:
            for i, o in enumerate(st["offers"]):
                name, price, mx, res, tag, _ = eval_offer(o)
                tvo.insert("", "end", values=trv((o["slot"], name, price, mx, res)), tags=zebra(tag, i))
        # teklif yokken tablo gizli, okununca satır sayısı kadar büyür
        n_rows = max(3, min(8, len(st["offers"] or [])))
        if int(tvo.cget("height")) != n_rows:
            tvo.configure(height=n_rows)
        if st["offers"] and not tvo.winfo_manager():
            tvo.pack(fill="x", pady=(6, 0))
        elif not st["offers"] and tvo.winfo_manager():
            tvo.pack_forget()
        lbl_ocr.config(text=tr(st["ocr_msg"]))
        render_overlay()

    def on_offers(v):
        st["scanning"] = False
        t = datetime.now().strftime("%H:%M:%S")
        why = v.get("why")
        retrying = False
        if not v["ok"] and why not in ("manual",):
            st["ocr_msg"] = st.get("ocr_prev", "")   # otomatik okuma başka bir dükkânda (ör. Fuel Shop): sessizce geç
        elif not v["ok"]:
            st["ocr_msg"] = f"⚠ {t}: could not read the Blessings screen (is it open? is 'Screen #' right?) — try again."
            st["scan_info"] = {"t": t, "fail": True}
            st["pic"] = False
            report_error("Blessings", "could not read the Blessings screen (manual read)", img=v.get("img"))
        else:
            st["pic"] = True
            new = v["offers"]
            if why == "retry" and st["offers"]:          # tekrar okumada, önceden sorunsuz okunanı koru
                old = {o["slot"]: o for o in st["offers"]}
                new = [o if (o["problem"] == "" or old.get(o["slot"], {}).get("problem", "x") != "") else old[o["slot"]]
                       for o in new]
            st["offers"] = new
            st["offers_t"] = v.get("grab_t") or time.time()
            st["offers_area"] = (v.get("img").info.get("area") if v.get("img") is not None else None)
            for o in new:             # ekranda görülen her eşya/paket listeye girsin
                if o["cid"] and not o["excluded"] and isinstance(o["qty"], int) and [o["cid"], o["qty"]] not in data["catalog"]:
                    data["catalog"].append([o["cid"], o["qty"]])
            goods = [eval_offer(o) for o in new]
            n_al = sum(1 for g in goods if g[5])
            norec = data["settings"].get("no_recommend_ids", [])
            bad = [o for o in new if o.get("problem") and o["cid"] not in norec]
            noprice = []
            for o, g in zip(new, goods):
                if g[3] == "no market price" and item_name(o["cid"]) not in noprice:
                    noprice.append(item_name(o["cid"]))
            checked = sum(1 for o in new if not o["sold"] and not o["excluded"])
            st["scan_info"] = {"t": t, "fail": False, "checked": checked, "bad": bad, "noprice": noprice,
                               "retry": st.get("retries", 0)}
            st["ocr_msg"] = (f"{t} read · {n_al} profitable offer(s)"
                             + (f" · {len(bad)} card(s) not read, try 'Read screen' again" if bad else "")
                             + (f" · no price: {', '.join(noprice)} (search on Exchange)" if noprice else ""))
            # okunamayan kart varsa kendiliğinden 2 kez daha dene (ekran animasyonu bitmemiş olabilir)
            if bad and why != "manual" and st.get("retries", 0) < 2:
                st["retries"] = st.get("retries", 0) + 1
                retrying = True
                root.after(1100, lambda: start_scan("retry"))
            elif why != "retry":
                st["retries"] = 0
            if bad and not retrying:          # tekrar denemeler bitti, hâlâ okunamayan kart var
                report_error("Blessings", f"{len(bad)} of {checked} card(s) not read",
                             [f"#{o['slot']}: {o['problem']}" for o in bad], v.get("img"))
        if why != "retry" and not retrying:
            st["retries"] = 0
        if st["scan_pending"] and not retrying:
            st["scan_pending"] = False
            root.after(300, lambda: start_scan("pending"))

    # ======================= KÜÇÜK ŞEFFAF PANEL =======================
    OV_ALPHA = 0.86
    OBG, OPANEL = BG, SURF
    ov = tk.Toplevel(root)
    ov.overrideredirect(True)                  # başlık çubuğu yok
    ov.attributes("-topmost", True)
    ov.attributes("-alpha", OV_ALPHA)
    ov.configure(bg=OBG, highlightthickness=1, highlightbackground=BORDER)
    # Panel içeriğinin yazıları (v6.8): adlandırılmış fontlar, boyut değişince yazılar yerinde büyür/küçülür.
    # Kodda yazan puntolar v6.7'deki boyutlardır; üstüne ayardaki fark eklenir (varsayılan +2, Ctrl + tekerlek 0..8).
    # Başlık, sekmeler ve alt çubuk (Read, Bug, LOG, Picture) sabit boyutta kalır.
    OV_FONT_MIN, OV_FONT_MAX, OV_FONT_DEF = 0, 8, 2
    ov_fonts = {}

    def ov_delta():
        try:
            return min(OV_FONT_MAX, max(OV_FONT_MIN, int(data["settings"].get("overlay_font", OV_FONT_DEF))))
        except (TypeError, ValueError):
            return OV_FONT_DEF

    def OF(size, weight="normal", family="Segoe UI"):
        key = (family, size, weight)
        if key not in ov_fonts:
            ov_fonts[key] = tkfont.Font(root=root, family=family, size=size + ov_delta(), weight=weight)
        return ov_fonts[key]

    if "overlay_font" not in data["settings"]:
        # v6.8'e ilk geçiş: yazılar 2 punto büyüdü, kayıtlı panel boyutu bir kez aynı oranda (~%20) büyütülür
        try:
            size, _, pos = data["settings"]["overlay_geom"].partition("+")
            w, h = (int(x) for x in size.split("x"))
            data["settings"]["overlay_geom"] = f"{round(w * 1.2)}x{round(h * 1.2)}+{pos}"
        except (KeyError, ValueError):
            pass
        data["settings"]["overlay_font"] = OV_FONT_DEF
    OV_MIN_W, OV_MIN_H = 200, 110              # elle boyutlandırmanın alt sınırı: daha küçüğü "tam boyut" sayılmaz
    # v7.0'a kadar küçük panelin başlık yüksekliği tam boyut diye kaydedilebiliyordu (panel bir daha büyümüyordu):
    # böyle kayıtlı bir boyut burada onarılır
    ov_m = re.fullmatch(r"(\d+)x(\d+)(.*)", data["settings"].get("overlay_geom", ""))
    ov_w0, ov_h0, ov_pos0 = (int(ov_m[1]), int(ov_m[2]), ov_m[3]) if ov_m else (360, 280, "+60+220")
    if ov_w0 < OV_MIN_W:
        ov_w0 = 360
    if ov_h0 < OV_MIN_H:
        ov_h0 = 280
    ov.geometry(f"{ov_w0}x{ov_h0}{ov_pos0}")
    ov_imgs = []                               # PhotoImage referansları (silinmesin)

    # Başlık: sürükleyerek taşı
    ov_head = tk.Frame(ov, bg=OPANEL)
    ov_head.pack(fill="x")
    ov_title = tk.Label(ov_head, text=tr("  Blessings · profitable offers"), bg=OPANEL, fg=MUTED,
                        font=("Segoe UI", 9, "bold"), anchor="w")
    ov_title.pack(side="left", fill="x", expand=True, pady=3)

    def quit_app():
        try:
            tray_obj["t"].stop()
        except Exception:
            pass
        save_ov_geom()
        save_data(data)
        root.destroy()

    def toggle_main():
        if root.state() in ("withdrawn", "iconic"):
            root.deiconify()
            root.lift()
        else:
            root.withdraw()

    ov_state = {"collapsed": False, "full_h": ov_h0}

    def toggle_collapse():
        if not ov_state["collapsed"]:
            # Yeni boyut hemen uygulanmaz: panel gizliyken görünene kadar, uygulama meşgulken boşta kalana kadar
            # bekler. O arada burası hâlâ eski küçük yüksekliği okur; onu tam boyut diye almak paneli kilitliyordu.
            h = ov.winfo_height()
            if h >= OV_MIN_H:
                ov_state["full_h"] = h
            active_body().pack_forget()
            ov_tabs.pack_forget()
            ov_upd.pack_forget()
            ov_foot.pack_forget()
            ov_state["collapsed"] = True
            fit_collapsed()
            btn_min.config(text="+")
        else:
            layout_overlay()
            ov.geometry(f"{ov.winfo_width()}x{ov_state['full_h']}")
            ov_state["collapsed"] = False
            btn_min.config(text="–")

    def fit_collapsed():
        """Küçük panel: sadece başlık; yeni sürüm varsa onun satırı da görünsün (gözden kaçmasın)."""
        h = ov_head.winfo_reqheight()
        if st.get("update"):
            if not ov_upd.winfo_ismapped():
                ov_upd.pack(fill="x", padx=6, pady=(4, 0))
            ov.update_idletasks()
            h += ov_upd.winfo_reqheight() + 8
        else:
            ov_upd.pack_forget()
        ov.update_idletasks()
        ov.geometry(f"{ov.winfo_width()}x{h}")

    def manual_collapse():
        ov_state["auto"] = False           # kullanıcı kendisi açtı/kapattı: otomatik küçültme karışmasın
        toggle_collapse()

    def auto_collapse_later(still_closed):
        """Blessings / Void Sea ekranı kapandı: birkaç sn bekle, ekran yeniden açılmadıysa ve panel
        o ekran yüzünden otomatik açıldıysa paneli küçült."""
        ov_state["close_seq"] = ov_state.get("close_seq", 0) + 1
        seq = ov_state["close_seq"]

        def go():
            if ov_state["close_seq"] == seq and ov_state.get("auto") and still_closed():
                ov_state["auto"] = False
                if not ov_state["collapsed"]:
                    toggle_collapse()
        root.after(AUTO_COLLAPSE_MS, go)

    btn_min = None
    for txt, cmd in (("✕", quit_app), ("–", manual_collapse), ("⚙", toggle_main)):
        b = tk.Label(ov_head, text=tr(txt), bg=OPANEL, fg=MUTED, font=("Segoe UI", 10, "bold"), cursor="hand2", padx=6)
        b.pack(side="right")
        b.bind("<Button-1>", lambda e, c=cmd: c())
        if txt == "–":
            btn_min = b

    drag = {"x": 0, "y": 0}

    def drag_start(e):
        drag["x"], drag["y"] = e.x_root - ov.winfo_x(), e.y_root - ov.winfo_y()

    def drag_move(e):
        ov.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    for w in (ov_head, ov_title):
        w.bind("<ButtonPress-1>", drag_start)
        w.bind("<B1-Motion>", drag_move)
        w.bind("<ButtonRelease-1>", lambda e: save_ov_geom())

    # Sekmeler: Blessings | Void Sea
    ov_tabs = tk.Frame(ov, bg=OBG)
    ov_tabs.pack(fill="x", padx=6, pady=(4, 0))
    tab_lbls = {}
    for key, txt in (("bl", "Blessings"), ("vs", "Void Sea")):
        lb = T(tk.Label(ov_tabs, bg=OBG, fg=MUTED, font=("Segoe UI", 9, "bold"), cursor="hand2", padx=8, pady=1), txt)
        lb.pack(side="left", padx=(0, 4))
        lb.bind("<Button-1>", lambda e, k=key: switch_tab(k))
        tab_lbls[key] = lb

    # Güncelleme notu (yeni sürüm varsa sekmelerin üstünde; tıklayınca indirme sayfası açılır)
    ov_upd = tk.Label(ov, text="", bg="#10303F", fg=PRI, font=("Segoe UI", 8, "bold"), anchor="w",
                      cursor="hand2", padx=6, pady=3)
    ov_upd.bind("<Button-1>", lambda e: q.put(("tray", "open")))   # kurulum ana pencereden onaylanır

    # Liste alanı: fare tekerleğiyle kaydırılır (v6.1: kaydırma çubuğu gösterilmiyor)
    ov_body_outer = tk.Frame(ov, bg=OBG)
    ov_body_outer.pack(fill="both", expand=True, padx=(8, 6), pady=4)
    ov_canvas = tk.Canvas(ov_body_outer, bg=OBG, highlightthickness=0, bd=0, yscrollincrement=20)
    ov_canvas.pack(side="left", fill="both", expand=True)
    ov_body = tk.Frame(ov_canvas, bg=OBG)
    ov_body_id = ov_canvas.create_window(0, 0, window=ov_body, anchor="nw")

    def _ov_sync(_e=None):
        ov_body.update_idletasks()          # yazılar yeniden kırıldıysa güncel yüksekliği al
        ov_canvas.configure(scrollregion=ov_canvas.bbox("all"))
        if ov_body.winfo_reqheight() <= ov_canvas.winfo_height() + 2:
            ov_canvas.yview_moveto(0)

    def ov_zoom(e):
        """Ctrl + fare tekerleği: paneldeki yazılar büyür/küçülür (hemen, yeniden başlatmadan)."""
        d = min(OV_FONT_MAX, max(OV_FONT_MIN, ov_delta() + (1 if e.delta > 0 else -1)))
        close_menu()                        # açık plan menüsü eski boyutta kalmasın
        if d != ov_delta():
            r = (10 + d) / (10 + ov_delta())
            data["settings"]["overlay_font"] = d
            for (_fam, size, _w), f in ov_fonts.items():
                f.configure(size=size + d)
            # Panel de yazıyla aynı oranda büyür/küçülür: başlık, sekmeler ve alt çubuk sabit, sadece içerik alanı.
            # Ters yön yok: panel elle küçültülürse yazı küçülmez.
            ov.update_idletasks()
            w, h = ov.winfo_width(), ov.winfo_height()
            nw = min(ov.winfo_screenwidth(), max(200, round(w * r)))
            if ov_state["collapsed"]:
                if ov_state["full_h"]:
                    ov_state["full_h"] = round(ov_state["full_h"] * r)
                ov.geometry(f"{nw}x{h}")
            else:
                body = active_body().winfo_height()
                ov.geometry(f"{nw}x{min(ov.winfo_screenheight(), max(110, round(h - body + body * r)))}")
            ov.after(50, _ov_sync)
            ov.after(50, _vs_sync)
            if ov_state.get("zoom_save"):
                ov.after_cancel(ov_state["zoom_save"])
            ov_state["zoom_save"] = ov.after(800, save_ov_geom)   # boyut + ayar; tekerlek dönerken her adımda yazma
        # Boyut kısa süre başlıkta görünür (alt çubukta yer az), sonra eski başlık döner
        txt = tr(f"  Text size {100 + d * 10}%")
        if "title_was" not in ov_state:
            ov_state["title_was"] = (ov_title.cget("text"), ov_title.cget("fg"))
        ov_title.config(text=txt, fg=PRI)
        if ov_state.get("title_back"):
            ov.after_cancel(ov_state["title_back"])

        def back():
            was = ov_state.pop("title_was", None)
            if was and ov_title.cget("text") == txt:     # arada liste başlığı değiştiyse dokunma
                ov_title.config(text=was[0], fg=was[1])
        ov_state["title_back"] = ov.after(1500, back)
        return "break"

    def _ov_wheel(e):
        if e.state & 0x0004:                # Ctrl basılı
            return ov_zoom(e)
        if ov_body.winfo_reqheight() > ov_canvas.winfo_height() + 2:
            ov_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def _bind_wheel(w):
        w.bind("<MouseWheel>", _ov_wheel)
        for c in w.winfo_children():
            _bind_wheel(c)

    ov_body.bind("<Configure>", _ov_sync)
    ov.bind("<Control-MouseWheel>", ov_zoom)       # başlık, sekmeler, alt çubuk üzerinde de çalışsın
    def _rewrap(body, width):
        """Panel boyutu değişince yazıların kırılma genişliğini yeni genişliğe uydur
        (yoksa açılışta dar sütuna kırılır, büyütünce sağdan taşar)."""
        wl = max(120, width - 14)

        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, tk.Label) and int(c.cget("wraplength") or 0) > 0:
                    c.config(wraplength=wl)
                walk(c)
        walk(body)

    def _ov_width():
        """Panelin genişliği; panel henüz gizliyken (açılışta) kayıtlı boyuttan."""
        w = ov.winfo_width()
        if w > 50:
            return w
        try:
            return int(str(data["settings"].get("overlay_geom", "360x")).split("x")[0])
        except ValueError:
            return 360

    ov_canvas.bind("<Configure>", lambda e: (ov_canvas.itemconfig(ov_body_id, width=e.width),
                                             _rewrap(ov_body, e.width), _ov_sync(), ov.after(60, _ov_sync)))
    _bind_wheel(ov_body_outer)

    # Void Sea sekmesinin gövdesi (sekme seçilince ov_body_outer'ın yerine geçer)
    # Blessings listesi gibi fare tekerleğiyle kaydırılır
    vs_outer = tk.Frame(ov, bg=OBG)
    vs_canvas = tk.Canvas(vs_outer, bg=OBG, highlightthickness=0, bd=0, yscrollincrement=20, width=1, height=1)
    vs_canvas.pack(side="left", fill="both", expand=True)
    vs_body = tk.Frame(vs_canvas, bg=OBG)
    vs_body_id = vs_canvas.create_window(0, 0, window=vs_body, anchor="nw")

    def _vs_sync(_e=None):
        vs_body.update_idletasks()          # yazılar yeniden kırıldıysa güncel yüksekliği al
        vs_canvas.configure(scrollregion=vs_canvas.bbox("all"))
        if vs_body.winfo_reqheight() <= vs_canvas.winfo_height() + 2:
            vs_canvas.yview_moveto(0)

    def _vs_wheel(e):
        if e.state & 0x0004:
            return ov_zoom(e)
        if vs_body.winfo_reqheight() > vs_canvas.winfo_height() + 2:
            vs_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def _vs_bind_wheel(w):
        w.bind("<MouseWheel>", _vs_wheel)
        for c in w.winfo_children():
            _vs_bind_wheel(c)

    vs_body.bind("<Configure>", _vs_sync)
    vs_canvas.bind("<Configure>", lambda e: (vs_canvas.itemconfig(vs_body_id, width=e.width),
                                             _rewrap(vs_body, e.width), _vs_sync(), ov.after(60, _vs_sync)))
    _vs_bind_wheel(vs_outer)
    ov_state_tab = {"tab": data["settings"].get("overlay_tab", "bl")}

    def active_body():
        return vs_outer if ov_state_tab["tab"] == "vs" else ov_body_outer

    def layout_overlay():
        """Paneli her zaman aynı sırayla diz: alt çubuk ve sekmeler içerikten ÖNCE yerleşmeli,
        yoksa içerik uzayınca sona kalan parça sıkışıp kaybolur."""
        for w in (ov_foot, ov_tabs, ov_upd, ov_body_outer, vs_outer):
            w.pack_forget()
        ov_foot.pack(fill="x", side="bottom")
        if st.get("update"):
            ov_upd.pack(fill="x", padx=6, pady=(4, 0))
        ov_tabs.pack(fill="x", padx=6, pady=(4, 0))
        active_body().pack(fill="both", expand=True, padx=(8, 6), pady=4)

    def switch_tab(key):
        if ov_state.get("collapsed"):
            toggle_collapse()
        ov_state_tab["tab"] = key
        layout_overlay()
        for k, lb in tab_lbls.items():
            lb.config(bg=SURF2 if k == key else OBG, fg=FG if k == key else MUTED)
        data["settings"]["overlay_tab"] = key
        if key == "vs":
            render_vs()

    # Alt çubuk: Ekran butonu + LOG ışığı + boyutlandırma tutamacı
    ov_foot = tk.Frame(ov, bg=OPANEL)
    ov_foot.pack(fill="x", side="bottom")
    ov_btn = icon_button(ov_foot, "\uE722", "Read", lambda: vs_scan() if ov_state_tab["tab"] == "vs"
                         else start_scan("manual"), "#10303F", PRI)       # \uE722: kamera
    ov_btn.pack(side="left", padx=(6, 4), pady=4)
    icon_button(ov_foot, "\uEBE8", "Bug", lambda: open_bug_report(), OPANEL, MUTED).pack(side="left", pady=4)
    ov_grip = tk.Label(ov_foot, text="◢", bg=OPANEL, fg=MUTED, cursor="size_nw_se", font=("Segoe UI", 9))
    ov_grip.pack(side="right", padx=(2, 3))
    # Picture ışığı: ekran okuma (OCR) çalışıyor mu — yeşil: son okuma başarılı / okuyucu hazır, kırmızı: hata
    pic_dot = tk.Canvas(ov_foot, width=12, height=12, bg=OPANEL, highlightthickness=0)
    pic_dot.pack(side="right", padx=(2, 6))
    pic_dot_id = pic_dot.create_oval(1, 1, 11, 11, fill=BAD, outline="")
    T(tk.Label(ov_foot, bg=OPANEL, fg=MUTED, font=("Segoe UI", 9, "bold")), "Picture").pack(side="right")
    ov_dot = tk.Canvas(ov_foot, width=12, height=12, bg=OPANEL, highlightthickness=0)
    ov_dot.pack(side="right", padx=(2, 6))
    ov_dot_id = ov_dot.create_oval(1, 1, 11, 11, fill=BAD, outline="")
    T(tk.Label(ov_foot, bg=OPANEL, fg=MUTED, font=("Segoe UI", 9, "bold")), "LOG").pack(side="right")
    tk.Label(ov_foot, text=tr(f"v{APP_VERSION}"), bg=OPANEL, fg=MUTED, font=("Segoe UI", 8)).pack(side="right", padx=(0, 8))
    ov_msg = tk.Label(ov_foot, text="", bg=OPANEL, fg=MUTED, font=("Segoe UI", 8), anchor="w")
    ov_msg.pack(side="left", fill="x", expand=True)

    rs = {"x": 0, "y": 0, "w": 0, "h": 0}

    def rs_start(e):
        rs.update(x=e.x_root, y=e.y_root, w=ov.winfo_width(), h=ov.winfo_height())

    def rs_move(e):
        w = max(OV_MIN_W, rs["w"] + e.x_root - rs["x"])
        h = max(OV_MIN_H, rs["h"] + e.y_root - rs["y"])
        ov.geometry(f"{w}x{h}")

    ov_grip.bind("<ButtonPress-1>", rs_start)
    ov_grip.bind("<B1-Motion>", rs_move)
    ov_grip.bind("<ButtonRelease-1>", lambda e: save_ov_geom())

    def save_ov_geom():
        try:
            size, _, pos = ov.geometry().partition("+")
            w, h = size.split("x")
            if ov_state["collapsed"] or int(h) < OV_MIN_H:        # kısıkken ya da boyut henüz uygulanmadıysa tam boyutu kaydet
                h = ov_state["full_h"]
            data["settings"]["overlay_geom"] = f"{w}x{h}+{pos}"
            save_data(data)
        except Exception:
            pass

    def _photo(pil_img):
        try:
            from PIL import ImageTk
            ph = ImageTk.PhotoImage(pil_img, master=ov)
            ov_imgs.append(ph)
            return ph
        except Exception:
            return None

    def render_overlay():
        goods = []
        if st["offers"]:
            for o in st["offers"]:
                ev = eval_offer(o)
                if ev[5]:
                    goods.append((o, ev))
        m = st.get("ocr_msg", "")
        info = st.get("scan_info") or {}
        sig = (id(st["offers"]), tuple(ev[3] for o, ev in goods), m[:12], eng.ref_price(DS) is None,
               info.get("t"), info.get("retry"))
        if st.get("ov_sig") == sig:          # değişiklik yoksa yeniden çizme (titremesin)
            return
        ov_title.config(text=tr(f"  Blessings · {len(goods)} profitable" if st["offers"] else "  Blessings · profitable offers"),
                        fg=GOOD if goods else MUTED)
        st["ov_sig"] = sig
        for w in ov_body.winfo_children():
            w.destroy()
        ov_imgs.clear()
        # ---- Okuma durumu şeridi: ne kadarı okundu, okunamayanlar neden okunamadı ----
        if info.get("fail"):
            lines = [(f"⚠ {info['t']} could not read the Blessings screen — press “Read”", BAD)]
        elif info:
            bad, nop = info.get("bad", []), info.get("noprice", [])
            chk = info.get("checked", 0)
            if bad:
                lines = [(f"⚠ {info['t']}  {chk - len(bad)}/{chk} cards read — MAY BE INCOMPLETE", ACC)]
                for o in bad:
                    lines.append((f"   #{o['slot']}: {o['problem']}", ACC))
                lines.append(("   → press “Read” again or check the card yourself", MUTED))
            else:
                lines = [(f"✔ {info['t']}  {chk}/{chk} cards read", MUTED)]
            if nop:
                lines.append((f"⚠ No market price: {', '.join(nop)} — search on Exchange", ACC))
        else:
            lines = []
        for txt_, col in lines:
            tk.Label(ov_body, text=tr(txt_), bg=OBG, fg=col, font=OF(8), anchor="w", justify="left",
                     wraplength=max(120, _ov_width() - 40)).pack(fill="x")
        if goods:
            # en kârlı en üstte
            def profit_of(o):
                return eng.judge(o["cid"], o["qty"], o["price"])[1] or 0
            goods.sort(key=lambda g: -profit_of(g[0]))
            for o, ev in goods:
                row = tk.Frame(ov_body, bg=OBG)
                row.pack(fill="x", pady=2)
                ph = _photo(o["icon"]) if o.get("icon") is not None else None
                if ph:
                    tk.Label(row, image=ph, bg=OBG).pack(side="left", padx=(0, 6))
                txt = tk.Frame(row, bg=OBG)
                txt.pack(side="left", fill="x", expand=True)
                tk.Label(txt, text=tr(f"#{o['slot']}  {item_name(o['cid'])} ×{o['qty']}"), bg=OBG, fg=FG,
                         font=OF(10, "bold"), anchor="w").pack(fill="x")
                tk.Label(txt, text=tr(f"{o['price']} DS   →   +{fmt(profit_of(o), 2)} FE profit"), bg=OBG, fg=GOOD,
                         font=OF(9), anchor="w").pack(fill="x")
        else:
            if st["offers"] is None:
                msg = "Not read yet.\nOpen or refresh Blessings."
            elif eng.ref_price(DS) is None:
                msg = "No Divinity Stone price.\nSearch it once on the Exchange."
            else:
                msg = "No profitable offers."
            tk.Label(ov_body, text=tr(msg), bg=OBG, fg=MUTED, font=OF(10), justify="left",
                     anchor="w").pack(fill="x", pady=6)
        _bind_wheel(ov_body)
        ov_canvas.yview_moveto(0)
        ov.after(50, _ov_sync)
        ov_msg.config(text=(tr("error, try again" if m.startswith("⚠") else ("reading…" if m.startswith("Reading") else ""))),
                      fg=BAD if m.startswith("⚠") else MUTED)

    # ---- Panel sadece oyun ön plandayken görünsün ----
    fg = {"visible": None}

    def _fg_state():
        """True: oyun ön planda · False: başka pencere · None: panelin kendisi (değiştirme)"""
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            from ctypes import wintypes
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            hwnd = u32.GetForegroundWindow()
            if not hwnd:
                return False
            ov_ids = {ov.winfo_id(), u32.GetParent(ov.winfo_id()), u32.GetAncestor(ov.winfo_id(), 2)}
            if hwnd in ov_ids:
                return None
            buf = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value.lower()
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = ""
            h = k32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                size = wintypes.DWORD(1024)
                pbuf = ctypes.create_unicode_buffer(1024)
                if k32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                    exe = pbuf.value.lower()
                k32.CloseHandle(h)
            if pid.value == os.getpid():
                # bizim büyük pencere: panel gizlenir. Açılır menü, Bug formu, onay penceresi gibi küçük
                # pencerelerimiz panelin durumunu değiştirmesin (önceden plan menüsü açılınca panel kayboluyordu)
                root_ids = {root.winfo_id(), u32.GetParent(root.winfo_id()), u32.GetAncestor(root.winfo_id(), 2)}
                return False if hwnd in root_ids else None
            # oyunu programın yolundan tanı: başlıkta "Torchlight" geçen tarayıcı sekmesi
            # (ör. Reddit'teki bir rehber) oyun sanılmasın. Başlık sadece yol okunamazsa yedek.
            if exe:
                return "torchlight" in exe
            return "torchlight" in title
        except Exception:
            return True

    # ---- Önerilen kartların çerçevesi (v6.9): oyunun üstünde, tıklamayı geçiren şeffaf pencere ----
    # Kartlar okunup "BUY" denince o kartların etrafına uygulamanın ana renginde (PRI) çerçeve çizilir. Yenileme, dükkânın kapanması,
    # satın alma ya da oyunun arka plana geçmesiyle kalkar. Ayar: "Outline profitable cards" (varsayılan açık).
    HL_KEY = "#010203"                         # bu renk pencerede görünmez (saydam)
    HL_COLOR = "#1C8AC2"                       # çerçeve rengi: ana renk (PRI) koyulaştırılmış, 5 px
    hl = tk.Toplevel(root)
    hl.overrideredirect(True)
    hl.withdraw()
    hl.configure(bg=HL_KEY)
    hl_cv = tk.Canvas(hl, bg=HL_KEY, highlightthickness=0, bd=0)
    hl_cv.pack(fill="both", expand=True)
    hl_state = {"sig": None, "styled": False}
    try:
        hl.attributes("-transparentcolor", HL_KEY)
        hl.attributes("-topmost", True)
    except tk.TclError:
        pass

    def _hl_clickthrough():
        """Tıklamalar pencerenin içinden oyuna geçsin, odak almasın, görev çubuğunda görünmesin."""
        if hl_state["styled"] or sys.platform != "win32":
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = u.GetParent(hl.winfo_id()) or hl.winfo_id()
            # WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE. WS_EX_LAYERED'i yeniden verme:
            # -transparentcolor ayarı silinir ve çerçeve hiç görünmez (denendi)
            u.SetWindowLongW(hwnd, -20, u.GetWindowLongW(hwnd, -20) | 0x20 | 0x80 | 0x08000000)
            hl_state["styled"] = True
        except Exception:
            pass

    def hide_highlight():
        """-> çerçeve görünüyor muydu"""
        was = hl_state["sig"] is not None
        if was:
            hl_state["sig"] = None
            hl_cv.delete("all")
            hl.withdraw()
        return was

    def update_highlight():
        area = st.get("offers_area")
        boxes = []
        if (data["settings"].get("card_outline", True) and st.get("offers") and area and fg["visible"]
                and st.get("shop_open") is not False and not st["scanning"]
                and st.get("refresh_t", 0) <= st.get("offers_t", 0)):
            boxes = [o["box"] for o in st["offers"] if o.get("box") and eval_offer(o)[5]]
        if not boxes:
            hide_highlight()
            return
        sig = (tuple(boxes), tuple(area.values()))
        if sig == hl_state["sig"]:
            return
        hl_state["sig"] = sig
        hl.geometry(f"{area['width']}x{area['height']}{area['left']:+d}{area['top']:+d}")
        hl_cv.delete("all")
        for x0, y0, x1, y1 in boxes:
            # ince koyu kenar + PRI renkli çerçeve + ince koyu iç kenar: açık renkli kart zemininde de seçilsin
            hl_cv.create_rectangle(x0 - 4, y0 - 4, x1 + 4, y1 + 4, outline=BG, width=1)
            hl_cv.create_rectangle(x0 - 1, y0 - 1, x1 + 1, y1 + 1, outline=HL_COLOR, width=5)
            hl_cv.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2, outline=BG, width=1)
        hl.deiconify()
        _hl_clickthrough()
        hl.attributes("-topmost", True)
        hl.lift()
        ov.lift()                              # panel çerçevenin altında kalmasın

    def fg_watch():
        state = _fg_state()
        if state:
            st["game_seen"] = time.time()
        if state is not None and state != fg["visible"]:
            fg["visible"] = state
            if state:
                ov.deiconify()
                ov.attributes("-topmost", True)
                ov.lift()
            else:
                ov.withdraw()
        try:
            update_highlight()
        except Exception:
            pass
        root.after(300, fg_watch)

    def update_dot():
        kind = st.get("log", ("bad", ""))[0]
        ov_dot.itemconfig(ov_dot_id, fill=GOOD if kind == "good" else BAD)
        pic_dot.itemconfig(pic_dot_id, fill=GOOD if st.get("pic") else BAD)

    LOG_ENABLE_HELP = ("To turn the log on in game: log in to a character → Settings → Other → “Enable Log”, "
                       "then go back to the character select screen and log in again.")

    def update_loghelp(kind):
        """Log bulunamadı ya da oyun açıkken log yazılmıyor: elle seçmeyi / log'u açmayı anlat."""
        txt = ""
        if kind == "bad" and st.get("log_nodetail"):
            txt = ("The game is writing only a basic log, so items, maps and Void Sea runs can't be read. "
                   + LOG_ENABLE_HELP)
        elif kind == "bad":
            txt = ("Log file not found. Click “Select log…” and choose UE_game.log in "
                   "<game folder>\\UE_game\\TorchLight\\Saved\\Logs.\n" + LOG_ENABLE_HELP)
        elif kind == "good" and time.time() - st.get("game_seen", 0) < 90:
            try:
                age = time.time() - os.path.getmtime(data["settings"].get("log_path", ""))
            except OSError:
                age = 0
            if age > 180:     # oyun açık ama log 3 dakikadır yazılmıyor: log kapalı olabilir
                txt = "The game is running but the log is not being written. " + LOG_ENABLE_HELP
        if txt != st.get("loghelp_txt"):
            st["loghelp_txt"] = txt
            lbl_loghelp.config(text=tr(txt))
            if txt:
                lbl_loghelp.pack(fill="x", pady=(2, 0))
            else:
                lbl_loghelp.pack_forget()

    # ---- Güncelleme kontrolü: açılışta ve 6 saatte bir GitHub'daki son sürüme bakılır ----
    def check_update():
        if data["settings"].get("check_updates", True):
            threading.Thread(target=lambda: q.put(("update", latest_release())), daemon=True).start()
        root.after(UPDATE_CHECK_MS, check_update)      # uygulama günlerce açık kalabiliyor: ara ara tekrar bak

    def show_update(rel):
        if st.get("updating"):
            return
        tag, url, zip_url = rel if rel else (None, None, None)
        new = bool(tag) and version_tuple(tag) > version_tuple(APP_VERSION) \
            and data["settings"].get("update_dismissed") != tag
        st["update"] = (tag, url, zip_url) if new else None
        if new:
            lbl_upd.config(text=tr(f"⬆  A new version is available: {tag}  (you have v{APP_VERSION})"), fg=FG)
            ov_upd.config(text=tr(f"⬆ Update {tag} available — click to open"))
            upd_go.config(state="normal" if zip_url else "disabled")
            if not upd_bar.winfo_ismapped():
                upd_bar.pack(fill="x", padx=16, pady=(4, 0), after=logbox)
            if st.get("update_notified") != tag:     # her yeni sürüm için bir kez Windows bildirimi
                st["update_notified"] = tag
                if tray_obj["t"] is not None:
                    tray_obj["t"].notify(tr(f"Version {tag} is available. Open the app and press “Install update”."))
        else:
            upd_bar.pack_forget()
        if not ov_state.get("collapsed"):
            layout_overlay()
        else:
            fit_collapsed()

    def open_update():
        import webbrowser
        webbrowser.open((st.get("update") or (None, RELEASES_URL, None))[1] or RELEASES_URL)

    def do_update():
        """Yeni sürümü indirip kurar ve uygulamayı yeniden başlatır (userdata'ya dokunulmaz)."""
        from tkinter import messagebox
        upd = st.get("update")
        if not upd or st.get("updating"):
            return
        if vs_log.active:      # koşu ortasında yeniden başlarsa toplanan memory'ler sayılmaz
            messagebox.showinfo(tr("Update"), tr("A Void Sea run is in progress. Finish the run first, then install "
                                          "the update."), parent=root)
            return
        if not messagebox.askyesno(tr("Install update"),
                                   tr(f"Download and install {upd[0]} now?\n\nThe app will restart. Your settings and "
                                   "run log (userdata folder) are not touched; the old files are kept in the backup folder."),
                                   parent=root):
            return
        st["updating"] = True
        upd_go.config(state="disabled")

        def work():
            try:
                reqs = install_update(upd[2], lambda s: q.put(("update_status", s)))
                q.put(("update_done", reqs))
            except Exception as e:
                q.put(("update_fail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def on_update_event(k, v):
        if k == "update_status":
            lbl_upd.config(text=tr(f"⬆  {st['update'][0]}: {v}"))
        elif k == "update_done":
            lbl_upd.config(text=tr(f"✔  {st['update'][0]} installed — restarting…"), fg=GOOD)
            root.update_idletasks()
            try:
                restart_app(after_pip=bool(v))
            except Exception as e:
                lbl_upd.config(text=tr(f"✔  Installed. Restart the app with Start.bat ({e})"), fg=WARN)
                return
            root.after(300, quit_app)
        else:
            st["updating"] = False
            upd_go.config(state="normal")
            lbl_upd.config(text=tr(f"⚠  Update failed: {v} — use “Release page” to download it yourself."), fg=WARN)
            report_error("Update", f"automatic update failed: {v}")

    def dismiss_update():
        """Bu sürüm için bir daha gösterme (daha yeni bir sürüm çıkınca yine görünür)."""
        if st.get("update"):
            data["settings"]["update_dismissed"] = st["update"][0]
            save_data(data)
        show_update(None)

    upd_x.bind("<Button-1>", lambda e: dismiss_update())

    def check_ocr():
        """Açılışta: ekran okuyucu (OCR) kullanılabilir mi? Picture ışığının ilk hâli."""
        def work():
            try:
                import screen_reader as SR
                SR.setup(data["settings"].get("tesseract_path", ""))
                ok = True
            except Exception:
                ok = False
            q.put(("pic", ok))
        threading.Thread(target=work, daemon=True).start()

    render_overlay()

    # ======================= VOID SEA SEKMESİ =======================
    import void_sea as VS
    vs = VS.VoidSeaState()
    vs_log = VS.RunLog(user_file("voidsea_runs.json"), data["settings"])   # koşu başına fısıltılar + kırmızı sayısı
    vs_st = {"scanning": False, "msg": "Open the Void Sea Terminal or press “Read”.", "watcher": None}

    # İki plan: "red" (Armament + Wonder: kırmızı Hero Memory) ve "fe" (Wealth + Wonder: Scrap/Thread'in FE değeri)
    def fe_plan():
        return data["settings"].get("vs_plan", "red") == "fe"

    def fe_prices():
        """Scrap / Thread'in net fiyatı (FE, satış vergisi düşülmüş); Exchange'de aranmadıysa None."""
        tax = float(data["settings"].get("tax", 0.2))
        out = {}
        for c in VS.FE_ITEMS:
            p = eng.ref_price(c)
            out[c] = p * (1 - tax) if p else None
        return out

    def fe_s():
        return VS.fe_model(vs_log.runs, fe_prices())

    show_plan(data["settings"].get("vs_plan", "red"), save=False)
    VS_COL = {"arm": ACC, "won": PRI, "wea": GOOD, "boss": "#C792EA", "?": MUTED}
    VS_PICKED = "#c3a6ff"                    # seçilmiş fısıltılar: Armament/Wonder renklerinden ayrı, açık mor
    VS_WHY = "#ff9ad5"                       # önerinin gerekçesi: diğer renklerden ayrı, pembe
    # Void Sea Seal alevleri oyundaki renkleriyle (v6.8.2): hangi satırın hangi alev olduğu bir bakışta belli
    VS_FLAME_HEX = {"blue": "#5cb8ff", "purple": "#d27cff", "yellow": "#ffc93c"}

    def vs_flame_col(side, default):
        """Alevin rengi (okunduysa), yoksa default."""
        t = (vs.tiers or {}).get(side)
        return VS_FLAME_HEX[VS.TIER_COLOR[t]] if t is not None else default

    def _whisper_short(effs):
        """Tablo için kısa özet: sadece türler ve kaç tane ("Mem qty ×2 · Rarity · Lucky"); değerler detay panelinde."""
        cnt = {}
        for k, *_ in effs:
            cnt[k] = cnt.get(k, 0) + 1
        return " · ".join(VS.SHORT_NAMES.get(k, k) + (f" ×{n}" if n > 1 else "") for k, n in cnt.items()) or "not read"

    def detail_card(title, summary, sum_col, rows=(), note=""):
        """Sağdaki detay kartı: başlık (büyük) / özet satırı (vurgulu) / etiket–değer satırları / not."""
        for w in rd_body.winfo_children():
            w.destroy()
        rd_canvas.yview_moveto(0)
        tk.Label(rd_body, text=tr(title), bg=SURF2, fg="#FFFFFF", font=(F_UI, 11, "bold"), anchor="w",
                 justify="left", wraplength=206).pack(fill="x", padx=12, pady=(12, 0))
        if summary:
            tk.Label(rd_body, text=tr(summary), bg=SURF2, fg=sum_col, font=(F_UI + " Semibold", 10), anchor="w",
                     justify="left", wraplength=206).pack(fill="x", padx=12, pady=(2, 0))
        if rows:
            tk.Frame(rd_body, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(10, 8))
            tk.Label(rd_body, text=tr("Whispers"), bg=SURF2, fg=MUTED, font=(F_UI, 8), anchor="w").pack(fill="x", padx=12)
        for label, value in rows:
            row = tk.Frame(rd_body, bg="#1F2A3E")
            row.pack(fill="x", padx=12, pady=(3, 0))
            tk.Label(row, text=tr(label), bg="#1F2A3E", fg=MUTED, font=(F_UI, 9), anchor="w").pack(side="left", padx=8, pady=4)
            tk.Label(row, text=tr(value), bg="#1F2A3E", fg=FG, font=(F_UI + " Semibold", 10), anchor="e").pack(side="right", padx=8)
        if note:
            tk.Label(rd_body, text=tr(note), bg=SURF2, fg=MUTED, font=(F_UI, 8), anchor="w", justify="left",
                     wraplength=206).pack(fill="x", padx=12, pady=(10, 0))
        tk.Frame(rd_body, bg=SURF2, height=10).pack(fill="x")    # son satır kartın kenarına yapışmasın
        _rd_bind_wheel(rd_canvas)

    def select_run(i, switch=True):
        """Grafik, Run history tablosu ve detay kartı için tek seçim: koşu kimliği = kayıttaki sırası
        (kayda sadece sona ekleniyor, değişmiyor). Grafikten de tablodan da aynı yol çalışır."""
        if i is None or not 0 <= i < len(vs_log.runs):
            return
        st["sel_run"] = i
        if switch and low_state["key"] != "runs":      # yeniden çizimde (dil değişimi vb.) sekmeye dokunma
            show_low("runs")
        iid = next((k for k, v in run_rows.items() if v == i), None)
        if iid:
            if tvr.selection() != (iid,):
                tvr.selection_set(iid)          # <<TreeviewSelect>> tekrar buraya gelir; aynı koşu, döngü yok
            tvr.see(iid)
        r = vs_log.runs[i]
        wh = VS.run_whispers(r["effects"])
        run_detail.config(highlightbackground=PRI)        # detay seçili koşuya ait: tabloyla aynı cyan vurgu
        rows = [(VS.EFFECT_NAMES.get(k, k), f"{v:g}") for k, v in wh]
        if fe_mode() and "picks" in r:
            p = r["picks"]
            th = "  ·  ".join(f"{q} {p.get(c, 0)}" for c, q in zip(VS.THREAD_IDS, ("Magic", "Rare", "Epic")))
            note = (f"Picked up: {p.get(VS.SCRAP_ID, 0)} Scrap\nThread: {th}"
                    + ("" if wh else "\nWhispers were not read for this run.")
                    + ("\nIn progress: the count is final when you leave the map." if fe_live(i) else ""))
            detail_card(f"Run {i + 1}  ·  {r['t'][5:]}", f"{VS.run_fe(r, fe_prices()):.1f} FE  ·  "
                        f"{p.get(VS.SCRAP_ID, 0)} Scrap", ACC, rows, note)
        else:
            detail_card(f"Run {i + 1}  ·  {r['t'][5:]}", f"{r['memories']} memories  ·  {r['red']} red",
                        BAD if r["red"] else PRI, rows, "" if wh else "Whispers were not read for this run.")
        draw_chart()

    def draw_chart():
        """Son 20 koşu: çubuk grafik, ortalama kesikli çizgi. Kırmızı planında memory sayısı (kırmızı çıkan koşu
        kırmızı çubuk), FE planında koşuda toplanan Scrap / Thread'in FE değeri."""
        chart.delete("all")
        fe = fe_mode()
        if fe:
            prices = fe_prices()
            items = [(i, VS.run_fe(vs_log.runs[i], prices), 0) for i in fe_run_ids()[-20:]]
            done = [VS.run_fe(vs_log.runs[i], prices) for i in fe_run_ids()
                    if vs_log.runs[i].get("tracked", True) and not fe_live(i)]
        else:
            items = [(i, vs_log.runs[i]["memories"], vs_log.runs[i]["red"]) for i in red_run_ids()[-20:]]
            done = [r["memories"] for r in VS.red_runs(vs_log.runs) if r.get("tracked")]
        w, h = chart.winfo_width(), int(chart.cget("height"))
        if w < 50:
            return
        if not items:
            chart.create_text(w // 2, h // 2, text=tr("No runs yet"), fill=MUTED, font=(F_UI, 9))
            chart_geo.update(n=0)
            return
        left, bottom, top_ = 28, h - 18, 24
        hi = max(4, math.ceil(max(v for _, v, _ in items)))
        step = max(1, -(-hi // 4))                 # 4 civarı yatay çizgi
        hi = step * -(-hi // step)
        y = lambda v: bottom - (bottom - top_) * v / hi
        for v in range(0, hi + 1, step):
            chart.create_line(left, y(v), w, y(v), fill=BORDER)
            chart.create_text(left - 6, y(v), text=str(v), fill=MUTED, font=(F_NUM, 8), anchor="e")
        slot = (w - left) / 20
        bw = max(6, slot * 0.62)
        chart_geo.update(left=left, slot=slot, ids=[i for i, _, _ in items], n=len(items))
        sel = st.get("sel_run")
        label = (lambda v: f"{v:.0f}" if v >= 10 else f"{v:.1f}") if fe else str
        for j, (i, val, red) in enumerate(items):
            x0 = left + slot * j + (slot - bw) / 2
            live = i == len(vs_log.runs) - 1 and vs_log.active
            on = i == sel
            col = BAD if red else PRI
            if on:      # seçili koşunun sütunu: arkada hafif zemin, çubuk açık renk ve beyaz kenarlı
                chart.create_rectangle(left + slot * j + 1, top_ - 6, left + slot * (j + 1) - 1, bottom,
                                       fill="#16304A", outline="")
                col = "#FF8A8A" if red else "#9BE7FF"
            if val:
                chart.create_rectangle(x0, y(val), x0 + bw, bottom, fill="" if live else col,
                                       outline="#FFFFFF" if on else col, width=2 if on else (1 if live else 0))
            elif on:
                chart.create_line(x0, bottom - 1, x0 + bw, bottom - 1, fill="#FFFFFF", width=2)
            chart.create_text(x0 + bw / 2, y(val) - 7, text=label(val),
                              fill="#FFFFFF" if on else MUTED, font=(F_NUM + (" SemiBold" if on else ""), 8))
            if len(items) <= 10 or j % 2 == (len(items) - 1) % 2:    # sıkışmasın: en yeniden geriye ikişer
                chart.create_text(x0 + bw / 2, bottom + 9, text=str(i + 1), fill=MUTED, font=(F_NUM, 8))
        if done:
            avg = sum(done) / len(done)
            chart.create_line(left, y(avg), w, y(avg), fill=ACC, dash=(4, 3))
            # sağ üstte: soldaki çubukların üstündeki sayılarla çakışmasın
            chart.create_text(w - 4, 4, text=tr(f"average {avg:.1f} FE per run" if fe else
                                                f"average {avg:.1f} per run"),
                              fill=ACC, font=(F_NUM, 8), anchor="ne")

    def render_fe_cmp():
        """Memory materials: fısıltı türlerinin koşu başına FE'ye etkisi (öğrenen modelden) ve kombinasyonlar."""
        prices = fe_prices()
        rows = VS.fe_whisper_stats(vs_log.runs, prices)
        s = fe_s()
        info = s.get("fit_info") or {"runs": 0, "active": False}
        f2 = lambda v: "—" if v is None else f"{v:.1f}"
        for i, d in enumerate(rows):
            if d["key"] == "rarity":
                model, conf, tag = "not counted", "—", "Insufficient"
            elif d["confidence"] == "Insufficient":
                model, conf, tag = "—", "Insufficient", "Insufficient"
            else:
                model = f"{d['model']:+.2f}  ({d['lo']:.2f}–{d['hi']:.2f})"
                conf = tag = d["confidence"]
            tvw.insert("", "end", values=trv((VS.EFFECT_NAMES[d["key"]], f"{d['n_with']} / {d['n_without']}",
                                              f2(d["avg_with"]), f2(d["avg_without"]), model, conf,
                                              f"{d['typical']:g}")), tags=zebra(tag, i))
        tvw.configure(height=max(1, min(7, len(rows))))
        lbl_cmp.config(text=tr("Effect = FE one typical whisper of this type adds to a run, at your prices (range: "
                               "10–90% when the runs are resampled). Typical = the usual value of that whisper."))
        done = info["runs"]
        lines = [("Analysis summary", FG, True),
                 (f"{done} finished Memory materials runs with whispers read", FG, False)]
        if info["active"]:
            lines.append((VS.describe_fe_learned(info), FG, False))
            if done < VS.FE_INT_MIN_RUNS:
                lines.append((f"Pairs of whispers are compared after {VS.FE_INT_MIN_RUNS} runs "
                              f"({VS.FE_INT_MIN_RUNS - done} to go).", MUTED, False))
            else:
                inter = VS.fe_interactions(s, VS.fe_typical(vs_log.runs))
                if inter:
                    lines.append(("Whisper pairs that work differently together than expected:", FG, False))
                    lines += [(f"   {txt}", ACC, False) for txt, _ in inter[:2]]
                else:
                    lines.append(("No whisper pair works differently together than expected.", MUTED, False))
            lines.append(("The advice uses what was learned from your runs.", MUTED, False))
        else:
            lines.append((f"The advice uses the values written on the whispers; it starts learning after "
                          f"{VS.FE_FIT_MIN_RUNS} finished runs ({max(0, VS.FE_FIT_MIN_RUNS - done)} to go).", MUTED, False))
        combos = VS.fe_combo_stats(vs_log.runs, prices)
        if combos:
            lines.append((f"Combinations seen in {VS.COMBO_MIN_RUNS}+ runs (average FE per run):", FG, False))
            lines += [(f"   {name}:  {avg:.1f} FE  ({n} runs)", MUTED, False) for name, n, avg in combos[:4]]
        else:
            lines.append((f"Combinations: none seen in {VS.COMBO_MIN_RUNS}+ runs yet.", MUTED, False))
        for w in cmp_sum.winfo_children():
            w.destroy()
        for txt, col, bold in lines:
            tk.Label(cmp_sum, text=tr(txt), bg=SURF2, fg=col, font=(F_UI, 9 if bold else 8, "bold" if bold else "normal"),
                     anchor="w", justify="left", wraplength=640).pack(fill="x", padx=10, pady=(4 if bold else 0, 0))

    def render_cmp():
        tvw.delete(*tvw.get_children())
        if fe_mode():
            render_fe_cmp()
            return
        runs = VS.red_runs(vs_log.runs)          # Memory materials koşuları karşılaştırmaya girmez
        rows = VS.whisper_stats(runs, data["settings"])
        done, left_out = VS.valid_runs(runs)
        n_red = sum(r["red"] for r in done)
        red_ok = n_red >= VS.RED_MIN_TOTAL
        f1 = lambda v: "—" if v is None else f"{v:.1f}"
        for i, d in enumerate(rows):
            red = (f"{d['red_with']:.2f} / {d['red_without']:.2f}" if red_ok and d["n_without"] and d["n_with"]
                   else "insufficient data")
            if d["key"] == "rarity":       # memory sayısını değiştirmez; değeri kırmızı tarafında
                model, conf = "no effect on count", "red only"
                tag = "red" if red_ok else "Insufficient"
            elif d["confidence"] == "Insufficient":
                model, conf, tag = "—", "Insufficient", "Insufficient"
            else:
                model = f"+{d['model']:.1f}  ({d['lo']:.1f}–{d['hi']:.1f})"
                conf = tag = d["confidence"]
            tvw.insert("", "end", values=trv((VS.EFFECT_NAMES[d["key"]], f"{d['n_with']} / {d['n_without']}",
                                          f1(d["avg_with"]), f1(d["avg_without"]), model, conf, red)),
                       tags=zebra(tag, i))
        tvw.configure(height=max(1, min(7, len(rows))))
        lbl_cmp.config(text=tr("Estimated effect = memories one typical whisper adds to a run (range: 10–90% when the "
                            "runs are resampled)."))
        # ---- Analiz özeti ----
        labels = [d["confidence"] for d in rows if d["confidence"] not in (None, "Insufficient")]
        order = ["Low", "Preliminary", "Good"]
        overall = min(labels, key=order.index) if labels else "Insufficient"
        combos = VS.combo_stats(runs)
        lines = [("Analysis summary", FG, True),
                 (f"{len(done)} of {len(runs)} runs used  ·  {sum(r['memories'] for r in done)} Hero Memories"
                  f"  ·  {n_red} red", FG, False)]
        for r, why in left_out:
            lines.append((f"Left out: run {vs_log.runs.index(r) + 1} ({r['t'][5:]}): {why}", MUTED, False))
        lines.append((f"Model confidence: {overall}"
                      + ("  —  more runs are needed for reliable comparisons" if overall in ("Insufficient", "Low")
                         else ""), {"Good": GOOD, "Preliminary": FG}.get(overall, WARN), False))
        if not red_ok:
            lines.append((f"Red chance: insufficient data ({n_red} of {VS.RED_MIN_TOTAL} red needed).", MUTED, False))
        # fısıltı çiftleri: birlikte tek tek etkilerden beklenenden fazla / az memory
        if len(done) < VS.INT_MIN_RUNS:
            lines.append((f"Pairs of whispers are compared after {VS.INT_MIN_RUNS} runs "
                          f"({VS.INT_MIN_RUNS - len(done)} to go).", MUTED, False))
        else:
            inter = VS.red_interactions(VS.model_settings(data["settings"]), VS.typical_values(runs))
            if inter:
                lines.append(("Whisper pairs that work differently together than expected:", FG, False))
                lines += [(f"   {txt}", ACC, False) for txt, _ in inter[:2]]
            else:
                lines.append(("No whisper pair works differently together than expected.", MUTED, False))
        if combos:
            lines.append(("Combinations seen in 3+ runs (average memories):", FG, False))
            lines += [(f"   {name}:  {avg:.1f}  ({n} runs, {red} red)", MUTED, False) for name, n, avg, red in combos[:4]]
        else:
            lines.append((f"Combinations: none seen in {VS.COMBO_MIN_RUNS}+ runs yet.", MUTED, False))
        for w in cmp_sum.winfo_children():
            w.destroy()
        for txt, col, bold in lines:
            tk.Label(cmp_sum, text=tr(txt), bg=SURF2, fg=col, font=(F_UI, 9 if bold else 8, "bold" if bold else "normal"),
                     anchor="w", justify="left", wraplength=640).pack(fill="x", padx=10, pady=(4 if bold else 0, 0))

    # ======================= KOYU AÇILIR MENÜ =======================
    # Tepsi simgesinin sağ tık menüsü ve overlay'deki plan seçimi. Windows'un düz beyaz menüsü yerine
    # uygulamanın renkleri: koyu lacivert zemin (hafif dikey geçiş), ince kenar, yuvarlak köşe (Windows 11),
    # üzerine gelince solda cyan çizgi; Quit üzerine gelince sıcak koyu ton.
    MENU = {"bg_top": "#101A2B", "bg_bot": "#0C1421", "head": "#0B121D", "border": "#22324A", "hover": "#16304A",
            "accent": "#2BC3FF", "text": "#E6EEF8", "sub": "#8FA6C0", "off": "#5B6B80", "icon": "#9FCBFF",
            "icon_on": "#D8F1FF", "quit_bg": "#2A1A1A", "quit_fg": "#FFB3B3"}
    menu_state = {"win": None}

    def close_menu(_e=None):
        w = menu_state.get("win")
        menu_state["win"] = None
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def dark_menu(items, x, y, header=None, width=248, up=False, focus=True, fs=0):
        """items: [(kind, metin, simge, komut)]; kind: "item" | "quit" | "sep" | "info".
        header: (başlık, sağdaki küçük yazı, alt satır) ya da None. up=True: menü imlecin üstüne açılır (tepsi).
        focus=False: odağı almaz (overlay'deki menü: oyun ön planda kalsın, panel gizlenmesin).
        fs: yazılara eklenecek punto (paneldeki menü panelin yazı boyutunu izler); ölçüler aynı oranda büyür."""
        close_menu()
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=MENU["border"])
        menu_state["win"] = win
        k = (10 + fs) / 10

        def Z(n):
            return int(round(n * k))
        width = Z(width)
        ROW, SEP, PAD, HEAD = Z(36), Z(11), Z(14), Z(54 if header and header[2] else 40) if header else 0
        h = 8 + HEAD + sum(SEP if k == "sep" else ROW for k, *_ in items) + 6
        cv = tk.Canvas(win, width=width, height=h, highlightthickness=0, bd=0, bg=MENU["bg_top"])
        cv.pack(padx=1, pady=1)
        # zemin: yukarıdan aşağı hafif koyulaşan geçiş
        a, b = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (MENU["bg_top"], MENU["bg_bot"])]
        for yy in range(0, h, 2):
            t = yy / max(1, h - 1)
            cv.create_line(0, yy, width, yy, width=2,
                           fill="#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
        yy = 4
        if header:
            cv.create_rectangle(4, yy, width - 4, yy + HEAD - 4, fill=MENU["head"], outline="")
            ph = st.get("menu_icon")
            tx = PAD
            if ph is not None:
                cv.create_image(PAD, yy + 12, image=ph, anchor="nw")
                tx = PAD + 24
            cv.create_text(tx, yy + 20, text=tr(header[0]), fill=MENU["text"], font=("Segoe UI Semibold", 10), anchor="w")
            cv.create_text(width - PAD, yy + 20, text=tr(header[1]), fill=MENU["sub"], font=("Segoe UI", 9), anchor="e")
            if header[2]:
                cv.create_text(tx, yy + 38, text=tr(header[2]), fill=MENU["sub"], font=("Segoe UI", 8), anchor="w")
            yy += HEAD
        rows = []
        for kind, text, glyph, cmd in items:
            if kind == "sep":
                cv.create_line(12, yy + SEP // 2, width - 12, yy + SEP // 2, fill=MENU["border"])
                yy += SEP
                continue
            y0 = yy
            bg = cv.create_rectangle(6, y0 + 2, width - 6, y0 + ROW - 2, fill="", outline="")
            bar = cv.create_rectangle(6, y0 + Z(9), 9, y0 + ROW - Z(9), fill="", outline="")
            fg = MENU["off"] if kind == "info" else MENU["text"]
            ic = None
            if glyph and ICON_FONT:
                ic = cv.create_text(PAD + Z(8), y0 + ROW // 2, text=glyph, fill=MENU["off"] if kind == "info" else MENU["icon"],
                                    font=(ICON_FONT, 11 + fs))
            tx = cv.create_text(PAD + (Z(26) if ICON_FONT else 0), y0 + ROW // 2, text=tr(text), fill=fg,
                                font=("Segoe UI", 10 + fs), anchor="w")
            if kind != "info":
                rows.append((y0, y0 + ROW, kind, bg, bar, ic, tx, cmd))
            yy += ROW
        hover = {"row": None}

        def paint(row, on):
            y0, y1, kind, bg, bar, ic, tx, _cmd = row
            quit_ = kind == "quit"
            cv.itemconfig(bg, fill=(MENU["quit_bg"] if quit_ else MENU["hover"]) if on else "")
            cv.itemconfig(bar, fill=MENU["accent"] if on and not quit_ else "")
            cv.itemconfig(tx, fill=(MENU["quit_fg"] if quit_ else "#FFFFFF") if on else MENU["text"])
            if ic is not None:
                cv.itemconfig(ic, fill=(MENU["quit_fg"] if quit_ else MENU["icon_on"]) if on else MENU["icon"])

        def at(ey):
            return next((r for r in rows if r[0] <= ey < r[1]), None)

        def motion(e):
            r = at(e.y)
            if r is not hover["row"]:
                if hover["row"]:
                    paint(hover["row"], False)
                if r:
                    paint(r, True)
                hover["row"] = r
            cv.config(cursor="hand2" if r else "")

        def click(e):
            r = at(e.y)
            if r:
                close_menu()
                r[7]()

        cv.bind("<Motion>", motion)
        cv.bind("<Leave>", lambda e: hover["row"] and (paint(hover["row"], False), hover.update(row=None)))
        cv.bind("<ButtonRelease-1>", click)
        win.bind("<Escape>", close_menu)
        win.update_idletasks()
        W, Hh = width + 2, h + 2
        sx, sy = root.winfo_screenwidth(), root.winfo_screenheight()
        try:            # çalışma alanı (görev çubuğu hariç)
            import ctypes
            from ctypes import wintypes
            rc = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)
            wx0, wy0, wx1, wy1 = rc.left, rc.top, rc.right, rc.bottom
        except Exception:
            wx0, wy0, wx1, wy1 = 0, 0, sx, sy
        px = x - W if up else x
        py = y - Hh if up else y
        px = max(wx0 + 4, min(px, wx1 - W - 4)) if x < wx1 else px
        py = max(wy0 + 4, min(py, wy1 - Hh - 4)) if y < wy1 else py
        win.geometry(f"{W}x{Hh}+{px}+{py}")
        try:            # Windows 11: yuvarlak köşe + ince kenar rengi (eski Windows'ta etkisiz)
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
            col = MENU["border"]
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(ctypes.c_int(int(col[5:7] + col[3:5] + col[1:3], 16))), 4)
            if focus:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            else:       # oyunun üstündeki menü: odağı almasın (WS_EX_NOACTIVATE), oyun ön planda kalsın
                GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
                u = ctypes.windll.user32
                u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
        except Exception:
            pass
        if focus:
            win.focus_force()

        def watch():
            # dışarı tıklanınca kapan (odak tepsiden gelince FocusOut güvenilir değil)
            if menu_state.get("win") is not win:
                return
            try:
                import ctypes
                down = any(ctypes.windll.user32.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02))
                mx, my = root.winfo_pointerxy()
                inside = px <= mx < px + W and py <= my < py + Hh
                if down and not inside:
                    close_menu()
                    return
            except Exception:
                pass
            win.after(80, watch)
        win.after(250, watch)
        return win

    def show_tray_menu():
        x, y = root.winfo_pointerxy()
        upd = st.get("update")
        log_kind = st.get("log", ("muted", ""))[0]
        status = (f"Update {upd[0]} available" if upd else "Up to date") + " · " + \
                 {"good": "log OK", "bad": "log not readable"}.get(log_kind, "log starting")
        G = {"open": "\uE8A7", "upd": "\uE895", "restart": "\uE72C", "folder": "\uE838", "bug": "\uEBE8",
             "quit": "\uE7E8", "on": "\uE73A", "off": "\uE739"}       # on/off: i\u015Faretli / bo\u015F kutu
        outline = bool(data["settings"].get("card_outline", True))

        def toggle_outline():
            data["settings"]["card_outline"] = not outline
            save_data(data)
            update_highlight()

        dark_menu([("item", "Open", G["open"], lambda: q.put(("tray", "open"))),
                   ("sep", "", None, None),
                   ("item", "Outline profitable cards", G["on" if outline else "off"], toggle_outline),
                   ("sep", "", None, None),
                   ("item", "Check for updates", G["upd"], lambda: q.put(("tray", "check_update"))),
                   ("item", "Restart", G["restart"], lambda: q.put(("tray", "restart"))),
                   ("item", "Open data folder", G["folder"], lambda: q.put(("tray", "folder"))),
                   ("item", "Report a bug", G["bug"], lambda: open_bug_report()),
                   ("sep", "", None, None),
                   ("quit", "Quit", G["quit"], lambda: q.put(("tray", "quit")))],
                  x, y, header=("TLI Blessing Helper", f"v{APP_VERSION}", status), up=True)

    # ======================= HATA BİLDİRİMİ (Bug) =======================
    # GitHub'a doğrudan gönderim için uygulamaya bir anahtar (token) gömmek gerekirdi; herkese açık olur.
    # Bunun yerine: uygulamadaki form -> GitHub'ın Bug şablonu alanları doldurulmuş olarak tarayıcıda açılır
    # (kullanıcı sadece "Submit"e basar). Hesabı olmayan için "Copy report": metin panoya kopyalanır.
    BUG_PARTS = ["Blessings / DS Calculator", "Void Sea Seal (flames)", "Void Sea Terminal (cards)",
                 "Run log / memory counting", "Inventory value", "Overlay / main window", "Setup / starting the app",
                 "Other"]
    bug_state = {"win": None}

    def _mask_paths(text):
        """Kullanıcı adını ve kişisel klasör yolunu rapordan çıkar."""
        home = os.path.expanduser("~")
        text = text.replace(home, "%USERPROFILE%").replace(home.replace("\\", "/"), "%USERPROFILE%")
        user = os.path.basename(home)
        return re.sub(re.escape(user), "<user>", text, flags=re.I) if len(user) >= 3 else text

    def _error_tail(limit=2500):
        for name in ("error_report.md", "error_log.txt"):
            p = user_file(name)
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        return _mask_paths(f.read()[-limit:])
                except OSError:
                    pass
        return ""

    def open_bug_report():
        if bug_state["win"] is not None:
            try:
                bug_state["win"].deiconify()
                bug_state["win"].lift()
                return
            except tk.TclError:
                pass
        win = tk.Toplevel(root, bg=BG)
        bug_state["win"] = win
        win.title(tr("Report a bug"))
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: (bug_state.update(win=None), win.destroy()))
        try:
            if st.get("win_icon"):
                win.iconphoto(False, st["win_icon"])
        except Exception:
            pass
        pad = dict(padx=18)
        hdr = tk.Frame(win, bg=BG)
        hdr.pack(fill="x", pady=(16, 4), **pad)
        if ICON_FONT:
            tk.Label(hdr, text="\uEBE8", bg=BG, fg=PRI, font=(ICON_FONT, 16)).pack(side="left", padx=(0, 10))
        T(tk.Label(hdr, bg=BG, fg=FG, font=(F_UI, 14, "bold")), "Report a bug").pack(side="left")
        tk.Label(hdr, text=f"v{APP_VERSION}", bg=BG, fg=MUTED, font=(F_UI, 9)).pack(side="right")
        T(tk.Label(win, bg=BG, fg=MUTED, font=(F_UI, 9), justify="left", wraplength=470, anchor="w"),
          "The more you fill in, the faster it can be fixed. Nothing is sent until you submit it on GitHub.").pack(
            fill="x", **pad)

        def field(label):
            T(tk.Label(win, bg=BG, fg=FG, font=(F_UI, 9, "bold"), anchor="w"), label).pack(fill="x", pady=(12, 3), **pad)

        field("Which part?")
        part = ttk.Combobox(win, values=[tr(p) for p in BUG_PARTS], state="readonly", width=52,
                            style="Dark.TCombobox", font=(F_UI, 10))
        default_part = {"vs": 2, "inv": 4}.get(st.get("main_tab"), 0) if root.state() == "normal" else \
            (2 if ov_state_tab["tab"] == "vs" else 0)
        part.current(default_part)
        part.pack(fill="x", **pad)
        field("What happened, and what did you expect?")
        what = tk.Text(win, height=6, width=56, bg=SURF2, fg=FG, insertbackground=FG, relief="flat",
                       font=(F_UI, 10), wrap="word", highlightthickness=1, highlightbackground=BORDER,
                       highlightcolor=PRI)
        what.pack(fill="x", **pad)
        field("Game resolution and window mode")
        area = None
        try:
            import screen_reader as SR
            area = SR.game_window_rect()
        except Exception:
            pass
        screen = tk.Entry(win, bg=SURF2, fg=FG, insertbackground=FG, relief="flat", font=(F_UI, 10),
                          highlightthickness=1, highlightbackground=BORDER, highlightcolor=PRI)
        screen.insert(0, f"{area['width']}x{area['height']}, windowed" if area else "")
        screen.pack(fill="x", ipady=4, **pad)
        opts = tk.Frame(win, bg=BG)
        opts.pack(fill="x", pady=(12, 0), **pad)
        v_log = tk.BooleanVar(value=True)
        v_shot = tk.BooleanVar(value=True)
        for var, txt in ((v_log, "Add the app's error log (your user name is removed)"),
                         (v_shot, "Save a screenshot of the game to attach")):
            tk.Checkbutton(opts, text=tr(txt), variable=var, bg=BG, fg=FG, selectcolor=SURF2, activebackground=BG,
                           activeforeground=FG, font=(F_UI, 9), anchor="w", highlightthickness=0).pack(fill="x")
        lbl_status = tk.Label(win, text="", bg=BG, fg=MUTED, font=(F_UI, 9), justify="left", wraplength=470, anchor="w")
        lbl_status.pack(fill="x", pady=(10, 0), **pad)

        def report():
            """-> (alanlar, düz metin rapor) ya da açıklama boşsa None."""
            text = what.get("1.0", "end").strip()
            if not text:
                lbl_status.config(text=tr("Please describe what happened."), fg=WARN)
                what.focus_set()
                return None
            f = {"version": f"v{APP_VERSION}", "part": BUG_PARTS[part.current()], "screen": screen.get().strip(),
                 "what": text, "logs": _error_tail() if v_log.get() else ""}
            plain = (f"Bug report — TLI Blessing Helper {f['version']}\nPart: {f['part']}\nScreen: {f['screen']}\n\n"
                     f"{f['what']}\n" + (f"\nError log (end):\n{f['logs']}\n" if f["logs"] else ""))
            return f, plain

        def save_shot():
            if not v_shot.get():
                return None
            try:
                import screen_reader as SR
                win.withdraw()
                root.update()
                time.sleep(0.15)
                img = SR.grab_screen(int(data["settings"].get("monitor", 1)))
                p = user_file("bug_screenshot.png")
                img.save(p)
                return p
            except Exception:
                return None
            finally:
                win.deiconify()

        def send():
            r = report()
            if not r:
                return
            f, _plain = r
            import urllib.parse
            import webbrowser
            title = f"[Bug] {f['part']}: {f['what'].splitlines()[0][:60]}"
            params = {"template": "bug_report.yml", "title": title, "version": f["version"], "part": f["part"],
                      "screen": f["screen"], "what": f["what"], "logs": f["logs"]}
            url = ISSUES_NEW_URL + "?" + urllib.parse.urlencode(params)
            if len(url) > 7500:             # GitHub uzun adresleri kesiyor: log kısaltılır
                params["logs"] = f["logs"][-800:]
                url = ISSUES_NEW_URL + "?" + urllib.parse.urlencode(params)
            shot = save_shot()
            webbrowser.open(url)
            if shot:
                try:
                    os.startfile(USER_DIR)
                except Exception:
                    pass
            lbl_status.config(fg=GOOD, text=tr("GitHub opened with your report filled in. Press “Submit new issue” there"
                                               + (" and drag bug_screenshot.png from the opened folder into the "
                                                  "Screenshots box." if shot else ".")))

        def copy():
            r = report()
            if not r:
                return
            shot = save_shot()
            root.clipboard_clear()
            root.clipboard_append(r[1])
            lbl_status.config(fg=GOOD, text=tr("Report copied. Paste it in a Reddit message or comment"
                                               + (" (screenshot saved as userdata\\bug_screenshot.png)." if shot else ".")))

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", pady=(12, 16), **pad)
        T(ttk.Button(btns, style="Primary.TButton", command=send), "Open on GitHub").pack(side="left")
        T(ttk.Button(btns, command=copy), "Copy report").pack(side="left", padx=8)
        T(ttk.Button(btns, command=lambda: (bug_state.update(win=None), win.destroy())), "Cancel").pack(side="right")
        T(tk.Label(win, bg=BG, fg=MUTED, font=(F_UI, 8), justify="left", wraplength=470, anchor="w"),
          "“Open on GitHub” needs a GitHub account. No account? Use “Copy report” and send it on Reddit.").pack(
            fill="x", pady=(0, 14), **pad)
        win.update_idletasks()
        x = root.winfo_pointerx() - win.winfo_width() // 2
        y = root.winfo_pointery() - win.winfo_height() // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        what.focus_set()

    # ======================= ENVANTER DEĞERİ =======================
    inv_imgs = {}
    inv_blank = tk.PhotoImage(master=root, width=ICON_PX, height=ICON_PX)   # simgesi olmayan satır hizalı dursun
    icon_q = queue.Queue()
    icon_asked = set()

    def icon_worker():
        """Simgeler sırayla, yavaşça (siteyi yormadan) indirilir; biten her simge sayfayı yeniler."""
        while True:
            cid, name = icon_q.get()
            if fetch_icon(cid, name, img_url=ITEM_IMGS.get(str(cid))):
                q.put(("icon", cid))
            time.sleep(0.4)

    threading.Thread(target=icon_worker, daemon=True).start()

    def item_list_tick():
        """Güncel eşya listesi günde bir indirilir (yeni sezonun eşyaları adsız kalmasın)."""
        def work():
            if refresh_item_list():
                q.put(("items", None))
        threading.Thread(target=work, daemon=True).start()
        root.after(6 * 3600 * 1000, item_list_tick)

    root.after(4000, item_list_tick)
    root.after(2000, lambda: (load_community_cache(), community_tick()))

    def inv_icon(cid):
        if cid in inv_imgs:
            return inv_imgs[cid]
        if os.path.isfile(icon_path(cid)):
            try:
                from PIL import Image, ImageTk
                inv_imgs[cid] = ImageTk.PhotoImage(Image.open(icon_path(cid)), master=root)
                return inv_imgs[cid]
            except Exception:
                return inv_blank
        name = ITEM_NAMES.get(str(cid))
        if (name or str(cid) in ITEM_IMGS) and cid not in icon_asked and icon_wanted(cid):
            icon_asked.add(cid)
            icon_q.put((cid, name or ""))
        return inv_blank

    def forget_icon_miss(cid):
        """Eşyanın adı/simge adresi yeni öğrenildi: "bulunamadı" işareti silinsin, simge yeniden aransın."""
        icon_asked.discard(cid)
        try:
            os.remove(os.path.join(ICON_DIR, f"{cid}.none"))
        except OSError:
            pass

    def name_item_dialog(cid):
        """Listede adı olmayan (yeni sezon) bir eşyaya oyuncu ad verir; simgesi de bu adla tlidb'de aranır."""
        win = tk.Toplevel(root, bg=BG)
        win.title(tr("Name this item"))
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.transient(root)
        try:
            if st.get("win_icon"):
                win.iconphoto(False, st["win_icon"])
        except Exception:
            pass
        pad = dict(padx=20)
        T(tk.Label(win, bg=BG, fg=FG, font=(F_UI, 13, "bold"), anchor="w"), "Name this item").pack(
            fill="x", pady=(18, 2), **pad)
        T(tk.Label(win, bg=BG, fg=MUTED, font=(F_UI, 9), anchor="w", justify="left", wraplength=380),
          "This item is new and not in the item list yet. Type its English name as shown in game "
          "(hover it in your bag). The icon is then looked up on tlidb.").pack(fill="x", **pad)
        ent = tk.Entry(win, bg=SURF2, fg=FG, insertbackground=FG, relief="flat", font=(F_UI, 10))
        ent.pack(fill="x", pady=(12, 0), ipady=5, **pad)
        cur = ITEM_NAMES.get(str(cid))
        if cur:
            ent.insert(0, cur)
        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", pady=(16, 18), **pad)

        def save(_e=None):
            try:
                ok = save_item_name(cid, ent.get())
            except OSError:
                ok = False
            if not ok:
                ent.focus_set()
                return
            forget_icon_miss(cid)
            inv_imgs.pop(cid, None)
            win.destroy()
            render_inv()
        T(ttk.Button(btns, style="Primary.TButton", command=save), "Save").pack(side="right")
        T(ttk.Button(btns, command=win.destroy), "Cancel").pack(side="right", padx=8)
        ent.bind("<Return>", save)
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.focus_force()
        ent.focus_set()

    def nameable(cid):
        return bool(cid) and (cid not in ITEM_NAMES or cid in USER_NAMED)

    def inv_menu(e):
        cid = tvinv.identify_row(e.y)
        if nameable(cid):
            dark_menu([("item", "Name this item…", "", lambda: name_item_dialog(cid))],
                      e.x_root, e.y_root, width=200)

    def inv_double(e):
        cid = tvinv.identify_row(e.y)
        if nameable(cid):
            name_item_dialog(cid)

    tvinv.bind("<Button-3>", inv_menu)
    tvinv.bind("<Double-1>", inv_double)

    # ---- Topluluk fiyatları (isteğe bağlı): öğrenilen fiyatlar gönderilir, kendi fiyatı olmayanlar için ortak
    # fiyat indirilir. Kapalıyken hiçbir istek yapılmaz.
    COMMUNITY_FILE = user_file("community_prices.json")
    share = {"pending": {}, "last_up": 0.0, "last_down": 0.0, "busy": False}

    def community_on():
        return bool(data["settings"].get("community_prices", False))

    def install_id():
        """Rastgele kurulum numarası (kişiyle ilişkisi yok); sunucu aynı kişinin fiyatını iki kez saymasın diye."""
        iid = data["settings"].get("install_id")
        if not (isinstance(iid, str) and re.fullmatch(r"[0-9a-f]{32}", iid)):
            import uuid
            iid = data["settings"]["install_id"] = uuid.uuid4().hex
            save_data(data)
        return iid

    def clean_community(v):
        """Sunucudan / dosyadan gelen ortak fiyatlar: sadece geçerli eşya no ve makul pozitif fiyat kalır
        (bozuk bir cevap fiyat hesaplarını çökertmesin)."""
        out = {}
        if isinstance(v, dict):
            for cid, it in v.items():
                p = it.get("p") if isinstance(it, dict) else None
                if (re.fullmatch(r"\d{1,9}", str(cid)) and isinstance(p, (int, float)) and not isinstance(p, bool)
                        and 0 < p < 1e6):
                    out[str(cid)] = {"p": float(p), "n": it.get("n") if isinstance(it.get("n"), int) else 0}
        return out

    def load_community_cache():
        if not community_on():
            eng.community = {}
            return
        try:
            with open(COMMUNITY_FILE, encoding="utf-8") as f:
                eng.community = clean_community(json.load(f).get("prices", {}))
        except (OSError, ValueError, AttributeError):
            eng.community = {}

    def queue_share(v):
        """Log'dan yeni öğrenilen fiyat (son 24 saatte görülmüş): bir sonraki gönderimde yollanır."""
        if community_on() and v.get("prices") and not v.get("filtered") and abs(time.time() - v["ts"]) < 86400:
            lst = v["prices"][:3]
            share["pending"][str(v["cid"])] = (sum(lst) / len(lst), int(v["ts"]))

    def _http(method, body=None):
        import urllib.request
        head = {"User-Agent": f"TLI-Blessing-Helper/{APP_VERSION}", "Content-Type": "application/json"}
        key = owner_key() if method == "POST" else None
        if key:
            head["x-owner-key"] = key
        req = urllib.request.Request(PRICES_API, method=method, data=json.dumps(body).encode() if body else None,
                                     headers=head)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def community_tick():
        """15 sn'de bir: gönderilecek fiyat varsa gönder (en fazla 2 dk'da bir), ortak fiyatları 30 dk'da bir indir."""
        root.after(15000, community_tick)
        if not community_on() or share["busy"]:
            return
        now = time.time()
        up = share["pending"] if share["pending"] and now - share["last_up"] >= PRICE_SHARE_EVERY else None
        down = now - share["last_down"] >= PRICE_FETCH_EVERY
        if not up and not down:
            return
        if up:
            share["pending"] = {}
            share["last_up"] = now
        if down:
            share["last_down"] = now
        share["busy"] = True
        iid = install_id()

        def work():
            try:
                if up:
                    try:
                        _http("POST", {"install": iid, "prices": [{"id": c, "p": p, "t": t} for c, (p, t) in up.items()]})
                    except Exception:
                        for c, pt in up.items():                     # gönderilemedi: sonra tekrar dene
                            share["pending"].setdefault(c, pt)
                if down:
                    try:
                        res = _http("GET")
                        q.put(("community", res.get("prices", {}) if isinstance(res, dict) else {}))
                    except Exception:
                        pass
            finally:
                share["busy"] = False
        threading.Thread(target=work, daemon=True).start()

    SHARE_DOES = ["Sends the Exchange prices this app reads from your game log (item, price, time) to the "
                  "TLI Blessing Helper price server.",
                  "Downloads prices other players shared, for items you haven't searched yourself. "
                  "They are marked with “≈”.",
                  "Also fills in missing prices in the DS Calculator."]
    SHARE_DOESNT = ["Sends no account, name, IP address or game data other than prices. A random install "
                    "number is sent so the same player isn't counted twice.",
                    "Never replaces your own searches: they always come first.",
                    "Uses a shared price only when at least 3 players sent it, or when the app's developer "
                    "checked it in game; extreme values are ignored.",
                    "Sends nothing while it is off. You can turn it off any time."]

    def confirm_share():
        """"Share prices" açılırken: ne yapıp ne yapmadığını anlatan onay penceresi (uygulamanın dilinde).
        -> True: kullanıcı açtı."""
        win = tk.Toplevel(root, bg=BG)
        win.title(tr("Share prices"))
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.transient(root)
        try:
            if st.get("win_icon"):
                win.iconphoto(False, st["win_icon"])
        except Exception:
            pass
        ans = {"ok": False}
        pad = dict(padx=20)
        T(tk.Label(win, bg=BG, fg=FG, font=(F_UI, 14, "bold"), anchor="w"), "Share prices").pack(
            fill="x", pady=(18, 2), **pad)
        T(tk.Label(win, bg=BG, fg=MUTED, font=(F_UI, 9), anchor="w", justify="left", wraplength=440),
          "Players help each other fill in Exchange prices.").pack(fill="x", **pad)
        for title, rows, col, mark in (("What it does", SHARE_DOES, PRI, "✓"),
                                       ("What it doesn't do", SHARE_DOESNT, GOOD, "✕")):
            T(tk.Label(win, bg=BG, fg=col, font=(F_UI, 10, "bold"), anchor="w"), title).pack(fill="x", pady=(14, 4), **pad)
            for r in rows:
                row = tk.Frame(win, bg=BG)
                row.pack(fill="x", pady=2, **pad)
                tk.Label(row, text=mark, bg=BG, fg=col, font=(F_UI, 9, "bold"), width=2, anchor="nw").pack(side="left", anchor="n")
                T(tk.Label(row, bg=BG, fg=FG, font=(F_UI, 9), anchor="w", justify="left", wraplength=410), r).pack(
                    side="left", fill="x")
        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", pady=(18, 18), **pad)

        def done(ok):
            ans["ok"] = ok
            win.grab_release()
            win.destroy()
        T(ttk.Button(btns, style="Primary.TButton", command=lambda: done(True)), "Turn on").pack(side="right")
        T(ttk.Button(btns, command=lambda: done(False)), "Cancel").pack(side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", lambda: done(False))
        win.bind("<Escape>", lambda e: done(False))
        win.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.grab_set()
        win.focus_force()
        root.wait_window(win)
        return ans["ok"]

    def toggle_community():
        if var_comm.get() and not confirm_share():
            var_comm.set(False)
            return
        data["settings"]["community_prices"] = bool(var_comm.get())
        save_data(data)
        share["last_down"] = 0.0                  # açılınca hemen indir
        load_community_cache()
        st["inv_dirty"] = True
        st["dirty"] = True
        render_inv()

    def render_inv():
        """Envanterin seçili sekmesi: eşyalar toplam FE değerine göre (fiyatı bilinmeyenler en altta)."""
        inv = st.get("inv")
        st["inv_dirty"] = False
        pages = inv["pages"] if inv else {}
        price = lambda c: eng.ref_price(c)
        totals = {p: sum(n * price(c) for c, n in items.items() if price(c)) for p, items in pages.items()}
        for page, (cell, lb, line_) in inv_tabs.items():
            on = page == inv_state["page"]
            cell.config(bg=TAB_ON if on else TAB_OFF, highlightbackground=TAB_EDGE_ON if on else BORDER)
            lb.config(bg=TAB_ON if on else TAB_OFF, fg="#FFFFFF" if on else MUTED,
                      text=tr(INV_PAGES[page]) + (f"  ·  {totals[page]:,.0f} FE" if totals.get(page) else ""))
            line_.config(bg=PRI if on else TAB_OFF)
        tvinv.delete(*tvinv.get_children())
        if not inv:
            lbl_inv_total.config(text="—")
            lbl_inv_sub.config(text=tr("Log in to a character or press “Sort Order” in your inventory."))
            lbl_inv_hint.config(text="")
            return
        lbl_inv_total.config(text=tr(f"{sum(totals.values()):,.0f} FE"))
        lbl_inv_sub.config(text=tr(f"Inventory value at your Exchange prices (before tax)\n"
                                   f"Updated {datetime.fromtimestamp(inv['ts']).strftime('%H:%M')} · "
                                   f"press “Sort Order” in game to refresh"))
        items = pages.get(inv_state["page"], {})
        rows = sorted(items.items(), key=lambda cn: (price(cn[0]) is None, -(cn[1] * (price(cn[0]) or 0)), -cn[1]))
        n_np = 0
        for i, (cid, n) in enumerate(rows):
            p = price(cid)
            no_trade = p is None and str(cid) in NOT_TRADEABLE
            if p is None and not no_trade:
                n_np += 1
            comm = p and str(cid) not in eng.data["prices"] and cid != FE      # topluluk fiyatı: "≈"
            tvinv.insert("", "end", iid=str(cid), text="  " + tr(item_name(cid)), image=inv_icon(cid),
                         values=(f"{n:,}", ("≈ " if comm else "") + fmt(p, 4) if p else
                                 (tr("not tradeable") if no_trade else "—"),
                                 f"{n * p:,.1f}" if p else "—"),
                         tags=zebra("np" if p is None else ("top" if i < 3 else ""), i))
        hint = "Sorted by total value. "
        if any(str(c) not in ITEM_NAMES for c in items):
            hint += "Unnamed items are new this season: right-click one to name it. "
        if n_np:
            hint += (f"{n_np} item(s) have no price yet: search them once on the Exchange and they are added. ")
        if community_on():
            hint += "≈ = community price (shared by other players; your own searches always come first)."
        else:
            hint += "Turn on “Share prices” to fill in missing prices from other players."
        lbl_inv_hint.config(text=tr(hint), fg=ACC if n_np else MUTED)

    def render_runs():
        runs = vs_log.runs
        cur = runs[-1] if runs else None
        sig = (len(runs), cur and (cur["red"], cur["memories"], cur.get("tracked"), vs_log.active,
                                   sum(cur.get("picks", {}).values())),
               fe_mode() and tuple(round(v or 0, 4) for v in fe_prices().values()))   # FE değerleri fiyatla değişir
        if st.get("runs_sig") == sig:          # değişmediyse yeniden çizme (seçim kaybolmasın)
            return
        st["runs_sig"] = sig
        if fe_mode():
            render_fe_runs()
            return
        ids = red_run_ids()
        cur = runs[ids[-1]] if ids else None
        n_runs, n_red, avg, n_mem, avg_mem = vs_log.stats()
        vs_kpis["runs"][0].config(text=tr(f"{n_runs}"))
        vs_kpis["runs"][1].config(text=tr("tracked by the app"))
        vs_kpis["mem"][0].config(text=tr(f"{n_mem}"))
        vs_kpis["mem"][1].config(text=tr(f"{avg_mem:.1f} per run"))
        vs_kpis["red"][0].config(text=tr(f"{n_red}"))
        vs_kpis["red"][1].config(text=tr(f"{avg:.2f} per run"))
        retext(vs_kpis["last"][2], "Last run")
        vs_kpis["last"][0].config(text=tr(f"{cur['memories']} mem" if cur else "—"))
        vs_kpis["last"][1].config(text=(tr(f"{cur['red']} red · {cur['t'][5:]}" if cur else "no runs yet")))
        tvr.delete(*tvr.get_children())
        run_rows.clear()
        for row_i, i in enumerate(reversed(ids)):
            r = runs[i]
            live = i == len(runs) - 1 and vs_log.active
            tag = "live" if live else ("gotred" if r["red"] else "plain")
            iid = tvr.insert("", "end", values=trv(("In progress" if live else r["t"][5:],
                                                _whisper_short(VS.run_whispers(r["effects"])),
                                                r["memories"], r["red"])), tags=zebra(tag, row_i))
            run_rows[iid] = i
        if not ids:
            run_detail.config(highlightbackground=BORDER)
            detail_card("No runs yet", "", MUTED,
                        note="Enter the Void Sea Terminal in game; each run is added here automatically.")
        elif st.get("sel_run") in ids:
            select_run(st["sel_run"], switch=False)   # yeniden çizimden sonra aynı koşu seçili kalsın
        else:
            run_detail.config(highlightbackground=BORDER)
            detail_card("No run selected", "Click a run in the table or a bar in the chart.", MUTED,
                        note="Red memories are counted with the +1 / −1 buttons on the overlay's Void Sea tab.")
        draw_chart()
        render_cmp()

    def fe_run_ids():
        """FE planı sayfasındaki koşular (kayıttaki sıra numaraları): o planla yapılan, Scrap / Thread sayılanlar."""
        return [i for i, r in enumerate(vs_log.runs) if "picks" in r and VS.is_fe_run(r)]

    def red_run_ids():
        """Red memories sayfasındaki koşular: Memory materials planının koşuları hariç."""
        return [i for i, r in enumerate(vs_log.runs) if not VS.is_fe_run(r)]

    def fe_live(i):
        return i == len(vs_log.runs) - 1 and vs_log.active

    def render_fe_runs():
        """FE planı (Memory materials): aynı sayfa; kartlar, grafik ve tablo toplanan Scrap / Thread'in FE değeriyle."""
        runs = vs_log.runs
        prices = fe_prices()
        s = fe_s()
        ids = fe_run_ids()
        done = [i for i in ids if runs[i].get("tracked", True) and not fe_live(i)]
        vals = [VS.run_fe(runs[i], prices) for i in done]
        live_i = ids[-1] if ids and fe_live(ids[-1]) else None
        vs_kpis["runs"][0].config(text=tr(f"{len(done)}"))
        vs_kpis["runs"][1].config(text=tr("finished · 1 in progress" if live_i is not None else ("finished run" if len(done) == 1 else "finished runs")))
        vs_kpis["mem"][0].config(text=tr(f"{sum(vals):,.1f} FE" if vals else "—"))
        vs_kpis["mem"][1].config(text=tr("after tax, all finished runs"))
        vs_kpis["red"][0].config(text=tr(f"{sum(vals) / len(vals):.1f} FE" if vals else "—"))
        vs_kpis["red"][1].config(text=tr("after tax" if vals else "after your first finished run"))
        last = live_i if live_i is not None else (done[-1] if done else None)
        if last is None:
            retext(vs_kpis["last"][2], "Last run")
            vs_kpis["last"][0].config(text="—")
            vs_kpis["last"][1].config(text=tr("no run with this plan yet"))
        else:
            r = runs[last]
            retext(vs_kpis["last"][2], "This run · in progress" if live_i is not None else "Last run")
            vs_kpis["last"][0].config(text=tr(f"{VS.run_fe(r, prices):.1f} FE"))
            vs_kpis["last"][1].config(text=tr(f"{r['picks'].get(VS.SCRAP_ID, 0)} Scrap · "
                                              f"{sum(r['picks'].get(c, 0) for c in VS.THREAD_IDS)} Thread"
                                              + ("" if live_i is not None else f" · {r['t'][5:]}")))
        tvr.delete(*tvr.get_children())
        run_rows.clear()
        for row_i, i in enumerate(reversed(ids)):
            r = runs[i]
            p = r["picks"]
            live = fe_live(i)
            iid = tvr.insert("", "end", values=trv(("In progress" if live else r["t"][5:],
                                                _whisper_short(VS.run_whispers(r["effects"])),
                                                f"{p.get(VS.SCRAP_ID, 0)} / {sum(p.get(c, 0) for c in VS.THREAD_IDS)}",
                                                f"{VS.run_fe(r, prices):.1f}")),
                             tags=zebra("live" if live else "plain", row_i))
            run_rows[iid] = i
        src = ("last run" if s["n_base"] == 1 else f"last {s['n_base']} runs") if s["n_base"] else "default"
        sub = (f"Newest first. Click a run to see its whispers. Price after tax: Scrap {s['unit']['scrap']:.3f} FE, "
               f"Thread {s['unit']['thread']:.3f} FE · base per run {s['base']['scrap']:.0f} Scrap / "
               f"{s['base']['thread']:.1f} Thread ({src}).")
        if s["missing"]:
            sub += " No price yet for " + ", ".join(s["missing"]) + ": search it on the Exchange once."
        low_views["runs"] = (fr_runs, "Run history", sub)
        if low_state["key"] == "runs":
            lbl_low_sub.config(text=tr(sub), fg=ACC if s["missing"] else MUTED)
        if not ids:
            run_detail.config(highlightbackground=BORDER)
            detail_card("No runs yet", "", MUTED,
                        note="Pick the Memory materials plan and run the Void Sea; each run is added here "
                             "automatically with the Scrap and Threads you pick up.")
        elif st.get("sel_run") in ids:
            select_run(st["sel_run"], switch=False)
        else:
            run_detail.config(highlightbackground=BORDER)
            detail_card("No run selected", "Click a run in the table or a bar in the chart.", MUTED,
                        note="A run counts toward the averages when you leave the Void Sea map.")
        draw_chart()
        render_cmp()

    def show_run(_e=None):
        sel = tvr.selection()
        i = run_rows.get(sel[0]) if sel else None
        if i is not None and i != st.get("sel_run"):
            select_run(i)

    def chart_click(e):
        g = chart_geo
        if not g.get("n") or e.x < g["left"]:
            return
        j = int((e.x - g["left"]) // g["slot"])
        if 0 <= j < g["n"]:
            select_run(g["ids"][j])

    tvr.bind("<<TreeviewSelect>>", show_run)
    chart.bind("<Button-1>", chart_click)

    def start_vs_watcher():
        if vs_st["watcher"]:
            vs_st["watcher"].stop_flag = True
        path = data["settings"].get("log_path")
        if path and os.path.isfile(path):
            vs_st["watcher"] = VS.LogWatcher(path, q)
            vs_st["watcher"].start()

    def vs_scan(retry=False, auto=False):
        """auto=True: log'daki sayfa açılışıyla tetiklenen okuma. Sadece gerçek bir karar ekranı
        (alev ya da kart seçimi) tanınırsa sekmeyi değiştirir; değilse sessizce geçer."""
        if vs_st["scanning"]:
            return
        if not retry:
            vs_st["retries"] = 0
            vs_st["auto"] = auto
            vs_st["prev_msg"] = vs_st["msg"]
        try:   # panel kartların üstünü kapatmasın: yakalarken görünmez yap
            root.attributes("-alpha", 0.0)
            ov.attributes("-alpha", 0.0)
            root.update()
            time.sleep(0.06)
            img = VS.grab(int(data["settings"].get("monitor", 1)))
        except Exception as e:
            vs_st["msg"] = f"⚠ Screen capture failed: {e}"
            report_error("Void Sea", f"screen capture failed: {e}")
            st["pic"] = False
            render_vs()
            return
        finally:
            root.attributes("-alpha", 1.0)
            ov.attributes("-alpha", OV_ALPHA)
        vs_st["scanning"] = True
        vs_st["msg"] = "Reading…"
        render_vs()

        def work():
            try:
                q.put(("vs", ("scan", (VS.analyze_any(img), img))))   # oyunun diline göre (en / zh)
            except Exception as e:
                q.put(("vs", ("error", str(e))))
        threading.Thread(target=work, daemon=True).start()

    def on_vs(ev, val):
        if ev == "open":
            # Void Sea Seal / Terminal açıldı: animasyon bitince, sayfa hâlâ açıksa bir kez oku.
            # VoidBeacon normal haritalarda kısa açılır pencere olarak da çıkıyor; onlar 1 sn'de kapanır.
            vs_st["open_page"] = val
            vs_st["open_seq"] = vs_st.get("open_seq", 0) + 1
            seq = vs_st["open_seq"]
            root.after(1200, lambda: vs_scan(auto=True) if vs_st.get("open_page") and vs_st["open_seq"] == seq else None)
            return
        elif ev == "close":
            vs_st["open_page"] = None
            auto_collapse_later(lambda: not vs_st.get("open_page"))
            return
        elif ev == "scan":
            vs_st["scanning"] = False
            res, img = val
            t = datetime.now().strftime("%H:%M:%S")
            auto = vs_st.get("auto", False)
            ok = vs.on_analyze(res)
            if ok:
                st["pic"] = True
            decision = ok and res["mode"] in ("flame", "cards")
            # eksik okuma (kartlar henüz belirmemiş olabilir): kendiliğinden 2 kez daha dene
            # (otomatik okumada sadece sayfa hâlâ açıksa)
            # sağdaki "Seal Whispers Owned" paneli sayfadan sonra kayarak geliyor: okunamadıysa o da yeniden denenir
            if ((not (ok and vs.complete()) or (decision and not vs.owned_seen)) and vs_st.get("retries", 0) < 2
                    and (not auto or vs_st.get("open_page"))):
                vs_st["retries"] = vs_st.get("retries", 0) + 1
                vs_st["msg"] = f"Reading… (incomplete, retry {vs_st['retries']}/2)"
                root.after(1200, lambda: vs_scan(retry=True))
            elif auto and not decision:
                # karar ekranı değil (özet ekranı, haritadaki açılır pencere…): sekmeye dokunma, uyarı verme
                vs_st["msg"] = vs_st.get("prev_msg", "")
            elif ok:
                if auto:
                    switch_tab("vs")
                    ov_state["auto"] = True
                    if not vs_st.get("open_page"):     # okuma bitene kadar ekran kapandıysa
                        auto_collapse_later(lambda: not vs_st.get("open_page"))
                vs_st["msg"] = f"{t} read" + (f" · {vs.note}" if vs.note else "")
                if not vs.complete() or vs.note.startswith("read "):   # eksik okunan ekranı incelemek için sakla
                    try:
                        img.save(user_file("last_scan_voidsea.png"))
                    except Exception:
                        pass
                if not vs.complete():
                    vs_st["msg"] = f"⚠ {t} some cards were not read — press “Read”"
                if not vs.complete() or vs.note:
                    report_error("Void Sea", "some cards were not read" if not vs.complete() else vs.note,
                                 [f"screen: {res['mode']}", f"owned panel: {res.get('owned_n')}"]
                                 + [f"{VS.SIDE_LABEL[sd]}: {VS.describe(e)}" for sd, e, _ in vs.cards], img)
            else:
                vs_st["msg"] = f"⚠ {t} not read — press “Read” while the screen is open"
                report_error("Void Sea", "screen not read (manual read)", [f"screen: {res['mode']}"], img)
                try:
                    img.save(user_file("last_scan_voidsea.png"))
                except Exception:
                    pass
        elif ev == "error":
            st["pic"] = False
            vs_st["scanning"] = False
            vs_st["msg"] = f"⚠ OCR error: {val}"
            report_error("Void Sea", "OCR error", [str(val)])
        elif ev == "entered":
            offers = vs.offers               # Terminal'de görülen kartlar: reset silmeden önce al
            vs.reset()
            vs_log.start(val, offers)
            vs_st["msg"] = "Entered the Terminal: " + VS.describe(val)
        elif ev == "level":
            vs_log.on_level()
            render_runs()                    # koşu bittiyse geçmiş tablosunda "now" kalksın
            return                           # her bölge değişiminde paneli yeniden çizmeye gerek yok
        elif ev == "memory":
            vs_log.on_memory(val)
        render_vs()

    def vs_red(delta):
        vs_log.add_red(delta)
        render_vs()

    def render_vs():
        for w in vs_body.winfo_children():
            w.destroy()
        wrap = max(120, _ov_width() - 30)
        best, gains = vs.advice(data["settings"])
        n = len(vs.cards)
        pos = VS.POS_NAME.get(n, [str(i + 1) for i in range(n)])
        tab_lbls["vs"].config(text=tr(f"Void Sea · {vs.count()}/6"))

        def line(text, fg, size=9, bold=False, cmd=None):
            lb = tk.Label(vs_body, text=tr(text), bg=OBG, fg=fg, font=OF(size, "bold" if bold else "normal"),
                          anchor="w", justify="left", wraplength=wrap, cursor="hand2" if cmd else "")
            lb.pack(fill="x", pady=1)
            if cmd:
                lb.bind("<Button-1>", lambda e: cmd())

        _vs_plan_chip(wrap)
        if not data["settings"].get("vs_expanded", False):
            # kısa görünüm: sadece ne seçilecek, okundu mu, kaç whisper seçildi
            text, col = _vs_decision(pos)
            line(text, col, 12, True)
            m = vs_st["msg"]
            if vs_st["scanning"] or m.startswith("Reading"):
                state, scol = "Reading…", MUTED
            elif m.startswith("⚠"):
                state, scol = m, BAD
            elif vs.cards or vs.mode in ("flame", "overview"):
                state, scol = "✓ Read", GOOD
            else:
                state, scol = "Not read yet", MUTED
            line(f"{state}  ·  {vs.count()}/6 whispers picked", scol, 9)
            if vs.mode in ("cards", "flame") and (vs.cards or vs.mode == "flame") and not vs.owned_seen:
                line("⚠ Open the “Seal Whispers Owned” panel and press Read.", ACC, 8)
            _vs_finish(line, compact=True)
            return
        if fe_plan():
            _vs_fe_body(line, pos, n)
            _vs_finish(line)
            return

        # Kırmızı (Ultimate) memory sayacı en üstte: log kaliteyi göstermediği için elle girilir
        cur = vs_log.current()
        n_runs, n_red, avg, n_mem, avg_mem = vs_log.stats()
        row = tk.Frame(vs_body, bg=OBG)
        row.pack(fill="x", pady=(0, 2))
        tk.Label(row, text=tr(f"Red (last run): {cur['red'] if cur else 0}"), bg=OBG, fg=BAD,
                 font=OF(10, "bold")).pack(side="left")
        for txt, d in (("+1", 1), ("−1", -1)):
            b = tk.Label(row, text=tr(f" {txt} "), bg="#3A1A1F" if d > 0 else SURF2, fg=FG,
                         font=OF(9, "bold"), cursor="hand2", padx=4)
            b.pack(side="left", padx=(6, 0))
            b.bind("<Button-1>", lambda e, d=d: vs_red(d))
        # toplanan Hero Memory: log'dan otomatik, sadece Void Sea haritasında toplananlar
        line(f"Memories (last run): {cur['memories'] if cur else 0}  · automatic", FG, 9, True)
        line(f"Log: {n_runs} runs · {n_red} red ({avg:.2f}/run) · {n_mem} memories ({avg_mem:.1f}/run)", MUTED, 8)
        # her koşu bitince model kayda göre kendini günceller (void_sea.RunLog.refit)
        mdl = vs_log.model
        if mdl:
            if mdl["active"]:
                line(f"Learning: {mdl['runs']} runs · {mdl['memories']} memories — advice tuned to your log",
                     MUTED, 8)
                learned = VS.describe_learned(mdl)
                if learned:
                    line(learned, MUTED, 8)
            else:
                line(f"Learning: {mdl['runs']}/{VS.FIT_MIN_RUNS} runs — then advice is tuned to your log",
                     MUTED, 8)
            if mdl["runs"]:
                line(f"Success threshold: {mdl['thr']} memories (no red and this many or fewer = failure) · "
                     f"{mdl['ok']} successful / {mdl['fail']} failed", MUTED, 8)
            line(f"Target combination: {VS.describe_combo(mdl['target'])}", GOOD, 9)
            for i, (alt, x_) in enumerate(mdl.get("alts", []), 2):
                line(f"Option {i}: {VS.describe_combo(alt)}", MUTED, 8)

        _vs_owned_warning(line)

        if len(vs.picked) >= 6:
            line("Terminal is full (6/6).", MUTED, 10)
        elif vs.mode == "overview":
            line(f"{vs.need} more whisper(s) needed for the Terminal", FG, 11, True)
            line("Advice appears here when the selection screen opens.", MUTED, 8)
        elif vs.mode == "flame":
            # Void Sea Seal: hangi alev konmalı? Seçilmiş fısıltılara göre Rare+ fısıltının ortalama katkısı.
            fbest, out = vs.flame(data["settings"])
            if fbest in ("arm", "won"):
                line(f"▶ Put in the {VS.SIDE_LABEL[fbest]} flame", vs_flame_col(fbest, VS_COL[fbest]), 12, True)
            elif fbest == "tie":
                line("Flames are almost equal", FG, 12, True)
            else:
                line("No effect on red", MUTED, 11, True)
            for side in sorted(out, key=lambda s: -out[s][0]):
                line(f"{VS.flame_label(side, vs.tiers)} flame: average {out[side][1]:+.0f}% red",
                     vs_flame_col(side, GOOD if side == fbest else FG), 9, side == fbest)
            if vs.selected in ("arm", "won"):
                ok = fbest not in ("arm", "won") or vs.selected == fbest
                line(f"Currently selected: {VS.SIDE_LABEL[vs.selected]}" + ("" if ok else "  → change it"),
                     MUTED if ok else BAD, 9, ok is False)
        elif not vs.cards:
            line("Not read yet.", MUTED, 10)
        else:
            if isinstance(best, int):
                side = vs.cards[best][0]
                line(f"▶ {pos[best].capitalize()} · {VS.SIDE_LABEL[side]}", VS_COL[side], 12, True)
            elif best == "tie":
                line("Almost equal", FG, 12, True)
            else:
                line("No effect on red", MUTED, 11, True)
            for i in sorted(range(n), key=lambda i: -gains[i][0]):
                side, effs, _x = vs.cards[i]
                good = gains[i][0] > 0
                line(f"{pos[i]}  {VS.SIDE_LABEL[side]}: {VS.describe(effs)}   {gains[i][1]:+.0f}%",
                     GOOD if i == best else (FG if good else MUTED), 9)
        if vs.picked:
            line("Advice based on these picks: " + "; ".join(VS.describe(e) for _, e in vs.picked), VS_PICKED, 9)
        why = vs.reason(data["settings"], [f"{pos[i]} {VS.SIDE_LABEL[c[0]]}" for i, c in enumerate(vs.cards)])
        if why:
            line(why, VS_WHY, 9)
        _vs_finish(line)

    def _vs_owned_warning(line):
        # öneri sahip olunan fısıltılara göre: sağdaki panel bu okumada görülmediyse açıkça söyle
        if vs.mode in ("cards", "flame") and (vs.cards or vs.mode == "flame") and not vs.owned_seen:
            base = (f"is based on the last {len(vs.picked)} whispers read" if vs.picked
                    else "assumes you own no whispers")
            line(f"⚠ The “Seal Whispers Owned” panel on the right was not read (is it closed?). The advice {base}. "
                 "Open the panel and press “Read”.", ACC, 8)

    def _vs_fe_body(line, pos, n):
        """FE planı (Wealth + Wonder): toplanan Memory Scrap / Thread'in FE değerini en çok artıran seçim."""
        s = fe_s()
        prices = fe_prices()
        cur = vs_log.current()
        if cur is not None and "picks" in cur:
            p = cur["picks"]
            th = " ".join(f"{p[c]}{VS.THREAD_SHORT[c]}" for c in VS.THREAD_IDS if p.get(c)) or "0"
            line(f"Last run: {p.get(VS.SCRAP_ID, 0)} Scrap · {th} Thread · {VS.run_fe(cur, prices):.1f} FE  · automatic",
                 FG, 9, True)
        if s["missing"]:
            line("⚠ No price for " + ", ".join(s["missing"]) + ": search it on the Exchange once.", ACC, 8)
        boss_here = any(c[0] == "boss" for c in vs.cards) or any(k == "luckI" for k, *_ in vs._picked())
        if boss_here and not s["unit"].get("invite"):
            line("⚠ No price for Void Sea Invitation - Craving: search it on the Exchange once.", ACC, 8)
        _vs_owned_warning(line)
        labels = [f"{pos[i]} {VS.SIDE_LABEL[c[0]]}" for i, c in enumerate(vs.cards)]
        learn = ""

        if len(vs.picked) >= 6:
            line("Terminal is full (6/6).", MUTED, 10)
        elif vs.mode == "overview":
            line(f"{vs.need} more whisper(s) needed for the Terminal", FG, 11, True)
            line("Advice appears here when the selection screen opens.", MUTED, 8)
        elif vs.mode == "flame":
            fbest, out = vs.fe_flame(s)
            if fbest in out:
                line(f"▶ Put in the {VS.SIDE_LABEL[fbest]} flame", vs_flame_col(fbest, VS_COL[fbest]), 12, True)
            elif fbest == "tie":
                line("Flames are almost equal", FG, 12, True)
            else:
                line("No effect on FE", MUTED, 11, True)
            for side in sorted(out, key=lambda x: -out[x][0]):
                line(f"{VS.flame_label(side, vs.tiers)} flame: average {out[side][0]:+.1f} FE ({out[side][1]:+.0f}%)",
                     vs_flame_col(side, GOOD if side == fbest else FG), 9, side == fbest)
            if vs.selected in out:
                ok = fbest not in out or vs.selected == fbest
                line(f"Currently selected: {VS.SIDE_LABEL[vs.selected]}" + ("" if ok else "  → change it"),
                     MUTED if ok else BAD, 9, ok is False)
            elif vs.selected == "arm":
                line("The Armament flame is selected: the FE plan uses Wealth or Boss + Wonder.", ACC, 9)
        elif not vs.cards:
            line("Not read yet.", MUTED, 10)
        else:
            best, gains = vs.fe_advice(s)
            if isinstance(best, int):
                side = vs.cards[best][0]
                line(f"▶ {pos[best].capitalize()} · {VS.SIDE_LABEL[side]}", VS_COL[side], 12, True)
            elif best == "tie":
                line("Almost equal", FG, 12, True)
            else:
                line("No effect on FE", MUTED, 11, True)
            # kalan seçimler sayılıyorsa ana değer koşu sonuna kadarki, parantezde şu anki kazanç
            look = VS.fe_look_on(s, vs.fe_left())
            now = vs.fe_now(s) if look else None
            for i in sorted(range(n), key=lambda i: -gains[i][0]):
                side, effs, _x = vs.cards[i]
                good = gains[i][0] > 0
                extra = f" (now {now[i][0]:+.1f})" if look and good else ""
                line(f"{pos[i]}  {VS.SIDE_LABEL[side]}: {VS.describe(effs, fe=True)}   {gains[i][0]:+.1f} FE{extra}",
                     GOOD if i == best else (FG if good else MUTED), 9)
            paths = VS.fe_paths_note(vs._picked(), vs.cards, s, vs.fe_left())
            if paths:
                line(paths, FG, 9, True)
            if len(vs.picked) < 6:           # az görülen tür: seçilirse öğrenmeye yardım eder
                learn = VS.fe_learn_note(vs.cards, best, labels, *VS.fe_learn_counts(vs_log.runs))
        if vs.mode in ("cards", "flame") and (vs.cards or vs.mode == "flame") and len(vs.picked) < 6:
            line(VS.fe_progress(vs.count(), s), FG if vs.fe_left() == 0 else MUTED, 9, vs.fe_left() == 0)
        if vs.picked:
            line("Advice based on these picks: " + "; ".join(VS.describe(e, fe=True) for _, e in vs.picked), VS_PICKED, 9)
        why = vs.fe_reason(s, labels)
        if why:
            line(why, VS_WHY, 9)
        if learn:
            line(learn, VS_WHY, 9)
        if boss_here and s["unit"].get("invite"):
            line(VS.fe_inv_note(s), MUTED, 8)
        if s.get("fit"):                     # koşulardan öğrenilen etkiler (yazılı değerden farklıysa)
            line(VS.describe_fe_learned(s["fit"]), MUTED, 8)
        # kayıt / taban / fiyat bilgisi en altta (öneri ve gerekçesi önce görünsün)
        frs = VS.fe_runs(vs_log.runs)
        if frs:
            avg = sum(VS.run_fe(r, prices) for r in frs) / len(frs)
            line(f"Log: {len(frs)} runs · {avg:.1f} FE per run (after tax)", MUTED, 8)
        src = (("from your last run" if s["n_base"] == 1 else f"from your last {s['n_base']} runs") if s["n_base"]
               else "default — learns from your runs")
        line(f"Base per run: {s['base']['scrap']:.0f} Scrap, {s['base']['thread']:.1f} Thread ({src})", MUTED, 8)
        mix = " / ".join(f"{VS.THREAD_SHORT[c]} {r * 100:.0f}%" for c, r in s["mix"].items())
        line(f"Price after tax: Scrap {s['unit']['scrap']:.3f} FE · Thread {s['unit']['thread']:.3f} FE ({mix})",
             MUTED, 8)
        if boss_here and s["unit"].get("invite"):
            line(f"Void Sea Invitation after tax: {s['unit']['invite']:.1f} FE", MUTED, 8)

    VS_PLAN_NAME = {"red": "Red memories", "fe": "Memory materials"}

    def _vs_plan_chip(wrap):
        """Panelin üstünde plan adı; tıklayınca iki plan arasında seçim (koyu menü)."""
        row = tk.Frame(vs_body, bg=OBG)
        row.pack(fill="x", pady=(0, 3))
        key = "fe" if fe_plan() else "red"
        chip = tk.Label(row, text=tr(f"◎ {VS_PLAN_NAME[key]}  ▾"), bg="#14243A", fg=PRI, font=OF(9, "bold"),
                        padx=8, pady=2, cursor="hand2")
        chip.pack(side="left")

        def choose(_e=None):
            items = []
            for k, sub in (("red", "Armament + Wonder"), ("fe", "Wealth or Boss + Wonder")):
                items.append(("item", f"{VS_PLAN_NAME[k]} · {sub}", "\uE73E" if k == key else " ",
                              lambda k=k: show_plan(k)))
            dark_menu(items, chip.winfo_rootx(), chip.winfo_rooty() + chip.winfo_height() + 2, width=270, focus=False,
                      fs=ov_delta())
        chip.bind("<Button-1>", choose)

    def _vs_decision(pos):
        """Kısa görünümün ana satırı: (yazı, renk)."""
        fe = fe_plan()
        s = fe_s() if fe else None
        if len(vs.picked) >= 6:
            return "Terminal is full (6/6)", MUTED
        if vs.mode == "overview":
            return f"{vs.need} more whisper(s) needed", FG
        if vs.mode == "flame":
            fbest, out = vs.fe_flame(s) if fe else vs.flame(data["settings"])
            if fbest in out:
                return f"▶ Put in the {VS.SIDE_LABEL[fbest]} flame", vs_flame_col(fbest, VS_COL[fbest])
            return ("Flames are almost equal", FG) if fbest == "tie" else ("No flame helps", MUTED)
        if not vs.cards:
            return "Open the Void Sea Terminal", MUTED
        best, _gains = vs.fe_advice(s) if fe else vs.advice(data["settings"])
        if isinstance(best, int):
            side = vs.cards[best][0]
            return f"▶ {pos[best].capitalize()} · {VS.SIDE_LABEL[side]}", VS_COL[side]
        return ("Almost equal: either card", FG) if best == "tie" else ("No card helps", MUTED)

    def _vs_toggle_expand():
        data["settings"]["vs_expanded"] = not data["settings"].get("vs_expanded", False)
        save_data(data)
        render_vs()

    def _vs_finish(line, compact=False):
        m = vs_st["msg"]
        if not compact:
            line(m, BAD if m.startswith("⚠") else MUTED, 8)
        lk = tk.Label(vs_body, text=tr("Expand  ▾" if compact else "Show less  ▴"), bg=OBG, fg=PRI,
                      font=OF(8, "bold"), anchor="w", cursor="hand2")
        lk.pack(fill="x", pady=(4, 0))
        lk.bind("<Button-1>", lambda e: _vs_toggle_expand())
        render_runs()
        _vs_bind_wheel(vs_body)
        vs_canvas.yview_moveto(0)
        ov.after(50, _vs_sync)
        ov.after(300, _vs_sync)       # uzun satırlar kırıldıktan sonra yükseklik değişir: tekrar bak
        if ov_state_tab["tab"] == "vs":
            ov_msg.config(text=(tr("error, try again" if m.startswith("⚠") else ("reading…" if m.startswith("Reading") else ""))),
                          fg=BAD if m.startswith("⚠") else MUTED)

    root.after(1000, start_vs_watcher)        # start_tailer log yolunu bulduktan sonra
    switch_tab(ov_state_tab["tab"])
    render_runs()                             # ana penceredeki koşu geçmişi (panel Blessings'te açılsa da)

    def match_buy_to_offer(b):
        """Alımı son okunan tekliflerle eşleştir: adet/eşya eksikse tamamla,
        kartın fiyatı yanlış okunduysa gerçek fiyatla rakamları öğren."""
        offers = st.get("offers") or []
        if not offers or abs(time.time() - b["ts"]) > 600:
            return
        cand = [o for o in offers if not o["sold"] and not o["excluded"] and o["cid"]]
        hit = [o for o in cand if o["price"] == b["ds"] and (b["cid"] in (None, o["cid"]))]
        if not hit:      # fiyatı eşleşen yok: aynı eşyadan tek kart varsa onun fiyatı yanlış okunmuş demektir
            same = [o for o in cand if o["cid"] == b["cid"] and (b["qty"] in (None, o["qty"]))]
            if len(same) == 1:
                o = same[0]
                # rakamlar sadece okunamamış (emin olunamamış) karttan ve okumadan sonra yenileme olmadıysa
                # öğrenilir: yenilemeden sonra aynı eşya başka fiyatla gelince eski kartın "106"sı "105" diye
                # öğrenilebiliyordu, sonraki okumalar bozuluyordu (GitHub #7)
                fresh = st.get("refresh_t", 0) <= st.get("offers_t", 0)
                if o["price"] is None and fresh:
                    try:
                        import screen_reader as SR
                        if SR.learn_price(o.get("glyphs") or [], b["ds"]):
                            o["price"] = b["ds"]
                    except Exception:
                        pass
                hit = [o]
        if len(hit) == 1:
            o = hit[0]
            if b["cid"] is None:
                b["cid"] = o["cid"]
            if b["qty"] is None:
                b["qty"] = o["qty"]
            o["sold"] = True                      # panelden düşsün
            st["ov_sig"] = None

    def _pump_once():
        n = 0
        try:
            while n < 20000:
                k, v = q.get_nowait()
                n += 1
                if k == "status":
                    st["status"] = v + "  ·  " + data["settings"].get("log_path", "")
                elif k == "newlog":
                    eng.purchases.clear()
                    eng.refreshes.clear()
                elif k == "logstat":
                    lp = data["settings"].get("log_path", "")
                    try:
                        mt = datetime.fromtimestamp(os.path.getmtime(lp)).strftime("%H:%M:%S")
                    except OSError:
                        mt = "?"
                    st["log_nodetail"] = v.get("detail") is False
                    if st["log_nodetail"]:      # dosya okunuyor ama işe yarar satır yok: hata say
                        st["log"] = ("bad", f"✖ Log can't be used — the game isn't writing details (Enable Log is off)\n{lp}")
                    else:
                        st["log"] = ("good", f"✔ Reading log · {v['lines']:,} lines · last write {mt}\n{lp}")
                elif k == "logerr":
                    st["log_nodetail"] = False
                    st["log"] = ("bad", f"✖ {v} — use 'Select log…' to pick UE_game.log")
                elif k == "tray":
                    if v == "open":
                        root.deiconify()
                        root.lift()
                        try:
                            root.focus_force()
                        except Exception:
                            pass
                    elif v == "quit":
                        quit_app()
                        return False
                    elif v == "restart":
                        try:
                            restart_app()          # yeni süreç bu süreç kapanınca açılır
                        except Exception as e:
                            report_error("Restart", f"could not restart: {e}")
                        else:
                            quit_app()
                            return False
                    elif v == "check_update":      # elle kontrol: "bir daha gösterme" dense bile göster
                        threading.Thread(target=lambda: q.put(("update_manual", latest_release())), daemon=True).start()
                    elif v == "folder":
                        try:
                            os.makedirs(USER_DIR, exist_ok=True)
                            os.startfile(USER_DIR)
                        except Exception:
                            pass
                    elif v == "menu":
                        show_tray_menu()
                    elif v == "bug":
                        open_bug_report()
                elif k == "update_manual":
                    tray = tray_obj["t"]
                    if v is None:
                        if tray is not None:
                            tray.notify(tr("Could not check for updates. Check your internet connection."))
                    elif version_tuple(v[0]) > version_tuple(APP_VERSION):
                        data["settings"].pop("update_dismissed", None)
                        st["update_notified"] = v[0]       # pencere zaten açılıyor: ayrıca bildirim gerekmez
                        show_update(v)
                        root.deiconify()
                        root.lift()
                    elif tray is not None:
                        tray.notify(tr(f"You have the latest version (v{APP_VERSION})."))
                    continue
                elif k == "offers":
                    on_offers(v)
                elif k == "pic":
                    st["pic"] = v
                elif k == "update":
                    if v is not None:          # bağlantı yoksa mevcut durumu bozma
                        show_update(v)
                    continue
                elif k in ("update_status", "update_done", "update_fail"):
                    on_update_event(k, v)
                    continue
                elif k == "ocr_error":
                    st["pic"] = False
                    st["scanning"] = False
                    st["ocr_msg"] = f"⚠ OCR error: {v}"
                    report_error("Blessings", "OCR error", [v])
                elif k == "vs":
                    on_vs(*v)
                    continue
                elif k == "inv":
                    st["inv"] = v
                    st["inv_dirty"] = True
                    continue
                elif k == "icon":
                    st["inv_dirty"] = True
                    continue
                elif k == "items":               # güncel eşya listesi indi: yeni adlar ve simge adresleri
                    known = set(ITEM_NAMES) | set(ITEM_IMGS)
                    apply_item_extras()
                    for c in (set(ITEM_NAMES) | set(ITEM_IMGS)) - known:
                        forget_icon_miss(c)
                    st["inv_dirty"] = True
                    continue
                elif k == "tab_icon":            # Inventory value sekmesinin FE simgesi indi
                    try:
                        from PIL import Image
                        set_tab_icon(v, Image.open(TAB_FE_PATH))
                    except Exception:
                        pass
                    continue
                elif k == "pick":
                    # sadece canlı olaylar (ilk log taramasındaki eski toplamalar değil); koşu açık değilse sayılmaz
                    if abs(time.time() - v["ts"]) < 60 and vs_log.active:
                        vs_log.on_pick(v["cid"], v["n"])
                        if fe_plan():
                            render_vs()
                    continue
                elif k == "shop":
                    # sadece canlı olaylar (ilk log taramasındaki eski olaylar değil)
                    if abs(time.time() - v["ts"]) < 20:
                        st["shop_open"] = True
                        if v["why"] == "refresh":
                            st["refresh_t"] = v["ts"]      # bundan önce okunan kartlar artık ekranda değil
                    if data["settings"].get("auto_ocr", True) and abs(time.time() - v["ts"]) < 20:
                        if ov_state_tab["tab"] != "bl" or ov_state["collapsed"]:
                            switch_tab("bl")
                        ov_state["auto"] = True
                        root.after(800, lambda w=v["why"]: start_scan(w))
                elif k == "shop_close":
                    if abs(time.time() - v["ts"]) < 20:
                        st["shop_open"] = False
                        auto_collapse_later(lambda: not st.get("shop_open"))
                else:
                    if k == "buy":
                        match_buy_to_offer(v)
                    elif k == "price":
                        st["inv_dirty"] = True       # Inventory value sekmesi yeni fiyatla yeniden çizilsin
                        queue_share(v)
                    elif k == "community":           # indirilen ortak fiyatlar
                        v = clean_community(v)
                        if community_on() and v:
                            eng.community = v
                            st["inv_dirty"] = True
                            try:
                                with open(COMMUNITY_FILE, "w", encoding="utf-8") as f:
                                    json.dump({"ts": time.time(), "prices": v}, f)
                            except OSError:
                                pass
                        st["dirty"] = True
                        continue
                    eng.on_event(k, v)
                st["dirty"] = True
        except queue.Empty:
            pass
        kind, txt = st.get("log", ("muted", ""))
        head_, _, path_ = txt.partition("\n")          # durum satırı öne, dosya yolu altta sönük
        if kind == "muted" and txt.startswith("Reading log… "):
            head_, path_ = "Reading log…", txt[len("Reading log… "):]
        lbl_log.config(text=tr(head_), fg={"good": GOOD, "bad": BAD}.get(kind, MUTED))
        if path_ != path_txt["v"]:
            path_txt["v"] = path_
            show_path()
        update_loghelp(kind)
        update_dot()
        # envanter sekmesi açıksa: değişen envanter / yeni simgeler en fazla 1.5 sn'de bir çizilir
        if st.get("inv_dirty") and st.get("main_tab") == "inv" and time.time() - st.get("inv_drawn", 0) > 1.5:
            st["inv_drawn"] = time.time()
            render_inv()
        if time.time() - st.get("stale_chk", 0) > 60:     # fiyatın eskiyip eskimediği zamanla değişir
            st["stale_chk"] = time.time()
            st["dirty"] = True
        if st["dirty"]:
            st["dirty"] = False
            refresh_ui()
            render_offers()
            if time.time() - st["last_save"] > 5:
                save_data(data)
                st["last_save"] = time.time()
        return True

    seen_errors = set()

    def pump():
        """Döngü: bir olayda hata olsa bile uygulama durmasın, hata dosyaya yazılsın."""
        alive = True
        try:
            alive = _pump_once()
        except Exception:
            import traceback
            tb = traceback.format_exc()
            if tb not in seen_errors:
                seen_errors.add(tb)
                try:
                    with open(user_file("error_log.txt"), "a", encoding="utf-8") as f:
                        f.write(f"{datetime.now().isoformat()} v{APP_VERSION}\n{tb}\n")
                except Exception:
                    pass
                report_error("Program error", tb.strip().splitlines()[-1], [tb])
            st["dirty"] = True
        if alive:
            root.after(400, pump)

    start_tailer()
    check_ocr()
    root.after(3000, check_update)
    refresh_ui()
    root.after(200, pump)
    # Simge (oyunun kendi simgesi, olmazsa küp) + sistem tepsisi
    tray_obj = {"t": None}
    try:
        import tray as TR
        icon_img = TR.app_icon(data["settings"].get("log_path", ""))
        if icon_img is not None:
            try:
                from PIL import ImageTk
                st["win_icon"] = ImageTk.PhotoImage(icon_img.resize((32, 32)), master=root)
                root.iconphoto(True, st["win_icon"])
                st["menu_icon"] = ImageTk.PhotoImage(icon_img.resize((16, 16)), master=root)   # tepsi menüsünün başlığı
            except Exception:
                pass
            if sys.platform == "win32":   # bildirim başlığındaki simge
                try:
                    p = user_file("app_icon.png")
                    icon_img.resize((64, 64)).save(p)
                    TR.register_app_id(os.path.abspath(p))
                except Exception:
                    pass
            tray_obj["t"] = TR.Tray(icon_img, lambda a: q.put(("tray", a)), APP_VERSION)
    except Exception:
        pass

    def on_close():
        # X: kapanmaz; tepsi varsa gizli simgelere, yoksa görev çubuğuna iner
        if tray_obj["t"] is not None and tray_obj["t"].ok:
            root.withdraw()
        else:
            root.iconify()

    root.protocol("WM_DELETE_WINDOW", on_close)
    ov.withdraw()                                      # panel: oyun ön plana gelince görünür
    if start_hidden:                                   # Windows açılışı: doğrudan gizli simgelere
        on_close()
    root.after(500, fg_watch)
    root.mainloop()


# --------------------------------------------------------------------------- #
# Test / komut satırı
# --------------------------------------------------------------------------- #
def replay(path, tax=None):
    data = {"settings": dict(DEFAULT_SETTINGS), "prices": {}, "catalog": list(DEFAULT_CATALOG)}
    if tax is not None:
        data["settings"]["tax"] = tax
    eng = Engine(data)
    p = Parser(eng.on_event)
    with open(path, "rb") as f:
        for raw in f:
            p.feed(raw.decode("utf-8", "replace").lstrip("﻿"))
    d = eng.ref_price(DS)
    print(f"DS fiyatı: {d} FE  -> 1 FE = {1 / d:.1f} DS (piyasa), başabaş {1 / eng.ds_net():.1f} DS" if d else "DS fiyatı yok")
    print(f"Öğrenilen fiyat sayısı: {len(data['prices'])}  DS bakiye: {eng.ds_balance}")
    print("\nMAKS DS tablosu:")
    for cid, qty, pr, mx in eng.catalog_rows():
        print(f"  {item_name(cid):40s} x{qty:<4} piyasa={pr}  maks DS={None if mx is None else round(mx)}")
    print("\nAlımlar:")
    for pu in eng.purchases:
        ok, pf = eng.judge(pu["cid"], pu["qty"], pu["ds"]) if pu["cid"] else (None, None)
        print(f"  {datetime.fromtimestamp(pu['ts']).strftime('%H:%M:%S')}  {item_name(pu['cid']) if pu['cid'] else '?':36s} x{pu['qty']:<3} {pu['ds']:>4} DS  ->",
              "fiyat yok" if ok is None else ("AL " if ok else "ALMA") + f" {pf:+.2f} FE")
    print("\nÖzet:", eng.summary())


def startup_failed(tb):
    """Açılışta çökme: pythonw ile konsol yok, kullanıcı hiçbir şey görmüyordu.
    Hatayı error_log.txt'ye yaz ve Windows mesaj kutusuyla göster (tkinter bozuksa da çalışsın)."""
    last = tb.strip().splitlines()[-1] if tb.strip() else "Unknown error"
    try:
        os.makedirs(USER_DIR, exist_ok=True)
        with open(user_file("error_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} v{APP_VERSION} startup (Python {sys.version.split()[0]})\n{tb}\n")
    except Exception:
        pass
    hint = ""
    if "ModuleNotFoundError" in last or "ImportError" in last:
        hint = "A required package is missing. Run Setup.bat once, then start the app again.\n\n"
    msg = (f"TLI Blessing Helper could not start.\n\n{last}\n\n{hint}"
           f"Details were saved to userdata\\error_log.txt.\n"
           f"Please attach that file when you report the problem on GitHub.")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "TLI Blessing Helper", 0x10)
    except Exception:
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--replay":
        replay(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        try:
            if "--wait-pid" in sys.argv:               # güncellemeden sonra: eski sürüm kapansın
                try:
                    wait_for_pid(sys.argv[sys.argv.index("--wait-pid") + 1])
                except (IndexError, ValueError):
                    pass
                if "--pip" in sys.argv:
                    pip_install_requirements()
            run_gui(start_hidden="--tray" in sys.argv)
        except Exception:
            import traceback
            startup_failed(traceback.format_exc())
