# -*- coding: utf-8 -*-
"""Sistem tepsisi (gizli simgeler) desteği ve uygulama simgesi."""
import os
import sys

SKIP_EXE = ("crash", "unins", "redist", "setup", "helper", "vc_", "dxsetup", "report", "easyanticheat", "eac")


def cube_icon(size=64):
    """Yedek simge: izometrik turuncu küp (kendi çizimimiz)."""
    from PIL import Image, ImageDraw
    s = size
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c, t, m, b = s / 2, s * 0.08, s * 0.32, s * 0.92
    l, r = s * 0.1, s * 0.9
    top = [(c, t), (r, m - s * 0.02), (c, m + s * 0.2), (l, m - s * 0.02)]
    left = [(l, m - s * 0.02), (c, m + s * 0.2), (c, b), (l, b - s * 0.22)]
    right = [(r, m - s * 0.02), (c, m + s * 0.2), (c, b), (r, b - s * 0.22)]
    d.polygon(top, fill=(255, 196, 92, 255))
    d.polygon(left, fill=(226, 120, 30, 255))
    d.polygon(right, fill=(170, 74, 18, 255))
    for poly in (top, left, right):
        d.line(poly + [poly[0]], fill=(60, 24, 6, 255), width=max(1, s // 32))
    return im


def find_game_exe(log_path):
    """Log yolundan oyun klasörünü bul, içindeki ana .exe'yi döndür."""
    if not log_path:
        return None
    p = os.path.normpath(log_path)
    parts = p.split(os.sep)
    low = [x.lower() for x in parts]
    if "ue_game" in low:
        root = os.sep.join(parts[:low.index("ue_game")])
    else:
        root = os.path.dirname(os.path.dirname(os.path.dirname(p)))
    if not os.path.isdir(root):
        return None
    cands = []
    base = root.rstrip("\\/").count(os.sep)
    for dp, dns, fns in os.walk(root):
        for f in fns:
            fl = f.lower()
            if fl.endswith(".exe") and not any(k in fl for k in SKIP_EXE):
                full = os.path.join(dp, f)
                try:
                    cands.append(("torchlight" in fl, os.path.getsize(full), full))
                except OSError:
                    pass
        if dp.count(os.sep) - base >= 4:
            dns[:] = []
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][2]


def exe_icon(path, size=64):
    """Windows: bir .exe'nin simgesini PIL görüntüsü olarak al."""
    if sys.platform != "win32" or not path:
        return None
    import ctypes
    from ctypes import wintypes
    from PIL import Image

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                                            wintypes.UINT, wintypes.UINT]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int,
                                  ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    hicon, iid = wintypes.HICON(), wintypes.UINT()
    n = user32.PrivateExtractIconsW(path, 0, size, size, ctypes.byref(hicon), ctypes.byref(iid), 1, 0)
    if n < 1 or not hicon:
        return None
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    img = None
    try:
        if hbmp and bits:
            old = gdi32.SelectObject(mem, hbmp)
            user32.DrawIconEx(mem, 0, 0, hicon, size, size, 0, None, 3)   # DI_NORMAL
            raw = ctypes.string_at(bits, size * size * 4)
            img = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1).copy()
            if img.getextrema()[3][1] == 0:            # eski tip simge: alfa yok
                img.putalpha(255)
            gdi32.SelectObject(mem, old)
    finally:
        if hbmp:
            gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)
        user32.DestroyIcon(hicon)
    return img


ICON_URL = "https://cdn.tlidb.com/UI/Textures/Common/Icon/GamePlay/UI_S3BossTicket1_Icon_128.webp"  # Desire Core - Record
ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "userdata", "icon.png")   # indirilen, oyunun görseli


def desire_core_icon():
    """Klasördeki simge.png; yoksa tlidb'den indirip simge.png olarak kaydeder."""
    from PIL import Image
    if os.path.isfile(ICON_FILE):
        try:
            return Image.open(ICON_FILE).convert("RGBA")
        except Exception:
            pass
    import io
    import urllib.request
    req = urllib.request.Request(ICON_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=5) as r:
        raw = r.read()
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    img.thumbnail((128, 128))
    try:
        img.save(ICON_FILE)
    except Exception:
        pass
    return img


def app_icon(log_path):
    """Sıra: Desire Core simgesi (simge.png / tlidb) -> oyunun .exe simgesi -> çizilmiş küp."""
    try:
        img = desire_core_icon()
        if img is not None:
            return img
    except Exception:
        pass
    try:
        img = exe_icon(find_game_exe(log_path))
        if img is not None:
            return img
    except Exception:
        pass
    try:
        return cube_icon()
    except Exception:
        return None


class Tray:
    """pystray ile sistem tepsisi simgesi. on_open/on_quit başka thread'den çağrılır."""

    def __init__(self, image, on_open, on_quit):
        self.icon = None
        self.ok = False
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open", lambda i, it: on_open(), default=True),
                pystray.MenuItem("Quit", lambda i, it: on_quit()),
            )
            self.icon = pystray.Icon("tli_blessing", image, "TLI Blessing Helper", menu)
            self.icon.run_detached()
            self.ok = True
        except Exception:
            self.icon = None

    def stop(self):
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
