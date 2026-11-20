# -*- coding: utf-8 -*-
"""W-BOT Gondel V2 - parametrischer Generator fuer Fusion 360.

Laeuft IN Fusion (ueber den Fusion-MCP oder als Skript). Baut alle Teile als
Volumenkoerper, prueft Kollisionen in allen Stellungen und exportiert STLs in
Drucklage. Alle Masse in mm. Welt: Stiftspitze (aktiv) bei (0,0,0), z zeigt von
der Tafel weg, y an der Tafel nach oben. Trommelachse bei (0, AY).
"""
import math
import os

import adsk.core
import adsk.fusion

MM = 0.1
app = adsk.core.Application.get()
tm = adsk.fusion.TemporaryBRepManager.get()
BT = adsk.fusion.BooleanTypes

# ----------------------------------------------------------------------------
# Parameter
# ----------------------------------------------------------------------------
AY = 34.0            # Trommelachse ueber dem Stift
RP = 34.0            # Teilkreis der Stifte
SLOTS = (-60.0, 0.0, 60.0)
DRUM_LIMIT = 66.0    # Software-Fenster Revolver
STOP_A = 134.0       # Anschlagschrauben auf dem Parkring (Winkel ab Station)

# Passungen. Seit 2026-11-20 wird auf zwei Duesen gedruckt: grosse, unkritische Teile mit 0,8 mm,
# alle Gleit- und Passteile mit 0,4 mm (siehe NOZZLE_04 und set_fits).
# 0,8 mm (the author's Bambu H2S, aus Passprobe und Lochprobe): kleine stehende Loecher schrumpfen um
# rund 0,6 mm, liegende um rund 0,3 mm, grosse Konturen kaum. Hier nur Festsitze und Durchgangsloecher.
FITS = {
    '08': dict(
        rod2_slide=2.7,      # in 0,8-Teilen nicht mehr verwendet (Gleitsitz klemmt trotz 2,7)
        rod2_press=2.6,      # 2-mm-Stange sitzt stramm, stehendes Loch (getestet)
        pin2_press_v=2.55, pin2_press_h=2.2, pin2_turn=2.75,
        rod15_slide=1.95,
        rod15_press=2.3,     # 1,5-mm-Stange: 2,1 geht nicht, 2,3 geht mit etwas Spiel (getestet)
        m3=3.8, m3_head=6.4, m3_headh=3.2,
        m3_af=5.75,          # Muttertasche stramm (getestet)
        m3_nh=2.7, m3_slot=6.1, m3_sh=3.0,   # Einschubschlitz: breiter und hoeher als die Tasche
        m2=2.8, m2_af=4.2, m2_nh=2.3, m2_slot=4.4,
        m4=4.8, m4_af=7.3, m4_nh=3.5,
        m5=5.8, m5_af=8.3, m5_nh=4.3,
        brg_od=22.2, stub=7.9, pen_bore=19.0, pen_hole=20.0,
    ),
    # 0,4 mm: vorlaeufig, bis die Lochprobe mit der 0,4-mm-Duese zurueck ist.
    '04': dict(
        rod2_slide=2.3, rod2_press=2.05,
        pin2_press_v=2.05, pin2_press_h=2.0, pin2_turn=2.25,
        rod15_slide=1.75, rod15_press=1.6,
        m3=3.4, m3_head=6.2, m3_headh=3.2, m3_af=5.7, m3_nh=2.7, m3_slot=5.8, m3_sh=2.8,
        m2=2.3, m2_af=4.15, m2_nh=2.3, m2_slot=4.25,
        m4=4.4, m4_af=7.2, m4_nh=3.5,
        m5=5.4, m5_af=8.2, m5_nh=4.3,
        brg_od=22.2, stub=7.9, pen_bore=19.0, pen_hole=20.0,
    ),
}
F = dict(FITS['08'])
# Teile (Namensanfang), die mit der 0,4-mm-Duese gedruckt werden
NOZZLE_04 = ('03_', '07_', '11_', '12_', '13_', '14_', '18_', '21_', '24_')
MIXED_04 = ('03_Hornanker', '07_Stiftschlitten', '11_Hubschlitten', '12_Kopfstueck', '13_Pleuel', '14_Kurbel', '24_Riemenklemme')


def set_fits(nozzle):
    """Schaltet die Passungen um ('08' oder '04'). Danach build() neu aufrufen."""
    F.clear()
    F.update(FITS[nozzle])


def nozzle_of(name):
    return '04' if name.startswith(NOZZLE_04) else '08'


# z-Ebenen
Z_B0, Z_B1 = 5.0, 11.0          # Grundplatte
Z_FL = 17.0                      # Lagerbock-Flansch oben
Z_SH = 44.0                      # Schulter unten
Z_BRG0, Z_BRG1 = 45.0, 52.0      # 608ZZ
Z_TNG = 55.0                     # Zunge oben
Z_D1B, Z_D1T = 48.0, 54.0        # Trommel unten (Scheibe)
Z_D2B, Z_D2T = 97.0, 103.0       # Trommel oben
Z_HORN_FLOOR = 63.0              # Nutgrund im Hornanker (Horn-Aussenseite liegt 0,3 darueber)
Z_HA_T = 65.1                    # Hornanker oben
CAR_H = 32.0                     # Schlittenhoehe
PEN_STROKE = 5.0                 # Stifthub
PEN_OVER = 1.5                   # Weg bis Festanschlag unter "Stift unten"
Z_CAR_DN = Z_D1T + PEN_OVER      # Schlittenunterkante, Stift unten
Z_CAR_UP = Z_CAR_DN + PEN_STROKE
PIN_Z = 4.5                      # Mitnehmerstift ueber Schlittenunterkante
Z_SHELF = Z_CAR_UP + PIN_Z - 1.0  # Parkring-Oberseite (=64.0)
RING_T = 6.0
RING_RI, RING_RO = 52.0, 63.0
POST_R = 61.5
POST_A = (56.44, -56.44, 125.0, -125.0)
SPRING_POCKET = 8.5

# Schlitten (lokal: Stiftachse im Ursprung, +x radial nach aussen)
CAR_MR = (13.3, 25.0)    # Fuehrungsstange 2 mm (Radius, Winkel), nah am Mitnehmerstift gegen Verkanten
CAR_AR = (13.0, 205.0)   # Verdrehsicherung 1,5 mm
CAR_SCREW = -50.0        # Klemmschraube M3
COLS = ((45.0, -90.0), (45.0, -30.0), (45.0, 30.0), (45.0, 90.0), (40.0, 180.0))
DRUM_R = 51.0
BELT_X = 8.0             # Riemennuten bei x = +-8, Austritt oben bei y = 176
BELT_PIN_Y = 149.0       # M5 als Umlenkstift fuer die Riemenschlaufe
BELT_Z1 = 26.5           # Oberkante Klemmblock (Riemen z 20..26)

# Servo MG90S nach TowerPro-Masszeichnung: A 32,5 (Hoehe mit Welle), C 28,4
# (bis Getriebedom), F 18,5 (Boden bis Laschen-Oberseite), E 32,1, B 22,8, D 12,4
SV_TAB2HORN = 14.0  # Laschenflaeche (Wellenseite) bis Horn-Aussenflaeche (= Wellenende)
SV_TAB2TOP = 4.2    # Laschenflaeche bis Gehaeuse-Oberseite
SV_BODY = 22.7
SV_TURRET = 5.7     # Getriebedom ueber der Gehaeuse-Oberseite
SV_SHAFT = 4.1
HORN_T = 1.6        # Armdicke Standardhorn
HORN_FLOAT = 0.3    # Luft zwischen Horn-Aussenseite und Nutgrund
HORN_LEN = 16.0     # Nabenmitte bis Armspitze (measured: Einarm gesamt 19,47)

# Hubmodul (Welt, Station unten bei -y)
ROD_Y = -36.5
ROD_X = 7.5
JOINT_Y = -32.3
CRANK_R = 6.3
LINK_L = 14.0
JOINT_Z_UP = 52.0
CRANK_Z = JOINT_Z_UP - LINK_L - CRANK_R       # 31.7
B_SWING = 88.0
LINK_X0, LINK_X1 = 11.6, 14.6
CRANK_X0, CRANK_X1 = 15.1, 20.1
HORN_B_X = 18.0                               # Nutgrund in der Kurbel
WALL_X1 = HORN_B_X + HORN_FLOAT + SV_TAB2HORN # Laschenflaeche Servo B
WALL_X0 = WALL_X1 - SV_TAB2TOP                # Wand so dick wie Lasche bis Gehaeuse-Oberseite
HT_EAR = (26.0, -22.28)
LB_HOLES = (90, 225, 315)      # unsymmetrisch: Lagerbock passt nur in einer Lage
SKIDS = ((-64.0, 0.0), (64.0, 0.0), (0.0, 72.0))

BOM = {}


def bom(key, n=1):
    BOM[key] = BOM.get(key, 0) + n


# ----------------------------------------------------------------------------
# CSG-Helfer
# ----------------------------------------------------------------------------
def P(x, y, z):
    return adsk.core.Point3D.create(x * MM, y * MM, z * MM)


def V(x, y, z):
    return adsk.core.Vector3D.create(x, y, z)


def cyl(x, y, z0, z1, r, r1=None):
    return tm.createCylinderOrCone(P(x, y, z0), r * MM, P(x, y, z1), (r if r1 is None else r1) * MM)


def cylp(p0, p1, r, r1=None):
    return tm.createCylinderOrCone(P(*p0), r * MM, P(*p1), (r if r1 is None else r1) * MM)


def box(x0, x1, y0, y1, z0, z1):
    c = P((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)
    obb = adsk.core.OrientedBoundingBox3D.create(c, V(1, 0, 0), V(0, 1, 0), abs(x1 - x0) * MM, abs(y1 - y0) * MM, abs(z1 - z0) * MM)
    return tm.createBox(obb)


def obox(cx, cy, cz, lx, ly, lz, ang=0.0):
    a = math.radians(ang)
    obb = adsk.core.OrientedBoundingBox3D.create(P(cx, cy, cz), V(math.cos(a), math.sin(a), 0), V(-math.sin(a), math.cos(a), 0), lx * MM, ly * MM, lz * MM)
    return tm.createBox(obb)


def gbox(c, ldir, wdir, l, w, h):
    obb = adsk.core.OrientedBoundingBox3D.create(P(*c), V(*ldir), V(*wdir), l * MM, w * MM, h * MM)
    return tm.createBox(obb)


def sphere(x, y, z, r):
    return tm.createSphere(P(x, y, z), r * MM)


def U(a, *bs):
    for b in bs:
        tm.booleanOperation(a, b, BT.UnionBooleanType)
    return a


def C(a, *bs):
    for b in bs:
        tm.booleanOperation(a, b, BT.DifferenceBooleanType)
    return a


def I(a, b):
    tm.booleanOperation(a, b, BT.IntersectionBooleanType)
    return a


def cp(b):
    return tm.copy(b)


def m_rot(deg, axis=(0, 0, 1), org=(0, 0, 0)):
    m = adsk.core.Matrix3D.create()
    m.setToRotation(math.radians(deg), V(*axis), P(*org))
    return m


def m_tr(dx, dy, dz):
    m = adsk.core.Matrix3D.create()
    m.translation = V(dx * MM, dy * MM, dz * MM)
    return m


def xf(b, *mats):
    for m in mats:
        tm.transform(b, m)
    return b


def hexz(x, y, z0, z1, af, rot=0.0):
    zc, h = (z0 + z1) / 2.0, z1 - z0
    b = obox(x, y, zc, af, af * 2.0, h, rot)
    I(b, obox(x, y, zc, af, af * 2.0, h, rot + 60))
    I(b, obox(x, y, zc, af, af * 2.0, h, rot + 120))
    return b


def hexx(x0, x1, y, z, af):
    b = hexz(0, 0, x0, x1, af)
    return xf(b, m_rot(90, (0, 1, 0)), m_tr(0, y, z))  # z->x


def half(cx, cy, ang, z0, z1, R=300.0):
    a = math.radians(ang)
    nx, ny = -math.sin(a), math.cos(a)
    return obox(cx + nx * R / 2.0, cy + ny * R / 2.0, (z0 + z1) / 2.0, 2 * R, R, z1 - z0, ang)


def wedge(cx, cy, a1, a2, R, z0, z1):
    w = cyl(cx, cy, z0, z1, R)
    I(w, half(cx, cy, a1, z0, z1))
    I(w, half(cx, cy, a2 + 180.0, z0, z1))
    return w


def pol(r, a):
    """Weltpunkt aus Radius und Winkel ab Station (unten), gegen den Uhrzeigersinn."""
    return (r * math.sin(math.radians(a)), AY - r * math.cos(math.radians(a)))


def lp(r, th):
    return (r * math.cos(math.radians(th)), r * math.sin(math.radians(th)))


def rot2(p, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


def drum_to_world(b, slot=0.0):
    return xf(b, m_rot(slot - 90.0), m_tr(0, AY, 0))


def chamfer_x(body, x, z, size, y0, y1, sign):
    """Fase an einer Kante parallel y bei (x,z); sign=+1: Material liegt bei +x."""
    s = size * math.sqrt(2.0)
    c = ((x), (y0 + y1) / 2.0, z)
    k = gbox(c, (1 / math.sqrt(2), 0, 1 / math.sqrt(2)), (0, 1, 0), s, abs(y1 - y0), s)
    C(body, k)
    return body


# Luecke im Parkring und Hub-Pad sind schraeg geschnitten (Parallelogramm):
# der radial liegende Mitnehmerstift ueberbrueckt die Fuge immer und kann
# nicht hineinfallen.
SKEW = math.degrees(math.atan2(1.0, 0.5))   # Kantenrichtung in der xy-Ebene
SKEW_Y = -21.0                               # Mitte der Stiftauflage (r = 55)
PAD_W = 12.0
GAP_W = PAD_W + 1.6     # 0,8 mm Luft je Seite: ein schief stehender Hubschlitten streift nicht


def station_slab(width_x, z0, z1, y0=-45.0, y1=-10.0):
    s = obox(0, SKEW_Y, (z0 + z1) / 2.0, 90.0, width_x * math.sin(math.radians(SKEW)), z1 - z0, SKEW)
    I(s, box(-40, 40, y0, y1, z0 - 1, z1 + 1))
    return s


def chamfer_skew(body, x_at_mid, z, size):
    a = math.radians(SKEW)
    ldir = (math.cos(a), math.sin(a), 0)
    n = (-math.sin(a), math.cos(a), 0)
    d = 1 / math.sqrt(2.0)
    wdir = (n[0] * d, n[1] * d, d)
    s = size * math.sqrt(2.0)
    C(body, gbox((x_at_mid, SKEW_Y, z), ldir, wdir, 90.0, s, s))
    return body


def vol(b):
    return b.volume * 1000.0


def bbox(b):
    bb = b.boundingBox
    return (bb.minPoint.x / MM, bb.minPoint.y / MM, bb.minPoint.z / MM, bb.maxPoint.x / MM, bb.maxPoint.y / MM, bb.maxPoint.z / MM)


def ivol(a, b):
    A, B = bbox(a), bbox(b)
    for i in range(3):
        if A[i] > B[i + 3] - 1e-6 or B[i] > A[i + 3] - 1e-6:
            return 0.0
    c = cp(a)
    try:
        tm.booleanOperation(c, b, BT.IntersectionBooleanType)
    except Exception:
        return 0.0
    if c.faces.count == 0:
        return 0.0
    return vol(c)


# ----------------------------------------------------------------------------
# Teile (feste Gruppe, Weltkoordinaten)
# ----------------------------------------------------------------------------
def carriage_points():
    mr = lp(*CAR_MR)
    ar = lp(*CAR_AR)
    return mr, ar


def drum_sockets():
    mr, ar = carriage_points()
    out = []
    for k in SLOTS:
        c = lp(RP, k)
        m = rot2(mr, k)
        a = rot2(ar, k)
        out.append((c, (c[0] + m[0], c[1] + m[1]), (c[0] + a[0], c[1] + a[1])))
    return out


def part_grundplatte():
    gp = cyl(0, AY, Z_B0, Z_B1, 23.5)
    rim = cyl(0, AY, Z_B0, Z_B1, 68.0)
    C(rim, cyl(0, AY, Z_B0 - 1, Z_B1 + 1, 46.0))
    C(rim, wedge(0, AY, -134.0, -46.0, 90.0, Z_B0 - 1, Z_B1 + 1))
    U(gp, rim)
    U(gp, wedge(0, AY, 56.0, 124.0, 50.0, Z_B0, Z_B1))
    for s in (-1, 1):
        U(gp, cyl(s * 64.0, 0, Z_B0, Z_B1, 8.5))
    U(gp, box(-17, 17, 78, 101, Z_B0, Z_B1))
    # Lagerbock-Flansch: 3x M3x12 von oben, Mutter unten
    for th in LB_HOLES:
        x, y = 15 * math.cos(math.radians(th)), AY + 15 * math.sin(math.radians(th))
        C(gp, cyl(x, y, Z_B0 - 1, Z_B1 + 1, F['m3'] / 2))
        C(gp, hexz(x, y, Z_B0 - 1, Z_B0 + F['m3_nh'], F['m3_af']))
    # Stuetzen: M3x12 von unten, Kopf versenkt
    for a in POST_A:
        x, y = pol(POST_R, a)
        C(gp, cyl(x, y, Z_B0 - 1, Z_B1 + 1, F['m3'] / 2))
        C(gp, cyl(x, y, Z_B0 - 1, Z_B0 + F['m3_headh'], F['m3_head'] / 2))
    # Gleitfuesse: M3x8 von oben
    for (sx, sy) in SKIDS:
        C(gp, cyl(sx, sy, Z_B0 - 1, Z_B1 + 1, F['m3'] / 2))
    # Riemenarm: 2x M4x16 von oben, Mutter unten
    for s in (-1, 1):
        C(gp, cyl(s * 9.0, 90, Z_B0 - 1, Z_B1 + 1, F['m4'] / 2))
        C(gp, hexz(s * 9.0, 90, Z_B0 - 1, Z_B0 + F['m4_nh'], F['m4_af'], 30))
    return gp


def part_lagerbock():
    lb = cyl(0, AY, Z_B1, Z_FL, 20.0)
    U(lb, cyl(0, AY, Z_FL, Z_FL + 4, 11.0, 8.0))
    U(lb, cyl(0, AY, Z_FL, Z_SH, 8.0))
    U(lb, cyl(0, AY, Z_SH, Z_BRG0, 5.75))
    U(lb, cyl(0, AY, Z_BRG0, Z_BRG1, F['stub'] / 2))
    tng = cyl(0, AY, Z_BRG1, Z_TNG, F['stub'] / 2)
    I(tng, box(-2.8, 2.8, AY - 5, AY + 5, Z_BRG1 - 1, Z_TNG + 1))
    U(lb, tng)
    C(lb, cyl(0, AY, 36.0, Z_TNG + 1, F['m3'] / 2))
    C(lb, box(-F['m3_slot'] / 2, F['m3_slot'] / 2, AY - 2.95, AY + 9, 37.6, 37.6 + F['m3_sh']))
    for th in LB_HOLES:
        x, y = 15 * math.cos(math.radians(th)), AY + 15 * math.sin(math.radians(th))
        C(lb, cyl(x, y, Z_B1 - 1, Z_FL + 5, F['m3'] / 2))
        C(lb, cyl(x, y, Z_FL, Z_FL + 6, 3.4))  # Platz fuer Schraubenkopf am Kegel
    bom('M3x12', 3); bom('M3 Mutter', 3)      # Flansch
    bom('M3x20', 1); bom('M3 Mutter', 1)      # Hornanker-Spannschraube
    return lb


def taper_slot(wh, wt, z0, z1, y_end=19.0):
    """Trapeznut fuer einen Hornarm entlang +y ab dem Ursprung: Breite wh bei r=3,5, wt bei r=HORN_LEN."""
    sl = box(-5, 5, 0, y_end, z0, z1)
    run = HORN_LEN - 3.5
    d = (wh - wt) / 2.0
    ang_r = math.degrees(math.atan2(run, -d))
    ang_l = math.degrees(math.atan2(run, d))
    I(sl, half(wh / 2.0, 3.5, ang_r, z0, z1))
    I(sl, half(-wh / 2.0, 3.5, ang_l + 180.0, z0, z1))
    return sl


def part_hornanker(wh=5.8, wt=4.2):
    """Fester Anker fuer das Doppelarm-Horn von Servo A. Liegt lose in der Kassette."""
    L = 18.0
    ha = cyl(0, AY, Z_BRG1, 54.3, 5.75)
    body = box(-6, 6, AY - L + 6, AY + L - 6, 54.3, Z_HA_T)
    U(body, cyl(0, AY - L + 6, 54.3, Z_HA_T, 6.0), cyl(0, AY + L - 6, 54.3, Z_HA_T, 6.0))
    U(ha, body)
    C(ha, box(-2.9, 2.9, AY - 4.15, AY + 4.15, Z_BRG1 - 1, Z_TNG + 0.2))
    C(ha, cyl(0, AY, Z_TNG, 58.0, F['m3'] / 2))
    C(ha, cyl(0, AY, 57.5, Z_HA_T + 1, 3.25))
    C(ha, cyl(0, AY, Z_HORN_FLOOR, Z_HA_T + 1, 3.8))
    sl = taper_slot(wh, wt, Z_HORN_FLOOR, Z_HA_T + 1)
    sl2 = xf(cp(sl), m_rot(180))
    C(ha, xf(sl, m_tr(0, AY, 0)), xf(sl2, m_tr(0, AY, 0)))
    return ha


def part_parkring():
    pr = cyl(0, AY, Z_SHELF - RING_T, Z_SHELF, RING_RO)
    for a in POST_A:
        x, y = pol(POST_R, a)
        U(pr, cyl(x, y, Z_SHELF - RING_T, Z_SHELF, 6.5))
    for s in (-1, 1):
        U(pr, cyl(s * HT_EAR[0], HT_EAR[1], Z_SHELF - RING_T, Z_SHELF, 6.5))
    C(pr, cyl(0, AY, Z_SHELF - RING_T - 1, Z_SHELF + 1, RING_RI))
    C(pr, station_slab(GAP_W, Z_SHELF - RING_T - 1, Z_SHELF + 1))
    for s in (-1, 1):
        chamfer_skew(pr, s * GAP_W / 2.0, Z_SHELF, 0.4)
    for a in POST_A:
        x, y = pol(POST_R, a)
        C(pr, cyl(x, y, Z_SHELF - RING_T - 1, Z_SHELF + 1, F['m3'] / 2))
        C(pr, cyl(x, y, Z_SHELF - F['m3_headh'], Z_SHELF + 1, F['m3_head'] / 2))
    for s in (-1, 1):
        C(pr, cyl(s * HT_EAR[0], HT_EAR[1], Z_SHELF - RING_T - 1, Z_SHELF + 1, F['m3'] / 2))
    for a in (STOP_A, -STOP_A):
        x, y = pol(55.0, a)
        C(pr, cyl(x, y, Z_SHELF - RING_T - 1, Z_SHELF + 1, F['m3'] / 2))
    bom('M3x12', 4); bom('M3 Mutter', 4)   # Ring auf Stuetzen
    bom('M3x12', 2); bom('M3 Mutter', 2)   # Ring auf Hubtraeger
    bom('M3x12', 2); bom('M3 Mutter', 2)   # Anschlagschrauben
    return pr


def part_stuetze():
    """lokal: z 0..H, Mutterschlitze oeffnen nach +x"""
    H = Z_SHELF - RING_T - Z_B1
    st = cyl(0, 0, 0, H, 5.5)
    C(st, cyl(0, 0, -1, 12.5, F['m3'] / 2))
    C(st, cyl(0, 0, H - 12.5, H + 1, F['m3'] / 2))
    C(st, box(-2.95, 7, -F['m3_slot'] / 2, F['m3_slot'] / 2, 6.4, 6.4 + F['m3_sh']))
    zt = H - 9.0
    C(st, box(-2.95, 7, -F['m3_slot'] / 2, F['m3_slot'] / 2, zt, zt + F['m3_sh']))
    return st


def place_stuetzen(st):
    out = []
    for a in POST_A:
        x, y = pol(POST_R, a)
        ang = math.degrees(math.atan2(y - AY, x))
        out.append(xf(cp(st), m_rot(ang), m_tr(x, y, Z_B1)))
    bom('M3x12', 4); bom('M3 Mutter', 4)   # Stuetzen von unten
    return out


def part_gleitfuss():
    gf = sphere(0, 0, 16.0, 16.0)
    I(gf, cyl(0, 0, 0, Z_B0, 7.0))
    C(gf, hexz(0, 0, 2.4, Z_B0 + 1, F['m3_af']))
    return gf


def part_hubtraeger():
    wx0, wx1 = WALL_X0, WALL_X1
    ht = box(-wx1, wx1, -45.5, -42.3, 17.5, 58)
    U(ht, box(-11.5, 11.5, -45.5, -42.3, 58, 73))
    U(ht, box(wx0, wx1, -45.5, -17.5, 17.5, 58))
    U(ht, box(-wx1, -wx0, -45.5, -17.5, 17.5, 58))
    U(ht, box(-11.5, 10.6, -42.3, -29.5, 17.5, 25.5))
    for s in (1, -1):
        ex, ey = s * HT_EAR[0], HT_EAR[1]
        xi = 21.6                      # Bock sitzt innen an der Wand, 1,3 mm Wand am Mutterschlitz
        blk = box(min(s * xi, s * wx0), max(s * xi, s * wx0), -25.5, -17.5, 39.0, 58.0)
        d = 1 / math.sqrt(2)
        for (x_wall, x_edge) in ((s * wx0, s * xi),):
            dx = x_edge - x_wall
            z_edge = 46.0
            z_wall = z_edge - abs(dx)
            sg = 1.0 if dx > 0 else -1.0
            ldir = (sg * d, 0, d)
            nx, nz = sg * d, -d
            cx = (x_wall + x_edge) / 2.0 + nx * 10.0
            cz = (z_wall + z_edge) / 2.0 + nz * 10.0
            C(blk, gbox((cx, -21.5, cz), ldir, (0, 1, 0), 60.0, 12.0, 20.0))
        U(ht, blk)
        C(ht, cyl(ex, ey, 46.5, 59, F['m3'] / 2))
        C(ht, box(ex - F['m3_slot'] / 2, ex + F['m3_slot'] / 2, ey - 2.95, -17.0, 51.7, 51.7 + F['m3_sh']))
    # Servo-B-Ausschnitt und M2
    zc = CRANK_Z + 5.3
    C(ht, box(wx0 - 1, wx1 + 1, JOINT_Y - 6.5, JOINT_Y + 6.5, zc - 11.9, zc + 11.9))
    for dz in (-13.9, 13.9):
        C(ht, cylp((wx0 - 1, JOINT_Y, zc + dz), (wx1 + 1, JOINT_Y, zc + dz), F['m2'] / 2))
        C(ht, hexx(wx0 - 1, wx0 + F['m2_nh'], JOINT_Y, zc + dz, F['m2_af']))
    # Stangen 1,5 mm unten (Sackloch)
    for s in (-1, 1):
        C(ht, cyl(s * ROD_X, ROD_Y, 20.5, 26, F['rod2_press'] / 2))
    # Kopfstueck-Schraube von hinten
    C(ht, cylp((0, -46, 69.5), (0, -42, 69.5), F['m3'] / 2))
    bom('M2x8', 2); bom('M2 Mutter', 2)    # Servo B
    bom('M3x12', 1); bom('M3 Mutter', 1)   # Kopfstueck
    return ht


def part_kopfstueck():
    ks = box(-11.5, 10.6, -42.3, -30.5, 66, 73)
    for s in (-1, 1):
        C(ks, cyl(s * ROD_X, ROD_Y, 65, 70, F['rod2_press'] / 2))
    C(ks, cylp((0, -43, 69.5), (0, -33, 69.5), F['m3'] / 2))
    C(ks, box(-F['m3_slot'] / 2, F['m3_slot'] / 2, -38.2, -38.2 + F['m3_nh'], 66.6, 74))
    return ks


def part_hubschlitten():
    """Welt, Stellung OBEN (Pad buendig mit Parkring)."""
    hs = box(-11, 11, -40.0, -29.5, Z_SHELF - 28, Z_SHELF)
    pad = station_slab(PAD_W, Z_SHELF - 5, Z_SHELF, -29.6, -18.3)
    for s in (-1, 1):
        chamfer_skew(pad, s * PAD_W / 2.0, Z_SHELF, 0.4)
    U(hs, pad)
    for s in (-1, 1):
        C(hs, cyl(s * ROD_X, ROD_Y, Z_SHELF - 29, Z_SHELF + 1, F['rod2_slide'] / 2))
        C(hs, cyl(s * ROD_X, ROD_Y, Z_SHELF - 21, Z_SHELF - 7, 1.75))
    C(hs, cylp((1.0, JOINT_Y, JOINT_Z_UP), (11.5, JOINT_Y, JOINT_Z_UP), F['pin2_press_h'] / 2))
    return hs


def crank_state(deg):
    """Kurbelwinkel ab oberem Totpunkt (0 = Stift oben). Gibt (pin_y, pin_z, joint_z)."""
    a = math.radians(deg)
    py = JOINT_Y - CRANK_R * math.sin(a)
    pz = CRANK_Z + CRANK_R * math.cos(a)
    jz = pz + math.sqrt(LINK_L ** 2 - (py - JOINT_Y) ** 2)
    return py, pz, jz


def part_pleuel(length=LINK_L):
    """lokal: Bohrungen bei z=0 und z=length, x = Dicke 0..3"""
    t = LINK_X1 - LINK_X0
    pl = cylp((0, 0, 0), (t, 0, 0), 3.2)
    U(pl, cylp((0, 0, length), (t, 0, length), 3.2))
    U(pl, box(0, t, -2.5, 2.5, 0, length))
    C(pl, cylp((-1, 0, 0), (t + 1, 0, 0), F['pin2_turn'] / 2))
    C(pl, cylp((-1, 0, length), (t + 1, 0, length), F['pin2_turn'] / 2))
    return pl


def place_pleuel(pl, deg):
    py, pz, jz = crank_state(deg)
    tilt = math.degrees(math.atan2(JOINT_Y - py, jz - pz))  # Neigung um x
    b = cp(pl)
    # lokal z-Achse -> Richtung (dy, dz); Drehung um x: z->(-sin, cos)*...  y' = -sin(t) * z
    return xf(b, m_rot(-tilt, (1, 0, 0)), m_tr(LINK_X0, py, pz))


def part_kurbel(wh=6.0, wt=4.5):
    """Welt, Totpunkt-Stellung (Kurbelzapfen bei +z, Hornarm bei +y). Das Einarm-Horn
    liegt formschluessig in einer Trapeznut auf der Servoseite."""
    t = CRANK_X1 - CRANK_X0
    zf = HORN_B_X - CRANK_X0            # Nutgrund lokal
    ku = cyl(0, 0, 0, t, 9.3)
    U(ku, box(-4.6, 4.6, 0, HORN_LEN + 1.5, 0, t))
    U(ku, cyl(0, HORN_LEN + 1.5, 0, t, 4.6))
    C(ku, cyl(0, 0, -1, t + 1, 2.6))
    C(ku, cyl(0, 0, zf, t + 1, 3.8))
    C(ku, taper_slot(wh, wt, zf, t + 1, HORN_LEN + 0.8))
    C(ku, cyl(-CRANK_R, 0, -1, t + 1, F['pin2_press_v'] / 2))
    # lokal (x,y,z) -> Welt (z, y, -x): Dicke nach +x, Arm nach +y, Zapfen nach +z
    return xf(ku, m_rot(90, (0, 1, 0)), m_tr(CRANK_X0, JOINT_Y, CRANK_Z))


def crank_rot(deg):
    # positive deg: Zapfen schwenkt von +z nach -y
    return m_rot(deg, (1, 0, 0), (0, JOINT_Y, CRANK_Z))


def find_old_body(prefix):
    des = adsk.fusion.Design.cast(app.activeProduct)
    for o in des.rootComponent.allOccurrences:
        if o.name.startswith(prefix):
            return o.bRepBodies.item(0)
    return None


BELT_LAND = 7.3        # halbe Stegbreite = Radius der Umlenkrundung (Riemenruecken liegt an)
BELT_SLOT = 2.0        # Nutbreite fuer den einfachen Riemen (1,38 dick)
BELT_W = 23.5          # halbe Blockbreite
BELT_COVER_Y = (149.0, 164.0)   # 2x M5x20 durch Deckel und Steg
SHOE_L, SHOE_T, SHOE_H = 16.0, 4.0, 6.7
SHOE_POCKET = 4.5      # Taschentiefe ab Nutwand
SHOE_Y0 = 150.0
SHOE_Z = 23.0          # Achse der Klemmschrauben
RIDGE_H, RIDGE_W = 0.6, 0.9


def part_riemenarm():
    """Arm fuer EINEN durchgehenden, ungeschnittenen Riemen. Der Riemen kommt links herein, laeuft
    mit dem Ruecken um die Rundung (R 7,3) des Mittelstegs und geht rechts wieder hinaus. Die
    Zaehne zeigen nach aussen; je Seite drueckt eine M3 ein gezahntes Klemmstueck in die Zaehne."""
    xo = BELT_LAND + BELT_SLOT
    zc0 = 19.5
    ra = box(-16, 16, 100, 137, Z_B1, 20)
    U(ra, box(-17, 17, 78, 101, Z_B1, 21))
    blk = box(-BELT_W, BELT_W, 136, 176, Z_B1, BELT_Z1)
    for sg in (-1, 1):
        x_o = sg * xo
        cx = x_o + sg * 6.0
        xe = sg * (BELT_W + 0.1)
        C(blk, box(min(x_o, xe), max(x_o, xe), 170, 176.1, Z_B1 - 1, BELT_Z1 + 1))
        q = cyl(cx, 170, Z_B1, BELT_Z1, 6.0)
        I(q, box(min(x_o, cx), max(x_o, cx), 170, 176.1, Z_B1 - 1, BELT_Z1 + 1))
        U(blk, q)
    # Riemenkanal: U um den Steg
    chan = cyl(0, BELT_PIN_Y, zc0, BELT_Z1 + 1, xo)
    U(chan, box(-xo, xo, BELT_PIN_Y, 177, zc0, BELT_Z1 + 1))
    land = cyl(0, BELT_PIN_Y, zc0 - 1, BELT_Z1 + 2, BELT_LAND)
    U(land, box(-BELT_LAND, BELT_LAND, BELT_PIN_Y, 178, zc0 - 1, BELT_Z1 + 2))
    C(chan, land)
    C(blk, chan)
    ys = SHOE_Y0 + (SHOE_L + 0.4) / 2.0
    for sg in (-1, 1):
        # Tasche fuer das Klemmstueck
        x0, x1 = sg * xo, sg * (xo + SHOE_POCKET)
        C(blk, box(min(x0, x1), max(x0, x1), SHOE_Y0, SHOE_Y0 + SHOE_L + 0.4, zc0, BELT_Z1 + 1))
        # Klemmschraube M3x12 von aussen, Mutter im Schlitz von oben
        C(blk, cylp((sg * (xo + 1), ys, SHOE_Z), (sg * (BELT_W + 1), ys, SHOE_Z), F['m3'] / 2))
        n0, n1 = sg * (xo + SHOE_POCKET + 1.6), sg * (xo + SHOE_POCKET + 1.6 + F['m3_sh'])
        C(blk, box(min(n0, n1), max(n0, n1), ys - F['m3_slot'] / 2, ys + F['m3_slot'] / 2, SHOE_Z - 3.4, BELT_Z1 + 1))
    U(ra, blk)
    for y in BELT_COVER_Y:
        C(ra, cyl(0, y, Z_B1 - 1, BELT_Z1 + 1, F['m5'] / 2))
        C(ra, hexz(0, y, Z_B1 - 1, Z_B1 + F['m5_nh'], F['m5_af']))
    for sg in (-1, 1):
        C(ra, cyl(sg * 9.0, 90, Z_B1 - 1, 22, F['m4'] / 2))
    bom('M5x20', 2); bom('M5 Mutter', 2)   # Riemendeckel
    bom('M4x16', 2); bom('M4 Mutter', 2)   # Riemenarm auf Grundplatte
    bom('M3x12', 2); bom('M3 Mutter', 2)   # Klemmschrauben Riemen
    return ra


def part_riemendeckel():
    xo = BELT_LAND + BELT_SLOT
    rd = box(-BELT_W, BELT_W, 136, 170, BELT_Z1, BELT_Z1 + 3.0)
    U(rd, box(-xo, xo, 170, 176, BELT_Z1, BELT_Z1 + 3.0))
    for y in BELT_COVER_Y:
        C(rd, cyl(0, y, BELT_Z1 - 1, BELT_Z1 + 4, F['m5'] / 2))
    return rd


def part_riemenklemme():
    """Gezahntes Klemmstueck (0,4-mm-Duese). lokal: Rippen zeigen nach -x, Ruecken bei x = SHOE_T."""
    kl = box(0, SHOE_T, 0, SHOE_L, 0, SHOE_H)
    for k in range(8):
        yc = 1.0 + 2.0 * k
        U(kl, box(-RIDGE_H, 0.05, yc - RIDGE_W / 2, yc + RIDGE_W / 2, 0, SHOE_H))
    return kl


def place_riemenklemmen(kl):
    xf_ = BELT_LAND + 1.38 + 0.05      # Zahnkoepfe des Riemens
    a = xf(cp(kl), m_tr(xf_, SHOE_Y0 + 0.2, 19.5))
    b = xf(cp(kl), m_rot(180), m_tr(-xf_, SHOE_Y0 + 0.2 + SHOE_L, 19.5))
    return [a, b]


def part_einsatz(d_pen):
    """Geschlitzte Huelse: zentriert einen duenneren Stift im 19-mm-Schlitten."""
    ro = F['pen_bore'] / 2 - 0.05
    e = cyl(0, 0, 0, CAR_H, ro)
    U(e, cyl(0, 0, CAR_H, CAR_H + 1.5, ro + 1.2))
    C(e, cyl(0, 0, -1, CAR_H + 3, (d_pen + 0.1) / 2.0))   # Schlitz federt, daher fast ohne Luft
    C(e, box(0, 15, -1.75, 1.75, -1, CAR_H + 3))
    return e


# ----------------------------------------------------------------------------
# Trommel (lokal: Achse im Ursprung, +x = Station bei Slot 0)
# ----------------------------------------------------------------------------
def part_trommel_unten():
    tu = cyl(0, 0, Z_D1B, Z_D1T, DRUM_R)
    U(tu, cyl(0, 0, Z_BRG0, Z_D1B, 19.0))
    C(tu, cyl(0, 0, Z_BRG0 - 1, Z_BRG1, F['brg_od'] / 2))
    C(tu, cyl(0, 0, Z_BRG1 - 1, Z_D1T + 1, 9.25))
    for (c, m, a) in drum_sockets():
        C(tu, cyl(c[0], c[1], Z_D1B - 1, Z_D1T + 1, F['pen_hole'] / 2))
        C(tu, cyl(m[0], m[1], Z_D1T - 4, Z_D1T + 1, F['rod2_press'] / 2))
        C(tu, cyl(a[0], a[1], Z_D1T - 4, Z_D1T + 1, F['rod15_press'] / 2))
    for (r, th) in COLS:
        x, y = lp(r, th)
        C(tu, cyl(x, y, Z_D1B - 1, Z_D1T + 1, F['m3'] / 2))
    for th in (90, 210, 330):
        x, y = lp(15.0, th)
        C(tu, cyl(x, y, Z_BRG0 - 1, Z_D1T + 1, F['m3'] / 2))
        C(tu, hexz(x, y, Z_D1T - F['m3_nh'], Z_D1T + 1, F['m3_af'], th))
    bom('M3x12', 5); bom('M3 Mutter', 5)   # Trommel unten an Saeulen
    bom('M3x12', 3); bom('M3 Mutter', 3)   # Lagerring
    return tu


def part_lagerring():
    lr = cyl(0, 0, 42.0, Z_BRG0, 19.0)
    C(lr, cyl(0, 0, 41, Z_BRG0 + 1, 9.3))
    for th in (90, 210, 330):
        x, y = lp(15.0, th)
        C(lr, cyl(x, y, 41, Z_BRG0 + 1, F['m3'] / 2))
    return lr


SB_U0, SB_U1 = -21.75, 11.15     # Servokasten innen
SB_V = 6.5
SB_W = 3.2
Z_LEDGE_T = Z_HORN_FLOOR + HORN_FLOAT + SV_TAB2HORN   # 77.3
Z_LEDGE_B = Z_LEDGE_T - 5.0


def part_trommel_oben():
    to = cyl(0, 0, Z_D2B, Z_D2T, DRUM_R)
    for (r, th) in COLS:
        x, y = lp(r, th)
        U(to, cyl(x, y, Z_D1T, Z_D2B, 5.0))
    U(to, box(SB_U0 - SB_W, SB_U1 + SB_W, -SB_V - SB_W, SB_V + SB_W, Z_LEDGE_B, Z_D2B))
    C(to, box(SB_U0, SB_U1, -SB_V, SB_V, Z_LEDGE_T, Z_D2T + 1))
    C(to, box(-17.2, 6.6, -SB_V, SB_V, Z_LEDGE_B - 1, Z_LEDGE_T + 0.1))
    for u in (-19.2, 8.6):
        C(to, cyl(u, 0, Z_LEDGE_B - 1, Z_LEDGE_T + 1, F['m2'] / 2))
        # Mutterschlitz M2 von der langen Kastenseite
        C(to, box(u - F['m2_slot'] / 2, u + F['m2_slot'] / 2, -SB_V - SB_W - 1, 2.3, Z_LEDGE_B + 1.5, Z_LEDGE_B + 1.5 + F['m2_nh']))
    for (c, m, a) in drum_sockets():
        C(to, cyl(c[0], c[1], Z_D2B - 1, Z_D2T + 1, F['pen_hole'] / 2))
        C(to, cyl(m[0], m[1], Z_D2B - 1, Z_D2B + 4, F['rod2_press'] / 2))
        C(to, cyl(a[0], a[1], Z_D2B - 1, Z_D2B + 4, F['rod15_press'] / 2))
    for (r, th) in COLS:
        x, y = lp(r, th)
        C(to, cyl(x, y, Z_D1T - 1, Z_D1T + 14, F['m3'] / 2))
        d = lp(1.0, th)
        cx, cy = x + d[0] * 2.0, y + d[1] * 2.0
        C(to, obox(cx, cy, Z_D1T + 2.6 + F['m3_sh'] / 2, 10.0, F['m3_slot'], F['m3_sh'], th))
    bom('M2x8', 2); bom('M2 Mutter', 2)    # Servo A
    return to


def part_schlitten():
    """lokal: Stiftachse im Ursprung, z 0..CAR_H. Bohrung 19 mm: Stifte bis 18,5 mm direkt,
    duennere mit Einsatz. Geklemmt wird mit einer M3 gegen die Gegenseite."""
    mr, ar = carriage_points()
    H = CAR_H
    rb = F['pen_bore'] / 2
    ss = cyl(0, 0, 0, H, rb + 3.0)
    U(ss, cyl(mr[0], mr[1], 0, H, 4.4))
    U(ss, cyl(ar[0], ar[1], 0, H, 3.4))
    U(ss, box(rb + 1.0, 17, -3, 3, 0, 9.5))
    sd = lp(1.0, CAR_SCREW)
    U(ss, obox(sd[0] * 12.6, sd[1] * 12.6, H / 2, 9.2, 9.0, H, CAR_SCREW))
    C(ss, cyl(0, 0, -1, H + 1, rb))
    C(ss, cylp((0, 0, H - 1.4), (0, 0, H + 0.01), rb, rb + 1.4))
    # Fuehrung 2 mm: zwei Laufflaechen, Freistich, Federtasche (nur Federn 0,3 x 4)
    zp = H - SPRING_POCKET
    C(ss, cyl(mr[0], mr[1], -1, zp + 0.5, F['rod2_slide'] / 2))
    C(ss, cyl(mr[0], mr[1], 7.0, zp - 7.0, 1.75))
    C(ss, cyl(mr[0], mr[1], zp, H + 1, 2.4))
    # Verdrehsicherung 1,5 mm: Langloch Richtung Fuehrungsstange
    w = F['rod15_slide']
    dd = lp(0.7, CAR_MR[1])
    C(ss, obox(ar[0], ar[1], H / 2, 1.4, w, H + 2, CAR_MR[1]))
    for sgn in (-1, 1):
        C(ss, cyl(ar[0] + sgn * dd[0], ar[1] + sgn * dd[1], -1, H + 1, w / 2))
    # Mitnehmerstift 2 mm radial
    C(ss, cylp((rb + 1.0, 0, PIN_Z), (17.5, 0, PIN_Z), F['pin2_press_h'] / 2))
    # Klemmschraube M3 + Mutterschlitz von oben
    zs = H - 5.0
    C(ss, cylp((sd[0] * (rb - 1.5), sd[1] * (rb - 1.5), zs), (sd[0] * 18.5, sd[1] * 18.5, zs), F['m3'] / 2))
    C(ss, obox(sd[0] * 13.45, sd[1] * 13.45, (zs - 3.2 + H + 1) / 2, F['m3_nh'], F['m3_slot'], H + 1 - (zs - 3.2), CAR_SCREW))
    return ss


def place_schlitten(ss, k, zc):
    return xf(cp(ss), m_tr(RP, 0, zc), m_rot(k))


def ref_pin():
    return cylp((10.5, 0, PIN_Z), (24.0, 0, PIN_Z), 1.0)


def ref_pen():
    """lokal zum Schlitten: Spitze bei z = -Z_CAR_DN"""
    z0 = -Z_CAR_DN
    p = cyl(0, 0, z0, z0 + 2, 0.9)
    U(p, cyl(0, 0, z0 + 2, z0 + 7, 0.9, 6.15))
    U(p, cyl(0, 0, z0 + 7, z0 + 25, 6.15, 8.4))
    U(p, cyl(0, 0, z0 + 25, z0 + 140, 8.4))
    return p


def ref_servo_a():
    """Trommel-lokal"""
    zt = Z_LEDGE_T
    zc = zt - SV_TAB2TOP
    s = box(-21.5, 10.9, -6.1, 6.1, zt, zt + 2.5)
    U(s, box(-16.7, 6.1, -6.1, 6.1, zc, zc + SV_BODY))
    U(s, cyl(0, 0, zc - SV_TURRET, zc, 5.9))
    U(s, cyl(0, 0, zc - SV_TURRET - SV_SHAFT, zc - SV_TURRET, 2.35))
    return s


def ref_horn_arm(z0):
    """Standard-Hornarm entlang +y, Nabe im Ursprung, Aussenseite bei z0."""
    h = taper_slot(5.5, 4.0, z0, z0 + HORN_T, HORN_LEN - 2.0)
    U(h, cyl(0, HORN_LEN - 2.0, z0, z0 + HORN_T, 2.0))
    U(h, cyl(0, 0, z0, z0 + 4.2, 3.55))
    return h


def ref_horn_a():
    z0 = Z_HORN_FLOOR + HORN_FLOAT
    h = ref_horn_arm(z0)
    U(h, xf(ref_horn_arm(z0), m_rot(180)))
    return xf(h, m_tr(0, AY, 0))


def ref_servo_b():
    x = WALL_X1
    xc = x - SV_TAB2TOP
    zc = CRANK_Z + 5.3
    s = box(x, x + 2.5, JOINT_Y - 6.1, JOINT_Y + 6.1, zc - 16.2, zc + 16.2)
    U(s, box(xc, xc + SV_BODY, JOINT_Y - 6.1, JOINT_Y + 6.1, zc - 11.4, zc + 11.4))
    U(s, cylp((xc - SV_TURRET, JOINT_Y, CRANK_Z), (xc, JOINT_Y, CRANK_Z), 5.9))
    U(s, cylp((xc - SV_TURRET - SV_SHAFT, JOINT_Y, CRANK_Z), (xc - SV_TURRET, JOINT_Y, CRANK_Z), 2.35))
    return s


def ref_horn_b():
    h = ref_horn_arm(HORN_B_X + HORN_FLOAT - CRANK_X0)
    return xf(h, m_rot(90, (0, 1, 0)), m_tr(CRANK_X0, JOINT_Y, CRANK_Z))


def ref_bearing():
    b = cyl(0, AY, Z_BRG0, Z_BRG1, 11.0)
    C(b, cyl(0, AY, Z_BRG0 - 1, Z_BRG1 + 1, 4.0))
    return b


# ----------------------------------------------------------------------------
# Zusammenbau
# ----------------------------------------------------------------------------
def build():
    BOM.clear()
    parts = {}
    parts['01_Grundplatte'] = dict(fix=[part_grundplatte()])
    parts['02_Lagerbock'] = dict(fix=[part_lagerbock()])
    parts['03_Hornanker'] = dict(fix=[part_hornanker()])
    parts['08_Parkring'] = dict(fix=[part_parkring()])
    st = part_stuetze()
    parts['09_Stuetze'] = dict(fix=place_stuetzen(st), proto=st)
    gf = part_gleitfuss()
    parts['17_Gleitfuss'] = dict(fix=[xf(cp(gf), m_tr(sx, sy, 0)) for (sx, sy) in SKIDS], proto=gf)
    bom('M3x8', 3); bom('M3 Mutter', 3)
    parts['10_Hubtraeger'] = dict(fix=[part_hubtraeger()])
    parts['12_Kopfstueck'] = dict(fix=[part_kopfstueck()])
    parts['15_Riemenarm'] = dict(fix=[part_riemenarm()])
    parts['16_Riemendeckel'] = dict(fix=[part_riemendeckel()])
    kl = part_riemenklemme()
    parts['24_Riemenklemme'] = dict(fix=place_riemenklemmen(kl), proto=kl)
    parts['11_Hubschlitten'] = dict(lift=[part_hubschlitten()])
    parts['13_Pleuel'] = dict(link=part_pleuel())
    parts['14_Kurbel'] = dict(crank=[part_kurbel()])
    parts['04_Trommel_unten'] = dict(drum=[part_trommel_unten()])
    parts['05_Lagerring'] = dict(drum=[part_lagerring()])
    parts['06_Trommel_oben'] = dict(drum=[part_trommel_oben()])
    parts['07_Stiftschlitten'] = dict(car=part_schlitten())
    bom('M3x12', 3); bom('M3 Mutter', 3)   # Stiftklemmung
    return parts


def build_mixed():
    """Baugruppe so, wie sie gedruckt wird: jedes Teil mit den Passungen seiner Duese."""
    set_fits('04')
    p4 = build()
    set_fits('08')
    parts = build()
    for k in MIXED_04:
        parts[k] = p4[k]
    return parts


def print_bodies_all():
    """Alle Druckteile in Drucklage, jedes mit den Passungen seiner Duese."""
    res = []
    for nz in ('08', '04'):
        set_fits(nz)
        res += [t for t in print_bodies(build()) if nozzle_of(t[0]) == nz]
    set_fits('08')
    return sorted(res, key=lambda t: t[0])


def pose(parts, slot=0.0, pen_down=None, crank_deg=0.0, refs=True):
    """Liefert Liste (name, gruppe, body) in Weltkoordinaten.
    pen_down: Index 0..2 des abgesenkten Stifts oder None; crank_deg: 0 = oben."""
    out = []
    for name, d in parts.items():
        for b in d.get('fix', []):
            out.append((name, 'fix', cp(b)))
        for b in d.get('drum', []):
            out.append((name, 'drum', drum_to_world(cp(b), slot)))
    _, _, jz = crank_state(crank_deg)
    dz = jz - JOINT_Z_UP
    for b in parts['11_Hubschlitten']['lift']:
        out.append(('11_Hubschlitten', 'lift', xf(cp(b), m_tr(0, 0, dz))))
    out.append(('13_Pleuel', 'lift', place_pleuel(parts['13_Pleuel']['link'], crank_deg)))
    for b in parts['14_Kurbel']['crank']:
        out.append(('14_Kurbel', 'lift', xf(cp(b), crank_rot(crank_deg))))
    ss = parts['07_Stiftschlitten']['car']
    pin, pen = ref_pin(), ref_pen()
    for i, k in enumerate(SLOTS):
        zc = max(Z_CAR_DN, Z_CAR_UP + dz) if pen_down == i else Z_CAR_UP
        out.append(('07_Stiftschlitten', 'drum', drum_to_world(place_schlitten(ss, k, zc), slot)))
        if refs:
            out.append(('REF_Mitnehmerstift', 'drum', drum_to_world(place_schlitten(pin, k, zc), slot)))
            out.append(('REF_Lumocolor', 'drum', drum_to_world(place_schlitten(pen, k, zc), slot)))
    if refs:
        out.append(('REF_Servo_A', 'drum', drum_to_world(ref_servo_a(), slot)))
        out.append(('REF_Horn_A', 'fix', ref_horn_a()))
        out.append(('REF_Servo_B', 'fix', ref_servo_b()))
        out.append(('REF_Horn_B', 'lift', xf(ref_horn_b(), crank_rot(crank_deg))))
        out.append(('REF_608ZZ', 'brg', ref_bearing()))
    return out


def check(parts, verbose=False):
    msgs = []
    worst = {}

    state = {'first': True}

    def run(tag, items):
        n = len(items)
        first = state['first']
        state['first'] = False
        for i in range(n):
            for j in range(i + 1, n):
                a, b = items[i], items[j]
                if a[1] == b[1] and a[1] in ('fix', 'drum'):
                    if a[0] == b[0] or not first:
                        continue
                if 'brg' in (a[1], b[1]):
                    continue
                v = ivol(a[2], b[2])
                if v > 0.02:
                    key = (a[0], b[0])
                    if key not in worst or worst[key][0] < v:
                        worst[key] = (v, tag)

    # Drehen: alle Stifte oben, Hub oben
    for s in (-70, -66, -60, -45, -30, -15, 0, 15, 30, 45, 60, 66, 70):
        run('dreh %+d' % s, pose(parts, s, None, 0.0))
    # Schreiben: aktiver Stift unten, Hubweg
    for i, s in enumerate((60.0, 0.0, -60.0)):
        idx = {60.0: 0, 0.0: 1, -60.0: 2}[s]
        for cdeg in (0.0, 30.0, 60.0, B_SWING, -30.0, -60.0, -B_SWING):
            run('slot %+d kurbel %+d' % (s, cdeg), pose(parts, s, idx, cdeg))
    for k, (v, tag) in sorted(worst.items(), key=lambda kv: -kv[1][0]):
        msgs.append('KOLLISION %-22s x %-22s %8.2f mm3  (%s)' % (k[0], k[1], v, tag))
    if not msgs:
        msgs.append('keine Kollisionen')
    return msgs


TOP = '50_Gondel_V2'
PRINT = '51_Druckteile_V2'


def _new_comp(parent, name):
    occ = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    occ.component.name = name
    return occ


def _add_bodies(comp, bodies):
    bf = comp.features.baseFeatures.add()
    bf.startEdit()
    res = []
    for nm, b in bodies:
        nb = comp.bRepBodies.add(b, bf)
        nb.name = nm
        res.append(nb)
    bf.finishEdit()
    return res


def commit(parts, slot=0.0, pen_down=1, crank_deg=B_SWING):
    des = adsk.fusion.Design.cast(app.activeProduct)
    root = des.rootComponent
    for o in list(root.occurrences):
        if o.component.name in (TOP,):
            o.deleteMe()
    top = _new_comp(root, TOP)
    groups = {}
    for name, grp, b in pose(parts, slot, pen_down, crank_deg):
        groups.setdefault(name, []).append(b)
    for name in sorted(groups):
        occ = _new_comp(top.component, name)
        _add_bodies(occ.component, [('%s_%d' % (name, i + 1) if len(groups[name]) > 1 else name, b) for i, b in enumerate(groups[name])])
    return len(groups)


# Drucklage: Liste von Matrizen, danach wird auf z=0 abgesetzt
def print_bodies(parts):
    flip = m_rot(180, (1, 0, 0))
    out = []
    out.append(('01_Grundplatte', parts['01_Grundplatte']['fix'][0], [], 1))
    out.append(('02_Lagerbock', parts['02_Lagerbock']['fix'][0], [], 1))
    out.append(('03_Hornanker_B_mittel', parts['03_Hornanker']['fix'][0], [], 1))
    out.append(('03_Hornanker_A_eng', part_hornanker(5.5, 3.9), [], 1))
    out.append(('03_Hornanker_C_weit', part_hornanker(6.1, 4.5), [], 1))
    out.append(('04_Trommel_unten', parts['04_Trommel_unten']['drum'][0], [flip], 1))
    out.append(('05_Lagerring', parts['05_Lagerring']['drum'][0], [], 1))
    out.append(('06_Trommel_oben', parts['06_Trommel_oben']['drum'][0], [flip], 1))
    out.append(('07_Stiftschlitten_3x', parts['07_Stiftschlitten']['car'], [], 3))
    out.append(('08_Parkring', parts['08_Parkring']['fix'][0], [flip], 1))
    out.append(('09_Stuetze_4x', parts['09_Stuetze']['proto'], [], 4))
    out.append(('10_Hubtraeger', parts['10_Hubtraeger']['fix'][0], [], 1))
    out.append(('11_Hubschlitten', parts['11_Hubschlitten']['lift'][0], [flip], 1))
    out.append(('12_Kopfstueck', parts['12_Kopfstueck']['fix'][0], [], 1))
    out.append(('13_Pleuel_14.00', parts['13_Pleuel']['link'], [m_rot(90, (0, 1, 0))], 1))
    for dl, nm in ((-0.3, '13.70'), (-0.15, '13.85'), (0.15, '14.15'), (0.3, '14.30')):
        out.append(('13_Pleuel_' + nm, part_pleuel(LINK_L + dl), [m_rot(90, (0, 1, 0))], 1))
    out.append(('14_Kurbel', parts['14_Kurbel']['crank'][0], [m_rot(-90, (0, 1, 0))], 1))
    out.append(('15_Riemenarm', parts['15_Riemenarm']['fix'][0], [], 1))
    out.append(('16_Riemendeckel', parts['16_Riemendeckel']['fix'][0], [], 1))
    out.append(('24_Riemenklemme_2x', parts['24_Riemenklemme']['proto'], [], 2))
    for d, nm in ((11.0, '11mm'), (13.0, '13mm'), (15.0, '15mm'), (16.8, '17mm_Lumocolor')):
        out.append(('21_Stifteinsatz_%s_3x' % nm, part_einsatz(d), [flip], 3))
    out.append(('17_Gleitfuss_3x', parts['17_Gleitfuss']['proto'], [flip], 3))
    out.append(('20_Einstelllehre_5mm', box(0, 30, 0, 20, 0, PEN_STROKE), [], 1))
    out.append(('18_Servo_Distanz_1mm_4x', part_distanz(), [], 4))
    out.append(('19_Passprobe', part_passprobe(), [], 1))
    res = []
    for name, body, mats, n in out:
        b = cp(body)
        xf(b, *mats)
        bb = bbox(b)
        xf(b, m_tr(-(bb[0] + bb[3]) / 2.0, -(bb[1] + bb[4]) / 2.0, -bb[2]))
        res.append((name, b, n))
    return res


def part_distanz():
    d = box(-2.4, 2.4, -6.1, 6.1, 0, 1.0)
    C(d, cyl(0, 0, -1, 2, F['m2'] / 2))
    return d


def part_passprobe():
    """Kompaktes Probestueck fuer alle Passungen (6 mm dick). Von links nach rechts:
    7 Loecher (2,2 / 2,35 / 2,5 / 2,0 / 2,1 / 1,65 / 1,85), 3 M3-Taschen (5,6 / 5,75 / 5,9),
    2 M2-Taschen (4,1 / 4,2), am rechten Ende ein M3- und ein M2-Mutterschlitz. An der linken
    Stirnseite ein liegendes 2,05-mm-Loch. Seitlich der Lagersitz mit Zapfen."""
    L, W, T = 92.0, 13.0, 6.0
    pp = box(0, L, 0, W, 0, T)
    xs = 4.5
    for dia in (2.2, 2.35, 2.5, 2.0, 2.1, 1.65, 1.85):
        C(pp, cyl(xs, 5.0, -1, T + 1, dia / 2))
        xs += 5.0
    xs = 43.0
    for af in (5.6, 5.75, 5.9):
        C(pp, cyl(xs, 6.5, -1, T + 1, F['m3'] / 2))
        C(pp, hexz(xs, 6.5, T - F['m3_nh'], T + 1, af))
        xs += 9.0
    xs = 69.0
    for af in (4.1, 4.2):
        C(pp, cyl(xs, 6.5, -1, T + 1, F['m2'] / 2))
        C(pp, hexz(xs, 6.5, T - F['m2_nh'], T + 1, af))
        xs += 7.0
    # liegendes Loch fuer den Mitnehmerstift
    C(pp, cylp((-1, 10.5, 3.0), (7.5, 10.5, 3.0), F['pin2_press_h'] / 2))
    # Mutterschlitze von der rechten Stirnseite, je mit Schraubenloch von oben
    C(pp, box(84.5, L + 1, 3.6 - F['m3_slot'] / 2, 3.6 + F['m3_slot'] / 2, 1.6, 1.6 + F['m3_nh']))
    C(pp, cyl(88.0, 3.6, -1, T + 1, F['m3'] / 2))
    C(pp, box(85.5, L + 1, 10.0 - F['m2_af'] / 2, 10.0 + F['m2_af'] / 2, 1.6, 1.6 + F['m2_nh']))
    C(pp, cyl(88.5, 10.0, -1, T + 1, F['m2'] / 2))
    # Lagersitz als Ring mit Zapfen, seitlich angesetzt
    cx, cy = 26.0, W + 12.6
    ring = cyl(cx, cy, 0, 5.0, 13.8)
    C(ring, cyl(cx, cy, 1.2, 6.0, F['brg_od'] / 2))
    U(ring, cyl(cx, cy, 1.2, 8.0, F['stub'] / 2))
    U(pp, ring)
    return pp


def commit_print(parts):
    des = adsk.fusion.Design.cast(app.activeProduct)
    root = des.rootComponent
    for o in list(root.occurrences):
        if o.component.name == PRINT:
            o.deleteMe()
    top = _new_comp(root, PRINT)
    pb = print_bodies_all() if parts is None else print_bodies(parts)
    x = 0.0
    made = []
    for name, b, n in pb:
        bb = bbox(b)
        w = bb[3] - bb[0]
        xf(b, m_tr(x + w / 2.0, -260.0, 0))
        x += w + 8.0
        occ = _new_comp(top.component, 'D_' + name)
        _add_bodies(occ.component, [(name, b)])
        made.append((name, occ.component.bRepBodies.item(0), n))
    return made


def export_stl(parts, folder):
    des = adsk.fusion.Design.cast(app.activeProduct)
    made = commit_print(parts)
    em = des.exportManager
    if not os.path.isdir(folder):
        os.makedirs(folder)
    res = []
    for name, body, n in made:
        path = os.path.join(folder, name + '.stl')
        opt = em.createSTLExportOptions(body, path)
        opt.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
        opt.isBinaryFormat = True
        opt.sendToPrintUtility = False
        ok = em.execute(opt)
        if not ok or not os.path.isfile(path):
            raise RuntimeError('STL-Export fehlgeschlagen: ' + name)
        bb = bbox(body)
        res.append('%-28s %6.1f x %6.1f x %5.1f mm  %6.1f cm3  x%d' % (name, bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2], body.volume, n))
    return res


def report(parts):
    lines = []
    for deg in (0.0, B_SWING):
        py, pz, jz = crank_state(deg)
        lines.append('Kurbel %5.1f Grad: Zapfen y=%.2f z=%.2f, Gelenk z=%.2f, Pad-Oberseite z=%.2f' % (deg, py, pz, jz, Z_SHELF + jz - JOINT_Z_UP))
    lines.append('Parkring-Oberseite z=%.2f, Stift unten: Mitnehmer-Unterkante z=%.2f, Festanschlag z=%.2f' % (Z_SHELF, Z_CAR_DN + PIN_Z - 1, Z_D1T + PIN_Z - 1))
    lines.append('Feder: Einbaulaenge oben %.1f, unten %.1f, Festanschlag %.1f' % (Z_D2B - (Z_CAR_UP + CAR_H) + SPRING_POCKET, Z_D2B - (Z_CAR_DN + CAR_H) + SPRING_POCKET, Z_D2B - (Z_D1T + CAR_H) + SPRING_POCKET))
    lines.append('Stangen 2 mm: Fuehrung %.1f mm; Stangen 1,5 mm: Fuehrung %.1f mm, Hub %.1f mm' % (Z_D2B + 4 - (Z_D1T - 4), Z_D2B + 4 - (Z_D1T - 4), 70 - 20.5))
    for name, d in parts.items():
        for key in ('fix', 'drum', 'lift', 'crank'):
            for b in d.get(key, []):
                bb = bbox(b)
                lines.append('%-22s vol %6.1f cm3  bbox %.1f %.1f %.1f .. %.1f %.1f %.1f' % (name, b.volume, bb[0], bb[1], bb[2], bb[3], bb[4], bb[5]))
    return lines


def main(mode='check'):
    parts = build()
    out = []
    if 'report' in mode:
        out += report(parts)
    if 'check' in mode:
        out += check(parts)
    if 'commit' in mode:
        n = commit(parts)
        out.append('commit: %d Komponenten' % n)
    if 'export' in mode:
        out += export_stl(parts, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'STL'))
    if 'bom' in mode:
        for k in sorted(BOM):
            out.append('BOM %-12s %d' % (k, BOM[k]))
    print('\n'.join(out))
    return parts


def export_anim(parts, folder):
    """Exportiert die Baugruppen fuer die Web-Animation: Trommel bei Slot 0, alle Stifte
    oben, Hub oben. Jede Gruppe wird ein Koerper in einer Hilfskomponente."""
    des = adsk.fusion.Design.cast(app.activeProduct)
    root = des.rootComponent
    name = '52_Anim_V2'
    for o in list(root.occurrences):
        if o.component.name == name:
            o.deleteMe()
    groups = {}

    def add(key, body):
        groups.setdefault(key, []).append(body)

    ss = parts['07_Stiftschlitten']['car']
    pin, pen = ref_pin(), ref_pen()
    for nm, grp, b in pose(parts, 0.0, None, 0.0, refs=True):
        if nm in ('07_Stiftschlitten', 'REF_Mitnehmerstift', 'REF_Lumocolor', 'REF_608ZZ'):
            continue
        if nm == '11_Hubschlitten':
            add('lifter', b)
        elif nm == '13_Pleuel':
            add('link', b)
        elif nm in ('14_Kurbel', 'REF_Horn_B'):
            add('crank', b)
        elif nm == 'REF_Servo_B':
            add('servo_b', b)
        elif nm == 'REF_Servo_A':
            add('servo_a', b)
        elif nm == 'REF_Horn_A':
            add('horn_a', b)
        elif grp == 'drum':
            add('drum', b)
        elif nm == '08_Parkring':
            add('ring', b)
        else:
            add('fixed', b)
    for i, k in enumerate(SLOTS):
        add('car%d' % i, drum_to_world(place_schlitten(ss, k, Z_CAR_UP), 0.0))
        add('pin%d' % i, drum_to_world(place_schlitten(pin, k, Z_CAR_UP), 0.0))
        add('pen%d' % i, drum_to_world(place_schlitten(pen, k, Z_CAR_UP), 0.0))
    top = _new_comp(root, name)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    em = des.exportManager
    out = []
    for key in sorted(groups):
        occ = _new_comp(top.component, 'A_' + key)
        made = _add_bodies(occ.component, [('%s_%d' % (key, i), b) for i, b in enumerate(groups[key])])
        path = os.path.join(folder, key + '.stl')
        opt = em.createSTLExportOptions(occ, path)
        opt.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementLow
        opt.isBinaryFormat = True
        opt.sendToPrintUtility = False
        em.execute(opt)
        out.append('%s %d' % (key, len(made)))
    top.isLightBulbOn = False
    return out


def write_stl(body, path):
    """Vernetzt einen (auch temporaeren) Koerper und schreibt eine binaere STL in mm.
    Braucht keinen Schreibzugriff auf die Fusion-Datei."""
    import struct
    calc = body.meshManager.createMeshCalculator()
    calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.VeryHighQualityTriangleMesh)
    mesh = calc.calculate()
    co = mesh.nodeCoordinatesAsDouble
    idx = mesh.nodeIndices
    n = len(idx) // 3
    with open(path, 'wb') as f:
        f.write(b'W-BOT Gondel V2'.ljust(80, b' '))
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


def export_stl_ro(parts, folder, nozzle=None):
    """STL-Export der Druckteile ohne Aenderung an der Fusion-Datei (Nur-Lese-Lauf).
    nozzle '08' oder '04' exportiert nur die Teile dieser Duese; vorher set_fits() und build()."""
    if not os.path.isdir(folder):
        os.makedirs(folder)
    res = []
    for name, b, n in print_bodies(parts):
        if nozzle and nozzle_of(name) != nozzle:
            continue
        path = os.path.join(folder, name + '.stl')
        tri = write_stl(b, path)
        bb = bbox(b)
        res.append('%-34s %6.1f x %6.1f x %5.1f mm  %6.1f cm3  %6d Dreiecke  x%d' % (name, bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2], b.volume, tri, n))
    return res
