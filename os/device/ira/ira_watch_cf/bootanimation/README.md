# IRA CARE OS boot animation (specification only)

No frames and no `bootanimation.zip` exist yet. This folder holds the specification as text;
`desc.txt` is the real control file the zip will carry. Nothing here has been shown on a
device or on Cuttlefish.

## What the person sees

A black round screen. The IRA mark fades in, in the product's coral (`#E2574C`, the Help
button's colour from D-072), over about one second, then a slow pulse of the mark while the
system starts. No text: the first words the person reads are the IRA app's own home screen,
in large letters. The background stays black throughout, so the panel draws little power and
the edge of the round glass is invisible.

## `desc.txt`, line by line

The format is Android's boot animation format (`frameworks/base/cmds/bootanimation/FORMAT.md`
in the AOSP source, Apache 2.0):

| Line | Meaning |
| --- | --- |
| `454 454 30` | frames are 454 x 454 pixels, played at 30 frames per second |
| `c 1 0 part0` | part 0, the fade-in: played once (`1`), no pause after it (`0`), and always completed (`c`) even if boot finishes first, so the mark never cuts off half-drawn |
| `p 0 0 part1` | part 1, the pulse: repeated until boot completes (`0`), no pause; may be interrupted (`p`) |

The file ends with a newline, which the parser expects.

## How the frames are produced

1. Draw the mark once as a vector (SVG), centred, inside a 320-pixel circle, so it stays
   within a round panel's safe area whatever bezel mask the hardware has.
2. Render the frames with any scriptable tool that writes PNG (for example Inkscape's command
   line, or Blender, already used for the hardware model in `scripts/hardware/build_watch.py`):
   - `part0/`: 30 frames (one second), opacity 0 to 1 on an ease-out curve.
   - `part1/`: 60 frames (two seconds), scale 1.00 to 1.04 and back, seamless at the loop.
   - File names zero-padded in play order: `00000.png`, `00001.png` and so on.
   - 8-bit RGB on black; no alpha needed.
3. Package without compression (the platform reads the entries in place):

   ```sh
   cd bootanimation_build        # holds desc.txt, part0/, part1/
   zip -0qry -i \*.txt \*.png @ ../bootanimation.zip desc.txt part0 part1
   ```

4. Check that every entry is stored, not deflated: `unzip -v bootanimation.zip` shows
   `Stored` on every line.
5. Add the zip to the product with the `PRODUCT_COPY_FILES` line that is commented out in
   `../ira_watch_cf.mk` (section 6). Produce the zip on the build host; do not commit it
   unless the owner agrees that generated binary assets may enter the repository.

Memory: 90 frames of 454 x 454 RGB are about 55 MB if all were decoded at once, so keep the
frame count low, and use the format's optional `trim.txt` cropping (also in `FORMAT.md`) once
the real artwork exists.
