import os
import io
import json
import math
import hashlib
import ezdxf
import streamlit as st
from PIL import Image
from pydantic import BaseModel, Field
from typing import Literal
from google import genai
from google.genai import types

# =====================================================================
# 1. STRICT ISO 1219 SCHEMA FOR GEMINI AI
# =====================================================================
TIPURI_COMPONENTE = Literal[
    "CYL_DOUBLE",  # Cilindru cu dublu efect (ports: P1, P2)
    "CYL_SINGLE",  # Cilindru cu simplu efect (ports: P1)
    "THROTTLE_CHECK_LEFT",  # Drosel de cale pt. portul 4 / stânga (ports: 1, 2)
    "THROTTLE_CHECK_RIGHT",  # Drosel de cale pt. portul 2 / dreapta (ports: 1, 2)
    "VALVE_5_3_VENTED",  # Distribuitor 5/3 centru ventilat/evacuat (ports: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_3_CLOSED",  # Distribuitor 5/3 centru închis (ports: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_3_PRESSURIZED",  # Distribuitor 5/3 centru sub presiune (ports: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_2_BISTABLE",  # Distribuitor 5/2 bistabil (ports: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_2_MONOSTABLE",  # Distribuitor 5/2 monostabil cu arc (ports: 1, 2, 4, 3, 5, 14)
    "VALVE_3_2_NC_BUTTON",  # Distribuitor 3/2 NI cu buton și arc (ports: 1, 2, 3)
    "VALVE_3_2_NC_ROLLER",  # Distribuitor 3/2 NI cu rolă (limitator) (ports: 1, 2, 3)
    "LOGIC_OR",  # Supapă logică SAU / Selecție (ports: 1L, 1R, 2)
    "LOGIC_AND",  # Supapă logică ȘI / Simultaneitate (ports: 1L, 1R, 2)
    "FRL_EXPANDED",  # Grup FRL desfășurat: Filtru + Regulator + Manometru + Lubrificator (ports: 1, 2)
    "FRL_MACRO",  # Grup FRL simbol simplificat pe scurt ISO 1219 (ports: 1, 2)
    "AIR_SOURCE"  # Sursă aer comprimat / Compresor (ports: 1)
]


class ComponentaISO(BaseModel):
    id: str = Field(
        description="ISO label: M1 (cylinder), V2/V3 (throttles), V1 (main valve), S1/S2 (3/2 buttons), Z1 (FRL), P1 (Air source)")
    tip: TIPURI_COMPONENTE = Field(description="Exact ISO 1219 component type from the allowed list")
    nume: str = Field(description="Romanian technical name, e.g., Distribuitor 5/3 cu centru ventilat")
    parametru: str = Field(description="Technical spec, e.g., '6 bar', '50% drosel', 'Ø50x200 mm'")
    x: int = Field(description="Grid X coordinate (multiple of 20, between 140 and 660). Center main column at X=400.")
    y: int = Field(
        description="FluidSIM Layer Y coordinate (top=90 for Cylinder, 210 for Throttles, 340 for Main Valve, 460 for FRL, 550 for Source)")


class ConexiuneISO(BaseModel):
    de_la: str = Field(description="Source ComponentID.PortID, e.g., 'P1.1', 'Z1.2', 'V1.4', 'V1.2', 'V2.2', 'V3.2'")
    pana_la: str = Field(
        description="Target ComponentID.PortID, e.g., 'Z1.1', 'V1.1', 'V2.1', 'V3.1', 'M1.P1', 'M1.P2'")
    circuit_activ_in_starea: Literal["PERMANENT", "STARE_14", "STARE_12", "PILOT"] = Field(
        description="When this pipe has high pressure: PERMANENT (supply lines), STARE_14 (left port 4 path), STARE_12 (right port 2 path)"
    )


class PasFluidSIM(BaseModel):
    pas: int
    element: str
    cum_se_pune_in_fluidsim: str = Field(
        description="Exact Festo FluidSIM-P library path, how to configure properties (double-click settings, actuators, center position) and port connections"
    )


class ProiectFluidSIM(BaseModel):
    titlu: str
    explicatie_centru_si_ansamblu: str = Field(
        description="Detailed explanation of the valve center state and full assembly operation")
    simbol_macro_descriere: str = Field(
        description="How this assembly is represented as a single simplified Macro block vs expanded")
    componente: list[ComponentaISO]
    conexiuni: list[ConexiuneISO]
    ghid_fluidsim: list[PasFluidSIM]


# =====================================================================
# 2. DETERMINISTIC ISO 1219 CAD VECTOR ENGINE (SHARED BY SVG & DXF)
# =====================================================================
def get_component_geometry(comp: dict):
    t = comp["tip"]
    cx, cy = comp["x"], comp["y"]
    prims = []
    pins = {}

    def L(x1, y1, x2, y2, dashed=False, group="static", role="body"):
        prims.append(
            {"k": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "dashed": dashed, "group": group, "role": role})

    def R(x, y, w, h, dashed=False, group="static", role="box"):
        prims.append({"k": "rect", "x": x, "y": y, "w": w, "h": h, "dashed": dashed, "group": group, "role": role})

    def C(x, y, r, group="static", role="body"):
        prims.append({"k": "circle", "x": x, "y": y, "r": r, "group": group, "role": role})

    def A(x1, y1, x2, y2, group="static", role="flow"):
        prims.append({"k": "arrow", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "group": group, "role": role})

    def T(x, y, direction="down", group="static"):
        if direction == "down":
            L(x, y, x, y - 10, group=group, role="stop")
            L(x - 5, y - 10, x + 5, y - 10, group=group, role="stop")
        else:
            L(x, y, x, y + 10, group=group, role="stop")
            L(x - 5, y + 10, x + 5, y + 10, group=group, role="stop")

    def TXT(x, y, s, size=10, bold=False):
        prims.append({"k": "text", "x": x, "y": y, "s": str(s), "size": size, "bold": bold, "group": "static"})

    def EXHAUST_TRI(x, y):
        prims.append({"k": "poly", "pts": [(x - 5, y), (x + 5, y), (x, y + 9)], "group": "static", "role": "exhaust"})

    if t in ("CYL_DOUBLE", "CYL_SINGLE"):
        R(cx - 70, cy - 26, 140, 52)
        L(cx - 55, cy + 26, cx - 55, cy + 40)
        pins["P1"] = (cx - 55, cy + 40)
        TXT(cx - 68, cy + 38, "P1", 9)

        if t == "CYL_DOUBLE":
            L(cx + 55, cy + 26, cx + 55, cy + 40)
            pins["P2"] = (cx + 55, cy + 40)
            TXT(cx + 60, cy + 38, "P2", 9)
        else:
            pts = [(cx - 10, cy - 18), (cx + 5, cy + 18), (cx + 20, cy - 18), (cx + 35, cy + 18), (cx + 50, cy - 18),
                   (cx + 65, cy + 18)]
            for i in range(len(pts) - 1):
                L(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])

        R(cx - 32, cy - 24, 12, 48, group="piston", role="piston")
        R(cx - 37, cy - 10, 5, 20, group="piston", role="cushion")
        R(cx - 20, cy - 10, 5, 20, group="piston", role="cushion")
        L(cx - 20, cy - 4, cx + 105, cy - 4, group="piston", role="rod")
        L(cx - 20, cy + 4, cx + 105, cy + 4, group="piston", role="rod")
        R(cx + 105, cy - 10, 6, 20, group="piston", role="rod_end")

        TXT(cx - 110, cy - 5, comp["id"], 12, bold=True)
        TXT(cx - 70, cy - 34, f"{comp['nume']} ({comp['parametru']})", 9)

    elif t in ("THROTTLE_CHECK_LEFT", "THROTTLE_CHECK_RIGHT"):
        R(cx - 28, cy - 28, 56, 56, dashed=True)
        L(cx, cy - 40, cx, cy - 28)
        L(cx, cy + 28, cx, cy + 40)
        pins["2"] = (cx, cy - 40)
        pins["1"] = (cx, cy + 40)
        TXT(cx - 14, cy - 32, "2", 9)
        TXT(cx - 14, cy + 38, "1", 9)

        L(cx, cy - 28, cx - 12, cy - 28)
        L(cx - 12, cy - 28, cx - 12, cy + 28)
        L(cx - 12, cy + 28, cx, cy + 28)
        L(cx - 19, cy - 12, cx - 15, cy)
        L(cx - 15, cy, cx - 19, cy + 12)
        L(cx - 5, cy - 12, cx - 9, cy)
        L(cx - 9, cy, cx - 5, cy + 12)
        A(cx - 23, cy + 14, cx - 1, cy - 14)

        L(cx, cy - 28, cx + 14, cy - 28)
        L(cx + 14, cy - 28, cx + 14, cy + 28)
        L(cx + 14, cy + 28, cx, cy + 28)
        L(cx + 8, cy - 4, cx + 14, cy + 4)
        L(cx + 20, cy - 4, cx + 14, cy + 4)
        C(cx + 14, cy - 3, 4)

        TXT(cx - 60, cy + 4, comp["id"], 11, bold=True)
        TXT(cx + 33, cy + 4, comp["parametru"], 8)

    elif t.startswith("VALVE_5_"):
        is_5_3 = "5_3" in t
        p4x, p2x = cx - 13, cx + 13
        p5x, p1x, p3x = cx - 15, cx, cx + 15

        L(p4x, cy - 22, p4x, cy - 38)
        L(p2x, cy - 22, p2x, cy - 38)
        L(p5x, cy + 22, p5x, cy + 34)
        L(p1x, cy + 22, p1x, cy + 38)
        L(p3x, cy + 22, p3x, cy + 34)

        EXHAUST_TRI(p5x, cy + 34)
        EXHAUST_TRI(p3x, cy + 34)

        pins["4"] = (p4x, cy - 38)
        pins["2"] = (p2x, cy - 38)
        pins["1"] = (p1x, cy + 38)
        pins["5"] = (p5x, cy + 43)
        pins["3"] = (p3x, cy + 43)

        TXT(p4x - 11, cy - 27, "4", 9, bold=True)
        TXT(p2x + 5, cy - 27, "2", 9, bold=True)
        TXT(p5x - 11, cy + 33, "5", 9, bold=True)
        TXT(p1x + 4, cy + 35, "1", 9, bold=True)
        TXT(p3x + 5, cy + 33, "3", 9, bold=True)

        def draw_square_14(scx, grp):
            R(scx - 22, cy - 22, 44, 44, group=grp)
            A(scx, cy + 22, scx - 13, cy - 22, group=grp)
            A(scx + 13, cy - 22, scx + 15, cy + 22, group=grp)
            T(scx - 15, cy + 22, "down", group=grp)

        def draw_square_12(scx, grp):
            R(scx - 22, cy - 22, 44, 44, group=grp)
            A(scx, cy + 22, scx + 13, cy - 22, group=grp)
            A(scx - 13, cy - 22, scx - 15, cy + 22, group=grp)
            T(scx + 15, cy + 22, "down", group=grp)

        def draw_square_center(scx, grp):
            R(scx - 22, cy - 22, 44, 44, group=grp)
            if t == "VALVE_5_3_VENTED":
                T(scx, cy + 22, "down", group=grp)
                A(scx - 13, cy - 22, scx - 15, cy + 22, group=grp)
                A(scx + 13, cy - 22, scx + 15, cy + 22, group=grp)
            elif t == "VALVE_5_3_CLOSED":
                T(scx, cy + 22, "down", group=grp)
                T(scx - 15, cy + 22, "down", group=grp)
                T(scx + 15, cy + 22, "down", group=grp)
                T(scx - 13, cy - 22, "up", group=grp)
                T(scx + 13, cy - 22, "up", group=grp)
            elif t == "VALVE_5_3_PRESSURIZED":
                L(scx, cy + 22, scx, cy, group=grp)
                A(scx, cy, scx - 13, cy - 22, group=grp)
                A(scx, cy, scx + 13, cy - 22, group=grp)
                T(scx - 15, cy + 22, "down", group=grp)
                T(scx + 15, cy + 22, "down", group=grp)

        if is_5_3:
            draw_square_14(cx - 44, "valve_spool")
            draw_square_center(cx, "valve_spool")
            draw_square_12(cx + 44, "valve_spool")
            left_edge, right_edge = cx - 66, cx + 66
        else:
            draw_square_14(cx - 44, "valve_spool")
            draw_square_12(cx, "valve_spool")
            left_edge, right_edge = cx - 66, cx + 22

        R(left_edge - 34, cy - 12, 24, 24, group="valve_spool")
        L(left_edge - 34, cy + 12, left_edge - 10, cy - 12, group="valve_spool")
        prims.append({"k": "poly", "pts": [(left_edge - 10, cy - 6), (left_edge - 10, cy + 6), (left_edge, cy)],
                      "group": "valve_spool", "role": "pilot"})
        if is_5_3:
            pts_l = [(left_edge - 10, cy - 14), (left_edge - 7, cy - 21), (left_edge - 4, cy - 14),
                     (left_edge - 1, cy - 21), (left_edge, cy - 14)]
            for i in range(len(pts_l) - 1):
                L(pts_l[i][0], pts_l[i][1], pts_l[i + 1][0], pts_l[i + 1][1], group="valve_spool")
        TXT(left_edge - 32, cy - 16, "14", 9, bold=True)
        pins["14"] = (left_edge - 34, cy)

        if t == "VALVE_5_2_MONOSTABLE":
            pts_r = [(right_edge, cy), (right_edge + 6, cy - 10), (right_edge + 12, cy + 10),
                     (right_edge + 18, cy - 10), (right_edge + 24, cy)]
            for i in range(len(pts_r) - 1):
                L(pts_r[i][0], pts_r[i][1], pts_r[i + 1][0], pts_r[i + 1][1], group="valve_spool")
        else:
            R(right_edge + 10, cy - 12, 24, 24, group="valve_spool")
            L(right_edge + 10, cy + 12, right_edge + 34, cy - 12, group="valve_spool")
            prims.append({"k": "poly", "pts": [(right_edge + 10, cy - 6), (right_edge + 10, cy + 6), (right_edge, cy)],
                          "group": "valve_spool", "role": "pilot"})
            if is_5_3:
                pts_r = [(right_edge, cy - 14), (right_edge + 3, cy - 21), (right_edge + 6, cy - 14),
                         (right_edge + 9, cy - 21), (right_edge + 10, cy - 14)]
                for i in range(len(pts_r) - 1):
                    L(pts_r[i][0], pts_r[i][1], pts_r[i + 1][0], pts_r[i + 1][1], group="valve_spool")
            TXT(right_edge + 14, cy - 16, "12", 9, bold=True)
            pins["12"] = (right_edge + 34, cy)

        TXT(left_edge - 34, cy + 36, f"{comp['id']}: {comp['nume']}", 9, bold=True)

    elif t.startswith("VALVE_3_2"):
        R(cx - 36, cy - 18, 36, 36)
        R(cx, cy - 18, 36, 36)
        A(cx - 18, cy + 18, cx - 18, cy - 18)
        T(cx - 8, cy + 18, "down")
        T(cx + 12, cy + 18, "down")
        A(cx + 18, cy - 18, cx + 28, cy + 18)
        EXHAUST_TRI(cx + 28, cy + 18)

        L(cx + 18, cy - 18, cx + 18, cy - 30)
        L(cx + 12, cy + 18, cx + 12, cy + 30)
        pins["2"] = (cx + 18, cy - 30)
        pins["1"] = (cx + 12, cy + 30)
        pins["3"] = (cx + 28, cy + 27)
        TXT(cx + 22, cy - 22, "2", 8)
        TXT(cx + 2, cy + 28, "1", 8)
        TXT(cx + 32, cy + 26, "3", 8)

        if "BUTTON" in t:
            L(cx - 36, cy - 5, cx - 50, cy - 5)
            L(cx - 36, cy + 5, cx - 50, cy + 5)
            L(cx - 50, cy - 12, cx - 50, cy + 12)
        else:
            L(cx - 36, cy, cx - 52, cy)
            C(cx - 56, cy, 5)
        pts_s = [(cx + 36, cy), (cx + 42, cy - 8), (cx + 48, cy + 8), (cx + 54, cy - 8), (cx + 60, cy)]
        for i in range(len(pts_s) - 1):
            L(pts_s[i][0], pts_s[i][1], pts_s[i + 1][0], pts_s[i + 1][1])
        TXT(cx - 55, cy - 24, f"{comp['id']} ({comp['nume']})", 8, bold=True)

    elif t in ("LOGIC_OR", "LOGIC_AND"):
        R(cx - 28, cy - 18, 56, 36)
        L(cx - 28, cy, cx - 40, cy)
        L(cx + 28, cy, cx + 40, cy)
        L(cx, cy - 18, cx, cy - 30)
        pins["1L"] = (cx - 40, cy)
        pins["1R"] = (cx + 40, cy)
        pins["2"] = (cx, cy - 30)
        TXT(cx - 38, cy - 6, "1", 8)
        TXT(cx + 32, cy - 6, "1", 8)
        TXT(cx + 4, cy - 22, "2", 8)
        if t == "LOGIC_OR":
            L(cx - 20, cy - 8, cx - 12, cy)
            L(cx - 20, cy + 8, cx - 12, cy)
            L(cx + 20, cy - 8, cx + 12, cy)
            L(cx + 20, cy + 8, cx + 12, cy)
            C(cx, cy, 5)
        else:
            L(cx - 14, cy - 12, cx - 14, cy + 12)
            L(cx + 14, cy - 12, cx + 14, cy + 12)
            L(cx - 8, cy - 10, cx - 8, cy + 10)
        TXT(cx - 28, cy + 28, f"{comp['id']}: {comp['nume']}", 8, bold=True)

    elif t in ("FRL_EXPANDED", "FRL_MACRO"):
        L(cx, cy - 38, cx, cy - 24)
        L(cx, cy + 24, cx, cy + 38)
        pins["2"] = (cx, cy - 38)
        pins["1"] = (cx, cy + 38)
        TXT(cx + 5, cy - 30, "2", 9)
        TXT(cx + 5, cy + 34, "1", 9)

        R(cx - 26, cy - 24, 52, 48)
        L(cx - 26, cy + 10, cx + 26, cy + 10, dashed=True)
        A(cx, cy + 8, cx, cy - 14)
        L(cx + 26, cy, cx + 40, cy)
        C(cx + 50, cy, 10)
        A(cx + 44, cy + 6, cx + 56, cy - 6)
        L(cx - 12, cy - 24, cx - 12, cy - 14)

        TXT(cx - 72, cy + 4, comp["id"], 11, bold=True)
        TXT(cx + 64, cy + 4, f"{comp['nume']} ({comp['parametru']})", 8)

    elif t == "AIR_SOURCE":
        L(cx, cy - 24, cx, cy - 8)
        pins["1"] = (cx, cy - 24)
        prims.append({"k": "poly", "pts": [(cx, cy - 8), (cx - 12, cy + 14), (cx + 12, cy + 14)], "group": "static",
                      "role": "source"})
        TXT(cx + 16, cy + 8, f"{comp['id']} ({comp['parametru']})", 9, bold=True)

    return prims, pins


def compute_orthogonal_wires(schema: dict, all_pins: dict):
    wires = []
    for idx, conn in enumerate(schema["conexiuni"]):
        p_start = all_pins.get(conn["de_la"])
        p_end = all_pins.get(conn["pana_la"])
        if not p_start or not p_end:
            continue
        x1, y1 = p_start
        x2, y2 = p_end
        if x1 == x2 or y1 == y2:
            pts = [(x1, y1), (x2, y2)]
        else:
            mid_y = int((y1 + y2) / 2) + ((idx % 3) - 1) * 6
            pts = [(x1, y1), (x1, mid_y), (x2, mid_y), (x2, y2)]
        wires.append({
            "pts": pts,
            "stare": conn["circuit_activ_in_starea"],
            "label": f"{conn['de_la']} -> {conn['pana_la']}"
        })
    return wires


# =====================================================================
# 3. 1:1 CAD DXF EXPORT (EXACT SAME VECTORS AS SCREEN)
# =====================================================================
def export_exact_dxf(schema: dict) -> bytes:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    doc.layers.add("ISO_SYMBOLS", color=7)
    doc.layers.add("PNEUMATIC_LINES", color=5)
    doc.layers.add("LABELS", color=3)

    all_pins = {}

    def fy(y):
        return 650 - y

    for comp in schema["componente"]:
        prims, pins = get_component_geometry(comp)
        for port_name, (px, py) in pins.items():
            all_pins[f"{comp['id']}.{port_name}"] = (px, py)

        for p in prims:
            k = p["k"]
            if k == "line":
                msp.add_line((p["x1"], fy(p["y1"])), (p["x2"], fy(p["y2"])), dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "rect":
                x, y, w, h = p["x"], p["y"], p["w"], p["h"]
                pts = [(x, fy(y)), (x + w, fy(y)), (x + w, fy(y + h)), (x, fy(y + h)), (x, fy(y))]
                msp.add_lwpolyline(pts, dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "circle":
                msp.add_circle((p["x"], fy(p["y"])), p["r"], dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "poly":
                pts = [(pt[0], fy(pt[1])) for pt in p["pts"]] + [(p["pts"][0][0], fy(p["pts"][0][1]))]
                msp.add_lwpolyline(pts, dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "arrow":
                x1, y1, x2, y2 = p["x1"], fy(p["y1"]), p["x2"], fy(p["y2"])
                msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "ISO_SYMBOLS"})
                ang = math.atan2(y2 - y1, x2 - x1)
                a1 = ang + math.pi * 0.85
                a2 = ang - math.pi * 0.85
                msp.add_lwpolyline([
                    (x2, y2),
                    (x2 + 6 * math.cos(a1), y2 + 6 * math.sin(a1)),
                    (x2 + 6 * math.cos(a2), y2 + 6 * math.sin(a2)),
                    (x2, y2)
                ], dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "text":
                msp.add_text(p["s"],
                             dxfattribs={"height": p["size"] * 0.65, "insert": (p["x"], fy(p["y"])), "layer": "LABELS"})

    wires = compute_orthogonal_wires(schema, all_pins)
    for w in wires:
        pts_cad = [(pt[0], fy(pt[1])) for pt in w["pts"]]
        msp.add_lwpolyline(pts_cad, dxfattribs={"layer": "PNEUMATIC_LINES"})

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


# =====================================================================
# 4. INTERACTIVE FLUIDSIM-P CANVAS RENDERER (HTML5 + SVG)
# =====================================================================
def render_fluidsim_canvas(schema: dict) -> str:
    all_pins = {}
    svg_static = []
    svg_spool = []
    svg_piston = []

    for comp in schema["componente"]:
        prims, pins = get_component_geometry(comp)
        for port_name, (px, py) in pins.items():
            all_pins[f"{comp['id']}.{port_name}"] = (px, py)

        for p in prims:
            k = p["k"]
            dash = 'stroke-dasharray="4,3"' if p.get("dashed") else ""
            elem = ""
            if k == "line":
                elem = f'<line x1="{p["x1"]}" y1="{p["y1"]}" x2="{p["x2"]}" y2="{p["y2"]}" stroke="#0f172a" stroke-width="2" {dash}/>'
            elif k == "rect":
                fill = "#ffffff" if p.get("role") == "box" else ("#94a3b8" if p.get("role") == "piston" else "none")
                if p.get("dashed"):
                    fill = "none"
                elem = f'<rect x="{p["x"]}" y="{p["y"]}" width="{p["w"]}" height="{p["h"]}" fill="{fill}" stroke="#0f172a" stroke-width="2" {dash}/>'
            elif k == "circle":
                elem = f'<circle cx="{p["x"]}" cy="{p["y"]}" r="{p["r"]}" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>'
            elif k == "poly":
                pts_str = " ".join(f"{pt[0]},{pt[1]}" for pt in p["pts"])
                fill = "#38bdf8" if p.get("role") == "exhaust" else "#ffffff"
                elem = f'<polygon points="{pts_str}" fill="{fill}" stroke="#0f172a" stroke-width="1.8"/>'
            elif k == "arrow":
                elem = f'<line x1="{p["x1"]}" y1="{p["y1"]}" x2="{p["x2"]}" y2="{p["y2"]}" stroke="#0f172a" stroke-width="2" marker-end="url(#isoArrow)"/>'
            elif k == "text":
                weight = "bold" if p.get("bold") else "normal"
                elem = f'<text x="{p["x"]}" y="{p["y"]}" fill="#0f172a" font-family="Consolas, monospace" font-size="{p["size"]}" font-weight="{weight}">{p["s"]}</text>'

            if p.get("group") == "valve_spool":
                svg_spool.append(elem)
            elif p.get("group") == "piston":
                svg_piston.append(elem)
            else:
                svg_static.append(elem)

    wires = compute_orthogonal_wires(schema, all_pins)
    svg_wires = []
    for w in wires:
        pts_str = " ".join(f"{pt[0]},{pt[1]}" for pt in w["pts"])
        stare = w["stare"]
        svg_wires.append(
            f'<polyline class="pipe pipe-{stare}" points="{pts_str}" fill="none" stroke="#0284c7" stroke-width="3.2" stroke-linejoin="round"/>'
        )
        for pt in (w["pts"][0], w["pts"][-1]):
            svg_wires.append(f'<circle cx="{pt[0]}" cy="{pt[1]}" r="3" fill="#0f172a"/>')

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin: 0; font-family: 'Segoe UI', system-ui, sans-serif; background: #1e293b; color: #f8fafc; user-select: none; }}
  .toolbar {{
    display: flex; flex-wrap: wrap; gap: 6px; padding: 10px; background: #0f172a;
    border-bottom: 2px solid #334155; align-items: center; justify-content: space-between;
  }}
  .btn-group {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  button {{
    background: #334155; color: #f8fafc; border: 1px solid #475569; padding: 8px 12px;
    border-radius: 6px; font-weight: 600; font-size: 12px; cursor: pointer; transition: 0.15s;
  }}
  button:hover {{ background: #475569; }}
  button.active {{ background: #0055cc; border-color: #60a5fa; box-shadow: 0 0 8px rgba(56,189,248,0.5); }}
  .MacroBtn {{ background: #4f46e5; border-color: #818cf8; }}
  .canvas-wrap {{
    background-color: #f8fafc;
    background-image: radial-gradient(#cbd5e1 1.2px, transparent 1.2px);
    background-size: 20px 20px;
    width: 100%; height: 540px; overflow: auto; position: relative;
  }}
  svg {{ width: 100%; height: 100%; min-width: 720px; min-height: 540px; }}
  #valveSpool, #pistonGroup {{ transition: transform 0.38s cubic-bezier(0.22, 1, 0.36, 1); }}
  .legend {{
    display: flex; gap: 16px; padding: 8px 14px; background: #0f172a; font-size: 12px;
    border-top: 1px solid #334155; align-items: center; justify-content: space-between;
  }}
  .dot {{ display: inline-block; width: 12px; height: 4px; margin-right: 5px; vertical-align: middle; }}
</style>
</head>
<body>

<div class="toolbar">
  <div class="btn-group">
    <button id="btn14" onclick="setMode('14')">▶ Comută Starea 14 (Avans Cilindru)</button>
    <button id="btn0" class="active" onclick="setMode('0')">⏸ Poziția Centrală (0 - Repaus/Ventilat)</button>
    <button id="btn12" onclick="setMode('12')">◀ Comută Starea 12 (Retragere Cilindru)</button>
  </div>
  <button class="MacroBtn" id="btnView" onclick="toggleMacro()">🔄 Vedere: Ansamblu Desfășurat ISO 1219</button>
</div>

<div class="canvas-wrap">
  <svg viewBox="0 0 800 600" id="fluidsimSvg">
    <defs>
      <marker id="isoArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
        <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="#0f172a"/>
      </marker>
    </defs>

    <g id="expandedLayer">
      {''.join(svg_wires)}
      {''.join(svg_static)}
      <g id="valveSpool" transform="translate(0, 0)">
        {''.join(svg_spool)}
      </g>
      <g id="pistonGroup" transform="translate(0, 0)">
        {''.join(svg_piston)}
      </g>
    </g>

    <g id="macroLayer" style="display:none;">
      <rect x="210" y="150" width="380" height="240" rx="8" fill="#ffffff" stroke="#0f172a" stroke-width="3" stroke-dasharray="10,5"/>
      <rect x="225" y="165" width="350" height="210" fill="#f1f5f9" stroke="#0284c7" stroke-width="2"/>
      <text x="400" y="220" text-anchor="middle" fill="#0f172a" font-size="16" font-weight="bold">BLOC MACRO FLUIDSIM (SIMBOL PE SCURT)</text>
      <text x="400" y="250" text-anchor="middle" fill="#0055cc" font-size="14" font-weight="bold">{schema['titlu']}</text>
      <text x="400" y="285" text-anchor="middle" fill="#334155" font-size="12">{schema['simbol_macro_descriere'][:85]}...</text>
      <line x1="400" y1="390" x2="400" y2="470" stroke="#0055cc" stroke-width="4"/>
      <polygon points="400,470 388,492 412,492" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>
      <text x="418" y="445" fill="#0f172a" font-size="12" font-weight="bold">Port 1 (Alimentare 6 bar) + Comenzi 14 / 12</text>
    </g>
  </svg>
</div>

<div class="legend">
  <div>
    <span><span class="dot" style="background:#0055cc;"></span>Presiune Activă (6 bar)</span>
    <span style="margin-left:14px;"><span class="dot" style="background:#38bdf8;"></span>Conductă Ventilată / Evacuată la Atmosferă (0 bar)</span>
  </div>
  <div id="statusLabel" style="color:#38bdf8; font-weight:600;">Stare: 0 (Centru) — Caseta centrală este aliniată cu porturile 1, 2, 4, 3, 5</div>
</div>

<script>
  let isMacro = false;

  function setMode(m) {{
    ['14', '0', '12'].forEach(id => document.getElementById('btn' + id).classList.remove('active'));
    document.getElementById('btn' + m).classList.add('active');

    const spool = document.getElementById('valveSpool');
    const piston = document.getElementById('pistonGroup');
    const status = document.getElementById('statusLabel');

    const pipesPerm = document.querySelectorAll('.pipe-PERMANENT');
    const pipes14 = document.querySelectorAll('.pipe-STARE_14');
    const pipes12 = document.querySelectorAll('.pipe-STARE_12');

    pipesPerm.forEach(p => {{ p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); }});

    if (m === '14') {{
      spool.setAttribute('transform', 'translate(44, 0)');
      piston.setAttribute('transform', 'translate(45, 0)');
      pipes14.forEach(p => {{ p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); }});
      pipes12.forEach(p => {{ p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); }});
      status.innerText = 'Stare 14 Activă: Caseta 14 a culisat pe porturi (1→4 presiune, 2→3 evacuare). Tija avansează!';
    }} else if (m === '0') {{
      spool.setAttribute('transform', 'translate(0, 0)');
      pipes14.forEach(p => {{ p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); }});
      pipes12.forEach(p => {{ p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); }});
      status.innerText = 'Stare 0 (Centru): Caseta centrală este pe porturi (1 blocat, 4→5 și 2→3 ventilate la atmosferă).';
    }} else if (m === '12') {{
      spool.setAttribute('transform', 'translate(-44, 0)');
      piston.setAttribute('transform', 'translate(-22, 0)');
      pipes14.forEach(p => {{ p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); }});
      pipes12.forEach(p => {{ p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); }});
      status.innerText = 'Stare 12 Activă: Caseta 12 a culisat pe porturi (1→2 presiune, 4→5 evacuare). Tija se retrage!';
    }}
  }}

  function toggleMacro() {{
    isMacro = !isMacro;
    document.getElementById('expandedLayer').style.display = isMacro ? 'none' : 'block';
    document.getElementById('macroLayer').style.display = isMacro ? 'block' : 'none';
    document.getElementById('btnView').innerText = isMacro
      ? '🔄 Vedere: Simbol Macro Simplificat (Click pt. Ansamblu Desfășurat)'
      : '🔄 Vedere: Ansamblu Desfășurat ISO 1219 (Click pt. Simbol Macro)';
  }}

  setMode('0');
</script>
</body>
</html>"""
    return html


# =====================================================================
# 5. GEMINI AI ENGINE (STRUCTURED ISO 1219 LAYOUT + CACHE)
# =====================================================================
CACHE_FILE = "scheme_iso1219_cache.json"


def genereaza_proiect_fluidsim(api_key: str, model_name: str, cerinta_text: str = None, imagine: Image.Image = None,
                               foloseste_cache: bool = True) -> dict:
    cache = json.load(open(CACHE_FILE, "r", encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}

    if imagine is not None:
        img_bytes = io.BytesIO()
        imagine.save(img_bytes, format="PNG")
        img_hash = hashlib.md5(img_bytes.getvalue()).hexdigest()[:12]
        cheie_cache = f"v3_foto_{img_hash}_{(cerinta_text or '').strip().lower()}"
    else:
        cheie_cache = f"v3_text_{(cerinta_text or '').strip().lower()}"

    if foloseste_cache and cheie_cache in cache:
        st.toast("Încărcat instant din Cache Local (0 consum API!)", icon="⚡")
        return cache[cheie_cache]

    client = genai.Client(api_key=api_key)
    prompt = """
    Ești inginer proiectant expert în Festo FluidSIM-P și standardul ISO 1219-1 / ISO 1219-2.
    Utilizatorul îți cere o piesă sau o schemă (sau îți trimite o poză).

    REGULI OBLIGATORII DE PROIECTARE FLUIDSIM:
    1. Desfășoară ÎNTOTDEAUNA ansamblul complet pe straturi ierarhice (de jos în sus, ca în FluidSIM):
       - Y = 540, X = 400: Sursa de aer comprimat `P1` (`AIR_SOURCE`, port `1`).
       - Y = 450, X = 400: Grupul de preparare a aerului `Z1` (`FRL_EXPANDED`, porturi `1` jos, `2` sus).
       - Y = 340, X = 400: Distribuitorul principal `V1` (ex: `VALVE_5_3_VENTED`, `VALVE_5_3_CLOSED`, `VALVE_5_3_PRESSURIZED`, `VALVE_5_2_BISTABLE`, `VALVE_5_2_MONOSTABLE`).
       - Y = 215, X = 320 (pentru calea 4) și X = 480 (pentru calea 2): Drosele cu supapă de sens `V2` (`THROTTLE_CHECK_LEFT`) și `V3` (`THROTTLE_CHECK_RIGHT`).
       - Y = 90,  X = 400: Cilindrul `M1` (`CYL_DOUBLE` cu porturile `P1` în stânga și `P2` în dreapta, sau `CYL_SINGLE`).
    2. Conectează porturile exact cu ID-urile lor reale:
       - `P1.1` -> `Z1.1` (`PERMANENT`)
       - `Z1.2` -> `V1.1` (`PERMANENT`)
       - `V1.4` -> `V2.1` (`STARE_14`)
       - `V2.2` -> `M1.P1` (`STARE_14`)
       - `V1.2` -> `V3.1` (`STARE_12`)
       - `V3.2` -> `M1.P2` (`STARE_12`)
    3. În `ghid_fluidsim`, explică strict cum se construiește și se configurează fiecare element în Festo FluidSIM-P (din ce categorie a bibliotecii îl tragi pe foaie, ce setări bifezi la dublu-click pentru poziția de centru, arcuri, bobine 14/12, amortizare și cum setezi evacuările pe porturile 3 și 5).
    """

    continut = [imagine, prompt + f"\nCerință: {cerinta_text or ''}"] if imagine else [
        prompt + f"\nCerință: {cerinta_text}"]

    response = client.models.generate_content(
        model=model_name,
        contents=continut,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ProiectFluidSIM,
            temperature=0.1,
        ),
    )

    rezultat = json.loads(response.text)
    cache[cheie_cache] = rezultat
    json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return rezultat


# =====================================================================
# 6. STREAMLIT INTERFACE (PC & MOBILE)
# =====================================================================
st.set_page_config(page_title="FluidSIM-P ISO 1219 CAD & Simulator", layout="wide")
st.title("⚙️ Festo FluidSIM-P CAD Generator & Simulator ISO 1219 (1:1)")

try:
    default_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    default_key = os.environ.get("GEMINI_API_KEY", "")

with st.sidebar:
    st.header("🔑 Configurare AI & Cache")
    api_key = st.text_input("Gemini API Key:", value=default_key, type="password")

    model_ales = st.text_input(
        "Scrie ID-ul modelului AI:",
        value="gemini-2.5-flash",
        help="Poți scrie orice model (ex: gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.0-flash, gemini-3-flash-preview)"
    ).strip()

    if st.button("🔍 Vezi modelele permise de cheia ta"):
        if not api_key:
            st.warning("Introdu mai întâi cheia API mai sus!")
        else:
            try:
                client_test = genai.Client(api_key=api_key)
                modele = [
                    m.name.replace("models/", "")
                    for m in client_test.models.list()
                    if "gemini" in m.name
                ]
                st.success("Copiază oricare ID de mai jos în căsuța de model:")
                st.code("\n".join(modele))
            except Exception as e:
                st.error(f"Eroare la citirea modelelor: {e}")

    foloseste_cache = st.checkbox("Folosește Cache Local (0 consum API la repetare)", value=True)
    st.divider()
    st.caption(
        "Tot ce vezi pe ecran este generat cu același motor vectorial ISO 1219 ca și fișierul .DXF pentru FluidSIM.")

tab_text, tab_foto = st.tabs(["🔤 Generează Schemă după Denumire / Ansamblu", "📸 Recunoaște Schemă din Poză"])

if "schema_activa" not in st.session_state:
    st.session_state["schema_activa"] = None

with tab_text:
    col_in, col_btn = st.columns([4, 1])
    with col_in:
        cerinta = st.text_input(
            "Scrie denumirea distribuitorului sau a ansamblului pneumatic:",
            value="Distribuitor 5/3 cu centru ventilat în ansamblu cu cilindru dublu efect, drosele și grup FRL"
        )
    with col_btn:
        st.write("")
        st.write("")
        gen_click = st.button("🚀 Generează Schema 1:1", use_container_width=True)

    if gen_click:
        if not api_key:
            st.error("Introdu cheia Gemini API în panoul din stânga!")
        elif cerinta:
            with st.spinner(f"Se construiește geometria ISO 1219 folosind {model_ales}..."):
                st.session_state["schema_activa"] = genereaza_proiect_fluidsim(
                    api_key, model_ales, cerinta_text=cerinta, foloseste_cache=foloseste_cache
                )

with tab_foto:
    fisier_foto = st.file_uploader("Încarcă o poză cu o schemă din caiet / tablă / FluidSIM",
                                   type=["jpg", "jpeg", "png"])
    indicatii_foto = st.text_input("Detalii opționale:", placeholder="Ex: Desfășoară complet schema din poză")
    if fisier_foto:
        img = Image.open(fisier_foto)
        st.image(img, caption="Poză încărcată", width=380)
        if st.button("🔍 Transformă Poza în Schemă FluidSIM 1:1"):
            if not api_key:
                st.error("Introdu cheia Gemini API în panoul din stânga!")
            else:
                with st.spinner(f"Se recunosc simbolurile ISO 1219 folosind {model_ales}..."):
                    st.session_state["schema_activa"] = genereaza_proiect_fluidsim(
                        api_key, model_ales, cerinta_text=indicatii_foto, imagine=img, foloseste_cache=foloseste_cache
                    )

# =====================================================================
# 7. DISPLAY SIMULATOR, FLUIDSIM BUILD GUIDE & 1:1 DXF EXPORT
# =====================================================================
schema = st.session_state.get("schema_activa")

if schema:
    st.subheader(f"📐 {schema['titlu']}")
    st.info(f"**Funcționare & Logică ISO 1219:** {schema['explicatie_centru_si_ansamblu']}")

    html_canvas = render_fluidsim_canvas(schema)
    st.components.v1.html(html_canvas, height=645, scrolling=False)

    col_dxf, col_json = st.columns(2)
    with col_dxf:
        dxf_bytes = export_exact_dxf(schema)
        st.download_button(
            "📥 DESCARCĂ .DXF IDENTIC 1:1 PENTRU FESTO FLUIDSIM-P (File -> Import -> DXF)",
            data=dxf_bytes,
            file_name="schema_iso1219_fluidsim.dxf",
            mime="application/dxf",
            use_container_width=True
        )
    with col_json:
        st.download_button(
            "📥 Descarcă Specificația Tehnică (.JSON)",
            data=json.dumps(schema, indent=2, ensure_ascii=False),
            file_name="specificatie_fluidsim.json",
            mime="application/json",
            use_container_width=True
        )

    st.markdown("### 💻 Cum construiești și configurezi schema manual în Festo FluidSIM-P")
    col_g1, col_g2 = st.columns([3, 2])

    with col_g1:
        for pas in schema.get("ghid_fluidsim", []):
            with st.expander(f"Pasul {pas['pas']}: {pas['element']}", expanded=True):
                st.markdown(f"**Configurare în FluidSIM-P:** {pas['cum_se_pune_in_fluidsim']}")

    with col_g2:
        st.markdown("#### 🔗 Tabel de Conexiuni Pin-la-Pin (Traseu Conducte)")
        tabel_con = [
            {
                "De la (Pin)": c["de_la"],
                "Până la (Pin)": c["pana_la"],
                "Regim Conductă": c["circuit_activ_in_starea"]
            }
            for c in schema["conexiuni"]
        ]
        st.table(tabel_con)