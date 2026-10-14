ACME Texture Upscaler 2000  v1.1.0
==================================

A portable AI texture upscaler. Point it at a folder, choose a scale, press
Start. Every image is SORTED first: texture art goes to the AI, lettering gets
a sharp resize, and what an AI upscaler damages (soft glows, lighting and data
textures, tiny images, unsure lettering) keeps the original. A report in the
output folder (_acme_report) says what happened to every file and why.


Running it
----------

Unzip anywhere and run ACMETextureUpscaler2000.exe. Nothing is installed. Keep
the "upscaler" folder next to the program - that is the engine and its models.

  1. Browse to the folder of textures (an output folder is filled in).
  2. Pick a scale (2x / 3x / 4x / 8x).
  3. Scan and sort - read the summary (Open report shows the contact sheets).
  4. Start.


The classes
-----------

  Art                 -> AI (NMKD Superscale by default)
  Normal map          -> AI detail over a plain resize, vectors renormalised
  Low quality (JPEG)  -> Real-ESRGAN x4plus (trained on JPEG damage)
  Lettering (OCR)     -> sharp resize, never AI
  Possible lettering  -> original kept (512 px and larger: sharp resize)
  Soft / blurry       -> original kept (glows, halos, smoke, already blurry)
  Lighting / data     -> original kept (lightmaps, LUTs, ramps, strips, flat,
                         noise/dither, BC4/BC6H/float DDS, name hints)
  Tiny (< 32 px)      -> original kept
  Too large           -> original kept (nothing is made over 4096 px)

Thresholds and what kept images get (copy / plain resize / leave out) are under
Advanced. Your own lists in the input folder win: acme_keep.txt,
acme_sharp.txt, acme_ai.txt (file names, relative paths or patterns like *.tga).


Quality rules
-------------

- 4x networks run at 4x and are shrunk with an area filter for 2x/3x; 8x is two
  4x passes halved; the 4096 px cap takes the next smaller scale.
- Alpha never goes through the AI; cut-outs stay clean 0/255 edges.
- Every AI output is checked (size, blank, still matches the source); a failure
  is redone alone, then plain-resized. Colour shifts are flagged in the report.
- Detail strength below 100% lays the AI's detail over a plain resize (keeps
  the original's tones); photo models default to 75%.


Models
------

  Texture 4x - NMKD Superscale   default; keeps texture detail
  Photo 4x - x4plus              photos and JPEG damage
  Art 4x - x4plus-anime          drawn / cel art
  Fast 2x/3x/4x - anime video    quick

"Get more models..." adds Real-CUGAN and waifu2x from their official releases.
Any other ncnn model (.param + .bin) put in upscaler\models is listed too.


Command line
------------

  ACMETextureUpscaler2000.exe --cli <input> <output> [--scale 4] [--model name]
      [--strength 0-100] [--sort-only] [--no-sort] [--no-ocr] [--no-sheets]
      [--kept copy|plain|omit] [--lowq jpeg|same] [--png] [--redo]
      [--log file]

  Files already in the output folder are skipped (a cancelled run resumes);
  --redo makes them again.


Notes
-----

Upscaling runs on the GPU (Vulkan); do not run it while a game is using the GPU
(outputs can come back blank - they are caught and redone, but it is slow).
DDS is written back as uncompressed RGBA. Changes per version: CHANGELOG.md
(in the source repository). Licences: THIRD_PARTY_NOTICES.txt.
