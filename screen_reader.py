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
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


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
            try:
                with open(f, encoding="utf-8") as fh:
                    for t in json.load(fh):
                        _templates.append((t["d"], np.array(t["v"], dtype=float), float(t["a"])))
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


def _classify(vec, asp, np):
    """-> (rakam, emin_mi)"""
    best = {}
    img = vec.reshape(GH, GW)
    shifts = [np.roll(np.roll(img, dy, 0), dx, 1).ravel() for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    for d, v, a in _load_templates(np):
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
    best = int(rows.argmax())
    lo = hi = best
    while lo > 0 and rows[lo - 1] > 0:
        lo -= 1
    while hi < len(rows) - 1 and rows[hi + 1] > 0:
        hi += 1
    band = m[lo:hi + 1]
    cols = np.nonzero(band.sum(0))[0]
    sub = band[:, cols.min():cols.max() + 1]

    # elmas simgesinin kenarı (rakamlardan belirgin boşlukla ayrık sol parçalar) rakam sanılmasın
    spans = []
    gl = _split_glyphs(sub, np, spans)
    while len(gl) > 1 and spans[1][0] - spans[0][1] > 4.5 * s:
        gl, spans = gl[1:], spans[1:]
    if gl and spans:
        sub = sub[:, spans[0][0]:]
    # 1) şablonlarla
    glyphs = [_glyph_vec(g, np, Image) for g in gl]
    t_txt, t_sure = "", bool(glyphs)
    for v, a in glyphs:
        d, sure = _classify(v, a, np)
        if d is None:
            t_txt, t_sure = "", False
            break
        t_txt += d
        t_sure = t_sure and sure

    # 2) Tesseract
    o_txt = ""
    img = _to_img(sub, np, Image, 4)
    for psm in (7, 8, 10):
        t = pytesseract.image_to_string(img, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789").strip()
        if t.isdigit():
            o_txt = t
            break

    # 3) karar: şablon eminse onu, değilse ikisi aynıysa onu; aksi halde okunamadı
    if t_txt and t_sure:
        return int(t_txt), glyphs, ""
    if t_txt and t_txt == o_txt:
        return int(t_txt), glyphs, ""
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
    red = ((a[..., 0] > 170) & (a[..., 1] < 110) & (a[..., 2] < 110)).sum()
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


CARD_TOP = 110        # kartın üst kenarı (1440p, yerinde dururken)


def _card_top(im, cx, s, np):
    """Kartın üst kenarının y'si: koyu arka plandan kartın renkli zeminine geçilen ilk satır
    (parlaklık 0.15 civarından 0.45 üstüne çıkar). Bulunamazsa yerinde dururkenki değer."""
    y0, y1 = int(60 * s), int(450 * s)
    strip = np.asarray(im.crop((int(cx - 30 * s), y0, int(cx + 30 * s), y1)).convert("RGB")).astype(float)
    val = strip.max(2).mean(1) / 255
    run = max(2, int(3 * s))
    for y in range(len(val) - run):
        if (val[y:y + run] > 0.35).all():
            return y0 + y
    return CARD_TOP * s


def read_offers(im, item_names=None):
    """PIL görüntüsünden 8 teklifi okur.
    Dönen: [{"slot","text","cid","qty","price","sold","excluded"}]"""
    np, Image, pytesseract = _imports()
    W, H = im.size
    if H < 1400:
        # 1080p gibi düşük çözünürlükte beyaz yazı piksel piksel ayrılınca rakamlar bozuluyor:
        # önce yumuşak büyütüp 1440p boyutunda oku (rakam şablonları da bu boyuttan)
        im = im.resize((round(W * 1440 / H), 1440), Image.LANCZOS)
        W, H = im.size
    s = H / 1440.0
    # 16:9'dan geniş ekranlarda arayüz ortalanır
    x_off = (W - 2560 * s) / 2 if W / H > 16 / 9 + 0.01 else 0
    out = []
    for i in range(N_CARDS):
        cx = x_off + (CARD_X0 + CARD_DX * i) * s
        sold = _sold_out(im, cx, s, np)
        rec = {"slot": i + 1, "text": "", "cid": None, "qty": 1, "price": None, "sold": sold, "excluded": False,
               "price_why": "", "problem": ""}
        if not sold:
            # eşya tanınana kadar (ve adet "x20" gibi okunana kadar) gevşek eşikleri de dene
            best = None
            for lo, sp in NAME_THRESHOLDS:
                txt = _read_name(im, cx, s, np, Image, pytesseract, lo, sp)
                got = match_item(txt, item_names)
                has_qty = bool(re.search(r"x\s?\d", txt.lower()))
                if best is None or (got[0] or got[2]) and not (best[1][0] or best[1][2]):
                    best = (txt, got)
                if (got[0] or got[2]) and has_qty:
                    best = (txt, got)
                    break
            rec["text"] = best[0]
            try:  # kartın ikonu (küçük panelde göstermek için): yenileme animasyonunda kart kayabildiği
                #     için kesit, kartın bulunan üst kenarına göre alınır
                top = _card_top(im, cx, s, np)
                rec["icon"] = im.crop((int(cx - 52 * s), int(top + 34 * s), int(cx + 52 * s),
                                       int(top + 144 * s))).resize((40, 40), Image.LANCZOS)
            except Exception:
                rec["icon"] = None
            rec["cid"], rec["qty"], rec["excluded"] = best[1]
            if not rec["excluded"]:
                rec["price"], rec["glyphs"], rec["price_why"] = _read_price(im, cx, s, np, Image, pytesseract)
                rec["price_img"] = im.crop((int(cx - 30 * s), int(325 * s), int(cx + 62 * s), int(370 * s)))
            # Okunamayan kartın NEDENİ (panelde gösterilir)
            if not rec["excluded"]:
                if rec["cid"] is None:
                    t = rec["text"].strip()
                    rec["problem"] = "item name not recognized" + (f" (read: “{t[-22:]}”)" if t else " (name not read)")
                elif rec["price"] is None:
                    rec["problem"] = "price not read: " + (rec["price_why"] or "unknown reason")
        out.append(rec)
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
