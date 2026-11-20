# Printing (v1.0)

**Open [`cad/3mf/WBOT_Complete_v1.0.3mf`](../cad/3mf) in Bambu Studio** (Bambu Lab H2S). All 54 parts, correct quantities, no supports.

| Plate | Nozzle | Parts |
|---|---|---|
| 1 | 0.8 mm (0.4 also works) | 23 objects |
| 2 | 0.8 mm (0.4 also works) | 6 objects |
| 3 | **0.4 mm only** | 25 objects: sliding and press-fit parts |

Switch to the 0.4 mm preset ([`cad/bambu-presets/nozzle-0.4`](../cad/bambu-presets)) before slicing plate 3.

| Setting | 0.8 mm nozzle | 0.4 mm nozzle |
|---|---|---|
| Layer | 0.32 mm | 0.20 mm |
| Walls / infill | 3 / 25 % | 3 / 25 % |
| Material | PLA (PETG for park ring, lifter carriage, pen carriages) | same |
| Time / filament | ≈ 7.7 h / 576 g | ≈ 2.7 h / 65 g |

Smooth plate for **park ring** and **lifter carriage** (sliding face on the bed).

## Test print first

Print `19_Passprobe` (or `23_Lochprobe_04`, 23 min) and check rod, nut and bearing fits before the big parts.

| Feature | Drawn | Fits when |
|---|---:|---|
| 2 mm rod, press fit | 2.6 mm | tight, no wobble |
| 2 mm pin lying | 2.2 mm | holds lightly |
| 1.5 mm rod | 2.3 mm | slight play, captive |
| M3 / M2 nut pocket | 5.75 / 4.2 mm | snug |
| Bearing seat | 22.2 mm | no play |

Different printer? Edit the numbers at the top of [`cad/source/gondola_v2_fusion.py`](../cad/source) and re-export.

## After printing

- Ream sliding bores with a 2.1–2.2 mm drill (lifter: 1.6–1.7 mm)
- Deburr and chamfer the sawn rod ends
- Pens thinner than 18 mm need a pen insert (11, 13, 15 or 17 mm); the 17 mm one fits the Lumocolor 351

## Part names

| File | Name | Qty | | File | Name | Qty |
|---|---|---:|---|---|---|---:|
| 01 Grundplatte | base plate | 1 | | 14 Kurbel | crank | 1 |
| 02 Lagerbock | bearing block | 1 | | 15 Riemenarm | belt arm | 1 |
| 03 Hornanker A/B/C | horn anchor (use the snuggest) | 1 | | 16 Riemendeckel | belt cover | 1 |
| 04/06 Trommel | drum lower / upper | 1 | | 17 Gleitfuss | glide foot | 3 |
| 05 Lagerring | bearing ring | 1 | | 18 Servo_Distanz | servo spacer | 4 |
| 07 Stiftschlitten | pen carriage | 3 | | 20 Einstelllehre | pen height gauge | 1 |
| 08 Parkring | park ring | 1 | | 21 Stifteinsatz | pen insert | 3 |
| 09 Stuetze | post | 4 | | 24 Riemenklemme | belt clamp | 2 |
| 10 Hubtraeger | lifter bracket | 1 | | 30c Klemmreiter | clamp rider | 3 |
| 11 Hubschlitten | lifter carriage | 1 | | 30n Motorhalter | motor mount L / R | 1 + 1 |
| 12 Kopfstueck | head piece | 1 | | 33n Umlenkrolle | idler | 2 |
| 13 Pleuel 13.70–14.30 | connecting rods (tuning set) | 1 | | 35n DLC32_Halter | board holder | 1 |
| | | | | 38n Gegengewicht | counterweight body / lid / plate | 2 sets |
