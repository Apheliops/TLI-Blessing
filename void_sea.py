# -*- coding: utf-8 -*-
"""
Void Sea Terminal danışmanı (TLI Blessing Helper'ın "Void Sea" sekmesi).

Void Sea Terminal ekranındaki 3 Seal Whisper kartını (her biri Armament, Wonder
ya da Wealth olabilir) ekrandan okur ve kırmızı Hero Memory beklentisini en çok
artıranı önerir. UE_game.log'u izleyerek terminal ekranı açılınca ve o ekrandaki
her tıklamadan sonra otomatik okur; terminale girildiğinde listeyi sıfırlar.

Yazı okuma: varsa Windows OCR (pip install winocr), yoksa Blessing Helper'ın
kullandığı Tesseract.

Test:
  python void_sea.py --image ekran.png
  python void_sea.py --log UE_game.log
"""
import asyncio
import math
import os
import re
import threading
import time

# --------------------------------------------------------------------------- #
# Fısıltı metinlerini etkiye çevirme
# --------------------------------------------------------------------------- #
NUM = r"(\d+(?:[.,]\d+)?)"
# "%" OCR'da "o/o", "0/0", "°/o", "96" ("20%" -> "2096") ya da "chance" önünde "00" ("+21%" -> "+2100",
# 3840×1600, GitHub #4) olarak okunabiliyor
PCT = NUM + r"\s*(?:%|o/o|0/0|[°º]/o|96(?=\s)|00(?=\s*chance))"
PCT_OPT = NUM + r"\s*(?:%|o/o|0/0|[°º]/o)?"


def _item_patterns(suf, word):
    """Memory Scrap (S) / Memory Thread (T) etkileri: Wealth (çıkma şansı, miktar, sabit ek, şanslı ek) ve
    Wonder (×2 / ×3 çarpılma şansı). Kırmızıya katkıları yok; FE planında kullanılır (tlidb metinleri).
    Çarpılma değeri: şans % × (kat − 1), ör. "%84 şansla ×3" -> 168 (beklenen ek yüzde)."""
    # kartın üstündeki parıltı animasyonu harf bozabiliyor: "Memor+ Scraps", "Memoro Scraps", "aropped"
    item = r"memor\S?\s*" + word
    return [
        ("mult" + suf, re.compile(PCT + r"\s*chance\s*for\s*" + item + r"\s*\S{0,2}ropped[^%]*?"
                                  r"(?:multiplied\s*by\s*" + NUM + r"|doubled)", re.I),
         lambda m: _f(m.group(1)) * ((_f(m.group(2)) if m.group(2) else 2.0) - 1)),
        # "%" hiç okunamazsa ("+21 chance for Memory Threads dropped … multiplied by 2", 3840×1600, #4)
        ("mult" + suf, re.compile(NUM + r"\s*chance\s*for\s*" + item + r"\s*\S{0,2}ropped[^%]*?"
                                  r"(?:multiplied\s*by\s*" + NUM + r"|doubled)", re.I),
         lambda m: _f(m.group(1)) * ((_f(m.group(2)) if m.group(2) else 2.0) - 1)),
        ("app" + suf, re.compile(PCT + r"\s*chance\s*for\s*" + item + r"\s*to\s*appear", re.I), lambda m: _f(m.group(1))),
        ("qty" + suf, re.compile(PCT + r"\s*additional\s*" + item + r"\s*drop\s*quantit", re.I), lambda m: _f(m.group(1))),
        ("luck" + suf, re.compile(PCT + r"\s*chance\s*for\s*" + NUM + r"\s*additional\s*" + item, re.I),
         lambda m: _f(m.group(1)) / 100 * _f(m.group(2))),
        # kalın beyaz "20%" okunamazsa ("chance for 49 additional Memory Scrap(s)"): şans her kademede %20
        ("luck" + suf, re.compile(r"chance\s*for\s*" + NUM + r"\s*additional\s*" + item, re.I),
         lambda m: 0.2 * _f(m.group(1))),
        ("flat" + suf, re.compile(NUM + r"\s*additional\s*" + item, re.I), lambda m: _f(m.group(1))),
    ]


# Sıra önemli: daha özel desenler önce.
EFFECT_PATTERNS = [
    ("luck",   re.compile(PCT + r"\s*chance\s*for\s*" + NUM + r"\s*additional\s*hero\s*memor", re.I),
               lambda m: _f(m.group(1)) / 100 * _f(m.group(2))),
    # kalın beyaz "20%" bazen hiç okunmuyor ("chance for 12 additional Hero…"); bu etkinin şansı her kademede %20
    ("luck",   re.compile(r"chance\s*for\s*" + NUM + r"\s*(?:\S\s+)?additional\s*hero\s*memor", re.I),
               lambda m: 0.2 * _f(m.group(1))),
    ("appear", re.compile(PCT + r"\s*chance\s*for\s*hero\s*memor\w*\s*to\s*appear", re.I), lambda m: _f(m.group(1))),
    ("memQty", re.compile(PCT + r"\s*additional\s*hero\s*memory\s*drop\s*quantity", re.I), lambda m: _f(m.group(1))),
    ("flat",   re.compile(NUM + r"\s*additional\s*hero\s*memor\w*(?:\s*\(\w*\))?\s*drop", re.I), lambda m: _f(m.group(1))),
    ("rep",    re.compile(PCT + r"\s*chance\s*for\s*void\s*sea\s*treasure\s*drops?\s*to\s*be\s*replicated\s*" + NUM, re.I),
               lambda m: _f(m.group(1)) / 100 * _f(m.group(2))),
    ("baseQ",  re.compile(PCT + r"[^%]*?base\s*drop\s*quantity", re.I), lambda m: _f(m.group(1))),
    # "Drop Rarity" oyunda camgöbeği bağlantı yazısı: "DropRarity" / "Drop-Rarity" diye bitişik okunabiliyor
    # bu ikisi her zaman yüzde: "%" okunamasa da ("+25 Void Sea Treasure DropRarity") kabul et
    # "Dro Rarit" gibi harf eksik okunuşlar da kabul
    ("rarity", re.compile(PCT_OPT + r"\s*void\s*sea\s*treasure[\W_]*dro\w*[\W_]*rarit\w*", re.I), lambda m: _f(m.group(1))),
    ("tQty",   re.compile(PCT_OPT + r"\s*void\s*sea\s*treasure[\W_]*dro\w*[\W_]*quantit\w*", re.I), lambda m: _f(m.group(1))),
    *[pat for suf, word in (("S", r"scraps?"), ("T", r"threads?")) for pat in _item_patterns(suf, word)],
    ("none",   re.compile(r"memor\S?\s*(?:scrap|thread)", re.I), lambda m: 0.0),
    # Boss Whisper (#9): "40% chance for 1 additional Void Sea Invitation(s) to drop from Void Sea Treasures"
    ("luckI",  re.compile(PCT + r"\s*chance\s*for\s*" + NUM + r"\s*additional\s*(?:void\s*sea\s*)?invit", re.I),
               lambda m: _f(m.group(1)) / 100 * _f(m.group(2))),
    # sayılar okunamadıysa: kart Boss, değeri bilinmiyor (describe "not counted" yazar)
    ("none",   re.compile(r"invit\w{3,}", re.I), lambda m: 0.0),
]
# Çince istemci (v6.1; Reddit'ten bir oyuncunun 2560×1440 ekran görüntülerinden). Metin boşluksuz eşleşir:
# Çince OCR karakterlerin arasına boşluk koyabiliyor. 英雄追忆 = Hero Memory, 追忆碎絮 = Memory Scrap,
# 追忆游丝 = Memory Thread, 虚海请柬 = Void Sea Invitation (首领秘语 / Boss fısıltısının ödülü).
# (✓) ekranda görüldü; (tlidb) tlidb.com/cn fısıltı listesinden; (?) İngilizcesinden tahmin.
CN_NUM = r"(\d+(?:\.\d+)?)"
# Windows'un Çince OCR'ı bazı karakterleri karıştırıyor (gerçek ekranlarda görüldü): 额外 -> "颜外" ya da "外",
# 概率 -> "溉率", 英雄 -> "英雜", kalın beyaz "%" düşebiliyor ("…额外" + ayrı satırda "32").
_EW = r"[额颜]?外"           # 额外
_GL = r"[概溉]?率"           # 概率
_HM = r"英.?追忆"            # 英雄追忆
CN_EFFECT_PATTERNS = [
    # (✓) 虚海宝藏有 20% 概率额外掉落 17 枚英雄追忆
    ("luck",   re.compile(CN_NUM + "%" + _GL + _EW + "掉落" + CN_NUM + r"\D{0,2}" + _HM),
               lambda m: _f(m.group(1)) / 100 * _f(m.group(2))),
    # (✓) 虚海宝藏基础掉落中，掉落英雄追忆的概率提升 112%
    ("appear", re.compile("掉落" + _HM + "的" + _GL + r"提升\+?" + CN_NUM + "%?"), lambda m: _f(m.group(1))),
    # (✓) 虚海宝藏基础掉落中，英雄追忆的掉落数量额外 +32%
    ("memQty", re.compile(_HM + "的掉落数量" + _EW + r"\+?" + CN_NUM + "%?"), lambda m: _f(m.group(1))),
    # (✓) 虚海宝藏额外掉落 16 枚英雄追忆
    ("flat",   re.compile(_EW + "掉落" + CN_NUM + r"\D{0,2}" + _HM), lambda m: _f(m.group(1))),
    # (?) kopyalanma: "…有 3% 概率…复制 …"
    ("rep",    re.compile(CN_NUM + "%" + _GL + r"\D{0,12}复制\D{0,3}" + CN_NUM),
               lambda m: _f(m.group(1)) / 100 * _f(m.group(2))),
    # (tlidb) Drop Rarity: "虚海宝藏的 掉落稀有度+25%" (稀有 tek başına kalite adı; "稀有度" gerekli)
    ("rarity", re.compile(r"稀有度\D{0,4}?\+?" + CN_NUM + "%"), lambda m: _f(m.group(1))),
    # (tlidb) base drop quantity: "虚海宝藏的基础掉落数量 +9%" (英雄追忆/追忆游丝 "的掉落数量" ile karışmaz)
    ("baseQ",  re.compile(r"基础掉落数量\D{0,3}?\+?" + CN_NUM + "%"), lambda m: _f(m.group(1))),
    # (tlidb) Treasure Drop Quantity: "虚海宝藏的掉落数量 +14%"
    ("tQty",   re.compile(r"虚海宝藏(?:的)?掉落数量\D{0,3}?\+?" + CN_NUM + "%"), lambda m: _f(m.group(1))),
    # (?) 首领秘语: İngilizcesinden ve 英雄追忆 satırından tahmin ("…20% 概率额外掉落 1 张虚海请柬")
    ("luckI",  re.compile(CN_NUM + "%" + _GL + _EW + "掉落" + CN_NUM + r"\D{0,2}虚海请柬"),
               lambda m: _f(m.group(1)) / 100 * _f(m.group(2))),
    # (✓) kırmızıya katkısı yok: 追忆碎絮 (Scrap), 追忆游丝 (Thread), 虚海请柬 (Invitation)
    ("none",   re.compile(r"追忆碎絮|追忆游丝|请柬"), lambda m: 0.0),
]
CJK_RX = re.compile(r"[一-鿿]")


def squash_cn(text):
    """Çince satır: boşlukları ve tam genişlikli işaretleri ayıkla ("２０％" -> "20%", "，" -> ",")."""
    text = text.translate(str.maketrans("０１２３４５６７８９％＋，．：", "0123456789%+,.:"))
    return re.sub(r"\s+", "", text)


EFFECT_NAMES = {
    "appear": "Appear chance", "memQty": "Memory quantity", "flat": "Flat extra",
    "luck": "Lucky extra", "baseQ": "Base quantity", "tQty": "Treasure quantity",
    "rarity": "Rarity", "rep": "Replication", "none": "No effect on red",
    "appS": "Scrap appear", "qtyS": "Scrap quantity", "flatS": "Flat Scrap", "luckS": "Lucky Scrap",
    "multS": "Scrap multiply",
    "appT": "Thread appear", "qtyT": "Thread quantity", "flatT": "Flat Thread", "luckT": "Lucky Thread",
    "multT": "Thread multiply", "luckI": "Lucky Invitation",
}
ARM_ONLY = {"appear", "memQty", "flat", "luck"}
WON_ONLY = {"rarity", "tQty", "multS", "multT"}
WEA_ONLY = {"appS", "qtyS", "flatS", "luckS", "appT", "qtyT", "flatT", "luckT"}
SIDE_LABEL = {"arm": "Armament", "won": "Wonder", "wea": "Wealth", "boss": "Boss", "?": "?", "other": "Other"}
POS_NAME = {1: ["middle"], 2: ["left", "right"], 3: ["left", "middle", "right"]}

# Doğrulanmış değer değil, dengeli tahmin. Taban 3 / üs 1 sabit ek memory'yi aşırı değerli gösterip
# neredeyse her zaman Armament öneriyordu; kırmızı en üst kalite olduğu için rarity'nin etkisi
# doğrusaldan güçlü varsayıldı. Koşu kaydı biriktikçe gerçek veriyle ayarlanmalı.
DEFAULTS = {"vs_base_mem": 10.0,  # fısıltısız bir koşuda beklenen Hero Memory sayısı
            "vs_p0": 5.0,         # fısıltısız kırmızı oranı (%)
            "vs_k": 1.0}          # rarity'nin kırmızıya etkisinin üssü (v4.8: 2 -> 1, Blacksail rehberine göre
                                  # Rarity "isteğe bağlı"; Çıkma şansı ve ek memory temel)


def _f(s):
    return float(s.replace(",", "."))


# Koşu kaydından öğrenilen model (RunLog.refit günceller); yeterli koşu yoksa None ya da active=False
LEARNED = None


def model_settings(settings):
    s = {k: float(settings.get(k, v)) for k, v in DEFAULTS.items()}
    if LEARNED and LEARNED.get("active"):
        s["fit"] = LEARNED
    return s


def clean(text):
    text = re.sub(r"</?[a-z]+(?:\s[^>]*)?>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_effects(text):
    """Bir metinden bulunan etkiler: [(anahtar, değer, kaynak metin)]."""
    text = clean(text)
    patterns = EFFECT_PATTERNS
    if CJK_RX.search(text):                    # Çince istemci
        text, patterns = squash_cn(text), CN_EFFECT_PATTERNS
    found, used = [], []
    for key, rx, val in patterns:
        for m in rx.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in used):
                continue
            used.append((m.start(), m.end()))
            found.append((key, val(m), m.group(0)))
    return found


def describe(effs, fe=False):
    """fe=True (FE planı): Scrap / Thread yazısı bulunup sayısı okunamayan etki "kırmızıya etkisiz" değil
    "sayı okunamadı" diye gösterilir."""
    if not effs:
        return "not read"

    def name(k, v, *src):
        if k == "none" and src and re.search(r"invit|请柬", src[0], re.I):
            return "Void Sea Invitation (not counted)"
        return "Scrap / Thread (number not read)" if fe and k == "none" else f"{EFFECT_NAMES[k]} {v:g}"
    return ", ".join(name(*e) for e in effs)


def sig(effs):
    return tuple(sorted((k, round(v, 2)) for k, v, *_ in effs or []))


# --------------------------------------------------------------------------- #
# Hesap modeli
#   memory  = taban × (1+taban miktar) × (1+treasure miktarı) × (1+çıkma şansı) × (1+memory miktarı)
#             + sabit ek + şanslı ek
#   kırmızı = (taban düşüşlerdeki memory × min(1, taban oran × (1+rarity)^üs)
#              + ek memory'ler (sabit + şanslı) × taban oran) × (1 + kopyalanma)
#   Rarity sadece taban (base) düşüşleri etkiler, ek memory'leri etkilemez
#   (r/TorchlightInfinite "Torchlight: Infinite – Blacksail Guide").
# Aynı türden bonuslar toplanır, farklı türler çarpılır.
# --------------------------------------------------------------------------- #
def totals(effects):
    t = dict(appear=0.0, memQty=0.0, flat=0.0, luck=0.0, baseQ=0.0, tQty=0.0, rarity=0.0, rep=0.0)
    for key, value, *_ in effects:
        if key in t:
            t[key] += value
    return t


# Öğrenilen modelde her etkinin memory'ye etkisi bir katsayıyla ölçeklenir:
#   memory = taban × Π (1+etki)^c × (1 + sabit/(taban×Π))^c_sabit
# Katsayılar 1 iken yukarıdaki elle kurulan modelin aynısıdır.
# Rarity burada yok: memory sayısını değiştirmez, sadece taban düşüşlerin kırmızı oranını artırır
# (v5.9'a kadar sayıya da katsayı öğreniliyordu; az veride sahte etki çıkıp öneriyi Rarity'ye kaydırıyordu).
FIT_KEYS = ["baseQ", "tQty", "appear", "memQty", "flat"]
FIT_PRIOR = {"baseQ": 1.0, "tQty": 1.0, "appear": 1.0, "memQty": 1.0, "flat": 1.0}


def _features(t, base):
    nominal = (1 + t["baseQ"] / 100) * (1 + t["tQty"] / 100) * (1 + t["appear"] / 100) * (1 + t["memQty"] / 100)
    x = {k: math.log(1 + t[k] / 100) for k in ("baseQ", "tQty", "appear", "memQty")}
    x["flat"] = math.log(1 + (t["flat"] + t["luck"]) / (base * nominal))
    return x


def breakdown(effects, s):
    """-> (beklenen memory, kırmızı oranı (tüm memory'ler içinde), kopyalanma).
    Rarity sadece taban düşüşlerdeki memory'lerin kırmızı oranını artırır; ek memory'ler taban oranda kalır."""
    t = totals(effects)
    fit = s.get("fit")
    if fit:
        x = _features(t, s["vs_base_mem"])
        # etkileşimler: sabit ek içermeyenler taban düşüşleri (rarity'nin etkilediği kısım), içerenler toplamı değiştirir
        ib = ia = 0.0
        for pair, d in (fit.get("inter") or {}).items():
            i, j = pair.split("*")
            if i in x and j in x:
                if "flat" in (i, j):
                    ia += d * x[i] * x[j]
                else:
                    ib += d * x[i] * x[j]
        base = s["vs_base_mem"] * math.exp(sum(fit["c"][k] * x[k] for k in FIT_KEYS if k != "flat") + ib)
        mem = base * math.exp(fit["c"]["flat"] * x["flat"] + ia)
        p0, k = fit["p0"], fit["k"]
    else:
        base = (s["vs_base_mem"] * (1 + t["baseQ"] / 100) * (1 + t["tQty"] / 100)
                * (1 + t["appear"] / 100) * (1 + t["memQty"] / 100))
        mem = base + t["flat"] + t["luck"]
        p0, k = s["vs_p0"] / 100, s["vs_k"]
    p_base = min(1.0, p0 * (1 + t["rarity"] / 100) ** k)
    p = (base * p_base + (mem - base) * p0) / mem if mem > 0 else p_base
    return mem, p, t["rep"]


def red_score(effects, s):
    mem, p, rep = breakdown(effects, s)
    return mem * p * (1 + rep)


def _change(before, effs, s):
    """Bir fısıltının yaptığı değişiklik, okunur metin olarak."""
    m0, p0, r0 = breakdown(before, s)
    m1, p1, r1 = breakdown(before + effs, s)
    parts = []
    if m1 - m0 > 0.05:
        parts.append(f"memory {m0:.1f}→{m1:.1f}")
    if p1 - p0 > 1e-6:
        parts.append(f"red rate {p0 * 100:.1f}%→{p1 * 100:.1f}%")
    if r1 - r0 > 1e-6:
        parts.append("replication")
    return ", ".join(parts) or "no effect on red"


def _stacked(before, effs):
    """Kartın etkisi seçilenlerde zaten varsa adı (aynı tür toplandığı için katkısı azalır)."""
    have = {k for k, *_ in before}
    for k, *_ in effs:
        if k in have and k in ("appear", "memQty", "baseQ", "tQty"):
            return EFFECT_NAMES[k]
    return None


def reason_cards(picked, cards, best, gains, labels, s):
    """Önerinin gerekçesi: en iyi kart neyi değiştiriyor, ikincisi neden geride."""
    if not cards:
        return ""
    if best is None:
        return "Why: none of the cards raises the memory count or the red rate."
    order = sorted(range(len(gains)), key=lambda i: -gains[i][0])
    if best == "tie":
        a, b = order[0], order[1]
        return (f"Why: {labels[a]} ({gains[a][1]:+.0f}%) and {labels[b]} ({gains[b][1]:+.0f}%) "
                f"differ by less than 3%; either is fine.")
    txt = f"Why: {labels[best]} → {_change(picked, cards[best][1], s)}; expected red {gains[best][1]:+.0f}%."
    for o in order[1:]:          # önerilmeyen kartların hepsi (v6.9.1'den önce sadece ikincisi)
        txt += f" {labels[o]} only {gains[o][1]:+.0f}% ({_change(picked, cards[o][1], s)})."
    if len(order) > 1:
        st = _stacked(picked, cards[order[1]][1])
        if st and not _stacked(picked, cards[best][1]):
            txt += f" You already have {st}: bonuses of the same type add up, different types multiply."
    return txt


def flame_label(side, tiers):
    """"Armament (mor, Magic+)" gibi; renk bilinmiyorsa sadece tür."""
    t = (tiers or {}).get(side)
    if t is None:
        return SIDE_LABEL[side]
    return f"{SIDE_LABEL[side]} ({FLAME_COLOR_TR[TIER_COLOR[t]]}, {TIER_NAMES[t]}+)"


def reason_flame(picked, out, best, s, tiers=None):
    tiers = tiers or {}
    m0, p0, _ = breakdown(picked, s)
    if best is None:
        return "Why: neither flame raises the expected red."
    av = {}
    for side, idx in (("arm", 0), ("won", 1)):
        vals = flame_values(FLAME_POOL[side], tiers.get(side, DEFAULT_TIER))
        av[side] = sum(breakdown(picked + [e], s)[idx] for e in vals) / len(vals)
    txt = (f"Why: {flame_label('arm', tiers)} raises the memory count (avg. {m0:.1f}→{av['arm']:.1f}); "
           f"{flame_label('won', tiers)} raises the red rate with Rarity "
           f"(avg. {p0 * 100:.1f}%→{av['won'] * 100:.1f}%) but 2 of its 5 kinds (Scrap/Thread ×2) do nothing for red.")
    close = abs(out["arm"][0] - out["won"][0]) <= max(out["arm"][0], out["won"][0]) * 0.03
    if best == "tie":
        txt += " Their averages are almost the same."
    elif close:
        txt += f" The gains are almost equal; picked the higher-quality {SIDE_LABEL[best]} flame."
    return txt


def recommend(picked, cards, s, score=None):
    """-> (en iyi kartın sırası | "tie" | None, [(mutlak artış, yüzde artış)] kart sırasıyla)
    score: kırmızı planında red_score, FE planında fe_score."""
    score = score or red_score
    base = score(picked, s)
    gains = []
    for _side, effs, _x in cards:
        g = score(picked + effs, s) - base
        gains.append((g, g / base * 100 if base > 0 else 0.0))
    if not gains:
        return None, gains
    order = sorted(range(len(gains)), key=lambda i: gains[i][0], reverse=True)
    top = gains[order[0]][0]
    if top <= 1e-9:
        return None, gains
    if len(order) > 1 and top - gains[order[1]][0] <= top * 0.03:
        return "tie", gains
    return order[0], gains


# --------------------------------------------------------------------------- #
# Ekran okuma
# --------------------------------------------------------------------------- #
HEAD_RX = re.compile(r"^\W*(?:whisper\s*of\s*(armament|wonder|wealth)|boss\s*whisper)\W*$", re.I)   # kart başlığı tek başına durur
FLAME_RX = re.compile(r"put\s*in\s*void\s*sea|void\s*sea\s*flames|请放入虚海|虚海夜火", re.I)
# "After defeating Pirates, a Whisper of X of Rare quality…" — seçili alev (OCR "Whisperof_Wonder" gibi okuyabilir)
SEL_RX = re.compile(r"whisper[\W_]*of[\W_]*(arm\w*|won\w*|wealth)|(boss)[\W_]*whisper", re.I)
SIDE_OF = {"armament": "arm", "wonder": "won", "wealth": "wea"}
OWNED_RX = re.compile(r"(\d)\s*/\s*6\s*$")   # "4 / 6" ("14 / 6" okunsa da son rakam)
# Çince: kart başlıkları 军械秘语 (Armament) / 奇珍秘语 (Wonder) / 财富秘语 (Wealth) / 首领秘语 (Boss: İngilizcede
# "Boss Whisper", #9; ödülü 虚海请柬 / Void Sea Invitation, kırmızıya katkısı yok). Alev yazısı: "击败虚海之子后，可选的封印秘语必定
# 出现一个 奇珍秘语，且品质至少为 稀有" (mavi alevde kalite kısmı yok). tlidb.com/cn/Void_Sea_Flame'deki
# diğer biçimler: "…中至少有2个为 X秘语", "…中至少有一个优质的 X秘语", "击败虚海之子后，X秘语的品质至少为 魔法",
# "…且品质为传奇". Kaliteler: 普通 < 魔法 < 稀有 < 传奇 (İngilizcede Normal < Magic < Rare < Epic).
CN_SIDE = {"军械": "arm", "奇珍": "won", "财富": "wea", "首领": "boss"}
CN_HEAD_RX = re.compile(r"^\W*(军械|奇珍|财富|首领)秘语\W*$")
CN_SEL_RX = re.compile(r"(?:出现一个|有2个为|有一个(?:优质|珍贵)的|击败虚海之子后,?)(军械|奇珍|财富|首领)秘语")
CN_QUAL = {"普通": 0, "魔法": 1, "稀有": 2, "传奇": 3, "史诗": 3}
CN_QUAL_RX = re.compile(r"品质(?:至少)?为(普通|魔法|稀有|传奇|史诗)")


def head_side(text):
    """Kart başlığı satırıysa türü ("arm"/"won"/"wea"/"boss"), değilse None."""
    m = HEAD_RX.search(text)
    if m:
        return SIDE_OF[m.group(1).lower()] if m.group(1) else "boss"
    s = squash_cn(text)
    m = CN_HEAD_RX.search(s)
    if m:
        return CN_SIDE[m.group(1)]
    # OCR başlığı bozuk okuyabiliyor ("军秘语`", "一丿〕奇珍秘语", "，《财富秘语"): kısa, rakamsız, "秘语" ile
    # biten satırda tür karakterlerinden birine bak
    if s.rstrip("`'\".,、;:!?)）》〕0").endswith("秘语") and len(s) <= 9 and not re.search(r"\d|封印", s):
        return _cn_side_chars(s)
    return None


def _cn_side_chars(s):
    for chars, side in (("军械槭", "arm"), ("奇珍", "won"), ("财富", "wea"), ("首领", "boss")):
        if any(c in s for c in chars):
            return side
    return None


def grab(monitor_index=1):
    """Oyun penceresini yakalar (pencere modunda sadece o alan); bulunamazsa seçili monitörün tamamını."""
    from screen_reader import grab_screen
    return grab_screen(monitor_index)


# Oyunun dili ekrandan anlaşılır: önce son işe yarayan dil denenir, ekranda bir şey bulunamazsa diğeri.
# Çince için Windows'ta "zh-Hans" OCR dil paketi kurulu olmalı (Çince Windows'ta hazır gelir).
OCR_LANG = "en"
_ZH_TAG = []


def zh_ocr_tag():
    """Kurulu Çince (Basitleştirilmiş) Windows OCR dilinin etiketi ya da None."""
    if not _ZH_TAG:
        tag = None
        try:
            from winrt.windows.media.ocr import OcrEngine
            tags = [l.language_tag for l in OcrEngine.available_recognizer_languages]
            tag = next((t for t in tags if t.lower().startswith(("zh-hans", "zh-cn"))), None)
        except Exception:
            pass
        _ZH_TAG.append(tag)
    return _ZH_TAG[0]


async def _await(op):
    # winocr bir WinRT IAsyncOperation döndürüyor; asyncio.run bunu yalnız Python 3.14'te kabul ediyor,
    # eski Python'larda "a coroutine was expected" hatası veriyordu. Coroutine içine sarınca hepsinde çalışır.
    return await op


def _ocr_winocr(img):
    import winocr
    res = asyncio.run(_await(winocr.recognize_pil(img, OCR_LANG)))
    out = []
    for line in res.lines:
        rs = [w.bounding_rect for w in line.words]
        x0 = min(r.x for r in rs); x1 = max(r.x + r.width for r in rs)
        out.append((line.text, (x0 + x1) / 2, min(r.y for r in rs)))
    return out


def _ocr_tesseract(img):
    import pytesseract
    d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 11")
    lines = {}
    for i, word in enumerate(d["text"]):
        if not word.strip():
            continue
        # psm 11 her kelimeyi ayrı bulur: aynı satırdaki yakın kelimeleri birleştir
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append((d["left"][i], d["top"][i], d["width"][i], word))
    out = []
    for words in lines.values():
        words.sort()
        x0 = words[0][0]; x1 = max(w[0] + w[2] for w in words)
        out.append((" ".join(w[3] for w in words), (x0 + x1) / 2, min(w[1] for w in words)))
    return out


def ocr_lines(img):
    """[(metin, x_merkez, y_üst)] — küçük yazılar için görüntü büyütülür."""
    from PIL import ImageOps
    scale = 2 if img.width < 2200 else 1
    big = img.resize((img.width * scale, img.height * scale)) if scale > 1 else img
    big = ImageOps.autocontrast(big.convert("L")).convert("RGB")
    try:
        raw = _ocr_winocr(big)
    except ImportError:
        raw = _ocr_tesseract(big)
    return [(t, x / scale, y / scale) for t, x, y in raw]


def read_screen(lines, width):
    """Ekranda yan yana 3 kart (her biri herhangi bir türden) ve sağda
    'Seal Whispers Owned' paneli vardır. Kart başlığı 'Whisper of X',
    etki metni hemen altındadır.
    -> (kartlar [(tür, etkiler, x)], sahip olunan etkiler, sahip olunan sayı | None)"""
    panel_x = width * 0.80
    main = [l for l in lines if l[1] < panel_x]
    panel = [l for l in lines if l[1] >= panel_x]

    heads = sorted(((head_side(t), x, y) for t, x, y in main if head_side(t)), key=lambda h: h[1])
    texts = [[] for _ in heads]
    for t, x, y in main:
        if head_side(t):
            continue
        above = [i for i, h in enumerate(heads) if h[2] < y]
        if not above:
            continue
        i = min(above, key=lambda i: abs(heads[i][1] - x))
        if y - heads[i][2] < 200:  # başlığın hemen altındaki metin; Confirm vb. daha aşağıda
            texts[i].append(t)
    cards = [(h[0], parse_effects(" ".join(tx)), h[1]) for h, tx in zip(heads, texts)]

    # Başlık yoksa kart yok. Eskiden ekrandaki etkiler türünden ayrılıp kart sayılıyordu: boss savaşında soldaki aktif
    # etki listesi sahte kart oluyordu (başlıkta da "Void Sea Terminal" yazdığı için). Gerçek kartları read_cards_color okur.
    owned_n = None
    for t, *_ in panel:
        m = OWNED_RX.search(t)
        if m:
            owned_n = int(m.group(1))
    owned = parse_effects(" ".join(t for t, *_ in panel if not OWNED_RX.search(t)))
    if owned_n is None and owned:  # soluk "n/6" sayacı okunamadıysa paneldeki fısıltıları say
        owned_n = len(owned)
    return cards, owned, owned_n


def read_owned(img):
    """Sağdaki 'Seal Whispers Owned' panelini ayrıca kırpıp büyüterek okur.
    Satır başındaki renkli elmas ikonları OCR'da tek harf ("A") gibi çıkar; onlar atılır.
    -> (etkiler, sayı | None)"""
    from PIL import ImageOps
    W, H = img.size
    crop = img.crop((int(W * 0.80), 0, W, H))
    s = (2 if crop.width < 1000 else 1) * max(1.0, 1440 / H)     # düşük çözünürlükte daha çok büyüt
    big = crop.resize((int(crop.width * s), int(crop.height * s)))
    big = ImageOps.autocontrast(big.convert("L")).convert("RGB")
    lines = [t for t, *_ in _ocr_any(big)]
    owned_n = None
    for t in lines:
        m = OWNED_RX.search(t)
        if m:
            owned_n = int(m.group(1))
    text = " ".join(t for t in lines if not OWNED_RX.search(t))
    if not CJK_RX.search(text):          # Çincede tek karakter = tek kelime olabilir: o zaman dokunma
        text = re.sub(r"(?<!\S)[^\d\s+%](?!\S)", " ", text)   # tek karakterlik ikon çöpleri
    return parse_effects(text), owned_n


def _side_of_word(word):
    if word in CN_SIDE:
        return CN_SIDE[word]
    w = word.lower()
    if w == "boss":
        return "boss"
    return "arm" if w.startswith("arm") else "won" if w.startswith("won") else "wea"


QUAL_RX = re.compile(r"\b(normal|magic|rare|epic)\s*quality", re.I)


def read_selected_tier(lines, width):
    """Seçili alevin garanti ettiği en düşük kademe ("of Magic quality or higher")."""
    main = " ".join(t for t, x, y in lines if x < width * 0.80)
    m = QUAL_RX.search(main)
    if m:
        return TIER_NAMES.index(m.group(1).capitalize())
    cn = squash_cn(main)
    m = CN_QUAL_RX.search(cn)
    if m:
        return CN_QUAL[m.group(1)]
    if CN_SEL_RX.search(cn):                 # Çince mavi alev: tür garanti, kalite yazmıyor
        return 0
    # mavi alev: "a Whisper of Armament is guaranteed…" — kalite yazmaz, sadece tür garanti
    if re.search(r"(?:whisper[\W_]*of[\W_]*\w+|boss[\W_]*whisper)[\W_]*is[\W_]*guarant", main, re.I):
        return 0
    return None


def read_selected_flame(img, lines):
    """Void Sea Seal ekranında seçili alevin türü. Renkli (camgöbeği) yazı gri tonlamada
    kaybolabildiği için bulunamazsa o satır renkli ve büyütülmüş olarak yeniden okunur."""
    main = " ".join(t for t, x, y in lines if x < img.width * 0.80)
    m = SEL_RX.search(main) or CN_SEL_RX.search(squash_cn(main))
    if m:
        return _side_of_word(next(g for g in m.groups() if g))
    # fısıltı garanti etmeyen alev: "100% chance for Void Pirates to drop 1 additional Embers"
    if re.search(r"pirates\s*to\s*drop|additional\s*embers", main, re.I):
        return "other"
    after = [(x, y) for t, x, y in lines if re.search(r"defeating\s*pirates", t, re.I) or "击败虚海" in squash_cn(t)]
    if not after:
        return None
    x, y = after[0]
    box = (int(max(0, x - img.width * 0.13)), int(max(0, y - 10)),
           int(min(img.width, x + img.width * 0.13)), int(min(img.height, y + img.height * 0.06)))
    crop = img.crop(box)
    crop = crop.resize((crop.width * 3, crop.height * 3))
    try:
        txt = " ".join(t for t, *_ in _ocr_winocr(crop))
    except ImportError:
        txt = " ".join(t for t, *_ in _ocr_tesseract(crop))
    m = SEL_RX.search(txt) or CN_SEL_RX.search(squash_cn(txt))
    return _side_of_word(next(g for g in m.groups() if g)) if m else None


NEED_RX = re.compile(r"need\s*(\d)\s*more\b.*whisper", re.I)   # "Sea\ Whisper" gibi bozuk okunabiliyor
CN_NEED_RX = re.compile(r"还需要获得(\d)条封印秘语")              # 还需要获得 3 条封印秘语才能开启虚海终港
TERMINAL_RX = re.compile(r"\bswitch\b|select\s*a\s*seal|void\s*sea\s*terminal", re.I)
CN_TERMINAL_RX = re.compile(r"切换|请选择一条封印秘语|虚海终港")
SWITCH_RX = re.compile(r"\W*(?:switch|切换)\W*", re.I)


def is_terminal(text):
    return bool(TERMINAL_RX.search(text) or CN_TERMINAL_RX.search(squash_cn(text)))


def need_count(text):
    m = NEED_RX.search(text) or CN_NEED_RX.search(squash_cn(text))
    return int(m.group(1)) if m else None
NUM_LINE_RX = re.compile(r"^\W*(\d+(?:[.,]\d+)?)\W*$")


def _ocr_any(img):
    try:
        return _ocr_winocr(img)
    except ImportError:
        return _ocr_tesseract(img)


def _side_of_heading(text):
    """Bozuk okunmuş başlıktan tür: 'Whispä of Annament' -> arm, "his of %'üonder" -> won."""
    cn = squash_cn(text)
    if "秘语" in cn:
        return _cn_side_chars(cn)
    t = text.lower()
    if re.search(r"arm|ann?am|mament|amen", t):
        return "arm"
    if re.search(r"ond|nder|wond", t):
        return "won"
    if re.search(r"ealt|weal|alth", t):
        return "wea"
    if "boss" in t:
        return "boss"
    return None


def _fill_numbers(text, nums):
    """Kalın beyaz rakamlar renkli okumada düşebiliyor: rakamsız kalan '%' ve
    'additional Hero' / 'additional Memory' (Scrap / Thread) öncesine sırayla eksik rakamları yerleştirir.
    "32% additional Hero Memory…" gibi zaten "%" ile biten yere sayı konmaz."""
    nums = list(nums)
    # "chance" önündeki tek başına "o" / "0" / "00" okunamamış "%"tir ("+21" ayrı satır, "00 chance for…"):
    # sayı sanılırsa değer 0 çıkıyordu
    text = re.sub(r"(?<![\d.%])(?:0{1,2}|y?o)(?=\s*chance\b)", "%", text, flags=re.I)
    out, last = [], 0
    for m in re.finditer(r"%|additional\s*(?:hero|memory|void)|[枚个张]\s*(?:英雄|追忆|虚海)", text, re.I):
        before = text[:m.start()].rstrip()
        out.append(text[last:m.start()])
        if nums and not (before and (before[-1].isdigit() or before[-1] == "%")):
            out.append(f" {nums.pop(0)} ")
        last = m.start()
    out.append(text[last:])
    text = "".join(out)
    # Wonder: "…to be multiplied by" + ayrı satırda "2" ("time(s)"): kalan sayı katsayının yerine
    mm = re.search(r"multiplied\s*by(?!\s*\d)", text, re.I)
    if nums and mm:
        text = f"{text[:mm.end()]} {nums.pop(0)} {text[mm.end():]}"
    # "%" de okunamadıysa yerleşmeyen sayı kalır: metin sayıyla başlamıyorsa başına koy ("+25 Void Sea…")
    if nums and not re.match(r"\W*\d", text):
        text = f"{nums[0]} {text}"
    return text


def _tier_values():
    """Her etkinin olabilecek değerleri: alev havuzlarındaki Normal / Magic / Rare / Epic kademeleri (tlidb)."""
    out = {}
    for pool in (FLAME_POOL, FE_FLAME_POOL, {"boss": FE_BOSS_POOL}):
        for side in pool.values():
            for k, vals in side:
                if k != "none":
                    out.setdefault(k, set()).update(round(v, 1) for v in vals)
    return out


def tier_ok(effs):
    """Okunan etkilerin hepsi bilinen bir kademe değerinde mi? (havuzda olmayan tür: kontrol edilemez, geçer)"""
    tiers = _tier_values()
    return bool(effs) and all(k != "none" and (k not in tiers or round(v, 1) in tiers[k]) for k, v, *_ in effs)


def _tier_fix(text, nums):
    """Kart değeri kademelere uymadı (rakam düşmüş, "+" "1" okunmuş, sayılar yer değiştirmiş): okunan sayılarla
    (baştaki "1"i atılmış hâlleri de) eksik yerler denenir; kademe değerlerine uyan tek bir sonuç varsa o.
    Birden çok farklı sonuç çıkarsa tahmin edilmez (None)."""
    import itertools
    cands = []
    for n in nums:
        for c in (n, n[1:] if len(n) > 1 and n[0] == "1" else None):
            if c and c not in cands:
                cands.append(c)
    texts = [text]
    # metindeki "142 %" gibi bir sayının baştaki "1"i atılmış hâli
    for m in re.finditer(r"(?<![\d.])1(\d+)", text):
        texts.append(text[:m.start()] + m.group(1) + text[m.end():])
    # "%" hiç okunmadıysa ("28" + "chance for Hero Memories…"): başa "%" koyup sayıyı onun önüne yerleştir
    if not re.search(r"%|o/o", text):
        texts += ["% " + re.sub(r"^\W*\d+(?:[.,]\d+)?\s*", "", t) for t in texts]
    found = {}
    for t in texts:
        for r in range(3):
            for pool in itertools.permutations(cands, r):
                got = parse_effects(_fill_numbers(t, list(pool)))
                if tier_ok(got):
                    found.setdefault(tuple((k, round(v, 1)) for k, v, *_ in got), got)
    return next(iter(found.values())) if len(found) == 1 else None


def card_tier(img, cx, ui=1.0):
    """Kartın kalitesi başlığının renginden: 0 Normal (renksiz / gri), 1 Magic (mavi), 2 Rare (mor), 3 Epic
    (turuncu); belli değilse None. Kayıtlı ekranlarda okunan değerin kademesiyle 34 / 34 eşleşti."""
    import colorsys
    import numpy as np
    H = img.height
    box = (int(cx - 0.11 * ui * H), int((0.5 + 0.075 * ui) * H), int(cx + 0.11 * ui * H), int((0.5 + 0.105 * ui) * H))
    a = np.asarray(img.crop(box)).astype(float) / 255
    mx, mn = a.max(axis=2), a.min(axis=2)
    sel = (mx > 0.55) & ((mx - mn) / np.maximum(mx, 1e-6) > 0.25)
    if sel.sum() < 0.02 * sel.size:
        return 0
    hue = colorsys.rgb_to_hsv(*(a[..., i][sel].mean() for i in range(3)))[0] * 360
    if 195 <= hue <= 245:
        return 1
    if 265 <= hue <= 315:
        return 2
    if 25 <= hue <= 60:
        return 3
    return None


def _tier_fill(text, tier):
    """Sayısı okunamayan kart: türü metinden, kalitesi renkten (card_tier) belli; değer o kademenin tlidb değeri.
    Metne uyan tek bir etki çıkarsa o, yoksa None."""
    found = {}
    for k, vals in FE_BOSS_POOL:         # Boss: yazıda "şans" ve "1 adet" iki ayrı sayı
        got = parse_effects(_fill_numbers(text, [f"{vals[tier] * 100:g}", "1"]))
        if got and all(kk == k for kk, *_ in got):
            found.setdefault((k, round(vals[tier], 2)), got)
    for pool in (FLAME_POOL, FE_FLAME_POOL):
        for side in pool.values():
            for k, vals in side:
                if k == "none":
                    continue
                v = vals[tier]
                for n in [v] + ([v / 0.2] if k.startswith("luck") else []):
                    n = f"{n:g}"
                    for nums in ([n], ["20", n]):
                        got = parse_effects(_fill_numbers(text, nums))
                        if got and all(kk == k and round(vv, 1) == round(v, 1) for kk, vv, *_ in got):
                            found.setdefault((k, round(v, 1)), got)
    return next(iter(found.values())) if len(found) == 1 else None


def _cn_numbers(text, whites, more_whites):
    """Çince kart: kalın beyaz rakamlar renkli okumada kısmen düşüyor ("20%" -> "0%", "17 枚" -> "7 枚").
    Ayrıca okunan beyaz rakamlarla tamamla: kısmi rakam, sonu ona uyan beyaz sayıyla değişir; rakamsız
    kalan "%" / "枚英雄" yerlerine kalan beyaz sayılar konur. Hâlâ rakam eksikse (seçili olmayan gri kartta
    rakam daha soluk) beyaz eşiği düşürülerek tekrar okunur."""
    text = squash_cn(text)          # "％" -> "%", "枚 英 雄" -> "枚英雄": yer işaretleri bulunabilsin

    def fix(ws):
        used = [False] * len(ws)

        def rep(m):
            d = m.group(0)
            for k, w in enumerate(ws):
                if not used[k] and (w == d or (len(w) > len(d) and w.endswith(d))):
                    used[k] = True
                    return w
            return d
        t = re.sub(r"\d+(?:\.\d+)?", rep, text)
        rest = [w for k, w in enumerate(ws) if not used[k]]
        return _fill_numbers(t, rest) if rest else t

    def missing(t):
        return bool(re.search(r"(?<!\d)%|(?<!\d)[枚个]英", squash_cn(t)))
    out = fix(whites)
    for thr in (180, 160, 140):
        if not missing(out):
            break
        ws = more_whites(thr)
        if len(ws) > len(whites):
            whites, out = ws, fix(ws)
    return out


def _reading_order(lines, tol=28):
    """OCR satırlarını okuma sırasına koyar: aynı satırdaki (y farkı < tol) parçalar soldan sağa.
    Kartlardaki renkli bağlantı yazıları ("Drop Rarity") ayrı satır olarak, bazen başta gelir."""
    rows = []
    for t, x, y in sorted(lines, key=lambda l: l[2]):
        if rows and abs(rows[-1][0] - y) < tol:
            rows[-1][1].append((t, x, y))
        else:
            rows.append([y, [(t, x, y)]])
    return [p for _, parts in rows for p in sorted(parts, key=lambda p: p[1])]


def _white_numbers(crop, np, Image, thr=200, f=1.0):
    """Kartlardaki kalın beyaz rakamlar (renkli okumada düşebiliyor): sadece beyaz pikselleri
    siyah-beyaz maske yapıp beyaz alanın etrafından sıkı kırparak okur. Küçük rakam büyük boş
    alanda kalınca OCR kararsız olduğu için birkaç büyütme oranı sırayla denenir."""
    a = np.asarray(crop).astype(int)
    return _mask_numbers(a.min(axis=2) > thr, np, Image, f)


def _white_more(crop, np, Image, f=1.0, thr=200):
    """Beyaz rakamlar için ek adaylar (okunan değer kademe değerlerine uymadığında): başındaki "+" silinmiş
    maske bütün olarak ve satır satır (Wonder: üstte "+42", altta "2 time(s)"; OCR "+42"yi "142" okuyabiliyor,
    iki satır birlikteyken hiç okumayabiliyor)."""
    wm = np.asarray(crop).astype(int).min(axis=2) > thr
    if not wm.any():
        return []
    bands = _mask_bands(wm, np)
    for y0, y1 in bands:
        _drop_plus(wm, y0, y1, np)
    nums = _mask_numbers(wm, np, Image, f)
    if len(bands) > 1:
        for y0, y1 in bands:
            band = np.zeros_like(wm)
            band[y0:y1 + 1] = wm[y0:y1 + 1]
            nums += _mask_numbers(band, np, Image, f)
    return nums


def _mask_bands(wm, np):
    """Maskedeki yazı satırları: [(üst, alt)] (aralarında boş satır olan parçalar; kırıntılar atılır)."""
    rows = np.nonzero(wm.any(axis=1))[0]
    bands, start = [], rows[0]
    for p, q in zip(rows, rows[1:]):
        if q - p > 2:
            bands.append((start, p))
            start = q
    bands.append((start, rows[-1]))
    tall = max(y1 - y0 for y0, y1 in bands)
    return [(y0, y1) for y0, y1 in bands if y1 - y0 >= 0.4 * tall]


def _drop_plus(wm, y0, y1, np):
    """Satırın başındaki "+" işaretini siler: OCR onu "1" okuyabiliyor ("+42" -> "142").
    "+" rakamlardan kısa, satırın ortasında duran, kabaca kare bir işaret."""
    cols = np.nonzero(wm[y0:y1 + 1].any(axis=0))[0]
    if len(cols) < 2:
        return
    x0 = x1 = cols[0]
    for c in cols[1:]:
        if c - x1 > 1:
            break
        x1 = c
    g = np.nonzero(wm[y0:y1 + 1, x0:x1 + 1].any(axis=1))[0]
    h, w, band_h = g[-1] - g[0] + 1, x1 - x0 + 1, y1 - y0 + 1
    if h < 0.8 * band_h and g[0] > 0 and g[-1] < band_h - 1 and 0.7 <= w / h <= 1.4:
        wm[y0:y1 + 1, x0:x1 + 1] = False


def _mask_numbers(wm, np, Image, f):
    ys, xs = np.nonzero(wm)
    if len(xs) == 0:
        return []
    box = (max(0, xs.min() - 12), max(0, ys.min() - 12), xs.max() + 12, ys.max() + 12)
    mask = Image.fromarray(np.where(wm, 0, 255).astype("uint8")).convert("RGB").crop(box)
    for s in (3, 4, 5, 2):
        s *= f            # düşük çözünürlükte (1080p…) yazı küçük: büyütme oranı 1440p'ye göre ölçeklenir
        found = _reading_order(_ocr_any(mask.resize((int(mask.width * s), int(mask.height * s)))))
        nums = [n for t, *_ in found for n in re.findall(r"\d+(?:[.,]\d+)?", t)]
        if nums:
            return nums
    return []


# Kart metinlerinde geçen kelimeler (tlidb). Seçili kartın parlaması ve parıltı animasyonu harfleri bozabiliyor
# ("rnrmory", "r.lemory", "Memo", "*raps", "Sfraps", "c,Kance", "dropp&d"): bozuk kelime en yakın kelimeye çevrilir.
CARD_WORDS = ("memory", "memorys", "memories", "scrap", "scraps", "thread", "threads", "dropped", "drop", "drops",
              "chance", "additional", "multiplied", "treasure", "treasures", "void", "sea", "hero", "appear",
              "quantity", "rarity", "replicated", "doubled", "base", "time", "times", "from", "for", "the", "with",
              "invitation", "invitations")


def _fix_words(text):
    import difflib

    def core(w):
        return re.sub(r"[^a-z]", "", w.lower())

    def close(c, cut):
        return (difflib.get_close_matches(c, CARD_WORDS, n=1, cutoff=cut) or [None])[0]

    def plain(w):          # rakam / "%" / "+" içermeyen, harfli parça ("+21%", "2" olduğu gibi kalır)
        return bool(core(w)) and not re.search(r"[\d%+]", w)
    # parlayan kartta kalın beyaz "1" harf okunabiliyor: "drop I additional"
    toks = re.sub(r"(?<!\S)[Il|](?=\s+additional)", "1", text).split()
    out, i = [], 0
    while i < len(toks):
        w = toks[i]
        c = core(w)
        if not plain(w) or c in CARD_WORDS:
            out.append(w)
            i += 1
            continue
        # ikiye bölünmüş kelime: "cha ce", "em ory", "r nemory"
        if i + 1 < len(toks) and plain(toks[i + 1]) and core(toks[i + 1]) not in CARD_WORDS:
            m = close(c + core(toks[i + 1]), 0.75)
            if m:
                out.append(m)
                i += 2
                continue
        if len(c) >= 3:
            out.append(close(c, 0.6) or w)
        elif len(c) > 1 or c == "a":
            out.append(w)
        # tek harflik kırıntı ("r lcmory") atılır
        i += 1
    return " ".join(out)


def read_cards_color(img, lines):
    """Void Sea Terminal'deki 3 kartı tek tek renkli ve büyütülmüş okur.
    Kartlar 'Switch' butonunun altında, ekran yüksekliğiyle orantılı aralıklarla durur."""
    import numpy as np
    from PIL import Image, ImageOps
    W, H = img.size
    sw =[(x, y) for t, x, y in lines if SWITCH_RX.fullmatch(squash_cn(t))]
    cx0 = sw[0][0] if sw else W / 2
    # arayüz ölçeği: sahne ekranın dikey ortasına göre çiziliyor; varsayılan ölçekte Switch merkezin 0.25·H
    # üstünde. Daha büyük ölçekte (ör. 21:9 ekranda %7-8) kartlar da birbirinden o oranda uzak.
    ui = min(1.6, max(0.85, (0.5 * H - sw[0][1]) / (0.25 * H))) if sw else 1.0   # büyük arayüz ölçeği (#4)
    if abs(ui - 1.0) < 0.03:      # varsayılan ölçek (16:9): eski, denenmiş konumlar aynen kalsın
        ui = 1.0
    f = max(1.0, 1440 / H)       # 1080p'de kart yazısı 1440p'dekinin 3/4'ü: büyütmeyi ona göre artır
    cards = []
    for dx in (-0.315 * ui, 0.0, 0.315 * ui):
        cx = cx0 + dx * H
        box = (int(max(0, cx - 0.17 * ui * H)), int((0.5 + 0.065 * ui) * H),
               int(min(W, cx + 0.17 * ui * H)), int((0.5 + 0.19 * ui) * H))
        crop = img.crop(box)
        white_nums = _white_numbers(crop, np, Image, f=f)
        # önce 2 kat; etki çıkmazsa 3 kat dene ("Drop Rarity" 2 katta "Dro Rarit" okunabiliyor).
        # bir geçişte bulunan başlık sonrakinde okunamasa da korunur (kart listeden düşmesin)
        head = ""
        effs = []
        # okuma kalitesi: sadece "Memory Scrap / Thread" bulunduysa (sayı düşmüş) ya da "şanslı ek" "%" okunmadan
        # çıktıysa (sayılar yer değiştirmiş olabilir: "chance for 20 additional") okuma bitmiş sayılmaz, bir
        # sonraki büyütme denenir; en iyisi kalır (FE planında Wealth kartı "etkisiz" ya da yanlış görünüyordu)
        def quality(es):
            if not es:
                return 0
            if all(k == "none" for k, *_ in es):
                return 1
            weak = any(k in ("luck", "luckS", "luckT", "luckI") and not re.search(r"\d\s*%", src) for k, _v, src in es)
            # 4: değerler tlidb kademe değerlerinden biri (yanlış okunmuş sayı kademeye uymaz)
            return 2 if weak else 4 if tier_ok(es) else 3
        more = []
        # 3. deneme: gri + kontrast açılmış (seçili kartın arkasındaki parlamada soluk alt satır hiç okunmuyordu)
        gray = lambda: ImageOps.autocontrast(crop.convert("L"), cutoff=2).convert("RGB")
        for src, s in ((lambda: crop, 2 * f), (lambda: crop, 3 * f), (gray, 2 * f)):
            im = src()
            col = [t for t, *_ in _reading_order(_ocr_any(im.resize((int(im.width * s), int(im.height * s)))))]
            head = next((t for t in col if (re.search(r"of|boss", t, re.I) or "秘语" in squash_cn(t))
                         and _side_of_heading(t)), "") or head
            body = [t for t in col if t != head]
            if CJK_RX.search(" ".join(body)):
                # Çince: rakamlar satır içinde kalsın ("…额外" + ayrı satırda "32"), kısmi okunanlar beyaz rakamla tamamlansın
                text = " ".join(body)
                got = parse_effects(_cn_numbers(text, white_nums,
                                                lambda thr: _white_numbers(crop, np, Image, thr=thr, f=f)))
                if quality(got) > quality(effs):
                    effs = got
                if quality(effs) == 4:
                    break
                continue
            # tek başına satırdaki sayılar eksik yerlere dağıtılır; "multiplied by"dan hemen sonraki ("2" time(s))
            # ise kat sayısıdır, yerinde kalır (yoksa "%"in yerine geçip 42 yerine 2 okunuyordu)
            standalone, parts = [], []
            for t in body:
                m = NUM_LINE_RX.match(t)
                if m and not (parts and re.search(r"multiplied\s*by\s*$", parts[-1], re.I)):
                    standalone.append(m.group(1))
                else:
                    parts.append(t)
            text = _fix_words(" ".join(parts))
            present = re.findall(r"\d+(?:[.,]\d+)?", text)
            pool = standalone + [n for n in white_nums if n not in standalone and n not in present]
            got = parse_effects(_fill_numbers(text, pool))
            if not tier_ok(got):
                if not more:
                    more = _white_more(crop, np, Image, f=f) or ["-"]
                fixed = _tier_fix(text, standalone + white_nums + present + [n for n in more if n != "-"])
                if fixed:
                    got = fixed
            if quality(got) > quality(effs):
                effs = got
            if quality(effs) == 4:
                break
        # hiç sayı okunamadıysa (sadece "Memory Scrap / Thread" ya da hiçbir şey): kalite kartın renginden,
        # değer o kademenin tlidb değeri
        if text and quality(effs) <= 1:
            tier = card_tier(img, cx, ui)
            got = _tier_fill(text, tier) if tier is not None else None
            if got:
                effs = got
        side = _side_of_heading(head)
        if side is None and effs and re.search(r"invit", effs[0][2], re.I):
            side = "boss"
        if side is None and effs:
            k = effs[0][0]
            if k in ARM_ONLY:
                side = "arm"
            elif k in WEA_ONLY:
                side = "wea"
            elif k in WON_ONLY or re.search(r"doubled|multipl", text, re.I):
                # Wonder: "Memory Scraps … to be multiplied by 2 time(s)" / "doubled"
                side = "won"
            elif k == "none":                                          # Memory Scrap / Thread: Wealth
                side = "wea"
        if side or effs:
            cards.append((side or "?", effs, cx))
    return cards


def analyze(img):
    """Ekranı okur. -> {"mode": "flame" | "cards", "cards", "owned", "owned_n", "selected"}"""
    lines = ocr_lines(img)
    cards, owned, owned_n = read_screen(lines, img.width)
    owned2, owned_n2 = read_owned(img)
    if len(owned2) >= len(owned):
        owned = owned2
    owned_n = owned_n2 if owned_n2 is not None else owned_n
    if owned_n is None and owned:
        owned_n = len(owned)
    main_lines = [t for t, x, y in lines if x < img.width * 0.80]
    # 21:9 ekranda büyük başlık "Put" / "in Void Sea" / "Flames" diye ayrı satırlara bölünebiliyor: birleşik metne de bak
    flame = (any(FLAME_RX.search(t) or FLAME_RX.search(squash_cn(t)) for t in main_lines)
             or bool(FLAME_RX.search(" ".join(main_lines))))
    # Terminalin özet ekranı: "Need 2 more Seal Whisper(s) to open the Void Sea Terminal" — kart yok
    need = next((n for t, x, y in lines if (n := need_count(t)) is not None), None)
    if not flame and need is not None:
        return {"mode": "overview", "cards": [], "owned": owned, "owned_n": owned_n, "selected": None,
                "need": need, "cjk": cn_screen(lines)}
    if not flame and any(is_terminal(t) for t, x, y in lines if x < img.width * 0.80):
        colored = read_cards_color(img, lines)
        # rakamı düşmüş etki (Çincede "0% 概率…" -> Lucky extra 0) okunmuş sayılmasın
        good = lambda cs: sum(1 for _, e, _ in cs if e and all(v > 0 or k == "none" for k, v, *_ in e))
        if good(colored) >= good(cards):
            cards = colored
    # Switch'e basılınca ortada tek kart kalır: Base Drop Quantity (üç kartın arasında hiç çıkmıyor, 618 kartta 0)
    switch = None
    if (not flame and len(cards) == 1 and cards[0][1] and all(k == "baseQ" for k, *_ in cards[0][1])
            and any(SWITCH_RX.fullmatch(squash_cn(t)) for t, *_ in lines)):
        switch = cards[0][1][0][1]
        cards = []
    res = {"mode": "flame" if flame else "cards", "cards": [] if flame else cards, "owned": owned,
           "owned_n": owned_n, "selected": read_selected_flame(img, lines) if flame else None, "tiers": {},
           "cjk": cn_screen(lines), "switch": switch}
    if flame:
        try:
            cols = read_flame_colors(img)
        except Exception:
            cols = {}
        try:
            shapes = read_flame_shapes(img)
        except Exception:
            shapes = {}
        res["pair"] = flame_pair(res["selected"], shapes)
        res["tiers"] = flame_tiers(cols, res["selected"], read_selected_tier(lines, img.width), shapes)
    return res


CN_SCREEN_RX = re.compile(r"秘语|虚海|封印|切换")


def cn_screen(lines):
    """Ekranda oyunun Çince kelimeleri var mı (Çince OCR İngilizce ekranda rastgele Çince karakter de üretebiliyor)."""
    return any(CN_SCREEN_RX.search(squash_cn(t)) for t, *_ in lines)


def _useful(res, lang="en"):
    # Çince OCR İngilizce ekrandan da Latin harfli bir şeyler çıkarabiliyor: Çince sonuç ancak ekranda
    # Çince karakter varsa kabul edilir (İngilizce oynayan ama Çince paketi kurulu olanlar için)
    if lang != "en" and not res.get("cjk", True):
        return False
    return (res["mode"] in ("flame", "overview") or bool(res["cards"]) or bool(res["owned"])
            or res.get("switch") is not None)


def analyze_any(img):
    """analyze() oyunun diliyle: önce son işe yarayan OCR dili, sonuç boşsa diğer kurulu dil (en / zh)."""
    global OCR_LANG
    langs = [OCR_LANG] + [l for l in ("en", zh_ocr_tag()) if l and l != OCR_LANG]
    first = None
    for lang in langs:
        OCR_LANG = lang
        res = analyze(img)
        if _useful(res, lang):
            return res
        first = first or res
    OCR_LANG = langs[0]
    return first


def flame_pair(selected, shapes=None):
    """Ekrandaki iki üst alevin türleri. Yazıda sadece seçili alev geçer; karşısındaki ikonundan anlaşılır
    (read_flame_shapes, FE planı): çatallı Wealth, sivri Wonder ya da Boss, diğerleri fısıltı vermez.
    Sivri karşı alevin türü seçili alevden çıkar: Wonder karşısında Boss, Boss ya da Wealth karşısında Wonder.
    -> (tür, tür) | tek alev fısıltı veriyorsa (tür,) | fısıltı veren yoksa () | seçili fısıltı vermiyor, karşısı
    sivri ama hangisi belli değil ("?",). Şekiller okunamazsa eski varsayım."""
    vals = list((shapes or {}).values())
    own = {"won": "point", "boss": "point", "wea": "notch", "other": "other"}.get(selected)
    if FLAME_PAIR[0] == "wea" and len(vals) == 2 and own in vals:
        vals.remove(own)
        rest = vals[0]
        if selected == "other":
            return {"notch": ("wea",), "point": ("?",)}.get(rest, ())
        if rest == "other":
            return (selected,)
        if rest == "notch":
            return {"won": ("wea", "won"), "boss": ("boss", "wea"), "wea": ("wea",)}[selected]
        return {"won": ("boss", "won"), "boss": ("boss", "won"), "wea": ("wea", "won")}[selected]
    if selected == "boss":
        return ("boss", "won")
    if selected == "wea":
        return ("wea", "won")
    if selected == "arm":
        return ("arm", "won")
    return FLAME_PAIR


FLAME_PAIR = ("arm", "won")     # uygulama FE planında ("wea", "won") yapar


def flame_tiers(cols, selected, sel_tier, shapes=None):
    """Alevlerin garanti kademesi {tür: kademe}. Seçili alevin türü ve kademesi yazıdan; köşesi, rengi
    ortadaki daireyle eşleşen köşe. Diğer köşe diğer türdür, kademesi kendi renginden."""
    tier = lambda c: FLAME_COLOR_TIER.get(c) if c else None
    left, right, center = cols.get("left"), cols.get("right"), cols.get("center")
    out = {}
    pair = flame_pair(selected, shapes)
    if len(pair) < 2:
        if selected in pair:
            t = sel_tier if sel_tier is not None else tier(center)
            if t is not None:
                out[selected] = t
        elif pair:              # seçili alev fısıltı vermiyor: öteki köşenin rengi
            side = next((k for k, v in (shapes or {}).items() if v != "other"), None)
            if side and tier(cols.get(side)) is not None:
                out[pair[0]] = tier(cols[side])
        return out
    if selected not in pair:
        # seçili alev yazıdan okunamadı: eski varsayım (sol Armament/Wealth, sağ Wonder)
        for side, c in zip(pair, (left, right)):
            if tier(c) is not None:
                out[side] = tier(c)
        return out
    other = pair[1] if selected == pair[0] else pair[0]
    if left == right:
        sel_c = oth_c = left                                 # iki alev aynı renk: köşe fark etmez
    elif sel_tier is not None and tier(left) == sel_tier and tier(right) != sel_tier:
        sel_c, oth_c = left, right                           # yazıdaki kalite sol köşenin rengine uyuyor
    elif sel_tier is not None and tier(right) == sel_tier and tier(left) != sel_tier:
        sel_c, oth_c = right, left
    elif center == left:                                     # yazıdan anlaşılamadı: ortadaki dairenin rengi
                                                             # (sarı seçiliyken sönük, okunamayabiliyor)
        sel_c, oth_c = left, right
    elif center == right:
        sel_c, oth_c = right, left
    else:
        sel_c = oth_c = None                                 # hangi köşenin seçili olduğu anlaşılamadı
    t = sel_tier if sel_tier is not None else tier(sel_c)
    if t is not None:
        out[selected] = t
    if tier(oth_c) is not None:
        out[other] = tier(oth_c)
    return out


# --------------------------------------------------------------------------- #
# Alev seçimi (Void Sea Seal): üst-soldaki alev Armament, üst-sağdaki Wonder fısıltısı garanti eder.
# Alevin rengi kalitesi (mavi < mor < sarı); oyun yazısı: sarı -> "Rare quality or higher",
# mor -> "Magic quality or higher", mavi -> (varsayım) "Normal quality or higher".
# Garanti kademeden Epic'e kadar her kademe eşit ağırlıklı varsayılır.
# Her türde 5 etki çeşidi eşit sayıda (tlidb: tür başına 8'er kayıt).
# Wonder'ın Scrap ×2 ve Thread ×2 çeşitleri kırmızıya katkı yapmaz.
# --------------------------------------------------------------------------- #
TIER_NAMES = ["Normal", "Magic", "Rare", "Epic"]
FLAME_POOL = {   # kademe değerleri: Normal / Magic / Rare / Epic
    "arm": [("baseQ", (9, 14, 18, 36)), ("appear", (28, 42, 56, 112)), ("memQty", (21, 32, 42, 84)),
            ("flat", (2, 3, 4, 8)), ("luck", (0.2 * 8, 0.2 * 12, 0.2 * 17, 0.2 * 33))],
    "won": [("baseQ", (9, 14, 18, 36)), ("tQty", (14, 21, 28, 56)), ("rarity", (25, 37, 49, 98)),
            ("none", (0, 0, 0, 0)), ("none", (0, 0, 0, 0))],
}
FLAME_COLOR_TIER = {"blue": 0, "purple": 1, "yellow": 2}     # alev rengi -> garanti en düşük kademe
FLAME_COLOR_TR = {"blue": "blue", "purple": "purple", "yellow": "yellow"}
TIER_COLOR = {v: k for k, v in FLAME_COLOR_TIER.items()}
DEFAULT_TIER = 2      # renk okunamazsa: önceki sürümlerdeki gibi Rare ve üstü


def flame_values(pool, tier):
    return [(k, v) for k, vals in pool for v in vals[tier:]]


def flame_advice(picked, s, tiers=None, pool=None, score=None):
    """tiers: {tür: garanti en düşük kademe}. Katkılar %3'ten yakınsa kalitesi yüksek alev önerilir.
    pool/score: kırmızı planında FLAME_POOL (Armament/Wonder) + red_score, FE planında FE_FLAME_POOL + fe_score.
    -> (en iyi tür | "tie" | None, {tür: (ortalama mutlak artış, ortalama yüzde artış)})"""
    tiers = tiers or {}
    pool = pool or FLAME_POOL
    score = score or red_score
    base = score(picked, s)
    out = {}
    for side, kinds in pool.items():
        gs = [score(picked + [e], s) - base for e in flame_values(kinds, tiers.get(side, DEFAULT_TIER))]
        g = sum(gs) / len(gs)
        out[side] = (g, g / base * 100 if base > 0 else 0.0)
    a_side, w_side = list(pool)
    a, w = out[a_side][0], out[w_side][0]
    top = max(a, w)
    if top <= 1e-9:
        return None, out
    if abs(a - w) <= top * 0.03:
        ta, tw = tiers.get(a_side, DEFAULT_TIER), tiers.get(w_side, DEFAULT_TIER)
        if ta != tw:
            return (a_side if ta > tw else w_side), out
        return "tie", out
    return (a_side if a > w else w_side), out


def read_flame_shapes(img):
    """Üst köşelerdeki alevler -> {"left"|"right": "notch" (Wealth: altı çatallı) | "point" (Wonder, Boss: göz, alt sivri)
    | "other" (S, damla ya da boş: fısıltı vermez)}. Ölçüler ölçekten bağımsız: en büyük parlak renkli parçanın
    yükseklik / genişlik oranı ve alt kısmındaki iç boşluk. 11 ekranın 22 köşesinde çatallı oran en çok 1.34; sivri en
    az 1.44, alt boşluk en çok 0.09; S (2 köşe, mavi) alt boşluk en az 0.16. Arada kalan alev yazılmaz: eski varsayım."""
    import numpy as np
    from PIL import Image
    W, H = img.size
    out = {}
    for side, dx in (("left", -0.231), ("right", 0.233)):
        cx, cy, r = W / 2 + dx * H, 0.305 * H, 0.07 * H
        crop = img.crop((int(cx - r), int(cy - r), int(cx + r), int(cy + r))).convert("RGB")
        a = np.asarray(crop.resize((120, 120), Image.BICUBIC)).astype(float) / 255
        mx, mn = a.max(2), a.min(2)
        mask = (mx > 0.62) & ((mx - mn) / np.maximum(mx, 1e-6) > 0.3)
        seen = np.zeros_like(mask)
        best = []
        for y, x in zip(*np.where(mask)):
            if seen[y, x]:
                continue
            stack, part = [(y, x)], []
            seen[y, x] = True
            while stack:
                p, q = stack.pop()
                part.append((p, q))
                for u, v in ((p + 1, q), (p - 1, q), (p, q + 1), (p, q - 1)):
                    if 0 <= u < 120 and 0 <= v < 120 and mask[u, v] and not seen[u, v]:
                        seen[u, v] = True
                        stack.append((u, v))
            if len(part) > len(best):
                best = part
        if len(best) < 150:         # renkli ikon yok: boş slot ya da gri alev
            out[side] = "other"
            continue
        ys = np.array([b[0] for b in best])
        xs = np.array([b[1] for b in best])
        h, w = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
        ratio = h / w
        if ratio <= 1.37:
            out[side] = "notch"
        elif ratio >= 1.41:
            body = np.zeros((h, w), bool)
            body[ys - ys.min(), xs - xs.min()] = True
            gaps = []
            for row in body[int(0.6 * h):]:
                idx = np.where(row)[0]
                if len(idx):
                    gaps.append(1 - len(idx) / (idx[-1] - idx[0] + 1))
            gap = sum(gaps) / len(gaps) if gaps else 0.0
            if gap >= 0.13:
                out[side] = "other"
            elif gap <= 0.11:
                out[side] = "point"
    return out


def read_flame_colors(img):
    """Üst-sol, üst-sağ alevin ve ortadaki dairenin (seçili alev) rengi ->
    {"left"|"right"|"center": "blue"|"purple"|"yellow"}. Hangi köşenin Armament, hangisinin Wonder
    olduğu oturumdan oturuma değişiyor; tür, seçili alevin yazısından ve ortadaki rengin eşleştiği
    köşeden çıkarılır (analyze). Sahne ekranın ortasına, yüksekliğe göre ölçekli çiziliyor."""
    import numpy as np
    W, H = img.size
    out = {}
    for side, dx, dy, rr in (("left", -0.231, 0.305, 0.04), ("right", 0.233, 0.305, 0.04), ("center", 0.0, 0.498, 0.035)):
        cx, cy, r = W / 2 + dx * H, dy * H, rr * H
        a = np.asarray(img.crop((int(cx - r), int(cy - r), int(cx + r), int(cy + r))).convert("RGB")).astype(float)
        a = a.reshape(-1, 3) / 255
        mx, mn = a.max(1), a.min(1)
        keep = (mx > 0.6) & ((mx - mn) / np.maximum(mx, 1e-6) > 0.35)     # parlak ve renkli (arka plan koyu)
        if keep.sum() < 30:
            continue
        r_, g_, b_ = a[keep].T
        d = np.maximum(mx[keep] - mn[keep], 1e-6)
        h = np.where(mx[keep] == r_, ((g_ - b_) / d) % 6,
                     np.where(mx[keep] == g_, (b_ - r_) / d + 2, (r_ - g_) / d + 4)) * 60
        votes = {"yellow": int(((h >= 15) & (h < 75)).sum()),
                 "blue": int(((h >= 170) & (h < 250)).sum()),
                 "purple": int(((h >= 255) & (h < 330)).sum())}
        best = max(votes, key=votes.get)
        if votes[best] >= keep.sum() * 0.5:
            out[side] = best
    return out


# --------------------------------------------------------------------------- #
# FE planı (Wealth + Wonder): Void Sea haritasında toplanan Memory Scrap ve Memory Thread'lerin FE değeri
#   X (Scrap ya da Thread) = ( taban_X × (1+taban miktar) × (1+çıkma şansı_X) × (1+miktar_X) + sabit_X + şanslı_X )
#                            × (1+treasure miktarı) × (1+çarpılma_X)
#   değer = Scrap × Scrap'in net fiyatı + Thread × bir Thread'in ortalama net fiyatı
# Varsayım (doğrulanmadı): Wonder'ın treasure miktarı ve ×2/×3'ü Wealth'in ek Scrap/Thread'lerini de çarpar.
# Rarity bu planda sayılmaz (kullanıcının kararı). Taban sayılar son koşulardan öğrenilir (seviye değiştikçe
# değişiyor); kayıt yokken FE_DEFAULTS. Thread kalitesi (Magic/Rare/Epic) son koşularda toplananların oranıyla.
# --------------------------------------------------------------------------- #
# Boss Whisper (v7.1): "%X chance for 1 additional Void Sea Invitation(s)": beklenen Invitation = şans × adet.
#   Invitation = ( kendiliğinden düşen + öğrenilen katsayı × şanslar toplamı ) × (1+treasure miktarı)
# Wonder'ın treasure miktarı ikisini de büyütür; Scrap ×2 / Thread ×2 ve Rarity Invitation'ı etkilemez. Değer FE:
# Invitation × net fiyat. Varsayımlar (doğrulanmadı): şanslar toplanır; düşen tür Craving (Ecstasy ve Sorrow Lord
# savaşlarından çıkıyor, Maxroll). Bunları koşular düzeltir: v7.1'den sonra Invitation sayılıyor (kayıtta "inv": true).
INV_IDS = ("410201", "410202", "410203")
INV_DEFAULT = "410201"
FE_ITEMS = {"5140": "Memory Scrap", "5142": "Memory Thread - Magic", "5143": "Memory Thread - Rare",
            "5144": "Memory Thread - Epic", "410201": "Void Sea Invitation - Craving",
            "410202": "Void Sea Invitation - Ecstasy", "410203": "Void Sea Invitation - Sorrow"}
SCRAP_ID = "5140"
THREAD_IDS = ("5142", "5143", "5144")
THREAD_SHORT = {"5142": "M", "5143": "R", "5144": "E"}
FE_DEFAULTS = {"scrap": 40.0, "thread": 2.0}    # fısıltısız bir koşuda taban Scrap / Thread (kayıt yokken)
FE_BASE_RUNS = 10                                # taban sayılar bu kadar son koşudan
FE_PRIOR_RUNS = 3      # az koşuda öğrenilen taban varsayılana çekilir (varsayılan bu kadar koşu sayılır)
FE_KEYS = ("baseQ", "tQty", "appS", "qtyS", "flatS", "luckS", "multS", "appT", "qtyT", "flatT", "luckT", "multT",
           "luckI")
FE_INV_PRIOR = 3.0     # öğrenme: yazılı değerin ağırlığı (bu kadar beklenen Invitation'lık koşu kadar)
FE_INV_BASE_RUNS = 5   # Boss'suz koşularda kendiliğinden düşme oranı 0'a bu kadar koşu ağırlığında çekilir
# Boss kademeleri: Magic / Rare / Epic oyuncunun ekranlarından (#9: mavi %10, mor %20, turuncu %40);
# tlidb'de sayı yok. Normal görülmedi, tahmin.
FE_BOSS_POOL = [("luckI", (0.05, 0.1, 0.2, 0.4))]
# Boss alevi her seçimde çıkmıyor: Boss yolunda bir seçimde Boss alevi bulunma olasılığı. Başlangıç %50 (tlidb:
# Netherrealm'de Boss alevi taşıyan canavar %50 şansla), alev ekranlarında görülenlerle düzelir.
FE_BOSS_RATE = 0.5
FE_BOSS_RATE_N = 4


def fe_boss_rate(runs):
    """Alev ekranlarının kaçında Boss alevi vardı (kayıttaki "flames")."""
    seen = [o["flames"] for r in runs for o in r.get("offers", []) if "flames" in o]
    return (sum("boss" in f for f in seen) + FE_BOSS_RATE * FE_BOSS_RATE_N) / (len(seen) + FE_BOSS_RATE_N)
FE_FLAME_POOL = {   # kademe değerleri: Normal / Magic / Rare / Epic (tlidb Whisper_of_Wealth / _of_Wonder)
    "wea": [("appS", (14, 21, 28, 56)), ("qtyS", (35, 53, 70, 140)), ("flatS", (10, 15, 19, 39)),
            ("luckS", (0.2 * 49, 0.2 * 73, 0.2 * 97, 0.2 * 194)),
            ("appT", (35, 53, 70, 140)), ("qtyT", (21, 32, 42, 84)), ("flatT", (1, 1, 1, 2)),
            ("luckT", (0.2 * 3, 0.2 * 4, 0.2 * 5, 0.2 * 10))],
    "won": [("tQty", (14, 21, 28, 56)), ("rarity", (25, 37, 49, 98)),
            ("multS", (21, 32, 42, 84)), ("multT", (21, 32, 42, 84))],
}


# Switch: dördüncü seçenek, üç kartın yerine alınabilen Base Drop Quantity (Wonder). Kalitesini oyun yazmıyor, tlidb ve
# rehberlerde de yok. Gözlem (4 ekran, 2026-11-06): üç kartın en düşük kalitesine eşit; rastgele olsa yaklaşık %3.
# Kanıt az: tahmin olarak gösterilir, gerçek kart okununca o kullanılır, kayıttaki Switch'lerle denetlenir.
FE_SWITCH_VALUES = (9, 14, 18, 36)
FE_SWITCH_MIN_CHECKS = 5   # bu kadar denetimden sonra
FE_SWITCH_MIN_RATE = 0.6   # kural bundan az tutuyorsa tahmin yapılmaz
SWITCH_X = 10 ** 9         # kart listesinde Switch'in konumu (ekranda yeri yok)


def _card_tier(effs):
    """Kartın kademesi yazılı değerinden; birden çok kademeye uyuyorsa ("Flat Thread 1") ya da katlanmışsa None."""
    if not effs:
        return None
    k, v = effs[0][0], effs[0][1]
    for pool in (*FE_FLAME_POOL.values(), *FLAME_POOL.values(), FE_BOSS_POOL):
        for kk, vals in pool:
            if kk == k and kk != "none":
                hits = [i for i, x in enumerate(vals) if abs(x - v) < 0.051]
                return hits[0] if len(hits) == 1 else None
    return None


def switch_estimate(card_effs):
    """Üç kartın etkilerinden Switch kartının tahmini değeri (en düşük kademe) ya da None."""
    tiers = [t for t in (_card_tier(e) for e in card_effs) if t is not None]
    return FE_SWITCH_VALUES[min(tiers)] if tiers else None


def fe_switch_rule(runs):
    """Kural kayıtlarda tutuyor mu -> (tutan, denetlenen). Gerçek değer: okunan Switch kartı ("switch") ya da o adımda
    seçilmiş Base Quantity (Base Quantity sadece Switch'ten geldiği için)."""
    hits = total = 0
    for r in runs:
        eff = run_whispers(r["effects"])
        for o in r.get("offers", []):
            real = o.get("switch")
            if real is None and o["step"] < len(eff) and eff[o["step"]][0] == "baseQ":
                real = eff[o["step"]][1]
            if real is None:
                continue
            est = switch_estimate([[tuple(e) for e in c[1]] for c in o["cards"]])
            if est is not None:
                total += 1
                hits += abs(est - real) < 0.051
    return hits, total


def fe_switch_ok(s):
    hits, total = (s or {}).get("switch_rule") or (0, 0)
    return total < FE_SWITCH_MIN_CHECKS or hits / total >= FE_SWITCH_MIN_RATE


def fe_switch_note(s):
    hits, total = (s or {}).get("switch_rule") or (0, 0)
    return (f"Switch is an estimate: same quality as the lowest card ({hits} of {total} checks so far). "
            "Press Switch, then “Read” to see the real card.")


def fe_totals(effects):
    t = {k: 0.0 for k in FE_KEYS}
    for key, value, *_ in effects:
        if key in t:
            t[key] += value
    return t


def _fe_factors(t, suf):
    """-> (tabana uygulanan çarpan, sabit + şanslı ek, en sonda her şeye uygulanan çarpan)"""
    pre = (1 + t["baseQ"] / 100) * (1 + t["app" + suf] / 100) * (1 + t["qty" + suf] / 100)
    post = (1 + t["tQty"] / 100) * (1 + t["mult" + suf] / 100)
    return pre, t["flat" + suf] + t["luck" + suf], post


FE_FIT_KEYS = ("baseQ", "tQty", "app", "qty", "mult", "add")     # her eşya (S / T) için ayrı katsayılar
FE_FIT_MIN_RUNS = 5        # bundan az bitmiş Memory materials koşusunda yazılı değerler kullanılır
# Etkileşimler (iki fısıltı türü birlikte formülün söylediğinden iyi / kötü mü): bir çift ancak hem birlikte hem
# ayrı ayrı yeterince koşuda görüldüyse modele girer; katsayı 0'dan ("etkileşim yok") başlar, güçlü çekilir.
FE_INT_MIN_RUNS = 30       # etkileşimlere bakmak için en az bu kadar koşu
FE_INT_MIN_EACH = 12       # çiftin birlikte / sadece biri / sadece öteki olduğu koşular, her biri en az bu kadar
FE_INT_LAMBDA = 35.0       # 12 / 35: simülasyonla seçildi (etkileşim yoksa zararsız, varsa kayıp ~%20 az)
FE_FIT_LAMBDA = {"scrap": 20.0, "thread": 12.0}  # yazılı değere (katsayı 1) çekme gücü: simülasyonla seçildi
                                                # (v6.7; 8 gerçek dünya × 3 sapma × 10–80 koşu, en az FE kaybı)


def _fe_feats(t, suf, base):
    """Öğrenen model için özellikler (log): katsayılar 1 iken fe_expect'in yazılı-değer formülünün aynısı."""
    pre = (1 + t["baseQ"] / 100) * (1 + t["app" + suf] / 100) * (1 + t["qty" + suf] / 100)
    return {"baseQ": math.log(1 + t["baseQ"] / 100), "tQty": math.log(1 + t["tQty"] / 100),
            "app": math.log(1 + t["app" + suf] / 100), "qty": math.log(1 + t["qty" + suf] / 100),
            "mult": math.log(1 + t["mult" + suf] / 100),
            "add": math.log(1 + (t["flat" + suf] + t["luck" + suf]) / (base * pre))}


def fe_expect(effects, s):
    """-> {"scrap": beklenen Scrap, "thread": beklenen Thread}. s["fit"] varsa koşulardan öğrenilen katsayılarla."""
    t = fe_totals(effects)
    fit = s.get("fit")
    out = {}
    for x, suf in (("scrap", "S"), ("thread", "T")):
        if fit:
            f, xs = fit[x], _fe_feats(t, suf, s["base"][x])
            eta = f["a"] + sum(f["c"][k] * xs[k] for k in FE_FIT_KEYS)
            eta += sum(d * xs[i] * xs[j] for (i, j), d in f.get("inter", {}).items())
            out[x] = math.exp(eta)
        else:
            pre, add, post = _fe_factors(t, suf)
            out[x] = (s["base"][x] * pre + add) * post
    inv = s.get("inv") or {}
    out["invite"] = (inv.get("b", 0.0) + inv.get("c", 1.0) * t["luckI"]) * (1 + t["tQty"] / 100)
    return out


def fe_inv_fit(runs):
    """Invitation'ın koşulardan öğrenilmesi. Sadece Invitation'ın sayıldığı koşular ("inv"): eskilerde hiç
    sayılmadığı için sıfır düşmüş gibi okunamaz. -> {"b": Boss'suz koşu başına, "c": yazılı değerin kaç katı,
    "runs": sayılan koşu, "boss_runs": Boss'lu koşu, "got": Boss'lu koşularda düşen}"""
    rows = []
    for r in fe_runs(runs):
        if r.get("inv"):
            t = fe_totals(run_whispers(r["effects"]))
            rows.append((t["luckI"], 1 + t["tQty"] / 100, sum(r["picks"].get(c, 0) for c in INV_IDS)))
    plain = [x for x in rows if x[0] <= 0]
    boss = [x for x in rows if x[0] > 0]
    b = sum(x[2] for x in plain) / (sum(x[1] for x in plain) + FE_INV_BASE_RUNS)
    got = sum(x[2] for x in boss)
    extra = got - sum(b * x[1] for x in boss)
    written = sum(x[0] * x[1] for x in boss)
    c = (extra + FE_INV_PRIOR) / (written + FE_INV_PRIOR)
    return {"b": b, "c": max(0.0, min(3.0, c)), "runs": len(rows), "boss_runs": len(boss), "got": got}


def _ppois(X, y, lam, np, prior=None, lams=None):
    """Cezalı Poisson regresyonu: y ~ Poisson(exp(a + X·c)), c önsele çekilir (varsayılan 1 = yazılı değer),
    a serbest. prior / lams: sütun başına önsel ve çekme gücü (etkileşim sütunları 0'a). -> (a, c)"""
    n, k = X.shape
    X1 = np.hstack([np.ones((n, 1)), X])
    prior = np.concatenate([[0.0], np.ones(k) if prior is None else np.asarray(prior, dtype=float)])
    P = np.diag([0.0] + ([lam] * k if lams is None else list(lams)))
    beta = prior.copy()
    beta[0] = math.log(y.mean() + 0.5) - float(X.mean(0) @ prior[1:])

    def obj(b):
        eta = np.clip(X1 @ b, -20, 20)
        return float(y @ eta - np.exp(eta).sum()) - 0.5 * float((b - prior) @ P @ (b - prior))

    cur = obj(beta)
    for _ in range(60):
        mu = np.exp(np.clip(X1 @ beta, -20, 20))
        grad = X1.T @ (y - mu) - P @ (beta - prior)
        H = X1.T @ (X1 * mu[:, None]) + P + 1e-9 * np.eye(k + 1)
        step = np.linalg.solve(H, grad)
        t = 1.0
        while t > 1e-4 and obj(beta + t * step) < cur:
            t /= 2
        beta = beta + t * step
        new = obj(beta)
        if abs(new - cur) < 1e-9:
            break
        cur = new
    return float(beta[0]), beta[1:]


def fe_fit(runs, base):
    """Memory materials koşularından öğrenme: her fısıltı türünün Scrap / Thread'e gerçek etkisi (katsayı;
    1 = yazdığı kadar). -> {"active", "runs", "scrap": {"a", "c"}, "thread": {...}}"""
    rows = [r for r in fe_runs(runs) if run_whispers(r["effects"])]
    info = {"active": len(rows) >= FE_FIT_MIN_RUNS, "runs": len(rows)}
    if not info["active"]:
        return info
    try:
        import numpy as np
    except ImportError:
        info["active"] = False
        return info
    for x, suf in (("scrap", "S"), ("thread", "T")):
        X = np.array([[_fe_feats(fe_totals(run_whispers(r["effects"])), suf, base[x])[k] for k in FE_FIT_KEYS]
                      for r in rows])
        y = np.array([float(r["picks"].get(SCRAP_ID, 0) if x == "scrap" else
                            sum(r["picks"].get(c, 0) for c in THREAD_IDS)) for r in rows])
        pairs = _int_pairs(X, len(rows), np, FE_INT_MIN_RUNS, FE_INT_MIN_EACH)
        cols = [X] + [(X[:, i] * X[:, j])[:, None] for i, j in pairs]
        nk = len(FE_FIT_KEYS)
        a, c = _ppois(np.hstack(cols), y, FE_FIT_LAMBDA[x], np,
                      prior=[1.0] * nk + [0.0] * len(pairs),
                      lams=[FE_FIT_LAMBDA[x]] * nk + [FE_INT_LAMBDA] * len(pairs))
        info[x] = {"a": a, "c": {k: float(min(3.0, max(0.0, v))) for k, v in zip(FE_FIT_KEYS, c[:nk])},
                   "inter": {(FE_FIT_KEYS[i], FE_FIT_KEYS[j]): float(max(-5.0, min(5.0, d)))
                             for (i, j), d in zip(pairs, c[nk:])}}
    return info


def fe_interactions(s, typical, min_fe=0.2):
    """Öğrenilen etkileşimler, okunur: [(metin, FE farkı)]. FE farkı = iki fısıltı (tipik değerleriyle) birlikte
    seçildiğinde, formülün tek tek etkilerden beklediğine göre koşu başına fazladan (+) / eksik (−) FE."""
    fit = s.get("fit")
    if not fit:
        return []
    kinds = {"S": {"app": "appS", "qty": "qtyS", "mult": "multS", "add": "flatS"},
             "T": {"app": "appT", "qty": "qtyT", "mult": "multT", "add": "flatT"}}
    out = []
    for x, suf, name in (("scrap", "S", "Scrap"), ("thread", "T", "Thread")):
        for (i, j), d in fit[x].get("inter", {}).items():
            ki = kinds[suf].get(i, i)
            kj = kinds[suf].get(j, j)
            effs = [(ki, typical.get(ki, 0.0)), (kj, typical.get(kj, 0.0))]
            no = dict(s, fit=dict(fit, **{x: dict(fit[x], inter={})}))
            diff = fe_score(effs, s) - fe_score(effs, no)
            if abs(diff) >= min_fe:
                label = f"{EFFECT_NAMES.get(ki, ki)} + {EFFECT_NAMES.get(kj, kj)}"
                out.append((f"{label}: {'better' if diff > 0 else 'worse'} together than expected "
                            f"({diff:+.2f} FE per run)", diff))
    return sorted(out, key=lambda t: -abs(t[1]))


def fe_typical(runs):
    """Memory materials koşularında her fısıltı türünün ortanca değeri (görülmediyse alev havuzunun ortası)."""
    seen = {}
    for r in fe_runs(runs):
        for k, v in run_whispers(r["effects"]):
            seen.setdefault(k, []).append(v)
    out = {}
    for side in (*FE_FLAME_POOL.values(), FE_BOSS_POOL, [("baseQ", FE_SWITCH_VALUES)]):
        for k, vals in side:
            out[k] = sorted(seen[k])[len(seen[k]) // 2] if seen.get(k) else vals[2]
    return out


def fe_score(effects, s):
    e = fe_expect(effects, s)
    return e["scrap"] * s["unit"]["scrap"] + e["thread"] * s["unit"]["thread"] + e["invite"] * s["unit"].get("invite", 0.0)


def is_fe_run(r):
    """Memory materials (FE) planıyla yapılan koşu: Red sayfasına ve kırmızı modeline girmez
    (Armament seçilmediği için Hero Memory sayısı o plana göre değil)."""
    return r.get("plan") == "fe"


def red_runs(runs):
    """Red memories planının koşuları (plan alanı olmayan v6.6 öncesi koşular dahil)."""
    return [r for r in runs if not is_fe_run(r)]


def fe_runs(runs):
    """Memory materials planıyla yapılan, toplanan Scrap/Thread sayısı bilinen ve sayımı tamamlanmış koşular."""
    return [r for r in runs if "picks" in r and is_fe_run(r) and r.get("tracked", True)]


def run_fe(r, prices):
    """Bir koşuda toplananların net FE değeri (fiyatı bilinmeyenler sayılmaz)."""
    return sum(n * prices[c] for c, n in r.get("picks", {}).items() if prices.get(c))


_FE_CACHE = {}


def fe_model(runs, prices, cache=True):
    """FE planının ayarları. prices: {eşya: net FE (vergi düşülmüş)}; fiyatı bilinmeyen None.
    -> {"base": {...}, "unit": {...}, "n_base": taban için kullanılan koşu, "mix": {thread: oran},
        "missing": [fiyatı olmayan eşyalar], "fit": öğrenilen model (FE_FIT_MIN_RUNS koşudan sonra)}"""
    if cache:      # overlay her çizimde soruyor: koşular ve fiyatlar değişmediyse aynı sonuç
        key = (tuple((r["t"], tuple(sorted(r["picks"].items())), len(r["effects"])) for r in fe_runs(runs)),
               tuple(sorted((k, v) for k, v in prices.items() if v is not None)))
        if _FE_CACHE.get("key") == key:
            return _FE_CACHE["val"]
        val = fe_model(runs, prices, cache=False)
        val["screens"] = fe_screens(runs)      # kalan seçimler hesabı için gerçek kart üçlüleri (fe_look)
        _FE_CACHE.update(key=key, val=val)
        return val
    recent = fe_runs(runs)[-FE_BASE_RUNS:]
    base = dict(FE_DEFAULTS)
    if recent:
        for x, suf in (("scrap", "S"), ("thread", "T")):
            vals = []
            for r in recent:
                got = r["picks"].get(SCRAP_ID, 0) if x == "scrap" else sum(r["picks"].get(c, 0) for c in THREAD_IDS)
                pre, add, post = _fe_factors(fe_totals(run_whispers(r["effects"])), suf)
                vals.append(max(0.0, (got / post - add) / pre))
            # tek bir koşu (çok çarpanlı ya da şanssız) tabanı bozmasın: varsayılanla ağırlıklı ortalama
            base[x] = (sum(vals) + FE_PRIOR_RUNS * FE_DEFAULTS[x]) / (len(vals) + FE_PRIOR_RUNS)
    mix = {c: sum(r["picks"].get(c, 0) for r in recent) for c in THREAD_IDS}
    if not sum(mix.values()):
        mix = {"5144": 1}               # henüz Thread toplanmadı: Epic varsayılır (yüksek seviyede hepsi Epic)
    priced = {c: n for c, n in mix.items() if n and prices.get(c)}
    missing = [FE_ITEMS[c] for c in [SCRAP_ID] + [c for c, n in mix.items() if n] if not prices.get(c)]
    thread_v = sum(n * prices[c] for c, n in priced.items()) / sum(priced.values()) if priced else 0.0
    total = sum(mix.values())
    # Invitation: düşenlerin türüne göre (henüz düşmediyse Craving); fiyatı olmayan tür sayılmaz
    inv_mix = {c: sum(r["picks"].get(c, 0) for r in fe_runs(runs)) for c in INV_IDS}
    inv_priced = {c: n for c, n in inv_mix.items() if n and prices.get(c)}
    inv_v = (sum(n * prices[c] for c, n in inv_priced.items()) / sum(inv_priced.values()) if inv_priced
             else prices.get(INV_DEFAULT) or 0.0)
    out = {"base": base, "unit": {"scrap": prices.get(SCRAP_ID) or 0.0, "thread": thread_v, "invite": inv_v},
           "n_base": len(recent), "mix": {c: n / total for c, n in mix.items() if n}, "missing": missing,
           "inv": fe_inv_fit(runs), "boss_rate": fe_boss_rate(runs), "switch_rule": fe_switch_rule(runs)}
    fit = fe_fit(runs, base)
    out["fit_info"] = fit
    if fit["active"]:
        out["fit"] = fit
    out["band"] = fe_band(runs, out, prices)
    return out


# Koşu tahmini (v7.3): seçili fısıltılarla haritadan ne kadar FE çıkar; koşu başında kayda yazılır ("est"), bitince gerçekle
# karşılaştırılır. Geçmişe dönük ölçüm (50 koşu, her biri sadece öncekilerle): ort. tahmin 18.4, gerçek 18.8 FE; ortalama
# sapma 3.4 FE; korelasyon 0.78. Tek koşu şansa bağlı olduğu için aralık gösterilir: Scrap ve Thread için ayrı ayrı son
# koşuların gerçek / tahmin oranının %10-%90 arası (Thread az sayıda ve toplu düştüğü için oransal olarak çok daha oynak,
# v7.3.1), Invitation için Poisson (düşüp düşmemesi tek başına büyük fark yaratıyor). Sapmalar hafifçe birlikte hareket ediyor
# (Scrap - Thread korelasyonu 0.2): birleştirme, bağımsız (kare toplamı) ile tam birlikte (düz toplam) arasının ortası.
# Ölçüm, görmediği 47 koşu: aralık içi %79; Thread payı yüksek dizilerde %73 -> %77 (tek oranlı eski hâle göre).
FE_BAND_MIN_RUNS = 8
FE_BAND_DEFAULT = {"scrap": (0.7, 1.35), "thread": (0.5, 1.7)}
FE_EST_MIN_RUNS = 3      # bu kadar kayıtlı tahmin olmadan isabet özeti yazılmaz


def _fe_core(e, s):
    return e["scrap"] * s["unit"]["scrap"] + e["thread"] * s["unit"]["thread"]


def fe_band(runs, s, prices):
    """Scrap ve Thread için gerçek / tahmin oranının olağan aralığı (son 40 koşu) -> {"scrap": (alt, üst), "thread": …}"""
    rows = {"scrap": [], "thread": []}
    for r in fe_runs(runs)[-40:]:
        eff = run_whispers(r["effects"])
        if not eff:
            continue
        e = fe_expect(eff, s)
        got = {"scrap": r["picks"].get(SCRAP_ID, 0), "thread": sum(r["picks"].get(c, 0) for c in THREAD_IDS)}
        for x in rows:
            if e[x] > 0:
                rows[x].append(got[x] / e[x])
    out = {}
    for x, vals in rows.items():
        if len(vals) < FE_BAND_MIN_RUNS:
            out[x] = FE_BAND_DEFAULT[x]
        else:
            vals.sort()
            out[x] = (vals[int(0.1 * len(vals))], vals[min(len(vals) - 1, int(0.9 * len(vals)))])
    return out


def _pois_q(lam, q):
    """Poisson dağılımının q yüzdeliği."""
    if lam <= 0:
        return 0
    n, p = 0, math.exp(-lam)
    total = p
    while total < q and n < 50:
        n += 1
        p *= lam / n
        total += p
    return n


def fe_estimate(effects, s):
    """Bu fısıltılarla bir koşuda beklenen -> {"scrap", "thread", "invite", "fe", "lo", "hi"}; fiyat yoksa None."""
    if s["unit"]["scrap"] <= 0:
        return None
    e = fe_expect(effects, s)
    u = s["unit"].get("invite", 0.0)
    band = s.get("band") or FE_BAND_DEFAULT
    down, up = [], []        # her parçanın tahminden sapması
    for x in ("scrap", "thread"):
        v = e[x] * s["unit"][x]
        down.append(v * max(0.0, 1 - band[x][0]))
        up.append(v * max(0.0, band[x][1] - 1))
    down.append(max(0.0, e["invite"] - _pois_q(e["invite"], 0.1)) * u)
    up.append(max(0.0, _pois_q(e["invite"], 0.9) - e["invite"]) * u)
    fe = _fe_core(e, s) + e["invite"] * u
    spread = lambda vals: (sum(vals) + math.hypot(*vals)) / 2
    return {"scrap": round(e["scrap"], 1), "thread": round(e["thread"], 2), "invite": round(e["invite"], 3),
            "fe": round(fe, 2), "lo": round(max(0.0, fe - spread(down)), 1), "hi": round(fe + spread(up), 1)}


def fe_est_value(est, s):
    """Kayıtlı tahminin bugünkü fiyatlarla değeri: fiyat değişimi tahmin hatası gibi görünmesin."""
    u = s["unit"]
    return est["scrap"] * u["scrap"] + est["thread"] * u["thread"] + est.get("invite", 0.0) * u.get("invite", 0.0)


def fe_est_line(est):
    return f"Expected: {est['fe']:.1f} FE (usually {est['lo']:.0f}–{est['hi']:.0f})"


def fe_gap_lines(r, s, prices, live=False):
    """Kayıtlı tahmin ile gerçek: [özet, ayrıntı]; tahmin yoksa boş."""
    est = r.get("est")
    if not est:
        return []
    act, want = run_fe(r, prices), fe_est_value(est, s)
    p = r.get("picks", {})
    scrap, thread, inv = p.get(SCRAP_ID, 0), sum(p.get(c, 0) for c in THREAD_IDS), sum(p.get(c, 0) for c in INV_IDS)
    if live:
        return [f"So far {act:.1f} of about {want:.1f} FE (usually {est['lo']:.0f}–{est['hi']:.0f})"]
    detail = f"Scrap {est['scrap']:.0f}→{scrap}, Thread {est['thread']:.1f}→{thread}"
    if inv or est.get("invite"):
        detail += f", Invitation {est.get('invite', 0.0):.2f}→{inv}"
    return [f"Expected {want:.1f} FE, got {act:.1f} FE ({act - want:+.1f})", detail]


def fe_est_stats(runs, s, prices):
    """Bitmiş ve tahmini kayıtlı koşularda isabet özeti; az koşuda boş."""
    gaps = [run_fe(r, prices) - fe_est_value(r["est"], s) for r in fe_runs(runs) if r.get("est")][-20:]
    if len(gaps) < FE_EST_MIN_RUNS:
        return ""
    bias = sum(gaps) / len(gaps)
    off = sum(abs(g) for g in gaps) / len(gaps)
    side = "higher" if bias > 0 else "lower"
    return f"Estimate vs actual, last {len(gaps)} runs: off by {off:.1f} FE on average, actual {abs(bias):.1f} FE {side}."


FE_KIND_NAMES = {"baseQ": "Base quantity", "tQty": "Treasure quantity", "app": "appear", "qty": "quantity",
                 "mult": "multiply", "add": "flat / lucky extra"}


def describe_fe_learned(fit):
    """Koşulardan öğrenilen ve yazılı değerden belirgin ayrılan etkiler (okunur)."""
    if not fit or not fit.get("active"):
        return ""
    parts = []
    for x, name in (("scrap", "Scrap"), ("thread", "Thread")):
        for k in FE_FIT_KEYS:
            c = fit[x]["c"][k]
            label = FE_KIND_NAMES[k] if k in ("baseQ", "tQty") else f"{name} {FE_KIND_NAMES[k]}"
            if k in ("baseQ", "tQty"):
                label += f" ({name})"
            if c >= 1.2:
                parts.append(f"{label} stronger than written (×{c:.2f})")
            elif c <= 0.8:
                parts.append(f"{label} weaker than written (×{c:.2f})")
    head = f"From your {fit['runs']} runs" + (" (early estimate)" if fit["runs"] < 20 else "")
    return head + ": " + ("; ".join(parts) if parts else "whispers work about as written.")


def fe_whisper_stats(runs, prices, bootstrap=True):
    """Memory materials: fısıltı türlerinin karşılaştırması (kırmızı planındaki tablonun FE hâli).
    [{key, n_with, n_without, avg_with, avg_without (FE / koşu), typical, model (bir tipik fısıltının boş bir koşuya
    kattığı FE), lo, hi, confidence}]. Rarity bu planda sayılmaz (confidence None)."""
    done = [r for r in fe_runs(runs) if run_whispers(r["effects"])]
    s = fe_model(runs, prices)
    rows = []
    for k in ("appS", "qtyS", "flatS", "luckS", "multS", "appT", "qtyT", "flatT", "luckT", "multT", "luckI",
              "baseQ", "tQty", "rarity"):
        vals, w, wo = [], [], []
        for r in done:
            wh = [v for kk, v in run_whispers(r["effects"]) if kk == k]
            vals += wh
            (w if wh else wo).append(r)
        if not vals:
            continue
        avg = lambda rs: sum(run_fe(r, prices) for r in rs) / len(rs) if rs else None
        rows.append({"key": k, "n_with": len(w), "n_without": len(wo), "avg_with": avg(w), "avg_without": avg(wo),
                     "typical": sorted(vals)[len(vals) // 2]})
    eff = lambda d, sx: fe_score([(d["key"], d["typical"])], sx) - fe_score([], sx)
    for d in rows:
        d["model"] = eff(d, s)
        d["lo"] = d["hi"] = d["model"]
    if bootstrap and len(done) >= FE_FIT_MIN_RUNS:
        import random
        rnd = random.Random(1)
        samples = {d["key"]: [] for d in rows}
        for _ in range(BOOTSTRAP_N):
            sb = fe_model([rnd.choice(done) for _ in done], prices, cache=False)
            for d in rows:
                samples[d["key"]].append(eff(d, sb))
        for d in rows:
            xs = sorted(samples[d["key"]])
            d["lo"], d["hi"] = xs[len(xs) // 10], xs[(len(xs) * 9) // 10]
    for d in rows:
        n_min = min(d["n_with"], d["n_without"])
        width, est = d["hi"] - d["lo"], abs(d["model"])
        if d["key"] == "rarity":
            d["confidence"] = None
        elif n_min < COMPARE_MIN_CONTRAST or len(done) < FE_FIT_MIN_RUNS:
            d["confidence"] = "Insufficient"
        elif d["lo"] <= 0.01 or width > est:
            d["confidence"] = "Low"
        elif n_min >= 10 and width <= 0.5 * est:
            d["confidence"] = "Good"
        else:
            d["confidence"] = "Preliminary"
    return sorted(rows, key=lambda d: (d["confidence"] in (None, "Insufficient"), -d["model"]))


def fe_combo_stats(runs, prices):
    """Aynı fısıltı türü kombinasyonu en az COMBO_MIN_RUNS Memory materials koşusunda görüldüyse:
    [(kısa yazım, koşu sayısı, ortalama FE)], ortalamaya göre."""
    grp = {}
    for r in fe_runs(runs):
        if run_whispers(r["effects"]):
            grp.setdefault(_combo_name(r["effects"]), []).append(run_fe(r, prices))
    return sorted(((n, len(v), sum(v) / len(v)) for n, v in grp.items() if len(v) >= COMBO_MIN_RUNS),
                  key=lambda c: -c[2])


def fe_flame_advice(picked, s, tiers=None, pair=None):
    """FE planında alevler (ekrandaki ikisi: Wealth / Boss / Wonder): o kalitedeki fısıltıların ortalama FE katkısı.
    -> (en iyi tür | "tie" | None, {tür: (ortalama FE artışı, yüzde)})"""
    if pair is not None and len(pair) < 2:
        if not pair or pair[0] == "?":
            return None, {}
        side = pair[0]
        base = fe_score(picked, s)
        gs = [fe_score(picked + [e], s) - base
              for e in flame_values(FE_BOSS_POOL if side == "boss" else FE_FLAME_POOL[side], (tiers or {}).get(side, DEFAULT_TIER))]
        g = sum(gs) / len(gs)
        return (side if g > 1e-9 else None), {side: (g, g / base * 100 if base > 0 else 0.0)}
    pool = FE_FLAME_POOL
    if pair and "boss" in pair:
        pool = {side: FE_BOSS_POOL if side == "boss" else FE_FLAME_POOL[side] for side in pair}
    return flame_advice(picked, s, tiers, pool=pool, score=fe_score)


def fe_change(before, effs, s):
    e0, e1 = fe_expect(before, s), fe_expect(before + effs, s)
    parts = []
    if e1["scrap"] - e0["scrap"] > 0.05:
        parts.append(f"Scrap {e0['scrap']:.0f}→{e1['scrap']:.0f}")
    if e1["thread"] - e0["thread"] > 0.005:
        parts.append(f"Thread {e0['thread']:.1f}→{e1['thread']:.1f}")
    if e1["invite"] - e0["invite"] > 0.005:
        parts.append(f"Invitation {e0['invite']:.2f}→{e1['invite']:.2f}")
    return ", ".join(parts) or "no effect on Scrap / Thread"


# --------------------------------------------------------------------------- #
# Kalan seçimler (v7.0): çarpanlar birbirini büyüttüğü için erken seçilen kartın değeri sonraki seçimlerle artar.
# Bir kartın değeri: onu alıp kalan seçimleri oynayınca koşu sonunda beklenen FE − bu seçimi hiç yapmamak.
# Sonraki ekranlar oyuncunun kendi koşularında gördüğü gerçek kart üçlüleri (offers), her ekranda o anki en iyi
# kart alınır; bütün kartlar aynı rastgele gelecekle karşılaştırılır. Son seçimde (5/6) gelecek yok: anlık kazanç.
# Doğrulama (31 koşu, 182 ekran; koşular ikiye bölünüp ekranlar bir yarıdan, oyun öteki yarıdan): kararların ~%18'i
# değişti, koşu başına +0.045 FE (+%0.21, ±0.013); öğrenilen modelde de yazılı formülde de aynı.
# Alev önerisi BİLEREK değişmedi: "3 karttan en iyisi" aynı verilerle +%4.5 görünürken ayrı verilerle −%3.8 çıktı
# (alev + kalite başına çok az ekran; aşırı uyum). Ekran sayısı artınca yeniden denenmeli.
# --------------------------------------------------------------------------- #
FE_LOOK_MIN_SCREENS = 30   # bundan az kayıtlı ekranda (yaklaşık 5 koşu) anlık kazanca bakılır
FE_LOOK_K = 40             # kart başına denenen gelecek sayısı
FE_LOOK_SWITCH = False     # gelecek ekranlara Switch kartı eklensin mi (koşu bölmeli testle seçilir)
_FE_LOOK_CACHE = {}


def fe_screens(runs):
    """Memory materials koşularında ekrana gelmiş gerçek kart üçlüleri: [[kart etkileri], …]."""
    out = []
    for r in fe_runs(runs):
        for o in r.get("offers", []):
            screen = [[(k, v) for k, v in effs] for _side, effs in o["cards"]]
            if FE_LOOK_SWITCH:           # gerçek oyunda her ekranda Switch de var
                v = o.get("switch") or switch_estimate(screen)
                if v is not None:
                    screen = screen + [[("baseQ", v)]]
            out.append(screen)
    return out


def fe_look_on(s, left):
    """Bu adımda kalan seçimler hesaba katılıyor mu?"""
    return left > 0 and len(s.get("screens") or ()) >= FE_LOOK_MIN_SCREENS


def _has_boss(effs):
    return any(k == "luckI" for k, *_ in effs)


def _boss_on(picked, cards, s):
    """Boss yolu denensin mi: fiyatı biliniyor ve Boss bu koşuda ya da oyuncunun ekranlarında görüldü."""
    if s["unit"].get("invite", 0.0) <= 0:
        return False
    return (_has_boss(picked) or any(_has_boss(c[1]) for c in cards)
            or any(_has_boss(e) for screen in s["screens"] for e in screen))


def _to_boss(e):
    """Boss yolunun gelecek ekranı: kayıtlı Wealth kartı yerine aynı kalitede Boss kartı (oyuncu alevini Boss seçseydi).
    Kalitesi anlaşılamayan Wealth kartı Rare sayılır."""
    if not e or not all(k in WEA_ONLY for k, *_ in e):
        return e
    k, v = e[0][0], e[0][1]
    vals = next((vs for kk, vs in FE_FLAME_POOL["wea"] if kk == k), ())
    tier = next((i for i, x in enumerate(vals) if abs(x - v) < 0.051), 2)
    return [("luckI", FE_BOSS_POOL[0][1][tier])]


def _fe_look_calc(picked, cards, s, left):
    """-> (en iyi kart | "tie" | None, kazançlar, yollar). yollar: {"wea": değer, "boss": değer} ya da None."""
    import random
    import zlib
    effs = [c[1] for c in cards]
    key = (repr((sig(picked), [sig(e) for e in effs], left)), len(s["screens"]), round(s["unit"]["scrap"], 6),
           round(s["unit"]["thread"], 6), round(s["base"]["scrap"], 4), round(s["base"]["thread"], 4), bool(s.get("fit")),
           round(s["unit"].get("invite", 0.0), 6), tuple(sorted((s.get("inv") or {}).items())),
           round(s.get("boss_rate", FE_BOSS_RATE), 3))
    if key in _FE_LOOK_CACHE:
        return _FE_LOOK_CACHE[key]
    # aynı durumda hep aynı sonuç: overlay her çizimde soruyor, öneri kendi kendine değişmesin
    rng = random.Random(zlib.crc32(key[0].encode("utf-8")))
    futures = [[rng.choice(s["screens"]) for _ in range(left)] for _ in range(FE_LOOK_K)]
    # İki yol: gelecek ekranlar kayıtlı hâliyle (Wealth + Wonder) ya da Boss alevi bulunan seçimlerde Wealth kartları
    # Boss'a çevrilmiş (Boss + Wonder).
    # Kartın değeri, onu alınca hangi yol iyiyse onun koşu sonu FE'si: seçilenler ve kalan hak kararı değiştirir.
    lanes = {"wea": futures}
    if _boss_on(picked, cards, s):
        # Boss alevi her seçimde yok: olmadığı seçimde ekran kayıtlı hâliyle kalır
        rate = s.get("boss_rate", FE_BOSS_RATE)
        rng2 = random.Random(zlib.crc32(key[0].encode("utf-8")) + 1)
        lanes["boss"] = [[[_to_boss(e) for e in screen] if rng2.random() < rate else screen for screen in fut]
                         for fut in futures]

    def final(start, fs):
        total = 0.0
        for fut in fs:
            q = start
            for screen in fut:
                q = q + max(screen, key=lambda e: fe_score(q + e, s))
            total += fe_score(q, s)
        return total / len(fs)
    base = max(final(picked, fs) for fs in lanes.values())
    vals = {name: [final(picked + e, fs) for e in effs] for name, fs in lanes.items()}
    gains = []
    for i, e in enumerate(effs):
        g = max(v[i] for v in vals.values()) - base if any(k in FE_KEYS for k, *_ in e) else 0.0
        gains.append((g, g / base * 100 if base > 0 else 0.0))
    order = sorted(range(len(gains)), key=lambda i: gains[i][0], reverse=True)
    top = gains[order[0]][0]
    paths = {name: max(v) for name, v in vals.items()} if "boss" in lanes else None
    if top <= 1e-9:
        res = None, gains, paths
    elif len(order) > 1 and top - gains[order[1]][0] <= top * 0.03:
        res = "tie", gains, paths
    else:
        res = order[0], gains, paths
    if len(_FE_LOOK_CACHE) > 32:
        _FE_LOOK_CACHE.clear()
    _FE_LOOK_CACHE[key] = res
    return res


def fe_look(picked, cards, s, left):
    """recommend() gibi -> (en iyi kart | "tie" | None, [(koşu sonuna kadarki artış, yüzde)]).
    Son seçimde ya da yeterli kayıtlı ekran yoksa recommend()'in anlık kazancı."""
    if not cards or not fe_look_on(s, left):
        return recommend(picked, cards, s, score=fe_score)
    return _fe_look_calc(picked, cards, s, left)[:2]


def fe_paths_note(picked, cards, s, left):
    """Boss + Wonder mu Wealth + Wonder mı: bu seçimden koşu sonuna beklenen FE (Boss görülmediyse boş)."""
    if not cards or not fe_look_on(s, left):
        return ""
    paths = _fe_look_calc(picked, cards, s, left)[2]
    if not paths:
        return ""
    a, b = paths["boss"], paths["wea"]
    if abs(a - b) <= max(a, b) * 0.03:
        return f"Plan: Boss + Wonder and Wealth + Wonder are about equal (about {a:.1f} FE by the end of the run)."
    won, lost = ("Boss", "Wealth") if a > b else ("Wealth", "Boss")
    return (f"Plan: {won} + Wonder, about {max(a, b):.1f} FE by the end of the run "
            f"({lost} + Wonder about {min(a, b):.1f} FE).")


def fe_inv_note(s):
    """Boss / Invitation hesabının dayanağı: öğrenilen oran ya da yazılı değer."""
    inv = s.get("inv") or {}
    rate = f" A Boss flame is expected on about {s.get('boss_rate', FE_BOSS_RATE) * 100:.0f}% of picks."
    if not inv.get("boss_runs"):
        return "Boss value is an estimate: chance × Invitation price. It learns as your runs count Invitations." + rate
    return (f"From your {inv['boss_runs']} Boss runs: {inv['got']} Invitation(s), "
            f"×{inv['c']:.2f} of the written chance." + rate)


def fe_flame_note(pair, selected):
    """Fısıltı vermeyen alev varsa panelde söylenecek; yoksa boş."""
    if pair is None or len(pair) == 2:
        return ""
    if pair == ("?",):
        return "The selected flame gives no whisper. Select the other flame and press “Read”."
    if not pair:
        return "No whisper flame here: the next cards are random."
    return "The other flame gives no whisper." if selected in pair else "The selected flame gives no whisper  → change it"


def fe_progress(count, s):
    """Panelde kaçıncı seçimde olunduğu."""
    pick = min(6, count + 1)
    if pick == 6:
        return "Last pick (6 of 6): only what it adds now counts."
    n = 6 - pick
    return f"Pick {pick} of 6: {n} more after this" + (", values count them too." if fe_look_on(s, n) else ".")


def fe_reason_cards(picked, cards, best, gains, labels, s, left=0):
    if not cards:
        return ""
    if best is None:
        return "Why: none of the cards adds Scrap or Threads."
    order = sorted(range(len(gains)), key=lambda i: -gains[i][0])
    if best == "tie":
        a, b = order[0], order[1]
        return f"Why: {labels[a]} ({gains[a][0]:+.1f} FE) and {labels[b]} ({gains[b][0]:+.1f} FE) are almost equal."
    look = fe_look_on(s, left)
    txt = (f"Why: {labels[best]} → {fe_change(picked, cards[best][1], s)}; {gains[best][0]:+.1f} FE "
           f"{'by the end of the run' if look else 'per run'}.")
    # önerilmeyen kartların hepsi (v6.9.1'den önce sadece ikinci kart yazıyordu, üçüncünün sebebi görünmüyordu)
    for o in order[1:]:
        txt += f" {labels[o]} only {gains[o][0]:+.1f} FE ({fe_change(picked, cards[o][1], s)})."
    if look:              # öneri, şu an en çok kazandıran kart değilse sebebi
        _b, now = recommend(picked, cards, s, score=fe_score)
        o = max(range(len(now)), key=lambda i: now[i][0])
        if o != best and now[o][0] > now[best][0]:
            txt += (f" {labels[o]} adds more right now ({now[o][0]:+.1f} vs {now[best][0]:+.1f} FE), "
                    f"but your next picks multiply {labels[best]} more.")
    return txt


FE_LEARN_MIN = 5   # bir fısıltı türü bundan az Memory materials koşusunda görüldüyse "az gördüm" notu çıkar


def fe_learn_counts(runs):
    """-> ({tür: o türün görüldüğü koşu sayısı}, fısıltılı Memory materials koşu sayısı)"""
    done = [r for r in fe_runs(runs) if run_whispers(r["effects"])]
    cnt = {}
    for r in done:
        for k in {k for k, *_ in run_whispers(r["effects"])}:
            cnt[k] = cnt.get(k, 0) + 1
    return cnt, len(done)


def fe_learn_note(cards, best, labels, counts, n):
    """Az görülen (Scrap / Thread'e etkisi olan) türü taşıyan kartlar için not: seçilirse model daha iyi öğrenir.
    Önerilmeyen bir kart az görülen türü taşıyorsa daha az FE gösterse de onu seçmek öğrenmeye yardım eder."""
    rare = {}                                   # kart sırası -> az görülen türleri
    for i, (_side, effs, *_x) in enumerate(cards):
        ks = list(dict.fromkeys(k for k, *_ in effs if k in FE_KEYS and counts.get(k, 0) < FE_LEARN_MIN))
        if ks:
            rare[i] = ks
    if not rare:
        return ""
    others = [i for i in rare if i != best]    # önerilmeyen kartlar öncelikli (asıl yardım onlarda)
    kinds = list(dict.fromkeys(k for i in (others or list(rare)) for k in rare[i]))
    seen = ", ".join(f"{EFFECT_NAMES.get(k, k)} ({counts.get(k, 0)} of {n} runs)" if counts.get(k, 0)
                     else f"{EFFECT_NAMES.get(k, k)} (not seen yet)" for k in kinds)
    if not others:
        return f"Learning: few runs so far with {seen}. Picking {labels[best]} also helps the app learn."
    others = [labels[i] for i in others]
    return (f"Learning: few runs so far with {seen}. Picking {' or '.join(others)} helps the app learn, "
            "even if it shows less FE now.")


def fe_reason_flame(picked, out, best, tiers=None):
    if not out:
        return ""
    if best is None:
        return "Why: neither flame adds FE."
    if len(out) == 1:
        a = next(iter(out))
        return f"Why: on average a {flame_label(a, tiers)} whisper adds {out[a][0]:+.1f} FE."
    a, b = list(out)
    txt = (f"Why: on average a {flame_label(a, tiers)} whisper adds {out[a][0]:+.1f} FE, "
           f"a {flame_label(b, tiers)} whisper {out[b][0]:+.1f} FE"
           + (" (Wonder multiplies what you already have; its Rarity kind counts as 0)." if "won" in out else "."))
    if best == "tie":
        txt += " Their averages are almost the same."
    return txt


# --------------------------------------------------------------------------- #
# Kayıttan öğrenme
#   memory: her koşuda toplanan memory sayısı Poisson regresyonuyla (log bağlantı) etkilere oturtulur.
#           Ayrıca her koşu başarılı/başarısız etiketlenir: kırmızısız ve memory ≤ eşik ise başarısız.
#           Eşik sabit değil: kırmızısız koşuların ortanca memory sayısı, her güncellemede yeniden.
#           İki kanıt birlikte kullanılır (sayı + etiket); kırmızılı koşu az memory'yle cezalandırılmaz.
#           Katsayılar elle kurulan modele (FIT_PRIOR) doğru çekilir, az veride ondan uzaklaşmaz.
#   kırmızı: rarity üssü Poisson olabilirliğiyle bulunur (önsel: ayardaki üs); kırmızı yokken önsel kalır.
# --------------------------------------------------------------------------- #
FIT_MIN_RUNS = 5      # bundan az koşuda öneriler elle kurulan modelle yapılır
# Etkileşimler (iki fısıltı türü birlikte formülün söylediğinden fazla / az memory getiriyor mu): bir çift ancak hem
# birlikte hem ayrı ayrı yeterince koşuda görüldüyse modele girer; katsayı 0'dan başlar, güçlü çekilir.
INT_MIN_RUNS = 30
INT_MIN_EACH = 12     # simülasyonla seçildi (8 / 12 ve çekim 20 / 35 / 50 denendi)
INT_LAMBDA = 20.0
FIT_LAMBDA = 20.0     # önsele çekme gücü: simülasyonla seçildi (v6.7; 6/12/20/35/60/100 denendi, 20 en az
                      # kayıp: yazıldığı gibi çalışan dünyada az koşuda sapmaz, sapan dünyada 30–60 koşuda öğrenir)
TARGET_KINDS = ["baseQ", "tQty", "appear", "memQty", "flat", "luck", "rarity"]


def fit_model(runs, s):
    rows = []
    for r in runs:
        w = run_whispers(r["effects"])
        # yarıda kalan koşu (sürüyor ya da uygulama koşu ortasında kapandı) sayımı eksik olabilir
        if w and r.get("tracked", True):
            rows.append((totals(w), r["memories"], r.get("red", 0)))
    thr = success_threshold([m for _, m, red in rows if not red])
    ok = sum(1 for _, m, red in rows if red or m > thr)
    info = {"runs": len(rows), "memories": sum(m for _, m, _ in rows), "red": sum(x for *_, x in rows),
            "c": dict(FIT_PRIOR), "k": s["vs_k"], "p0": s["vs_p0"] / 100,
            "thr": thr, "ok": ok, "fail": len(rows) - ok,
            "active": len(rows) >= FIT_MIN_RUNS, "t": time.strftime("%Y-%m-%d %H:%M")}
    if not info["active"]:
        return info
    try:
        import numpy as np
    except ImportError:
        info["active"] = False
        return info
    X = np.array([[_features(t, s["vs_base_mem"])[k] for k in FIT_KEYS] for t, _, _ in rows])
    y = np.array([m for _, m, _ in rows], dtype=float)
    has_red = np.array([bool(red) for *_, red in rows])
    pairs = _int_pairs(X, len(rows), np, INT_MIN_RUNS, INT_MIN_EACH)
    nk = len(FIT_KEYS)
    Xa = np.hstack([X] + [(X[:, i] * X[:, j])[:, None] for i, j in pairs])
    c = _poisson_fit(Xa, y, np, thr=thr, has_red=has_red,
                     prior=[FIT_PRIOR[k] for k in FIT_KEYS] + [0.0] * len(pairs),
                     lams=[FIT_LAMBDA] * nk + [INT_LAMBDA] * len(pairs))
    info["c"] = {k: float(min(3.0, max(0.0, v))) for k, v in zip(FIT_KEYS, c[:nk])}
    # JSON'a yazılabilsin: çift anahtarı "tür*tür"
    info["inter"] = {f"{FIT_KEYS[i]}*{FIT_KEYS[j]}": float(max(-5.0, min(5.0, d))) for (i, j), d in zip(pairs, c[nk:])}
    # rarity üssü: kırmızı sayısı ~ Poisson(p0 × memory × (1+rarity)^k), p0 profillenir
    m = np.array([m for _, m, _ in rows], dtype=float)
    red = np.array([x for *_, x in rows], dtype=float)
    lr = np.array([math.log(1 + t["rarity"] / 100) for t, _, _ in rows])
    R = red.sum()
    best_k, best_ll = s["vs_k"], -1e18
    for k in np.arange(0.0, 4.01, 0.1):
        ll = -((k - s["vs_k"]) ** 2) / 2
        if R > 0:
            ll += float((red * k * lr).sum() - R * math.log((m * np.exp(k * lr)).sum()))
        if ll > best_ll:
            best_k, best_ll = float(k), ll
    info["k"] = round(best_k, 2)
    # kırmızı oranı (toplanan memory'ler içinde): önsel 50 memory ağırlığında
    info["p0"] = float((R + s["vs_p0"] / 100 * 50) / ((m * np.exp(best_k * lr)).sum() + 50))
    return info


def _int_pairs(X, n, np, min_runs, min_each):
    """Etkileşimine bakılabilecek tür çiftleri (sütun sıraları): yeterli koşu ve her durumda yeterli örnek."""
    if n < min_runs:
        return []
    on = X > 1e-9
    out = []
    for i in range(X.shape[1]):
        for j in range(i + 1, X.shape[1]):
            both = int((on[:, i] & on[:, j]).sum())
            only_i = int((on[:, i] & ~on[:, j]).sum())
            only_j = int((~on[:, i] & on[:, j]).sum())
            if min(both, only_i, only_j) >= min_each:
                out.append((i, j))
    return out


def red_interactions(s, typical, min_mem=0.3):
    """Kırmızı planı: öğrenilen etkileşimler, okunur: [(metin, memory farkı)]. Fark = iki fısıltı (tipik
    değerleriyle) birlikte seçildiğinde, tek tek etkilerden beklenene göre koşu başına fazladan (+) / eksik (−) memory."""
    fit = s.get("fit")
    if not fit or not fit.get("inter"):
        return []
    out = []
    no = dict(s, fit=dict(fit, inter={}))
    for pair in fit["inter"]:
        i, j = pair.split("*")
        effs = [(i, typical.get(i, 0.0)), (j, typical.get(j, 0.0))]
        diff = breakdown(effs, s)[0] - breakdown(effs, no)[0]
        if abs(diff) >= min_mem:
            label = f"{EFFECT_NAMES[i]} + {EFFECT_NAMES[j]}"
            out.append((f"{label}: {'more' if diff > 0 else 'fewer'} Hero Memories together than expected "
                        f"({diff:+.1f} per run)", diff))
    return sorted(out, key=lambda t: -abs(t[1]))


def success_threshold(counts):
    """Başarı eşiği: kırmızısız koşuların ortanca memory sayısı (aşağı yuvarlanır).
    Bu sayı ve altı başarısız. Veri değiştikçe eşik de değişir."""
    if not counts:
        return 0
    c = sorted(counts)
    n = len(c)
    return int(c[n // 2] if n % 2 else (c[n // 2 - 1] + c[n // 2]) / 2)


def _poisson_fit(X, y, np, thr=None, has_red=None, prior=None, lams=None):
    """Cezalı Poisson regresyonu (Gauss-Newton): memory ~ Poisson(μ = exp(a + X·c)), c önsele çekilir.
    a (kişinin taban seviyesi: loot filtresi, toplama alışkanlığı) serbest.
    thr verilirse başarı etiketi de olabilirliğe girer: başarılı = kırmızı var ya da memory > thr,
    P(başarılı) = P(memory > thr | μ). Kırmızılı koşuların sayısı kullanılmaz (az memory'yle cezalanmaz). -> c"""
    n, k = X.shape
    X1 = np.hstack([np.ones((n, 1)), X])
    prior = np.concatenate([[0.0], [FIT_PRIOR[key] for key in FIT_KEYS] if prior is None else list(prior)])
    P = np.diag([0.0] + ([FIT_LAMBDA] * k if lams is None else list(lams)))
    red = has_red if has_red is not None else np.zeros(n, dtype=bool)
    w_cnt = (~red).astype(float)                       # sayı kanıtı: kırmızısız koşular
    lab = thr is not None
    succ = (red | (y > thr)).astype(float) if lab else None
    beta = prior.copy()
    beta[0] = math.log(y.mean() + 0.5) - float(X.mean(0) @ prior[1:])
    lg = math.lgamma(thr + 1) if lab else 0.0

    def label_terms(mu):
        """S = P(memory > thr), dS/dη = μ·pmf(thr; μ)"""
        cdf = np.zeros_like(mu)
        for j in range(thr + 1):
            cdf += np.exp(-mu + j * np.log(mu) - math.lgamma(j + 1))
        S = np.clip(1 - cdf, 1e-9, 1 - 1e-9)
        dS = mu * np.exp(-mu + thr * np.log(mu) - lg)
        return S, dS

    def obj(b):
        eta = np.clip(X1 @ b, -20, 20)
        mu = np.exp(eta)
        v = float(w_cnt @ (y * eta - mu))
        if lab:
            S, _ = label_terms(mu)
            v += float(succ @ np.log(S) + (1 - succ) @ np.log(1 - S))
        return v - 0.5 * float((b - prior) @ P @ (b - prior))

    cur = obj(beta)
    for _ in range(60):
        mu = np.exp(np.clip(X1 @ beta, -20, 20))
        g_eta = w_cnt * (y - mu)
        W = w_cnt * mu
        if lab:
            S, dS = label_terms(mu)
            g_eta = g_eta + dS * (succ - S) / (S * (1 - S))
            W = W + dS ** 2 / (S * (1 - S))
        grad = X1.T @ g_eta - P @ (beta - prior)
        H = X1.T @ (X1 * W[:, None]) + P + 1e-9 * np.eye(k + 1)
        step = np.linalg.solve(H, grad)
        t = 1.0
        while t > 1e-4 and obj(beta + t * step) < cur:
            t /= 2
        beta = beta + t * step
        new = obj(beta)
        if abs(new - cur) < 1e-9:
            break
        cur = new
    return beta[1:]


def typical_values(runs):
    """Her etki türünün kayıtta görülen ortalama değeri (görülmediyse alev havuzunun ortalaması)."""
    seen = {}
    for r in runs:
        for k, v, *_ in run_whispers(r["effects"]):
            seen.setdefault(k, []).append(v)
    out = {}
    for k in TARGET_KINDS:
        if seen.get(k):
            out[k] = sum(seen[k]) / len(seen[k])
        else:
            vals = [v for pool in FLAME_POOL.values() for kk, vs in pool for v in vs[2:] if kk == k]
            out[k] = sum(vals) / len(vals)
    return out


def optimal_combo(runs, s, top=3):
    """Modele göre kırmızı beklentisini en çok artıran 6'lı fısıltı kombinasyonları (tipik değerlerle).
    -> [([(tür, adet)], fısıltısıza göre kat)], en iyisi önce"""
    import itertools
    vals = typical_values(runs)
    base = red_score([], s)
    scored = sorted(((red_score([(k, vals[k]) for k in combo], s), combo)
                     for combo in itertools.combinations_with_replacement(TARGET_KINDS, 6)), reverse=True)
    return [([(k, combo.count(k)) for k in TARGET_KINDS if k in combo], sc / base if base > 0 else 0.0)
            for sc, combo in scored[:top]]


def describe_combo(counts):
    return ", ".join(f"{n}× {EFFECT_NAMES[k]}" for k, n in counts)


def describe_learned(info):
    """Kayıttan öğrenilen ve elle kurulan modelden belirgin ayrılan etkiler."""
    parts = []
    for k in FIT_KEYS:
        c, prior = info["c"][k], FIT_PRIOR[k]
        if c - prior >= 0.15:
            parts.append(f"{EFFECT_NAMES[k]} stronger than expected (×{c:.2f})")
        elif prior - c >= 0.15:
            parts.append(f"{EFFECT_NAMES[k]} weaker than expected (×{c:.2f})")
    if info["red"] and abs(info["k"] - DEFAULTS["vs_k"]) >= 0.3:
        parts.append(f"rarity exponent {info['k']:g}")
    return "From your log: " + "; ".join(parts) if parts else ""


SHORT_NAMES = {"appear": "Appear", "memQty": "Mem qty", "flat": "Flat", "luck": "Lucky", "baseQ": "Base qty",
               "tQty": "Treasure", "rarity": "Rarity", "rep": "Replication", "none": "None",
               "appS": "Scrap app.", "qtyS": "Scrap qty", "flatS": "Scrap flat", "luckS": "Scrap lucky",
               "multS": "Scrap ×", "appT": "Thread app.", "qtyT": "Thread qty", "flatT": "Thread flat",
               "luckT": "Thread lucky", "multT": "Thread ×", "luckI": "Invitation"}
COMPARE_MIN_RUNS = 5     # bir türün olduğu ve olmadığı koşulardan en az bu kadar yoksa sonuç "az veri"


COMPARE_MIN_CONTRAST = 3     # türün olduğu ya da olmadığı koşu bundan azsa etki ayrılamaz: "Insufficient"
RED_MIN_TOTAL = 5            # kırmızı karşılaştırması için kayıtta en az bu kadar kırmızı olmalı
COMBO_MIN_RUNS = 3           # bir kombinasyon en az bu kadar koşuda görüldüyse karşılaştırılır
BOOTSTRAP_N = 40             # güven aralığı için yeniden örnekleme sayısı


def valid_runs(runs):
    """Karşılaştırmaya giren koşular: fit_model'le aynı kural (fısıltısı okunmuş, sayımı yarıda kalmamış).
    -> (geçerli koşular, [(koşu, neden dışarıda)])"""
    ok, out = [], []
    for r in runs:
        if not run_whispers(r["effects"]):
            out.append((r, "whispers not read"))
        elif not r.get("tracked", True):
            out.append((r, "run still in progress or the app was closed during the run"))
        else:
            ok.append(r)
    return ok, out


def _effect(k, typical, s):
    return breakdown([(k, typical)], s)[0] - breakdown([], s)[0]


def whisper_stats(runs, settings, bootstrap=True):
    """Fısıltı türlerinin karşılaştırması (sadece geçerli koşular):
    [{key, n_with, n_without, avg_with, avg_without, red_with, red_without, typical, model, lo, hi, confidence}]
    avg_* / red_*: o türün olduğu / olmadığı koşulardaki ortalama memory / kırmızı (türler birlikte
    seçildiği için tek başına yanıltabilir). model: öğrenilen modele göre o türden tek bir tipik fısıltının
    (kayıttaki ortanca değer) boş bir koşuya kattığı memory; model türleri birbirinden ayırır.
    lo/hi: koşular yeniden örneklenip model tekrar kurulunca bu tahminin %10–%90 aralığı.
    confidence: Insufficient / Low / Preliminary / Good. Rarity memory sayısını değiştirmez (model 0)."""
    done, _ = valid_runs(runs)
    s = model_settings(settings)
    rows = []
    for k in ("appear", "memQty", "flat", "luck", "baseQ", "tQty", "rarity"):
        vals, w, wo = [], [], []
        for r in done:
            wh = [v for kk, v in run_whispers(r["effects"]) if kk == k]
            vals += wh
            (w if wh else wo).append(r)
        if not vals:
            continue
        avg = lambda rs, f: sum(f(r) for r in rs) / len(rs) if rs else None
        rows.append({"key": k, "n_with": len(w), "n_without": len(wo),
                     "avg_with": avg(w, lambda r: r["memories"]), "avg_without": avg(wo, lambda r: r["memories"]),
                     "red_with": avg(w, lambda r: r["red"]), "red_without": avg(wo, lambda r: r["red"]),
                     "typical": sorted(vals)[len(vals) // 2]})
    for d in rows:
        d["model"] = _effect(d["key"], d["typical"], s)
        d["lo"] = d["hi"] = d["model"]
    if bootstrap and len(done) >= FIT_MIN_RUNS:
        import random
        rnd = random.Random(1)                     # aynı kayıtta hep aynı sonuç
        s0 = {k: float(settings.get(k, v)) for k, v in DEFAULTS.items()}
        samples = {d["key"]: [] for d in rows}
        for _ in range(BOOTSTRAP_N):
            info = fit_model([rnd.choice(done) for _ in done], s0)
            sb = dict(s0, fit=info) if info["active"] else s0
            for d in rows:
                samples[d["key"]].append(_effect(d["key"], d["typical"], sb))
        for d in rows:
            xs = sorted(samples[d["key"]])
            d["lo"], d["hi"] = xs[len(xs) // 10], xs[(len(xs) * 9) // 10]
    for d in rows:
        n_min = min(d["n_with"], d["n_without"])
        width, est = d["hi"] - d["lo"], abs(d["model"])
        if d["key"] == "rarity":
            d["confidence"] = None                  # memory'ye etkisi yok; kırmızı tarafı ayrı
        elif n_min < COMPARE_MIN_CONTRAST:
            d["confidence"] = "Insufficient"
        elif d["lo"] <= 0.05 or width > est:
            d["confidence"] = "Low"
        elif n_min >= 10 and width <= 0.5 * est:
            d["confidence"] = "Good"
        else:
            d["confidence"] = "Preliminary"
    return sorted(rows, key=lambda d: (d["confidence"] in (None, "Insufficient"), -d["model"]))


def combo_stats(runs):
    """Aynı tür kombinasyonu (değerler hariç, adetler dahil) en az COMBO_MIN_RUNS koşuda görüldüyse:
    [(kısa yazım, koşu sayısı, ortalama memory, toplam kırmızı)], ortalamaya göre."""
    done, _ = valid_runs(runs)
    grp = {}
    for r in done:
        cnt = {}
        for k, _v in run_whispers(r["effects"]):
            cnt[k] = cnt.get(k, 0) + 1
        key = tuple(sorted(cnt.items()))
        grp.setdefault(key, []).append(r)
    out = []
    for key, rs in grp.items():
        if len(rs) >= COMBO_MIN_RUNS:
            name = " + ".join(SHORT_NAMES.get(k, k) + (f" ×{n}" if n > 1 else "") for k, n in key)
            out.append((name, len(rs), sum(r["memories"] for r in rs) / len(rs), sum(r["red"] for r in rs)))
    return sorted(out, key=lambda c: -c[2])


# --------------------------------------------------------------------------- #
# Sonuç paylaşımı (GitHub Discussions · Show and tell): koşu kaydının özeti, Markdown
# Kişisel bilgi yok: sadece fısıltı türleri, sayılar, tarih (gün), fiyatlar.
# --------------------------------------------------------------------------- #
def _md_table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _combo_name(effects):
    cnt = {}
    for k, _v in run_whispers(effects):
        cnt[k] = cnt.get(k, 0) + 1
    return " + ".join(SHORT_NAMES.get(k, k) + (f" ×{n}" if n > 1 else "") for k, n in sorted(cnt.items())) or "not read"


def share_summary(runs, plan, settings, prices, version):
    """-> (başlık, Markdown özet) ya da (None, neden) — paylaşacak koşu yoksa."""
    if plan == "fe":
        frs = fe_runs(runs)
        if not frs:
            return None, "No finished Memory materials runs yet."
        s = fe_model(runs, prices)
        vals = [run_fe(r, prices) for r in frs]
        scrap = [r["picks"].get(SCRAP_ID, 0) for r in frs]
        thr = {c: sum(r["picks"].get(c, 0) for r in frs) for c in THREAD_IDS}
        grp = {}
        for r, v in zip(frs, vals):
            grp.setdefault(_combo_name(r["effects"]), []).append(v)
        combos = sorted(((n, len(v), sum(v) / len(v)) for n, v in grp.items() if len(v) >= 2), key=lambda c: -c[2])
        lines = [f"**Plan:** Memory materials (Wealth or Boss + Wonder) · **App:** v{version}", "",
                 f"- Runs: **{len(frs)}** · FE earned: **{sum(vals):.1f}** · FE per run: **{sum(vals) / len(vals):.1f}** "
                 f"(after tax)",
                 f"- Per run: {sum(scrap) / len(frs):.0f} Memory Scrap · Threads Magic / Rare / Epic: "
                 + " / ".join(f"{thr[c] / len(frs):.1f}" for c in THREAD_IDS),
                 f"- Prices after tax: Scrap {s['unit']['scrap']:.3f} FE · Thread {s['unit']['thread']:.3f} FE "
                 f"(average of the qualities picked up)", ""]
        rows = [d for d in fe_whisper_stats(runs, prices, bootstrap=False) if d["key"] != "rarity"]
        if rows and s.get("fit"):
            f1 = lambda v: "—" if v is None else f"{v:.1f}"
            lines += ["**Whisper types compared** (FE per run, learned from the runs)", "",
                      _md_table(["Whisper type", "Runs with / w/o", "FE with", "FE w/o", "Effect (FE / run)", "Typical"],
                                [(EFFECT_NAMES[d["key"]], f"{d['n_with']} / {d['n_without']}", f1(d["avg_with"]),
                                  f1(d["avg_without"]), f"{d['model']:+.2f}", f"{d['typical']:g}") for d in rows]),
                      "", describe_fe_learned(s["fit"]), ""]
            inter = fe_interactions(s, fe_typical(runs))
            if inter:
                lines += ["**Whisper pairs that work differently together than expected**", ""]
                lines += [f"- {txt}" for txt, _ in inter[:5]] + [""]
        if combos:
            lines += ["**Whisper combinations seen in 2+ runs** (average FE per run)", "",
                      _md_table(["Whispers", "Runs", "FE / run"], [(n, k, f"{a:.1f}") for n, k, a in combos[:8]]), ""]
        lines += ["**Last runs**", "",
                  _md_table(["Date", "Whispers", "Scrap", "Thread M / R / E", "FE"],
                            [(r["t"][:10], _combo_name(r["effects"]), r["picks"].get(SCRAP_ID, 0),
                              " / ".join(str(r["picks"].get(c, 0)) for c in THREAD_IDS), f"{v:.1f}")
                             for r, v in list(zip(frs, vals))[-10:][::-1]])]
        return f"Memory materials: {sum(vals) / len(vals):.1f} FE per run over {len(frs)} run{'s' * (len(frs) != 1)}", "\n".join(lines)

    rr = red_runs(runs)
    done, _ = valid_runs(rr)
    if not done:
        return None, "No finished Red memories runs yet."
    mem = sum(r["memories"] for r in done)
    red = sum(r["red"] for r in done)
    lines = [f"**Plan:** Red memories (Armament + Wonder) · **App:** v{version}", "",
             f"- Runs: **{len(done)}** · Hero Memories: **{mem}** ({mem / len(done):.1f} per run) · "
             f"Red: **{red}** ({red / len(done):.2f} per run)", ""]
    rows = []
    for d in whisper_stats(rr, settings, bootstrap=False):
        f1 = lambda v: "—" if v is None else f"{v:.1f}"
        eff = ("no effect on count" if d["key"] == "rarity" else
               "—" if d["confidence"] == "Insufficient" else f"+{d['model']:.1f}")
        rows.append((EFFECT_NAMES[d["key"]], f"{d['n_with']} / {d['n_without']}", f1(d["avg_with"]),
                     f1(d["avg_without"]), eff, d["confidence"] or "—"))
    if rows:
        lines += ["**Whisper types compared** (Hero Memories per run)", "",
                  _md_table(["Whisper type", "Runs with / w/o", "Avg with", "Avg w/o", "Estimated effect",
                             "Confidence"], rows), ""]
    inter = red_interactions(model_settings(settings), typical_values(rr))
    if inter:
        lines += ["**Whisper pairs that work differently together than expected**", ""]
        lines += [f"- {txt}" for txt, _ in inter[:5]] + [""]
    combos = combo_stats(rr)
    if combos:
        lines += [f"**Combinations seen in {COMBO_MIN_RUNS}+ runs**", "",
                  _md_table(["Whispers", "Runs", "Memories / run", "Red"],
                            [(n, k, f"{a:.1f}", rd) for n, k, a, rd in combos[:8]]), ""]
    lines += ["**Last runs**", "",
              _md_table(["Date", "Whispers", "Memories", "Red"],
                        [(r["t"][:10], _combo_name(r["effects"]), r["memories"], r["red"]) for r in done[-10:][::-1]])]
    return f"Red memories: {mem / len(done):.1f} Hero Memories per run over {len(done)} run{'s' * (len(done) != 1)}", "\n".join(lines)


# --------------------------------------------------------------------------- #
# Durum: ekrandaki kartlar ve seçilmiş fısıltılar
# --------------------------------------------------------------------------- #
class VoidSeaState:
    def __init__(self):
        self.picked = []     # [(tür, [etkiler])]
        self.cards = []      # [(tür, [etkiler], x)]
        self.note = ""
        self.mode = "cards"  # "cards": 3 fısıltı kartı · "flame": Void Sea Seal alev seçimi
        self.selected = None # alev ekranında seçili alev
        self.tiers = {}      # alev ekranında alevlerin garanti en düşük kademesi {tür: kademe}
        self.pair = None     # alev ekranındaki iki alevin türü
        self.flame_known = None  # bu alev ekranında yazısı okunan sivri ikonlu alev: "won" | "boss"
        self.switch_seen = None  # bu ekranda okunan Switch kartının değeri
        self.switch_view = False # son okuma Switch ekranıydı
        self.owned_n = None  # paneldeki "n / 6" sayacı
        self.owned_seen = False  # son okumada sağdaki "Seal Whispers Owned" paneli okundu mu
        # ekrana gelen kartlar (seçilmeyenler dahil): koşu başlayınca kayda "offers" olarak yazılır.
        # Rastgele kartların türü / kalitesi ileride "3 karttan en iyisi" alev hesabı için kullanılacak.
        self.offers = []     # [{"step": kaçıncı seçim (0-5), "cards": [[tür, [[k, v]…]]…], "flame"?, "tier"?}]
        self.flame_seen = None   # son okunan alev ekranı: (adım, seçili alev, garanti kademe)

    def reset(self):
        self.picked, self.cards, self.note, self.mode, self.selected = [], [], "", "cards", None
        self.owned_n = None
        self.owned_seen = False
        self.offers, self.flame_seen = [], None
        self.pair, self.flame_known = None, None
        self.switch_seen, self.switch_view = None, False

    def _note_offer(self):
        """Eksiksiz okunan kart ekranını offers'a yazar. Aynı adımın yeniden okunması eskisinin yerine geçer;
        adım sayısı geri düştüyse yeni bir Seal turu başlamıştır, eski kayıtlar atılır."""
        step = self.count()
        if self.mode == "flame":
            if self.selected:
                self.flame_seen = (step, self.selected, self.tiers.get(self.selected), self.pair)
            return
        if self.mode != "cards" or not self.cards or not self.complete() or step >= 6:
            return
        if self.offers and step < max(o["step"] for o in self.offers):
            self.offers = []
        entry = {"step": step, "cards": [[side, [[k, v] for k, v, *_ in effs]] for side, effs, _ in self.cards]}
        if self.switch_seen is not None:
            entry["switch"] = self.switch_seen
        if self.flame_seen and self.flame_seen[0] == step:
            entry["flame"], entry["tier"] = self.flame_seen[1], self.flame_seen[2]
            pair = self.flame_seen[3]
            if pair is not None and "?" not in pair:   # Boss alevi ne sıklıkla çıkıyor (fe_boss_rate)
                entry["flames"] = list(pair)
        self.offers = [o for o in self.offers if o["step"] != step] + [entry]

    def count(self):
        return self.owned_n if self.owned_n is not None else len(self.picked)

    def on_analyze(self, res):
        """analyze() sonucunu uygular. Ekran tanınmadıysa False döner."""
        if res.get("switch") is not None:      # Switch ekranı: üç kart yerinde kalır, dördüncü seçenek öğrenilir
            self.on_scan([], res["owned"], res["owned_n"])
            self.mode, self.switch_seen, self.switch_view = "cards", res["switch"], True
            self._note_offer()
            return True
        self.switch_view = False
        before = [sig(c[1]) for c in self.cards]
        if res["mode"] in ("flame", "overview"):
            self.on_scan([], res["owned"], res["owned_n"])
            self.mode, self.selected = res["mode"], res["selected"]
            self.tiers = res.get("tiers") or {}
            self.pair = res.get("pair")
            self.need = res.get("need")
            if res["mode"] == "flame":
                known = self.flame_known
                if self.pair == ("?",) and known:
                    # fısıltısız alev seçili ama sivri olanın türü bu ekranda okunmuştu
                    self.pair = (known,)
                    if "?" in self.tiers:
                        self.tiers[known] = self.tiers.pop("?")
                elif self.selected in ("won", "boss"):
                    self.flame_known = self.selected
            self._note_offer()
            return True
        ok = self.on_scan(res["cards"], res["owned"], res["owned_n"])
        if ok:
            self.mode = "cards"
            if before and before != [sig(c[1]) for c in self.cards]:
                self.switch_seen = None      # başka bir ekran: Switch kartı da başka
            self.flame_known = None          # alev ekranından çıkıldı: sonraki ekranda alevler başka
            self._note_offer()
        return ok

    def complete(self):
        """Okuma eksiksiz mi? Kart ekranında 3 kartın hepsinin etkisi okunmuş olmalı
        (kartlar açılış animasyonundayken okunursa bazıları boş kalır)."""
        if self.mode in ("flame", "overview") or (self.switch_view and not self.cards):
            return True
        # sadece "Memory Scrap / Thread" okunduysa (sayı düşmüş, Boss kartı hariç) kart okunmamış sayılır
        return len(self.cards) >= 3 and all(effs and (side == "boss" or any(k != "none" for k, *_ in effs))
                                            for side, effs, _ in self.cards)

    def flame(self, settings):
        s = model_settings(settings)
        return flame_advice([e for _, effs in self.picked for e in effs], s, self.tiers)

    def on_scan(self, cards, owned, owned_n):
        """Yeni okuma. Kart yoksa False döner (ekran Void Sea Terminal değil)."""
        self.note = ""
        # öneri sahip olunan fısıltılara göre yapılır; panel kapalıysa bunu kullanıcıya söylemek gerek
        self.owned_seen = owned_n is not None
        # Seçilenler: önce sağdaki panel, okunamazsa ekrandan kaybolan kart.
        if owned_n is not None:
            self.owned_n = owned_n
            if owned_n == 0:
                self.picked = []
            elif len(owned) >= owned_n:
                self.picked = [("?", [e]) for e in owned]
            elif owned_n < len(self.picked):
                self.picked = self.picked[:owned_n]
            elif owned_n == len(self.picked) + 1 and self.cards and len(owned) <= len(self.picked):
                # panel eksik okundu ama bir kart seçildi: ekrandan kaybolan kart seçilendir
                now = {sig(e) for _, e, _ in cards}
                gone = [c for c in self.cards if sig(c[1]) not in now]
                if len(gone) == 1:
                    self.picked.append((gone[0][0], gone[0][1]))
                else:
                    self.note = "Could not tell which card was picked: open the Seal Whispers Owned panel and press “Read”"
            else:
                # bir kısmı okunamadı: okunanlar elimizdekinden azsa eskisini koru
                if len(owned) >= len(self.picked):
                    self.picked = [("?", [e]) for e in owned]
                self.note = f"read {len(owned)}/{owned_n} of the owned whispers"
        if cards:
            self.cards = self._merge(cards)
        return bool(cards)

    def _merge(self, new, tol=60):
        """Aynı ekranın yeniden okunmasında bir kart okunamazsa (seçili kartın parlaması,
        Confirm efekti…) önceki doğru okumayı koru. Okunan kartlardan biri bile öncekiyle aynı
        yerde ve aynı içerikteyse ekran aynıdır; içerik farklıysa yeni ekrandır, eskisi kullanılmaz."""
        if self.mode != "cards" or not self.cards:
            return new
        old = [c for c in self.cards if c[1]]
        same = False
        for o in old:
            near = [n for n in new if abs(n[2] - o[2]) < tol and n[1]]
            if near:
                if sig(near[0][1]) != sig(o[1]):
                    return new                       # kartlar değişmiş: yeni ekran
                same = True
        if not same:
            return new
        kept = [n for n in new if n[1]]
        for o in old:
            if not any(abs(n[2] - o[2]) < tol for n in kept):
                kept.append(o)
        return sorted(kept, key=lambda c: c[2])

    def advice(self, settings):
        s = model_settings(settings)
        if len(self.picked) >= 6:
            return None, []
        return recommend([e for _, effs in self.picked for e in effs], self.cards, s)

    def reason(self, settings, labels):
        """Önerinin gerekçesi (kart ya da alev ekranı)."""
        s = model_settings(settings)
        picked = [e for _, effs in self.picked for e in effs]
        if self.mode == "flame":
            best, out = flame_advice(picked, s, self.tiers)
            return reason_flame(picked, out, best, s, self.tiers)
        if self.mode != "cards" or len(self.picked) >= 6:
            return ""
        best, gains = recommend(picked, self.cards, s)
        return reason_cards(picked, self.cards, best, gains, labels, s)

    # FE planı (Wealth + Wonder): s = fe_model(...)
    def _picked(self):
        return [e for _, effs in self.picked for e in effs]

    def options(self, s):
        """FE planında seçenekler: ekrandaki kartlar + Switch kartı (okunduysa gerçeği, yoksa tahmini)."""
        if self.mode != "cards" or len(self.cards) < 3:
            return list(self.cards)
        if self.switch_seen is not None:
            return list(self.cards) + [("won", [("baseQ", self.switch_seen, "read")], SWITCH_X)]
        v = switch_estimate([c[1] for c in self.cards]) if fe_switch_ok(s) else None
        return list(self.cards) + ([("won", [("baseQ", v, "estimate")], SWITCH_X)] if v is not None else [])

    def fe_advice(self, s):
        if len(self.picked) >= 6:
            return None, []
        return fe_look(self._picked(), self.options(s), s, self.fe_left())

    def fe_now(self, s):
        """Kartların şu anki (kalan seçimler sayılmadan) FE kazancı."""
        return recommend(self._picked(), self.options(s), s, score=fe_score)[1]

    def fe_left(self):
        """Bu seçimden sonra kalan seçim sayısı (5/6 okumasında 0: bu son seçim)."""
        return max(0, 5 - self.count())

    def fe_flame(self, s):
        return fe_flame_advice(self._picked(), s, self.tiers, self.pair)

    def fe_reason(self, s, labels):
        picked = self._picked()
        if self.mode == "flame":
            best, out = self.fe_flame(s)
            return fe_reason_flame(picked, out, best, self.tiers)
        if self.mode != "cards" or len(self.picked) >= 6:
            return ""
        best, gains = self.fe_advice(s)
        return fe_reason_cards(picked, self.options(s), best, gains, labels, s, self.fe_left())


# --------------------------------------------------------------------------- #
# Koşu kaydı: her Void Sea koşusunun fısıltıları + düşen kırmızı (Ultimate) memory sayısı
# Kırmızı log'da görünmediği için oyuncu panelden "Kırmızı +1" ile girer.
# --------------------------------------------------------------------------- #
class RunLog:
    def __init__(self, path, settings=None):
        self.path = path
        self.settings = settings if settings is not None else {}   # uygulamanın ayarları (canlı sözlük)
        self.model_path = os.path.join(os.path.dirname(path) or ".", "voidsea_model.json")
        self.model = None
        self.csv_path = os.path.splitext(path)[0] + ".csv"
        base = os.path.splitext(path)[0]       # eski adlı kayıt (voidsea_kayit) kullanılıyorsa eski yedek adı
        self.backup_path = base + (".yedek.json" if base.endswith("voidsea_kayit") else ".backup.json")
        self.runs = []
        self.active = False     # terminaldeyiz (girişten sonraki ilk bölge değişimine kadar)
        self.runs = self._load()
        self.refit()

    def _load(self):
        """Kayıt hiçbir durumda sessizce boşalmasın: ana dosya bozuksa (yazarken kapanma vb.)
        kenara alınır ve yedekten yüklenir."""
        import json
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            try:
                os.replace(self.path, self.path + f".corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            except OSError:
                pass
            try:
                with open(self.backup_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return []

    def save(self):
        import csv, json
        try:
            # önce geçici dosyaya yaz, sonra tek adımda değiştir: yarım dosya oluşmaz.
            # bir önceki hâl yedek olarak kalır.
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.runs, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(self.path):
                try:
                    import shutil
                    shutil.copyfile(self.path, self.backup_path)
                except OSError:
                    pass
            os.replace(tmp, self.path)
            keys = list(totals([]).keys())
            with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["date", "red", "memories_picked_up"] + keys + ["whispers", "memory_scrap",
                            "thread_magic", "thread_rare", "thread_epic", "expected_fe"])
                for r in self.runs:
                    t = totals([tuple(e) for e in r["effects"]])
                    p = r.get("picks")              # v6.6 öncesi koşularda yok: boş bırak
                    w.writerow([r["t"], r["red"], r["memories"]] + [f"{t[k]:g}" for k in keys]
                               + [describe(run_whispers(r["effects"]))]
                               + ([p.get(c, 0) for c in (SCRAP_ID,) + THREAD_IDS] if p is not None else [""] * 4)
                               + [(r.get("est") or {}).get("fe", "")])
        except OSError:
            pass

    def start(self, effs, offers=None, est=None):
        """offers: Terminal'de ekrana gelen kartlar (VoidSeaState.offers); okunmadıysa yazılmaz.
        est: bu fısıltılarla beklenen (fe_estimate), koşu bitince gerçekle karşılaştırılır."""
        run = {"t": time.strftime("%Y-%m-%d %H:%M"), "effects": [[k, v] for k, v, *_ in effs],
               "red": 0, "memories": 0, "tracked": False, "picks": {},
               "plan": self.settings.get("vs_plan", "red"), "inv": True}
        if offers:
            run["offers"] = offers
        if est:
            run["est"] = est
        self.runs.append(run)
        self.active = True
        self.save()

    def on_pick(self, cid, n):
        """Void Sea haritasında çantaya Memory Scrap / Thread / Void Sea Invitation girdi: n adet."""
        if self.active and self.runs and cid in FE_ITEMS and n > 0:
            p = self.runs[-1].setdefault("picks", {})
            p[cid] = p.get(cid, 0) + n
            self.save()

    def on_level(self):
        if self.active:          # koşu bitti: sayım eksiksiz, kayda göre modeli güncelle
            self.active = False
            if self.runs:
                self.runs[-1]["tracked"] = True
                self.save()
            self.refit()

    def on_memory(self, uid=None):
        """Çantaya bir Hero Memory girdi. Her eşya kendi kimliğiyle bir kez sayılır: çantada "Sort"
        (ResetItemsLayout) ya da taşıma aynı memory'leri log'a yeniden "Add" diye yazıyor."""
        if self.active and self.runs:
            seen = getattr(self, "_mem_ids", None)
            if uid:
                if seen is None or seen[0] is not self.runs[-1]:
                    seen = self._mem_ids = (self.runs[-1], set())
                if uid in seen[1]:
                    return
                seen[1].add(uid)
            self.runs[-1]["memories"] += 1
            self.save()

    def add_red(self, delta):
        """Son koşunun kırmızı sayısını değiştirir. Hiç koşu yoksa fısıltısız bir kayıt açar."""
        if not self.runs:
            self.runs.append({"t": time.strftime("%Y-%m-%d %H:%M"), "effects": [], "red": 0, "memories": 0})
        r = self.runs[-1]
        r["red"] = max(0, r["red"] + delta)
        self.save()
        self.refit()

    def refit(self):
        """Modeli kayda göre yeniden kurar; öneriler bunu kullanır. Öğrenilen hâl ve o anki en iyi
        kombinasyon voidsea_model.json'a yazılır (koşu ya da kırmızı sayısı değiştikçe geçmişe eklenir)."""
        global LEARNED
        import json
        try:
            s = {k: float(self.settings.get(k, v)) for k, v in DEFAULTS.items()}
            runs = red_runs(self.runs)
            info = fit_model(runs, s)
            if info["active"]:
                s["fit"] = info
            combos = optimal_combo(runs, s)
            info["target"], info["target_x"] = combos[0]
            info["alts"] = combos[1:]
        except Exception:
            return
        LEARNED = self.model = info
        try:
            hist = []
            if os.path.exists(self.model_path):
                with open(self.model_path, encoding="utf-8") as f:
                    old = json.load(f)
                    hist = old.get("history", old.get("gecmis", []))      # v5.5 öncesi anahtar: "gecmis"
            if not hist or hist[-1].get("runs") != info["runs"] or hist[-1].get("red") != info["red"]:
                hist.append(info)
            tmp = self.model_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"current": info, "history": hist}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.model_path)
        except (OSError, ValueError):
            pass

    def current(self):
        return self.runs[-1] if self.runs else None

    def stats(self):
        """Red memories planının özet sayıları (Memory materials koşuları hariç)."""
        runs = red_runs(self.runs)
        n = len(runs)
        red = sum(r["red"] for r in runs)
        mem = sum(r["memories"] for r in runs)
        return n, red, (red / n if n else 0.0), mem, (mem / n if n else 0.0)


def run_whispers(effects):
    """Kayıttaki etkilerden terminalin 6 fısıltısı: log bloğunda fısıltılardan sonra haritanın
    "kopyalanma" satırları ve bir tekrar geliyor."""
    return [tuple(e) for e in effects if e[0] != "rep"][:6]


# --------------------------------------------------------------------------- #
# Log izleme
# --------------------------------------------------------------------------- #
TS_RX = re.compile(r"^\[\d{4}\.\d{2}\.\d{2}-")
DESC_RX = re.compile(r"\+Description \[(.*)\]\s*$")
# Void Sea Seal ve Void Sea Terminal sayfaları (log: "PageBase@ OpenFlow5! OpenView VoidBeacon")
OPEN_RX = re.compile(r"OpenView (VoidBeacon|VoidPort)\s*$")
# sayfa kapandı: "PageBase@ BackFlow0! Switch = true 15414_VoidBeaconCtrl 1260244"
# ya da oyunun diğer log biçiminde: "Page@ CloseView 11485_VoidBeaconCtrl"
CLOSE_RX = re.compile(r"(?:BackFlow0!.*|CloseView )\d+_(VoidBeacon|VoidPort)Ctrl")
# Hero Memory türleri: 71001 Origin, 71002 Discipline, 71003 Progress (kalite log'da yok)
MEM_RX = re.compile(r"ItemChange@ Add Id=(7100[123]_\S+)")      # tür + eşyanın tekil kimliği


def is_void_sea_block(effs):
    """Normal haritalarda da "kopyalanma" (rep) satırları çıkıyor: terminal girişi sayılmak için
    Hero Memory / Treasure etkisi olmalı."""
    return any(k not in ("none", "rep") for k, *_ in effs)


def is_block_end(line):
    # Blok, "AffixInfos" satırından sonraki boş "[Game] " satırından sonra gelen ilk zaman damgalı satırda biter.
    return bool(TS_RX.match(line)) and not line.rstrip().endswith("[Game]")


class LogWatcher(threading.Thread):
    """UE_game.log'u sondan izler ve kuyruğa ("vs", (olay, veri)) koyar:
       open      Void Sea ekranı açıldı (VoidBeacon ya da VoidPort); her açılışta bir kez okunur.
                 VoidBeacon normal haritalarda da (kısa açılır pencere olarak) açılabiliyor.
       close     o sayfa kapandı
       entered   terminale girildi; veri = log'a yazılan fısıltı etkileri
       level     yeni bir bölgeye geçildi (terminalden çıkış)
       memory    çantaya bir Hero Memory girdi (loot filtresinin gösterip senin topladığı)"""

    def __init__(self, path, q):
        super().__init__(daemon=True)
        self.path, self.q = path, q
        self.stop_flag = False

    def run(self):
        f, pos, block, inside = None, 0, [], False
        while not self.stop_flag:
            try:
                if f is None:
                    # bayt modu: metin modunda tell() bayt konumu vermiyor (Çince karakterlerde dosya
                    # boyutundan büyük çıkıp "log döndü" sanılıyor ve satırlar atlanıyordu)
                    f = open(self.path, "rb")
                    f.seek(0, os.SEEK_END)
                    pos = f.tell()
                if os.path.getsize(self.path) < pos:  # oyun yeniden başladı, log döndü
                    f.close(); f = None
                    continue
                raw = f.readline()
                if not raw.endswith(b"\n"):          # satır yok ya da oyun satırı henüz yazıyor
                    f.seek(pos)
                    time.sleep(0.25)
                    continue
                pos = f.tell()
                s = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                m = OPEN_RX.search(s)
                if m:
                    self.q.put(("vs", ("open", m.group(1))))
                elif (m := CLOSE_RX.search(s)):
                    self.q.put(("vs", ("close", m.group(1))))
                elif "UGameMgr::EnterLevel(" in s:
                    self.q.put(("vs", ("level", None)))
                elif (m := MEM_RX.search(s)):
                    self.q.put(("vs", ("memory", m.group(1))))
                elif s.endswith("[Game] AffixInfos"):
                    inside, block = True, []
                elif inside:
                    m = DESC_RX.search(s)
                    if m:
                        block.append(clean(m.group(1)))
                    elif is_block_end(s):
                        inside = False
                        effs = [e for d in block for e in parse_effects(d)]
                        if is_void_sea_block(effs):
                            self.q.put(("vs", ("entered", effs)))
            except OSError:
                f = None
                time.sleep(3)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--image")
    ap.add_argument("--log")
    a = ap.parse_args()
    if a.image:
        from PIL import Image
        im = Image.open(a.image).convert("RGB")
        res = analyze_any(im)
        print(f"OCR dili: {OCR_LANG}")
        st = VoidSeaState(); st.on_analyze(res)
        print(f"Ekran: {res['mode']} · seçilmiş {res['owned_n']}/6: {describe(res['owned']) if res['owned'] else '-'}")
        if st.mode == "flame":
            best, out = st.flame({})
            print(f"  Seçili alev: {SIDE_LABEL.get(st.selected, '?')}")
            for side, (g, pct) in out.items():
                print(f"  {SIDE_LABEL[side]} alevi: ortalama {pct:+.1f}%" + ("  <== ÖNERİ" if best == side else ""))
        best, gains = st.advice({})
        for i, (side, effs, _x) in enumerate(st.cards):
            print(f"  Kart {i + 1} [{SIDE_LABEL[side]}] {describe(effs)}  {gains[i][1]:+.1f}%"
                  + ("  <== ÖNERİ" if best == i else ""))
    if a.log:
        with open(a.log, encoding="utf-8", errors="replace") as fh:
            inside, block = False, []
            for raw in fh:
                s = raw.rstrip("\r\n")
                if s.endswith("[Game] AffixInfos"):
                    inside, block = True, []
                elif inside:
                    m = DESC_RX.search(s)
                    if m:
                        block.append(clean(m.group(1)))
                    elif is_block_end(s):
                        inside = False
                        effs = [e for d in block for e in parse_effects(d)]
                        if is_void_sea_block(effs):
                            print("Terminal:", describe(effs))
