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
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone

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
    "autostart": True,           # Windows açılışında gizli başlat
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
APP_VERSION = "5.7"

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


def item_name(cid):
    return ITEM_NAMES.get(str(cid)) or f"Item #{cid}"


def load_data():
    data = {"settings": dict(DEFAULT_SETTINGS), "prices": {}, "catalog": list(DEFAULT_CATALOG)}
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            saved = json.load(f)
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
                       and (c[0] == FE or c[1] <= 200)]
    for k, v in SEED_PRICES.items():          # hiç fiyatı olmayanlara başlangıç değeri
        data["prices"].setdefault(k, dict(v))
    return data


def save_data(data):
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
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
RX_PRICE = re.compile(r"\+\d+ \[([0-9.eE+-]+)\]\s*$")
RX_CUR = re.compile(r"\+currency \[(\d+)\]")
RX_FUNC = re.compile(r"CheckFuncDownloaded FuncId = (\d+)")
BLESSINGS_FUNC = "2003"     # oyunun "Blessings from Gods" özelliğinin numarası


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

    def reset(self):
        self.__init__(self.emit)

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

    def _finish_block(self, kind, syn, lines):
        if kind == "send":
            text = "\n".join(lines)
            refers = RX_REFER.findall(text)
            # Sadece basit eşya aramaları (affix/filtre olmadan)
            if len(refers) == 1 and "params" not in text and refers[0].isdigit():
                self.pending_search[syn] = refers[0]
            return
        cid = self.pending_search.pop(syn, None)
        if not cid:
            return
        prices, started, cur = [], False, None
        for l in lines:
            if "unitPrices" in l:
                started = True
            mc = RX_CUR.search(l)
            if mc:
                cur = mc.group(1)
                break
            if started:
                mp = RX_PRICE.search(l)
                if mp:
                    try:
                        prices.append(float(mp.group(1)))
                    except ValueError:
                        pass
        if prices and cur == FE:
            self.emit("price", {"cid": cid, "prices": sorted(prices), "ts": self.last_ts or time.time()})


# --------------------------------------------------------------------------- #
# Hesap motoru
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, data):
        self.data = data
        self.purchases = []
        self.refreshes = []
        self.ds_balance = None

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
            return None
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
                    try:
                        parser.feed(raw.decode("utf-8", "replace").lstrip("﻿"))
                    except Exception:
                        pass
                nlines += len(lines)
                self.q.put(("logstat", {"lines": nlines, "size": size}))
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

    if sys.platform == "win32":      # ekran yakalamada gerçek pikselleri al
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    q = queue.Queue()
    if not claim_single_instance(lambda: q.put(("tray", "open"))):
        return                                   # zaten açık: onun penceresini öne getirdik
    data = load_data()
    eng = Engine(data)
    set_autostart(bool(data["settings"].get("autostart", True)))
    st = {"tailer": None, "dirty": True, "status": "Starting…", "last_save": 0}

    root = tk.Tk()
    root.title(f"TLI Blessing Helper v{APP_VERSION}")
    root.geometry("740x880")
    root.minsize(520, 500)
    root.attributes("-topmost", False)

    BG, FG, MUTED, GOOD, BAD, ACC = "#16181d", "#e8e6e3", "#8b9099", "#3ecf8e", "#f0616d", "#f2a541"
    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=BG, foreground=FG, fieldbackground="#20232a", font=("Segoe UI", 10))
    style.configure("Treeview", background="#1d2026", foreground=FG, fieldbackground="#1d2026", rowheight=24, borderwidth=0)
    style.configure("Treeview.Heading", background="#262a32", foreground=MUTED, font=("Segoe UI", 9, "bold"))
    style.map("Treeview", background=[("selected", "#2f3540")])
    style.configure("TButton", background="#262a32", foreground=FG, padding=4)
    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.configure("TLabelframe", background=BG, foreground=MUTED)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=("Segoe UI", 9, "bold"))

    # --- Log durumu: iki sekme için ortak, en üstte ---
    logbox = tk.Frame(root, bg=BG)
    logbox.pack(fill="x", padx=12, pady=(10, 2))
    logrow = tk.Frame(logbox, bg=BG)
    logrow.pack(fill="x")
    btn_log = ttk.Button(logrow, text="📂 Select log…", command=lambda: pick_log())
    btn_log.pack(side="right", padx=(6, 0))
    lbl_log = tk.Label(logrow, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold"), anchor="w", justify="left", wraplength=440)
    lbl_log.pack(side="left", fill="x", expand=True)
    # log bulunamadı / yazılmıyor: ne yapılacağını anlat (sorun yokken gizli)
    lbl_loghelp = tk.Label(logbox, text="", bg=BG, fg=ACC, font=("Segoe UI", 9), anchor="w", justify="left", wraplength=560)

    # --- Sekmeler: DS Calculator | Void Sea Terminal ---
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background="#262a32", foreground=MUTED, padding=(12, 4))
    style.map("TNotebook.Tab", background=[("selected", "#2f3540")], foreground=[("selected", FG)])
    nb = ttk.Notebook(root)
    tab_ds = tk.Frame(nb, bg=BG)
    tab_vs = tk.Frame(nb, bg=BG)
    nb.add(tab_ds, text="DS Calculator")
    nb.add(tab_vs, text="Void Sea Terminal")
    tk.Label(tab_vs, text="Coming soon.", bg=BG, fg=MUTED, font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=12, pady=16)

    # --- Üst bilgi: DS kuru ---
    top = tk.Frame(tab_ds, bg=BG)
    top.pack(fill="x", padx=4, pady=(8, 4))
    ratebar = tk.Frame(top, bg=BG)
    ratebar.pack(fill="x")
    lbl_rate = tk.Label(ratebar, text="", bg=BG, fg=ACC, font=("Segoe UI", 15, "bold"), anchor="w")
    lbl_rate.pack(side="left")
    lbl_src = tk.Label(ratebar, text="source: Exchange searches (game log)", bg=BG, fg=MUTED,
                       font=("Segoe UI", 8), anchor="e")
    lbl_src.pack(side="right", anchor="s", pady=(0, 4))
    lbl_info = tk.Label(top, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w", justify="left")
    lbl_info.pack(fill="x")

    # --- Ekrandan okunan teklifler ---
    fr_o = ttk.LabelFrame(tab_ds, text="  OFFERS ON SCREEN  ")
    fr_o.pack(fill="x", padx=4, pady=6)
    orow = tk.Frame(fr_o, bg=BG)
    orow.pack(fill="x", padx=6, pady=(6, 0))
    btn_scan = ttk.Button(orow, text="📷 Read screen", command=lambda: start_scan("manual"))
    btn_scan.pack(side="left")
    lbl_ocr = tk.Label(orow, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w", justify="left")
    lbl_ocr.pack(side="left", padx=8, fill="x", expand=True)
    tvo = ttk.Treeview(fr_o, columns=("n", "item", "ds", "max", "res"), show="headings", height=8)
    for c, t, w, a in [("n", "#", 28, "center"), ("item", "Item", 210, "w"), ("ds", "Price DS", 70, "e"),
                       ("max", "Max DS", 70, "e"), ("res", "Verdict", 120, "e")]:
        tvo.heading(c, text=t)
        tvo.column(c, width=w, anchor=a)
    tvo.tag_configure("good", foreground=GOOD, font=("Segoe UI", 10, "bold"))
    tvo.tag_configure("bad", foreground=BAD)
    tvo.tag_configure("unk", foreground=MUTED)
    tvo.pack(fill="x", padx=6, pady=6)

    # --- DS ile alınabilen eşyalar: hangileri önerilsin (en değersizden en değerliye) ---
    fr_it = ttk.LabelFrame(tab_ds, text="  ITEMS SOLD FOR DS — click a row to recommend / hide  ")
    fr_it.pack(fill="x", padx=4, pady=6)
    tvi = ttk.Treeview(fr_it, columns=("item", "packs", "val", "rec"), show="headings", height=6)
    for c, t, w, a in [("item", "Item", 210, "w"), ("packs", "Packs", 90, "center"),
                       ("val", "Value (FE, best pack)", 140, "e"), ("rec", "Recommend", 110, "center")]:
        tvi.heading(c, text=t)
        tvi.column(c, width=w, anchor=a)
    tvi.tag_configure("on", foreground=FG)
    tvi.tag_configure("off", foreground=MUTED)
    sbi = ttk.Scrollbar(fr_it, orient="vertical", command=tvi.yview)
    tvi.configure(yscrollcommand=sbi.set)
    sbi.pack(side="right", fill="y", padx=(0, 6), pady=6)
    tvi.pack(fill="x", padx=(6, 0), pady=6)
    item_rows = {}

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
        tvi.delete(*tvi.get_children())
        item_rows.clear()
        for val, cid, qs in ds_items():
            on = cid not in norec
            iid = tvi.insert("", "end", values=(item_name(cid), ", ".join(f"×{q}" for q in qs),
                                                "—  (search on Exchange)" if val is None else fmt(val, 2),
                                                "✔ recommend" if on else "✖ hidden"),
                             tags=("on" if on else "off",))
            item_rows[iid] = cid
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
    tk.Label(arow, text="Add: name/ID", bg=BG, fg=MUTED).pack(side="left")
    ent_add = tk.Entry(arow, width=22, bg="#20232a", fg=FG, insertbackground=FG, relief="flat")
    ent_add.pack(side="left", padx=4)
    tk.Label(arow, text="qty", bg=BG, fg=MUTED).pack(side="left")
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
                lbl_add.config(text=f"{len(hits)} matches, added the first")
        if txt.isdigit() and not cid:
            cid = txt
        if not cid:
            lbl_add.config(text="not found")
            return
        entry = [cid, qty]
        if entry not in data["catalog"]:
            data["catalog"].append(entry)
            save_data(data)
        lbl_add.config(text=f"added: {item_name(cid)}")
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
    lbl_sel = tk.Label(qrow, text="(no selection)", bg=BG, fg=FG, width=26, anchor="w")
    lbl_sel.pack(side="left")
    ent = tk.Entry(qrow, width=8, bg="#20232a", fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 12))
    ent.pack(side="left", padx=6)
    tk.Label(qrow, text="DS", bg=BG, fg=MUTED).pack(side="left")
    lbl_verdict = tk.Label(qrow, text="", bg=BG, fg=FG, font=("Segoe UI", 12, "bold"))
    lbl_verdict.pack(side="left", padx=10)

    # --- Son alımlar ---
    fr_p = ttk.LabelFrame(tab_ds, text="  RECENT PURCHASES (from log)  ")
    fr_p.pack(fill="both", expand=True, padx=4, pady=6)
    tvp = ttk.Treeview(fr_p, columns=("t", "item", "ds", "res"), show="headings", height=5)
    for c, t, w, a in [("t", "Time", 60, "center"), ("item", "Item", 220, "w"),
                       ("ds", "DS", 60, "e"), ("res", "Result", 130, "e")]:
        tvp.heading(c, text=t)
        tvp.column(c, width=w, anchor=a)
    tvp.tag_configure("good", foreground=GOOD)
    tvp.tag_configure("bad", foreground=BAD)
    tvp.tag_configure("unk", foreground=MUTED)
    tvp.pack(fill="both", expand=True, padx=6, pady=6)
    lbl_sum = tk.Label(fr_p, text="", bg=BG, fg=FG, anchor="w", justify="left", font=("Segoe UI", 9))
    lbl_sum.pack(fill="x", padx=6, pady=(0, 6))

    # --- DS sekmesinin alt çubuğu: hesap ayarları ---
    bot = tk.Frame(tab_ds, bg=BG)
    bot.pack(fill="x", padx=4, pady=(2, 8), side="bottom", before=fr_o)   # her zaman görünsün
    tk.Label(bot, text="Tax %", bg=BG, fg=MUTED).pack(side="left")
    ent_tax = tk.Entry(bot, width=4, bg="#20232a", fg=FG, insertbackground=FG, relief="flat")
    ent_tax.insert(0, f"{data['settings']['tax'] * 100:g}")
    ent_tax.pack(side="left", padx=(2, 10))
    tk.Label(bot, text="Min profit %", bg=BG, fg=MUTED).pack(side="left")
    ent_mp = tk.Entry(bot, width=4, bg="#20232a", fg=FG, insertbackground=FG, relief="flat")
    ent_mp.insert(0, f"{data['settings'].get('min_profit_pct', 0):g}")
    ent_mp.pack(side="left", padx=(2, 10))
    var_top = tk.BooleanVar(value=bool(data["settings"].get("always_on_top", True)))

    def toggle_top():
        data["settings"]["always_on_top"] = var_top.get()
        root.attributes("-topmost", var_top.get())
        save_data(data)

    # Uygulama geneli alt çubuk (sekmelerin altında): açılışta başlat, ekran no, kapat
    bot2 = tk.Frame(root, bg=BG)
    bot2.pack(fill="x", padx=12, pady=(0, 10), side="bottom")
    ttk.Button(bot2, text="Quit app", command=lambda: quit_app()).pack(side="right")
    var_auto = tk.BooleanVar(value=bool(data["settings"].get("auto_ocr", True)))

    var_boot = tk.BooleanVar(value=bool(data["settings"].get("autostart", True)))

    def save_flags():
        data["settings"]["auto_ocr"] = var_auto.get()
        data["settings"]["autostart"] = var_boot.get()
        set_autostart(var_boot.get())
        save_data(data)

    ttk.Checkbutton(bot, text="Auto-read on refresh", variable=var_auto, command=save_flags).pack(side="left", padx=(8, 0))
    ttk.Checkbutton(bot2, text="Start with Windows", variable=var_boot, command=save_flags).pack(side="left")
    tk.Label(bot2, text="Screen #", bg=BG, fg=MUTED).pack(side="left", padx=(8, 0))
    ent_mon = tk.Entry(bot2, width=3, bg="#20232a", fg=FG, insertbackground=FG, relief="flat")
    ent_mon.insert(0, str(data["settings"].get("monitor", 1)))
    ent_mon.pack(side="left", padx=2)

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

    nb.pack(fill="both", expand=True, padx=8, pady=(4, 4))

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
            lbl_sel.config(text="(pick a row)")
            lbl_verdict.config(text="")
            return
        cid, qty = r
        lbl_sel.config(text=f"{item_name(cid)} ×{qty}")
        try:
            ds = float(ent.get().replace(",", "."))
        except ValueError:
            lbl_verdict.config(text="", fg=FG)
            return
        ok, profit = eng.judge(cid, qty, ds)
        if ok is None:
            lbl_verdict.config(text="No price", fg=MUTED)
        elif ok:
            lbl_verdict.config(text=f"✔ BUY  +{fmt(profit, 2)} FE", fg=GOOD)
        else:
            lbl_verdict.config(text=f"✖ SKIP  {fmt(profit, 2)} FE", fg=BAD)

    tv.bind("<<TreeviewSelect>>", lambda e: (update_verdict(), ent.focus_set()))
    ent.bind("<KeyRelease>", update_verdict)

    def refresh_ui():
        d_ref = eng.ref_price(DS)
        dnet = eng.ds_net()
        if d_ref:
            lbl_rate.config(text=f"1 FE  ≈  {fmt(1 / d_ref, 1)} DS market   →   break-even {fmt(1 / dnet, 1)} DS")
            age = data["prices"][DS]["ts"]
            lbl_info.config(text=(f"Divinity Stone: {fmt(d_ref, 5)} FE each  ·  tax {data['settings']['tax'] * 100:g}%"
                                  f"  ·  price from {datetime.fromtimestamp(age).strftime('%d.%m %H:%M')}"
                                  + (f"  ·  balance {eng.ds_balance:,} DS" if eng.ds_balance is not None else "")))
        else:
            lbl_rate.config(text="No Divinity Stone price")
            lbl_info.config(text="Open the Exchange in game and search 'Divinity Stone' — the price is learned automatically.")

        render_items()
        sel_key = selected()
        tv.delete(*tv.get_children())
        row_map.clear()
        for cid, qty, p, mx in eng.catalog_rows():
            tag = "fe" if cid == FE else ("np" if p is None else "")
            iid = tv.insert("", "end", values=(item_name(cid), qty,
                                               "—  (search on Exchange)" if p is None else fmt(p, 4),
                                               "—" if mx is None else f"{int(mx):,}"),
                            tags=(tag,))
            row_map[iid] = (cid, qty)
            if sel_key == (cid, qty):
                tv.selection_set(iid)

        tvp.delete(*tvp.get_children())
        for p in reversed(eng.purchases[-60:]):
            t = datetime.fromtimestamp(p["ts"]).strftime("%H:%M")
            if not p["cid"]:
                tvp.insert("", "end", values=(t, "?", p["ds"], "item not read"), tags=("unk",))
                continue
            ok, profit = eng.judge(p["cid"], p["qty"], p["ds"])
            res = "no price" if ok is None else (("✔ +" if ok else "✖ ") + fmt(profit, 2) + " FE")
            tvp.insert("", "end", values=(t, f"{item_name(p['cid'])} ×{p['qty'] if p['qty'] is not None else '?'}", p["ds"], res),
                       tags=("unk" if ok is None else ("good" if ok else "bad"),))
        sm = eng.summary()
        lbl_sum.config(text=(f"Purchases {sm['n_buy']} ({sm['ds_buy']:,} DS)  ·  Refreshes {sm['n_refresh']} ({sm['ds_refresh']:,} DS)\n"
                             + f"Value received {fmt(sm['value'], 1)} FE  −  DS spent (in FE) {fmt(sm['cost'], 1)} FE  =  "
                             + f"NET {fmt(sm['net'], 1)} FE"
                             + (f"   (price unknown for {sm['unknown']} purchases)" if sm["unknown"] else "")))
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
    st.update({"offers": None, "scanning": False, "ocr_ts": None, "ocr_msg": "Open or refresh Blessings — it is read automatically.",
               "scan_pending": False})

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
            st["dirty"] = True
            return
        # pencere kartların üstünü kapatmasın: yakalarken görünmez yap
        try:
            root.attributes("-alpha", 0.0)
            ov.attributes("-alpha", 0.0)
            root.update()
            time.sleep(0.06)
            img = SR.grab_screen(int(data["settings"].get("monitor", 1)))
        except Exception as e:
            st["ocr_msg"] = f"⚠ Screen capture failed: {e}"
            st["pic"] = False
            st["dirty"] = True
            return
        finally:
            root.attributes("-alpha", 1.0)
            ov.attributes("-alpha", OV_ALPHA)
        st["scanning"] = True
        st["ocr_prev"] = st["ocr_msg"] if not st["ocr_msg"].startswith("Reading") else st.get("ocr_prev", "")
        st["ocr_msg"] = "Reading…"
        st["dirty"] = True

        def work():
            try:
                offers = SR.read_offers(img, ITEM_NAMES)
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
                q.put(("offers", {"offers": offers, "ok": ok, "why": why}))
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
            for o in st["offers"]:
                name, price, mx, res, tag, _ = eval_offer(o)
                tvo.insert("", "end", values=(o["slot"], name, price, mx, res), tags=(tag,))
        lbl_ocr.config(text=st["ocr_msg"])
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
        else:
            st["pic"] = True
            new = v["offers"]
            if why == "retry" and st["offers"]:          # tekrar okumada, önceden sorunsuz okunanı koru
                old = {o["slot"]: o for o in st["offers"]}
                new = [o if (o["problem"] == "" or old.get(o["slot"], {}).get("problem", "x") != "") else old[o["slot"]]
                       for o in new]
            st["offers"] = new
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
        if why != "retry" and not retrying:
            st["retries"] = 0
        if st["scan_pending"] and not retrying:
            st["scan_pending"] = False
            root.after(300, lambda: start_scan("pending"))

    # ======================= KÜÇÜK ŞEFFAF PANEL =======================
    OV_ALPHA = 0.86
    OBG, OPANEL = "#101216", "#1a1d23"
    ov = tk.Toplevel(root)
    ov.overrideredirect(True)                  # başlık çubuğu yok
    ov.attributes("-topmost", True)
    ov.attributes("-alpha", OV_ALPHA)
    ov.configure(bg=OBG)
    ov.geometry(data["settings"].get("overlay_geom", "300x230+60+220"))
    ov_imgs = []                               # PhotoImage referansları (silinmesin)

    # Başlık: sürükleyerek taşı
    ov_head = tk.Frame(ov, bg=OPANEL)
    ov_head.pack(fill="x")
    ov_title = tk.Label(ov_head, text="  Blessings · profitable offers", bg=OPANEL, fg=MUTED,
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

    ov_state = {"collapsed": False, "full_h": None}

    def toggle_collapse():
        if not ov_state["collapsed"]:
            ov_state["full_h"] = ov.winfo_height()
            active_body().pack_forget()
            ov_tabs.pack_forget()
            ov_foot.pack_forget()
            ov.update_idletasks()
            ov.geometry(f"{ov.winfo_width()}x{ov_head.winfo_reqheight()}")
            ov_state["collapsed"] = True
            btn_min.config(text="+")
        else:
            layout_overlay()
            ov.geometry(f"{ov.winfo_width()}x{ov_state['full_h'] or 230}")
            ov_state["collapsed"] = False
            btn_min.config(text="–")

    btn_min = None
    for txt, cmd in (("✕", quit_app), ("–", toggle_collapse), ("⚙", toggle_main)):
        b = tk.Label(ov_head, text=txt, bg=OPANEL, fg=MUTED, font=("Segoe UI", 10, "bold"), cursor="hand2", padx=6)
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
        lb = tk.Label(ov_tabs, text=txt, bg=OBG, fg=MUTED, font=("Segoe UI", 9, "bold"), cursor="hand2", padx=8, pady=1)
        lb.pack(side="left", padx=(0, 4))
        lb.bind("<Button-1>", lambda e, k=key: switch_tab(k))
        tab_lbls[key] = lb

    # Liste alanı
    # Liste alanı: kaydırılabilir (fare tekerleği + ince kaydırma çubuğu)
    ov_body_outer = tk.Frame(ov, bg=OBG)
    ov_body_outer.pack(fill="both", expand=True, padx=(6, 2), pady=4)
    ov_canvas = tk.Canvas(ov_body_outer, bg=OBG, highlightthickness=0, bd=0, yscrollincrement=20)
    # ttk kaydırma çubuğu: Windows'un yerleşik çubuğu yeniden boyutlandırmada bazen kendini çizmiyordu
    style.configure("Ov.Vertical.TScrollbar", background="#6b7280", troughcolor="#1e2128", bordercolor="#1e2128",
                    lightcolor="#6b7280", darkcolor="#6b7280", arrowcolor=FG, arrowsize=11, gripcount=0)
    style.map("Ov.Vertical.TScrollbar", background=[("active", "#8b93a1")])
    ov_sb = ttk.Scrollbar(ov_body_outer, orient="vertical", command=ov_canvas.yview, style="Ov.Vertical.TScrollbar")
    ov_canvas.configure(yscrollcommand=ov_sb.set)
    ov_canvas.pack(side="left", fill="both", expand=True)
    ov_body = tk.Frame(ov_canvas, bg=OBG)
    ov_body_id = ov_canvas.create_window(0, 0, window=ov_body, anchor="nw")

    def _ov_sync(_e=None):
        ov_body.update_idletasks()          # yazılar yeniden kırıldıysa güncel yüksekliği al
        ov_canvas.configure(scrollregion=ov_canvas.bbox("all"))
        need = ov_body.winfo_reqheight() > ov_canvas.winfo_height() + 2
        packed = ov_sb.winfo_manager() == "pack"   # haritalanmayı beklese de yerleştirilmiş say
        if need and not packed:
            ov_sb.pack(side="right", fill="y")
        elif not need and packed:
            ov_sb.pack_forget()
            ov_canvas.yview_moveto(0)

    def _ov_wheel(e):
        if ov_body.winfo_reqheight() > ov_canvas.winfo_height() + 2:
            ov_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def _bind_wheel(w):
        w.bind("<MouseWheel>", _ov_wheel)
        for c in w.winfo_children():
            _bind_wheel(c)

    ov_body.bind("<Configure>", _ov_sync)
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
            return int(str(data["settings"].get("overlay_geom", "300x")).split("x")[0])
        except ValueError:
            return 300

    ov_canvas.bind("<Configure>", lambda e: (ov_canvas.itemconfig(ov_body_id, width=e.width),
                                             _rewrap(ov_body, e.width), _ov_sync(), ov.after(60, _ov_sync)))
    _bind_wheel(ov_body_outer)

    # Void Sea sekmesinin gövdesi (sekme seçilince ov_body_outer'ın yerine geçer)
    # Blessings listesi gibi kaydırılabilir: fare tekerleği + ince kaydırma çubuğu
    vs_outer = tk.Frame(ov, bg=OBG)
    # width/height=1: tuval varsayılan boyutuyla kaydırma çubuğunun yerini kapmasın
    vs_canvas = tk.Canvas(vs_outer, bg=OBG, highlightthickness=0, bd=0, yscrollincrement=20, width=1, height=1)
    vs_sb = ttk.Scrollbar(vs_outer, orient="vertical", command=vs_canvas.yview, style="Ov.Vertical.TScrollbar")
    vs_canvas.configure(yscrollcommand=vs_sb.set)
    vs_canvas.pack(side="left", fill="both", expand=True)
    vs_body = tk.Frame(vs_canvas, bg=OBG)
    vs_body_id = vs_canvas.create_window(0, 0, window=vs_body, anchor="nw")

    def _vs_sync(_e=None):
        vs_body.update_idletasks()          # yazılar yeniden kırıldıysa güncel yüksekliği al
        vs_canvas.configure(scrollregion=vs_canvas.bbox("all"))
        need = vs_body.winfo_reqheight() > vs_canvas.winfo_height() + 2
        packed = vs_sb.winfo_manager() == "pack"   # haritalanmayı beklese de yerleştirilmiş say
        if need and not packed:
            vs_sb.pack(side="right", fill="y", before=vs_canvas)
        elif not need and packed:
            vs_sb.pack_forget()
            vs_canvas.yview_moveto(0)

    def _vs_wheel(e):
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
        for w in (ov_foot, ov_tabs, ov_body_outer, vs_outer):
            w.pack_forget()
        ov_foot.pack(fill="x", side="bottom")
        ov_tabs.pack(fill="x", padx=6, pady=(4, 0))
        active_body().pack(fill="both", expand=True, padx=(6, 2), pady=4)

    def switch_tab(key):
        if ov_state.get("collapsed"):
            toggle_collapse()
        ov_state_tab["tab"] = key
        layout_overlay()
        for k, lb in tab_lbls.items():
            lb.config(bg=OPANEL if k == key else OBG, fg=FG if k == key else MUTED)
        data["settings"]["overlay_tab"] = key
        if key == "vs":
            render_vs()

    # Alt çubuk: Ekran butonu + LOG ışığı + boyutlandırma tutamacı
    ov_foot = tk.Frame(ov, bg=OPANEL)
    ov_foot.pack(fill="x", side="bottom")
    ov_btn = tk.Label(ov_foot, text=" Read ", bg="#2a2f38", fg=FG, font=("Segoe UI", 9, "bold"),
                      cursor="hand2", padx=6, pady=2)
    ov_btn.pack(side="left", padx=6, pady=4)
    ov_btn.bind("<Button-1>", lambda e: vs_scan() if ov_state_tab["tab"] == "vs" else start_scan("manual"))
    ov_grip = tk.Label(ov_foot, text="◢", bg=OPANEL, fg=MUTED, cursor="size_nw_se", font=("Segoe UI", 9))
    ov_grip.pack(side="right", padx=(2, 3))
    # Picture ışığı: ekran okuma (OCR) çalışıyor mu — yeşil: son okuma başarılı / okuyucu hazır, kırmızı: hata
    pic_dot = tk.Canvas(ov_foot, width=12, height=12, bg=OPANEL, highlightthickness=0)
    pic_dot.pack(side="right", padx=(2, 6))
    pic_dot_id = pic_dot.create_oval(1, 1, 11, 11, fill=BAD, outline="")
    tk.Label(ov_foot, text="Picture", bg=OPANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(side="right")
    ov_dot = tk.Canvas(ov_foot, width=12, height=12, bg=OPANEL, highlightthickness=0)
    ov_dot.pack(side="right", padx=(2, 6))
    ov_dot_id = ov_dot.create_oval(1, 1, 11, 11, fill=BAD, outline="")
    tk.Label(ov_foot, text="LOG", bg=OPANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(side="right")
    tk.Label(ov_foot, text=f"v{APP_VERSION}", bg=OPANEL, fg=MUTED, font=("Segoe UI", 8)).pack(side="right", padx=(0, 8))
    ov_msg = tk.Label(ov_foot, text="", bg=OPANEL, fg=MUTED, font=("Segoe UI", 8), anchor="w")
    ov_msg.pack(side="left", fill="x", expand=True)

    rs = {"x": 0, "y": 0, "w": 0, "h": 0}

    def rs_start(e):
        rs.update(x=e.x_root, y=e.y_root, w=ov.winfo_width(), h=ov.winfo_height())

    def rs_move(e):
        w = max(200, rs["w"] + e.x_root - rs["x"])
        h = max(110, rs["h"] + e.y_root - rs["y"])
        ov.geometry(f"{w}x{h}")

    ov_grip.bind("<ButtonPress-1>", rs_start)
    ov_grip.bind("<B1-Motion>", rs_move)
    ov_grip.bind("<ButtonRelease-1>", lambda e: save_ov_geom())

    def save_ov_geom():
        try:
            g = ov.geometry()
            if ov_state["collapsed"] and ov_state["full_h"]:      # kısıkken tam boyutu kaydet
                size, _, pos = g.partition("+")
                g = f"{size.split('x')[0]}x{ov_state['full_h']}+{pos}"
            data["settings"]["overlay_geom"] = g
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
        ov_title.config(text=f"  Blessings · {len(goods)} profitable" if st["offers"] else "  Blessings · profitable offers",
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
            tk.Label(ov_body, text=txt_, bg=OBG, fg=col, font=("Segoe UI", 8), anchor="w", justify="left",
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
                tk.Label(txt, text=f"#{o['slot']}  {item_name(o['cid'])} ×{o['qty']}", bg=OBG, fg=FG,
                         font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
                tk.Label(txt, text=f"{o['price']} DS   →   +{fmt(profit_of(o), 2)} FE profit", bg=OBG, fg=GOOD,
                         font=("Segoe UI", 9), anchor="w").pack(fill="x")
        else:
            if st["offers"] is None:
                msg = "Not read yet.\nOpen or refresh Blessings."
            elif eng.ref_price(DS) is None:
                msg = "No Divinity Stone price.\nSearch it once on the Exchange."
            else:
                msg = "No profitable offers."
            tk.Label(ov_body, text=msg, bg=OBG, fg=MUTED, font=("Segoe UI", 10), justify="left",
                     anchor="w").pack(fill="x", pady=6)
        _bind_wheel(ov_body)
        ov_canvas.yview_moveto(0)
        ov.after(50, _ov_sync)
        ov_msg.config(text=("error, try again" if m.startswith("⚠") else ("reading…" if m.startswith("Reading") else "")),
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
                return False                                  # bizim büyük pencere
            # oyunu programın yolundan tanı: başlıkta "Torchlight" geçen tarayıcı sekmesi
            # (ör. Reddit'teki bir rehber) oyun sanılmasın. Başlık sadece yol okunamazsa yedek.
            if exe:
                return "torchlight" in exe
            return "torchlight" in title
        except Exception:
            return True

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
        if kind == "bad":
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
            lbl_loghelp.config(text=txt)
            if txt:
                lbl_loghelp.pack(fill="x", pady=(2, 0))
            else:
                lbl_loghelp.pack_forget()

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
    VS_COL = {"arm": ACC, "won": "#5fc4d6", "wea": MUTED, "?": MUTED}
    VS_PICKED = "#c3a6ff"                    # seçilmiş fısıltılar: Armament/Wonder renklerinden ayrı, açık mor
    VS_WHY = "#ff9ad5"                       # önerinin gerekçesi: diğer renklerden ayrı, pembe

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
                q.put(("vs", ("scan", (VS.analyze(img), img))))
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
            if (not (ok and vs.complete()) and vs_st.get("retries", 0) < 2
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
                vs_st["msg"] = f"{t} read" + (f" · {vs.note}" if vs.note else "")
                if not vs.complete() or vs.note.startswith("read "):   # eksik okunan ekranı incelemek için sakla
                    try:
                        img.save(user_file("last_scan_voidsea.png"))
                    except Exception:
                        pass
                if not vs.complete():
                    vs_st["msg"] = f"⚠ {t} some cards were not read — press “Read”"
            else:
                vs_st["msg"] = f"⚠ {t} not read — press “Read” while the screen is open"
                try:
                    img.save(user_file("last_scan_voidsea.png"))
                except Exception:
                    pass
        elif ev == "error":
            st["pic"] = False
            vs_st["scanning"] = False
            vs_st["msg"] = f"⚠ OCR error: {val}"
        elif ev == "entered":
            vs.reset()
            vs_log.start(val)
            vs_st["msg"] = "Terminale girildi: " + VS.describe(val)
        elif ev == "level":
            vs_log.on_level()
            return                           # her bölge değişiminde paneli yeniden çizmeye gerek yok
        elif ev == "memory":
            vs_log.on_memory(val)
        render_vs()

    def vs_take(i):
        vs.take(i)
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
        tab_lbls["vs"].config(text=f"Void Sea · {vs.count()}/6")

        def line(text, fg, size=9, bold=False, cmd=None):
            lb = tk.Label(vs_body, text=text, bg=OBG, fg=fg, font=("Segoe UI", size, "bold" if bold else "normal"),
                          anchor="w", justify="left", wraplength=wrap, cursor="hand2" if cmd else "")
            lb.pack(fill="x", pady=1)
            if cmd:
                lb.bind("<Button-1>", lambda e: cmd())

        # Kırmızı (Ultimate) memory sayacı en üstte: log kaliteyi göstermediği için elle girilir
        cur = vs_log.current()
        n_runs, n_red, avg, n_mem, avg_mem = vs_log.stats()
        row = tk.Frame(vs_body, bg=OBG)
        row.pack(fill="x", pady=(0, 2))
        tk.Label(row, text=f"Red (last run): {cur['red'] if cur else 0}", bg=OBG, fg=BAD,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        for txt, d in (("+1", 1), ("−1", -1)):
            b = tk.Label(row, text=f" {txt} ", bg="#3a1f24" if d > 0 else "#2a2f38", fg=FG,
                         font=("Segoe UI", 9, "bold"), cursor="hand2", padx=4)
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

        # öneri sahip olunan fısıltılara göre: sağdaki panel bu okumada görülmediyse açıkça söyle
        if vs.mode in ("cards", "flame") and (vs.cards or vs.mode == "flame") and not vs.owned_seen:
            base = (f"is based on the last {len(vs.picked)} whispers read" if vs.picked
                    else "assumes you own no whispers")
            line(f"⚠ The “Seal Whispers Owned” panel on the right was not read (is it closed?). The advice {base}. "
                 "Open the panel and press “Read”.", ACC, 8)

        if len(vs.picked) >= 6:
            line("Terminal dolu (6/6).", MUTED, 10)
        elif vs.mode == "overview":
            line(f"{vs.need} more whisper(s) needed for the Terminal", FG, 11, True)
            line("Advice appears here when the selection screen opens.", MUTED, 8)
        elif vs.mode == "flame":
            # Void Sea Seal: hangi alev konmalı? Seçilmiş fısıltılara göre Rare+ fısıltının ortalama katkısı.
            fbest, out = vs.flame(data["settings"])
            if fbest in ("arm", "won"):
                line(f"▶ Put in the {VS.SIDE_LABEL[fbest]} flame", VS_COL[fbest], 12, True)
            elif fbest == "tie":
                line("Flames are almost equal", FG, 12, True)
            else:
                line("No effect on red", MUTED, 11, True)
            for side in sorted(out, key=lambda s: -out[s][0]):
                line(f"{VS.flame_label(side, vs.tiers)} flame: average {out[side][1]:+.0f}% red",
                     GOOD if side == fbest else FG, 9)
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
                     GOOD if i == best else (FG if good else MUTED), 9, cmd=lambda i=i: vs_take(i))
            line("(if your pick is not added automatically, click it)", MUTED, 7)
        if vs.picked:
            line("Advice based on these picks: " + "; ".join(VS.describe(e) for _, e in vs.picked), VS_PICKED, 9)
        why = vs.reason(data["settings"], [f"{pos[i]} {VS.SIDE_LABEL[c[0]]}" for i, c in enumerate(vs.cards)])
        if why:
            line(why, VS_WHY, 9)
        m = vs_st["msg"]
        line(m, BAD if m.startswith("⚠") else MUTED, 8)
        _vs_bind_wheel(vs_body)
        vs_canvas.yview_moveto(0)
        ov.after(50, _vs_sync)
        ov.after(300, _vs_sync)       # uzun satırlar kırıldıktan sonra yükseklik değişir: tekrar bak
        if ov_state_tab["tab"] == "vs":
            ov_msg.config(text=("error, try again" if m.startswith("⚠") else ("reading…" if m.startswith("Reading") else "")),
                          fg=BAD if m.startswith("⚠") else MUTED)

    root.after(1000, start_vs_watcher)        # start_tailer log yolunu bulduktan sonra
    switch_tab(ov_state_tab["tab"])

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
                    st["log"] = ("good", f"✔ Reading log · {v['lines']:,} lines · last write {mt}\n{lp}")
                elif k == "logerr":
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
                elif k == "offers":
                    on_offers(v)
                elif k == "pic":
                    st["pic"] = v
                elif k == "ocr_error":
                    st["pic"] = False
                    st["scanning"] = False
                    st["ocr_msg"] = f"⚠ OCR error: {v}"
                elif k == "vs":
                    on_vs(*v)
                    continue
                elif k == "shop":
                    # sadece canlı olaylar (ilk log taramasındaki eski olaylar değil)
                    if data["settings"].get("auto_ocr", True) and abs(time.time() - v["ts"]) < 20:
                        if ov_state_tab["tab"] != "bl":
                            switch_tab("bl")
                        root.after(800, lambda w=v["why"]: start_scan(w))
                else:
                    if k == "buy":
                        match_buy_to_offer(v)
                    eng.on_event(k, v)
                st["dirty"] = True
        except queue.Empty:
            pass
        kind, txt = st.get("log", ("muted", ""))
        lbl_log.config(text=txt, fg={"good": GOOD, "bad": BAD}.get(kind, MUTED))
        update_loghelp(kind)
        update_dot()
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
            st["dirty"] = True
        if alive:
            root.after(400, pump)

    start_tailer()
    check_ocr()
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
            except Exception:
                pass
            tray_obj["t"] = TR.Tray(icon_img, lambda: q.put(("tray", "open")), lambda: q.put(("tray", "quit")))
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


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--replay":
        replay(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        run_gui(start_hidden="--tray" in sys.argv)
