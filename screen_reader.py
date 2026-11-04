# -*- coding: utf-8 -*-
"""
Blessings from Gods ekranını okur (ekran görüntüsü + Tesseract OCR).

Gereken paketler:  pip install mss pillow numpy pytesseract
Gereken program :  Tesseract-OCR (https://github.com/UB-Mannheim/tesseract/wiki)
"""
import difflib
import json
import os
import re
import shutil
import unicodedata

FE = "100300"

# Kart yerleşimi, 2560x1440 referans çözünürlükte (16:9 ekranlar için ölçeklenir)
CARD_X0 = 591      # 1. kartın merkez x'i
CARD_DX = 177      # kartlar arası mesafe
N_CARDS = 8

# Blessings'te görülen eşyalar -> ID (isim okununca eşleştirme için)
KNOWN = [
    (("flame", "elementium"), FE),
    (("flame", "ementi"), FE),          # "Flame Flementium" gibi okunuşlar
    (("flame", "sand"), "100200"),
    (("energy", "core"), "5080"),
    (("netherrealm",), "5028"),
    (("resonance",), "5028"),
    (("wedge", "legend"), "5250"),
    (("wedge", "rare"), "5230"),
    (("wedge", "magic"), "5220"),
    (("ultimate", "ember"), "200030"),
    (("ultimate",), "200030"),
    (("divinity", "fragment"), "5201"),
]
EXCLUDED_WORDS = ("arcane", "arca ne")      # Flame Sand artık değerlendiriliyor

# Çince istemci (众神所赐): isimler Windows'un Çince OCR'ıyla okunur (Tesseract'ta Çince yok).
# Kaynak: oyuncunun ekran görüntüsü + tlidb.com/cn/Blessings_from_Gods; 至臻灰烬 tlidb.com/cn/Ultimate_Ember.
CN_KNOWN = [("初火源质", FE), ("初火灵砂", "100200"), ("能量核心", "5080"), ("异界回响", "5028"),
            ("至臻灰烬", "200030")]
CN_WEDGE = {"传奇": "5250", "稀有": "5230", "魔法": "5220"}    # 升华之楔（…）= Transcendence Wedge (…)
CN_EXCLUDED = ("奥秘", "绑定")                                  # 奥秘辉石 = Arcane Stone; 绑定 = Bound (tahmin)


class OcrError(Exception):
    pass


def _imports():
    try:
        import numpy as np
        from PIL import Image
        import pytesseract
    except ImportError as e:
        raise OcrError(f"Missing package: {e.name}. Run 'Setup.bat'.")
    return np, Image, pytesseract


def find_tesseract(custom=""):
    cands = [custom] if custom else []
    cands += [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return shutil.which("tesseract")


def setup(tesseract_path=""):
    np, Image, pytesseract = _imports()
    exe = find_tesseract(tesseract_path)
    if not exe:
        raise OcrError("Tesseract-OCR not found. Run Setup.bat (see README.md).")
    pytesseract.pytesseract.tesseract_cmd = exe
    return exe


def game_window_rect():
    """Oyun penceresinin iç alanı (başlık çubuğu ve kenarlar hariç), ekran koordinatlarında:
    {"left", "top", "width", "height"}; bulunamazsa ya da simge durumundaysa None.
    Oyun pencere modunda (ör. 2560×1440 monitörde 1920×1080) çalışıyorsa sadece o alan okunmalı."""
    import sys
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    def exe_of(hwnd):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            return buf.value.lower() if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
        finally:
            k32.CloseHandle(h)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd) and not u32.IsIconic(hwnd) and "torchlight" in exe_of(hwnd):
            r = wintypes.RECT()
            if u32.GetClientRect(hwnd, ctypes.byref(r)) and r.right > 640 and r.bottom > 360:
                pt = wintypes.POINT(0, 0)
                u32.ClientToScreen(hwnd, ctypes.byref(pt))
                found.append({"left": pt.x, "top": pt.y, "width": r.right, "height": r.bottom})
        return True

    try:
        u32.EnumWindows(cb, 0)
    except Exception:
        return None
    return max(found, key=lambda r: r["width"] * r["height"]) if found else None


def grab_screen(monitor_index=1):
    """Oyun penceresini yakalar (pencere modu, kenarlıksız, tam ekran); bulunamazsa seçili monitörün tamamını."""
    try:
        import mss
        from PIL import Image
    except ImportError as e:
        raise OcrError(f"Missing package: {e.name}. Run 'Setup.bat'.")
    with mss.mss() as sct:
        area = game_window_rect()
        if area is None:
            mons = sct.monitors
            if monitor_index >= len(mons):
                monitor_index = 1
            area = mons[monitor_index]
        shot = sct.grab(area)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        # yakalanan alanın ekrandaki yeri: kart çerçeveleri (önerilen kartların üstüne) buna göre çizilir
        img.info["area"] = {k: int(area[k]) for k in ("left", "top", "width", "height")}
        return img


def _white(a, np, lo=150, spread=80):
    a = a.astype(int)
    mn, mx = a.min(2), a.max(2)
    return (mn > lo) & ((mx - mn) < spread)


def _to_img(mask, np, Image, scale, pad=8):
    h, w = mask.shape
    out = np.full((h + 2 * pad, w + 2 * pad), 255, "uint8")
    out[pad:pad + h, pad:pad + w] = np.where(mask, 0, 255)
    img = Image.fromarray(out)
    return img.resize((img.width * scale, img.height * scale), Image.LANCZOS)


# ---------------- Rakam tanıma (oyunun kendi fontundan şablonlar) ----------------
_HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIGITS = os.path.join(_HERE, "digits.json")        # hazır şablonlar
LEARNED_DIGITS = os.path.join(_HERE, "userdata", "learned_digits.json")   # alımlardan öğrenilenler (kişisel)
if not os.path.exists(LEARNED_DIGITS) and os.path.exists(os.path.join(_HERE, "userdata", "rakamlar.json")):
    LEARNED_DIGITS = os.path.join(_HERE, "userdata", "rakamlar.json")   # v5.5 öncesi ad (taşınamadıysa)
GW, GH = 16, 24
_templates = None


def _load_templates(np):
    global _templates
    if _templates is None:
        _templates = []
        for f in (BUILTIN_DIGITS, LEARNED_DIGITS):
            builtin = list(_templates)
            try:
                with open(f, encoding="utf-8") as fh:
                    for t in json.load(fh):
                        v, a = np.array(t["v"], dtype=float), float(t["a"])
                        # öğrenilmiş şablon hazır şablonlara göre açıkça başka bir rakamsa yanlış etiketlenmiştir
                        # (yenilenmiş kartın eski fiyatıyla öğrenilmiş olabilir): kullanılmaz
                        if builtin:
                            d, sure = _classify(v, a, np, temps=builtin)
                            if sure and d != t["d"]:
                                continue
                        _templates.append((t["d"], v, a))
            except Exception:
                pass
    return _templates


def _split_glyphs(m, np, spans=None):
    cols = m.sum(0) > 0
    out, i = [], 0
    while i < len(cols):
        if cols[i]:
            j = i
            while j < len(cols) and cols[j]:
                j += 1
            g = m[:, i:j]
            r = np.nonzero(g.sum(1))[0]
            g = g[r.min():r.max() + 1]
            if g.shape[0] >= 0.6 * m.shape[0]:          # ikondaki parıltı gibi kırıntıları at
                out.append(g)
                if spans is not None:
                    spans.append((i, j))
            i = j
        else:
            i += 1
    return out


def _glyph_vec(g, np, Image):
    from PIL import ImageFilter
    im = Image.fromarray((g * 255).astype("uint8"))
    pad = Image.new("L", (im.width + 4, im.height + 4), 0)
    pad.paste(im, (2, 2))
    pad = pad.filter(ImageFilter.GaussianBlur(0.7)).resize((GW, GH), Image.BILINEAR)
    return np.asarray(pad).astype(float).ravel() / 255, g.shape[1] / g.shape[0]


def _classify(vec, asp, np, top=None, temps=None):
    """-> (rakam, emin_mi). top: verilirse en yakın iki rakam eklenir. temps: kullanılacak şablonlar (varsayılan hepsi)."""
    best = {}
    img = vec.reshape(GH, GW)
    shifts = [np.roll(np.roll(img, dy, 0), dx, 1).ravel() for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    for d, v, a in (_load_templates(np) if temps is None else temps):
        if v.size != vec.size:
            continue
        dist = min(float(((sh - v) ** 2).sum()) for sh in shifts) + 20 * (asp - a) ** 2
        if d not in best or dist < best[d]:
            best[d] = dist
    if not best:
        return None, False
    ranked = sorted(best.items(), key=lambda x: x[1])
    d1, x1 = ranked[0]
    x2 = ranked[1][1] if len(ranked) > 1 else 1e9
    if top is not None:
        top.extend(d for d, _ in ranked[:2])
    sure = x2 >= 1.3 * max(x1, 0.5)          # en yakın rakam, ikinciden belirgin şekilde yakın
    return d1, sure


def learn_price(glyphs, value):
    """Gerçek fiyat biliniyorsa (log'daki alım) o kartın rakamlarını şablon olarak kaydet."""
    global _templates
    txt = str(int(value))
    if not glyphs or len(glyphs) != len(txt):
        return False
    try:
        with open(LEARNED_DIGITS, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = []
    for (v, a), d in zip(glyphs, txt):
        data.append({"d": d, "a": round(float(a), 4), "v": [round(float(x), 3) for x in v]})
    data = data[-400:]
    with open(LEARNED_DIGITS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    _templates = None
    return True


def _read_price(im, cx, s, np, Image, pytesseract):
    """-> (fiyat ya da None, rakam vektörleri, neden). Okunamazsa daha gevşek beyaz eşikle tekrar dener
    (renkli kart zemini / düşük çözünürlükte rakam kenarları beyazdan sapıyor)."""
    first = None
    for lo, sp in ((150, 80), (140, 100), (130, 120)):
        r = _read_price_once(im, cx, s, np, Image, pytesseract, lo, sp)
        if r[0] is not None:
            return r
        first = first or r
    return first


def _read_price_once(im, cx, s, np, Image, pytesseract, lo, spread):
    reg = np.asarray(im.crop((int(cx - 6 * s), int(312 * s), int(cx + 62 * s), int(385 * s))))
    m = _white(reg, np, lo, spread)
    rows = m.sum(1)
    if rows.max() < 2:
        return None, [], "price text not visible (card covered or screen moving)"
    # rakam satırı: art arda dolu satırlardan oluşan, toplamda en çok beyaz içeren ve yeterince yüksek bant.
    # (tek "en beyaz satır" yetmiyor: kartın alt kenarındaki ince çizgi tek satırda rakamlardan beyaz olabiliyor)
    groups, y = [], 0
    while y < len(rows):
        if rows[y] > 0:
            e = y
            while e < len(rows) and rows[e] > 0:
                e += 1
            groups.append((y, e - 1))
            y = e
        else:
            y += 1
    tall = [g for g in groups if g[1] - g[0] + 1 >= 7 * s] or groups
    lo, hi = max(tall, key=lambda g: rows[g[0]:g[1] + 1].sum())
    band = m[lo:hi + 1]
    cols = np.nonzero(band.sum(0))[0]
    sub = band[:, cols.min():cols.max() + 1]

    # elmas simgesinin kenarı (rakamlardan belirgin boşlukla ayrık sol parçalar) rakam sanılmasın
    spans = []
    gl = _split_glyphs(sub, np, spans)
    while len(gl) > 1 and spans[1][0] - spans[0][1] > 4.5 * s:
        # …ama boyu diğer rakamlarla aynıysa atma: elmas kenarı rakamlardan kısa kalıyor. Bazı ekranlarda
        # (Çince istemci) "1" rakamının iki yanı geniş kalıyor, "15" -> "5", "156" -> "56" okunuyordu.
        ratio = gl[0].shape[0] / max(g.shape[0] for g in gl[1:])
        if ratio >= 0.9 and _classify(*_glyph_vec(gl[0], np, Image), np)[0] is not None:
            break
        gl, spans = gl[1:], spans[1:]
    if gl and spans:
        sub = sub[:, spans[0][0]:]
    # 1) şablonlarla
    glyphs = [_glyph_vec(g, np, Image) for g in gl]
    t_txt, t_sure, tops = "", bool(glyphs), []
    for v, a in glyphs:
        tops.append([])
        d, sure = _classify(v, a, np, tops[-1])
        if d is None:
            t_txt, t_sure = "", False
            break
        t_txt += d
        t_sure = t_sure and sure

    if t_txt and t_sure:
        return int(t_txt), glyphs, ""                   # şablon emin: Tesseract'a gerek yok (okuma ~2 kat hızlı)
    # 2) Tesseract: rakam yüksekliğine göre üç boyda okunur, çoğunluk alınır. Sabit 4× büyütme büyük arayüz
    #    ölçeğinde (rakam 18 px) fazla geliyordu: "75" -> "15" / "5" (3840×1600, GitHub #6). 1440p'deki eski boy
    #    (~13 px × 4) bu boyların içinde.
    o_reads = []
    gh = max(1, sub.shape[0])
    base = _to_img(sub, np, Image, 1)
    for target in (26, 52, 38):
        f = max(1.0, target / gh)
        img = base.resize((max(1, int(base.width * f)), max(1, int(base.height * f))), Image.LANCZOS)
        for psm in (7, 8, 10):
            t = pytesseract.image_to_string(img, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789").strip()
            if t.isdigit():
                o_reads.append(t)
                break
        if len(o_reads) == 2 and o_reads[0] == o_reads[1]:
            break                                       # ilk iki boy uyuştu: üçüncüye gerek yok
    o_txt = max(o_reads, key=lambda t: (o_reads.count(t), t == t_txt)) if o_reads else ""

    # 3) karar: şablon eminse onu, değilse ikisi aynıysa onu; aksi halde okunamadı
    if t_txt and t_sure:
        return int(t_txt), glyphs, ""
    if t_txt and t_txt == o_txt:
        return int(t_txt), glyphs, ""
    # şablon emin değil, Tesseract en az iki boyda aynı değeri okudu ve uyuşmayan her rakam şablonun ikinci
    # adayı: Tesseract'ınki kabul. Büyük arayüz ölçeğinde hazır şablonlar zayıf kalıyor, "106" -> 105 / 106 (GitHub #7)
    if (t_txt and o_txt and len(o_txt) == len(t_txt) and o_reads.count(o_txt) >= 2
            and all(a == b or b in tp for a, b, tp in zip(t_txt, o_txt, tops))):
        return int(o_txt), glyphs, ""
    if not t_txt and o_txt and not _load_templates(np):
        return int(o_txt), glyphs, ""                   # şablon dosyası yoksa eski yöntem
    if not glyphs:
        why = "digits could not be separated"
    elif t_txt and o_txt:
        why = f"digits uncertain (two methods said {t_txt} / {o_txt})"
    elif t_txt:
        why = f"digits uncertain (maybe: {t_txt})"
    else:
        why = "digits not recognized"
    return None, glyphs, why


def _sold_out(im, cx, s, np):
    a = np.asarray(im.crop((int(cx - 50 * s), int(325 * s), int(cx + 50 * s), int(370 * s)))).astype(int)
    # "Sold out" kırmızısı: yeşil ve mavi birbirine yakın; Flame Elementium kartının turuncusunda yeşil, maviden çok yüksek
    red = ((a[..., 0] > 170) & (a[..., 1] < 110) & (a[..., 2] < 110) & (abs(a[..., 1] - a[..., 2]) < 40)).sum()
    return red > 60 * s * s


# isim yazısının beyaz eşikleri: önce sıkı; eşya tanınmazsa (renkli kart zemini, düşük çözünürlük) gevşet
NAME_THRESHOLDS = ((165, 70), (150, 90), (140, 110))


def _read_name(im, cx, s, np, Image, pytesseract, lo=165, spread=70):
    a = np.asarray(im.crop((int(cx - 74 * s), int(232 * s), int(cx + 74 * s), int(325 * s))))
    img = _to_img(_white(a, np, lo, spread), np, Image, 3, pad=4)
    return pytesseract.image_to_string(img, config="--psm 6").replace("\n", " ").strip()


def match_item(text, item_names=None):
    """OCR metni -> (cid, adet, hariç_mi). cid None ise tanınamadı."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("{", "(").replace("}", ")").replace("!", ")")
    flat = re.sub(r"[^a-z0-9()]+", " ", t)
    qty = 1
    m = re.search(r"x\s?(\d{1,4})\b", flat)
    if m:
        qty = int(m.group(1))
    squash = flat.replace(" ", "")
    if any(w.replace(" ", "") in squash for w in EXCLUDED_WORDS) or ("arca" in squash and "stone" in squash):
        return None, qty, True
    if "bound" in squash:            # Bound eşyalar Exchange'te satılamaz -> değerlendirme dışı
        return None, qty, True
    for words, cid in KNOWN:
        if all(w in squash for w in words):
            return cid, qty, False
    # bilinmeyen eşya: isim tablosunda en yakın eşleşme
    if item_names:
        name = re.sub(r"\bx\s?\d+\b", "", flat).strip()
        name = " ".join(w for w in name.split() if len(w) > 2)
        if len(name) >= 5:
            lookup = {v.lower(): k for k, v in item_names.items()}
            best = difflib.get_close_matches(name, list(lookup), n=1, cutoff=0.75)
            if best:
                return lookup[best[0]], qty, False
    return None, qty, False


_ZH = []          # kurulu Çince OCR dili (önbellek)
_LAST = {"cn": False}     # son okumada isimler Çinceydi: sonraki okumada önce Çince denensin


def zh_ocr_tag():
    if not _ZH:
        tag = None
        try:
            from winrt.windows.media.ocr import OcrEngine
            tags = [l.language_tag for l in OcrEngine.available_recognizer_languages]
            tag = next((t for t in tags if t.lower().startswith(("zh-hans", "zh-cn"))), None)
        except Exception:
            pass
        _ZH.append(tag)
    return _ZH[0]


def _read_name_cn(im, cx, s):
    """Kartın adını Windows'un Çince OCR'ıyla okur (renkli, büyütülmüş)."""
    import asyncio
    import winocr
    crop = im.crop((int(cx - 74 * s), int(232 * s), int(cx + 74 * s), int(325 * s)))
    crop = crop.resize((crop.width * 3, crop.height * 3))
    async def _await(op):     # IAsyncOperation coroutine değil: eski Python'larda asyncio.run kabul etmiyor
        return await op
    res = asyncio.run(_await(winocr.recognize_pil(crop.convert("RGB"), zh_ocr_tag())))
    return " ".join(l.text for l in res.lines)


def match_item_cn(text):
    """Çince ad -> (cid, adet, hariç_mi). "能量核心 X 10", "升华之楔（稀有） X 20", "奥秘辉石（大）"."""
    t = re.sub(r"\s+", "", text.translate(str.maketrans("０１２３４５６７８９（）", "0123456789()")))
    # adet: "X 20"; OCR "X"i ")(" diye okuyabiliyor -> sondaki sayı adet
    m = re.search(r"[xX×]\s*(\d{1,4})", t) or re.search(r"(?<![\d.])(\d{1,4})$", t)
    qty = int(m.group(1)) if m else 1
    if any(w in t for w in CN_EXCLUDED):
        return None, qty, True
    if "升华" in t or "之楔" in t:
        for word, cid in CN_WEDGE.items():
            if word in t:
                return cid, qty, False
        return None, qty, False
    for word, cid in CN_KNOWN:
        if word in t or (len(word) == 4 and sum(c in t for c in word) >= 3):   # bir karakteri yanlış okunsa da
            return cid, qty, False
    return None, qty, False


def _name_cn(im, cx, s):
    try:
        txt = _read_name_cn(im, cx, s)
    except Exception:
        return "", (None, 1, False)
    return txt, match_item_cn(txt)


CARD_TOP = 110        # kartın üst kenarı (1440p, yerinde dururken)
CARD_HALF_W = 76      # kart çerçevesi: kart merkezinden yarı genişlik (1440p)
CARD_BOX_H = 258      # kart çerçevesi: kartın üst kenarından fiyat satırının altına (1440p)
CARD_ROW_DY = 251     # ikinci kart satırının kayması (büyük arayüz ölçeğinde 8. kart alta iner; ×1.44'te ölçüldü)


def _card_top(im, cx, s, np, default=CARD_TOP):
    """Kartın üst kenarının y'si: koyu arka plandan kartın renkli zeminine geçilen ilk satır
    (parlaklık 0.15 civarından 0.45 üstüne çıkar). Bulunamazsa default × ölçek (default=None: None)."""
    y0, y1 = int(60 * s), int(450 * s)
    strip = np.asarray(im.crop((int(cx - 30 * s), y0, int(cx + 30 * s), y1)).convert("RGB")).astype(float)
    val = strip.max(2).mean(1) / 255
    run = max(2, int(3 * s))
    for y in range(len(val) - run):
        if (val[y:y + run] > 0.35).all():
            return y0 + y
    return None if default is None else default * s


def _locate_cards(im, s, np):
    """Kartları ekranda bul: kartların renkli zemini koyu arka plandan belirgin parlak.
    21:9 gibi geniş ekranlarda ve farklı arayüz ölçeklerinde kartlar varsayılan yerde durmuyor
    (geniş ekranda arayüz ortalanmıyor, sola dayalı; ölçek oyuncunun ayarına göre değişebiliyor).
    Büyük arayüz ölçeğinde (ör. 2560×1440 pencerede ×1.44, GitHub #4) 8 kart tek satıra sığmıyor: sağa
    sığmayanlar alt satırın başından devam ediyor.
    -> ([(kartın merkez x'i, satırın dikey kayması)] × 8, etkin ölçek) | None"""
    W, H = im.size
    y0, y1 = int(140 * s), int(200 * s)
    col = np.asarray(im.crop((0, y0, W, y1)).convert("RGB")).astype(float).max(2).mean(0) / 255
    mask = col > 0.30
    runs, x = [], 0
    while x < W:
        if mask[x]:
            e = x
            while e < W and mask[e]:
                e += 1
            runs.append((x, e))
            x = e
        else:
            x += 1
    # kartların hepsi aynı genişlikte (varsayılan ölçekte ~145 px, büyük ölçekte daha geniş): en çok tekrar eden
    # genişliktekiler kart sayılır; sarı sekme gibi tek kalan parlak alanlar elenir
    widths = [(b - a, (a + b) / 2) for a, b in runs if 80 * s <= b - a <= 300 * s]
    ref = max((w for w, _ in widths), key=lambda w: sum(1 for v, _ in widths if abs(v - w) <= 0.06 * w), default=0)
    cands = [c for w, c in widths if abs(w - ref) <= 0.06 * ref]
    gaps = [b - a for a, b in zip(cands, cands[1:]) if 150 * s <= b - a <= 300 * s]
    if len(cands) < 4 or not gaps:
        return None
    spacing = float(np.median(gaps))
    k = spacing / (CARD_DX * s)                 # oyunun arayüz ölçeği / varsayılan
    if not 0.85 <= k <= 1.6:
        return None
    # en soldaki bulunan kart hangi sırada? (satılmış kartlar sönük olup bulunamayabilir)
    n0 = max(0, round((cands[0] - CARD_X0 * s * k) / spacing))
    c0 = cands[0] - n0 * spacing
    fits = sum(1 for c in cands if abs(((c - c0) / spacing) - round((c - c0) / spacing)) < 0.15)
    if fits < 4:
        return None
    # satıra sığan kart sayısı: kartın yarısı ekranın içinde kalmalı; sığmayanlar alt satırda
    half = 0.37 * spacing
    per_row = max(1, min(N_CARDS, int((W - half - c0) // spacing) + 1))
    row2 = _second_row_shift(im, c0, s * k, np) if per_row < N_CARDS else 0
    return [(c0 + i * spacing, 0) if i < per_row else (c0 + (i - per_row) * spacing, row2)
            for i in range(N_CARDS)], s * k


def _second_row_shift(im, cx, s, np):
    """Alt satırdaki kartların, üst satıra göre dikey kayması (px): ilk kartın sütununda, kartın altındaki koyu
    boşluktan sonra yeniden parlak zemine geçilen satır. Bulunamazsa kartın varsayılan satır aralığı."""
    top = _card_top(im, cx, s, np)
    y0 = int(top + 150 * s)
    strip = np.asarray(im.crop((int(cx - 30 * s), y0, int(cx + 30 * s), im.height)).convert("RGB")).astype(float)
    val = strip.max(2).mean(1) / 255
    run, dark = max(2, int(20 * s)), 0       # kart zemini uzun parlak bir bölge; fiyat rakamları kısa
    for y in range(len(val) - run):
        if val[y] < 0.30:
            dark += 1
        elif dark >= 10 * s and (val[y:y + run] > 0.35).all():
            return int(y0 + y - top)
    return int(round(CARD_ROW_DY * s))


def _price_moving(a, b, cx, s, np):
    """İki ardışık karede kartın fiyat yazısı (beyaz maske) farklı mı: yenileme animasyonunda kart kayıyor /
    beliriyor, o karede rakamlar "emin" ama yanlış okunabiliyor (GitHub #6). İkon parlaması maskeyi değiştirmez."""
    box = (int(cx - 6 * s), int(312 * s), int(cx + 62 * s), int(385 * s))
    ma = _white(np.asarray(a.crop(box)), np, 150, 80)
    mb = _white(np.asarray(b.crop(box)), np, 150, 80)
    n = max(int(ma.sum()), int(mb.sum()))
    if n == 0:
        return False

    def grow(m):                     # 1 px tolerans: kenar pikselindeki küçük farklar hareket sayılmasın
        g = m.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            g |= np.roll(np.roll(m, dy, 0), dx, 1)
        return g
    diff = (ma & ~grow(mb)) | (mb & ~grow(ma))
    return int(diff.sum()) > 0.15 * n


def read_offers(im, item_names=None, im2=None):
    """PIL görüntüsünden 8 teklifi okur. im2: ~0.15 sn sonra yakalanan ikinci kare (varsa fiyatı hâlâ hareket
    eden kart okunmaz, "card still moving" sorunu verilir; tekrar okuma onu yeniden dener).
    Dönen: [{"slot","text","cid","qty","price","sold","excluded"}]"""
    np, Image, pytesseract = _imports()
    W, H = im.size
    H0 = H                           # kart çerçevesi koordinatları büyütmeden önceki (ekrandaki) boyuta göre
    if im2 is not None and im2.size != im.size:
        im2 = None
    if H < 1400:
        # 1080p gibi düşük çözünürlükte beyaz yazı piksel piksel ayrılınca rakamlar bozuluyor:
        # önce yumuşak büyütüp 1440p boyutunda oku (rakam şablonları da bu boyuttan)
        im = im.resize((round(W * 1440 / H), 1440), Image.LANCZOS)
        if im2 is not None:
            im2 = im2.resize(im.size, Image.LANCZOS)
        W, H = im.size
    s = H / 1440.0
    # kartları ekranda bul; bulunamazsa 1440p varsayılan konumları (arayüz sola dayalı, geniş ekranda da)
    found = _locate_cards(im, s, np)
    if found:
        centers, s = found
    else:
        centers = [((CARD_X0 + CARD_DX * i) * s, 0) for i in range(N_CARDS)]
    full, full2 = im, im2
    # her satırın dikey hizası: üst kenarı bulunan kartların ortancası (satılmış kart sönük, kenarı bulunamıyor)
    row_tops = {}
    for cx, row_dy in centers:
        t = _card_top(full.crop((0, row_dy, W, H)) if row_dy else full, cx, s, np, default=None)
        if t is not None and abs(t - CARD_TOP * s) <= 80 * s:
            row_tops.setdefault(row_dy, []).append(t)
    row_top = {k: float(np.median(v)) for k, v in row_tops.items()}
    cn = bool(zh_ocr_tag())          # Windows'ta Çince OCR kurulu mu
    _load_templates(np)              # rakam şablonları bir kez, iş parçacıkları başlamadan yüklensin

    def one(i):
        """Tek kartı okur. -> (kayıt, Çince tanındı mı)"""
        is_cn = False
        cx, row_dy = centers[i]
        # alt satırdaki kart: görüntü o satır üstte olacak şekilde kaydırılır, gerisi aynı
        im = full.crop((0, row_dy, W, H)) if row_dy else full
        im2 = (full2.crop((0, row_dy, W, H)) if row_dy else full2) if full2 is not None else None
        # dikey hizalama: kartın bulunan üst kenarına göre (yenileme animasyonu, farklı arayüz ölçeği)
        top = _card_top(im, cx, s, np, default=None)
        if top is None or abs(top - CARD_TOP * s) > 80 * s:
            top = row_top.get(row_dy, CARD_TOP * s)
        dy = int(round(top - CARD_TOP * s))
        cim = im.crop((0, dy, W, int(460 * s) + dy)) if dy else im     # bu kart için kaydırılmış görünüm
        sold = _sold_out(cim, cx, s, np) or (dy != 0 and _sold_out(im, cx, s, np))   # satılmış kart sönük: üst kenar şaşabilir
        rec = {"slot": i + 1, "text": "", "cid": None, "qty": 1, "price": None, "sold": sold, "excluded": False,
               "price_why": "", "problem": ""}
        # kartın (fiyat satırı dahil) yakalanan görüntüdeki kutusu, ekran pikseliyle: (x0, y0, x1, y1)
        k = H0 / H
        rec["box"] = tuple(int(round(v * k)) for v in (cx - CARD_HALF_W * s, row_dy + top - 4 * s,
                                                        cx + CARD_HALF_W * s, row_dy + top + CARD_BOX_H * s))
        if not sold:
            # eşya tanınana kadar (ve adet "x20" gibi okunana kadar) gevşek eşikleri de dene
            best = None
            if cn and _LAST["cn"]:                  # Çince istemci: önce Çince OCR (Tesseract boşuna uğraşmasın)
                txt, got = _name_cn(cim, cx, s)
                if got[0] or got[2]:
                    best = (txt, got)
            for lo, sp in (() if best else NAME_THRESHOLDS):
                txt = _read_name(cim, cx, s, np, Image, pytesseract, lo, sp)
                got = match_item(txt, item_names)
                has_qty = bool(re.search(r"x\s?\d", txt.lower()))
                if best is None or (got[0] or got[2]) and not (best[1][0] or best[1][2]):
                    best = (txt, got)
                if (got[0] or got[2]) and has_qty:
                    best = (txt, got)
                    break
            if cn and not (best[1][0] or best[1][2]):      # İngilizce tanınmadı: Çince istemci olabilir
                txt, got = _name_cn(cim, cx, s)
                if got[0] or got[2]:
                    best = (txt, got)
                    is_cn = True
            elif cn and _LAST["cn"] and re.search(r"[一-鿿]", best[0]):
                is_cn = True
            rec["text"] = best[0]
            try:  # kartın ikonu (küçük panelde göstermek için): yenileme animasyonunda kart kayabildiği
                #     için kesit, kartın bulunan üst kenarına göre alınır
                rec["icon"] = im.crop((int(cx - 52 * s), int(top + 34 * s), int(cx + 52 * s),
                                       int(top + 144 * s))).resize((40, 40), Image.LANCZOS)
            except Exception:
                rec["icon"] = None
            rec["cid"], rec["qty"], rec["excluded"] = best[1]
            moving = False
            if im2 is not None and not rec["excluded"]:
                cim2 = im2.crop((0, dy, W, int(460 * s) + dy)) if dy else im2
                moving = _price_moving(cim, cim2, cx, s, np)
            if moving:
                rec["price"], rec["glyphs"], rec["price_why"] = None, [], "card still moving (refresh animation)"
            elif not rec["excluded"]:
                rec["price"], rec["glyphs"], rec["price_why"] = _read_price(cim, cx, s, np, Image, pytesseract)
                rec["price_img"] = cim.crop((int(cx - 30 * s), int(325 * s), int(cx + 62 * s), int(370 * s)))
            # Okunamayan kartın NEDENİ (panelde gösterilir)
            if not rec["excluded"]:
                if rec["cid"] is None:
                    t = rec["text"].strip()
                    rec["problem"] = "item name not recognized" + (f" (read: “{t[-22:]}”)" if t else " (name not read)")
                elif rec["price"] is None:
                    rec["problem"] = "price not read: " + (rec["price_why"] or "unknown reason")
        return rec, is_cn

    # kartlar paralel okunur: süre çoğunlukla Tesseract'ın her çağrıda açılan işleminde geçiyor
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        res = list(ex.map(one, range(N_CARDS)))
    out = [r for r, _ in res]
    n_cn = sum(1 for _, c in res if c)   # bu okumada Çince tanınan kart sayısı
    if cn and any(not o["sold"] for o in out):
        _LAST["cn"] = n_cn >= 3                    # sonraki okumada hangi dil önce denensin
    return out


def looks_like_blessings(offers):
    """Ekranda gerçekten Blessings var mı? (en az 3 kart tanındı/sold out)"""
    ok = sum(1 for o in offers if o["sold"] or o["excluded"] or (o["cid"] and o["price"] is not None))
    return ok >= 3


def save_samples(offers, folder, keep=300):
    """Fiyat bölgelerini küçük PNG olarak saklar (rakam tanımayı geliştirmek için)."""
    import time as _t
    os.makedirs(folder, exist_ok=True)
    stamp = _t.strftime("%Y%m%d-%H%M%S")
    for o in offers:
        img = o.get("price_img")
        if img is None:
            continue
        img.save(os.path.join(folder, f"{stamp}_k{o['slot']}_{o['price'] if o['price'] is not None else 'x'}.png"))
    files = sorted(f for f in os.listdir(folder) if f.endswith(".png"))
    for f in files[:-keep]:
        try:
            os.remove(os.path.join(folder, f))
        except OSError:
            pass
