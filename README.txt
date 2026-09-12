ACME Texture Upscaler 2000
==========================

A portable AI texture upscaler. Point it at a folder, choose a model and a
scale, press Start. Every image it finds is enlarged with an AI upscaler and
written to an output folder with the sub-folder tree preserved.


Running it
----------

Unzip anywhere and run ACMETextureUpscaler2000.exe. Nothing is installed. Keep
the "upscaler" folder next to the program - that is the engine and its models.

  1. Browse to the folder of textures.
  2. It fills in an output folder for you (folder_upscaled); change it if you like.
  3. Pick a model and a scale (2x / 3x / 4x).
  4. Scan folder, then Start.

A progress bar, a running count, elapsed time and a time-remaining estimate are
shown while it works. Cancel stops after the current image. Open output opens
the result folder.


Formats
-------

PNG, JPG/JPEG and WebP go straight through the engine. BMP, TGA, TIFF and DDS
are decoded first and written back in their own format where that format can be
written, otherwise as PNG. "Keep original format" controls that; turn it off to
write everything as PNG.


Models
------

It ships with Real-ESRGAN and lists whatever models are installed:

  Photo 4x - x4plus            the general, widely-used default. Recommended for
                               most textures. Sharpest, occasionally invents
                               detail that was not there.
  Soft 4x - net                fewer artifacts, more faithful, a touch softer
                               (download it with "Get more models" if not present).
  Art 4x - x4plus-anime        tuned for drawn / cel art.
  Fast 2x/3x/4x - anime video  quickest, lightest; good for large batches.

"Get more models..." can fetch two other portable engines and add them to the
same list:

  Real-CUGAN   often the most precise on drawn and game art - better detail
               preservation and less over-sharpening than Real-ESRGAN. Has a
               denoise level. A good first thing to try if the default looks
               over-processed.
  waifu2x      clean line art and gentle denoise.

Denoise (used by Real-CUGAN and waifu2x): -1 keeps film grain, 0 none, 1-3
increasing smoothing. Leave at 0 unless the source is noisy.

Detail strength (slider): how much of the AI result to keep over a plain
enlargement of the original. 100% is pure AI (the default). Lower it when a
model over-sharpens or invents detail you do not want; at 0% you get a plain
resize with no AI. This is the same control the Ninja Gaiden II texture pack
uses, so a value here matches that pack at the same setting.

To add an engine by hand instead of downloading, unzip its ncnn-vulkan release
so that the executable lands at:

  upscaler\realcugan\realcugan-ncnn-vulkan.exe
  upscaler\waifu2x\waifu2x-ncnn-vulkan.exe

The app finds the engine and its model sets on the next launch.


Which to choose
---------------

Start with Photo 4x (x4plus) at the scale you want. If it looks over-sharpened
or "painted", try Real-CUGAN, or Soft 4x. For clean 2D art or UI, x4plus-anime
or waifu2x. There is no single best model for every texture; the selector is
there so you can compare on your own art.


Notes
-----

Upscaling runs on the GPU (Vulkan). "Extra quality (slow)" turns on TTA mode,
which is markedly slower for a small quality gain. A 4x pass produces large
files (uncompressed for most formats), so give the output folder room.

The upscaling engines are Real-ESRGAN, Real-CUGAN and waifu2x, all by their
respective authors; this program is a front-end that drives them.
