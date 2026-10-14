"""The upscale itself - ACME Texture Upscaler 2000.

What each sorted image gets (acme_classify.py decides the class):

  ai / lowq   the AI model, in batches of 64 per engine run (starting the
              engine costs ~2 s; one run over a folder does it once), with
              the CPU preparing the next batch and finishing the last while
              the GPU works. Every output is checked: right size, not blank
              (a lost Vulkan device writes blanks), and shrunk back it still
              looks like the source (good results score <= 25, garbage 75+).
              A failure is redone alone, then falls back to a plain resize.
  normal      the AI's added detail laid over a plain resize (the means stay
              the source's), then the vectors renormalised (Z rebuilt);
              never colour-processed
  text        sharp resize (bicubic + light unsharp mask), never AI
  kept        the original copied (or a plain resize, or left out)

Scale rules (TEXTURE_UPGRADE_GUIDE s2): 4x networks always run at 4x and are
shrunk with an area filter for 2x/3x (asked for 2x they draw shifted ghost
edges); 8x = two 4x passes then halved; nothing over the size cap (4096) -
an image that would pass it takes the next smaller scale.

Alpha is never given to the AI: colour and alpha are resized apart (Pillow's
RGBA resize premultiplies and writes black under alpha 0), and a cut-out
(alpha only 0/255) is re-thresholded at 50% so its edge stays a clean cut.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageFilter

import acme_classify as C

AI_BATCH = 64
AI_BATCH_8X = 8          # 16x intermediates are big
WATCHDOG_S = 300         # an engine run that writes nothing for 5 minutes is ended
MIN_AI_SIDE = 16         # inputs under 16 px derail the engine's batch mode
SCALES_DOWN = {8: 4, 4: 3, 3: 2, 2: 1}

NATIVE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


# --- resize helpers --------------------------------------------------------------

def is_cutout(alpha):
    """alpha: L image. Only 0 and 255, with some 0."""
    h = alpha.histogram()
    return h[0] > 0 and sum(h[1:255]) == 0


def resize_alpha(alpha, size, resample=Image.Resampling.LANCZOS):
    a2 = alpha.resize(size, resample)
    if is_cutout(alpha):
        a2 = a2.point(lambda v: 255 if v >= 128 else 0)
    return a2


def resize_straight(img, size, resample=Image.Resampling.LANCZOS):
    """RGBA resize with colour and alpha apart (no premultiplication)."""
    img = img.convert("RGBA")
    out = img.convert("RGB").resize(size, resample).convert("RGBA")
    out.putalpha(resize_alpha(img.getchannel("A"), size, resample))
    return out


def sharp_resize(img, factor, size):
    """Lettering: high-quality cubic + a light unsharp mask whose radius follows
    the enlargement (Hulk H07 s5). Nothing is invented: shrunk back it is the
    source."""
    img = img.convert("RGBA")
    rgb = img.convert("RGB").resize(size, Image.Resampling.BICUBIC)
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=max(0.5, 0.45 * factor), percent=60, threshold=0))
    out = rgb.convert("RGBA")
    out.putalpha(resize_alpha(img.getchannel("A"), size, Image.Resampling.BICUBIC))
    return out


def fit_scale(w, h, scale, max_side):
    """The scale an image is made at: the asked one, or the next smaller that
    keeps both sides within max_side. 1 = it stays as it is."""
    while scale > 1 and max(w, h) * scale > max_side:
        scale = SCALES_DOWN[scale]
    return scale


def detail_transfer(ai_rgb, src_rgba, strength, size):
    """The AI's high-pass laid over a plain resize of the source (Fable / NG2
    packs, strength 0.75 for photo models): tone and brightness stay the
    source's, the added definition is the model's. ai_rgb is already at size."""
    plain = src_rgba.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    base = np.asarray(plain, dtype=np.float32)
    radius = 1.6 * max(1.0, size[0] / float(src_rgba.width) / 4.0)
    a = np.asarray(ai_rgb, dtype=np.float32)
    hi = a - np.asarray(ai_rgb.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
    out = np.clip(base + hi * float(strength) + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGB")


def renormalise(rgb, src_rgba):
    """Normal map vectors back to unit length. Where the source's blue is a
    constant ~255 (the shader rebuilds Z from X and Y) it is set back to 255;
    otherwise Z is rebuilt as sqrt(1 - x^2 - y^2)."""
    a = np.asarray(rgb, dtype=np.float32) / 127.5 - 1.0
    src_b = np.asarray(src_rgba.getchannel("B"))
    if int(src_b.min()) >= 250:
        out = np.asarray(rgb, dtype=np.float32).copy()
        out[..., 2] = 255.0
    else:
        x, y = a[..., 0], a[..., 1]
        n = np.sqrt(x * x + y * y)
        over = n > 1.0
        x = np.where(over, x / np.maximum(n, 1e-6), x)
        y = np.where(over, y / np.maximum(n, 1e-6), y)
        z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
        out = (np.stack([x, y, z], axis=2) + 1.0) * 127.5
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGB")


# --- checks ----------------------------------------------------------------------

def looks_blank(img):
    ex = img.convert("RGB").getextrema()
    return all(lo == hi for lo, hi in ex)


def source_distance(src_rgba, out_rgb):
    """Mean |difference| (0-255, alpha-weighted) between the source and the
    output shrunk back to the source size with an area filter (Hulk's
    matches_source). Faithful upscales score <= ~20, garbage 75+."""
    small = np.asarray(out_rgb.resize(src_rgba.size, Image.Resampling.BOX), dtype=np.float32)
    s = np.asarray(src_rgba, dtype=np.float32)
    wa = s[..., 3:4] / 255.0
    den = float(wa.sum()) * 3.0
    if den < 1:
        return float(np.abs(small - s[..., :3]).mean())
    return float((np.abs(small - s[..., :3]) * wa).sum() / den)


def noise_ratio(src_rgba, out_rgba):
    """High frequency the output carries vs the source's own local contrast
    (Hulk's ai_noise_ratio). Faithful 0.34 median; a smoke puff cracked by
    the AI 0.98, a cloud turned to gravel 1.18. Above 0.8 on a smooth source
    the result is invented."""
    s = np.asarray(src_rgba, dtype=np.float32)
    o = np.asarray(out_rgba, dtype=np.float32)
    sh, sw = s.shape[:2]
    oh, ow = o.shape[:2]
    if sw == 0 or ow % sw or oh % sh or ow // sw != oh // sh:
        return 0.0
    k = ow // sw
    sp = np.concatenate([s[..., :3] * s[..., 3:4] / 255.0, s[..., 3:4]], axis=2)
    op = np.concatenate([o[..., :3] * o[..., 3:4] / 255.0, o[..., 3:4]], axis=2)
    pad = np.pad(sp, ((1, 1), (1, 1), (0, 0)), mode="edge")
    g = np.zeros((sh, sw), np.float32)
    for dy, dx in ((0, 1), (2, 1), (1, 0), (1, 2)):
        nb = pad[dy:dy + sh, dx:dx + sw]
        g = np.maximum(g, np.abs(sp - nb).max(axis=2))
    blocks = op.reshape(sh, k, sw, k, 4)
    mean = blocks.mean(axis=(1, 3), keepdims=True)
    dev = ((blocks - mean) ** 2).max(axis=4)
    e = dev.mean(axis=(1, 3))
    return float(np.sqrt(e.mean()) / (np.sqrt((g * g).mean()) + 1.0))


def channel_means(rgba):
    a = np.asarray(rgba.convert("RGBA"), dtype=np.float32)
    wa = a[..., 3:4] / 255.0
    s = float(wa.sum())
    if s < 1:
        return a[..., :3].reshape(-1, 3).mean(axis=0)
    return (a[..., :3] * wa).reshape(-1, 3).sum(axis=0) / s


# --- the job list ----------------------------------------------------------------

class Job:
    __slots__ = ("item", "src", "dst", "scale", "action", "notes", "result", "img", "pooled",
                 "batch_name", "model")

    def __init__(self, item, dst, scale, action):
        self.item = item
        self.src = item.path
        self.dst = dst
        self.scale = scale
        self.action = action
        self.notes = []
        self.result = ""
        self.img = None
        self.pooled = (item.m or {}).get("pooled", 1.0)
        self.batch_name = None
        self.model = None


class Settings:
    def __init__(self):
        self.scale = 4
        self.strength = 1.0          # 1 = pure AI; < 1 = detail transfer at that strength
        self.keep_format = True
        self.skip_existing = True
        self.kept_mode = "copy"      # copy | plain | omit
        self.normal_mode = "ai"      # ai | plain
        self.noise = 0
        self.tta = False
        self.max_side = 4096


def out_path(job_dst_base, src, keep_format):
    ext = os.path.splitext(src)[1].lower()
    if not keep_format:
        ext = ".png"
    return job_dst_base + ext


# --- engine ----------------------------------------------------------------------

class Engine:
    """One ncnn-vulkan upscaler + model (the option dict from discover_models)."""

    def __init__(self, opt, noise=0, tta=False):
        self.opt = opt
        self.noise = noise
        self.tta = tta
        self.proc = None

    def native(self):
        """The scale one pass makes."""
        ns = self.opt.get("native_scale")
        if ns:
            return ns
        if self.opt["engine"] == "realcugan":
            # Real-CUGAN's model sets: se has 2/3/4x, pro 2/3x, nose 2x only
            return {"models-pro": 3, "models-nose": 2}.get(self.opt["model_name"], 4)
        return 4

    def passes_for(self, scale):
        """Passes needed to reach `scale` (then shrunk to it): 8x on a 4x
        network = two passes (16x) halved; 4x on a 2x network = two passes."""
        p = 1
        while self.native() ** p < scale:
            p += 1
        return p

    def args(self, inp, out):
        o = self.opt
        eng = o["engine"]
        a = [o["exe"], "-i", inp, "-o", out, "-s", str(self.native()), "-m", o["models_dir"], "-f", "png"]
        if eng == "realesrgan":
            a += ["-n", o["model_name"]]
        else:
            a += ["-n", str(self.noise)]
        if self.tta:
            a.append("-x")
        return a

    def run(self, inp, out, cancel, on_count=None):
        """Run the engine on a file or folder. False when cancelled or stuck."""
        if os.environ.get("ACME_NO_GPU"):
            raise RuntimeError("ACME_NO_GPU is set: the AI engine is not started")
        flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
        flags |= 0x00004000 if os.name == "nt" else 0         # BELOW_NORMAL_PRIORITY_CLASS
        self.proc = subprocess.Popen(self.args(inp, out), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags,
                                     cwd=os.path.dirname(self.opt["exe"]))
        last_n, last_t = -1, time.time()
        is_dir = os.path.isdir(out)
        while self.proc.poll() is None:
            if cancel.is_set():
                self.proc.terminate()
                self.proc.wait()
                return False
            time.sleep(0.25)
            n = len(os.listdir(out)) if is_dir else int(os.path.isfile(out))
            if n != last_n:
                last_n, last_t = n, time.time()
                if on_count:
                    on_count(n)
            elif time.time() - last_t > WATCHDOG_S:
                self.proc.terminate()
                self.proc.wait()
                return False
        return True


# --- the run ---------------------------------------------------------------------

class Runner:
    """Does a whole folder. Messages go to q as (kind, payload) like before."""

    def __init__(self, items, in_root, out_root, settings, engine_opt, lowq_opt, q, cancel):
        self.items = items
        self.in_root = in_root
        self.out_root = out_root
        self.s = settings
        self.engine_opt = engine_opt
        self.lowq_opt = lowq_opt or engine_opt
        self.q = q
        self.cancel = cancel
        self.lock = threading.Lock()
        self.done = 0
        self.counts = {"ai": 0, "sharp": 0, "kept": 0, "plain": 0, "fallback": 0, "skipped": 0,
                       "failed": 0, "existing": 0, "omitted": 0}
        self.results = {}
        self.t0 = time.time()
        self.workers = max(2, (os.cpu_count() or 4) - 2)

    # messages
    def log(self, m):
        self.q.put(("log", m))

    def tick(self, name):
        with self.lock:
            self.done += 1
            d = self.done
        self.q.put(("file", {"i": d, "total": len(self.items), "name": name,
                             "elapsed": time.time() - self.t0, "done": d}))

    def note(self, job, result, *notes):
        job.result = result
        job.notes.extend(notes)
        with self.lock:
            self.results[job.item.rel] = {"result": result, "notes": "; ".join(job.notes)}

    # file output
    def save(self, img, job, src_mode_alpha):
        path = job.dst
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ext = os.path.splitext(path)[1].lower()
        if not src_mode_alpha and img.mode == "RGBA" and img.getchannel("A").getextrema() == (255, 255):
            img = img.convert("RGB")
        try:
            if ext in (".jpg", ".jpeg", ".jfif"):
                img.convert("RGB").save(path, quality=95)
            elif ext == ".webp":
                img.save(path, quality=95, method=4)
            elif ext == ".png":
                img.save(path, compress_level=6)
            elif ext == ".bmp":
                img.save(path)
            else:
                img.save(path)
        except Exception:     # noqa: BLE001 - a format Pillow cannot write: PNG
            alt = os.path.splitext(path)[0] + ".png"
            img.save(alt)
            job.dst = alt
            job.notes.append("saved as PNG (cannot write %s)" % ext)

    def load_src(self, job):
        img, _mode = C.load_rgba(job.src)
        job.img = img
        return img, img.getchannel("A").getextrema()[0] < 255

    # non-AI actions
    def do_simple(self, job):
        try:
            if self.cancel.is_set():
                return
            if job.action == "skip":
                self.note(job, "skipped", job.item.reason)
                with self.lock:
                    self.counts["skipped"] += 1
                return
            if job.action == "omit":
                self.note(job, "left out")
                with self.lock:
                    self.counts["omitted"] += 1
                return
            if job.action == "copy":
                os.makedirs(os.path.dirname(job.dst), exist_ok=True)
                d = os.path.splitext(job.dst)[0] + os.path.splitext(job.src)[1]
                shutil.copy2(job.src, d)
                job.dst = d
                self.note(job, "original kept")
                with self.lock:
                    self.counts["kept"] += 1
                return
            img, has_a = self.load_src(job)
            size = (img.width * job.scale, img.height * job.scale)
            if job.action == "sharp":
                out = sharp_resize(img, job.scale, size)
                self.save(out, job, has_a)
                self.note(job, "sharp resize %dx" % job.scale)
                with self.lock:
                    self.counts["sharp"] += 1
            elif job.action == "plain" or job.action == "normal_plain":
                out = resize_straight(img, size)
                if job.action == "normal_plain":
                    rgb = renormalise(out.convert("RGB"), img)
                    rgb.putalpha(out.getchannel("A"))
                    out = rgb
                self.save(out, job, has_a)
                self.note(job, "plain resize %dx" % job.scale)
                with self.lock:
                    self.counts["plain"] += 1
        except Exception as e:     # noqa: BLE001
            self.note(job, "FAILED", str(e))
            with self.lock:
                self.counts["failed"] += 1
            self.log("FAILED %s : %s" % (job.item.rel, e))
        finally:
            job.img = None
            self.tick(job.item.rel)

    # AI
    def prepare(self, batch, folder):
        """Write the batch's inputs: RGB only, straight colour (the colour under
        alpha 0 is kept, not blackened)."""
        os.makedirs(folder, exist_ok=True)

        def one(k_job):
            k, job = k_job
            if self.cancel.is_set():
                return
            try:
                img, _ = self.load_src(job)
                job.batch_name = "%05d.png" % k
                img.convert("RGB").save(os.path.join(folder, job.batch_name), compress_level=1)
            except Exception as e:     # noqa: BLE001
                job.batch_name = None
                job.notes.append("cannot read: %s" % e)
        with ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(one, enumerate(batch)))

    def finish(self, job, raw_path, engine, passes):
        """Check one AI output and write the final file. Returns False when the
        output failed the checks (the caller redoes it alone)."""
        src = job.img
        if src is None:
            src, _ = self.load_src(job)
        has_a = src.getchannel("A").getextrema()[0] < 255
        k = engine.native() ** passes
        try:
            with Image.open(raw_path) as r:
                r.load()
                raw = r.convert("RGB")
        except Exception:      # noqa: BLE001
            return False
        if raw.size != (src.width * k, src.height * k) or looks_blank(raw):
            return False
        dist = source_distance(src, raw)
        if dist > 25.0:
            job.notes.append("AI output off the source (%.0f)" % dist)
            return False
        size = (src.width * job.scale, src.height * job.scale)
        rgb = raw if raw.size == size else raw.resize(size, Image.Resampling.BOX)   # area = FANT
        if job.action == "normal":
            rgb = detail_transfer(rgb, src, 1.0, size)
            rgb = renormalise(rgb, src)
        elif self.s.strength < 0.999 and job.action != "lowq":
            # (low-quality images always take the full JPEG-model result: a
            # detail transfer would lay it over a resize of the blocks)
            rgb = detail_transfer(rgb, src, self.s.strength, size)
        out = rgb.convert("RGBA")
        out.putalpha(resize_alpha(src.getchannel("A"), size))
        # an AI result that invented structure on a smooth source -> keep the original
        if job.pooled < 0.05 and job.action != "normal":
            nr = noise_ratio(src, out)
            if nr > 0.8:
                d = os.path.splitext(job.dst)[0] + os.path.splitext(job.src)[1]
                os.makedirs(os.path.dirname(d), exist_ok=True)
                if self.s.kept_mode == "copy":
                    shutil.copy2(job.src, d)
                    job.dst = d
                elif self.s.kept_mode == "plain":
                    self.save(resize_straight(src, size), job, has_a)
                job.item.cls, job.item.reason = "soft", "the AI invented grain on a smooth source (%.2f)" % nr
                self.note(job, "original kept", "AI result rejected: invented grain %.2f" % nr)
                with self.lock:
                    self.counts["kept"] += 1
                return True
        # channel means against the source (flag outliers, keep the result)
        if job.action != "normal":
            dm = channel_means(out) - channel_means(src)
            worst = float(np.abs(dm).max())
            if worst > 8.0:
                job.notes.append("colour shift R%+.0f G%+.0f B%+.0f" % tuple(dm))
        self.save(out, job, has_a)
        self.note(job, "AI %dx" % job.scale + (" (detail %.0f%%)" % (self.s.strength * 100)
                                                if self.s.strength < 0.999 and job.action == "ai" else ""))
        with self.lock:
            self.counts["ai"] += 1
        return True

    def fallback(self, job, why):
        try:
            src = job.img
            if src is None:
                src, _ = self.load_src(job)
            has_a = src.getchannel("A").getextrema()[0] < 255
            size = (src.width * job.scale, src.height * job.scale)
            out = resize_straight(src, size)
            if job.action == "normal":
                rgb = renormalise(out.convert("RGB"), src)
                rgb.putalpha(out.getchannel("A"))
                out = rgb
            self.save(out, job, has_a)
            self.note(job, "plain resize (AI failed)", why)
            with self.lock:
                self.counts["fallback"] += 1
            self.log("AI failed on %s (%s) - plain resize instead" % (job.item.rel, why))
        except Exception as e:     # noqa: BLE001
            self.note(job, "FAILED", str(e))
            with self.lock:
                self.counts["failed"] += 1

    def redo_single(self, job, engine, passes, tmp):
        """One job alone (after a failed batch output)."""
        d = os.path.join(tmp, "single")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        src = job.img
        if src is None:
            src, _ = self.load_src(job)
        p_in = os.path.join(d, "in.png")
        src.convert("RGB").save(p_in, compress_level=1)
        cur = p_in
        for i in range(passes):
            p_out = os.path.join(d, "out%d.png" % i)
            if not engine.run(cur, p_out, self.cancel) or not os.path.isfile(p_out):
                return False
            cur = p_out
        return self.finish(job, cur, engine, passes)

    def run_ai(self, jobs, engine, label):
        """All AI jobs for one engine, pipelined in batches."""
        if not jobs:
            return
        groups = {}   # passes -> jobs (a capped scale may need fewer passes)
        for j in jobs:
            groups.setdefault(engine.passes_for(j.scale), []).append(j)
        tmp = tempfile.mkdtemp(prefix="acme_", dir=self.out_root)
        try:
            for passes, gj in sorted(groups.items()):
                size = AI_BATCH_8X if engine.native() ** passes >= 16 else AI_BATCH
                batches = [gj[i:i + size] for i in range(0, len(gj), size)]
                self.log("%s: %d image(s) in %d batch(es)%s" % (
                    label, len(gj), len(batches), " (%d passes each)" % passes if passes > 1 else ""))
                cpu = ThreadPoolExecutor(1)
                fin = ThreadPoolExecutor(1)
                prep = cpu.submit(self.prepare, batches[0], os.path.join(tmp, "b0", "in"))
                pending_finish = None
                for bi, batch in enumerate(batches):
                    if self.cancel.is_set():
                        break
                    prep.result()
                    bdir = os.path.join(tmp, "b%d" % bi)
                    if bi + 1 < len(batches):
                        prep = cpu.submit(self.prepare, batches[bi + 1], os.path.join(tmp, "b%d" % (bi + 1), "in"))
                    cur = os.path.join(bdir, "in")
                    ok = True
                    for p in range(passes):
                        o = os.path.join(bdir, "out%d" % p)
                        os.makedirs(o, exist_ok=True)
                        self.q.put(("stage", "AI batch %d of %d%s" % (
                            bi + 1, len(batches), ", pass %d of %d" % (p + 1, passes) if passes > 1 else "")))
                        ok = engine.run(cur, o, self.cancel) and ok
                        cur = o
                    if self.cancel.is_set():
                        break
                    if pending_finish:
                        self.retry_failed(pending_finish.result(), engine, passes, tmp)
                    pending_finish = fin.submit(self.finish_batch, batch, cur, engine, passes, bdir)
                if pending_finish:
                    self.retry_failed(pending_finish.result(), engine, passes, tmp)
                cpu.shutdown(wait=True)
                fin.shutdown(wait=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def finish_batch(self, batch, out_dir, engine, passes, bdir):
        failed = []

        def one(job):
            if self.cancel.is_set():
                return
            try:
                if not job.batch_name:
                    self.fallback(job, "; ".join(job.notes) or "unreadable")
                    job.img = None
                    self.tick(job.item.rel)
                    return
                p = os.path.join(out_dir, job.batch_name)
                if os.path.isfile(p) and self.finish(job, p, engine, passes):
                    job.img = None
                    self.tick(job.item.rel)
                else:
                    with self.lock:
                        failed.append(job)
            except Exception as e:     # noqa: BLE001
                job.notes.append(str(e))
                with self.lock:
                    failed.append(job)
        with ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(one, batch))
        shutil.rmtree(bdir, ignore_errors=True)
        return failed

    def retry_failed(self, failed, engine, passes, tmp):
        for job in failed:
            if self.cancel.is_set():
                return
            try:
                ok = self.redo_single(job, engine, passes, tmp)
            except Exception as e:     # noqa: BLE001
                ok = False
                job.notes.append(str(e))
            if ok:
                job.notes.append("redone alone")
            else:
                self.fallback(job, "output failed the checks twice")
            job.img = None
            self.tick(job.item.rel)

    # whole run
    def plan(self):
        jobs = []
        for it in self.items:
            rel_base = os.path.splitext(it.rel)[0]
            dst_base = os.path.join(self.out_root, rel_base)
            ext = os.path.splitext(it.path)[1].lower()
            out_ext = ext if self.s.keep_format else ".png"
            m = it.m or {}
            w, h = m.get("w", 0), m.get("h", 0)
            scale = fit_scale(w, h, self.s.scale, self.s.max_side) if w else self.s.scale
            cls = it.cls
            if cls == "bad":
                action = "skip"
            elif w and scale == 1:
                cls = it.cls = "big" if cls not in C.KEPT_CLASSES else cls
                if it.cls == "big":
                    it.reason = "already %dx%d: a larger copy would pass %d px" % (w, h, self.s.max_side)
                action = "copy" if self.s.kept_mode != "omit" else "omit"
            elif cls == "maybe" and max(w, h) >= 512:
                # a big image the OCR half-reads is usually an atlas of small
                # tiles (Fable II: 136 of 210 unsure); a sharp resize keeps any
                # real lettering intact and still enlarges the tiles
                action = "sharp"
                it.reason += " - large atlas: sharp resize"
            elif cls in C.KEPT_CLASSES:
                action = {"copy": "copy", "plain": "plain", "omit": "omit"}[self.s.kept_mode]
            elif cls == "text":
                action = "sharp"
            elif cls == "normal":
                action = "normal" if self.s.normal_mode == "ai" else "normal_plain"
            elif cls in ("ai", "lowq"):
                action = cls
            else:
                action = "ai"
            if self.s.strength <= 0.001:   # detail 0%: no AI at all
                action = {"ai": "plain", "lowq": "plain", "normal": "normal_plain"}.get(action, action)
            if action in ("ai", "lowq", "normal") and w and min(w, h) < MIN_AI_SIDE:
                action = "plain"
                it.reason += " (under %d px: plain resize)" % MIN_AI_SIDE
            j = Job(it, dst_base + out_ext, scale, action)
            jobs.append(j)
        return jobs

    def exists(self, job):
        base = os.path.splitext(job.dst)[0]
        for e in (os.path.splitext(job.dst)[1], ".png", os.path.splitext(job.src)[1]):
            if os.path.isfile(base + e):
                return True
        return False

    def run(self):
        jobs = self.plan()
        if self.s.skip_existing:
            left = []
            for j in jobs:
                if j.action not in ("skip", "omit") and self.exists(j):
                    self.note(j, "already done")
                    self.counts["existing"] += 1
                    self.tick(j.item.rel)
                else:
                    left.append(j)
            jobs = left
        simple = [j for j in jobs if j.action not in ("ai", "lowq", "normal")]
        ai = [j for j in jobs if j.action in ("ai", "normal")]
        lowq = [j for j in jobs if j.action == "lowq"]
        if lowq and self.lowq_opt is self.engine_opt:
            ai += lowq
            lowq = []
        # the CPU work runs beside the first AI batches
        pool = ThreadPoolExecutor(max(1, self.workers // 2))
        futs = [pool.submit(self.do_simple, j) for j in simple]
        try:
            if ai:
                self.run_ai(ai, Engine(self.engine_opt, self.s.noise, self.s.tta), self.engine_opt["label"])
            if lowq and not self.cancel.is_set():
                self.run_ai(lowq, Engine(self.lowq_opt, 0, self.s.tta),
                            "low-quality images: " + self.lowq_opt["label"])
        finally:
            for f in futs:
                if self.cancel.is_set():
                    f.cancel()
            pool.shutdown(wait=True)
        return self.counts, self.results
