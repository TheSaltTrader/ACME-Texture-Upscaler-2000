"""Sorting images before they reach the AI - ACME Texture Upscaler 2000.

Every lesson here was paid for in a game port (the Hulk, Ninja Gaiden II,
Fable II, Bloodborne, Def Jam and Ninja Gaiden Black texture packs; see
TEXTURE_UPGRADE_GUIDE in those projects). An AI upscaler is trained on photos.
It is excellent on texture art and bad at a few kinds of image:

  text      lettering: it invents letter-shaped "slop"         -> sharp resize
  maybe     lettering it cannot read with confidence           -> original
  soft      halos, glows, smoke, already-blurry images: it
            turns smooth falloff into grain                     -> original
  light     lightmaps, shadow maps, LUTs, ramps, 1-D strips,
            flat fills, noise/dither, HDR/mask data: it
            changes the data (the lighting flips, not detail)   -> original
  tiny      under 32 px: nothing to recover, breaks batches     -> original
  lowq      heavy JPEG blocking: a texture model sharpens the
            blocks, so a JPEG-trained model is used instead     -> AI (x4plus)
  normal    normal maps: vectors, not colours                   -> AI + renormalise
  ai        everything else                                     -> AI

Each image gets one class and a one-line reason. The measurements are cheap
(numpy on a 1024 px crop at most); only the images left after them are read by
Windows' own OCR (Windows.Media.Ocr through PowerShell - the same script the
Hulk and Ninja Gaiden II ports use, nothing to download).
"""

import csv
import fnmatch
import os
import re
import struct
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# class -> (short label, what happens, longer description for the report)
CLASSES = {
    "ai":     ("Art", "AI upscale", "texture art: the AI model"),
    "normal": ("Normal map", "AI + renormalise", "normal map: upscaled, vectors renormalised, never colour-processed"),
    "lowq":   ("Low quality", "AI (JPEG-trained model)", "heavy JPEG blocking: a model trained on compressed images"),
    "text":   ("Lettering", "sharp resize", "lettering the OCR reads: sharp resize, never AI"),
    "maybe":  ("Possible lettering", "kept (512+ px: sharp resize)",
               "lettering the OCR cannot read with confidence: original kept; a large atlas gets a sharp resize"),
    "soft":   ("Soft / blurry", "kept", "no fine detail to recover (glow, halo, smoke, blurry): original kept"),
    "light":  ("Lighting / data", "kept", "lighting or data (lightmap, LUT, ramp, strip, flat, noise): original kept"),
    "tiny":   ("Tiny", "kept", "too small for the AI: original kept"),
    "big":    ("Too large", "kept", "already at the size limit: original kept"),
    "bad":    ("Unreadable", "skipped", "could not be decoded"),
}
CLASS_ORDER = ["ai", "normal", "lowq", "text", "maybe", "soft", "light", "tiny", "big", "bad"]
KEPT_CLASSES = {"maybe", "soft", "light", "tiny", "big"}


class Thresholds:
    """Every number the sorting uses (the Advanced panel edits these).

    Defaults are tuned on contact sheets of real game dumps (Hulk 2,775,
    Fable II 6,800 with its hand-checked marks, Ninja Gaiden Black, Def Jam,
    Bloodborne); see README "How images are sorted"."""

    def __init__(self):
        self.tiny_side = 32          # smaller than this on either side -> tiny (kept)
        self.strip_aspect = 8.0      # long side / short side at least this -> 1-D strip/ramp (kept)
        self.flat_std = 1.5          # luminance spread under this (0-255) -> flat fill (kept)
        self.soft_pooled = 0.003     # detail ratio under this -> soft (kept), any size
        self.glow_pooled = 0.015     # glow sprites (<=256 px, dark border): detail under this
        self.small_glow_pooled = 0.04  # small glow sprites (<=64 px, dark border)
        self.soft_rd = 0.15          # ...and the opaque part's ratio under this
        self.noise_corr = 0.10       # neighbour correlation under this -> noise/dither (kept)
        self.jpeg_block = 1.8        # 8x8 block-edge ratio over this in a .jpg -> low quality
        self.jpeg_block_other = 2.6  # ...in any other file (re-saved JPEGs)
        self.max_side = 4096         # no output larger than this a side
        self.use_names = True        # name hints (lightmap, _lm, lut, ramp, _nrm ...)
        self.use_ocr = True          # Windows OCR for lettering

    def as_dict(self):
        return dict(self.__dict__)


# --- name hints ------------------------------------------------------------------

# Matched against the file name split into tokens at every non-letter/digit
# ("Rock_Wall_LM.dds" -> rock, wall, lm), plus a few whole-word prefixes.
LIGHT_TOKENS = {
    "lightmap", "lightmaps", "lm", "lmap", "light", "lights", "lighting", "shadow", "shadows",
    "shadowmap", "shdw", "lut", "luts", "ramp", "ramps", "gradientmap", "irradiance", "gi",
    "envmap", "env", "reflection", "refl", "cubemap", "cube", "probe", "ao_lm", "bakedlight",
    "falloff", "attenuation", "atten", "ies", "noise", "dither", "bluenoise", "brdf", "lookup",
}
NORMAL_TOKENS = {"n", "nrm", "nrml", "norm", "normal", "normals", "normalmap", "nm", "ddna", "nmap",
                 "bump", "bumpmap"}
NAME_PREFIXES_LIGHT = ("lightmap", "lm_", "gi_env", "shadowmap", "lut_", "ramp_")


def name_tokens(path):
    base = os.path.splitext(os.path.basename(path))[0].lower()
    # camelCase -> camel case
    base = re.sub(r"([a-z])([A-Z])", r"\1 \2", os.path.splitext(os.path.basename(path))[0]).lower()
    return [t for t in re.split(r"[^a-z0-9]+", base) if t]


def name_hint(path):
    """'light' / 'normal' / None from the file name alone."""
    toks = name_tokens(path)
    base = os.path.basename(path).lower()
    if any(base.startswith(p) for p in NAME_PREFIXES_LIGHT):
        return "light", "name starts with a lighting/LUT prefix"
    for t in toks[1:] if len(toks) > 1 else toks:
        if t in NORMAL_TOKENS:
            return "normal", "name says normal map ('%s')" % t
    for t in toks:
        if t in LIGHT_TOKENS:
            return "light", "name says lighting/data ('%s')" % t
    return None, None


# --- DDS header: the format says what the data is --------------------------------

DXGI_DATA = {
    # single channel (masks, coverage, height) and HDR float/BC6H (lighting)
    61: "R8", 65: "A8", 80: "BC4", 81: "BC4", 95: "BC6H", 96: "BC6H", 10: "RGBA16F",
    2: "RGBA32F", 41: "R32F", 54: "R16F", 34: "RG16F",
}
DXGI_NORMAL = {83: "BC5", 84: "BC5"}


def dds_kind(path):
    """('light'|'normal'|None, format name) from a DDS header."""
    try:
        with open(path, "rb") as f:
            h = f.read(148)
    except OSError:
        return None, ""
    if len(h) < 128 or h[:4] != b"DDS ":
        return None, ""
    pf_flags = struct.unpack_from("<I", h, 80)[0]
    fourcc = h[84:88]
    if fourcc == b"DX10" and len(h) >= 148:
        dxgi = struct.unpack_from("<I", h, 128)[0]
        if dxgi in DXGI_DATA:
            return "light", DXGI_DATA[dxgi]
        if dxgi in DXGI_NORMAL:
            return "normal", DXGI_NORMAL[dxgi]
        return None, "DXGI %d" % dxgi
    if fourcc in (b"ATI1", b"BC4U", b"BC4S"):
        return "light", fourcc.decode("latin-1")
    if fourcc in (b"ATI2", b"BC5U", b"BC5S"):
        return "normal", fourcc.decode("latin-1")
    if pf_flags & 0x4:   # DDPF_FOURCC
        if fourcc in (b"q\x00\x00\x00", b"t\x00\x00\x00"):   # D3DFMT_A16B16G16R16F / A32B32G32R32F
            return "light", "float"
        return None, fourcc.decode("latin-1", "replace").strip("\x00")
    bits = struct.unpack_from("<I", h, 88)[0]
    if bits == 8 and not (pf_flags & 0x40):   # 8-bit luminance or alpha only, no RGB
        return "light", "8-bit"
    return None, "%d-bit" % bits


# --- decoding --------------------------------------------------------------------

def load_rgba(path):
    """PIL RGBA of any supported file (first frame), or raises."""
    with Image.open(path) as im:
        im.load()
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            a = np.asarray(im, dtype=np.float32)
            a = np.clip(a / max(1.0, float(a.max())) * 255.0, 0, 255).astype(np.uint8)
            return Image.fromarray(a, "L").convert("RGBA"), "16-bit"
        if im.mode == "F":
            a = np.asarray(im, dtype=np.float32)
            a = np.clip(a * 255.0, 0, 255).astype(np.uint8)
            return Image.fromarray(a, "L").convert("RGBA"), "float"
        return im.convert("RGBA"), im.mode


# --- measurements ----------------------------------------------------------------

def _shrink_enlarge(ch):
    """2x box shrink then bilinear enlarge at pixel centres (Hulk's soft test)."""
    h, w = ch.shape
    half = (ch[0::2, 0::2] + ch[0::2, 1::2] + ch[1::2, 0::2] + ch[1::2, 1::2]) * 0.25
    hh, hw = half.shape
    sy = (np.arange(h) + 0.5) * 0.5 - 0.5
    sx = (np.arange(w) + 0.5) * 0.5 - 0.5
    y0 = np.floor(sy).astype(int)
    x0 = np.floor(sx).astype(int)
    fy = (sy - y0)[:, None]
    fx = (sx - x0)[None, :]
    y1 = np.clip(y0 + 1, 0, hh - 1)
    x1 = np.clip(x0 + 1, 0, hw - 1)
    y0 = np.clip(y0, 0, hh - 1)
    x0 = np.clip(x0, 0, hw - 1)
    a = half[y0][:, x0]
    b = half[y0][:, x1]
    c = half[y1][:, x0]
    d = half[y1][:, x1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def _crop(a, side=1024):
    h, w = a.shape[:2]
    if h <= side and w <= side:
        return a
    y = max(0, (h - side) // 2)
    x = max(0, (w - side) // 2)
    return a[y:y + side, x:x + side]


def measure(img):
    """Numbers about an RGBA PIL image. Cheap: a 1024 px centre crop at most."""
    w, h = img.size
    m = {"w": w, "h": h}
    full = np.asarray(img, dtype=np.uint8)
    alpha_full = full[..., 3]
    amin, amax = int(alpha_full.min()), int(alpha_full.max())
    if amin == 255:
        m["alpha"] = "opaque"
    elif amax == 0:
        # alpha all zero: an unused alpha channel when there is a picture in
        # the colour (Fable II's DXT1 decodes, many game dumps) - measure the
        # colour as opaque; "empty" only when the colour is blank too
        if full[..., :3].max() == full[..., :3].min():
            m["alpha"] = "empty"
        else:
            m["alpha"] = "unused"
            full = full.copy()
            full[..., 3] = 255
    else:
        mid = np.count_nonzero((alpha_full > 0) & (alpha_full < 255))
        m["alpha"] = "cutout" if mid == 0 else "blended"
    if w < 4 or h < 4:   # too small to measure (and always "tiny")
        f = full.astype(np.float32)
        m.update(mean=[round(float(v), 1) for v in f[..., :3].reshape(-1, 3).mean(axis=0)],
                 lum_std=float(f[..., :3].std()), alpha_std=float(f[..., 3].std()), sat=0.0,
                 chan_std=float(f[..., :3].std()), pooled=1.0, rd=1.0, border=0.0, peak=0.0, p99=0.0, corr=1.0, block=1.0,
                 nsig=False, unit=0.0)
        return m
    a = _crop(full).astype(np.float32)
    ch, cw = a.shape[0] & ~1, a.shape[1] & ~1
    a = a[:ch, :cw]
    alpha = a[..., 3] / 255.0
    rgb = a[..., :3]
    wsum = float(alpha.sum())
    if wsum > 0:
        means = (rgb * alpha[..., None]).sum(axis=(0, 1)) / wsum
    else:
        means = rgb.reshape(-1, 3).mean(axis=0) if rgb.size else np.zeros(3)
    m["mean"] = [round(float(v), 1) for v in means]
    lum = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    vis = alpha > 0.5
    if vis.sum() >= 16:
        m["lum_std"] = float(lum[vis].std())
    else:
        m["lum_std"] = float(lum.std())
    m["chan_std"] = float(max(rgb[..., c][vis].std() if vis.sum() >= 16 else rgb[..., c].std()
                              for c in range(3)))
    m["alpha_std"] = float(a[..., 3].std())
    m["sat"] = float((rgb.max(axis=2) - rgb.min(axis=2))[vis].mean()) if vis.sum() else 0.0

    # soft (Hulk's soft_stats, as ported for NG2 / Fable II)
    m["pooled"] = m["rd"] = 1.0
    m["border"] = m["peak"] = m["p99"] = 0.0
    if cw >= 4 and ch >= 4:
        pre = np.concatenate([rgb * alpha[..., None], a[..., 3:4]], axis=2)
        up = np.stack([_shrink_enlarge(pre[..., c]) for c in range(4)], axis=2)
        d = pre - up
        n = float(cw * ch)
        err = float((d * d).sum() / n)
        var = float(sum(pre[..., c].var() for c in range(4)))
        m["pooled"] = err / (var + 1.0)
        worst = np.abs(d).max(axis=2)
        m["p99"] = float(np.percentile(worst, 99)) / 255.0
        bright = pre[..., :3].max(axis=2)
        m["peak"] = float(np.percentile(bright, 99))
        opaque = a[..., 3] >= 128
        rd = 0.0
        if opaque.sum() >= 16:
            ups = np.stack([_shrink_enlarge(rgb[..., c]) for c in range(3)], axis=2)
            dd = (rgb - ups)[opaque]
            rmse = float((dd * dd).sum()) / (3.0 * opaque.sum())
            rvar = float(rgb[opaque].var(axis=0).sum())
            rd = rmse / (rvar / 3.0 + 1.0)
        m["rd"] = rd
        edge = np.zeros((ch, cw), bool)
        edge[:2, :] = edge[-2:, :] = edge[:, :2] = edge[:, -2:] = True
        m["border"] = float(bright[edge].mean())

    # neighbour correlation of luminance: ~1 for pictures, ~0 for white noise,
    # negative for blue noise / dither patterns
    L = lum - lum.mean()
    den = float((L * L).sum())
    if den > 1e-3 and cw >= 4 and ch >= 4:
        cx = float((L[:, 1:] * L[:, :-1]).sum()) / den
        cy = float((L[1:, :] * L[:-1, :]).sum()) / den
        m["corr"] = (cx + cy) / 2.0
    else:
        m["corr"] = 1.0

    # 8x8 JPEG blocking: mean step across block edges / mean step elsewhere
    m["block"] = 1.0
    if cw >= 32 and ch >= 32:
        dx = np.abs(lum[:, 1:] - lum[:, :-1])
        dy = np.abs(lum[1:, :] - lum[:-1, :])
        # across 8x8 block edges vs across the middle of the blocks (offset 3):
        # JPEG steps at the edges only; DXT/BC blocks are 4x4, so they step at
        # both and score ~1 (Fable II's DXT1 art scored up to 1.9 against
        # "everywhere else", which is why the middle is the reference)
        rx = (dx[:, 7::8].mean() + 0.5) / (dx[:, 3::8].mean() + 0.5)
        ry = (dy[7::8, :].mean() + 0.5) / (dy[3::8, :].mean() + 0.5)
        m["block"] = float(min(rx, ry))   # both directions: a tile grid is usually one

    # normal-map signature (TEXTURE_UPGRADE_GUIDE s6): means ~128/128, blue high
    sm = np.asarray(img.convert("RGB").resize((32, 32), Image.Resampling.BOX), dtype=np.float32)
    r, g, b = (float(sm[..., c].mean()) for c in range(3))
    m["nsig"] = (abs(r - 128) <= 28 and abs(g - 128) <= 28 and b > 180)
    # a real tangent-space normal map is mostly unit vectors
    v = sm / 127.5 - 1.0
    ln = np.sqrt((v * v).sum(axis=2))
    m["unit"] = float(np.mean(np.abs(ln - 1.0) < 0.2))
    return m


def classify_measured(path, m, th, fmt_hint=None):
    """(class, reason) from the numbers alone; 'ocr' when only the OCR can tell."""
    w, h = m["w"], m["h"]
    if th.use_names:
        hint, why = name_hint(path)
    else:
        hint, why = None, None
    if fmt_hint and fmt_hint[0] == "light":
        return "light", "data format (%s): masks / HDR / lighting" % fmt_hint[1]
    if min(w, h) < th.tiny_side:
        return "tiny", "%dx%d is under %d px" % (w, h, th.tiny_side)
    if max(w, h) / float(min(w, h)) >= th.strip_aspect:
        return "light", "long strip %dx%d (ramp, LUT, trim or banner)" % (w, h)
    if m["alpha"] == "empty":
        return "light", "blank (fully transparent, no colour)"
    # normal maps before "flat": a gentle normal map has little luminance spread
    if hint == "normal" or (fmt_hint and fmt_hint[0] == "normal"):
        if m["nsig"] or m["unit"] > 0.5 or (fmt_hint and fmt_hint[0] == "normal"):
            return "normal", why or "two-channel normal format (%s)" % fmt_hint[1]
    if m["nsig"] and m["unit"] > 0.6 and m["chan_std"] >= th.flat_std:
        return "normal", "normal-map colours (mean %d/%d/%d)" % tuple(int(v) for v in m["mean"])
    if m["chan_std"] < th.flat_std and m["alpha_std"] < th.flat_std:
        return "light", "flat fill (spread %.1f)" % m["chan_std"]
    if hint == "light":
        return "light", why
    # noise / dither DATA: uncorrelated neighbours over a wide spread (white
    # noise), or a regular anti-correlated pattern (Bayer / blue-noise dither).
    # Gravel, asphalt and concrete are uncorrelated too but narrow (spread
    # 4-22 on Hulk's 19 such textures; real noise 39-75), so they stay art.
    if (m["corr"] < th.noise_corr and m["lum_std"] >= 35) or m["corr"] < -0.3:
        return "light", "noise / dither (neighbour correlation %.2f)" % m["corr"]
    side = max(w, h)
    if m["rd"] < th.soft_rd:
        if m["pooled"] < th.soft_pooled:
            return "soft", "no fine detail (detail %.4f)" % m["pooled"]
        sprite = m["border"] < 8 and m["peak"] >= 50
        if sprite and side <= 256 and m["p99"] < 0.14 and m["pooled"] < th.glow_pooled:
            return "soft", "glow sprite (dark border, detail %.4f)" % m["pooled"]
        if sprite and side <= 64 and m["pooled"] < th.small_glow_pooled:
            return "soft", "small glow sprite (detail %.4f)" % m["pooled"]
    is_jpeg = os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".jfif")
    lim = th.jpeg_block if is_jpeg else th.jpeg_block_other
    if m["block"] > lim:
        return "lowq", "JPEG blocking (block-edge ratio %.2f)" % m["block"]
    return "ocr", ""


# --- OCR -------------------------------------------------------------------------

def _ocr_script_path(work):
    from acme_ocr_script import SCRIPT
    p = os.path.join(work, "acme_find_text.ps1")
    with open(p, "w", encoding="utf-8-sig") as f:
        f.write(SCRIPT)
    return p


def run_ocr(pngs, work, procs=4, progress=None, cancel=None):
    """{png path (normcase): 'text'|'maybe'} or None when the OCR is unavailable.

    The list is split across `procs` PowerShell processes (Windows OCR is
    single-threaded per process)."""
    if not pngs:
        return {}
    os.makedirs(work, exist_ok=True)
    script = _ocr_script_path(work)
    procs = max(1, min(procs, (len(pngs) + 19) // 20))
    chunks = [pngs[i::procs] for i in range(procs)]
    res = {}
    lock = threading.Lock()
    done = [0]
    failed = [False]
    flags = 0x08000000 if os.name == "nt" else 0
    running = []

    def one(k, chunk):
        d = os.path.join(work, "ocr%d" % k)
        os.makedirs(d, exist_ok=True)
        lst = os.path.join(d, "list.txt")
        out = os.path.join(d, "text.txt")
        pos = os.path.join(d, "possible.txt")
        chk = os.path.join(d, "checked.txt")
        for p in (out, pos, chk):
            if os.path.exists(p):
                os.remove(p)
        with open(lst, "w", encoding="utf-8") as fh:
            fh.write("\n".join(chunk) + "\n")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
               "-List", lst, "-Out", out, "-Checked", chk, "-Possible", pos, "-Work", d]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             creationflags=flags)
        with lock:
            running.append(p)
        for line in p.stdout:
            if line.startswith("OCR "):
                with lock:
                    done[0] += 1
                    if progress:
                        progress(done[0], len(pngs))
            if cancel is not None and cancel.is_set():
                p.terminate()
                break
        rc = p.wait()
        if rc != 0:
            failed[0] = True
            return
        for path, cls in ((out, "text"), (pos, "maybe")):
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    for ln in fh:
                        ln = ln.strip()
                        if ln:
                            with lock:
                                res[os.path.normcase(os.path.abspath(ln))] = cls
            except OSError:
                pass

    threads = [threading.Thread(target=one, args=(k, c), daemon=True) for k, c in enumerate(chunks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if failed[0] and not res:
        return None
    return res


def ocr_available():
    """True when Windows OCR can be created (checked once, ~1 s)."""
    flags = 0x08000000 if os.name == "nt" else 0
    ps = ("Add-Type -AssemblyName System.Runtime.WindowsRuntime;"
          "$null=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime];"
          "if([Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()){exit 0}else{exit 2}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=flags,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def heuristic_text(img):
    """Fallback when Windows OCR is unavailable: lettering is two-tone (ink and
    ground), with many small, similar-height strokes in rows. Only ever gives
    'maybe' (original kept) - it is a guess, so it never sends text to a sharp
    resize."""
    a = np.asarray(img.convert("L"), dtype=np.float32)
    if a.shape[0] < 16 or a.shape[1] < 16:
        return False
    hist = np.histogram(a, bins=16, range=(0, 256))[0].astype(np.float32)
    hist /= hist.sum()
    top2 = np.sort(hist)[-2:].sum()
    if top2 < 0.6:
        return False
    thr = a.mean()
    ink = a < thr if (a < thr).mean() < 0.5 else a >= thr
    frac = ink.mean()
    if not (0.05 < frac < 0.4):
        return False
    # stroke transitions per row: lettering has many short runs
    tr = np.abs(np.diff(ink.astype(np.int8), axis=1)).sum(axis=1)
    rows = (tr >= 6).mean()
    return 0.15 < rows < 0.9


# --- whole folder ----------------------------------------------------------------

def read_overrides(root):
    """{class: [glob, ...]} from acme_keep.txt / acme_sharp.txt / acme_ai.txt in
    the input folder: one relative path or file-name pattern per line."""
    out = {}
    for fn, cls in (("acme_keep.txt", "keep"), ("acme_sharp.txt", "text"), ("acme_ai.txt", "ai")):
        p = os.path.join(root, fn)
        try:
            with open(p, encoding="utf-8-sig") as f:
                pats = [ln.split("#", 1)[0].strip().replace("\\", "/").lower() for ln in f]
            out[cls] = [x for x in pats if x]
        except OSError:
            pass
    return out


def override_for(rel, overrides):
    r = rel.replace("\\", "/").lower()
    base = r.rsplit("/", 1)[-1]
    for cls in ("ai", "text", "keep"):
        for pat in overrides.get(cls, ()):
            if fnmatch.fnmatch(r, pat) or fnmatch.fnmatch(base, pat):
                return cls, pat
    return None, None


class Item:
    __slots__ = ("path", "rel", "cls", "reason", "m", "size", "mtime")

    def __init__(self, path, rel):
        self.path = path
        self.rel = rel
        self.cls = None
        self.reason = ""
        self.m = None
        st = os.stat(path)
        self.size = st.st_size
        self.mtime = int(st.st_mtime)


CACHE_NAME = "classes_cache.csv"
CACHE_VERSION = "acme-classify-2"


def _cache_key(it, th):
    return "%s|%d|%d" % (it.rel.lower(), it.size, it.mtime)


def load_cache(path, th):
    out = {}
    sig = CACHE_VERSION + repr(sorted(th.as_dict().items()))
    try:
        with open(path, encoding="utf-8", newline="") as f:
            r = csv.reader(f)
            head = next(r, None)
            if not head or head[0] != sig:
                return {}
            for row in r:
                if len(row) >= 6:
                    out[row[0]] = (row[1], row[2], {"w": int(row[3]), "h": int(row[4]),
                                                    "pooled": float(row[5])})
    except OSError:
        pass
    return out


def save_cache(path, th, items):
    sig = CACHE_VERSION + repr(sorted(th.as_dict().items()))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([sig])
        for it in items:
            if it.cls and it.cls != "bad" and it.m:
                w.writerow([_cache_key(it, th), it.cls, it.reason, it.m.get("w", 0), it.m.get("h", 0),
                            "%.6f" % it.m.get("pooled", 1.0)])
    os.replace(tmp, path)


def classify_items(items, root, th, work, log=print, progress=None, cancel=None, cache_path=None,
                   threads=None):
    """Sort every Item in place (it.cls, it.reason). progress(stage, i, n)."""
    overrides = read_overrides(root)
    cache = load_cache(cache_path, th) if cache_path else {}
    todo = []
    for it in items:
        o, pat = override_for(it.rel, overrides)
        if o:
            it.cls = {"keep": "maybe", "text": "text", "ai": "ai"}[o]
            it.reason = "your acme_%s.txt (%s)" % ({"text": "sharp"}.get(o, o), pat)
            try:
                img, _ = load_rgba(it.path)
                it.m = measure(img)
            except Exception as e:    # noqa: BLE001
                it.cls, it.reason = "bad", "cannot decode: %s" % e
            continue
        c = cache.get(_cache_key(it, th))
        if c:
            it.cls, it.reason, it.m = c
            continue
        todo.append(it)
    if cache:
        log("%d image(s) sorted before (cached), %d to measure" % (len(items) - len(todo), len(todo)))

    ocr_dir = os.path.join(work, "ocr_in")
    os.makedirs(ocr_dir, exist_ok=True)
    ocr_wait = []
    lock = threading.Lock()
    counter = [0]

    def one(it):
        if cancel is not None and cancel.is_set():
            return
        try:
            fh = dds_kind(it.path) if it.path.lower().endswith(".dds") else (None, "")
            img, _mode = load_rgba(it.path)
            m = measure(img)
            it.m = m
            cls, why = classify_measured(it.path, m, th, fh if fh[0] else None)
            if cls == "ocr":
                if th.use_ocr:
                    # a PNG the OCR's GDI+ can read (TGA/DDS/WebP cannot be)
                    with lock:
                        k = len(ocr_wait)
                        ocr_wait.append(it)
                    p = os.path.join(ocr_dir, "%06d.png" % k)
                    img.save(p, compress_level=1)
                    it.reason = p
                else:
                    if heuristic_text(img):
                        cls, why = "maybe", "looks like lettering (two-tone strokes; OCR off)"
                    else:
                        cls, why = "ai", "texture art"
            if cls != "ocr":
                it.cls, it.reason = cls, why
        except Exception as e:      # noqa: BLE001
            it.cls, it.reason = "bad", "cannot decode: %s" % e
        with lock:
            counter[0] += 1
            if progress and counter[0] % 8 == 0:
                progress("measuring", counter[0], len(todo))

    with ThreadPoolExecutor(max_workers=threads or max(2, (os.cpu_count() or 4) - 1)) as ex:
        list(ex.map(one, todo))
    if progress:
        progress("measuring", len(todo), len(todo))

    if ocr_wait and not (cancel is not None and cancel.is_set()):
        log("reading %d image(s) for lettering with Windows OCR..." % len(ocr_wait))
        res = run_ocr([it.reason for it in ocr_wait], os.path.join(work, "ocr"),
                      procs=max(2, min(8, (os.cpu_count() or 4) // 2)),
                      progress=(lambda i, n: progress("reading lettering (OCR)", i, n)) if progress else None,
                      cancel=cancel)
        if res is None:
            log("Windows OCR is not available here - using the lettering heuristic instead")
        for it in ocr_wait:
            p = it.reason
            if res is None:
                try:
                    with Image.open(p) as im:
                        guess = heuristic_text(im.convert("RGBA"))
                except Exception:   # noqa: BLE001
                    guess = False
                it.cls, it.reason = ("maybe", "looks like lettering (two-tone strokes)") if guess \
                    else ("ai", "texture art")
            else:
                c = res.get(os.path.normcase(os.path.abspath(p)))
                if c == "text":
                    it.cls, it.reason = "text", "OCR reads a word"
                elif c == "maybe":
                    it.cls, it.reason = "maybe", "OCR finds letter-like shapes, not a sure word"
                else:
                    it.cls, it.reason = "ai", "texture art"
            try:
                os.remove(p)
            except OSError:
                pass
    for it in items:
        if it.cls is None:
            it.cls, it.reason = "bad", "not sorted (cancelled)"
    if cache_path and not (cancel is not None and cancel.is_set()):
        try:
            save_cache(cache_path, th, items)
        except OSError:
            pass
    return items


def counts(items):
    c = {k: 0 for k in CLASS_ORDER}
    for it in items:
        c[it.cls] = c.get(it.cls, 0) + 1
    return c


# --- report ----------------------------------------------------------------------

def write_report(folder, items, extra=None):
    """report.csv (one line per image) + summary.txt. extra: {rel: dict} of
    results from the upscale (action, out size, flags)."""
    os.makedirs(folder, exist_ok=True)
    extra = extra or {}
    with open(os.path.join(folder, "report.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "class", "what happens", "reason", "width", "height",
                    "detail", "corr", "jpeg_block", "result", "notes"])
        for it in items:
            m = it.m or {}
            e = extra.get(it.rel, {})
            w.writerow([it.rel, it.cls, CLASSES[it.cls][1], it.reason, m.get("w", ""), m.get("h", ""),
                        "%.4f" % m["pooled"] if "pooled" in m else "",
                        "%.2f" % m["corr"] if "corr" in m else "",
                        "%.2f" % m["block"] if "block" in m else "",
                        e.get("result", ""), e.get("notes", "")])
    c = counts(items)
    lines = ["ACME Texture Upscaler 2000 - how the images were sorted", ""]
    for k in CLASS_ORDER:
        if c.get(k):
            lines.append("%-20s %6d   -> %s" % (CLASSES[k][0], c[k], CLASSES[k][1]))
    lines.append("%-20s %6d" % ("Total", len(items)))
    with open(os.path.join(folder, "summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return lines


def _font(size):
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:   # noqa: BLE001
            pass
    return ImageFont.load_default()


def contact_sheets(folder, items, per_sheet=48, cols=8, cell=160, max_sheets=6, cancel=None):
    """sheets/<class>_NN.png: thumbnails (on a checkerboard, so alpha shows)
    labelled with the file name and the reason. Returns the files written."""
    out = []
    d = os.path.join(folder, "sheets")
    os.makedirs(d, exist_ok=True)
    font = _font(11)
    big = _font(16)
    by = {}
    for it in items:
        by.setdefault(it.cls, []).append(it)
    for cls in CLASS_ORDER:
        lst = by.get(cls, [])
        if not lst or cls == "bad":
            continue
        nsheets = min(max_sheets, (len(lst) + per_sheet - 1) // per_sheet)
        # spread the picks across the whole class, not the first N names
        if len(lst) > nsheets * per_sheet:
            step = len(lst) / float(nsheets * per_sheet)
            lst = [lst[int(i * step)] for i in range(nsheets * per_sheet)]
        for s in range(nsheets):
            if cancel is not None and cancel.is_set():
                return out
            part = lst[s * per_sheet:(s + 1) * per_sheet]
            rows = (len(part) + cols - 1) // cols
            lab = 30
            W, H = cols * cell, 34 + rows * (cell + lab)
            sheet = Image.new("RGB", (W, H), (32, 32, 36))
            dr = ImageDraw.Draw(sheet)
            dr.text((8, 8), "%s - %s (%d in class; sheet %d of %d)" % (
                CLASSES[cls][0], CLASSES[cls][1], len(by[cls]), s + 1, nsheets), fill=(255, 220, 120), font=big)
            for i, it in enumerate(part):
                x, y = (i % cols) * cell, 34 + (i // cols) * (cell + lab)
                try:
                    img, _ = load_rgba(it.path)
                    img.thumbnail((cell - 8, cell - 8), Image.Resampling.LANCZOS)
                    bg = Image.new("RGB", img.size, (200, 200, 200))
                    ck = Image.new("RGB", (16, 16), (150, 150, 150))
                    for yy in range(0, img.size[1], 16):
                        for xx in range(0, img.size[0], 16):
                            if (xx // 16 + yy // 16) % 2:
                                bg.paste(ck, (xx, yy))
                    bg.paste(img, (0, 0), img)
                    sheet.paste(bg, (x + (cell - img.size[0]) // 2, y + (cell - img.size[1]) // 2))
                except Exception:   # noqa: BLE001
                    dr.text((x + 8, y + 8), "unreadable", fill=(255, 80, 80), font=font)
                name = os.path.basename(it.rel)
                if len(name) > 26:
                    name = name[:12] + ".." + name[-12:]
                dr.text((x + 4, y + cell), name, fill=(230, 230, 230), font=font)
                why = it.reason if len(it.reason) <= 30 else it.reason[:29] + "."
                dr.text((x + 4, y + cell + 14), why, fill=(150, 200, 255), font=font)
            p = os.path.join(d, "%s_%02d.png" % (cls, s + 1))
            sheet.save(p)
            out.append(p)
    return out
