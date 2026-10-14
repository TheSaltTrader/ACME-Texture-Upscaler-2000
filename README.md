# ACME Texture Upscaler 2000

A portable AI texture upscaler for Windows. Point it at a folder, choose a scale,
press **Start**. Before anything reaches the AI, every image is **sorted**: texture
art goes to the AI, lettering gets a sharp resize, and the kinds of image an AI
upscaler damages (soft glows, lighting and data textures, tiny images, unsure
lettering) keep the original. A report says what happened to every file and why.

The rules come from the texture packs of several game PC ports (Hulk, Ninja Gaiden
II, Ninja Gaiden Black, Fable II, Bloodborne, Def Jam), where each one was found
the hard way and tuned on contact sheets of real textures.

## Download and run

The ready-to-run build is on the [Releases page](../../releases). Unzip the
`...-win-amd64.zip` anywhere and run `ACMETextureUpscaler2000.exe`. Nothing is
installed. Keep the `upscaler` folder next to the program (the engine and models).

1. Browse to the folder of textures (an output folder `<folder>_upscaled` is filled in).
2. Pick a scale: 2x / 3x / 4x / 8x.
3. **Scan and sort**, look at the summary (and the contact sheets), then **Start**.

Progress, elapsed time, an ETA and **Cancel** are shown while it works. **Open
report** opens `_acme_report\` in the output folder: `report.csv` (one line per
image: class, reason, measurements, result, warnings), `summary.txt`, and
`sheets\<class>_NN.png` contact sheets.

## How images are sorted

| Class | What it catches | What it gets |
| --- | --- | --- |
| **Art** | everything not below | the AI model |
| **Normal map** | means near 128/128 with high blue and unit-length vectors, a BC5/ATI2 DDS, or a name hint (`_n`, `_nrm`, `_normal`, `_ddna`...) | the AI's added detail over a plain resize (means stay put), then renormalised (Z rebuilt, or kept at 255 where the source's blue is constant); never colour-processed |
| **Low quality** | heavy JPEG blocking: 8x8 block-edge step / mid-block step > 1.8 in a .jpg (> 2.6 in other files) | Real-ESRGAN x4plus (trained on JPEG damage) instead of the texture model |
| **Lettering** | Windows OCR reads a real word (several sizes, grey, inverted, rotated reads) | sharp resize (bicubic + light unsharp mask), never AI |
| **Possible lettering** | OCR finds letter-like shapes but no sure word | original kept; 512 px and larger (usually atlases of small tiles) get the sharp resize |
| **Soft / blurry** | detail ratio < 0.003 at any size; glow sprites (<= 256 px, dark border, peak >= 50, detail < 0.015); small glows (<= 64 px, detail < 0.04) | original kept |
| **Lighting / data** | name hints (`lightmap`, `_lm`, `_light`, `_shadow`, `lut`, `ramp`, `gi_env`, `env`, `refl`, `cube`, `noise`, `dither`...), long strips (aspect >= 8), flat fills (spread < 1.5), blank images, white noise / dither patterns, single-channel and HDR DDS formats (BC4, BC6H, R8, A8, float) | original kept |
| **Tiny** | under 32 px on a side | original kept |
| **Too large** | already so big that even 2x would pass 4096 px | original kept |

*Detail ratio* = the error of a 2x shrink-and-enlarge relative to the image's
variance (alpha-weighted; the Hulk port's soft test). Every threshold is editable
under **Advanced**, where you can also choose what kept images get (copy the
original, plain resize, or leave them out), switch the OCR or name hints off, and
turn contact sheets off. After the AI, a result that added grain to a smooth
source (high-frequency ratio > 0.8 on a source with detail < 0.05) is rejected and
the original kept.

**Your own lists win:** `acme_keep.txt`, `acme_sharp.txt` and `acme_ai.txt` in the
input folder, one file name, relative path or pattern (`*.tga`) per line.

Without Windows OCR (or with it switched off) a two-tone stroke heuristic can only
mark *possible* lettering, which keeps the original.

## Quality rules

- **NMKD Superscale** is the default model: it keeps texture detail where the
  photo model x4plus smears stone and carving and shifts brightness. For photo
  models the **Detail strength** slider defaults to 75%: the AI's high-pass laid
  over a plain resize, so tones and brightness stay the original's.
- 4x networks always run at 4x and are shrunk with an area (FANT-style) filter for
  2x / 3x (asked for 2x directly they draw shifted ghost edges). **8x** = two 4x
  passes, then halved. Nothing goes over **4096 px**: an image that would takes
  the next smaller scale.
- **Alpha never goes through the AI.** Colour and alpha are resized apart (an
  RGBA resize premultiplies and writes black under transparent pixels), and a
  cut-out (alpha only 0/255) is re-thresholded at 50% so its edge stays clean.
- **Every AI output is checked**: right size, not blank (a lost Vulkan device
  writes blanks), and shrunk back it still matches the source (mean difference
  <= 25 of 255). A failure is redone alone, then falls back to a plain resize.
  Channel means are compared with the source; a shift over 8 levels is flagged in
  the report.
- **Batches of 64** per engine run (starting the engine costs ~2 s an image when
  run singly), with the CPU preparing the next batch and finishing the last while
  the GPU works; a run that writes nothing for 5 minutes is ended. Inputs under
  16 px never go to the AI.

## Models

| Model | Best for |
| --- | --- |
| Texture 4x - **NMKD Superscale** (default) | game and material textures |
| Photo 4x - `x4plus` | photos and JPEG-damaged images (used for the Low-quality class) |
| Art 4x - `x4plus-anime` | drawn / cel art |
| Fast 2x/3x/4x - anime video v3 | quick, light |

**Get more models...** fetches Real-CUGAN (precise on drawn and game art, has a
denoise level) and waifu2x from their official release pages. Any other ncnn
model (`.param` + `.bin`) dropped into `upscaler\models` is listed too.

## Command line

```
ACMETextureUpscaler2000.exe --cli <input> <output> [--scale 4] [--model <name>]
    [--strength 0-100] [--sort-only] [--no-sort] [--no-ocr] [--no-sheets]
    [--kept copy|plain|omit] [--lowq jpeg|same] [--png] [--log <file>]
```

## Formats

PNG, JPG, WebP, BMP, TGA, TIFF and DDS in; written back in the same format where
Pillow can write it (DDS as uncompressed RGBA), otherwise PNG ("Keep original
format" off = PNG for everything).

## Run from source

Python 3.10+ with Tkinter.

```
pip install -r requirements.txt
python tools/fetch_engine.py      # Real-ESRGAN engine + models + NMKD Superscale into ./upscaler/
python acme_upscaler.py           # or run.cmd
```

## Build

```
build_portable.cmd     # PyInstaller one-file exe, then stage_release.py -> Releases\vX.Y.Z + zip
```

## What is in this repository

Source only. The engine and model files are third-party and come with the
release zip (or `tools/fetch_engine.py`). No game textures, sample images or
comparison images are included.

## Credits and licences

This front-end is MIT licensed ([LICENSE](LICENSE)). The release zip carries
Real-ESRGAN models (BSD 3-Clause), Real-ESRGAN ncnn Vulkan (MIT), ncnn (BSD
3-Clause) and **4x_NMKD-Superscale-SP_178000_G by Nmkd (WTFPL**, per the Open
Model Database; ncnn conversion from upscayl/custom-models). Full texts:
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt). Lettering detection uses
Windows' built-in OCR; nothing is shipped for it.
