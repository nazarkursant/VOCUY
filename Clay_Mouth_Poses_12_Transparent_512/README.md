# Clay mouths: 12-pose lip-sync set

12 transparent 512 × 512 PNGs of hand-sculpted cartoon clay mouths, chosen
from the 20-pose clay set for consistent talking animation: thick coral-red
plasticine lips, cream teeth, a pink tongue and burgundy interiors. Each pose
has one job, the set covers every mouth shape VOCUY uses, and all poses share
one upper-lip line, so only the jaw and lips move between frames.

`mouth_map.csv` tells VOCUY which pose shows each sound. Edit it in Excel,
Numbers or Google Sheets. `clay_reference_sheet.png` is a contact sheet with the
lip line drawn in; VOCUY ignores it.

## Why these 12

Color is consistent across the clay set, so the choice was made on shape. These
source poses were left out:

| Source | Reason |
| --- | --- |
| 06 wide grin | Duplicates 10 (both rows of teeth), only wider |
| 08 puckered U | Duplicates 09 at a smaller size |
| 11 tongue low, 13 tongue forward | 12 covers L and TH; one tongue pose reads best |
| 16 closed soft press | Indistinguishable from 01 at viewing size |
| 17 pout, 18 grimace, 20 closed smile | Expressions, not speech shapes; read as mood changes |

## Shared image settings

- Canvas: 512 × 512 pixels, RGBA PNG with a transparent exterior.
- **Upper-lip line:** the top of the upper lip at the mouth's center column is at
  **y = 56** in every pose. Clay bumps along the lip reach up to 15 px above it.
  The source set was centered on each mouth's bounding box, which made the upper
  lip jump by up to 126 px between closed and wide open. Here the lip line stays
  put and open mouths extend downward, as a jaw does.
- The line sits high because pose 05 (wide AH) is 437 px tall; its lowest pixel
  is y = 491, keeping 21 px of margin.
- Horizontal center: x = 256.
- Pixels were moved by whole pixels only; no resampling, scaling or recoloring.
  The clay set's shared 1.549296× scale and relative sizes are preserved.

## Animation setup

1. In VOCUY, choose this folder as a voice's mouth folder in **Lip sync · After
   Effects**, then run the `_lipsync.jsx` script in After Effects.
2. Position the voice's `Mouth control` null; the mouth is parented to it. Put
   the lip line just under the character's nose. The mouth precomp's anchor is
   its center (256, 256), 200 px below the lip line.
3. The null's **Mouth pose** slider value is the pose number below. Edit its
   hold keyframes to retime or swap any pose.
4. Check the shapes against the actual recording. English words use dictionary
   pronunciations; names and other words are read from the spelling.

## Pose index

| ID | File | Clay source | Mouth shapes (`mouth_map.csv`) |
| --- | --- | --- | --- |
| 01 | `clay_mouth_01_rest_closed.png` | 01 | rest, mbp |
| 02 | `clay_mouth_02_slightly_open.png` | 02 | tdn, eh, uh (in-between) |
| 03 | `clay_mouth_03_ah_shallow.png` | 19 | uh, ah, tdn |
| 04 | `clay_mouth_04_ah_medium.png` | 03 | ah, wide |
| 05 | `clay_mouth_05_ah_wide.png` | 04 | wide |
| 06 | `clay_mouth_06_ee_smile_teeth.png` | 05 | ee, sz |
| 07 | `clay_mouth_07_eh_teeth_apart.png` | 15 | eh, tdn |
| 08 | `clay_mouth_08_sz_teeth_narrow.png` | 10 | sz, ch, ee |
| 09 | `clay_mouth_09_oh_round.png` | 07 | oh |
| 10 | `clay_mouth_10_oo_round_narrow.png` | 09 | oo, r, ch |
| 11 | `clay_mouth_11_fv_lip_tuck.png` | 14 | fv |
| 12 | `clay_mouth_12_l_th_tongue_up.png` | 12 | l, th |

Poses run from closed to open (01–05), then spread (06–08), rounded (09–10) and
special consonants (11–12). Neighbors in a sentence are usually one or two steps
apart, so transitions read smoothly.

## 01 — Rest, closed

- **Bounds:** 397 × 209 px, y 49–257.
- **Visible pose:** Thick lips pressed together in a relaxed seal. No teeth.
- **Use:** Silence and pauses of 0.15 s or more, and M / B / P. Rest and M/B/P
  share this image, so the mouth stays closed when a line starts or ends on them.

## 02 — Slightly open

- **Bounds:** 382 × 236 px, y 50–285.
- **Visible pose:** Lips parted around a narrow burgundy slit. No teeth visible.
- **Use:** The main in-between: T / D / N / K / G / H, unstressed vowels, and a
  softer variant of EH and UH. It bridges closed (01) and open (03–05).

## 03 — Shallow AH

- **Bounds:** 391 × 299 px, y 49–347.
- **Visible pose:** Broad oval opening with the two upper front teeth.
- **Use:** UH as in *but*, a lighter AH, and open consonant transitions.

## 04 — Medium AH

- **Bounds:** 373 × 336 px, y 41–376.
- **Visible pose:** Taller oval; upper front teeth and a deep burgundy interior.
- **Use:** The standard A / AH vowel; second choice for stressed vowels.

## 05 — Wide AH

- **Bounds:** 378 × 437 px, y 55–491.
- **Visible pose:** The tallest opening, with the jaw fully dropped.
- **Use:** An A held 0.2 s or longer, emphasis, surprise or a laugh. It is the
  biggest jump from rest, so VOCUY uses it only for held vowels.

## 06 — EE smile with teeth

- **Bounds:** 421 × 256 px, y 44–299.
- **Visible pose:** Wide upturned smile showing a row of upper teeth.
- **Use:** EE / EA / IE / Y, and a wider variant of S / Z.

## 07 — EH, teeth apart

- **Bounds:** 416 × 325 px, y 48–372.
- **Visible pose:** Open oval with upper and lower teeth separated by a dark gap.
- **Use:** E and short I, AI / AY, and teeth-visible consonant transitions.

## 08 — S / Z, narrow teeth

- **Bounds:** 413 × 281 px, y 47–327.
- **Visible pose:** Both rows of teeth close together inside a horizontal opening.
- **Use:** S / Z / X / soft C, CH / SH / J, and a narrower variant of EE.

## 09 — O, round

- **Bounds:** 328 × 367 px, y 50–416.
- **Visible pose:** Tall round ring of clay around a dark center. No teeth.
- **Use:** O / OH, OW / OA, AW / AU, OI.

## 10 — OO, round narrow

- **Bounds:** 325 × 269 px, y 51–319.
- **Visible pose:** Rounded, pushed-forward lips around a small horizontal opening.
- **Use:** OO / OU / EW, U as in *you*, W / WH / QU, R and ER (*work*, *her*), and a rounded
  variant of CH / SH. R is mapped only to this pose, so the mouth stays rounded
  through "wor-" instead of flickering.

## 11 — F / V, lip tuck

- **Bounds:** 398 × 209 px, y 49–257.
- **Visible pose:** Upper front teeth rest on the lower lip.
- **Use:** F / V / PH.

## 12 — L / TH, tongue up

- **Bounds:** 385 × 345 px, y 48–392.
- **Visible pose:** Open mouth with a pink tongue raised beneath the upper teeth.
- **Use:** L and TH. The tongue is distinctive, so a short flash reads clearly.

## Known limitations

- The poses were sculpted separately. The lip line is aligned, but the clay
  bumps and mouth corners do not match between poses, so the surface texture
  changes on every switch. This suits the stop-motion look.
- There is no mood variation. For a smiling or serious delivery, add a second
  set in the same layout and point a separate `mouth_map.csv` at it.
