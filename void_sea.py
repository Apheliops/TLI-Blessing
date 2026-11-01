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
# "%" OCR'da "o/o", "0/0", "°/o" ya da "96" ("20%" -> "2096") olarak okunabiliyor
PCT = NUM + r"\s*(?:%|o/o|0/0|[°º]/o|96(?=\s))"
PCT_OPT = NUM + r"\s*(?:%|o/o|0/0|[°º]/o)?"
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
    ("none",   re.compile(r"memory\s*(?:scrap|thread)", re.I), lambda m: 0.0),
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
}
ARM_ONLY = {"appear", "memQty", "flat", "luck"}
WON_ONLY = {"rarity", "tQty"}
SIDE_LABEL = {"arm": "Armament", "won": "Wonder", "wea": "Wealth", "boss": "Boss", "?": "?"}
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


def describe(effs):
    if not effs:
        return "not read"
    return ", ".join(f"{EFFECT_NAMES[k]} {v:g}" for k, v, *_ in effs)


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
        base = s["vs_base_mem"] * math.exp(sum(fit["c"][k] * x[k] for k in FIT_KEYS if k != "flat"))
        mem = base * math.exp(fit["c"]["flat"] * x["flat"])
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
    if len(order) > 1:
        o = order[1]
        txt += f" {labels[o]} only {gains[o][1]:+.0f}% ({_change(picked, cards[o][1], s)})."
        st = _stacked(picked, cards[o][1])
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


def recommend(picked, cards, s):
    """-> (en iyi kartın sırası | "tie" | None, [(mutlak artış, yüzde artış)] kart sırasıyla)"""
    base = red_score(picked, s)
    gains = []
    for _side, effs, _x in cards:
        g = red_score(picked + effs, s) - base
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
HEAD_RX = re.compile(r"^\W*whisper\s*of\s*(armament|wonder|wealth)\W*$", re.I)   # kart başlığı tek başına durur
FLAME_RX = re.compile(r"put\s*in\s*void\s*sea|void\s*sea\s*flames|请放入虚海|虚海夜火", re.I)
# "After defeating Pirates, a Whisper of X of Rare quality…" — seçili alev (OCR "Whisperof_Wonder" gibi okuyabilir)
SEL_RX = re.compile(r"whisper[\W_]*of[\W_]*(arm\w*|won\w*|wealth)", re.I)
SIDE_OF = {"armament": "arm", "wonder": "won", "wealth": "wea"}
OWNED_RX = re.compile(r"(\d)\s*/\s*6\s*$")   # "4 / 6" ("14 / 6" okunsa da son rakam)
# Çince: kart başlıkları 军械秘语 (Armament) / 奇珍秘语 (Wonder) / 财富秘语 (Wealth) / 首领秘语 (Boss: İngilizce
# istemcide henüz yok; ödülü 虚海请柬, kırmızıya katkısı yok). Alev yazısı: "击败虚海之子后，可选的封印秘语必定
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
        return SIDE_OF[m.group(1).lower()]
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

    if not heads:  # başlık okunamadıysa etkileri türünden ayır
        for e in parse_effects(" ".join(t for t, *_ in main)):
            side = "arm" if e[0] in ARM_ONLY else "won" if e[0] in WON_ONLY else None
            if side:
                cards.append((side, [e], 0))

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
    if re.search(r"whisper[\W_]*of[\W_]*\w+[\W_]*is[\W_]*guarant", main, re.I):
        return 0
    return None


def read_selected_flame(img, lines):
    """Void Sea Seal ekranında seçili alevin türü. Renkli (camgöbeği) yazı gri tonlamada
    kaybolabildiği için bulunamazsa o satır renkli ve büyütülmüş olarak yeniden okunur."""
    main = " ".join(t for t, x, y in lines if x < img.width * 0.80)
    m = SEL_RX.search(main) or CN_SEL_RX.search(squash_cn(main))
    if m:
        return _side_of_word(m.group(1))
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
    return _side_of_word(m.group(1)) if m else None


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
    return None


def _fill_numbers(text, nums):
    """Kalın beyaz rakamlar renkli okumada düşebiliyor: rakamsız kalan '%' ve
    'additional Hero' öncesine sırayla eksik rakamları yerleştirir."""
    nums = list(nums)
    out, last = [], 0
    for m in re.finditer(r"%|additional\s*hero|[枚个]\s*(?:英雄|追忆|虚海)", text, re.I):
        before = text[:m.start()].rstrip()
        out.append(text[last:m.start()])
        if nums and not (before and before[-1].isdigit()):
            out.append(f" {nums.pop(0)} ")
        last = m.start()
    out.append(text[last:])
    text = "".join(out)
    # "%" de okunamadıysa yerleşmeyen sayı kalır: metin sayıyla başlamıyorsa başına koy ("+25 Void Sea…")
    if nums and not re.match(r"\W*\d", text):
        text = f"{nums[0]} {text}"
    return text


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
    wm = a.min(axis=2) > thr
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


def read_cards_color(img, lines):
    """Void Sea Terminal'deki 3 kartı tek tek renkli ve büyütülmüş okur.
    Kartlar 'Switch' butonunun altında, ekran yüksekliğiyle orantılı aralıklarla durur."""
    import numpy as np
    from PIL import Image
    W, H = img.size
    sw = [(x, y) for t, x, y in lines if SWITCH_RX.fullmatch(squash_cn(t))]
    cx0 = sw[0][0] if sw else W / 2
    # arayüz ölçeği: sahne ekranın dikey ortasına göre çiziliyor; varsayılan ölçekte Switch merkezin 0.25·H
    # üstünde. Daha büyük ölçekte (ör. 21:9 ekranda %7-8) kartlar da birbirinden o oranda uzak.
    ui = min(1.3, max(0.85, (0.5 * H - sw[0][1]) / (0.25 * H))) if sw else 1.0
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
        for s in (2 * f, 3 * f):
            col = [t for t, *_ in _reading_order(_ocr_any(crop.resize((int(crop.width * s), int(crop.height * s)))))]
            head = next((t for t in col if (re.search(r"of", t, re.I) or "秘语" in squash_cn(t))
                         and _side_of_heading(t)), "") or head
            body = [t for t in col if t != head]
            if CJK_RX.search(" ".join(body)):
                # Çince: rakamlar satır içinde kalsın ("…额外" + ayrı satırda "32"), kısmi okunanlar beyaz rakamla tamamlansın
                text = " ".join(body)
                effs = parse_effects(_cn_numbers(text, white_nums,
                                                 lambda thr: _white_numbers(crop, np, Image, thr=thr, f=f)))
                if effs:
                    break
                continue
            standalone = [NUM_LINE_RX.match(t).group(1) for t in body if NUM_LINE_RX.match(t)]
            text = " ".join(t for t in body if not NUM_LINE_RX.match(t))
            present = re.findall(r"\d+(?:[.,]\d+)?", text)
            pool = standalone + [n for n in white_nums if n not in standalone and n not in present]
            effs = parse_effects(_fill_numbers(text, pool))
            if effs:
                break
        side = _side_of_heading(head)
        if side is None and effs:
            k = effs[0][0]
            if k in ARM_ONLY:
                side = "arm"
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
    flame = any(FLAME_RX.search(t) or FLAME_RX.search(squash_cn(t)) for t, x, y in lines if x < img.width * 0.80)
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
    res = {"mode": "flame" if flame else "cards", "cards": [] if flame else cards, "owned": owned,
           "owned_n": owned_n, "selected": read_selected_flame(img, lines) if flame else None, "tiers": {},
           "cjk": cn_screen(lines)}
    if flame:
        try:
            cols = read_flame_colors(img)
        except Exception:
            cols = {}
        res["tiers"] = flame_tiers(cols, res["selected"], read_selected_tier(lines, img.width))
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
    return res["mode"] in ("flame", "overview") or bool(res["cards"]) or bool(res["owned"])


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


def flame_tiers(cols, selected, sel_tier):
    """Alevlerin garanti kademesi {tür: kademe}. Seçili alevin türü ve kademesi yazıdan; köşesi, rengi
    ortadaki daireyle eşleşen köşe. Diğer köşe diğer türdür, kademesi kendi renginden."""
    tier = lambda c: FLAME_COLOR_TIER.get(c) if c else None
    left, right, center = cols.get("left"), cols.get("right"), cols.get("center")
    out = {}
    if selected == "boss":
        return out          # Çince: alevlerden biri Boss (首领) fısıltısı garanti ediyor; diğer köşenin türü bilinmiyor
    if selected not in ("arm", "won"):
        # seçili alev yazıdan okunamadı: eski varsayım (sol Armament, sağ Wonder)
        for side, c in (("arm", left), ("won", right)):
            if tier(c) is not None:
                out[side] = tier(c)
        return out
    other = "won" if selected == "arm" else "arm"
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


def flame_advice(picked, s, tiers=None):
    """tiers: {tür: garanti en düşük kademe}. Katkılar %3'ten yakınsa kalitesi yüksek alev önerilir.
    -> (en iyi tür | "tie" | None, {tür: (ortalama mutlak artış, ortalama yüzde artış)})"""
    tiers = tiers or {}
    base = red_score(picked, s)
    out = {}
    for side, pool in FLAME_POOL.items():
        gs = [red_score(picked + [e], s) - base for e in flame_values(pool, tiers.get(side, DEFAULT_TIER))]
        g = sum(gs) / len(gs)
        out[side] = (g, g / base * 100 if base > 0 else 0.0)
    a, w = out["arm"][0], out["won"][0]
    top = max(a, w)
    if top <= 1e-9:
        return None, out
    if abs(a - w) <= top * 0.03:
        ta, tw = tiers.get("arm", DEFAULT_TIER), tiers.get("won", DEFAULT_TIER)
        if ta != tw:
            return ("arm" if ta > tw else "won"), out
        return "tie", out
    return ("arm" if a > w else "won"), out


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
# Kayıttan öğrenme
#   memory: her koşuda toplanan memory sayısı Poisson regresyonuyla (log bağlantı) etkilere oturtulur.
#           Ayrıca her koşu başarılı/başarısız etiketlenir: kırmızısız ve memory ≤ eşik ise başarısız.
#           Eşik sabit değil: kırmızısız koşuların ortanca memory sayısı, her güncellemede yeniden.
#           İki kanıt birlikte kullanılır (sayı + etiket); kırmızılı koşu az memory'yle cezalandırılmaz.
#           Katsayılar elle kurulan modele (FIT_PRIOR) doğru çekilir, az veride ondan uzaklaşmaz.
#   kırmızı: rarity üssü Poisson olabilirliğiyle bulunur (önsel: ayardaki üs); kırmızı yokken önsel kalır.
# --------------------------------------------------------------------------- #
FIT_MIN_RUNS = 5      # bundan az koşuda öneriler elle kurulan modelle yapılır
FIT_LAMBDA = 6.0      # önsele çekme gücü (≈20 koşuda katsayı yarı yola gelir)
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
    c = _poisson_fit(X, y, np, thr=thr, has_red=has_red)
    info["c"] = {k: float(min(3.0, max(0.0, v))) for k, v in zip(FIT_KEYS, c)}
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


def success_threshold(counts):
    """Başarı eşiği: kırmızısız koşuların ortanca memory sayısı (aşağı yuvarlanır).
    Bu sayı ve altı başarısız. Veri değiştikçe eşik de değişir."""
    if not counts:
        return 0
    c = sorted(counts)
    n = len(c)
    return int(c[n // 2] if n % 2 else (c[n // 2 - 1] + c[n // 2]) / 2)


def _poisson_fit(X, y, np, thr=None, has_red=None):
    """Cezalı Poisson regresyonu (Gauss-Newton): memory ~ Poisson(μ = exp(a + X·c)), c önsele çekilir.
    a (kişinin taban seviyesi: loot filtresi, toplama alışkanlığı) serbest.
    thr verilirse başarı etiketi de olabilirliğe girer: başarılı = kırmızı var ya da memory > thr,
    P(başarılı) = P(memory > thr | μ). Kırmızılı koşuların sayısı kullanılmaz (az memory'yle cezalanmaz). -> c"""
    n, k = X.shape
    X1 = np.hstack([np.ones((n, 1)), X])
    prior = np.concatenate([[0.0], [FIT_PRIOR[key] for key in FIT_KEYS]])
    P = np.diag([0.0] + [FIT_LAMBDA] * k)
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
               "tQty": "Treasure", "rarity": "Rarity", "rep": "Replication", "none": "None"}
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
        self.owned_n = None  # paneldeki "n / 6" sayacı
        self.owned_seen = False  # son okumada sağdaki "Seal Whispers Owned" paneli okundu mu

    def reset(self):
        self.picked, self.cards, self.note, self.mode, self.selected = [], [], "", "cards", None
        self.owned_n = None
        self.owned_seen = False

    def count(self):
        return self.owned_n if self.owned_n is not None else len(self.picked)

    def on_analyze(self, res):
        """analyze() sonucunu uygular. Ekran tanınmadıysa False döner."""
        if res["mode"] in ("flame", "overview"):
            self.on_scan([], res["owned"], res["owned_n"])
            self.mode, self.selected = res["mode"], res["selected"]
            self.tiers = res.get("tiers") or {}
            self.need = res.get("need")
            return True
        ok = self.on_scan(res["cards"], res["owned"], res["owned_n"])
        if ok:
            self.mode = "cards"
        return ok

    def complete(self):
        """Okuma eksiksiz mi? Kart ekranında 3 kartın hepsinin etkisi okunmuş olmalı
        (kartlar açılış animasyonundayken okunursa bazıları boş kalır)."""
        if self.mode in ("flame", "overview"):
            return True
        return len(self.cards) >= 3 and all(effs for _, effs, _ in self.cards)

    def flame(self, settings):
        s = model_settings(settings)
        return flame_advice([e for _, effs in self.picked for e in effs], s, self.tiers)

    def take(self, i):
        """Elle: i. kartı seçtim."""
        if i < len(self.cards) and len(self.picked) < 6:
            self.picked.append((self.cards[i][0], self.cards[i][1]))

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
                    self.note = "Could not tell which card was picked: click the card to add it"
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
                w.writerow(["date", "red", "memories_picked_up"] + keys + ["whispers"])
                for r in self.runs:
                    t = totals([tuple(e) for e in r["effects"]])
                    w.writerow([r["t"], r["red"], r["memories"]] + [f"{t[k]:g}" for k in keys]
                               + [describe(run_whispers(r["effects"]))])
        except OSError:
            pass

    def start(self, effs):
        self.runs.append({"t": time.strftime("%Y-%m-%d %H:%M"), "effects": [[k, v] for k, v, *_ in effs],
                          "red": 0, "memories": 0, "tracked": False})
        self.active = True
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
            info = fit_model(self.runs, s)
            if info["active"]:
                s["fit"] = info
            combos = optimal_combo(self.runs, s)
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
        n = len(self.runs)
        red = sum(r["red"] for r in self.runs)
        mem = sum(r["memories"] for r in self.runs)
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
CLOSE_RX = re.compile(r"BackFlow0!.*\d+_(VoidBeacon|VoidPort)Ctrl")
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
