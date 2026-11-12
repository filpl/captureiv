"""Convert laser-scan point clouds (LAS, LAZ, E57) into a compact web format.

The output ("CVPC") streams to the browser viewer:

    b"CVPC" | uint16 version | uint16 reserved | uint32 header_length | header JSON (UTF-8)
    then `count` records of 10 bytes: x, y, z as uint16 (quantized to the header's
    bounding box), red, green, blue as uint8, classification as uint8.

Points are a uniform random sample (at most `max_points`) written in random order,
so the first bytes to arrive already show the whole site at low density and the
picture fills in as the rest streams. Source files are read in chunks so even very
large LAS/LAZ files convert with modest memory. A top-down PNG thumbnail is made too.

When the scan records its coordinate system (LAS/LAZ with a CRS), a transparent
top-down map overlay is also rendered in Web Mercator, so it lines up exactly under
flight paths on the project map; its corners are stored as WGS84 lat/lng bounds.
"""
from __future__ import annotations

import json
import struct

import numpy as np

MAGIC = b"CVPC"
VERSION = 1
RECORD_BYTES = 10
DEFAULT_MAX_POINTS = 3_000_000
CHUNK_POINTS = 400_000
CHUNK_BYTES = 32 * 1024 * 1024
# Standard LAS records are at most 67 bytes; anything far larger is padding (or a decompression bomb).
MAX_POINT_RECORD_BYTES = 160
# E57 scans are read one whole scan at a time (~40 bytes per point); refuse anything that wouldn't fit.
MAX_E57_SCAN_POINTS = 5_000_000
# Hard memory ceiling for the converter process (MB of address space); a bad file fails the conversion
# instead of exhausting the server. Override with SCAN_CONVERT_MEMORY_MB.
DEFAULT_MEMORY_MB = 1400
THUMB_SIZE = (960, 540)
OVERLAY_MAX_SIDE = 2048


class ScanError(ValueError):
    """The file can't be converted (bad, empty or unsupported); the message is user-facing."""


def _units_from_las(header):
    """Best effort: 'ft' when the CRS records say feet, otherwise metres."""
    try:
        for vlr in list(header.vlrs) + list(getattr(header, "evlrs", []) or []):
            raw = getattr(vlr, "record_data", b"") or b""
            text = (raw if isinstance(raw, bytes) else str(raw).encode()).lower()
            text += str(getattr(vlr, "string", "") or "").lower().encode()
            if b"foot" in text or b"feet" in text or b"ftus" in text:
                return "ft"
    except Exception:
        pass
    return "m"


def _sample_las(path, max_points, rng):
    import laspy
    from pathlib import Path
    path = Path(path)
    try:
        reader = laspy.open(str(path))
    except Exception as exc:
        raise ScanError("This LAS/LAZ file couldn't be opened. Export it again and re-upload.") from exc
    with reader as f:
        total = int(f.header.point_count)
        if total <= 0:
            raise ScanError("This scan has no points.")
        record = int(f.header.point_format.size)
        if record > MAX_POINT_RECORD_BYTES:
            raise ScanError("This scan stores unusually large point records. Export it again as standard LAS or LAZ.")
        if not f.header.are_points_compressed:
            available = max(0, path.stat().st_size - int(f.header.offset_to_point_data)) if hasattr(path, "stat") else None
            if available is not None and total * record > available:
                raise ScanError("This LAS file is incomplete. Export it again and re-upload.")
        chunk_points = max(1, min(CHUNK_POINTS, CHUNK_BYTES // max(record, 1)))
        dims = set(f.header.point_format.dimension_names)
        has_rgb = {"red", "green", "blue"} <= dims
        keep = min(1.0, max_points / total)
        origin = np.array(f.header.mins, dtype=np.float64)
        xs, ys, zs, cs, cls, intens = [], [], [], [], [], []
        for chunk in f.chunk_iterator(chunk_points):
            n = len(chunk)
            mask = rng.random(n) < keep if keep < 1.0 else np.ones(n, bool)
            if not mask.any():
                continue
            xs.append((np.asarray(chunk.x)[mask] - origin[0]).astype(np.float32))
            ys.append((np.asarray(chunk.y)[mask] - origin[1]).astype(np.float32))
            zs.append((np.asarray(chunk.z)[mask] - origin[2]).astype(np.float32))
            cls.append(np.asarray(chunk.classification)[mask].astype(np.uint8))
            if has_rgb:
                cs.append(np.stack([np.asarray(chunk.red)[mask], np.asarray(chunk.green)[mask],
                                    np.asarray(chunk.blue)[mask]], axis=1).astype(np.uint16))
            else:
                intens.append(np.asarray(chunk.intensity)[mask].astype(np.uint16))
        units = _units_from_las(f.header)
        try:
            crs = f.header.parse_crs()
            crs_wkt = crs.to_wkt() if crs is not None else ""
        except Exception:
            crs_wkt = ""
    if not xs:
        raise ScanError("This scan has no points.")
    xyz = np.stack([np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)], axis=1)
    rgb = _to_8bit(np.concatenate(cs)) if has_rgb else _gray(np.concatenate(intens))
    return xyz, rgb, np.concatenate(cls), origin, total, units, has_rgb, crs_wkt


def _sample_e57(path, max_points, rng):
    try:
        import pye57
    except ImportError as exc:
        raise ScanError("E57 support isn't installed on this server. Export LAS or LAZ instead.") from exc
    try:
        e57 = pye57.E57(str(path))
    except Exception as exc:
        raise ScanError("This E57 file couldn't be opened. Export it again and re-upload.") from exc
    counts = []
    for index in range(e57.scan_count):
        header = e57.get_header(index)
        counts.append(int(header.point_count))
    total = sum(counts)
    if total <= 0:
        raise ScanError("This scan has no points.")
    if max(counts) > MAX_E57_SCAN_POINTS:
        raise ScanError("One setup in this E57 has more than 5 million points, too many to convert here. "
                        "Export the scan as LAZ instead.")
    keep = min(1.0, max_points / total)
    xs, ys, zs, cs, has_rgb = [], [], [], [], True
    for index in range(e57.scan_count):
        data = e57.read_scan(index, colors=True, intensity=True, ignore_missing_fields=True, transform=True)
        x = np.asarray(data["cartesianX"], dtype=np.float64)
        mask = rng.random(len(x)) < keep if keep < 1.0 else np.ones(len(x), bool)
        xs.append(x[mask])
        ys.append(np.asarray(data["cartesianY"], dtype=np.float64)[mask])
        zs.append(np.asarray(data["cartesianZ"], dtype=np.float64)[mask])
        if "colorRed" in data:
            cs.append(np.stack([np.asarray(data["colorRed"])[mask], np.asarray(data["colorGreen"])[mask],
                                np.asarray(data["colorBlue"])[mask]], axis=1).astype(np.uint16))
        elif "intensity" in data:
            has_rgb = False
            cs.append(np.repeat(_gray(np.asarray(data["intensity"])[mask])[:, :1], 3, axis=1).astype(np.uint16))
        else:
            has_rgb = False
            cs.append(np.full((int(mask.sum()), 3), 200, dtype=np.uint16))
        del data
    x, y, z = np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)
    origin = np.array([x.min(), y.min(), z.min()])
    xyz = np.stack([x - origin[0], y - origin[1], z - origin[2]], axis=1).astype(np.float32)
    rgb = _to_8bit(np.concatenate(cs))
    # E57 scans are in the scanner's local frame; they open in 3D but can't be placed on the map.
    return xyz, rgb, np.zeros(len(xyz), np.uint8), origin, total, "m", has_rgb, ""


def _to_8bit(rgb):
    rgb = np.asarray(rgb)
    if rgb.size and rgb.max() > 255:
        rgb = rgb >> 8
    return np.clip(rgb, 0, 255).astype(np.uint8)


def _gray(values):
    values = np.asarray(values, dtype=np.float32)
    if values.size == 0:
        return np.zeros((0, 3), np.uint8)
    low, high = np.percentile(values, [2, 98])
    scaled = np.clip((values - low) / max(high - low, 1e-6), 0, 1) * 220 + 25
    return np.repeat(scaled.astype(np.uint8)[:, None], 3, axis=1)


def convert(src_path, ext, out_points, out_thumb, out_overlay=None, max_points=DEFAULT_MAX_POINTS, seed=7):
    """Convert one scan. Returns metadata for the database (point counts, bounds, units,
    and "geo" with the map overlay's lat/lng bounds when the scan is georeferenced)."""
    rng = np.random.default_rng(seed)
    ext = (ext or "").lower()
    if ext in {"las", "laz"}:
        xyz, rgb, cls, origin, total, units, has_rgb, crs_wkt = _sample_las(src_path, max_points, rng)
    elif ext == "e57":
        xyz, rgb, cls, origin, total, units, has_rgb, crs_wkt = _sample_e57(src_path, max_points, rng)
    else:
        raise ScanError("Upload a LAS, LAZ or E57 point cloud.")
    finite = np.isfinite(xyz).all(axis=1)
    xyz, rgb, cls = xyz[finite], rgb[finite], cls[finite]
    if len(xyz) == 0:
        raise ScanError("This scan has no usable points.")
    # Drop extreme outliers (stray returns, birds) so they don't squash the quantization grid.
    probe = xyz[rng.choice(len(xyz), size=min(len(xyz), 200_000), replace=False)]
    low = np.percentile(probe, 0.05, axis=0)
    high = np.percentile(probe, 99.95, axis=0)
    del probe
    inside = ((xyz >= low) & (xyz <= high)).all(axis=1)
    if inside.sum() >= 0.98 * len(xyz):
        xyz, rgb, cls = xyz[inside], rgb[inside], cls[inside]
    order = rng.permutation(len(xyz))
    xyz, rgb, cls = xyz[order], rgb[order], cls[order]
    del order
    mins = xyz.min(axis=0).astype(np.float64)
    maxs = xyz.max(axis=0).astype(np.float64)
    span = np.maximum(maxs - mins, 1e-6)
    xyz -= mins.astype(np.float32)  # in place: relative to the cloud's minimum corner from here on
    quant = np.round(xyz * (65535 / span).astype(np.float32)).astype(np.uint16)
    records = np.zeros(len(xyz), dtype=[("x", "<u2"), ("y", "<u2"), ("z", "<u2"),
                                        ("r", "u1"), ("g", "u1"), ("b", "u1"), ("c", "u1")])
    records["x"], records["y"], records["z"] = quant[:, 0], quant[:, 1], quant[:, 2]
    records["r"], records["g"], records["b"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    records["c"] = cls
    del quant
    world_min = (origin + mins).tolist()
    world_max = (origin + maxs).tolist()
    meta = {"count": int(len(xyz)), "source_points": int(total), "min": [0.0, 0.0, 0.0],
            "max": [float(v) for v in span], "origin": world_min, "world_max": world_max,
            "units": units, "has_rgb": bool(has_rgb), "record_bytes": RECORD_BYTES,
            "classes": sorted(int(v) for v in np.unique(cls)[:64])}
    header = json.dumps(meta, separators=(",", ":")).encode()
    # The CRS stays out of the streamed header (it's only needed server-side).
    geo = None
    if crs_wkt and out_overlay:
        geo = _overlay(xyz, rgb, np.asarray(origin, dtype=np.float64) + mins, crs_wkt, out_overlay)
    if geo:
        meta["geo"] = geo
        meta["crs_wkt"] = crs_wkt
    with open(out_points, "wb") as out:
        out.write(MAGIC + struct.pack("<HHI", VERSION, 0, len(header)) + header)
        out.write(records.tobytes())
    _thumbnail(xyz, span, rgb, out_thumb)
    return meta


def _thumbnail(xyz, span, rgb, out_thumb):
    """Top-down picture of the site: higher points drawn over lower ones."""
    from PIL import Image
    width, height = THUMB_SIZE
    scale = min((width - 40) / span[0], (height - 40) / span[1])
    px = (xyz[:, 0] * scale + (width - span[0] * scale) / 2).astype(np.int32)
    py = (height - 1 - (xyz[:, 1] * scale + (height - span[1] * scale) / 2)).astype(np.int32)
    ok = (px >= 0) & (px < width) & (py >= 0) & (py < height)
    order = np.argsort(xyz[ok, 2], kind="stable")
    image = np.zeros((height, width, 3), np.uint8)
    filled = np.zeros((height, width), bool)
    px, py, colors = px[ok][order], py[ok][order], rgb[ok][order]
    image[py, px] = colors
    filled[py, px] = True
    # Fill one-pixel gaps between sparse points.
    for dy, dx in ((0, 1), (1, 0), (1, 1), (0, -1), (-1, 0)):
        shifted = np.roll(np.roll(image, dy, axis=0), dx, axis=1)
        shifted_fill = np.roll(np.roll(filled, dy, axis=0), dx, axis=1)
        gap = ~filled & shifted_fill
        image[gap] = shifted[gap]
        filled |= gap
    background = np.array([238, 242, 248], np.uint8)
    image[~filled] = background
    Image.fromarray(image).save(out_thumb, format="PNG", optimize=True)


def _overlay(xyz, rgb, base, crs_wkt, out_overlay):
    """Render a transparent top-down image in Web Mercator. Returns {"bounds": [[s, w], [n, e]]}
    (WGS84) or None when the coordinate system can't be transformed."""
    from PIL import Image
    try:
        from pyproj import CRS, Transformer
        crs = CRS.from_wkt(crs_wkt)
        to_merc = Transformer.from_crs(crs, "EPSG:3857", always_xy=True)
        to_wgs = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        mx, my = to_merc.transform(xyz[:, 0].astype(np.float64) + base[0],
                                   xyz[:, 1].astype(np.float64) + base[1])
    except Exception:
        return None
    mx, my = np.asarray(mx), np.asarray(my)
    ok = np.isfinite(mx) & np.isfinite(my)
    if ok.sum() < 3:
        return None
    mx, my, z, colors = mx[ok], my[ok], xyz[ok, 2], rgb[ok]
    x0, x1, y0, y1 = mx.min(), mx.max(), my.min(), my.max()
    wx, wy = max(x1 - x0, 1e-3), max(y1 - y0, 1e-3)
    # Aim for roughly one sample per pixel so the overlay is solid, capped for file size.
    side = int(min(OVERLAY_MAX_SIDE, max(256, np.sqrt(len(mx) * 0.7 * max(wx, wy) / min(wx, wy)))))
    scale = side / max(wx, wy)
    width, height = max(1, int(np.ceil(wx * scale))), max(1, int(np.ceil(wy * scale)))
    px = np.clip(((mx - x0) * scale).astype(np.int32), 0, width - 1)
    py = np.clip(((y1 - my) * scale).astype(np.int32), 0, height - 1)
    order = np.argsort(z, kind="stable")
    image = np.zeros((height, width, 4), np.uint8)
    filled = np.zeros((height, width), bool)
    image[py[order], px[order], :3] = colors[order]
    filled[py[order], px[order]] = True
    for _ in range(2):
        for dy, dx in ((0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (-1, -1)):
            shifted = np.roll(np.roll(image, dy, axis=0), dx, axis=1)
            shifted_fill = np.roll(np.roll(filled, dy, axis=0), dx, axis=1)
            gap = ~filled & shifted_fill
            image[gap] = shifted[gap]
            filled |= gap
    image[filled, 3] = 255
    Image.fromarray(image, "RGBA").save(out_overlay, format="PNG", optimize=True)
    west, south = to_wgs.transform(x0, y1 - height / scale)
    east, north = to_wgs.transform(x0 + width / scale, y1)
    if not all(np.isfinite(v) for v in (west, south, east, north)):
        return None
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        return None
    return {"bounds": [[round(float(south), 8), round(float(west), 8)], [round(float(north), 8), round(float(east), 8)]],
            "size": [width, height]}


def local_points_for_latlng(meta, points):
    """Map [[lat, lng], ...] into the scan's local viewer coordinates (x, y relative to
    the converted cloud's minimum corner). Returns None when the scan isn't georeferenced."""
    crs_wkt = (meta or {}).get("crs_wkt")
    origin = (meta or {}).get("origin")
    if not crs_wkt or not origin:
        return None
    from pyproj import CRS, Transformer
    transformer = Transformer.from_crs("EPSG:4326", CRS.from_wkt(crs_wkt), always_xy=True)
    out = []
    for lat, lng in points:
        x, y = transformer.transform(lng, lat)
        out.append([round(float(x) - origin[0], 3), round(float(y) - origin[1], 3)])
    return out


def read_header(path):
    """Read a CVPC file's header (used by tests and integrity checks)."""
    with open(path, "rb") as handle:
        if handle.read(4) != MAGIC:
            raise ScanError("Not a converted scan file")
        _version, _reserved, length = struct.unpack("<HHI", handle.read(8))
        return json.loads(handle.read(length))


def main(argv=None):
    """Command line used by the platform: run each conversion in its own process so
    memory is returned to the system afterwards and a failure can't take the web
    worker down. Prints the metadata JSON (or {"error": ...}) on stdout."""
    import os
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    limit_mb = int(os.environ.get("SCAN_CONVERT_MEMORY_MB", DEFAULT_MEMORY_MB) or 0)
    if limit_mb > 0:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (limit_mb * 1024 * 1024, limit_mb * 1024 * 1024))
        except (ImportError, ValueError, OSError):
            pass
    if len(args) < 4:
        print(json.dumps({"error": "usage: scan_convert SRC EXT OUT_POINTS OUT_THUMB [OUT_OVERLAY] [MAX_POINTS]"}))
        return 2
    overlay = args[4] if len(args) > 4 and args[4] not in {"", "-"} else None
    max_points = int(args[5]) if len(args) > 5 else DEFAULT_MAX_POINTS
    try:
        meta = convert(args[0], args[1], args[2], args[3], out_overlay=overlay, max_points=max_points)
    except ScanError as exc:
        print(json.dumps({"error": str(exc)}))
        return 3
    except MemoryError:
        print(json.dumps({"error": "This scan is too large to convert here. Export a smaller area or LAZ."}))
        return 4
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
