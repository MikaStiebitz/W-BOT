# -*- coding: utf-8 -*-
"""Lochprobe fuer die 0,8-mm-Duese. Laeuft in Fusion im Nur-Lese-Modus: baut einen temporaeren
Koerper, vernetzt ihn und schreibt die STL selbst. Die Fusion-Datei wird nicht veraendert."""
import os
import struct

import adsk.core
import adsk.fusion

import gondel_v2 as g

# (Gruppe, Form, Masse)
ROUND_2 = (2.6, 2.8, 3.0, 3.2, 3.4)      # 2-mm-Stange, runde Loecher
SQUARE_2 = (2.2, 2.5, 2.8, 3.1)          # 2-mm-Stange, quadratische Loecher (Kantenlaenge)
ROUND_15 = (2.1, 2.3, 2.5, 2.7, 2.9)     # 1,5-mm-Stange, runde Loecher
HORIZ = ((33.5, 2.3), (64.5, 2.6), (97.5, 2.9))   # liegende Loecher quer durch den Stab


def build():
    L, W, T = 102.0, 12.0, 6.0
    b = g.box(0, L, 0, W, 0, T)
    # Markierung: abgeschraegte Ecke vorn links
    g.C(b, g.obox(0, 0, T / 2, 6.0, 6.0, T + 2, 45))
    x = 5.0
    for d in ROUND_2:
        g.C(b, g.cyl(x, W / 2, -1, T + 1, d / 2))
        x += 5.5
    x = 40.0
    for a in SQUARE_2:
        g.C(b, g.box(x - a / 2, x + a / 2, W / 2 - a / 2, W / 2 + a / 2, -1, T + 1))
        x += 6.0
    x = 71.0
    for d in ROUND_15:
        g.C(b, g.cyl(x, W / 2, -1, T + 1, d / 2))
        x += 5.0
    for (xh, d) in HORIZ:
        g.C(b, g.cylp((xh, -1, T / 2), (xh, W + 1, T / 2), d / 2))
    return b


# --- Lochprobe fuer die 0,4-mm-Duese (Gleit- und Passteile) ---
ROUND_2_04 = (2.0, 2.1, 2.2, 2.3, 2.4, 2.5)      # 2-mm-Stange, stehende Rundloecher
SLOT_15_04 = (1.6, 1.7, 1.8, 1.9, 2.0)           # 1,5-mm-Stange, Langloch wie im Stiftschlitten (Breite)
M3_04 = ((5.6, 3.2), (5.7, 3.4), (5.8, 3.6))     # M3: (Muttertasche Schluesselweite, Durchgangsloch)
HORIZ_04 = ((34.0, 2.0), (65.5, 2.1), (93.5, 2.2))  # liegende Loecher fuer den 2-mm-Stift


def build04():
    L, W, T = 97.0, 12.0, 6.0
    b = g.box(0, L, 0, W, 0, T)
    g.C(b, g.obox(0, 0, T / 2, 6.0, 6.0, T + 2, 45))
    x = 5.0
    for d in ROUND_2_04:
        g.C(b, g.cyl(x, W / 2, -1, T + 1, d / 2))
        x += 5.0
    x = 39.0
    for w in SLOT_15_04:
        g.C(b, g.box(x - w / 2, x + w / 2, W / 2 - 0.7, W / 2 + 0.7, -1, T + 1))
        for sgn in (-1, 1):
            g.C(b, g.cyl(x, W / 2 + sgn * 0.7, -1, T + 1, w / 2))
        x += 5.5
    x = 71.0
    for (af, d) in M3_04:
        g.C(b, g.cyl(x, W / 2, -1, T + 1, d / 2))
        g.C(b, g.hexz(x, W / 2, T - 2.7, T + 1, af))
        x += 8.5
    for (xh, d) in HORIZ_04:
        g.C(b, g.cylp((xh, -1, T / 2), (xh, W + 1, T / 2), d / 2))
    return b


def write_stl(body, path):
    calc = body.meshManager.createMeshCalculator()
    calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.HighQualityTriangleMesh)
    mesh = calc.calculate()
    co = mesh.nodeCoordinatesAsDouble
    idx = mesh.nodeIndices
    n = len(idx) // 3
    with open(path, 'wb') as f:
        f.write(b'Lochprobe W-BOT Gondel V2'.ljust(80, b' '))
        f.write(struct.pack('<I', n))
        for t in range(n):
            pts = []
            for k in range(3):
                i = idx[3 * t + k]
                pts.append((co[3 * i] * 10.0, co[3 * i + 1] * 10.0, co[3 * i + 2] * 10.0))
            ux, uy, uz = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1], pts[1][2] - pts[0][2])
            vx, vy, vz = (pts[2][0] - pts[0][0], pts[2][1] - pts[0][1], pts[2][2] - pts[0][2])
            nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            ln = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
            f.write(struct.pack('<3f', nx / ln, ny / ln, nz / ln))
            for p in pts:
                f.write(struct.pack('<3f', *p))
            f.write(b'\x00\x00')
    return n


def main(nozzle='08'):
    b = build04() if nozzle == '04' else build()
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, 'STL', '23_Lochprobe_04.stl' if nozzle == '04' else '22_Lochprobe.stl')
    n = write_stl(b, path)
    print('Lochprobe vol %.2f cm3, lumps %d, bbox %s, %d Dreiecke -> %s' % (b.volume, b.lumps.count, [round(v, 1) for v in g.bbox(b)], n, path))
