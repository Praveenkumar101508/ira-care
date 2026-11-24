"""IRA CARE watch: technical blueprint sheets (design intent, millimetres).

Draws five A3 landscape sheets as SVG from the same parameters as
scripts/hardware/build_watch.py:

  1  General arrangement: front, right side, back and top views, dimensioned
  2  Section A-A: the internal stack, wall and layer thicknesses
  3  Exploded view: numbered parts and the parts list
  4  Electronics: block diagram, buses and the software stack
  5  Charging dock and strap

Usage: python3 blueprint.py OUT_DIR [blue|print]
"""
import math
import sys
from pathlib import Path

P = {
    "case_diameter": 46.0, "case_height": 12.6, "case_wall": 2.2, "case_bevel": 1.6,
    "glass_diameter": 40.0, "glass_thickness": 1.0, "glass_dome": 0.6,
    "display_diameter": 35.3, "display_thickness": 0.8,
    "frame_thickness": 1.0,
    "board_diameter": 36.0, "board_thickness": 1.0,
    "battery_w": 30.0, "battery_d": 24.0, "battery_h": 4.6,
    "sensor_board_diameter": 22.0, "sensor_board_thickness": 0.8,
    "back_thickness": 1.4, "sensor_window_diameter": 16.0,
    "help_button": (11.0, 4.5, 3.0), "side_button": (6.5, 3.0, 2.2),
    "lug_width": 22.0, "strap_width": 22.0, "strap_thickness": 2.6, "strap_length": 110.0,
    "dock_diameter": 62.0, "dock_height": 16.0,
    "coil_diameter": 28.0, "bezel_diameter": 43.0, "lug_length": 6.0,
}

THEMES = {
    "blue": {"bg": "#0B3A66", "ink": "#FFFFFF", "faint": "#7FA3C7", "grid": "#164B7E", "hatch": "#C9DAEC"},
    "print": {"bg": "#FFFFFF", "ink": "#111111", "faint": "#777777", "grid": "#FFFFFF", "hatch": "#444444"},
}

W, H = 1680, 1188          # A3 landscape at 4 px per sheet millimetre
FONT = "DejaVu Sans Condensed, DejaVu Sans, Arial Narrow, Arial, sans-serif"


class Sheet:
    def __init__(self, theme, number, title, scale_note):
        self.t = THEMES[theme]
        self.number, self.title, self.scale_note = number, title, scale_note
        self.parts = []

    # primitives -------------------------------------------------------------
    def add(self, s):
        self.parts.append(s)

    def line(self, x1, y1, x2, y2, w=1.6, dash=None, col=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                 f'stroke="{col or self.t["ink"]}" stroke-width="{w}"{d} stroke-linecap="round"/>')

    def circle(self, cx, cy, r, w=1.6, dash=None, fill="none", col=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}" '
                 f'stroke="{col or self.t["ink"]}" stroke-width="{w}"{d}/>')

    def ellipse(self, cx, cy, rx, ry, w=1.6, dash=None, fill="none"):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" fill="{fill}" '
                 f'stroke="{self.t["ink"]}" stroke-width="{w}"{d}/>')

    def path(self, d, w=1.6, dash=None, fill="none"):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<path d="{d}" fill="{fill}" stroke="{self.t["ink"]}" stroke-width="{w}"{da} '
                 f'stroke-linejoin="round"/>')

    def rect(self, x, y, w_, h_, w=1.6, dash=None, rx=0, fill="none"):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w_:.1f}" height="{h_:.1f}" rx="{rx}" '
                 f'fill="{fill}" stroke="{self.t["ink"]}" stroke-width="{w}"{d}/>')

    def text(self, x, y, s, size=15, anchor="start", weight="normal", col=None, rot=None):
        tr = f' transform="rotate({rot} {x:.1f} {y:.1f})"' if rot is not None else ""
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
                 f'font-weight="{weight}" text-anchor="{anchor}" fill="{col or self.t["ink"]}"{tr}>{s}</text>')

    def arrow(self, x, y, ang, size=9):
        a1, a2 = ang + math.radians(160), ang - math.radians(160)
        pts = f"{x:.1f},{y:.1f} {x + size*math.cos(a1):.1f},{y + size*math.sin(a1):.1f} " \
              f"{x + size*math.cos(a2):.1f},{y + size*math.sin(a2):.1f}"
        self.add(f'<polygon points="{pts}" fill="{self.t["ink"]}"/>')

    # dimensioning -----------------------------------------------------------
    def dim_h(self, x1, x2, y_feat1, y_feat2, y, label, size=15):
        """Horizontal dimension between x1 and x2, drawn at y, extension lines from features."""
        for x, yf in ((x1, y_feat1), (x2, y_feat2)):
            s = 1 if y > yf else -1
            self.line(x, yf + 4 * s, x, y + 6 * s, w=0.9)
        self.line(x1, y, x2, y, w=1.1)
        self.arrow(x1, y, math.pi)
        self.arrow(x2, y, 0)
        self.text((x1 + x2) / 2, y - 7, label, size=size, anchor="middle")

    def dim_v(self, y1, y2, x_feat1, x_feat2, x, label, size=15, side="right", rotated=False):
        for yy, xf in ((y1, x_feat1), (y2, x_feat2)):
            s = 1 if x > xf else -1
            self.line(xf + 4 * s, yy, x + 6 * s, yy, w=0.9)
        self.line(x, y1, x, y2, w=1.1)
        self.arrow(x, y1, -math.pi / 2)
        self.arrow(x, y2, math.pi / 2)
        if rotated:
            self.text(x - 8, (y1 + y2) / 2, label, size=size, anchor="middle", rot=-90)
        elif side == "right":
            self.text(x + 10, (y1 + y2) / 2 + 5, label, size=size)
        else:
            self.text(x - 10, (y1 + y2) / 2 + 5, label, size=size, anchor="end")

    def leader(self, x, y, tx, ty, label, size=14, anchor=None):
        self.circle(x, y, 2.6, w=0, fill=self.t["ink"])
        self.line(x, y, tx, ty, w=0.9)
        anchor = anchor or ("start" if tx >= x else "end")
        self.line(tx, ty, tx + (40 if anchor == "start" else -40), ty, w=0.9)
        self.text(tx + (44 if anchor == "start" else -44), ty + 5, label, size=size, anchor=anchor)

    def balloon(self, x, y, n, tx, ty):
        self.circle(x, y, 2.6, w=0, fill=self.t["ink"])
        ang = math.atan2(ty - y, tx - x)
        self.line(x, y, tx - 17 * math.cos(ang), ty - 17 * math.sin(ang), w=0.9)
        self.circle(tx, ty, 17, w=1.4)
        self.text(tx, ty + 6, str(n), size=16, anchor="middle", weight="bold")

    def centre_mark(self, cx, cy, r):
        self.line(cx - r, cy, cx + r, cy, w=0.7, dash="18 4 3 4", col=self.t["faint"])
        self.line(cx, cy - r, cx, cy + r, w=0.7, dash="18 4 3 4", col=self.t["faint"])

    def hatch_rect(self, x, y, w_, h_, gap=7):
        cid = f"c{len(self.parts)}"
        self.add(f'<clipPath id="{cid}"><rect x="{x:.1f}" y="{y:.1f}" width="{w_:.1f}" height="{h_:.1f}"/></clipPath>')
        g = [f'<g clip-path="url(#{cid})">']
        k = -h_
        while k < w_ + h_:
            g.append(f'<line x1="{x + k:.1f}" y1="{y + h_:.1f}" x2="{x + k + h_:.1f}" y2="{y:.1f}" '
                     f'stroke="{self.t["hatch"]}" stroke-width="0.8"/>')
            k += gap
        g.append("</g>")
        self.add("".join(g))

    # frame ------------------------------------------------------------------
    def svg(self):
        t = self.t
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
               f'<rect width="{W}" height="{H}" fill="{t["bg"]}"/>']
        if t["grid"] != t["bg"]:
            for gx in range(0, W, 40):
                out.append(f'<line x1="{gx}" y1="0" x2="{gx}" y2="{H}" stroke="{t["grid"]}" stroke-width="1"/>')
            for gy in range(0, H, 40):
                out.append(f'<line x1="0" y1="{gy}" x2="{W}" y2="{gy}" stroke="{t["grid"]}" stroke-width="1"/>')
        out.append(f'<rect x="24" y="24" width="{W-48}" height="{H-48}" fill="none" stroke="{t["ink"]}" stroke-width="3"/>')
        out.append(f'<rect x="34" y="34" width="{W-68}" height="{H-68}" fill="none" stroke="{t["ink"]}" stroke-width="1"/>')
        out.extend(self.parts)
        out.extend(self._title_block())
        out.append("</svg>")
        return "\n".join(out)

    def _title_block(self):
        t = self.t
        x0, y0, w_, h_ = W - 34 - 620, H - 34 - 150, 620, 150
        s = []
        ink = t["ink"]

        def tx(x, y, txt, size=14, weight="normal"):
            txt = txt.replace("&", "&amp;")
            s.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{ink}">{txt}</text>')
        s.append(f'<rect x="{x0}" y="{y0}" width="{w_}" height="{h_}" fill="{t["bg"]}" stroke="{ink}" stroke-width="2"/>')
        for yy in (y0 + 52, y0 + 96):
            s.append(f'<line x1="{x0}" y1="{yy}" x2="{x0 + w_}" y2="{yy}" stroke="{ink}" stroke-width="1"/>')
        for xx in (x0 + 230, x0 + 420):
            s.append(f'<line x1="{xx}" y1="{y0 + 52}" x2="{xx}" y2="{y0 + 96}" stroke="{ink}" stroke-width="1"/>')
        s.append(f'<line x1="{x0 + 420}" y1="{y0 + 96}" x2="{x0 + 420}" y2="{y0 + h_}" stroke="{ink}" stroke-width="1"/>')
        tx(x0 + 14, y0 + 22, "IRA CARE WATCH", 15, "bold")
        tx(x0 + 14, y0 + 44, self.title, 19, "bold")
        tx(x0 + 14, y0 + 72, "SupraCloud · IRA CARE", 13)
        tx(x0 + 14, y0 + 89, "Drawn: Praveen Kamineti", 13)
        tx(x0 + 244, y0 + 72, f"Scale {self.scale_note}", 13)
        tx(x0 + 244, y0 + 89, "Units: millimetres", 13)
        tx(x0 + 434, y0 + 72, f"Sheet {self.number} of 5", 13)
        tx(x0 + 434, y0 + 89, "Rev A · 24 Nov 2026", 13)
        tx(x0 + 14, y0 + 118, "DESIGN INTENT. NOT FOR MANUFACTURE.", 13, "bold")
        tx(x0 + 14, y0 + 137, "Dimensions to be confirmed by a hardware engineer.", 12)
        tx(x0 + 434, y0 + 118, "Third-angle", 12)
        tx(x0 + 434, y0 + 137, "projection", 12)
        # third-angle symbol
        cx, cy = x0 + 560, y0 + 123
        s.append(f'<path d="M{cx-38} {cy-10} L{cx-12} {cy-16} L{cx-12} {cy+16} L{cx-38} {cy+10} Z" fill="none" stroke="{ink}" stroke-width="1.2"/>')
        s.append(f'<circle cx="{cx+18}" cy="{cy}" r="16" fill="none" stroke="{ink}" stroke-width="1.2"/>')
        s.append(f'<circle cx="{cx+18}" cy="{cy}" r="7" fill="none" stroke="{ink}" stroke-width="1.2"/>')
        return s


def heading(sh, x, y, s, sub=None):
    sh.text(x, y, s, size=17, weight="bold")
    sh.line(x, y + 7, x + 8.4 * len(s), y + 7, w=1.2)
    if sub:
        sh.text(x, y + 28, sub, size=13, col=sh.t["faint"])


# ---------------------------------------------------------------------------
# Sheet 1: general arrangement
# ---------------------------------------------------------------------------
def sheet1(theme):
    sh = Sheet(theme, 1, "GENERAL ARRANGEMENT", "2:1; back 1.2:1")
    k = 8.0                                   # 2:1 on a sheet drawn at 4 px per millimetre
    R = P["case_diameter"] / 2 * k
    gr = P["glass_diameter"] / 2 * k
    dr = P["display_diameter"] / 2 * k
    br = P["bezel_diameter"] / 2 * k
    lug = P["lug_width"] * k
    lug_out = 4.4 * k                         # lug reach beyond the case edge in plan
    hh = P["case_height"] * k
    # ---------------- front view
    cx, cy = 400, 430
    heading(sh, 120, 96, "FRONT VIEW")
    for s_ in (-1, 1):
        y_edge = cy + s_ * (R * 0.78)
        y_tip = cy + s_ * (R + lug_out)
        sh.path(f"M{cx - lug/2:.1f} {y_edge:.1f} L{cx - lug/2:.1f} {y_tip:.1f} L{cx + lug/2:.1f} {y_tip:.1f} L{cx + lug/2:.1f} {y_edge:.1f}")
        sh.path(f"M{cx - lug/2 + 4:.1f} {y_tip:.1f} L{cx - lug/2 + 4:.1f} {y_tip + s_*34:.1f} M{cx + lug/2 - 4:.1f} {y_tip:.1f} L{cx + lug/2 - 4:.1f} {y_tip + s_*34:.1f}", w=1.0, dash="7 5")
    sh.circle(cx, cy, R, w=2.4, fill=sh.t["bg"])
    sh.circle(cx, cy, br, w=1.4)
    sh.circle(cx, cy, gr, w=1.6)
    sh.circle(cx, cy, dr, w=1.0, dash="10 6")
    sh.centre_mark(cx, cy, R + 24)
    for ang_deg, (L, Wd, Pr) in ((-30, P["help_button"]), (30, P["side_button"])):
        a = math.radians(ang_deg)
        bx, by = cx + R * math.cos(a), cy + R * math.sin(a)
        ux, uy = math.cos(a), math.sin(a)
        vx, vy = -uy, ux
        hl, pr = L / 2 * k, Pr * k
        pts = [(bx - vx*hl - ux*2, by - vy*hl - uy*2), (bx + vx*hl - ux*2, by + vy*hl - uy*2),
               (bx + vx*hl + ux*pr, by + vy*hl + uy*pr), (bx - vx*hl + ux*pr, by - vy*hl + uy*pr)]
        sh.path("M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + " Z", w=1.8, fill=sh.t["bg"])
    # section line A-A
    sh.line(cx - R - 44, cy, cx - R - 12, cy, w=2.6)
    sh.line(cx + R + 12, cy, cx + R + 44, cy, w=2.6)
    sh.text(cx - R - 28, cy - 12, "A", size=18, weight="bold", anchor="middle")
    sh.text(cx + R + 28, cy - 12, "A", size=18, weight="bold", anchor="middle")
    # dimensions: top (lugs, glass), bottom (case), left (display, bezel) rotated
    sh.dim_h(cx - lug/2, cx + lug/2, cy - R - lug_out, cy - R - lug_out, cy - R - lug_out - 22, "22.0 lug width")
    sh.dim_h(cx - gr, cx + gr, cy, cy, cy - R - lug_out - 62, "⌀40.0 cover glass")
    sh.dim_h(cx - R, cx + R, cy, cy, cy + R + lug_out + 40, "⌀46.0 case")
    sh.dim_v(cy - dr, cy + dr, cx, cx, cx - R - 62, "⌀35.3 active display", rotated=True)
    sh.dim_v(cy - br, cy + br, cx, cx, cx - R - 112, "⌀43.0 bezel ring", rotated=True)
    a = math.radians(-30)
    sh.leader(cx + (R + 14) * math.cos(a), cy + (R + 14) * math.sin(a), cx + R + 56, cy - R + 6, "HELP 11.0 × 4.5 (2 o'clock)")
    a = math.radians(30)
    sh.leader(cx + (R + 10) * math.cos(a), cy + (R + 10) * math.sin(a), cx + R + 56, cy + R - 6, "Side 6.5 × 3.0 (4 o'clock)")
    sh.text(cx, cy + R + lug_out + 74, "Display 1.39 in round AMOLED, 466 × 466. Dashed: active area.", size=12.5, anchor="middle", col=sh.t["faint"])

    # ---------------- right side view
    sx, sy = 1150, 400
    heading(sh, 870, 96, "RIGHT SIDE VIEW (Help side)")
    top = sy - hh / 2
    bev = P["case_bevel"] * k
    bt = P["back_thickness"] * k
    sh.path(f"M{sx - R:.1f} {top + bev:.1f} Q{sx - R:.1f} {top:.1f} {sx - R + bev:.1f} {top:.1f} "
            f"L{sx + R - bev:.1f} {top:.1f} Q{sx + R:.1f} {top:.1f} {sx + R:.1f} {top + bev:.1f} "
            f"L{sx + R:.1f} {top + hh - bt:.1f} L{sx - R:.1f} {top + hh - bt:.1f} Z", w=2.4)
    sh.path(f"M{sx - gr:.1f} {top:.1f} Q{sx:.1f} {top - P['glass_dome']*k*2:.1f} {sx + gr:.1f} {top:.1f}", w=1.6)
    sh.path(f"M{sx - R + 6:.1f} {top + hh - bt:.1f} L{sx - R + 10:.1f} {top + hh:.1f} L{sx + R - 10:.1f} {top + hh:.1f} L{sx + R - 6:.1f} {top + hh - bt:.1f}", w=1.6)
    sw = P["sensor_window_diameter"] / 2 * k
    sh.path(f"M{sx - sw:.1f} {top + hh:.1f} Q{sx:.1f} {top + hh + 0.8*k*2:.1f} {sx + sw:.1f} {top + hh:.1f}", w=1.4)
    hb, sb = P["help_button"], P["side_button"]
    hbx = sx - 60 - hb[0] / 2 * k
    sh.rect(hbx, top + 2.4 * k, hb[0] * k, hb[1] * k, w=1.8, rx=4)
    sbx = sx + 50
    sh.rect(sbx, top + 4.4 * k, sb[0] * k, sb[1] * k, w=1.4, rx=3)
    for s_ in (-1, 1):
        x_edge = sx + s_ * R
        ll = P["lug_length"] * k
        sh.path(f"M{x_edge:.1f} {top + 2.6*k:.1f} L{x_edge + s_*ll:.1f} {top + 3.6*k:.1f} L{x_edge + s_*ll:.1f} {top + 8.4*k:.1f} L{x_edge:.1f} {top + 9.4*k:.1f}", w=1.6)
        sh.path(f"M{x_edge + s_*ll:.1f} {top + 4.8*k:.1f} q{s_*22:.1f} 4 {s_*36:.1f} 36", w=1.0, dash="7 5")
    sh.dim_h(sx - R, sx + R, top + hh, top + hh, top + hh + 54, "46.0")
    sh.dim_v(top, top + hh, sx - R - P["lug_length"] * k, sx - R, sx - R - 90, "12.6", side="left")
    sh.leader(hbx + 12, top + 2.4 * k + 8, sx - R + 40, top - 74, "HELP cap (coral), sealed tactile switch", anchor="start")
    sh.leader(sbx + sb[0] * k - 8, top + 4.4 * k + 8, sx + R - 20, top - 40, "Side button, sealed")
    sh.leader(sx + 10, top + hh + 6, sx + 70, top + hh + 112, "⌀16.0 sensor window: heart rate, skin temperature")
    sh.text(sx + 30, top - 20, "0.6 glass dome", size=12.5, col=sh.t["faint"])

    # ---------------- back view (reduced)
    kb = 4.8
    bx, by = 330, 930
    heading(sh, 120, 772, "BACK VIEW (1.2:1)")
    rb = P["case_diameter"] / 2 * kb
    sh.circle(bx, by, rb, w=2.2)
    sh.circle(bx, by, P["coil_diameter"] / 2 * kb, w=1.0, dash="9 5")
    sh.circle(bx, by, P["sensor_window_diameter"] / 2 * kb, w=1.6)
    for i in range(6):
        a = math.radians(i * 60)
        sh.circle(bx + 13 * math.cos(a), by + 13 * math.sin(a), 2.4, w=1.0)
    sh.centre_mark(bx, by, rb + 18)
    sh.leader(bx + P["coil_diameter"] / 2 * kb * 0.71, by - P["coil_diameter"] / 2 * kb * 0.71, bx + rb + 40, by - rb + 4, "⌀28 Qi coil behind the back (hidden)")
    sh.leader(bx + 4, by + 4, bx + rb + 40, by + rb - 30, "Optical window: LEDs, photodiodes")

    # ---------------- top view
    tx_, ty_ = 860, 940
    heading(sh, 680, 772, "TOP VIEW (12 o'clock edge, 2:1)")
    sh.rect(tx_ - R, ty_ - hh/2, 2*R, hh, w=2.2, rx=10)
    sh.rect(tx_ - lug/2, ty_ - hh/2 - 3, lug, hh + 6, w=1.2, dash="8 5")
    sh.line(tx_ - R + 18, ty_, tx_ + R - 18, ty_, w=0.9, dash="4 6")
    sh.text(tx_, ty_ - hh/2 - 18, "Antenna slot in the case wall (dotted)", size=12.5, anchor="middle", col=sh.t["faint"])
    sh.dim_h(tx_ - lug/2, tx_ + lug/2, ty_ + hh/2, ty_ + hh/2, ty_ + hh/2 + 44, "22.0 strap")

    # ---------------- notes
    nx, ny = 1110, 660
    sh.text(nx, ny, "NOTES", size=16, weight="bold")
    notes = ["1. Case 316L steel or Grade 2 titanium; nickel",
             "   release below 0.5 µg/cm² per week.",
             "2. IP68: gasket under the glass, O-rings on",
             "   the back cover and both buttons.",
             "3. Glass: Gorilla Glass DX+ or sapphire.",
             "4. The Help cap is the only coral part and is",
             "   found by touch against the bezel ring.",
             "5. Weight target 48 g with strap (estimate).",
             "6. Section A-A on sheet 2."]
    for i, n in enumerate(notes):
        sh.text(nx, ny + 26 + i * 21, n, size=13)
    return sh


# ---------------------------------------------------------------------------
# Sheet 2: section A-A
# ---------------------------------------------------------------------------
STACK = [  # bottom to top: name, thickness, width (mm)
    ("Back cover", P["back_thickness"], P["case_diameter"] - 6),
    ("Sensor board", P["sensor_board_thickness"], P["sensor_board_diameter"]),
    ("Battery", P["battery_h"], P["battery_w"]),
    ("Logic board", P["board_thickness"], P["board_diameter"]),
    ("Display frame", P["frame_thickness"], P["display_diameter"] + 2),
    ("Display", P["display_thickness"], P["display_diameter"]),
    ("Cover glass", P["glass_thickness"], P["glass_diameter"]),
]


def sheet2(theme):
    sh = Sheet(theme, 2, "SECTION A-A · INTERNAL STACK", "4.25:1")
    k = 17.0
    cx, base = 860, 860
    heading(sh, 120, 120, "SECTION A-A", "Cut through the centre, 9 to 3 o'clock (sheet 1). Case walls and back cover hatched.")
    R = P["case_diameter"] / 2 * k
    hh = P["case_height"] * k
    wall = P["case_wall"] * k
    top = base - hh
    # case walls (hatched) left and right
    for s in (-1, 1):
        x_out = cx + s * R
        x_in = cx + s * (R - wall)
        x0 = min(x_out, x_in)
        sh.hatch_rect(x0, top + P["case_bevel"] * k * 0.4, wall, hh - P["back_thickness"] * k - P["case_bevel"] * k * 0.4)
        sh.rect(x0, top + P["case_bevel"] * k * 0.4, wall, hh - P["back_thickness"] * k - P["case_bevel"] * k * 0.4, w=2.0)
    # layers
    y = base
    gaps = {"Back cover": 0.0, "Sensor board": 0.3, "Battery": 0.4, "Logic board": 0.4,
            "Display frame": 0.5, "Display": 0.0, "Cover glass": 0.0}
    ylist = []
    for name, t, wd in STACK:
        y -= gaps[name] * k
        h_ = t * k
        x0 = cx - wd / 2 * k
        if name == "Back cover":
            sh.hatch_rect(cx - R + 6, y - h_, 2 * R - 12, h_, gap=9)
            sh.rect(cx - R + 6, y - h_, 2 * R - 12, h_, w=2.0)
            sw = P["sensor_window_diameter"] / 2 * k
            sh.path(f"M{cx - sw:.1f} {y:.1f} Q{cx:.1f} {y + 0.8*k*2:.1f} {cx + sw:.1f} {y:.1f}", w=1.6)
        elif name == "Cover glass":
            sh.path(f"M{x0:.1f} {y:.1f} L{x0:.1f} {y - h_:.1f} Q{cx:.1f} {y - h_ - P['glass_dome']*k*2:.1f} "
                    f"{x0 + wd*k:.1f} {y - h_:.1f} L{x0 + wd*k:.1f} {y:.1f} Z", w=2.0)
        else:
            sh.rect(x0, y - h_, wd * k, h_, w=1.8, rx=2 if name != "Battery" else 8)
        ylist.append((name, y - h_, y, x0, wd * k))
        y -= h_
    # charging coil hint under battery
    sh.rect(cx - P["coil_diameter"] / 2 * k, base - P["back_thickness"] * k - 6, P["coil_diameter"] * k, 5, w=1.0, dash="6 4")
    # labels with leaders on the left, alternating
    lx = cx - R - 110
    for i, (name, y0, y1, x0, wd) in enumerate(ylist):
        ym = (y0 + y1) / 2
        ty = base - 20 - i * 52
        sh.leader(cx - 150, ym, lx, ty, name, size=15, anchor="end")
    # thickness chain on the right
    rx = cx + R + 70
    for name, y0, y1, x0, wd in ylist:
        t = (y1 - y0) / k
        sh.line(x0 + wd, y0, rx - 4, y0, w=0.6, dash="3 4", col=sh.t["faint"])
        sh.line(x0 + wd, y1, rx - 4, y1, w=0.6, dash="3 4", col=sh.t["faint"])
        if (y1 - y0) > 12:
            sh.line(rx, y0, rx, y1, w=1.0)
            sh.arrow(rx, y0, -math.pi / 2, 7)
            sh.arrow(rx, y1, math.pi / 2, 7)
        sh.text(rx + 12, (y0 + y1) / 2 + 5, f"{t:.1f}  {name.lower()}", size=13)
    sh.dim_v(top, base, cx + R, cx + R, cx + R + 250, "12.6 overall")
    sh.dim_h(cx - R, cx + R, base, base, base + 90, "⌀46.0 case")
    sh.dim_h(cx + R - wall, cx + R, top + 20, top + 20, top - 50, "2.2 wall")
    bw = P["battery_w"] * k
    sh.dim_h(cx - bw / 2, cx + bw / 2, base - P["back_thickness"]*k - 30, base - P["back_thickness"]*k - 30, base + 46, "30 × 24 × 4.6 battery, about 400 mAh")
    # table of the stack
    tx0, ty0 = 120, 210
    sh.text(tx0, ty0, "STACK, BACK TO FRONT", size=15, weight="bold")
    rows = [("Back cover", "1.4", "steel, O-ring, ⌀16 window, coil behind"),
            ("Sensor board", "0.8", "PPG AS7058, TMP117, off-body"),
            ("Battery", "4.6", "curved Li-Po ~400 mAh, 30 × 24"),
            ("Logic board", "1.0", "SW5100 SoC + modem, PMIC, eSIM"),
            ("Display frame", "1.0", "holds and seals the display"),
            ("Display", "0.8", "1.39 in AMOLED 466 × 466"),
            ("Cover glass", "1.0", "+0.6 dome; gasket below")]
    for i, (a, b, c) in enumerate(rows):
        yy = ty0 + 30 + i * 24
        sh.text(tx0, yy, a, size=13)
        sh.text(tx0 + 120, yy, b, size=13, anchor="end")
        sh.text(tx0 + 132, yy, c, size=13)
    sh.text(tx0, ty0 + 30 + 7 * 24 + 6, "Sum of layers 10.6; air and gaskets 1.4.", size=13, col=sh.t["faint"])
    return sh


# ---------------------------------------------------------------------------
# Sheet 3: exploded view and parts list
# ---------------------------------------------------------------------------
EXPLODE = [  # top to bottom: item, name, diameter or (w,d), thickness, shape
    (1, "Cover glass", P["glass_diameter"], 1.6, "disc"),
    (2, "Display", P["display_diameter"], 0.8, "disc"),
    (3, "Display frame", P["display_diameter"] + 2, 1.0, "ring"),
    (4, "Case with buttons", P["case_diameter"], 9.0, "case"),
    (5, "Logic board", P["board_diameter"], 1.0, "board"),
    (6, "Battery", (P["battery_w"], P["battery_d"]), P["battery_h"], "box"),
    (7, "Sensor board", P["sensor_board_diameter"], 0.8, "board"),
    (8, "Back cover", P["case_diameter"] - 6, 1.4, "back"),
]

PARTS_LIST = [
    (1, "Cover glass", "1", "Gorilla Glass DX+ or sapphire, ⌀40, domed"),
    (2, "Display", "1", "1.39–1.43 in round AMOLED 466×466, touch"),
    (3, "Display frame", "1", "Seals the display to the glass"),
    (4, "Case", "1", "316L steel ⌀46 × 12.6, 22 mm lugs, antenna slot"),
    ("4a", "Help button", "1", "Coral cap 11 × 4.5 on Panasonic EVP-BV, IP68"),
    ("4b", "Side button", "1", "Cap 6.5 × 3.0 on sealed tactile switch"),
    (5, "Logic board", "1", "Snapdragon W5+ SW5100 (LTE, VoLTE, GNSS, Wi-Fi, BT)"),
    (5, "  on board", "", "PMW5100 PMIC · OC1230 eSIM · BMI270 IMU · BMP585"),
    (5, "  on board", "", "2× IM69D130 mics · TAS2563 amp · DRV2605L haptics"),
    (6, "Battery", "1", "Grepow GRP3824038 curved Li-Po, ~410 mAh"),
    (7, "Sensor board", "1", "ams OSRAM AS7058 PPG · TI TMP117 skin temp"),
    (8, "Back cover", "1", "Steel, ⌀16 window, BQ51013C Qi coil behind"),
    (9, "Speaker", "1", "PUI AS01508MS 15 × 11 × 3.5, IP67 (in case)"),
    (10, "Vibration motor", "1", "Vybronics VG0832013D LRA ⌀8 (in case)"),
    (11, "Strap and clasp", "1", "Medical silicone 22 mm, magnetic clasp"),
]


def iso_disc(sh, cx, cy, d, t, k, ry_ratio=0.34, ring=False, board=False, back=False, case=False):
    rx = d / 2 * k
    ry = rx * ry_ratio
    h = t * k
    # side walls and bottom half-ellipse
    sh.line(cx - rx, cy, cx - rx, cy + h)
    sh.line(cx + rx, cy, cx + rx, cy + h)
    sh.path(f"M{cx - rx:.1f} {cy + h:.1f} A{rx:.1f} {ry:.1f} 0 0 0 {cx + rx:.1f} {cy + h:.1f}")
    sh.ellipse(cx, cy, rx, ry, w=1.8, fill=sh.t["bg"])
    if ring:
        sh.ellipse(cx, cy, rx * 0.88, ry * 0.88, w=1.0)
    if board:
        # components as small boxes
        import random
        rnd = random.Random(int(d * 10))
        for _ in range(9 if d > 30 else 3):
            a = rnd.uniform(0, 2 * math.pi)
            r = rnd.uniform(0, 0.62)
            x = cx + r * rx * math.cos(a)
            y = cy + r * ry * math.sin(a)
            w_ = rnd.uniform(10, 30)
            sh.rect(x - w_ / 2, y - 4, w_, 8, w=1.0)
    if back:
        sw = P["sensor_window_diameter"] / 2 * k
        sh.ellipse(cx, cy, sw, sw * ry_ratio, w=1.4)
        sh.ellipse(cx, cy, P["coil_diameter"] / 2 * k, P["coil_diameter"] / 2 * k * ry_ratio, w=0.9, dash="8 5")
    if case:
        sh.ellipse(cx, cy, rx * 0.905, ry * 0.905, w=1.0)
        # help button on the right side
        hx = cx + rx
        sh.rect(hx - 2, cy + h * 0.25, 16, h * 0.35, w=1.6, rx=3)
        sh.rect(hx - 6, cy + h * 0.68, 10, h * 0.22, w=1.2, rx=2)
        # lugs at 12 (behind) and 6 o'clock (in front), 22 mm apart
        lw = 22 * k
        sh.path(f"M{cx - lw/2:.1f} {cy - ry + 2:.1f} L{cx - lw/2:.1f} {cy - ry - 14:.1f} L{cx + lw/2:.1f} {cy - ry - 14:.1f} L{cx + lw/2:.1f} {cy - ry + 2:.1f}", w=1.2)
        sh.path(f"M{cx - lw/2:.1f} {cy + ry + h - 6:.1f} L{cx - lw/2:.1f} {cy + ry + h + 12:.1f} L{cx + lw/2:.1f} {cy + ry + h + 12:.1f} L{cx + lw/2:.1f} {cy + ry + h - 6:.1f}", w=1.2)


def sheet3(theme):
    sh = Sheet(theme, 3, "EXPLODED VIEW · PARTS LIST", "1.5:1 (oblique)")
    k = 6.0
    cx = 400
    heading(sh, 120, 110, "EXPLODED VIEW", "Parts lifted in assembly order, top to bottom.")
    y = 210
    for item, name, size, t, shape in EXPLODE:
        if shape == "box":
            w_, d_ = size[0] * k, size[1] * k * 0.34
            h_ = t * k
            sh.path(f"M{cx - w_/2:.1f} {y:.1f} L{cx + w_/2:.1f} {y:.1f} L{cx + w_/2:.1f} {y + h_:.1f} "
                    f"L{cx - w_/2:.1f} {y + h_:.1f} Z", w=1.6)
            sh.path(f"M{cx - w_/2:.1f} {y:.1f} L{cx - w_/2 + 18:.1f} {y - d_:.1f} L{cx + w_/2 + 18:.1f} {y - d_:.1f} "
                    f"L{cx + w_/2:.1f} {y:.1f} M{cx + w_/2 + 18:.1f} {y - d_:.1f} L{cx + w_/2 + 18:.1f} {y - d_ + h_:.1f} "
                    f"L{cx + w_/2:.1f} {y + h_:.1f}", w=1.6)
            anchor = (cx + w_/2 - 10, y + h_/2)
            y_next = y + h_ + 46
        else:
            iso_disc(sh, cx, y, size, t, k, ring=(shape == "ring"), board=(shape == "board"),
                     back=(shape == "back"), case=(shape == "case"))
            rx = size / 2 * k
            anchor = (cx + rx * 0.97, y + t * k * 0.5 + 2)
            y_next = y + t * k + rx * 0.34 * 2 + (40 if shape == "case" else 20)
        # balloon to the right, name after it
        sh.balloon(anchor[0], anchor[1], item, 690, anchor[1] - 4)
        sh.text(716, anchor[1] + 1, name, size=14)
        y = y_next
    # assembly axis
    sh.line(cx, 150, cx, y - 20, w=0.8, dash="18 5 3 5", col=sh.t["faint"])

    # parts list table
    tx0, ty0 = 900, 150
    cols = [0, 60, 250, 300]
    sh.text(tx0, ty0 - 20, "PARTS LIST (candidate components, to confirm)", size=16, weight="bold")
    sh.rect(tx0 - 10, ty0, 720, 34 + 30 * len(PARTS_LIST), w=1.6)
    sh.line(tx0 - 10, ty0 + 34, tx0 + 710, ty0 + 34, w=1.2)
    for c, hd in zip(cols, ("Item", "Part", "Qty", "Component / specification")):
        sh.text(tx0 + c, ty0 + 23, hd, size=14, weight="bold")
    for xx in cols[1:]:
        sh.line(tx0 + xx - 8, ty0, tx0 + xx - 8, ty0 + 34 + 30 * len(PARTS_LIST), w=0.8)
    for i, (item, name, qty, spec) in enumerate(PARTS_LIST):
        yy = ty0 + 34 + 30 * (i + 1) - 10
        sh.text(tx0 + cols[0], yy, str(item) if name.strip() != "on board" else "", size=13)
        sh.text(tx0 + cols[1], yy, name if name.strip() != "on board" else "", size=13)
        sh.text(tx0 + cols[2], yy, qty, size=13)
        sh.text(tx0 + cols[3], yy, spec, size=12.5)
        sh.line(tx0 - 10, yy + 10, tx0 + 710, yy + 10, w=0.4, col=sh.t["faint"])
    sh.text(tx0, ty0 + 70 + 30 * len(PARTS_LIST), "Sources and alternatives: docs/hardware/COMPONENTS.md.", size=13, col=sh.t["faint"])
    sh.text(tx0, ty0 + 92 + 30 * len(PARTS_LIST), "Items 9 to 11 sit in the case or outside it and are not drawn exploded.", size=13, col=sh.t["faint"])
    return sh


# ---------------------------------------------------------------------------
# Sheet 4: electronics block diagram and software stack
# ---------------------------------------------------------------------------
def box(sh, x, y, w_, h_, title, lines=(), bold=True):
    sh.rect(x, y, w_, h_, w=1.8, rx=6, fill=sh.t["bg"])
    sh.text(x + 12, y + 24, title, size=14.5, weight="bold" if bold else "normal")
    for i, l in enumerate(lines):
        sh.text(x + 12, y + 46 + i * 19, l, size=12.5)


def wire(sh, pts, label=None, lpos=None, dash=None):
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    sh.path(d, w=1.4, dash=dash)
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    sh.arrow(x2, y2, math.atan2(y2 - y1, x2 - x1), 8)
    if label:
        lx, ly = lpos if lpos else ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2 - 7)
        sh.text(lx, ly, label, size=12, anchor="middle", col=sh.t["faint"])


def sheet4(theme):
    sh = Sheet(theme, 4, "ELECTRONICS · BLOCK DIAGRAM", "not to scale")
    heading(sh, 120, 100, "BLOCK DIAGRAM", "Arrows show the direction of data or power. Bus names on the lines.")
    # centre SoC
    soc = (620, 380, 330, 230)
    box(sh, *soc, "SYSTEM-ON-CHIP", ["Qualcomm Snapdragon W5+ SW5100", "4 × Cortex-A53 · Adreno 702",
                                      "LTE Cat 1 bis/4 + VoLTE modem", "GNSS · Wi-Fi 2.4/5 GHz · BT 5.3",
                                      "QCC5100 always-on co-processor", "LPDDR4 + eMMC (on package)",
                                      "Runs AOSP + IRA CARE app"])
    sx, sy, sw_, sh_ = soc
    # left column: sensors
    sensors = [("MOTION", ["BMI270 6-axis, 50 Hz FIFO"], "I²C/SPI"),
               ("BAROMETER", ["BMP585, fall height"], "I²C"),
               ("HEART RATE (PPG)", ["AS7058 on sensor board"], "SPI"),
               ("SKIN TEMPERATURE", ["TMP117 ±0.1 °C"], "I²C"),
               ("BUTTONS", ["Help (coral) · Side"], "GPIO")]
    for i, (t, l, bus) in enumerate(sensors):
        y = 160 + i * 116
        box(sh, 120, y, 270, 82, t, l)
        wire(sh, [(390, y + 41), (500, y + 41), (500, sy + 40 + i * 36), (sx, sy + 40 + i * 36)], bus, (445, y + 33))
    # right column: outputs and radios
    outs = [("DISPLAY + TOUCH", ["1.39 in AMOLED 466×466", "QSPI / MIPI-DSI · touch I²C"]),
            ("AUDIO IN", ["2 × IM69D130 MEMS mics, PDM"]),
            ("AUDIO OUT", ["TAS2563 amp → 0.7 W speaker"]),
            ("HAPTICS", ["DRV2605L → LRA VG0832013D"]),
            ("eSIM", ["Infineon OC1230 eUICC"])]
    for i, (t, l) in enumerate(outs):
        y = 160 + i * 116
        box(sh, 1110, y, 300, 82 if len(l) < 2 else 92, t, l)
        yy = y + 41
        if t == "AUDIO IN":
            wire(sh, [(1110, yy), (1040, yy), (1040, sy + 40 + i * 36), (sx + sw_, sy + 40 + i * 36)])
        else:
            wire(sh, [(sx + sw_, sy + 40 + i * 36), (1040, sy + 40 + i * 36), (1040, yy), (1110, yy)])
    # antennas on top
    box(sh, 620, 170, 330, 120, "ANTENNAS (in the case slot)", ["LTE main + diversity", "GNSS L1", "Wi-Fi / Bluetooth"])
    wire(sh, [(785, 290), (785, sy)], "RF", (810, 340))
    # power at bottom
    box(sh, 470, 770, 250, 100, "Qi RECEIVER", ["TI BQ51013C, 5 W", "coil ⌀28 behind back"])
    box(sh, 760, 770, 250, 100, "PMIC + CHARGER", ["Qualcomm PMW5100", "rails · fuel gauge"])
    box(sh, 1050, 770, 250, 100, "BATTERY", ["~410 mAh curved Li-Po", "3.8 V nominal"])
    box(sh, 120, 770, 300, 100, "CHARGING DOCK", ["weighted puck, magnets", "Qi transmitter, USB-C in"])
    wire(sh, [(420, 820), (470, 820)], "5 W", (445, 810), dash="10 6")
    wire(sh, [(720, 820), (760, 820)])
    wire(sh, [(1010, 820), (1050, 820)])
    wire(sh, [(885, 770), (885, sy + sh_)], "power rails", (935, 700))
    # power budget note
    sh.text(120, 905, "POWER BUDGET (estimate, BLUEPRINT.md section 5): about 11.7 mA average → about 35 h on 410 mAh.", size=14)

    # software stack column
    sx0, sy0 = 120, 945
    sh.text(sx0, sy0, "SOFTWARE STACK (top = what the person sees)", size=15, weight="bold")
    layers = ["IRA CARE watch app (Kotlin, Compose) · aosp flavour, no Google services",
              "Sensor HAL contract (docs/hardware/SENSOR_HAL.md) · Android sensor framework",
              "AOSP (Android Open Source Project) built from the chip vendor's board support package",
              "Android Verified Boot · signed A/B updates over the air from our server",
              "Bootloader accepting only images signed with our key"]
    for i, l in enumerate(layers):
        sh.rect(sx0, sy0 + 12 + i * 32, 880, 28, w=1.4, rx=4)
        sh.text(sx0 + 12, sy0 + 31 + i * 32, l, size=13)
    return sh


# ---------------------------------------------------------------------------
# Sheet 5: dock and strap
# ---------------------------------------------------------------------------
def sheet5(theme):
    sh = Sheet(theme, 5, "CHARGING DOCK · STRAP", "dock 1.25:1; strap 1.15:1")
    k = 5.0
    heading(sh, 120, 110, "CHARGING DOCK", "Weighted puck. The watch drops into the magnetic cradle face-up.")
    # dock top view
    cx, cy = 360, 400
    Rd = P["dock_diameter"] / 2 * k
    sh.circle(cx, cy, Rd, w=2.2)
    sh.circle(cx, cy, P["case_diameter"] / 2 * k + 6, w=1.4)
    sh.circle(cx, cy, P["coil_diameter"] / 2 * k, w=1.0, dash="8 5")
    for a in (45, 135, 225, 315):
        r = (P["case_diameter"] / 2 * k + 6 + Rd) / 2
        sh.circle(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)), 7, w=1.2)
    sh.centre_mark(cx, cy, Rd + 20)
    sh.dim_h(cx - Rd, cx + Rd, cy, cy, cy + Rd + 50, "⌀62.0 dock")
    sh.leader(cx + (P["case_diameter"]/2*k + 6) * 0.71, cy - (P["case_diameter"]/2*k + 6) * 0.71, cx + Rd + 40, cy - Rd, "Cradle ⌀47, 3 mm deep")
    r = (P["case_diameter"] / 2 * k + 6 + Rd) / 2
    sh.leader(cx + r * 0.71, cy + r * 0.71, cx + Rd + 40, cy + Rd - 10, "4 × alignment magnets")
    sh.text(cx, cy - Rd - 18, "TOP VIEW", size=13, anchor="middle", col=sh.t["faint"])
    # dock side section
    sx, sy = 360, 760
    dh = P["dock_height"] * k
    sh.text(sx, sy - dh - 40, "SECTION", size=13, anchor="middle", col=sh.t["faint"])
    sh.path(f"M{sx - Rd:.1f} {sy:.1f} L{sx - Rd + 10:.1f} {sy - dh:.1f} L{sx - P['case_diameter']/2*k - 6:.1f} {sy - dh:.1f} "
            f"L{sx - P['case_diameter']/2*k - 6:.1f} {sy - dh + 15:.1f} L{sx + P['case_diameter']/2*k + 6:.1f} {sy - dh + 15:.1f} "
            f"L{sx + P['case_diameter']/2*k + 6:.1f} {sy - dh:.1f} L{sx + Rd - 10:.1f} {sy - dh:.1f} L{sx + Rd:.1f} {sy:.1f} Z", w=2.0)
    sh.hatch_rect(sx - Rd + 14, sy - 26, 2 * Rd - 28, 20)
    sh.rect(sx - Rd + 14, sy - 26, 2 * Rd - 28, 20, w=1.0)
    sh.rect(sx - P["coil_diameter"] / 2 * k, sy - dh + 18, P["coil_diameter"] * k, 6, w=1.0, dash="6 4")
    sh.dim_v(sy - dh, sy, sx + Rd, sx + Rd, sx + Rd + 50, "16.0 height")
    sh.leader(sx - 60, sy - 16, sx - 40, sy + 60, "Steel weight (hatched)")
    sh.leader(sx + 40, sy - dh + 21, sx + Rd + 40, sy - dh - 30, "Qi transmitter coil")
    sh.leader(sx + Rd - 4, sy - 4, sx + Rd + 40, sy + 40, "USB-C input, rubber foot")

    # strap
    heading(sh, 860, 110, "STRAP AND MAGNETIC CLASP", "Closes itself when the two ends are brought near.")
    k2 = 4.6
    x0, y0 = 860, 300
    L = P["strap_length"] * k2
    wd = P["strap_width"] * k2
    sh.rect(x0, y0, L * 0.55, wd, w=1.8, rx=wd / 2)
    sh.rect(x0 + L * 0.55 + 30, y0, L * 0.45, wd, w=1.8, rx=wd / 2)
    for i in range(6):
        sh.line(x0 + 40 + i * 34, y0 + 10, x0 + 40 + i * 34, y0 + wd - 10, w=0.8, dash="4 4")
    sh.rect(x0 + L * 0.55 - 40, y0 + 10, 30, wd - 20, w=1.4, rx=4)
    sh.rect(x0 + L * 0.55 + 40, y0 + 10, 30, wd - 20, w=1.4, rx=4)
    sh.dim_h(x0, x0 + L + 30, y0 + wd, y0 + wd, y0 + wd + 60, "110 closed length (S/M/L to be sized)")
    sh.dim_v(y0, y0 + wd, x0, x0, x0 - 40, "22", side="left")
    sh.leader(x0 + L * 0.55 - 25, y0 + wd / 2, x0 + L * 0.55 - 10, y0 - 50, "Magnet pair, about 1 kg pull")
    sh.leader(x0 + 330, y0 + wd - 8, x0 + 360, y0 + wd + 120, "Medical-grade silicone, 2.6 thick, vent holes")
    # notes
    ny = 640
    sh.text(860, ny, "NOTES", size=16, weight="bold")
    for i, n in enumerate(["1. Dock: Qi 5 W baseline; current wearable Qi transmitter",
                           "   controllers are not recommended for new designs, so the",
                           "   dock transmitter is a custom design (COMPONENTS.md).",
                           "2. The watch charges face-up so the time and a carer's",
                           "   arrival stay visible at the bedside.",
                           "3. Strap and clasp biocompatibility per ISO 10993-1,",
                           "   claimed on the finished device.",
                           "4. One-handed fastening tested with people with arthritis",
                           "   before design freeze."]):
        sh.text(860, ny + 28 + i * 22, n, size=13)
    return sh


def main():
    out = Path(sys.argv[1])
    theme = sys.argv[2] if len(sys.argv) > 2 else "blue"
    out.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate((sheet1, sheet2, sheet3, sheet4, sheet5), 1):
        (out / f"sheet{i}.svg").write_text(fn(theme).svg(), encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
