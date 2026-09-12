"""Download the Real-ESRGAN ncnn-vulkan engine into ./upscaler/.

The engine and its models are third-party redistributables (not part of this
repository's source), so a fresh checkout does not include them. Run this once
to make the app runnable from source:

    python tools/fetch_engine.py

It fetches the official portable Real-ESRGAN Windows build (executable + the
default model set + vcomp140.dll) and lays it out as ./upscaler/, which is
exactly where the app looks. The Real-CUGAN and waifu2x engines can be added
later from inside the app ("Get more models..."); this script only bootstraps
the bundled Real-ESRGAN engine.
"""
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

# Official portable Real-ESRGAN ncnn-vulkan Windows release (self-contained:
# the exe, models/, and vcomp140.dll; no installer).
URL = ("https://github.com/xinntao/Real-ESRGAN/releases/download/"
       "v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip")
ENGINE_EXE = "realesrgan-ncnn-vulkan.exe"

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(os.path.dirname(HERE), "upscaler")


def main():
    if os.path.isfile(os.path.join(DEST, ENGINE_EXE)):
        print("engine already present at %s - nothing to do" % DEST)
        return
    with tempfile.TemporaryDirectory() as td:
        zpath = os.path.join(td, "engine.zip")
        print("downloading %s" % URL)

        def hook(blocks, bs, total):
            if total > 0:
                done = min(1.0, blocks * bs / total)
                sys.stdout.write("\r  %5.1f%% of %.0f MB" %
                                 (done * 100, total / 1048576))
                sys.stdout.flush()
        urllib.request.urlretrieve(URL, zpath, hook)
        print("\nextracting...")
        xd = os.path.join(td, "x")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(xd)
        # The engine exe marks the root of what we want.
        src_root = None
        for dp, _, fns in os.walk(xd):
            if ENGINE_EXE in fns:
                src_root = dp
                break
        if not src_root:
            raise SystemExit("engine exe not found in the downloaded archive")
        os.makedirs(DEST, exist_ok=True)
        for name in os.listdir(src_root):
            s = os.path.join(src_root, name)
            d = os.path.join(DEST, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    print("installed engine + models to %s" % DEST)


if __name__ == "__main__":
    main()
