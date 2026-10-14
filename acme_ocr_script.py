"""Windows OCR lettering finder (PowerShell 5.1, Windows.Media.Ocr).

Written to a temp file and run by acme_classify.run_ocr. Origin: the Hulk
port (hulkrecomp ports/hulk/res/find_text.ps1, tuned on 1,036 hand-checked
textures) as extended by the Ninja Gaiden II port (-List mode).
"""

SCRIPT = r'''# ACME Texture Upscaler 2000: from the Hulk port's find_text.ps1 via Ninja Gaiden II
# (-List, no 3:1 strip rule); one change: an image is read as stored first
# (game dumps were stored upside down, a folder of images is not), flipped last.
<#
  Finds the dumped textures with lettering in them (signs, billboards, logos)
  for "Make enhanced textures" (hulk_textools.c), which then gives those a
  sharp non-AI resize instead of the AI upscale: the AI model invents shapes
  and turns small letters into smears.

  Uses Windows' own OCR (Windows.Media.Ocr, Windows 10 and later; nothing to
  download). Windows PowerShell 5.1. Each texture is enlarged first (Windows
  OCR does not read letters under ~20 px) and read several ways (see $reads);
  a texture counts as text when the OCR finds a real word in it (Test-Words).

  A texture without such a word may still have lettering the OCR cannot read
  with confidence (styled fonts, letters stacked down a narrow sign, short
  words): "possible text" (Test-Possible), listed in -Possible; the game
  keeps its own image for those rather than let the AI reshape the letters.

  Only the dumps not in the -Checked list are read; each one is appended to
  -Checked as it is done (the game counts those lines for its progress bar),
  to -Out when it has text and to -Possible when it may. A -Checked list
  written before the possible-text tier (no "# v2" first line) is started
  again. Exit code 0 done, 2 OCR unavailable.

    powershell -NoProfile -ExecutionPolicy Bypass -File find_text.ps1
        -Dump <textures\dump> -Out <text_textures.txt> -Checked <text_checked.txt>
        -Possible <possible_text.txt> -Work <scratch folder> [-Raw <words.tsv>]
#>
# NG2 (v1.4.0): copied from Hulk's port (hulkrecomp ports/hulk/res/find_text.ps1)
# with one addition: -List <file of PNG paths> instead of -Dump <folder>; with
# -List each output line is the PNG's FULL path, and an "OCR <i> <n>" line is
# printed per texture for the progress bar.
param(
    [string]$Dump = '',
    [string]$List = '',
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][string]$Checked,
    [Parameter(Mandatory = $true)][string]$Work,
    [string]$Possible = '',
    [string]$Raw = ''
)

$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        (New-Object Windows.Globalization.Language 'en-US'))
    if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if (-not $engine) { throw 'no OCR language installed' }
} catch {
    [Console]::Error.WriteLine("find_text: OCR unavailable: $($_.Exception.Message)")
    exit 2
}

# WinRT IAsyncOperation<T> -> result (PowerShell 5.1 has no await).
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
}

# A texture has text when the OCR reads a word of 4 or more letters/digits
# that looks like lettering, not like the shapes the OCR reads into brick,
# window grids, foliage and noise ("ll", "unun", "nrnu", "iiEt", "|||"):
#  - 3 or more letters/digits with at least 2 letters, and nothing else once
#    the punctuation at either end is dropped ("DOOLEY'S," "=DINGO");
#  - one case: all capitals, all small, or Capitalised (noise mixes them);
#  - not one repeated character ("lll", "000"); not only the thin strokes
#    noise is read as (i l I 1 j t f r); not, in small letters, only the
#    strokes of window bars and railings (n m u r i l j t f o a y);
#  - a letters-only word has a vowel (or y), as written words do.
# Measured on Hulk's 1036 dumps (10-07, every dump looked at by eye): 192 of
# the 261 with lettering found, none of the 775 without it taken. The misses:
# letters stacked down a narrow sign (PARK, HOTEL, BOWL...), heavily styled
# or outlined lettering, and words of 3 letters or fewer (DCC, DVS); those go
# in textures\skip_ai.txt by hand or with F11 in game.
function Test-Word([string]$w) {
    $t = $w -replace '^[^A-Za-z0-9]+', '' -replace '[^A-Za-z0-9]+$', ''
    if ($t.Length -lt 3) { return $false }
    if ($t -cnotmatch '^[A-Za-z0-9&]+$') { return $false }
    $l = $t -creplace '[^A-Za-z]', ''
    if ($l.Length -lt 2) { return $false }
    if (-not ($l -cmatch '^[A-Z]+$' -or $l -cmatch '^[a-z]+$' -or $l -cmatch '^[A-Z][a-z]+$')) { return $false }
    if ($t -cmatch '^[ilI1jtfr|!]+$') { return $false }
    if ($l -cmatch '^[a-z]+$' -and $t -cmatch '^[nmuril1jtfoay]+$') { return $false }
    if (($t.ToCharArray() | Select-Object -Unique).Count -lt 2) { return $false }
    if ($t -cmatch '^[A-Za-z]+$' -and $t -notmatch '[aeiouy]') { return $false }
    return $true
}
function Test-Words($words) {
    foreach ($w in $words) {
        if ((Test-Word $w) -and ($w -creplace '[^A-Za-z0-9]', '').Length -ge 4) { return $true }
    }
    return $false
}

# Possible text: the dumps with no confident word but some sign of lettering.
# Measured on the same 1036 dumps (10-07): of the 69 lettered dumps the
# confident test misses, these take 60; they also take 53 of the 775 without
# lettering (mostly window and door grilles, which the OCR reads as letters,
# and narrow strips - light trails, magenta masks; plus 4 Chinese neon signs
# not counted as lettering). Signs:
#  - a narrow strip, 3 or more times as long as it is wide: nearly every one
#    in Hulk is a shop sign (catches the stacked PARK / HOTEL / BOWL strips
#    and the styled 128 x 32 signs with no read at all);
#  - any read (the five below, and for a texture without a confident word
#    three more: 256 and 768 px in colour, inverted grey at 512; for a tall
#    strip also turned 90 degrees both ways and cut into letter-sized tiles
#    laid side by side) with a token of 3 or more letters/digits ("DCC",
#    "DVS", "Divi$ion", "ORENT/MPORTS").
# A glyph-stroke count (letter-sized high-contrast blobs) did not separate
# the 9 still missed (Burgers & Shakes, a CD cover, the DIESEL logo...) from
# brick and windows, so it is not used; those go in skip_ai.txt or F11.
function Test-Possible($words) {
    foreach ($w in $words) {
        if (($w -creplace '[^A-Za-z0-9]', '').Length -ge 3) { return $true }
    }
    return $false
}

New-Item -ItemType Directory -Force -Path $Work | Out-Null
$checkedSet = @{}
if (Test-Path -LiteralPath $Checked) {
    $lines = @([IO.File]::ReadAllLines($Checked))
    if ($Possible -and ($lines.Count -eq 0 -or $lines[0] -ne '# v2')) {
        $lines = @()   # read before the possible-text tier: read again
        [IO.File]::WriteAllText($Checked, "# v2`r`n")
    }
    foreach ($line in $lines) { $n = $line.Trim(); if ($n -and -not $n.StartsWith('#')) { $checkedSet[$n] = 1 } }
} elseif ($Possible) {
    [IO.File]::WriteAllText($Checked, "# v2`r`n")
}
$outSet = @{}
foreach ($markList in $Out, $Possible) {
    if ($markList -and (Test-Path -LiteralPath $markList)) {
        foreach ($line in [IO.File]::ReadAllLines($markList)) { $n = $line.Trim(); if ($n) { $outSet["$markList|$n"] = 1 } }
    }
}
if ($List) {
    $todo = @([IO.File]::ReadAllLines($List) | Where-Object { $_.Trim() } | ForEach-Object { Get-Item -LiteralPath $_.Trim() } |
              Where-Object { -not $checkedSet.ContainsKey($_.FullName) })
} else {
    $todo = @(Get-ChildItem -LiteralPath $Dump -Filter '*.png' -File | Where-Object { -not $checkedSet.ContainsKey($_.BaseName) })
}
$index = 0
$temp = Join-Path $Work 'ocr_input.png'
$found = 0

# The texture enlarged `scale` times (bicubic) on a plain background with a
# margin, optionally in grey, read by the OCR; returns the words.
$grey = New-Object System.Drawing.Imaging.ColorMatrix(, [single[][]]@(
    @(0.3, 0.3, 0.3, 0, 0), @(0.59, 0.59, 0.59, 0, 0), @(0.11, 0.11, 0.11, 0, 0), @(0, 0, 0, 1, 0), @(0, 0, 0, 0, 1)))
$inverse = New-Object System.Drawing.Imaging.ColorMatrix(, [single[][]]@(
    @(-0.3, -0.3, -0.3, 0, 0), @(-0.59, -0.59, -0.59, 0, 0), @(-0.11, -0.11, -0.11, 0, 0), @(0, 0, 0, 1, 0), @(1, 1, 1, 0, 1)))
function Read-Words([System.Drawing.Bitmap]$src, [System.Drawing.Color]$back, [int]$scale, [bool]$gray,
                    [bool]$invert = $false) {
    $pad = 16
    $w = $src.Width * $scale; $h = $src.Height * $scale
    $big = New-Object System.Drawing.Bitmap(($w + 2 * $pad), ($h + 2 * $pad), [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $g = [System.Drawing.Graphics]::FromImage($big)
    $g.Clear($back)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
    $attr = New-Object System.Drawing.Imaging.ImageAttributes
    $attr.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)   # no dark border from the edge
    if ($invert) { $attr.SetColorMatrix($inverse) } elseif ($gray) { $attr.SetColorMatrix($grey) }
    $g.DrawImage($src, (New-Object System.Drawing.Rectangle($pad, $pad, $w, $h)), 0, 0, $src.Width, $src.Height,
                 [System.Drawing.GraphicsUnit]::Pixel, $attr)
    $g.Dispose()
    $big.Save($temp, [System.Drawing.Imaging.ImageFormat]::Png)
    $big.Dispose()
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($temp)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $bitmap.Dispose()
    } finally {
        $stream.Dispose()
    }
    $words = @()
    foreach ($line in $result.Lines) { foreach ($word in $line.Words) { $words += $word.Text } }
    return , $words
}

# The reads tried, in order, until one finds a word: upright (the dumps hold
# most textures upside down - the game's texture coordinates run bottom-up)
# with the longer side at ~384 and ~512 px, in colour and in grey, then as
# stored. Chosen on Hulk's dumps from 12 combinations (sizes 384/512/640,
# colour/grey, both ways up): these five find all but 8 of what the twelve
# find together, with no false finds; larger enlargements read worse (the
# OCR misses letters over ~100 px). A texture with see-through parts is read
# over black and, for the first read, over white too. ~80 s for 1036 dumps.
$reads = @(
    @{ Up = $true; Side = 384; Gray = $false }, @{ Up = $true; Side = 384; Gray = $true },
    @{ Up = $true; Side = 512; Gray = $false }, @{ Up = $true; Side = 512; Gray = $true },
    @{ Up = $false; Side = 512; Gray = $true })

function Get-Scale($bmp, [int]$target) {
    return [int][Math]::Max(1, [Math]::Min(16, [Math]::Round($target / [Math]::Max($bmp.Width, $bmp.Height))))
}
# The extra reads for possible text (upright $src): s256, s768, inverse;
# for a tall strip also r90, r270 (turned) and tile1, tile075 (cut into
# tiles as high as the strip is wide, or 3/4 of that, laid side by side, so
# stacked letters become a line).
function Read-Extra([System.Drawing.Bitmap]$src, [string]$kind) {
    $black = [System.Drawing.Color]::Black
    switch ($kind) {
        's256' { return Read-Words $src $black (Get-Scale $src 256) $false }
        's768' { return Read-Words $src $black (Get-Scale $src 768) $false }
        'inverse' { return Read-Words $src ([System.Drawing.Color]::White) (Get-Scale $src 512) $false $true }
    }
    if ($kind -eq 'r90' -or $kind -eq 'r270') {
        $c = $src.Clone()
        $c.RotateFlip($(if ($kind -eq 'r90') { [System.Drawing.RotateFlipType]::Rotate90FlipNone } else { [System.Drawing.RotateFlipType]::Rotate270FlipNone }))
    } else {
        $th = [int]($src.Width * $(if ($kind -eq 'tile1') { 1.0 } else { 0.75 }))
        $n = [int][Math]::Ceiling($src.Height / $th)
        $c = New-Object System.Drawing.Bitmap(($src.Width * $n), $th, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = [System.Drawing.Graphics]::FromImage($c)
        for ($i = 0; $i -lt $n; ++$i) {
            $g.DrawImage($src, (New-Object System.Drawing.Rectangle(($i * $src.Width), 0, $src.Width, $th)), 0, ($i * $th),
                         $src.Width, $th, [System.Drawing.GraphicsUnit]::Pixel)
        }
        $g.Dispose()
    }
    try { return Read-Words $c $black (Get-Scale $c 512) $false } finally { $c.Dispose() }
}

$checkedWriter = New-Object IO.StreamWriter($Checked, $true)
$outWriter = New-Object IO.StreamWriter($Out, $true)
$possibleWriter = if ($Possible) { New-Object IO.StreamWriter($Possible, $true) } else { $null }
$maybe = 0
$rawWriter = if ($Raw) { New-Object IO.StreamWriter($Raw, $true) } else { $null }
try {
    foreach ($f in $todo) {
        ++$index
        if ($List) { [Console]::Out.WriteLine("OCR $index $($todo.Count) $($f.Name)") }
        $key = if ($List) { $f.FullName } else { $f.BaseName }
        $hit = $false
        $maybeText = $false
        $all = @()
        try {
            $src = [System.Drawing.Bitmap]::FromFile($f.FullName)
            try {
                $side = [Math]::Max($src.Width, $src.Height)
                $translucent = $false
                if ([System.Drawing.Image]::IsAlphaPixelFormat($src.PixelFormat)) {
                    $step = [Math]::Max(1, [int]($src.Width * $src.Height / 4096))
                    for ($i = 0; $i -lt $src.Width * $src.Height -and -not $translucent; $i += $step) {
                        if ($src.GetPixel($i % $src.Width, [int][Math]::Floor($i / $src.Width)).A -lt 128) { $translucent = $true }
                    }
                }
                $upright = $true   # ACME: images are stored upright (game dumps were not)
                $first = $true
                foreach ($read in $reads) {
                    if ($read.Up -ne $upright) {
                        $src.RotateFlip([System.Drawing.RotateFlipType]::RotateNoneFlipY)
                        $upright = $read.Up
                    }
                    $scale = [int][Math]::Max(1, [Math]::Min(16, [Math]::Round($read.Side / $side)))
                    $backs = if ($translucent -and $first) { @([System.Drawing.Color]::Black, [System.Drawing.Color]::White) } else { @([System.Drawing.Color]::Black) }
                    $first = $false
                    foreach ($back in $backs) {
                        $words = Read-Words $src $back $scale $read.Gray
                        $all += $words
                        $all += '/'
                        if (Test-Words $words) { $hit = $true; break }
                    }
                    if ($hit) { break }
                }
                if (-not $hit -and $possibleWriter) {
                    $long = [Math]::Max($src.Width, $src.Height)
                    $short = [Math]::Min($src.Width, $src.Height)
                    # NG2 (2026-10-12): NOT the 3:1 strip rule - on this game's contact sheet the 424
                    # strips it alone took were walls, wood, pillars, blades and hair (one lettered strip).
                    $maybeText = Test-Possible $all
                    if (-not $upright) { $src.RotateFlip([System.Drawing.RotateFlipType]::RotateNoneFlipY) }
                    $kinds = @('s256', 's768', 'inverse')
                    if ($src.Height -ge 2 * $src.Width) { $kinds += 'r90', 'r270', 'tile1', 'tile075' }
                    foreach ($kind in $kinds) {
                        if ($maybeText) { break }
                        $words = Read-Extra $src $kind
                        $all += $words
                        $all += '/'
                        $maybeText = Test-Possible $words
                    }
                }
            } finally {
                $src.Dispose()
            }
        } catch {
            [Console]::Error.WriteLine("find_text: $($f.Name): $($_.Exception.Message)")
        }
        if ($hit) {
            if (-not $outSet.ContainsKey("$Out|$key")) { $outWriter.WriteLine($key); $outWriter.Flush() }
            ++$found
        } elseif ($maybeText -and $possibleWriter) {
            if (-not $outSet.ContainsKey("$Possible|$key")) { $possibleWriter.WriteLine($key); $possibleWriter.Flush() }
            ++$maybe
        }
        $tier = if ($hit) { 1 } elseif ($maybeText) { 2 } else { 0 }
        if ($rawWriter) { $rawWriter.WriteLine("$key`t$tier`t$($all -join ' ')"); $rawWriter.Flush() }
        $checkedWriter.WriteLine($key)
        $checkedWriter.Flush()
    }
} finally {
    $checkedWriter.Dispose()
    $outWriter.Dispose()
    if ($possibleWriter) { $possibleWriter.Dispose() }
    if ($rawWriter) { $rawWriter.Dispose() }
    Remove-Item -LiteralPath $temp -ErrorAction SilentlyContinue
}
[Console]::Out.WriteLine("find_text: $($todo.Count) read, $found with text, $maybe possible text")
exit 0
'''
