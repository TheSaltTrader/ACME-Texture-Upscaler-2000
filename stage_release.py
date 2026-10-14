"""Stage a portable release of ACME Texture Upscaler 2000 into ./Releases/vX.Y.Z.

The release is the exe, the upscaler engine and its models, and the README - a
folder anyone can unzip and run. Also writes a zip beside it.
"""
import os
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def read_version():
    with open(os.path.join(HERE, "VERSION")) as f:
        return f.read().strip()


def main():
    version = read_version()
    dest = os.path.join(HERE, "Releases", "v" + version)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)

    exe = os.path.join(HERE, "dist", "ACMETextureUpscaler2000.exe")
    if not os.path.isfile(exe):
        raise SystemExit("build the exe first (build_portable.cmd)")
    shutil.copy2(exe, os.path.join(dest, "ACMETextureUpscaler2000.exe"))
    for doc in ("README.txt", "THIRD_PARTY_NOTICES.txt", "LICENSE"):
        shutil.copy2(os.path.join(HERE, doc), os.path.join(dest, doc))

    # The engine + models, external so the app finds them beside the exe.
    # (only the bundled engine; never a downloaded Real-CUGAN / waifu2x)
    src_up = os.path.join(HERE, "upscaler")
    need = ["realesrgan-ncnn-vulkan.exe", "vcomp140.dll"]
    models = ["realesrgan-x4plus", "realesrgan-x4plus-anime", "realesr-animevideov3-x2",
              "realesr-animevideov3-x3", "realesr-animevideov3-x4", "4x_NMKD-Superscale-SP_178000_G"]
    os.makedirs(os.path.join(dest, "upscaler", "models"))
    for n in need:
        shutil.copy2(os.path.join(src_up, n), os.path.join(dest, "upscaler", n))
    for m in models:
        for ext in (".param", ".bin"):
            p = os.path.join(src_up, "models", m + ext)
            if not os.path.isfile(p):
                raise SystemExit("missing model file %s (python tools/fetch_engine.py)" % p)
            shutil.copy2(p, os.path.join(dest, "upscaler", "models", m + ext))

    files = []
    total = 0
    for dp, _, fns in os.walk(dest):
        for fn in fns:
            p = os.path.join(dp, fn)
            files.append(p)
            total += os.path.getsize(p)
    print("staged %d files, %.0f MB -> %s" % (len(files), total / 1048576, dest))

    zpath = os.path.join(HERE, "Releases",
                         "ACMETextureUpscaler2000-v%s-win-amd64.zip" % version)
    if os.path.isfile(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, os.path.relpath(p, dest))
    print("zip: %s (%.0f MB)" % (zpath, os.path.getsize(zpath) / 1048576))


if __name__ == "__main__":
    main()
