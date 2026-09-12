"""ACME Texture Upscaler 2000 - a portable AI texture upscaler.

Point it at a folder, choose a model and a scale (2x / 3x / 4x), press Start.
Every image it finds is enlarged with an AI upscaler and written to an output
folder with the sub-folder tree preserved.

Portable: the program plus an `upscaler/` folder (the engine and its models)
sit side by side. Nothing is installed.

Models. It ships with Real-ESRGAN and discovers whatever models are present,
so the selector always reflects what is actually installed. It also recognises
two other portable engines if you drop them in beside Real-ESRGAN:
  upscaler/realcugan/realcugan-ncnn-vulkan.exe   (Real-CUGAN - strong on art)
  upscaler/waifu2x/waifu2x-ncnn-vulkan.exe       (waifu2x - clean line art)
Each is an ncnn-vulkan release you unzip into that sub-folder; the app finds
the engine and its model sets automatically. See README.txt.

Formats. The engines read PNG/JPG/WebP directly; BMP, TGA, TIFF and DDS are
decoded with Pillow first and written back in their own format where possible,
otherwise as PNG.
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, ttk

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

APP_NAME = "ACME Texture Upscaler 2000"
VERSION = "1.0.0"

NATIVE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
PIL_EXTS = {".bmp", ".tga", ".tif", ".tiff", ".dds"}
ALL_EXTS = NATIVE_EXTS | PIL_EXTS

# Friendly labels and native scale for the Real-ESRGAN models we know about.
# native_scale None means the model takes the scale itself (2/3/4); a number
# means the model is fixed at that scale and smaller factors are resized down.
REALESRGAN_KNOWN = {
    "realesr-animevideov3-x2": ("Fast 2x - anime video v3", 2),
    "realesr-animevideov3-x3": ("Fast 3x - anime video v3", 3),
    "realesr-animevideov3-x4": ("Fast 4x - anime video v3", 4),
    "realesrgan-x4plus": ("Photo 4x - x4plus (general, recommended)", 4),
    "realesrgan-x4plus-anime": ("Art 4x - x4plus-anime", 4),
    "realesrnet-x4plus": ("Soft 4x - net (fewer artifacts)", 4),
}

# The default the selector lands on: the widely-acclaimed general model, then
# fallbacks, whichever is installed.
PREFERRED_ORDER = [
    "Real-ESRGAN | Photo 4x - x4plus (general, recommended)",
    "Real-CUGAN | pro (art)",
    "Real-ESRGAN | Art 4x - x4plus-anime",
    "Real-ESRGAN | Fast 4x - anime video v3",
]

# Extra engines the app can fetch and install into upscaler/<dest>/ on request.
# Stable ncnn-vulkan release builds (self-contained: exe + models, no installer).
DOWNLOADS = {
    "Real-CUGAN - often more precise on drawn / game art": {
        "url": "https://github.com/nihui/realcugan-ncnn-vulkan/releases/download/"
               "20220728/realcugan-ncnn-vulkan-20220728-windows.zip",
        "exe": "realcugan-ncnn-vulkan.exe", "dest": "realcugan", "mb": 40},
    "waifu2x - clean line art and gentle denoise": {
        "url": "https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/"
               "20220728/waifu2x-ncnn-vulkan-20220728-windows.zip",
        "exe": "waifu2x-ncnn-vulkan.exe", "dest": "waifu2x", "mb": 55},
}


def install_download(entry, progress_cb):
    """Fetch a ncnn-vulkan engine zip and install it under upscaler/<dest>/.

    progress_cb(fraction_or_None, message) is called as it goes. Returns the
    install path on success; raises on failure.
    """
    dest = os.path.join(engine_root(), entry["dest"])
    with tempfile.TemporaryDirectory() as td:
        zpath = os.path.join(td, "dl.zip")
        progress_cb(None, "connecting...")

        def hook(blocks, bs, total):
            if total > 0:
                progress_cb(min(1.0, blocks * bs / total),
                            "downloading %.0f MB..." % (total / 1048576))
        urllib.request.urlretrieve(entry["url"], zpath, hook)
        progress_cb(None, "extracting...")
        xd = os.path.join(td, "x")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(xd)
        # Find the engine exe inside the extracted tree; its folder is the root.
        src_root = None
        for dp, _, fns in os.walk(xd):
            if entry["exe"] in fns:
                src_root = dp
                break
        if not src_root:
            raise RuntimeError("engine exe not found in the downloaded archive")
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(src_root):
            s = os.path.join(src_root, name)
            d = os.path.join(dest, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    progress_cb(1.0, "installed")
    return dest


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def engine_root():
    return os.path.join(app_dir(), "upscaler")


def discover_models():
    """Every installed model across the engines present, newest-engine first.

    Returns a list of option dicts: engine, exe, models_dir, model_name,
    native_scale, denoise (bool: engine supports a denoise level), label.
    """
    root = engine_root()
    opts = []

    # Real-ESRGAN (the bundled engine).
    rex = os.path.join(root, "realesrgan-ncnn-vulkan.exe")
    rex_models = os.path.join(root, "models")
    if os.path.isfile(rex) and os.path.isdir(rex_models):
        params = sorted({os.path.splitext(f)[0] for f in os.listdir(rex_models)
                         if f.lower().endswith(".param")})
        for name in params:
            label, ns = REALESRGAN_KNOWN.get(name, (name, 4))
            opts.append({"engine": "realesrgan", "exe": rex, "models_dir": rex_models,
                         "model_name": name, "native_scale": ns, "denoise": False,
                         "label": "Real-ESRGAN | " + label})

    # Real-CUGAN (optional): strong on drawn/game art, native 2/3/4, denoise.
    cugan = os.path.join(root, "realcugan", "realcugan-ncnn-vulkan.exe")
    if os.path.isfile(cugan):
        croot = os.path.join(root, "realcugan")
        for msub in ("models-se", "models-nose", "models-pro"):
            md = os.path.join(croot, msub)
            if os.path.isdir(md):
                opts.append({"engine": "realcugan", "exe": cugan, "models_dir": md,
                             "model_name": msub, "native_scale": None, "denoise": True,
                             "label": "Real-CUGAN | %s (art)" % msub.replace("models-", "")})

    # waifu2x (optional): clean line art and denoise.
    w2x = os.path.join(root, "waifu2x", "waifu2x-ncnn-vulkan.exe")
    if os.path.isfile(w2x):
        wroot = os.path.join(root, "waifu2x")
        for msub in ("models-cunet", "models-upconv_7_anime_style_art_rgb",
                     "models-upconv_7_photo"):
            md = os.path.join(wroot, msub)
            if os.path.isdir(md):
                opts.append({"engine": "waifu2x", "exe": w2x, "models_dir": md,
                             "model_name": msub, "native_scale": None, "denoise": True,
                             "label": "waifu2x | %s" % msub.replace("models-", "")})
    return opts


def human_time(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


class Worker(threading.Thread):
    def __init__(self, files, in_root, out_root, scale, opt, noise, tta,
                 keep_format, skip_existing, strength, q, cancel):
        super().__init__(daemon=True)
        self.files = files
        self.in_root = in_root
        self.out_root = out_root
        self.scale = scale
        self.opt = opt
        self.noise = noise
        self.tta = tta
        self.keep_format = keep_format
        self.skip_existing = skip_existing
        self.strength = strength  # 0..1: how much of the AI result to keep vs the original
        self.q = q
        self.cancel = cancel
        self.proc = None

    def log(self, m):
        self.q.put(("log", m))

    def run_scale(self):
        """Scale to ask the engine for; may exceed the target and be resized."""
        ns = self.opt["native_scale"]
        if ns is not None:
            return ns  # fixed-scale model
        if self.opt["engine"] == "waifu2x" and self.scale == 3:
            return 4   # waifu2x has no native 3x
        return self.scale

    def build_args(self, in_png, out_png, run_scale):
        o = self.opt
        eng = o["engine"]
        if eng == "realesrgan":
            a = [o["exe"], "-i", in_png, "-o", out_png, "-n", o["model_name"],
                 "-s", str(run_scale), "-m", o["models_dir"], "-f", "png"]
            if self.tta:
                a.append("-x")
            return a
        if eng == "realcugan":
            a = [o["exe"], "-i", in_png, "-o", out_png, "-s", str(run_scale),
                 "-n", str(self.noise), "-m", o["models_dir"], "-f", "png"]
            if self.tta:
                a.append("-x")
            return a
        if eng == "waifu2x":
            a = [o["exe"], "-i", in_png, "-o", out_png, "-s", str(run_scale),
                 "-n", str(self.noise), "-m", o["models_dir"], "-f", "png"]
            if self.tta:
                a.append("-x")
            return a
        return None

    def run_engine(self, in_png, out_png, run_scale):
        args = self.build_args(in_png, out_png, run_scale)
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        while self.proc.poll() is None:
            if self.cancel.is_set():
                self.proc.terminate()
                return False
            time.sleep(0.05)
        return self.proc.returncode == 0 and os.path.isfile(out_png)

    def run(self):
        if not self.opt:
            self.q.put(("done", {"ok": 0, "skip": 0, "fail": 0,
                                 "error": "no upscaler model is installed"}))
            return
        done = skipped = failed = 0
        total = len(self.files)
        t0 = time.time()
        tmp = os.path.join(self.out_root, ".acme_tmp")
        os.makedirs(tmp, exist_ok=True)
        for i, src in enumerate(self.files):
            if self.cancel.is_set():
                break
            rel = os.path.relpath(src, self.in_root)
            dst = os.path.join(self.out_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            self.q.put(("file", {"i": i, "total": total, "name": rel,
                                 "elapsed": time.time() - t0, "done": done}))
            if self.skip_existing and self._existing(dst):
                skipped += 1
                continue
            try:
                if self.process_one(src, dst, tmp):
                    done += 1
                else:
                    failed += 1
                    self.log("FAILED %s" % rel)
            except Exception as e:
                failed += 1
                self.log("FAILED %s : %s" % (rel, e))
        try:
            for f in os.listdir(tmp):
                os.remove(os.path.join(tmp, f))
            os.rmdir(tmp)
        except Exception:
            pass
        self.q.put(("done", {"ok": done, "skip": skipped, "fail": failed,
                             "elapsed": time.time() - t0,
                             "cancelled": self.cancel.is_set()}))

    def _existing(self, dst):
        base = os.path.splitext(dst)[0]
        return os.path.isfile(dst) or os.path.isfile(base + ".png")

    def process_one(self, src, dst, tmp):
        ext = os.path.splitext(src)[1].lower()
        native_in = ext in NATIVE_EXTS
        if native_in:
            engine_in = src
        else:
            if not HAVE_PIL:
                self.log("SKIP %s (needs Pillow)" % os.path.basename(src))
                return False
            engine_in = os.path.join(tmp, "in_%d.png" % time.time_ns())
            with Image.open(src) as im:
                im.convert("RGBA").save(engine_in, "PNG")

        rs = self.run_scale()
        engine_out = os.path.join(tmp, "out_%d.png" % time.time_ns())
        ok = self.run_engine(engine_in, engine_out, rs)
        if not native_in:
            self._rm(engine_in)
        if not ok:
            self._rm(engine_out)
            return False

        need_resize = rs != self.scale
        blend = HAVE_PIL and self.strength < 0.999
        keep_ext = ext if (self.keep_format and (native_in or HAVE_PIL)) else ".png"
        final_dst = os.path.splitext(dst)[0] + keep_ext

        # Fast path: full-strength, no resize, PNG in and out - just move the file.
        if not need_resize and keep_ext == ".png" and not blend:
            self._replace(engine_out, final_dst)
            return True

        if HAVE_PIL:
            with Image.open(engine_out) as raw:
                im = raw.convert("RGB")
                tw, th = im.width, im.height
                if need_resize:
                    with Image.open(src) as orig:
                        tw, th = orig.width * self.scale, orig.height * self.scale
                    im = im.resize((tw, th), Image.LANCZOS)
                if blend:
                    # Lay the AI detail over a plain enlargement of the original,
                    # at the chosen strength. 1.0 = pure AI, 0.0 = plain resize.
                    with Image.open(src) as orig:
                        base = orig.convert("RGB").resize((tw, th), Image.LANCZOS)
                    im = Image.blend(base, im, self.strength)
                try:
                    self._save(im, final_dst)
                except Exception:
                    final_dst = os.path.splitext(dst)[0] + ".png"
                    self._save(im, final_dst)
            self._rm(engine_out)
        else:
            self._replace(engine_out, final_dst)
        return True

    def _save(self, im, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            im.convert("RGB").save(path, quality=95)
        else:
            im.save(path)

    def _replace(self, src, dst):
        if os.path.abspath(src) == os.path.abspath(dst):
            return
        if os.path.isfile(dst):
            os.remove(dst)
        os.replace(src, dst)

    def _rm(self, p):
        try:
            if p and os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("%s  v%s" % (APP_NAME, VERSION))
        root.geometry("780x660")
        root.minsize(700, 600)

        self.models = discover_models()
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.scale_var = tk.IntVar(value=4)
        self.model_var = tk.StringVar(value=self._preferred_label())
        self.noise_var = tk.StringVar(value="0 - none")
        self.strength_var = tk.IntVar(value=100)
        self.recursive_var = tk.BooleanVar(value=True)
        self.skip_var = tk.BooleanVar(value=True)
        self.keep_var = tk.BooleanVar(value=True)
        self.tta_var = tk.BooleanVar(value=False)

        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.files = []
        self._build()
        self._poll()
        if not self.models:
            self._log("No upscaler engine found. Keep the 'upscaler' folder beside "
                      "this program (it holds realesrgan-ncnn-vulkan and models).")
        else:
            self._log("%d model(s) available." % len(self.models))
        if not HAVE_PIL:
            self._log("NOTE: Pillow missing - only PNG/JPG/WebP handled. Use the "
                      "portable exe, or 'pip install pillow'.")

    def _preferred_label(self):
        labels = [m["label"] for m in self.models]
        for pref in PREFERRED_ORDER:
            if pref in labels:
                return pref
        return labels[0] if labels else ""

    def _model_opt(self):
        for m in self.models:
            if m["label"] == self.model_var.get():
                return m
        return self.models[0] if self.models else None

    def _build(self):
        pad = {"padx": 10, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Folder of textures", width=16).grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.in_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text="Browse...", command=self._pick_in).grid(row=0, column=2, padx=4)
        ttk.Label(top, text="Save upscaled to", width=16).grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.out_var).grid(row=1, column=1, sticky="ew")
        ttk.Button(top, text="Browse...", command=self._pick_out).grid(row=1, column=2, padx=4)
        top.columnconfigure(1, weight=1)

        opt = ttk.LabelFrame(self.root, text="How")
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="Model").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.model_cb = ttk.Combobox(opt, textvariable=self.model_var,
                                     values=[m["label"] for m in self.models],
                                     state="readonly", width=46)
        self.model_cb.grid(row=0, column=1, sticky="w")
        self.model_cb.bind("<<ComboboxSelected>>", lambda e: self._sync_controls())
        ttk.Button(opt, text="Get more models...", command=self._downloads_dialog).grid(
            row=0, column=2, sticky="w", padx=6)

        ttk.Label(opt, text="Scale").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        sc = ttk.Frame(opt)
        sc.grid(row=1, column=1, sticky="w")
        for s in (2, 3, 4):
            ttk.Radiobutton(sc, text="%dx" % s, value=s, variable=self.scale_var).pack(side="left", padx=6)

        ttk.Label(opt, text="Denoise").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.noise_cb = ttk.Combobox(opt, textvariable=self.noise_var, state="readonly", width=16,
                                     values=["-1 - keep grain", "0 - none", "1 - light",
                                             "2 - medium", "3 - strong"])
        self.noise_cb.grid(row=2, column=1, sticky="w")
        self.noise_hint = ttk.Label(opt, text="(used by Real-CUGAN and waifu2x)")
        self.noise_hint.grid(row=2, column=1, sticky="w", padx=180)

        ttk.Label(opt, text="Detail strength").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        srow = ttk.Frame(opt)
        srow.grid(row=3, column=1, sticky="w")
        self.strength_scale = ttk.Scale(srow, from_=0, to=100, orient="horizontal",
                                       length=240, command=self._on_strength)
        self.strength_scale.set(100)
        self.strength_scale.pack(side="left")
        self.strength_lbl = ttk.Label(srow, text="100%  (full AI)")
        self.strength_lbl.pack(side="left", padx=8)

        checks = ttk.Frame(opt)
        checks.grid(row=4, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(checks, text="Include subfolders", variable=self.recursive_var).pack(side="left", padx=6)
        ttk.Checkbutton(checks, text="Skip already-done", variable=self.skip_var).pack(side="left", padx=6)
        ttk.Checkbutton(checks, text="Keep original format", variable=self.keep_var).pack(side="left", padx=6)
        ttk.Checkbutton(checks, text="Extra quality (slow)", variable=self.tta_var).pack(side="left", padx=6)

        act = ttk.Frame(self.root)
        act.pack(fill="x", **pad)
        self.scan_btn = ttk.Button(act, text="Scan folder", command=self._scan)
        self.scan_btn.pack(side="left")
        self.start_btn = ttk.Button(act, text="Start", command=self._start, state="disabled")
        self.start_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.open_btn = ttk.Button(act, text="Open output", command=self._open_out, state="disabled")
        self.open_btn.pack(side="left", padx=6)

        prog = ttk.Frame(self.root)
        prog.pack(fill="x", **pad)
        self.bar = ttk.Progressbar(prog, mode="determinate")
        self.bar.pack(fill="x")
        self.status = ttk.Label(prog, text="Pick a folder and scan.")
        self.status.pack(anchor="w", pady=2)
        self.timing = ttk.Label(prog, text="")
        self.timing.pack(anchor="w")

        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(logf, height=10, wrap="word")
        self.logbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        sb.pack(side="right", fill="y")
        self.logbox.config(yscrollcommand=sb.set, state="disabled")
        self._sync_controls()

    def _on_strength(self, _v):
        v = int(round(float(self.strength_scale.get())))
        self.strength_var.set(v)
        if v >= 100:
            tail = "  (full AI)"
        elif v == 0:
            tail = "  (plain resize, no AI)"
        else:
            tail = "  (AI over the original)"
        self.strength_lbl.config(text="%d%%%s" % (v, tail))

    def _sync_controls(self):
        o = self._model_opt()
        supports_denoise = bool(o and o.get("denoise"))
        state = "readonly" if supports_denoise else "disabled"
        self.noise_cb.config(state=state)

    def _refresh_models(self, select=None):
        self.models = discover_models()
        labels = [m["label"] for m in self.models]
        self.model_cb.config(values=labels)
        if select and select in labels:
            self.model_var.set(select)
        elif self.model_var.get() not in labels:
            self.model_var.set(self._preferred_label())
        self._sync_controls()

    def _downloads_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Get more models")
        dlg.transient(self.root)
        dlg.geometry("560x300")
        ttk.Label(dlg, wraplength=530, text=(
            "Real-ESRGAN is already installed. These are other portable engines. "
            "Real-CUGAN is often the most precise on drawn and game art; waifu2x "
            "is good for clean line art. They download from the engines' official "
            "release pages and install beside Real-ESRGAN.")).pack(anchor="w", padx=12, pady=8)
        installed = {os.path.basename(os.path.dirname(m["exe"])) for m in self.models}
        bar = ttk.Progressbar(dlg, mode="determinate")
        status = ttk.Label(dlg, text="")
        for name, entry in DOWNLOADS.items():
            row = ttk.Frame(dlg)
            row.pack(fill="x", padx=12, pady=4)
            here = entry["dest"] in installed
            ttk.Label(row, text=name + ("   [installed]" if here else "  (~%d MB)" % entry["mb"]),
                      wraplength=430).pack(side="left")
            b = ttk.Button(row, text="Re-download" if here else "Download")
            b.configure(command=lambda e=entry, btn=b: self._do_download(e, btn, bar, status, dlg))
            b.pack(side="right")
        bar.pack(fill="x", padx=12, pady=(10, 2))
        status.pack(anchor="w", padx=12)
        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=8)

    def _do_download(self, entry, btn, bar, status, dlg):
        btn.config(state="disabled")
        status.config(text="starting...")
        q = queue.Queue()

        def work():
            try:
                install_download(entry, lambda f, m: q.put((f, m)))
                q.put(("ok", None))
            except Exception as e:
                q.put(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()

        def pump():
            try:
                while True:
                    f, m = q.get_nowait()
                    if f == "ok":
                        status.config(text="Installed. New models are in the list.")
                        bar["value"] = bar["maximum"]
                        self._refresh_models(select=None)
                        self._log("Installed %s" % entry["dest"])
                        return
                    if f == "err":
                        status.config(text="Failed: " + m)
                        self._log("Download failed (%s): %s" % (entry["dest"], m))
                        btn.config(state="normal")
                        return
                    if f is None:
                        bar.config(mode="indeterminate"); bar.start(12)
                        status.config(text=m)
                    else:
                        bar.stop(); bar.config(mode="determinate", maximum=100)
                        bar["value"] = f * 100
                        status.config(text=m)
            except queue.Empty:
                pass
            dlg.after(80, pump)

        pump()

    def _pick_in(self):
        d = filedialog.askdirectory(title="Folder of textures to upscale")
        if d:
            self.in_var.set(d)
            if not self.out_var.get():
                self.out_var.set(d.rstrip("/\\") + "_upscaled")

    def _pick_out(self):
        d = filedialog.askdirectory(title="Where to save the upscaled textures")
        if d:
            self.out_var.set(d)

    def _log(self, m):
        self.logbox.config(state="normal")
        self.logbox.insert("end", m + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def _scan(self):
        root = self.in_var.get().strip()
        if not root or not os.path.isdir(root):
            self._log("Pick a folder that exists first.")
            return
        exts = ALL_EXTS if HAVE_PIL else NATIVE_EXTS
        found = []
        if self.recursive_var.get():
            for dp, _, fns in os.walk(root):
                if os.path.basename(dp) == ".acme_tmp":
                    continue
                for fn in fns:
                    if os.path.splitext(fn)[1].lower() in exts:
                        found.append(os.path.join(dp, fn))
        else:
            for fn in os.listdir(root):
                p = os.path.join(root, fn)
                if os.path.isfile(p) and os.path.splitext(fn)[1].lower() in exts:
                    found.append(p)
        self.files = sorted(found)
        mb = sum(os.path.getsize(f) for f in self.files) / (1024 * 1024)
        self.bar["maximum"] = max(1, len(self.files))
        self.bar["value"] = 0
        self.status.config(text="%d images found (%.0f MB). Ready." % (len(self.files), mb))
        self._log("Scan: %d images under %s" % (len(self.files), root))
        self.start_btn.config(state=("normal" if self.files and self.models else "disabled"))

    def _noise_level(self):
        try:
            return int(self.noise_var.get().split(" ")[0])
        except Exception:
            return 0

    def _start(self):
        o = self._model_opt()
        if not self.files or not o:
            return
        out = self.out_var.get().strip()
        if not out:
            self._log("Choose an output folder.")
            return
        os.makedirs(out, exist_ok=True)
        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self._log("Start: %s, %dx, %d files -> %s"
                  % (o["label"], self.scale_var.get(), len(self.files), out))
        self.worker = Worker(self.files, self.in_var.get().strip(), out,
                             self.scale_var.get(), o, self._noise_level(),
                             self.tta_var.get(), self.keep_var.get(),
                             self.skip_var.get(), self.strength_var.get() / 100.0,
                             self.q, self.cancel)
        self.worker.start()

    def _cancel(self):
        self.cancel.set()
        self.status.config(text="Cancelling after the current file...")

    def _open_out(self):
        out = self.out_var.get().strip()
        if out and os.path.isdir(out):
            try:
                os.startfile(out)
            except Exception:
                pass

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "file":
                    self._on_file(payload)
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _on_file(self, p):
        self.bar["value"] = p["i"]
        self.status.config(text="[%d/%d] %s" % (p["i"] + 1, p["total"], p["name"]))
        done, el = p["done"], p["elapsed"]
        if done >= 2 and el > 0:
            per = el / done
            left = (p["total"] - p["i"]) * per
            self.timing.config(text="elapsed %s   about %s left   (%.1f s/image)"
                               % (human_time(el), human_time(left), per))
        else:
            self.timing.config(text="elapsed %s   estimating..." % human_time(el))

    def _on_done(self, p):
        self.cancel_btn.config(state="disabled")
        self.scan_btn.config(state="normal")
        self.start_btn.config(state="normal")
        self.open_btn.config(state="normal")
        self.bar["value"] = self.bar["maximum"]
        if p.get("error"):
            self.status.config(text="Error: " + p["error"])
            self._log("ERROR: " + p["error"])
            return
        verb = "Cancelled" if p.get("cancelled") else "Done"
        txt = ("%s: %d upscaled, %d skipped, %d failed in %s"
               % (verb, p["ok"], p["skip"], p["fail"], human_time(p.get("elapsed", 0))))
        self.status.config(text=txt)
        self._log(txt)


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
