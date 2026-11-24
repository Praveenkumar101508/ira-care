"""Build the IRA CARE watch in Blender, headless, from parameters.

    python scripts/hardware/build_watch.py --out docs/hardware [--quick] [--no-video]

Runs under Blender's Python (`bpy` from PyPI, or `blender --background --python`).
Everything is true to size in millimetres (one Blender unit = one metre, so the
parameters are divided by 1000). The model has the parts a real watch has, as
separate named objects so the viewer can explode and name them:

    cover glass, display, display frame, case, bezel ring, Help button, side button,
    logic board with its components, battery, sensor board with the PPG module,
    back cover with the sensor window and charging coil, strap (two halves and a
    magnetic clasp), the charging dock, and a wrist for one render.

Outputs (all under --out):
    renders/hero.png, front.png, side.png, back.png, exploded.png, section.png, wrist_dock.png
    turntable.mp4 (20 s)        ira_watch.glb (glTF, part names and metadata as extras)
    ira_watch.blend             parts.json (the part list the viewer reads)

Nothing here is a manufacturing drawing: the dimensions are the design intent
recorded in docs/hardware/BLUEPRINT.md, to be confirmed by a hardware engineer.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import bpy
from mathutils import Vector

MM = 0.001

# ── Parameters (mm) ──────────────────────────────────────────────────────────
P = {
    "case_diameter": 46.0, "case_height": 12.6, "case_wall": 2.2, "case_bevel": 1.6,
    "glass_diameter": 40.0, "glass_thickness": 1.0, "glass_dome": 0.6,
    "display_diameter": 35.3, "display_thickness": 0.8,        # 1.39 inch AMOLED, 466 x 466
    "frame_thickness": 1.0,
    "board_diameter": 36.0, "board_thickness": 1.0,
    "battery_w": 30.0, "battery_d": 24.0, "battery_h": 4.6,     # about 400 mAh curved Li-Po
    "sensor_board_diameter": 22.0, "sensor_board_thickness": 0.8,
    "back_thickness": 1.4, "sensor_window_diameter": 16.0,
    "help_button": (11.0, 4.5, 3.0), "side_button": (6.5, 3.0, 2.2),
    "lug_width": 22.0, "strap_width": 22.0, "strap_thickness": 2.6, "strap_length": 110.0,
    "dock_diameter": 62.0, "dock_height": 16.0,
    "wrist_diameter": 58.0,
}

# What each part does for the person and which IRA CARE feature uses it (the viewer shows this).
PARTS = {
    "Cover glass": ("sapphire-hard cover over the display, domed so a wet finger still slides", "the whole screen; survives 24-hour wear and knocks"),
    "Display": ("1.39 inch round always-on display, 466 x 466: large words, the time, the carer coming", "the home screen, the question, the fall countdown, 'Sravani is here'"),
    "Display frame": ("holds the display flat against the glass and seals the top of the case", "IP68 sealing"),
    "Case": ("316L steel case, 46 mm, with two lugs for a 22 mm strap; the antenna runs in the slot", "everything; LTE and GNSS antennas live in the case gap"),
    "Bezel ring": ("a raised ring the finger can feel, so the Help button is found by touch", "orientation without looking"),
    "Help button": ("a large tactile button on the side, red, 11 mm long: one press tells the family and carers", "Help request (SOS) with location"),
    "Side button": ("a smaller button: I'm OK, and the menu", "I'm OK during a check-in or a fall countdown"),
    "Logic board": ("the main board: system-on-chip with LTE and VoLTE, GNSS, Wi-Fi, Bluetooth, eSIM, power management, memory", "runs the IRA CARE app, talks to the server, holds the outbox"),
    "SoC and modem": ("the wearable system-on-chip that runs AOSP and carries the LTE modem", "the app, the conversation stream, calls to the circle"),
    "Wi-Fi and Bluetooth": ("the Wi-Fi 5 and Bluetooth 5 radio for home use and pairing", "readings upload at home; pairing"),
    "GNSS": ("the satellite receiver, asked for one position only when help is needed", "the SOS carries where the person is"),
    "eSIM": ("the embedded SIM: the watch has its own line, no card to lose", "LTE anywhere; VoLTE calls"),
    "Power management": ("charges the battery from the dock and feeds every rail", "charging, battery notice to the carer"),
    "Motion sensor": ("6-axis accelerometer and gyroscope at 50 Hz with a FIFO: the fall detectors read it", "suspected fall (rule and trained model); movement after a missed check-in"),
    "Barometer": ("altitude change in the second before an impact (planned)", "fall confidence"),
    "Microphones": ("two noise-suppressing microphones, one each side, so IRA hears a quiet voice in a kitchen", "Talk to IRA; the check-in answer; 'I'm OK' and 'help' by voice"),
    "Speaker": ("a loud micro speaker behind the side grille", "IRA speaks the question and the carer's name"),
    "Vibration motor": ("a linear actuator: a firm pattern for a question, a fall, a carer's arrival", "haptics channel of the communication profile"),
    "Battery": ("about 400 mAh curved lithium-polymer cell for a day and a night on one charge", "24-hour wear; charged on the dock at the bedside"),
    "Sensor board": ("the board against the skin: optical heart-rate sensor, skin temperature, off-body detection", "heart rate at its own time; off-wrist; skin temperature trend (planned)"),
    "PPG module": ("the optical heart-rate sensor: green and red light through the window", "heart-rate samples and the out-of-range alert"),
    "Skin temperature sensor": ("a contact thermometer beside the optical window", "the daily report (planned)"),
    "Back cover": ("the back of the case with the sensor window and the charging coil behind it", "charging on the dock; IP68"),
    "Charging coil": ("wireless charging receiver coil, magnet-aligned with the dock", "charging without a plug for older hands"),
    "Strap": ("medical-grade silicone, 22 mm, soft, with a magnetic clasp that closes itself", "fastening with one hand or with arthritis"),
    "Clasp": ("the magnetic clasp: lay the two ends near each other and they close", "fastening with one hand"),
    "Dock": ("the charging dock: a weighted puck with a magnetic cradle, the watch drops into place", "charging without aligning anything"),
}

#: The stack, read top to bottom. The exploded view and the viewer lift the top layer
#: most, so the parts keep this order in the air (round 4: the earlier order was inverted,
#: which put the labels on the wrong parts). The bezel ring and both buttons travel with
#: the case; the strap and clasp stay where they are.
LAYERS = ["Cover glass", "Display", "Display frame", "Case", "Logic board", "Battery", "Sensor board", "Back cover",
          "Strap", "Clasp"]
EXPLODED_LAYERS = [n for n in LAYERS if n not in ("Strap", "Clasp")]
#: The air between one layer's top and the next layer's bottom in the exploded view.
EXPLODE_GAP_MM = 7.0
#: The turntable's length: the film opens with it (docs/film/NARRATION.md).
TURNTABLE_S = 20

#: One product look, shared with the browser watch in the dashboard (dashboard/src/app/globals.css,
#: the `.watch-*` rules) and the phone's theme: a graphite case, a coral Help button, the app's
#: own colours on the screen. sRGB hex; Blender wants linear (see `srgb`).
LOOK = {
    "graphite": "#2B2E33", "graphite_dark": "#1C1F22", "coral": "#E2574C", "strap": "#2F3A44",
    "screen_bg": "#000000", "ok": "#2E7D32", "sos": "#B3261E", "talk": "#2F5D8A", "dock": "#FAF8F4",
    "ink": "#1F2A24", "muted": "#9FB0A8", "paper": "#FAF8F4", "line": "#E1DED8",
}


def srgb(hex_colour: str, alpha: float = 1.0):
    """#RRGGBB (sRGB) to Blender's linear RGBA."""
    h = hex_colour.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return (*out, alpha)


# ── Helpers ──────────────────────────────────────────────────────────────────

def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.unit_settings.length_unit = "MILLIMETERS"
    return scene


def material(name, color=(0.8, 0.8, 0.8, 1), metallic=0.0, roughness=0.5, transmission=0.0, emission=None,
             emission_strength=1.0, ior=1.45, alpha=1.0, subsurface=0.0):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nodes = m.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["IOR"].default_value = ior
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = transmission
    if "Subsurface Weight" in bsdf.inputs:
        bsdf.inputs["Subsurface Weight"].default_value = subsurface
    if alpha < 1.0:
        bsdf.inputs["Alpha"].default_value = alpha
    if emission is not None:
        if isinstance(emission, str):
            tex = nodes.new("ShaderNodeTexImage")
            tex.image = bpy.data.images.load(emission)
            m.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
            m.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        else:
            bsdf.inputs["Emission Color"].default_value = emission
        bsdf.inputs["Emission Strength"].default_value = emission_strength
    return m


MATS = {}


def materials(screen_png):
    MATS["steel"] = material("Graphite case", srgb(LOOK["graphite"]), metallic=0.9, roughness=0.34)
    MATS["steel_dark"] = material("Graphite brushed", srgb(LOOK["graphite_dark"]), metallic=0.85, roughness=0.45)
    MATS["glass"] = material("Sapphire", (1, 1, 1, 1), metallic=0.0, roughness=0.02, transmission=1.0, ior=1.76)
    MATS["screen"] = material("Display", (0.02, 0.02, 0.02, 1), roughness=0.3, emission=screen_png, emission_strength=2.2)
    MATS["black"] = material("Black polymer", (0.02, 0.02, 0.022, 1), roughness=0.55)
    MATS["red"] = material("Help coral", srgb(LOOK["coral"]), roughness=0.4)
    MATS["pcb"] = material("Board", (0.05, 0.22, 0.10, 1), roughness=0.6)
    MATS["chip"] = material("Chip", (0.04, 0.04, 0.045, 1), roughness=0.35, metallic=0.1)
    MATS["silver"] = material("Shield can", (0.72, 0.72, 0.74, 1), metallic=1.0, roughness=0.5)
    MATS["battery"] = material("Battery pouch", (0.62, 0.63, 0.66, 1), metallic=0.9, roughness=0.6)
    MATS["copper"] = material("Copper coil", (0.80, 0.45, 0.25, 1), metallic=1.0, roughness=0.4)
    MATS["silicone"] = material("Medical silicone", srgb(LOOK["strap"]), roughness=0.8)
    MATS["dock"] = material("Dock polymer", srgb(LOOK["dock"]), roughness=0.6)
    MATS["skin"] = material("Skin", (0.76, 0.56, 0.45, 1), roughness=0.75, subsurface=0.08)
    MATS["window"] = material("Sensor window", (0.05, 0.05, 0.06, 1), roughness=0.05, transmission=0.6, ior=1.5)
    MATS["label"] = material("Label", srgb(LOOK["ink"]), roughness=0.9)
    MATS["floor"] = material("Floor", srgb(LOOK["line"]), roughness=0.95)


def name_part(obj, name, layer=None, group=None):
    obj.name = name
    obj["part"] = name
    obj["layer"] = layer or name
    obj["what"] = PARTS.get(name, ("", ""))[0]
    obj["feature"] = PARTS.get(name, ("", ""))[1]
    if group:
        obj["group"] = group
    return obj


def cylinder(name, r, h, z, mat, verts=128, bevel=0.0, layer=None, group=None, location=(0, 0)):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=r * MM, depth=h * MM, location=(location[0] * MM, location[1] * MM, z * MM))
    o = bpy.context.active_object
    o.data.materials.append(mat)
    if bevel:
        b = o.modifiers.new("bevel", "BEVEL")
        b.width = bevel * MM
        b.segments = 6
    bpy.ops.object.shade_smooth()
    return name_part(o, name, layer, group)


def box(name, size, loc, mat, bevel=0.0, layer=None, group=None, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1, location=tuple(v * MM for v in loc), rotation=rot)
    o = bpy.context.active_object
    o.scale = tuple(s * MM for s in size)
    bpy.ops.object.transform_apply(scale=True)
    o.data.materials.append(mat)
    if bevel:
        b = o.modifiers.new("bevel", "BEVEL")
        b.width = bevel * MM
        b.segments = 4
    return name_part(o, name, layer, group)


def screen_disc(name, r, z, mat, layer=None, group=None, segments=96):
    """A flat disc with UVs that put the square screen image exactly on it."""
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    centre = bm.verts.new((0, 0, 0))
    rim = [bm.verts.new((r * MM * math.cos(2 * math.pi * i / segments), r * MM * math.sin(2 * math.pi * i / segments), 0)) for i in range(segments)]
    faces = [bm.faces.new((centre, rim[i], rim[(i + 1) % segments])) for i in range(segments)]
    uv = bm.loops.layers.uv.new("UVMap")
    for f in faces:
        for loop in f.loops:
            x, y = loop.vert.co.x / (2 * r * MM), loop.vert.co.y / (2 * r * MM)
            loop[uv].uv = (x + 0.5, y + 0.5)
    bm.to_mesh(mesh)
    bm.free()
    o = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(o)
    o.location = (0, 0, z * MM)
    o.data.materials.append(mat)
    return name_part(o, name, layer, group)


def ring(name, r_outer, r_inner, h, z, mat, layer=None, group=None):
    o = cylinder(name, r_outer, h, z, mat, layer=layer, group=group)
    bpy.ops.mesh.primitive_cylinder_add(vertices=128, radius=r_inner * MM, depth=(h + 2) * MM, location=(0, 0, z * MM))
    cutter = bpy.context.active_object
    m = o.modifiers.new("hole", "BOOLEAN")
    m.operation = "DIFFERENCE"
    m.object = cutter
    cutter.hide_render = True
    cutter.hide_viewport = True
    cutter.hide_set(True)
    cutter.name = name + " cutter"
    return o


# ── The watch ────────────────────────────────────────────────────────────────

def build_watch():
    """Z is up; z = 0 is the back face against the skin. Returns the part objects."""
    parts = []
    H = P["case_height"]
    r = P["case_diameter"] / 2
    z = 0.0
    # back cover with sensor window and coil
    back = ring("Back cover", r - 0.2, P["sensor_window_diameter"] / 2, P["back_thickness"], z + P["back_thickness"] / 2, MATS["steel_dark"])
    parts.append(back)
    parts.append(cylinder("Sensor window", P["sensor_window_diameter"] / 2, P["back_thickness"] - 0.2, z + P["back_thickness"] / 2, MATS["window"], layer="Back cover", group="Back cover"))
    parts.append(ring("Charging coil", 14.0, 9.0, 0.5, z + P["back_thickness"] + 0.3, MATS["copper"], layer="Back cover", group="Back cover"))
    z += P["back_thickness"] + 0.6
    # sensor board
    sb = cylinder("Sensor board", P["sensor_board_diameter"] / 2, P["sensor_board_thickness"], z + P["sensor_board_thickness"] / 2, MATS["pcb"])
    parts.append(sb)
    parts.append(box("PPG module", (5.0, 3.2, 1.0), (0, 0, z + P["sensor_board_thickness"] + 0.5), MATS["chip"], layer="Sensor board", group="Sensor board"))
    parts.append(box("Skin temperature sensor", (2.0, 2.0, 0.8), (6.5, 0, z + P["sensor_board_thickness"] + 0.4), MATS["chip"], layer="Sensor board", group="Sensor board"))
    z += P["sensor_board_thickness"] + 1.6
    # battery
    parts.append(box("Battery", (P["battery_w"], P["battery_d"], P["battery_h"]), (0, 0, z + P["battery_h"] / 2), MATS["battery"], bevel=0.8))
    z += P["battery_h"] + 0.6
    # logic board and components
    lb = cylinder("Logic board", P["board_diameter"] / 2, P["board_thickness"], z + P["board_thickness"] / 2, MATS["pcb"])
    parts.append(lb)
    top = z + P["board_thickness"]
    comps = [("SoC and modem", (7.5, 7.5, 0.9), (-5.0, 3.0)), ("Wi-Fi and Bluetooth", (6.0, 6.0, 1.1), (6.5, 4.0)),
             ("GNSS", (4.5, 4.5, 1.0), (7.0, -4.5)), ("eSIM", (2.0, 2.0, 0.6), (-1.0, -3.0)), ("Power management", (4.0, 4.0, 0.8), (-7.0, -5.0)),
             ("Motion sensor", (2.5, 3.0, 0.8), (1.5, 9.0)), ("Barometer", (2.0, 2.0, 0.8), (-2.5, 9.5)),
             ("Vibration motor", (8.0, 3.2, 2.6), (-2.0, -10.5)), ("Speaker", (9.0, 5.0, 2.4), (9.0, -11.0))]
    for nm, size, (x, y) in comps:
        mat = MATS["silver"] if nm in ("SoC and modem", "Wi-Fi and Bluetooth", "GNSS") else MATS["chip"]
        parts.append(box(nm, size, (x, y, top + size[2] / 2), mat, layer="Logic board", group="Logic board"))
    for i, x in enumerate((-15.0, 15.0)):
        parts.append(box("Microphones", (3.0, 2.0, 1.0), (x, -2.0, top + 0.5), MATS["chip"], layer="Logic board", group="Logic board"))
    z += P["board_thickness"] + 3.2
    # frame, display, glass
    frame = ring("Display frame", r - P["case_wall"] + 0.3, P["display_diameter"] / 2 - 0.5, P["frame_thickness"], z + P["frame_thickness"] / 2, MATS["black"])
    parts.append(frame)
    disp = cylinder("Display", P["display_diameter"] / 2, P["display_thickness"], z + P["display_thickness"] / 2 + 0.2, MATS["black"])
    parts.append(disp)
    parts.append(screen_disc("Display", P["display_diameter"] / 2 - 0.3, z + P["display_thickness"] + 0.21, MATS["screen"], layer="Display", group="Display"))
    z_glass = H - P["glass_thickness"]
    glass = cylinder("Cover glass", P["glass_diameter"] / 2, P["glass_thickness"], z_glass + P["glass_thickness"] / 2, MATS["glass"], bevel=0.6)
    parts.append(glass)
    # case: a ring the full height with the bevel, and the bezel
    case = ring("Case", r, r - P["case_wall"], H - P["back_thickness"], P["back_thickness"] + (H - P["back_thickness"]) / 2, MATS["steel"])
    cb = case.modifiers.new("bevel", "BEVEL")
    cb.width = P["case_bevel"] * MM
    cb.segments = 8
    parts.append(case)
    parts.append(ring("Bezel ring", r - 0.3, P["glass_diameter"] / 2 + 0.2, 1.2, H - 0.6, MATS["steel_dark"], layer="Case", group="Case"))
    # lugs
    for sgn in (-1, 1):
        parts.append(box("Case", (P["lug_width"] + 4, 6.0, 5.0), (0, sgn * (r + 1.5), 4.5), MATS["steel"], bevel=1.0, layer="Case", group="Case"))
    # buttons at 2 o'clock (Help) and 4 o'clock (side)
    for nm, size, ang, mat in (("Help button", P["help_button"], math.radians(25), MATS["red"]), ("Side button", P["side_button"], math.radians(-25), MATS["steel_dark"])):
        x = (r + size[1] / 2 - 0.8) * math.cos(ang)
        y = (r + size[1] / 2 - 0.8) * math.sin(ang)
        parts.append(box(nm, (size[1], size[0], size[2]), (x, y, H * 0.55), mat, bevel=0.8, rot=(0, 0, ang), layer="Case", group="Case"))
    # strap: a band round the wrist (a torus stretched to the strap's width), the part
    # under the case removed, split at the bottom for the magnetic clasp
    wrist_r = P["wrist_diameter"] / 2 + P["strap_thickness"] / 2 + 0.6
    bpy.ops.mesh.primitive_torus_add(major_segments=160, minor_segments=16, major_radius=wrist_r * MM, minor_radius=P["strap_thickness"] / 2 * MM,
                                     location=(0, 0, -(P["wrist_diameter"] / 2 + 1.0) * MM), rotation=(0, math.radians(90), 0))
    strap = bpy.context.active_object
    strap.scale.z = P["strap_width"] / P["strap_thickness"]   # the torus axis is local Z (world X after the rotation)
    bpy.ops.object.transform_apply(scale=True)
    strap.data.materials.append(MATS["silicone"])
    bpy.ops.object.shade_smooth()
    name_part(strap, "Strap")
    for cname, size, loc in (("Strap cutter top", (60.0, 50.0, 30.0), (0, 0, 8.0)), ("Strap cutter gap", (60.0, 6.0, 12.0), (0, 0, -(P["wrist_diameter"] + 2.0)))):
        bpy.ops.mesh.primitive_cube_add(size=1, location=tuple(v * MM for v in loc))
        c = bpy.context.active_object
        c.scale = tuple(v * MM for v in size)
        bpy.ops.object.transform_apply(scale=True)
        c.hide_render = True
        c.hide_set(True)
        c.name = cname
        m = strap.modifiers.new(cname, "BOOLEAN")
        m.operation = "DIFFERENCE"
        m.object = c
    parts.append(strap)
    parts.append(box("Clasp", (P["strap_width"] - 2, 12.0, 3.4), (0, 0, -(P["wrist_diameter"] + 2.0)), MATS["steel"], bevel=1.0))
    return parts


def build_dock(location=(0, 0, 0)):
    x, y, z = location
    dock = cylinder("Dock", P["dock_diameter"] / 2, P["dock_height"], z + P["dock_height"] / 2, MATS["dock"], bevel=3.0, location=(x, y))
    cradle = ring("Dock", P["case_diameter"] / 2 + 1.5, P["case_diameter"] / 2 - 4, 2.0, z + P["dock_height"] + 0.6, MATS["steel_dark"], group="Dock")
    cradle.location.x, cradle.location.y = x * MM, y * MM
    cable = box("Dock", (40.0, 4.0, 3.0), (x + P["dock_diameter"] / 2 + 18, y, z + 2.0), MATS["black"], bevel=1.0, group="Dock")
    return [dock, cradle, cable]


def build_wrist():
    bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=P["wrist_diameter"] / 2 * MM, depth=0.26, location=(0, 0, -(P["wrist_diameter"] / 2 + 1.0) * MM), rotation=(0, math.radians(90), 0))
    w = bpy.context.active_object
    w.scale.y = 1.12
    w.data.materials.append(MATS["skin"])
    bpy.ops.object.shade_smooth()
    w.name = "Wrist"
    w["part"] = "Wrist"
    return w


# ── The screen image ─────────────────────────────────────────────────────────

def make_screen_png(path, *, size=1024, time_text="10:32", words="All well", note="Sravani is coming at 11:00."):
    """The watch's home screen as the browser watch draws it (dashboard/src/app/watch):
    the time, a line of words, a note, and the three keys in the app's colours."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (size, size), LOOK["screen_bg"])
    d = ImageDraw.Draw(im)
    bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    book = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

    def font(f, px):
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            return ImageFont.load_default()

    def centred(text, y, f, fill):
        tw = d.textlength(text, font=f)
        d.text(((size - tw) / 2, y), text, font=f, fill=fill)
    centred(time_text, size * 0.14, font(book, int(size * 0.062)), LOOK["muted"])
    centred(words, size * 0.22, font(bold, int(size * 0.10)), "#FFFFFF")
    centred(note, size * 0.335, font(book, int(size * 0.052)), LOOK["muted"])
    keys = (("I'm OK", LOOK["ok"]), ("Help", LOOK["sos"]), ("Talk to IRA", LOOK["talk"]))
    kw, kh, gap = size * 0.58, size * 0.11, size * 0.03
    y = size * 0.46
    for text, colour in keys:
        x0 = (size - kw) / 2
        d.rounded_rectangle([x0, y, x0 + kw, y + kh], radius=kh / 2, fill=colour)
        f = font(bold, int(size * 0.058))
        tw = d.textlength(text, font=f)
        d.text(((size - tw) / 2, y + kh * 0.22), text, font=f, fill="#FFFFFF")
        y += kh + gap
    im.save(path)
    return path


# ── Scene, cameras, lights ───────────────────────────────────────────────────

def lights():
    """A soft studio: a large key from the front left, a weaker fill, a rim from behind,
    and a dim grey world. The earlier set-up (45 W key, bright world) blew the case out
    to white (round 4)."""
    for nm, loc, energy, size in (("Key", (-0.20, -0.30, 0.38), 14, 0.9), ("Fill", (0.40, -0.20, 0.20), 5, 1.2), ("Rim", (0.10, 0.40, 0.30), 10, 0.6)):
        ld = bpy.data.lights.new(nm, "AREA")
        ld.energy = energy
        ld.size = size
        lo = bpy.data.objects.new(nm, ld)
        bpy.context.scene.collection.objects.link(lo)
        lo.location = loc
        look_at(lo, Vector((0, 0, 0.006)))
    world = bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs["Color"].default_value = srgb(LOOK["line"])
    bg.inputs["Strength"].default_value = 0.6


def floor(z=-0.0005, size=2.0):
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0, 0, z))
    f = bpy.context.active_object
    f.data.materials.append(MATS["floor"])
    f.name = "Floor"
    f.is_shadow_catcher = False
    return f


def look_at(obj, target):
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def camera(name, loc, target, lens=85):
    cd = bpy.data.cameras.new(name)
    cd.lens = lens
    cd.clip_start = 0.001
    cam = bpy.data.objects.new(name, cd)
    bpy.context.scene.collection.objects.link(cam)
    cam.location = loc
    look_at(cam, Vector(target))
    return cam


def render_setup(samples, res):
    s = bpy.context.scene
    s.render.engine = "CYCLES"
    s.cycles.device = "CPU"
    s.cycles.samples = samples
    s.cycles.use_denoising = True
    s.render.resolution_x, s.render.resolution_y = res
    s.render.resolution_percentage = 100
    s.render.film_transparent = False
    s.view_settings.view_transform = "AgX" if "AgX" in [i.identifier for i in s.view_settings.bl_rna.properties["view_transform"].enum_items] else "Filmic"
    s.view_settings.exposure = -0.3
    s.render.image_settings.file_format = "PNG"


def render(cam, path):
    s = bpy.context.scene
    s.camera = cam
    s.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("rendered", path, file=sys.stderr)


# ── Views ────────────────────────────────────────────────────────────────────

def layer_extent(parts, layer):
    """The lowest and highest z (mm) of a layer's objects, as built (assembled)."""
    lo, hi = float("inf"), float("-inf")
    for o in parts:
        if (o.get("layer") or o.name) != layer or o.type != "MESH":
            continue
        for corner in o.bound_box:
            z = (o.matrix_world @ Vector(corner)).z / MM
            lo, hi = min(lo, z), max(hi, z)
    return lo, hi


def explode_offsets(parts, gap_mm):
    """How far each layer rises when exploded, in mm: from the back cover up, every
    layer's bottom sits `gap_mm` above the top of the layer below it, so a tall part
    (the case) never swallows the thin one under it. The viewer uses the same numbers
    (parts.json, `explode_mm`)."""
    bpy.context.view_layer.update()
    offsets, top = {}, None
    for layer in reversed(EXPLODED_LAYERS):          # bottom of the stack first
        lo, hi = layer_extent(parts, layer)
        if lo == float("inf"):
            continue
        rise = 0.0 if top is None else max(0.0, top + gap_mm - lo)
        offsets[layer] = round(rise, 2)
        top = hi + rise
    return offsets


def explode(parts, amount_mm):
    """Lift each layer by its offset (explode_offsets), so the cover glass rises most and
    the back cover not at all and the parts keep their order in the air. Groups travel
    with their layer; the strap and clasp stay."""
    offsets = explode_offsets(parts, amount_mm)
    for o in parts:
        layer = o.get("layer") or o.name
        if layer not in offsets:
            continue
        o.location.z += offsets[layer] * MM
        o["explode_z"] = offsets[layer]
    return offsets


def anchor_2d(cam, point_world):
    """Where a world point lands in the rendered image, in pixels (origin top left)."""
    from bpy_extras.object_utils import world_to_camera_view
    s = bpy.context.scene
    v = world_to_camera_view(s, cam, Vector(point_world))
    return (v.x * s.render.resolution_x, (1.0 - v.y) * s.render.resolution_y)


def draw_labels(png_path, labels, *, font_px=38):
    """Draw the exploded view's labels over the render: dark ink on a pale panel, a
    leader line from the panel to a dot on the part itself. Done in 2D after the
    render so the text is always sharp and legible whatever the lighting (round 4).
    `labels` is a list of (text, (x_part, y_part), side) with pixel anchors."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(png_path).convert("RGBA")
    draw = ImageDraw.Draw(im)
    w, h = im.size
    font_file = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        font = ImageFont.truetype(font_file, font_px)
    except OSError:
        font = ImageFont.load_default()
    ink, paper, line = LOOK["ink"], LOOK["paper"], LOOK["line"]
    margin = int(w * 0.035)
    for text, (px, py), side in labels:
        tw = draw.textlength(text, font=font)
        th = font_px * 1.25
        pad = int(font_px * 0.45)
        if side > 0:
            x0 = w - margin - tw - 2 * pad
        else:
            x0 = margin
        y0 = py - th / 2 - pad / 2
        x1, y1 = x0 + tw + 2 * pad, y0 + th + pad
        # leader: from the panel's inner edge to a dot on the part
        start = (x0 if side > 0 else x1, (y0 + y1) / 2)
        draw.line([start, (px, py)], fill=ink, width=max(3, font_px // 12))
        r = max(6, font_px // 5)
        draw.ellipse([px - r, py - r, px + r, py + r], fill=LOOK["coral"], outline=ink, width=2)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=pad, fill=paper, outline=line, width=2)
        draw.text((x0 + pad, y0 + pad / 2), text, font=font, fill=ink)
    im.convert("RGB").save(png_path)


def section(parts):
    """Cut everything at x > 0 away with a boolean, for the cross-section render."""
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0.1, 0, 0.0))
    cutter = bpy.context.active_object
    cutter.scale = (0.2, 0.4, 0.4)
    cutter.hide_render = True
    cutter.hide_set(True)
    cutter.name = "Section cutter"
    for o in parts:
        if o.type != "MESH":
            continue
        m = o.modifiers.new("section", "BOOLEAN")
        m.operation = "DIFFERENCE"
        m.object = cutter
    return cutter


def unsection(parts, cutter):
    for o in parts:
        m = o.modifiers.get("section")
        if m:
            o.modifiers.remove(m)
    bpy.data.objects.remove(cutter)


def export(parts, out, offsets=None):
    for o in bpy.context.scene.objects:
        o.select_set(False)
    for o in parts:
        o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=os.path.join(out, "ira_watch.glb"), export_format="GLB", use_selection=True,
                              export_extras=True, export_apply=True, export_yup=True, export_image_format="AUTO")
    meta = []
    seen = set()
    for o in parts:
        n = o.get("part") or o.name
        if n in seen or n not in PARTS:
            continue
        seen.add(n)
        meta.append({"name": n, "layer": o.get("layer") or n, "group": o.get("group") or None,
                     "what": PARTS[n][0], "feature": PARTS[n][1]})
    with open(os.path.join(out, "parts.json"), "w") as f:
        json.dump({"layers": LAYERS, "explode_mm": offsets or {}, "look": LOOK, "parts": meta, "parameters_mm": P}, f, indent=1)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/hardware")
    ap.add_argument("--screen", default=None, help="PNG of the watch screen (default: <out>/renders/screen_home.png)")
    ap.add_argument("--quick", action="store_true", help="low samples and size, for a test run")
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--only", default="", help="comma list of renders to make (hero,front,side,back,exploded,section,wrist_dock,turntable)")
    args = ap.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    out = os.path.abspath(args.out)
    renders = os.path.join(out, "renders")
    os.makedirs(renders, exist_ok=True)
    screen = args.screen or make_screen_png(os.path.join(renders, "screen_home.png"))
    wanted = set(args.only.split(",")) if args.only else {"hero", "front", "side", "back", "exploded", "section", "wrist_dock", "turntable"}
    # The turntable opens the film (1920 x 1080): 20 seconds, square, padded to 16:9 there.
    samples, res, vres, fps = (16, (640, 480), (320, 320), 6) if args.quick else (96, (1600, 1200), (960, 960), 15)

    reset()
    materials(screen)
    parts = build_watch()
    lights()
    render_setup(samples, res)
    R = P["case_diameter"] / 2 * MM
    H = P["case_height"] * MM
    centre = (0, 0, H / 2)
    cams = {
        "hero": camera("Hero", (0.105, -0.125, 0.095), centre, lens=85),
        "front": camera("Front", (0, 0, 0.26), centre, lens=100),
        "side": camera("Side", (0.26, 0, H / 2), centre, lens=100),
        "back": camera("Back", (0, 0, -0.26), centre, lens=100),
        "exploded": camera("Exploded", (0.12, -0.27, 0.112), (0, 0, 0.033), lens=58),
        "section": camera("Section", (0.24, -0.02, 0.03), (0, 0, H / 2 - 0.002), lens=120),
        "wrist": camera("Wrist", (0.21, -0.29, 0.12), (0.03, -0.025, -0.035), lens=60),
    }
    fl = floor(z=-(P["wrist_diameter"] + 8) * MM, size=8.0)
    if "hero" in wanted:
        render(cams["hero"], os.path.join(renders, "hero.png"))
    if "front" in wanted:
        render(cams["front"], os.path.join(renders, "front.png"))
    if "side" in wanted:
        render(cams["side"], os.path.join(renders, "side.png"))
    if "back" in wanted:
        # From below the strap would hide the back: it is lifted out of this one view.
        fl.hide_render = True
        band = [o for o in parts if o.get("layer") in ("Strap", "Clasp")]
        for o in band:
            o.hide_render = True
        fl.hide_render = True   # a plain background behind the labels
        render(cams["back"], os.path.join(renders, "back.png"))
        for o in band:
            o.hide_render = False
        fl.hide_render = False
    # exploded view, labelled after the render (draw_labels)
    if "exploded" in wanted:
        originals = {o.name: o.location.copy() for o in parts}
        explode(parts, EXPLODE_GAP_MM)
        band = [o for o in parts if o.get("layer") in ("Strap", "Clasp")]
        for o in band:
            o.hide_render = True
        bpy.context.view_layer.update()
        labels = []
        for i, nm in enumerate(EXPLODED_LAYERS):
            objs = [o for o in parts if (o.get("layer") or o.name) == nm and o.name.startswith(nm) and o.type == "MESH"]
            if not objs:
                continue
            o = max(objs, key=lambda ob: ob.dimensions.x * ob.dimensions.y)
            hx, hy = o.dimensions.x / 2, o.dimensions.y / 2
            side = 1 if i % 2 == 0 else -1
            _, top = layer_extent([o], o.get("layer") or o.name)
            # a point on the part's top face, toward the camera and toward its label's side
            point = (o.location.x + side * 0.45 * hx, o.location.y - 0.8 * hy, top * MM)
            text = "Case with buttons" if nm == "Case" else nm
            labels.append((text, anchor_2d(cams["exploded"], point), side))
        path = os.path.join(renders, "exploded.png")
        render(cams["exploded"], path)
        draw_labels(path, labels, font_px=max(18, res[0] // 42))
        for o in parts:
            o.location = originals[o.name]
        for o in band:
            o.hide_render = False
        fl.hide_render = False
        bpy.context.view_layer.update()
    if "section" in wanted:
        cutter = section(parts)
        render(cams["section"], os.path.join(renders, "section.png"))
        unsection(parts, cutter)
    # the watch on a wrist, with its dock beside it
    if "wrist_dock" in wanted:
        wrist = build_wrist()
        dock_parts = build_dock(location=(62.0, -48.0, -(P["wrist_diameter"] + 2.5)))
        fl.location.z = -(P["wrist_diameter"] + 2.5) * MM
        render(cams["wrist"], os.path.join(renders, "wrist_dock.png"))
        fl.location.z = -(P["wrist_diameter"] + 8) * MM
        bpy.data.objects.remove(wrist)
        for o in dock_parts:
            bpy.data.objects.remove(o)
    # turntable: the camera orbits the watch for TURNTABLE_S seconds
    if "turntable" in wanted and not args.no_video:
        s = bpy.context.scene
        empty = bpy.data.objects.new("Pivot", None)
        s.collection.objects.link(empty)
        cam = cams["hero"]
        cam.parent = empty
        s.frame_start, s.frame_end = 1, TURNTABLE_S * fps
        s.render.fps = fps
        # linear keys (set before inserting: the action API differs between Blender versions)
        bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
        empty.rotation_euler = (0, 0, 0)
        empty.keyframe_insert("rotation_euler", frame=1)
        empty.rotation_euler = (0, 0, math.radians(360))
        empty.keyframe_insert("rotation_euler", frame=s.frame_end + 1)
        s.render.resolution_x, s.render.resolution_y = vres
        s.cycles.samples = max(8, samples // 5)
        # Frames as PNG, then ffmpeg: the PyPI build of Blender has no video encoder of its own.
        frames_dir = os.path.join(out, "renders", "turntable_frames")
        os.makedirs(frames_dir, exist_ok=True)
        s.render.image_settings.file_format = "PNG"
        s.render.filepath = os.path.join(frames_dir, "frame_")
        s.camera = cam
        bpy.ops.render.render(animation=True)
        import shutil
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps), "-i", os.path.join(frames_dir, "frame_%04d.png"),
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22", "-movflags", "+faststart",
                            os.path.join(out, "turntable.mp4")], check=True)
            shutil.rmtree(frames_dir)
            print("rendered turntable", file=sys.stderr)
        else:
            print("ffmpeg not found: the frames are in", frames_dir, file=sys.stderr)
        s.render.image_settings.file_format = "PNG"
        s.render.resolution_x, s.render.resolution_y = res
        cam.parent = None
    export(parts, out, explode_offsets(parts, EXPLODE_GAP_MM))
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out, "ira_watch.blend"), compress=True)
    print("done", out, file=sys.stderr)


if __name__ == "__main__":
    main()
