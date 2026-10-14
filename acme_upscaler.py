"""ACME Texture Upscaler 2000 - a portable AI texture upscaler.

Point it at a folder, choose a model and a scale (2x / 3x / 4x / 8x), press
Start. Every image is sorted first (acme_classify.py): texture art goes to the
AI; lettering gets a sharp resize; soft/blurry images, lighting and data
textures and tiny images keep the original; normal maps are upscaled and
renormalised; heavily JPEG-blocked images go to a JPEG-trained model. The
output folder keeps the sub-folder tree, and a report (CSV + summary +
optional contact sheets per class) says what happened to every file and why.

Portable: the program plus an `upscaler/` folder (the engine and its models)
sit side by side. Nothing is installed.

Models. Ships with Real-ESRGAN ncnn-vulkan and the NMKD Superscale texture
model (the default), and discovers whatever models are present. Two other
portable engines are recognised if dropped in (or fetched with "Get more
models..."):
  upscaler/realcugan/realcugan-ncnn-vulkan.exe   (Real-CUGAN - strong on art)
  upscaler/waifu2x/waifu2x-ncnn-vulkan.exe       (waifu2x - clean line art)
Any other ncnn model (.param + .bin) put in upscaler/models/ is listed too.

Formats. PNG/JPG/WebP/BMP/TGA/TIFF/DDS are decoded with Pillow and written
back in their own format where possible, otherwise as PNG.
"""

import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, ttk

import acme_classify as C
import acme_engine as E

APP_NAME = "ACME Texture Upscaler 2000"
VERSION = "1.1.0"

ALL_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga", ".tif", ".tiff", ".dds"}
REPORT_DIR = "_acme_report"

# Friendly labels and native scale for the models we know about. Anything
# else in upscaler/models is listed under its own name, its scale read from
# the name ("x2"/"2x" -> 2, otherwise 4).
REALESRGAN_KNOWN = {
    "4x_NMKD-Superscale-SP_178000_G": ("Texture 4x - NMKD Superscale (recommended)", 4),
    "realesr-animevideov3-x2": ("Fast 2x - anime video v3", 2),
    "realesr-animevideov3-x3": ("Fast 3x - anime video v3", 3),
    "realesr-animevideov3-x4": ("Fast 4x - anime video v3", 4),
    "realesrgan-x4plus": ("Photo 4x - x4plus (photos, JPEG damage)", 4),
    "realesrgan-x4plus-anime": ("Art 4x - x4plus-anime (cel / drawn)", 4),
    "realesrnet-x4plus": ("Soft 4x - net (fewer artifacts)", 4),
}
# Photo-trained models change brightness and smear texture detail; for those
# the default is detail transfer at 75% (the Fable II / NG2 packs' setting).
PHOTO_MODELS = {"realesrgan-x4plus", "realesrnet-x4plus"}

PREFERRED_ORDER = [
    "Real-ESRGAN | Texture 4x - NMKD Superscale (recommended)",
    "Real-ESRGAN | Photo 4x - x4plus (photos, JPEG damage)",
    "Real-CUGAN | se (art)",
    "Real-ESRGAN | Art 4x - x4plus-anime (cel / drawn)",
    "Real-ESRGAN | Fast 4x - anime video v3",
]
LOWQ_MODEL = "realesrgan-x4plus"   # trained on JPEG-damaged inputs

# Extra engines the app can fetch and install into upscaler/<dest>/ on request.
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
    """Fetch a ncnn-vulkan engine zip and install it under upscaler/<dest>/."""
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


def _scale_from_name(name):
    m = re.search(r"(?:^|[^0-9])([1-8])x|x([1-8])(?:[^0-9]|$)", name.lower())
    if m:
        v = int(m.group(1) or m.group(2))
        if v in (1, 2, 3, 4, 8):
            return v
    return 4


def discover_models():
    """Every installed model across the engines present."""
    root = engine_root()
    opts = []
    rex = os.path.join(root, "realesrgan-ncnn-vulkan.exe")
    rex_models = os.path.join(root, "models")
    if os.path.isfile(rex) and os.path.isdir(rex_models):
        params = sorted({os.path.splitext(f)[0] for f in os.listdir(rex_models)
                         if f.lower().endswith(".param")
                         and os.path.isfile(os.path.join(rex_models, os.path.splitext(f)[0] + ".bin"))})
        for name in params:
            label, ns = REALESRGAN_KNOWN.get(name, (name, _scale_from_name(name)))
            opts.append({"engine": "realesrgan", "exe": rex, "models_dir": rex_models,
                         "model_name": name, "native_scale": ns, "denoise": False,
                         "label": "Real-ESRGAN | " + label})
    cugan = os.path.join(root, "realcugan", "realcugan-ncnn-vulkan.exe")
    if os.path.isfile(cugan):
        croot = os.path.join(root, "realcugan")
        for msub in ("models-se", "models-nose", "models-pro"):
            md = os.path.join(croot, msub)
            if os.path.isdir(md):
                opts.append({"engine": "realcugan", "exe": cugan, "models_dir": md,
                             "model_name": msub, "native_scale": None, "denoise": True,
                             "label": "Real-CUGAN | %s (art)" % msub.replace("models-", "")})
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


def list_images(root, recursive, exclude=()):
    """Image files under root, skipping our own report/temp folders and any
    excluded folder (the output folder when it sits inside the input)."""
    ex = [os.path.normcase(os.path.abspath(e)) for e in exclude if e]
    found = []
    if recursive:
        for dp, dns, fns in os.walk(root):
            keep = []
            for d in dns:
                full = os.path.normcase(os.path.abspath(os.path.join(dp, d)))
                if d == REPORT_DIR or d.startswith("acme_") or d == ".acme_tmp" or full in ex:
                    continue
                keep.append(d)
            dns[:] = keep
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in ALL_EXTS:
                    found.append(os.path.join(dp, fn))
    else:
        for fn in os.listdir(root):
            p = os.path.join(root, fn)
            if os.path.isfile(p) and os.path.splitext(fn)[1].lower() in ALL_EXTS:
                found.append(p)
    return sorted(found)


class App:
    def __init__(self, root):
        self.root = root
        root.title("%s  v%s" % (APP_NAME, VERSION))
        root.geometry("820x760")
        root.minsize(740, 640)

        self.models = discover_models()
        self.th = C.Thresholds()
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.scale_var = tk.IntVar(value=4)
        self.model_var = tk.StringVar(value=self._preferred_label())
        self.noise_var = tk.StringVar(value="0 - none")
        self.strength_var = tk.IntVar(value=100)
        self.recursive_var = tk.BooleanVar(value=True)
        self.skip_var = tk.BooleanVar(value=True)
        self.keep_var = tk.BooleanVar(value=True)
        self.sort_var = tk.BooleanVar(value=True)
        self.tta_var = tk.BooleanVar(value=False)
        self.sheets_var = tk.BooleanVar(value=True)
        self.ocr_var = tk.BooleanVar(value=True)
        self.names_var = tk.BooleanVar(value=True)
        self.kept_var = tk.StringVar(value="Copy the original")
        self.normal_var = tk.StringVar(value="AI + renormalise")
        self.lowq_var = tk.StringVar(value="JPEG-trained model (x4plus)")
        self.th_vars = {}

        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.items = []
        self.scanned_key = None
        self.busy = False
        self._build()
        self._poll()
        if not self.models:
            self._log("No upscaler engine found. Keep the 'upscaler' folder beside "
                      "this program (it holds realesrgan-ncnn-vulkan and models).")
        else:
            self._log("%d model(s) available." % len(self.models))

    # --- models ------------------------------------------------------------------
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

    def _lowq_opt(self):
        if not self.lowq_var.get().startswith("JPEG"):
            return None
        for m in self.models:
            if m["engine"] == "realesrgan" and m["model_name"] == LOWQ_MODEL:
                return m
        return None

    # --- layout ------------------------------------------------------------------
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
                                     state="readonly", width=58)
        self.model_cb.grid(row=0, column=1, sticky="w")
        self.model_cb.bind("<<ComboboxSelected>>", lambda e: self._on_model())
        ttk.Button(opt, text="Get more models...", command=self._downloads_dialog).grid(
            row=0, column=2, sticky="w", padx=6)

        ttk.Label(opt, text="Scale").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        sc = ttk.Frame(opt)
        sc.grid(row=1, column=1, sticky="w")
        for s in (2, 3, 4, 8):
            ttk.Radiobutton(sc, text="%dx" % s, value=s, variable=self.scale_var).pack(side="left", padx=6)
        ttk.Label(sc, text="(8x = two 4x passes; nothing over 4096 px)").pack(side="left", padx=6)

        ttk.Label(opt, text="Detail strength").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        srow = ttk.Frame(opt)
        srow.grid(row=2, column=1, columnspan=2, sticky="w")
        self.strength_scale = ttk.Scale(srow, from_=0, to=100, orient="horizontal",
                                        length=240, command=self._on_strength)
        self.strength_scale.pack(side="left")
        self.strength_lbl = ttk.Label(srow, text="")
        self.strength_lbl.pack(side="left", padx=8)

        checks = ttk.Frame(opt)
        checks.grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(checks, text="Sort images first (recommended)",
                        variable=self.sort_var).pack(side="left", padx=6)
        ttk.Checkbutton(checks, text="Include subfolders", variable=self.recursive_var).pack(side="left", padx=6)
        ttk.Checkbutton(checks, text="Skip already-done", variable=self.skip_var).pack(side="left", padx=6)
        ttk.Checkbutton(checks, text="Keep original format", variable=self.keep_var).pack(side="left", padx=6)

        self.adv_btn = ttk.Button(self.root, text="Advanced  >", command=self._toggle_adv)
        self.adv_btn.pack(anchor="w", padx=10)
        self.adv = ttk.LabelFrame(self.root, text="Advanced")
        self._build_adv(self.adv)

        act = ttk.Frame(self.root)
        act.pack(fill="x", **pad)
        self.act_frame = act
        self.scan_btn = ttk.Button(act, text="Scan and sort", command=self._scan)
        self.scan_btn.pack(side="left")
        self.start_btn = ttk.Button(act, text="Start", command=self._start, state="disabled")
        self.start_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.open_btn = ttk.Button(act, text="Open output", command=self._open_out, state="disabled")
        self.open_btn.pack(side="left", padx=6)
        self.report_btn = ttk.Button(act, text="Open report", command=self._open_report, state="disabled")
        self.report_btn.pack(side="left")

        prog = ttk.Frame(self.root)
        prog.pack(fill="x", **pad)
        self.bar = ttk.Progressbar(prog, mode="determinate")
        self.bar.pack(fill="x")
        self.status = ttk.Label(prog, text="Pick a folder and press Scan and sort.")
        self.status.pack(anchor="w", pady=2)
        self.timing = ttk.Label(prog, text="")
        self.timing.pack(anchor="w")
        self.summary = ttk.Label(prog, text="", foreground="#1a5fb4", wraplength=780, justify="left")
        self.summary.pack(anchor="w", pady=2)

        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(logf, height=10, wrap="word")
        self.logbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        sb.pack(side="right", fill="y")
        self.logbox.config(yscrollcommand=sb.set, state="disabled")
        self._on_model()

    def _build_adv(self, f):
        r = 0
        ttk.Label(f, text="Kept images (soft, lighting, tiny, unsure lettering)").grid(
            row=r, column=0, sticky="w", padx=8, pady=3)
        ttk.Combobox(f, textvariable=self.kept_var, state="readonly", width=24,
                     values=["Copy the original", "Plain resize", "Leave out"]).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text="Normal maps").grid(row=r, column=0, sticky="w", padx=8, pady=3)
        ttk.Combobox(f, textvariable=self.normal_var, state="readonly", width=24,
                     values=["AI + renormalise", "Plain resize + renormalise"]).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text="Low-quality (JPEG-blocked) images").grid(row=r, column=0, sticky="w", padx=8, pady=3)
        ttk.Combobox(f, textvariable=self.lowq_var, state="readonly", width=24,
                     values=["JPEG-trained model (x4plus)", "The chosen model"]).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text="Denoise (Real-CUGAN / waifu2x)").grid(row=r, column=0, sticky="w", padx=8, pady=3)
        self.noise_cb = ttk.Combobox(f, textvariable=self.noise_var, state="readonly", width=24,
                                     values=["-1 - keep grain", "0 - none", "1 - light",
                                             "2 - medium", "3 - strong"])
        self.noise_cb.grid(row=r, column=1, sticky="w")
        r += 1
        cf = ttk.Frame(f)
        cf.grid(row=r, column=0, columnspan=4, sticky="w", padx=4, pady=3)
        ttk.Checkbutton(cf, text="Find lettering with Windows OCR", variable=self.ocr_var).pack(side="left", padx=4)
        ttk.Checkbutton(cf, text="Use file-name hints", variable=self.names_var).pack(side="left", padx=4)
        ttk.Checkbutton(cf, text="Contact sheets per class", variable=self.sheets_var).pack(side="left", padx=4)
        ttk.Checkbutton(cf, text="Extra quality (slow)", variable=self.tta_var).pack(side="left", padx=4)
        r += 1
        tf = ttk.Frame(f)
        tf.grid(row=r, column=0, columnspan=4, sticky="w", padx=4, pady=3)
        fields = [("tiny_side", "Tiny under (px)"), ("strip_aspect", "Strip aspect"),
                  ("soft_pooled", "Soft detail <"), ("glow_pooled", "Glow detail <"),
                  ("jpeg_block", "JPEG block >"), ("max_side", "Max side (px)")]
        for i, (k, lab) in enumerate(fields):
            ttk.Label(tf, text=lab).grid(row=i // 3, column=(i % 3) * 2, sticky="w", padx=4, pady=2)
            v = tk.StringVar(value=str(getattr(self.th, k)))
            self.th_vars[k] = v
            ttk.Entry(tf, textvariable=v, width=8).grid(row=i // 3, column=(i % 3) * 2 + 1, sticky="w", padx=4)
        r += 1
        ttk.Label(f, foreground="#555", wraplength=760, text=(
            "Your own lists in the input folder win over the sorting: acme_keep.txt (keep original), "
            "acme_sharp.txt (sharp resize), acme_ai.txt (always AI) - one file name, relative path "
            "or pattern (*.tga) per line.")).grid(row=r, column=0, columnspan=4, sticky="w", padx=8, pady=4)

    def _toggle_adv(self):
        if self.adv.winfo_ismapped():
            self.adv.pack_forget()
            self.adv_btn.config(text="Advanced  >")
        else:
            self.adv.pack(fill="x", padx=10, pady=4, before=self.act_frame)
            self.adv_btn.config(text="Advanced  v")

    def _on_model(self):
        o = self._model_opt()
        self.noise_cb.config(state="readonly" if (o and o.get("denoise")) else "disabled")
        photo = bool(o and o.get("model_name") in PHOTO_MODELS)
        self.strength_scale.set(75 if photo else 100)
        self._on_strength(None)

    def _on_strength(self, _v):
        v = int(round(float(self.strength_scale.get())))
        self.strength_var.set(v)
        if v >= 100:
            tail = "  (full AI result)"
        elif v == 0:
            tail = "  (plain resize, no AI)"
        else:
            tail = "  (AI detail over a plain resize - keeps the original's tones)"
        self.strength_lbl.config(text="%d%%%s" % (v, tail))

    def _refresh_models(self, select=None):
        self.models = discover_models()
        labels = [m["label"] for m in self.models]
        self.model_cb.config(values=labels)
        if select and select in labels:
            self.model_var.set(select)
        elif self.model_var.get() not in labels:
            self.model_var.set(self._preferred_label())
        self._on_model()

    def _downloads_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Get more models")
        dlg.transient(self.root)
        dlg.geometry("560x300")
        ttk.Label(dlg, wraplength=530, text=(
            "Real-ESRGAN and NMKD Superscale are already installed. These are other portable "
            "engines. Real-CUGAN is often the most precise on drawn and game art; waifu2x is "
            "good for clean line art. They download from the engines' official release pages "
            "and install beside Real-ESRGAN.")).pack(anchor="w", padx=12, pady=8)
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
            except Exception as e:     # noqa: BLE001
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
                        bar.config(mode="indeterminate")
                        bar.start(12)
                        status.config(text=m)
                    else:
                        bar.stop()
                        bar.config(mode="determinate", maximum=100)
                        bar["value"] = f * 100
                        status.config(text=m)
            except queue.Empty:
                pass
            dlg.after(80, pump)

        pump()

    # --- actions -------------------------------------------------------------------
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

    def _read_thresholds(self):
        th = C.Thresholds()
        for k, v in self.th_vars.items():
            try:
                cur = getattr(th, k)
                setattr(th, k, type(cur)(float(v.get())))
            except Exception:     # noqa: BLE001
                self._log("Advanced: '%s' is not a number - using %s" % (v.get(), getattr(th, k)))
        th.use_ocr = self.ocr_var.get()
        th.use_names = self.names_var.get()
        return th

    def _report_dir(self):
        out = self.out_var.get().strip()
        return os.path.join(out, REPORT_DIR) if out else None

    def _set_busy(self, busy):
        self.busy = busy
        st = "disabled" if busy else "normal"
        self.scan_btn.config(state=st)
        self.start_btn.config(state="disabled" if busy or not self.items or not self.models else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    def _scan(self, then_start=False):
        root = self.in_var.get().strip()
        if not root or not os.path.isdir(root):
            self._log("Pick a folder that exists first.")
            return
        out = self.out_var.get().strip()
        if not out:
            self._log("Choose an output folder.")
            return
        self.cancel.clear()
        self._set_busy(True)
        self.summary.config(text="")
        th = self._read_thresholds()
        sort = self.sort_var.get()
        recursive = self.recursive_var.get()
        sheets = self.sheets_var.get()
        rdir = self._report_dir()

        def work():
            try:
                files = list_images(root, recursive, exclude=[out])
                items = []
                for p in files:
                    try:
                        items.append(C.Item(p, os.path.relpath(p, root)))
                    except OSError:
                        pass
                self.q.put(("log", "Scan: %d images under %s" % (len(items), root)))
                if sort:
                    work_dir = tempfile.mkdtemp(prefix="acme_sort_")
                    try:
                        C.classify_items(items, root, th, work_dir,
                                         log=lambda m: self.q.put(("log", m)),
                                         progress=lambda st, i, n: self.q.put(("scanprog", (st, i, n))),
                                         cancel=self.cancel,
                                         cache_path=os.path.join(rdir, C.CACHE_NAME))
                    finally:
                        shutil.rmtree(work_dir, ignore_errors=True)
                else:
                    for i, it in enumerate(items):
                        try:
                            img, _ = C.load_rgba(it.path)
                            it.m = {"w": img.width, "h": img.height, "pooled": 1.0}
                            it.cls, it.reason = "ai", "sorting off"
                        except Exception as e:     # noqa: BLE001
                            it.cls, it.reason = "bad", "cannot decode: %s" % e
                        if i % 16 == 0:
                            self.q.put(("scanprog", ("reading", i, len(items))))
                if not self.cancel.is_set():
                    lines = C.write_report(rdir, items)
                    if sheets and sort:
                        self.q.put(("scanprog", ("contact sheets", 0, 1)))
                        n = len(C.contact_sheets(rdir, items, cancel=self.cancel))
                        self.q.put(("log", "%d contact sheet(s) in %s" % (n, os.path.join(rdir, "sheets"))))
                    self.q.put(("scan_done", (items, lines, then_start)))
                else:
                    self.q.put(("scan_done", (None, None, False)))
            except Exception as e:     # noqa: BLE001
                self.q.put(("log", "Scan failed: %s" % e))
                self.q.put(("scan_done", (None, None, False)))

        threading.Thread(target=work, daemon=True).start()

    def _settings(self):
        s = E.Settings()
        s.scale = self.scale_var.get()
        s.strength = self.strength_var.get() / 100.0
        s.keep_format = self.keep_var.get()
        s.skip_existing = self.skip_var.get()
        s.kept_mode = {"Copy the original": "copy", "Plain resize": "plain",
                       "Leave out": "omit"}[self.kept_var.get()]
        s.normal_mode = "ai" if self.normal_var.get().startswith("AI") else "plain"
        try:
            s.noise = int(self.noise_var.get().split(" ")[0])
        except Exception:     # noqa: BLE001
            s.noise = 0
        s.tta = self.tta_var.get()
        s.max_side = self._read_thresholds().max_side
        return s

    def _start(self):
        o = self._model_opt()
        if not self.items or not o:
            return
        out = self.out_var.get().strip()
        if not out:
            self._log("Choose an output folder.")
            return
        os.makedirs(out, exist_ok=True)
        self.cancel.clear()
        self._set_busy(True)
        self.open_btn.config(state="disabled")
        s = self._settings()
        if s.strength <= 0.001:
            self._log("Detail strength 0%: plain resize, the AI is not used.")
        lowq = self._lowq_opt()
        self._log("Start: %s, %dx, %d files -> %s" % (o["label"], s.scale, len(self.items), out))
        self.bar["maximum"] = max(1, len(self.items))
        self.bar["value"] = 0
        items = self.items
        in_root = self.in_var.get().strip()
        rdir = self._report_dir()

        def work():
            t0 = time.time()
            try:
                runner = E.Runner(items, in_root, out, s, o, lowq, self.q, self.cancel)
                counts, results = runner.run()
                C.write_report(rdir, items, results)
                self.q.put(("done", {"counts": counts, "elapsed": time.time() - t0,
                                     "cancelled": self.cancel.is_set()}))
            except Exception as e:     # noqa: BLE001
                self.q.put(("done", {"error": str(e)}))

        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        self.cancel.set()
        self.status.config(text="Cancelling...")

    def _open_out(self):
        out = self.out_var.get().strip()
        if out and os.path.isdir(out):
            try:
                os.startfile(out)
            except Exception:     # noqa: BLE001
                pass

    def _open_report(self):
        d = self._report_dir()
        if d and os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception:     # noqa: BLE001
                pass

    # --- messages ----------------------------------------------------------------
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "file":
                    self._on_file(payload)
                elif kind == "stage":
                    self.timing_stage = payload
                elif kind == "scanprog":
                    st, i, n = payload
                    self.bar["maximum"] = max(1, n)
                    self.bar["value"] = i
                    self.status.config(text="Sorting: %s %d / %d" % (st, i, n))
                elif kind == "scan_done":
                    self._on_scan_done(*payload)
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _on_scan_done(self, items, lines, then_start):
        self._set_busy(False)
        if items is None:
            self.status.config(text="Scan cancelled.")
            return
        self.items = items
        c = C.counts(items)
        parts = ["%s %d (%s)" % (C.CLASSES[k][0], c[k], C.CLASSES[k][1]) for k in C.CLASS_ORDER if c.get(k)]
        self.summary.config(text="Sorted: " + ",  ".join(parts))
        for ln in lines or []:
            if ln.strip():
                self._log("  " + ln)
        mb = sum(it.size for it in items) / (1024 * 1024)
        self.status.config(text="%d images (%.0f MB) sorted. Ready - press Start." % (len(items), mb))
        self.report_btn.config(state="normal")
        self.start_btn.config(state="normal" if items and self.models else "disabled")
        if then_start:
            self._start()

    def _on_file(self, p):
        self.bar["value"] = p["i"]
        self.status.config(text="[%d/%d] %s" % (p["i"], p["total"], p["name"]))
        done, el = p["done"], p["elapsed"]
        stage = getattr(self, "timing_stage", "")
        if done >= 4 and el > 0:
            per = el / done
            left = (p["total"] - done) * per
            self.timing.config(text="elapsed %s   about %s left   %s"
                               % (human_time(el), human_time(left), stage))
        else:
            self.timing.config(text="elapsed %s   estimating...   %s" % (human_time(el), stage))

    def _on_done(self, p):
        self._set_busy(False)
        self.open_btn.config(state="normal")
        self.report_btn.config(state="normal")
        self.bar["value"] = self.bar["maximum"]
        if p.get("error"):
            self.status.config(text="Error: " + p["error"])
            self._log("ERROR: " + p["error"])
            return
        c = p["counts"]
        verb = "Cancelled" if p.get("cancelled") else "Done"
        txt = ("%s in %s: %d AI, %d sharp (lettering), %d kept original, %d plain resize, "
               "%d AI fell back to resize, %d already done, %d skipped, %d failed"
               % (verb, human_time(p.get("elapsed", 0)), c["ai"], c["sharp"], c["kept"], c["plain"],
                  c["fallback"], c["existing"], c["skipped"] + c["omitted"], c["failed"]))
        self.status.config(text=txt.split(":")[0] + ".")
        self._log(txt)
        self._log("Report: %s" % os.path.join(self._report_dir(), "report.csv"))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        import acme_cli
        sys.exit(acme_cli.main(sys.argv[2:]))
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:     # noqa: BLE001
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
