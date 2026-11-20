# CAD

| Path | Content |
|---|---|
| `fusion/V-Shape-Plotter.f3d` | Autodesk Fusion archive of the whole machine: board and frame reference, gondola V2 (`50_Gondel_V2`), print-position parts, motor mounts, idlers, counterweights, reference models of motors, servos, bearing and pen. This is the current state of the design; earlier versions are not exported |
| `stl/gondola/` | Gondola V2 parts in print orientation, plus fit tests and pen inserts |
| `stl/frame/` | Motor mounts, idlers, DLC32 holder, counterweights, clamp riders |
| `3mf/` | Bambu Studio projects for the Bambu Lab H2S: **`WBOT_Complete_v1.0.3mf`** (every part, 3 plates; start here), `WBOT_Komplett_Duese08` (0.8 mm nozzle, 2 plates), `WBOT_Komplett_Duese04` (0.4 mm nozzle, 1 plate), and the fit tests |
| `bambu-presets/` | Resolved machine, process, and filament presets (0.8 mm and 0.4 mm) that the projects use |
| `source/gondola_v2_fusion.py` | Parametric generator of all gondola parts. Runs **inside Fusion** (Scripts and Add-Ins), builds the solids, checks collisions, exports the STL files. Fits and dimensions are constants at the top |
| `source/lochprobe_fusion.py` | Generator of the hole fit test |

File names are German; see the translation table in [../docs/printing.md](../docs/printing.md). Units are millimetres. World frame of the gondola: pen tip at the origin, z pointing away from the board, y up along the board.

The STL files are exports; if you change a dimension, change it in `source/gondola_v2_fusion.py` (or in Fusion) and re-export rather than editing meshes.
