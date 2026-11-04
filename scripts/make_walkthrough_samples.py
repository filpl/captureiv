"""Generate synthetic sample files for trying the site walkthrough viewer locally.

Creates, using only the Python standard library:
  * two floor-plan PDFs (vector drawings with rooms, grid lines and a title block),
  * equirectangular "360°" panoramas (PNG) for two capture dates, showing progress,
  * a GLB 3D model of a simple two-storey building shell.

These are test fixtures, not real site data. Usage:
    python scripts/make_walkthrough_samples.py OUTPUT_DIR
"""
from __future__ import annotations

import json
import math
import struct
import sys
import zlib
from pathlib import Path


# ------------------------------------------------------------------ PDF
def floor_plan_pdf(title: str, sheet: str, rooms: list[tuple[float, float, float, float, str]]) -> bytes:
    W, H = 1224, 792  # 17x11 in landscape (points)
    ops = ["1 w 0.55 0.6 0.7 RG"]
    for i in range(9):  # structural grid
        x = 110 + i * 110
        ops.append(f"{x} 70 m {x} 690 l S")
    for j in range(6):
        y = 90 + j * 115
        ops.append(f"90 {y} m 1000 {y} l S")
    ops.append("0 0 0 RG 6 w 110 90 880 575 re S")  # exterior walls
    ops.append("2.5 w")
    for x, y, w, h, _ in rooms:
        ops.append(f"{x} {y} {w} {h} re S")
    ops.append("0.85 0.2 0.2 RG 1.5 w")
    for x in (350, 640):  # stair/core hatch
        for k in range(8):
            ops.append(f"{x + k * 10} 560 m {x + k * 10} 650 l S")
    ops.append("0 0 0 RG 1 w 1030 70 160 650 re S 1030 160 160 0 re S")
    text = [
        ("BT /F1 22 Tf 1045 680 Td (CAPTURE IV) Tj ET"),
        (f"BT /F1 12 Tf 1045 650 Td (SAMPLE PROJECT) Tj ET"),
        (f"BT /F1 11 Tf 1045 620 Td ({title}) Tj ET"),
        (f"BT /F1 30 Tf 1045 100 Td ({sheet}) Tj ET"),
        ("BT /F1 9 Tf 1045 140 Td (SYNTHETIC TEST DRAWING) Tj ET"),
    ]
    for x, y, w, h, label in rooms:
        text.append(f"BT /F1 11 Tf {x + 8} {y + h - 18} Td ({label}) Tj ET")
    for i in range(9):
        text.append(f"BT /F1 10 Tf {106 + i * 110} 52 Td ({chr(65 + i)}) Tj ET")
    content = ("\n".join(ops + text)).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Contents 4 0 R "
        f"/Resources << /Font << /F1 5 0 R >> >> >>".encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# ------------------------------------------------------------------ PNG
def png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def panorama(seed: int, progress: float, width: int = 2048, height: int = 1024) -> bytes:
    """An equirectangular interior: floor, ceiling, columns, walls that fill in with progress."""
    columns = [(seed * 0.7 + k * math.tau / 6) % math.tau for k in range(6)]
    compass = {0: (220, 38, 38), 1: (37, 99, 235), 2: (22, 163, 74), 3: (234, 179, 8)}
    rows = []
    for j in range(height):
        lat = (0.5 - (j + 0.5) / height) * math.pi
        row = bytearray()
        for i in range(width):
            lon = (i + 0.5) / width * math.tau
            if lat < -0.12:  # concrete floor with a slab grid
                d = 1.5 / max(0.05, math.tan(-lat))
                fx, fz = d * math.cos(lon), d * math.sin(lon)
                line = (abs(fx % 2 - 1) > 0.95) or (abs(fz % 2 - 1) > 0.95)
                shade = 120 + int(30 * math.sin(fx * 0.3 + seed))
                c = (70, 70, 74) if line else (shade, shade - 4, shade - 10)
            elif lat > 0.35:  # deck above, with joists
                joist = int((lon * 12) % 2 < 0.25)
                c = (95, 85, 70) if joist else (150, 140, 120)
            else:
                band = int(lon / (math.tau / 4)) % 4
                wall_done = ((lon / math.tau) % 1.0) < progress
                base = (196, 178, 150) if wall_done else (60, 64, 72)
                if not wall_done and int(lon * 40) % 3 == 0:
                    base = (176, 140, 90)  # exposed studs
                c = base
                if abs(lat - 0.05) < 0.012:
                    c = compass[band]
                for col in columns:
                    if abs(((lon - col + math.pi) % math.tau) - math.pi) < 0.035:
                        c = (110, 112, 118)
                if wall_done and 0.0 < lat < 0.22 and int(lon * 6 + seed) % 5 == 0 and (lon * 6) % 1 < 0.6:
                    c = (150, 200, 240)  # window
            row += bytes(c)
        rows.append(bytes(row))
    return png(width, height, rows)


# ------------------------------------------------------------------ GLB
def box(x0, y0, z0, x1, y1, z1):
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 1, 2, 3, (0, 0, -1)), (5, 4, 7, 6, (0, 0, 1)), (4, 0, 3, 7, (-1, 0, 0)),
             (1, 5, 6, 2, (1, 0, 0)), (3, 2, 6, 7, (0, 1, 0)), (4, 5, 1, 0, (0, -1, 0))]
    pos, nor, idx = [], [], []
    for a, b, c, d, n in faces:
        base = len(pos)
        for k in (a, b, c, d):
            pos.append(v[k]); nor.append(n)
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    return pos, nor, idx


def building_glb() -> bytes:
    parts = {"slab": [], "wall": [], "column": []}
    W, D, storey = 32.0, 21.0, 3.6
    for level in range(2):
        y = level * storey
        parts["slab"].append(box(0, y - 0.25, 0, W, y, D))
        for x0, z0, x1, z1 in ((0, 0, W, 0.25), (0, D - 0.25, W, D), (0, 0, 0.25, D), (W - 0.25, 0, W, D)):
            parts["wall"].append(box(x0, y, z0, x1, y + (storey if level == 0 else 1.1), z1))
        for cx in range(1, 8):
            for cz in range(1, 5):
                parts["column"].append(box(cx * 4 - 0.2, y, cz * 4.2 - 0.2, cx * 4 + 0.2, y + storey - 0.25, cz * 4.2 + 0.2))
        parts["wall"].append(box(8, y, 6, 8.2, y + 2.8, 15)); parts["wall"].append(box(20, y, 6, 20.2, y + 2.8, 15))
    colors = {"slab": [0.62, 0.62, 0.6, 1], "wall": [0.78, 0.7, 0.58, 1], "column": [0.45, 0.47, 0.52, 1]}
    binary, views, accessors, meshes, nodes, materials = bytearray(), [], [], [], [], []

    def add_view(data: bytes, target):
        while len(binary) % 4:
            binary.append(0)
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(data), "target": target})
        binary.extend(data)
        return len(views) - 1

    for name, boxes in parts.items():
        pos, nor, idx = [], [], []
        for p, n, i in boxes:
            offset = len(pos); pos += p; nor += n; idx += [k + offset for k in i]
        pv = add_view(struct.pack(f"<{len(pos) * 3}f", *[c for v in pos for c in v]), 34962)
        nv = add_view(struct.pack(f"<{len(nor) * 3}f", *[c for v in nor for c in v]), 34962)
        iv = add_view(struct.pack(f"<{len(idx)}I", *idx), 34963)
        mins = [min(v[k] for v in pos) for k in range(3)]; maxs = [max(v[k] for v in pos) for k in range(3)]
        accessors += [{"bufferView": pv, "componentType": 5126, "count": len(pos), "type": "VEC3", "min": mins, "max": maxs},
                      {"bufferView": nv, "componentType": 5126, "count": len(nor), "type": "VEC3"},
                      {"bufferView": iv, "componentType": 5125, "count": len(idx), "type": "SCALAR"}]
        a = len(accessors) - 3
        materials.append({"name": name, "pbrMetallicRoughness": {"baseColorFactor": colors[name], "metallicFactor": 0, "roughnessFactor": 0.9}})
        meshes.append({"name": name, "primitives": [{"attributes": {"POSITION": a, "NORMAL": a + 1}, "indices": a + 2, "material": len(materials) - 1}]})
        nodes.append({"name": name, "mesh": len(meshes) - 1})
    while len(binary) % 4:
        binary.append(0)
    gltf = {"asset": {"version": "2.0", "generator": "Capture IV sample generator"}, "scene": 0,
            "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes, "materials": materials,
            "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(binary)}]}
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
            + struct.pack("<I4s", len(binary), b"BIN\x00") + bytes(binary))


def main(out_dir: str) -> None:
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    level1 = [(110, 90, 240, 200, "LOBBY 101"), (350, 90, 290, 200, "OPEN OFFICE 102"), (640, 90, 350, 200, "TENANT 103"),
              (110, 290, 240, 270, "UNIT 104"), (640, 290, 350, 270, "UNIT 105"), (350, 290, 290, 120, "CORRIDOR")]
    level2 = [(110, 90, 300, 230, "UNIT 201"), (410, 90, 290, 230, "UNIT 202"), (700, 90, 290, 230, "UNIT 203"),
              (110, 320, 300, 240, "UNIT 204"), (700, 320, 290, 240, "UNIT 205"), (410, 320, 290, 90, "CORRIDOR")]
    (out / "A-101 Level 1 plan.pdf").write_bytes(floor_plan_pdf("LEVEL 1 FLOOR PLAN", "A-101", level1))
    (out / "A-102 Level 2 plan.pdf").write_bytes(floor_plan_pdf("LEVEL 2 FLOOR PLAN", "A-102", level2))
    for k in range(4):
        (out / f"pano-early-{k + 1}.png").write_bytes(panorama(k, 0.25 + 0.05 * k))
        (out / f"pano-late-{k + 1}.png").write_bytes(panorama(k, 0.8 + 0.05 * k))
        print("panorama", k + 1, "of 4")
    (out / "sample-building.glb").write_bytes(building_glb())
    print("Sample files written to", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "walkthrough-samples")
