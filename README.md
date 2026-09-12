# ACME Texture Upscaler 2000

A portable AI texture upscaler for Windows. Point it at a folder, choose a model
and a scale, press **Start**. Every image it finds is enlarged with an AI
upscaler and written to an output folder with the sub-folder tree preserved.

It is built for game and mod texture packs: it reads the formats textures ship
in (including DDS and TGA), keeps the folder layout, and gives you a detail
control so a model that over-sharpens can be dialed back. It is the same
upscaling pipeline used to build the Ninja Gaiden II recompile texture pack,
packaged as a standalone tool.

## Download and run

The ready-to-run build is on the [Releases page](../../releases). Download the
`...-win-amd64.zip`, unzip it anywhere, and run `ACMETextureUpscaler2000.exe`.
Nothing is installed. Keep the `upscaler` folder next to the program — that is
the engine and its models.

1. Browse to the folder of textures.
2. It fills in an output folder for you (`<folder>_upscaled`); change it if you like.
3. Pick a model and a scale (2x / 3x / 4x).
4. **Scan folder**, then **Start**.

A progress bar, a running count, elapsed time and a time-remaining estimate are
shown while it works. **Cancel** stops after the current image. **Open output**
opens the result folder.

## Features

- **Any texture format in a pack.** PNG, JPG/JPEG and WebP go straight through
  the engine; BMP, TGA, TIFF and DDS are decoded first and written back in their
  own format where possible, otherwise as PNG.
- **2x / 3x / 4x** scaling.
- **Multiple models,** selectable per run — see below.
- **Detail-strength slider.** Blends the AI result with a plain enlargement of
  the same texture. 100% is pure AI (the default); 0% is a plain resize with no
  AI; anywhere in between is a mix, so a model that over-sharpens or invents
  detail can be reined in. This is the same control the Ninja Gaiden II pack
  uses, so a value here matches that pack at the same setting.
- **Progress + ETA,** a running count, and a per-image log.
- **Recursive** folder walking, **skip-existing** resume, and an optional
  **Extra quality (slow)** TTA mode.
- **Fully portable.** No install, no admin, no network needed to upscale.

## Models

Ships with **Real-ESRGAN** and lists whatever models are installed:

| Model | Best for |
| --- | --- |
| Photo 4x — `x4plus` | The general, widely-used default. Recommended for most textures. Sharpest; occasionally invents detail. |
| Soft 4x — `net` | Fewer artifacts, more faithful, a touch softer. Fetch with "Get more models". |
| Art 4x — `x4plus-anime` | Tuned for drawn / cel art. |
| Fast 2x/3x/4x — anime video | Quickest and lightest; good for large batches. |

**Get more models...** inside the app can fetch two other portable engines and
add them to the same list:

- **Real-CUGAN** — often the most precise on drawn and game art: better detail
  preservation and less over-sharpening than Real-ESRGAN. Has a denoise level.
- **waifu2x** — clean line art and gentle denoise.

There is no single best model for every texture; the selector is there so you
can compare on your own art. Start with Photo 4x (`x4plus`); if it looks
over-sharpened or "painted", try Real-CUGAN, Soft 4x, or lower the detail slider.

## Run from source

Needs Python 3.10+ with Tkinter (the standard Windows installer includes it).

```
pip install -r requirements.txt
python tools/fetch_engine.py      # downloads the Real-ESRGAN engine into ./upscaler/
python acme_upscaler.py           # or run.cmd
```

`tools/fetch_engine.py` pulls the official portable Real-ESRGAN Windows build
(the engine binaries and default models are third-party redistributables, so
they are not committed here). The in-app "Get more models" button adds
Real-CUGAN and waifu2x the same way.

## Build the portable exe

```
build_portable.cmd     # PyInstaller one-file build -> dist\ACMETextureUpscaler2000.exe
python stage_release.py  # bundles exe + upscaler engine into Releases\vX.Y.Z + a zip
```

Bump `VERSION` before building.

## What is in this repository

Source only: the application, the build and release scripts, and this
documentation. The upscaling engines (Real-ESRGAN, Real-CUGAN, waifu2x) are
third-party redistributables and are provided in the release zip, not committed
here. Sample and comparison images made from game textures are never included.

## Credits and license

This program is a front-end. The upscaling engines are **Real-ESRGAN**,
**Real-CUGAN** and **waifu2x**, each by their respective authors, distributed
under their own licenses. This front-end is released under the MIT License
(see [LICENSE](LICENSE)).
