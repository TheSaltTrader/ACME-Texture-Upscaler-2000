# Changelog

## 1.1.0 - 2026-10-13

Sorts every image before it reaches the AI, using what the Hulk, Ninja Gaiden II /
Black, Fable II, Bloodborne and Def Jam texture packs learned.

- **Sorting** (`acme_classify.py`), with a per-file reason, `_acme_report\report.csv`,
  `summary.txt` and contact sheets per class:
  - lettering (Windows OCR) -> sharp resize, never AI;
  - possible lettering, soft / blurry / glow, lighting / data (name hints, strips, flat
    fills, noise / dither, BC4 / BC6H / float DDS), tiny (< 32 px), too large -> original
    kept (512 px+ possible lettering gets the sharp resize);
  - normal maps -> AI detail over a plain resize, vectors renormalised;
  - heavy JPEG blocking -> Real-ESRGAN x4plus;
  - user lists `acme_keep.txt` / `acme_sharp.txt` / `acme_ai.txt` win.
- **Quality** (`acme_engine.py`): NMKD Superscale is the default model (bundled, WTFPL);
  4x networks run at 4x then area-shrink for 2x / 3x; new 8x (two 4x passes, halved);
  4096 px cap; alpha resized apart from colour (straight alpha), cut-outs re-thresholded;
  every AI output checked (size, blank, still matches the source) -> redone alone, then
  plain resize; invented-grain and channel-mean checks; detail strength 75% for photo
  models; batches of 64, pipelined with the CPU work.
- **Advanced** panel for every threshold and what kept images get (copy / plain / omit).
- `--cli` mode (`acme_cli.py`), `THIRD_PARTY_NOTICES.txt` (Real-ESRGAN BSD-3, ncnn BSD-3,
  Real-ESRGAN-ncnn-vulkan MIT, Superscale WTFPL).

## 1.0.0 - 2026-09-12

First release: folder in / out with the tree preserved; 2x / 3x / 4x; PNG, JPG, WebP,
BMP, TGA, TIFF, DDS; Real-ESRGAN models bundled (x4plus default, x4plus-anime, anime
video v3), Real-CUGAN and waifu2x via "Get more models..."; detail-strength slider;
progress, ETA, cancel; skip-existing resume; Extra quality (TTA).
