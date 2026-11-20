"""Testbild-Bibliothek des Mock-Servers (Striche in Board-mm, Format wie im API-Vertrag)."""
from __future__ import annotations

import math


def P(id, label, kind, default, min=None, max=None, choices=None, unit=None):
    return {"id": id, "label": label, "kind": kind, "default": default,
            "min": min, "max": max, "choices": choices, "unit": unit}


COLORS = ["schwarz", "rot", "blau"]

PATTERNS = [
    {"id": "grid", "name": "Kalibrierraster",
     "description": "Gitter mit Kreuzmarken. Prüft Maßhaltigkeit und Rechtwinkligkeit.",
     "params": [P("cols", "Spalten", "int", 6, 1, 14), P("rows", "Zeilen", "int", 4, 1, 9),
                P("cell", "Zellgröße", "float", 100.0, 20, 250, unit="mm"),
                P("crosses", "Kreuzmarken", "bool", True),
                P("color", "Farbe", "choice", "schwarz", choices=COLORS)]},
    {"id": "circles", "name": "Konzentrische Kreise",
     "description": "Kreise um die Brettmitte. Prüft Rundheit (Verzerrung in X/Y).",
     "params": [P("count", "Anzahl", "int", 5, 1, 10), P("r_max", "Größter Radius", "float", 200.0, 20, 480, unit="mm"),
                P("segments", "Segmente", "int", 120, 24, 360), P("color", "Farbe", "choice", "schwarz", choices=COLORS)]},
    {"id": "corners", "name": "Eckmarken",
     "description": "Marken nahe den vier Brettecken. Prüft Genauigkeit am Rand.",
     "params": [P("size", "Markengröße", "float", 30.0, 5, 100, unit="mm"),
                P("inset", "Abstand zum Rand", "float", 30.0, 10, 200, unit="mm"),
                P("shape", "Form", "choice", "Kreuz", choices=["Kreuz", "Quadrat", "Kreis"])]},
    {"id": "spiral", "name": "Spirale",
     "description": "Archimedische Spirale. Prüft gleichmäßige Fahrt und Kurvenglättung.",
     "params": [P("turns", "Windungen", "float", 6.0, 1, 20), P("r_max", "Außenradius", "float", 250.0, 50, 480, unit="mm"),
                P("color", "Farbe", "choice", "rot", choices=COLORS)]},
    {"id": "colors", "name": "Farbtest",
     "description": "Parallele Linien in allen drei Farben. Prüft den Revolver.",
     "params": [P("length", "Linienlänge", "float", 150.0, 50, 400, unit="mm"),
                P("lines", "Linien je Farbe", "int", 3, 1, 10), P("gap", "Linienabstand", "float", 8.0, 3, 40, unit="mm")]},
]


def _circle(cx, cy, r, n):
    return [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)] for i in range(n + 1)]


def _stroke(pts, color):
    return {"color": color, "points": [[round(x, 2), round(y, 2)] for x, y in pts]}


def resolve(pattern_id, given):
    pat = next((p for p in PATTERNS if p["id"] == pattern_id), None)
    if pat is None:
        return None, None
    vals = {}
    for p in pat["params"]:
        v = (given or {}).get(p["id"], p["default"])
        if p["kind"] in ("float", "int"):
            v = float(v)
            if p["min"] is not None:
                v = max(p["min"], v)
            if p["max"] is not None:
                v = min(p["max"], v)
            if p["kind"] == "int":
                v = int(round(v))
        elif p["kind"] == "bool":
            v = bool(v)
        elif p["kind"] == "choice" and v not in p["choices"]:
            v = p["default"]
        vals[p["id"]] = v
    return pat, vals


def build(pattern_id, given):
    pat, v = resolve(pattern_id, given)
    if pat is None:
        return None
    st = []
    cx, cy = 750.0, 500.0
    if pattern_id == "grid":
        c = COLORS.index(v["color"])
        w, h = v["cols"] * v["cell"], v["rows"] * v["cell"]
        x0, y0 = cx - w / 2, cy - h / 2
        for i in range(v["cols"] + 1):
            st.append(_stroke([[x0 + i * v["cell"], y0], [x0 + i * v["cell"], y0 + h]], c))
        for j in range(v["rows"] + 1):
            st.append(_stroke([[x0, y0 + j * v["cell"]], [x0 + w, y0 + j * v["cell"]]], c))
        if v["crosses"]:
            for i in range(v["cols"] + 1):
                for j in range(v["rows"] + 1):
                    x, y = x0 + i * v["cell"], y0 + j * v["cell"]
                    st.append(_stroke([[x - 4, y], [x + 4, y]], c))
                    st.append(_stroke([[x, y - 4], [x, y + 4]], c))
    elif pattern_id == "circles":
        c = COLORS.index(v["color"])
        for k in range(1, v["count"] + 1):
            st.append(_stroke(_circle(cx, cy, v["r_max"] * k / v["count"], v["segments"]), c))
    elif pattern_id == "corners":
        s, ins = v["size"], v["inset"]
        for (x, y) in [(ins, ins), (1500 - ins, ins), (1500 - ins, 1000 - ins), (ins, 1000 - ins)]:
            if v["shape"] == "Kreuz":
                st.append(_stroke([[x - s / 2, y], [x + s / 2, y]], 0))
                st.append(_stroke([[x, y - s / 2], [x, y + s / 2]], 0))
            elif v["shape"] == "Quadrat":
                st.append(_stroke([[x - s / 2, y - s / 2], [x + s / 2, y - s / 2], [x + s / 2, y + s / 2],
                                   [x - s / 2, y + s / 2], [x - s / 2, y - s / 2]], 0))
            else:
                st.append(_stroke(_circle(x, y, s / 2, 48), 0))
    elif pattern_id == "spiral":
        c = COLORS.index(v["color"])
        n = int(v["turns"] * 90)
        st.append(_stroke([[cx + v["r_max"] * (i / n) * math.cos(2 * math.pi * v["turns"] * i / n),
                            cy + v["r_max"] * (i / n) * math.sin(2 * math.pi * v["turns"] * i / n)]
                           for i in range(n + 1)], c))
    elif pattern_id == "colors":
        for c in range(3):
            for k in range(v["lines"]):
                y = cy + (c * v["lines"] + k - (3 * v["lines"]) / 2) * v["gap"]
                st.append(_stroke([[cx - v["length"] / 2, y], [cx + v["length"] / 2, y]], c))
    return {"name": pat["name"], "strokes": st}


def _all_pts(drawing):
    return [p for s in drawing["strokes"] for p in s["points"]]


def apply_layout(drawing, layout, safe):
    layout = layout or {}
    pts = _all_pts(drawing)
    if not pts:
        return drawing
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    scale = float(layout.get("scale") or 1.0)
    rot = math.radians(float(layout.get("rotate") or 0.0))
    dx, dy = float(layout.get("dx") or 0.0), float(layout.get("dy") or 0.0)
    tcx, tcy = cx, cy
    if layout.get("fit"):  # fit -> scale -> rotate -> offset
        w, h = max(max(xs) - min(xs), 1e-6), max(max(ys) - min(ys), 1e-6)
        scale *= min((safe[2] - safe[0]) / w, (safe[3] - safe[1]) / h)
        tcx, tcy = (safe[0] + safe[2]) / 2, (safe[1] + safe[3]) / 2
    out = []
    cr, sr = math.cos(rot), math.sin(rot)
    for s in drawing["strokes"]:
        np_ = []
        for x, y in s["points"]:
            x, y = (x - cx) * scale, (y - cy) * scale
            np_.append([round(tcx + x * cr - y * sr + dx, 2), round(tcy + x * sr + y * cr + dy, 2)])
        out.append({"color": s["color"], "points": np_})
    return {"name": drawing.get("name", "Zeichnung"), "strokes": out}


def analyse(drawing, safe, draw_feed=1500.0, travel_feed=3000.0):
    pts = _all_pts(drawing)
    if not pts:
        return {"bounds": [0, 0, 0, 0], "estimated_s": 0.0, "warnings": ["Zeichnung ist leer."],
                "stats": {"strokes": 0, "draw_mm": 0.0, "travel_mm": 0.0, "color_changes": 0, "lines": 0}}
    draw = travel = 0.0
    changes = 0
    last = None
    last_c = None
    for s in drawing["strokes"]:
        p = s["points"]
        if last is not None:
            travel += math.dist(last, p[0])
        if last_c is not None and s["color"] != last_c:
            changes += 1
        last_c = s["color"]
        draw += sum(math.dist(p[i], p[i + 1]) for i in range(len(p) - 1))
        last = p[-1]
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    warnings = []
    if bounds[0] < safe[0] or bounds[1] < safe[1] or bounds[2] > safe[2] or bounds[3] > safe[3]:
        warnings.append("Die Zeichnung ragt über den erlaubten Bereich hinaus und wird beim Zeichnen begrenzt.")
    n = len(drawing["strokes"])
    est = draw / (draw_feed / 60) + travel / (travel_feed / 60) + n * 0.8 + changes * 2.4
    return {"bounds": [round(b, 2) for b in bounds], "estimated_s": round(est, 1), "warnings": warnings,
            "stats": {"strokes": n, "draw_mm": round(draw, 1), "travel_mm": round(travel, 1),
                      "color_changes": changes, "lines": len(pts) + 4 * n}}
