"""Command line for ACME Texture Upscaler 2000 (scripted batches, tests).

    ACMETextureUpscaler2000.exe --cli <input folder> <output folder> [options]
    python acme_upscaler.py --cli <input folder> <output folder> [options]

Options:
    --scale 2|3|4|8         (default 4)
    --model <name>          a model file name or part of a label (default: the recommended one)
    --strength 0-100        detail strength (default 100; 75 for photo models)
    --sort-only             sort and write the report (and sheets), no upscale
    --no-sort               every image goes to the AI (the 1.0 behaviour)
    --no-ocr                lettering by the heuristic instead of Windows OCR
    --no-sheets             no contact sheets
    --kept copy|plain|omit  what kept images get (default copy)
    --lowq jpeg|same        model for JPEG-blocked images (default jpeg = x4plus)
    --png                   write PNG instead of the original format
    --log <file>            also write the log there

Progress and the summary go to stdout (and --log). Exit code 0 when nothing failed.
"""

import os
import queue
import sys
import threading
import time

import acme_classify as C
import acme_engine as E


def main(argv):
    import acme_upscaler as U
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        _out(__doc__)
        return 2
    src, dst = os.path.abspath(argv[0]), os.path.abspath(argv[1])
    opts = {"scale": "4", "model": "", "strength": None, "kept": "copy", "lowq": "jpeg", "log": ""}
    flags = set()
    i = 2
    while i < len(argv):
        a = argv[i]
        if a in ("--scale", "--model", "--strength", "--kept", "--lowq", "--log") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
            continue
        flags.add(a)
        i += 1
    logf = open(opts["log"], "a", encoding="utf-8") if opts["log"] else None

    def log(m):
        line = time.strftime("%H:%M:%S ") + m
        _out(line)
        if logf:
            logf.write(line + "\n")
            logf.flush()

    models = U.discover_models()
    if not models:
        log("no upscaler engine found beside the program")
        return 3
    opt = None
    if opts["model"]:
        for m in models:
            if opts["model"].lower() in (m["model_name"].lower(), m["label"].lower()) or \
                    opts["model"].lower() in m["label"].lower():
                opt = m
                break
    if opt is None:
        labels = [m["label"] for m in models]
        for pref in U.PREFERRED_ORDER:
            if pref in labels:
                opt = models[labels.index(pref)]
                break
        opt = opt or models[0]
    lowq = None
    if opts["lowq"] == "jpeg":
        lowq = next((m for m in models if m["engine"] == "realesrgan" and m["model_name"] == U.LOWQ_MODEL), None)

    th = C.Thresholds()
    th.use_ocr = "--no-ocr" not in flags
    rdir = os.path.join(dst, U.REPORT_DIR)
    files = U.list_images(src, True, exclude=[dst])
    items = [C.Item(p, os.path.relpath(p, src)) for p in files]
    log("%d images under %s; model %s" % (len(items), src, opt["label"]))
    cancel = threading.Event()
    t0 = time.time()
    if "--no-sort" in flags:
        for it in items:
            try:
                img, _ = C.load_rgba(it.path)
                it.m = {"w": img.width, "h": img.height, "pooled": 1.0}
                it.cls, it.reason = "ai", "sorting off"
            except Exception as e:     # noqa: BLE001
                it.cls, it.reason = "bad", "cannot decode: %s" % e
    else:
        import shutil
        import tempfile
        wd = tempfile.mkdtemp(prefix="acme_sort_")
        last = [0.0]

        def prog(st, k, n):
            if time.time() - last[0] > 5 or k == n:
                last[0] = time.time()
                log("sorting: %s %d/%d" % (st, k, n))
        try:
            C.classify_items(items, src, th, wd, log=log, progress=prog, cancel=cancel,
                             cache_path=os.path.join(rdir, C.CACHE_NAME))
        finally:
            shutil.rmtree(wd, ignore_errors=True)
    log("sorted in %.1f s" % (time.time() - t0))
    for ln in C.write_report(rdir, items):
        log("  " + ln)
    if "--no-sheets" not in flags and "--no-sort" not in flags:
        n = len(C.contact_sheets(rdir, items))
        log("%d contact sheet(s) in %s" % (n, os.path.join(rdir, "sheets")))
    if "--sort-only" in flags:
        return 0

    s = E.Settings()
    s.scale = int(opts["scale"])
    s.kept_mode = opts["kept"]
    s.keep_format = "--png" not in flags
    s.skip_existing = "--redo" not in flags
    if opts["strength"] is not None:
        s.strength = float(opts["strength"]) / 100.0
    elif opt.get("model_name") in U.PHOTO_MODELS:
        s.strength = 0.75
    q = queue.Queue()
    runner = E.Runner(items, src, dst, s, opt, lowq, q, cancel)
    res = {}

    def run():
        res["r"] = runner.run()
    t = threading.Thread(target=run)
    t.start()
    last = 0.0
    while t.is_alive() or not q.empty():
        try:
            kind, p = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if kind == "log":
            log(p)
        elif kind == "stage":
            log(p)
        elif kind == "file" and (time.time() - last > 5 or p["i"] == p["total"]):
            last = time.time()
            log("[%d/%d] %s" % (p["i"], p["total"], p["name"]))
    t.join()
    counts, results = res["r"]
    C.write_report(rdir, items, results)
    log("done in %.1f s: %s" % (time.time() - t0, ", ".join("%s %d" % kv for kv in counts.items())))
    return 1 if counts["failed"] else 0


def _out(s):
    try:
        if sys.stdout:
            print(s, flush=True)
    except Exception:     # noqa: BLE001
        pass
