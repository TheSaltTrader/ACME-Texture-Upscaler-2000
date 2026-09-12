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
    shutil.copy2(os.path.join(HERE, "README.txt"), os.path.join(dest, "README.txt"))

    # The engine + models, external so the app finds them beside the exe.
    src_up = os.path.join(HERE, "upscaler")
    shutil.copytree(src_up, os.path.join(dest, "upscaler"))

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
