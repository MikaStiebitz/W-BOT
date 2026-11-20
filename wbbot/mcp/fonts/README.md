# Single-stroke fonts

`hershey_simplex.json` is the engraving font used by the MCP text and equation
tools and by the calibration pattern labels.

It is a **single-stroke** font: each glyph stores the path a pen travels, not
the outline of the letter. Drawing an outline font with a 2 mm marker fills in
the counters and produces a blob; a single-stroke font is simply followed.

Format:

```json
{
  "cap_height": 14.0, "x_height": 9.0, "descender": -5.0,
  "line_height": 22.0, "letter_spacing": 1.5,
  "glyphs": { "A": { "advance": 13.0, "strokes": [[[x, y], ...], ...] } }
}
```

Coordinates are font units: baseline at `y = 0`, cap height at `y = 14`.
Text is scaled so `height_mm` maps the cap height.

`wbbot.core.hershey.default_font()` loads this file when it exists and falls
back to the table built into `hershey.py` otherwise, so the font can be
replaced without touching code. Regenerate it with:

```python
from wbbot.core.hershey import StrokeFont, GLYPHS
StrokeFont(glyphs=GLYPHS).to_json()
```
