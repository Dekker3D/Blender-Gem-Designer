"""GemCAD (.asc and .gem) file importers.

Parses GemCAD design files and creates gem-designer objects with
geometry-node tiers, reusing the addon's native modifier pipeline.

Format reference: https://github.com/mbparker/gemcad-file-reader

=== ASC (ASCII / text) format ===
Starts with "GemCad 5.0"; uses single-letter directives (g, y, I, H, F, a).
Tier lines ("a") carry angle, distance, and an index list — maps directly
to the gem-designer tier model.

=== GEM (binary) format ===
Little-endian binary. Tier data stores raw facet normals + polygon vertices;
the trailer section holds gear, symmetry, RI, and header/footnote text.
Tier angles and tooth indices must be computed from the normals.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

import bpy
from bpy.types import Context


# ============================================================================
# Shared helpers
# ============================================================================

def _parse_float_safe(value: str) -> float:
    """Parse a string to float, returning 0.0 on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ============================================================================
# ASC format parser
# ============================================================================

def load_asc(filepath: str) -> dict[str, Any]:
    """Parse a GemCAD .asc file and return structured data.

    Returns a dict with:
        info: {title, headers: [str], footnotes: [str]}
        render: {refractive_index}
        index: {gear, gear_location_angle, symmetry_folds, symmetry_mirror}
        tiers: [{name, angle, distance, cutting_instructions,
                 facets: [{index, name}]}]
    """
    with open(filepath, "r", encoding="ascii", errors="replace") as fh:
        lines = [line.strip() for line in fh if line.strip()]

    if not lines or not lines[0].startswith("GemCad "):
        raise ValueError(f"Not a valid GemCAD ASC file: {filepath}")

    data: dict[str, Any] = {
        "info": {"title": "", "headers": [], "footnotes": []},
        "render": {"refractive_index": 1.54},
        "index": {
            "gear": 96,
            "gear_location_angle": 0.0,
            "symmetry_folds": 1,
            "symmetry_mirror": False,
        },
        "tiers": [],
    }

    in_footnotes = False

    for line in lines[1:]:  # skip "GemCad 5.0" header
        if not line:
            continue

        parts = line.split()
        if not parts:
            continue

        directive = parts[0]

        if directive == "g" and len(parts) >= 3:
            data["index"]["gear"] = abs(int(parts[1]))
            data["index"]["gear_location_angle"] = _parse_float_safe(parts[2])

        elif directive == "y" and len(parts) >= 3:
            data["index"]["symmetry_folds"] = int(parts[1])
            data["index"]["symmetry_mirror"] = parts[2].lower() == "y"

        elif directive == "I" and len(parts) >= 2:
            data["render"]["refractive_index"] = _parse_float_safe(parts[1])

        elif directive == "H" and len(parts) >= 2:
            header_text = " ".join(parts[1:])
            data["info"]["headers"].append(header_text)
            if not data["info"]["title"]:
                data["info"]["title"] = header_text

        elif directive == "F" and len(parts) >= 2:
            footnote_text = " ".join(parts[1:])
            data["info"]["footnotes"].append(footnote_text)
            in_footnotes = True

        elif directive == "a" and len(parts) >= 4:
            # a <angle> <distance> <index1> [<index2> ...] [n <name> <index> ...] [<instructions>]
            angle = _parse_float_safe(parts[1])
            distance = _parse_float_safe(parts[2])

            facets: list[dict[str, Any]] = []
            cutting_instructions = ""
            current_index: float | None = None
            i = 3

            while i < len(parts):
                token = parts[i]

                if token == "n" and i + 1 < len(parts):
                    # Named facet: the name applies to the PREVIOUSLY read index
                    # (matches C# GemCadAscImport.ProcessLine behavior).
                    # e.g. "66 n G1 74" → named "G1" at index 66, then unnamed 74.
                    if current_index is not None:
                        facets.append({"index": current_index, "name": parts[i + 1]})
                        current_index = None
                    i += 2
                else:
                    try:
                        idx = float(token)
                        if current_index is not None:
                            facets.append({"index": current_index, "name": ""})
                        current_index = idx
                        i += 1
                    except ValueError:
                        # Non-numeric, non-"n" → cutting instructions
                        if current_index is not None:
                            facets.append({"index": current_index, "name": ""})
                            current_index = None
                        cutting_instructions = " ".join(parts[i:])
                        break

            # Flush any remaining pending index
            if current_index is not None:
                facets.append({"index": current_index, "name": ""})

            data["tiers"].append({
                "angle": angle,
                "distance": distance,
                "cutting_instructions": cutting_instructions,
                "facets": facets,
            })

    return data


# ============================================================================
# GEM binary format parser
# ============================================================================

def _read_double(buf: bytes, offset: int) -> tuple[float, int]:
    """Read a little-endian double from buffer at offset.  Returns (value, next_offset)."""
    return struct.unpack_from("<d", buf, offset)[0], offset + 8


def _read_int32(buf: bytes, offset: int) -> tuple[int, int]:
    """Read a little-endian int32 from buffer at offset.  Returns (value, next_offset)."""
    return struct.unpack_from("<i", buf, offset)[0], offset + 4


def _read_3d_point(buf: bytes, offset: int) -> tuple[tuple[float, float, float], int, int]:
    """Read 3 doubles (XYZ) + 1 int32 EOD marker.  Returns ((x,y,z), eod_marker, next_offset)."""
    x, offset = _read_double(buf, offset)
    y, offset = _read_double(buf, offset)
    z, offset = _read_double(buf, offset)
    eod, offset = _read_int32(buf, offset)
    return (x, y, z), eod, offset


def _read_ansi_string(buf: bytes, offset: int) -> tuple[str, int]:
    """Read a 1-byte length-prefixed ANSI string.  Returns (string, next_offset)."""
    if offset >= len(buf):
        return "", offset
    length = buf[offset]
    offset += 1
    if length == 0:
        return "", offset
    end = offset + length
    if end > len(buf):
        end = len(buf)
    raw = buf[offset:end]
    return raw.decode("latin-1"), end


def _detect_trailer(buf: bytes, offset: int) -> tuple[bool, int, int, bool]:
    """Try to read a trailer record at offset.

    Detection heuristic (from the C# code):
      int32==0, next 4 bytes not all zero, symmetry_folds>0, symmetry_mirror∈{0,1}

    Returns (is_trailer, symmetry_folds, symmetry_mirror_int, is_mirror).
    """
    if offset + 24 > len(buf):
        return False, 0, 0, False

    marker, off = _read_int32(buf, offset)
    if marker != 0:
        return False, 0, 0, False

    unknown2 = buf[off : off + 4]
    if sum(unknown2) == 0:
        return False, 0, 0, False
    off += 4

    sym_folds, off = _read_int32(buf, off)
    sym_mirror_int, off = _read_int32(buf, off)

    if sym_folds > 0 and sym_mirror_int in (0, 1):
        return True, sym_folds, sym_mirror_int, sym_mirror_int != 0

    return False, 0, 0, False


def _read_trailer(buf: bytes, offset: int) -> tuple[dict[str, Any], int]:
    """Parse the trailer section of a .gem file.

    Returns (metadata_dict, next_offset).
    metadata_dict has keys: symmetry_folds, symmetry_mirror, gear,
    refractive_index, gear_location_angle, headers, footnotes.
    """
    is_trailer, sym_folds, sym_mirror_int, sym_mirror = _detect_trailer(buf, offset)
    if not is_trailer:
        raise ValueError(f"Not a valid trailer at offset {offset}")

    # Skip past the already-read trailer header fields
    # marker(4) + unknown2(4) + sym_folds(4) + sym_mirror(4) = 16 bytes
    off = offset + 16

    gear, off = _read_int32(buf, off)
    refractive_index, off = _read_double(buf, off)
    off += 4  # skip unknown3 (4 bytes)
    gear_location_angle, off = _read_double(buf, off)

    # Read text lines (headers, then footnotes separated by empty line)
    headers: list[str] = []
    footnotes: list[str] = []
    target = headers

    while off < len(buf) - 3:
        line, new_off = _read_ansi_string(buf, off)
        off = new_off

        if line == "":
            # Empty line: switch to footnotes
            target = footnotes
            continue

        if line.lower() == "preform":
            # Stop at preform marker — we don't need preform data
            break

        target.append(line)

    return {
        "symmetry_folds": sym_folds,
        "symmetry_mirror": sym_mirror,
        "gear": abs(gear),
        "refractive_index": refractive_index,
        "gear_location_angle": gear_location_angle,
        "headers": headers,
        "footnotes": footnotes,
    }, off


def _parse_gem_binary(buf: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the binary data portion of a .gem file.

    Returns (tiers_list, metadata_dict).
    tiers_list: [{number, is_preform, facets: [{normal: (x,y,z), name, vertices: [(x,y,z),...]}]}]
    """
    offset = 0
    buf_len = len(buf)

    tiers: list[dict[str, Any]] = []
    current_tier: dict[str, Any] | None = None
    facets: list[dict[str, Any]] = []
    vertices: list[tuple[float, float, float]] = []
    in_preform = False
    metadata: dict[str, Any] | None = None

    while offset < buf_len - 11:  # need at least 3 doubles + 1 int32 = 28 bytes? No, minimum: int32(4) + 4bytes(4) + int32(4) + int32(4) = 16
        # Check for trailer
        is_trailer, _, _, _ = _detect_trailer(buf, offset)
        if is_trailer:
            metadata, offset = _read_trailer(buf, offset)
            if in_preform:
                # We've already parsed preform tiers; stop
                break
            # After the trailer, remaining data (if any) is preform
            in_preform = True
            continue

        # Parse a tier index record
        normal, eod_marker, offset = _read_3d_point(buf, offset)

        # Read the name/cutting-instructions string
        name_text, offset = _read_ansi_string(buf, offset)
        # Split on tab: first part is the facet name, rest is cutting instructions
        text_parts = name_text.split("\t")
        facet_name = text_parts[0].strip() if text_parts else ""
        cutting_instructions = "\t".join(text_parts[1:]) if len(text_parts) > 1 else ""

        tier_number = eod_marker

        # Read EOD marker after the string (C# ReadAnsiString with checkMarker=true)
        after_str_eod, offset = _read_int32(buf, offset)

        # Read vertices until EOD marker is 0 (last vertex IS included —
        # C# adds it before checking the updated eodMarker in the while condition)
        vertices.clear()
        while True:
            if offset + 28 > buf_len:  # 3 doubles + 1 int32
                break
            pt, eod, offset = _read_3d_point(buf, offset)
            vertices.append(pt)
            if eod <= 0:
                break

        # Group into tiers by tier number
        if current_tier is None or current_tier["number"] != tier_number:
            # Save previous tier
            if current_tier is not None and facets:
                current_tier["facets"] = list(facets)
                tiers.append(current_tier)
                facets.clear()

            current_tier = {
                "number": tier_number,
                "is_preform": in_preform,
                "cutting_instructions": cutting_instructions if not current_tier or current_tier["number"] != tier_number else "",
                "facets": [],
            }
        elif cutting_instructions and not current_tier.get("cutting_instructions"):
            current_tier["cutting_instructions"] = cutting_instructions

        facets.append({
            "normal": normal,
            "name": facet_name,
            "vertices": list(vertices),
        })

    # Save final tier
    if current_tier is not None and facets:
        current_tier["facets"] = list(facets)
        tiers.append(current_tier)

    if metadata is None:
        metadata = {
            "symmetry_folds": 1,
            "symmetry_mirror": False,
            "gear": 96,
            "refractive_index": 1.54,
            "gear_location_angle": 0.0,
            "headers": [],
            "footnotes": [],
        }

    return tiers, metadata


def load_gem(filepath: str) -> dict[str, Any]:
    """Parse a GemCAD .gem binary file and return structured data.

    Returns a dict with:
        info: {title, headers: [str], footnotes: [str]}
        render: {refractive_index}
        index: {gear, gear_location_angle, symmetry_folds, symmetry_mirror}
        tiers: [{number, is_preform, angle, distance,
                 facets: [{index, name, normal: (x,y,z), vertices: [(x,y,z),...]}]}]
    """
    with open(filepath, "rb") as fh:
        buf = fh.read()

    tiers, metadata = _parse_gem_binary(buf)

    # Compute tier angles and facet tooth indices from normals
    gear = metadata["gear"]
    step_angle = 360.0 / max(gear, 1)
    roll_angle_offset = metadata.get("gear_location_angle", 0.0) * step_angle

    for tier in tiers:
        if tier["is_preform"]:
            continue

        # Compute tier angle from the first facet's normal
        if tier["facets"]:
            first = tier["facets"][0]
            nx, ny, nz = first["normal"]

            if abs(nx) < 1e-12 and abs(ny) < 1e-12:
                # Vertical normal — table or culet
                angle = 0.0 if nz > 0 else -90.0
            else:
                # Angle between facet normal and its XY projection, minus 90°.
                # Uses dot-product acos (not atan2) to match the C# code's
                # AngleBetweenConnectedVectors semantics.
                xy_len = math.sqrt(nx * nx + ny * ny)
                norm_len = math.sqrt(nx * nx + ny * ny + nz * nz)
                cos_a = xy_len / norm_len if norm_len > 1e-12 else 0.0
                ang_between = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))
                angle = ang_between - 90.0
                if nz < 0:
                    angle = -abs(angle)
                else:
                    angle = abs(angle)

            tier["angle"] = round(angle, 2)

            # Compute tier distance: intersect ray (origin → facet normal)
            # with the first facet's polygon to find the cutting depth.
            # Matches C# GemCadGemImport.CalculateTierDefinitions.
            verts = first.get("vertices", [])
            if len(verts) >= 3:
                v0, v1, v2 = verts[0], verts[1], verts[2]
                # Triangle plane normal
                e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
                e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
                pn_x = e1[1] * e2[2] - e1[2] * e2[1]
                pn_y = e1[2] * e2[0] - e1[0] * e2[2]
                pn_z = e1[0] * e2[1] - e1[1] * e2[0]
                # Ray: P(t) = t * (nx, ny, nz), origin at (0,0,0)
                # Plane: pn · X = pn · v0
                denom = pn_x * nx + pn_y * ny + pn_z * nz
                if abs(denom) > 1e-12:
                    t = (pn_x * v0[0] + pn_y * v0[1] + pn_z * v0[2]) / denom
                    # Intersection point
                    ix, iy, iz = t * nx, t * ny, t * nz
                    distance = math.sqrt(ix * ix + iy * iy + iz * iz)
                    tier["distance"] = distance

        # Compute tooth indices for each facet
        for facet in tier["facets"]:
            nx, ny, nz = facet["normal"]

            if abs(nx) < 1e-12 and abs(ny) < 1e-12:
                index_angle_deg = float(gear)
            else:
                # getAngle2d: angle in XY plane from +X axis
                xy_angle = math.degrees(math.atan2(ny, nx))
                index_angle_deg = (xy_angle - 90.0 + roll_angle_offset) / step_angle * -1.0
                if index_angle_deg < 0:
                    index_angle_deg += gear

            # ClockN (wrap to [0, gear))
            index_angle_deg = abs(index_angle_deg) % gear
            if abs(index_angle_deg - round(index_angle_deg)) < 1e-6:
                index_angle_deg = round(index_angle_deg)

            facet["index"] = int(round(index_angle_deg)) % gear

    # Convert to the same shape as load_asc output
    return {
        "info": {
            "title": metadata["headers"][0] if metadata["headers"] else Path(filepath).stem,
            "headers": metadata["headers"],
            "footnotes": metadata["footnotes"],
        },
        "render": {"refractive_index": metadata["refractive_index"]},
        "index": {
            "gear": gear,
            "gear_location_angle": metadata["gear_location_angle"],
            "symmetry_folds": metadata["symmetry_folds"],
            "symmetry_mirror": metadata["symmetry_mirror"],
        },
        "tiers": tiers,
    }


# ============================================================================
# Symmetry reconstruction (shared between ASC, GEM, and GCS importers)
#
# These functions compute (rotational_symmetry, mirror_symmetry, base_index)
# from a sorted list of 0-based tooth indices on a given gear.
#
# NOTE: Duplicated from gcs_import.py.  Future refactor: extract to utils/symmetry.py.
# ============================================================================

def _facet_tooth_indices_asc(facets: list[dict[str, Any]], gear: int) -> list[int]:
    """Convert ASC-style facet index values to 0-based tooth indices, sorted."""
    indices = [int(f["index"]) % gear for f in facets]
    indices.sort()
    return indices


def _facet_tooth_indices_gem(facets: list[dict[str, Any]], gear: int) -> list[int]:
    """Convert GEM-style facet index values to 0-based tooth indices, sorted."""
    indices = [f["index"] % gear for f in facets]
    indices.sort()
    return indices


def _step_between(indices: list[int], gear: int, start: int, stride: int) -> int | None:
    """Check if every `stride`-th index (starting at `start`) has a constant step.

    Returns the step size if consistent, None otherwise.
    """
    step: int | None = None
    prev = indices[start]
    for i in range(start + stride, len(indices), stride):
        curr = indices[i]
        d = (curr - prev) % gear
        if step is None:
            step = d
        elif d != step:
            return None
        prev = curr
    # Also check wrap-around from last to first+start
    if step is not None and len(indices) > stride:
        first = indices[start]
        last = indices[start + ((len(indices) - start - 1) // stride) * stride]
        wrap = (first - last) % gear
        if wrap != step:
            return None
    return step


def _detect_single_symmetry(
    indices: list[int], gear: int
) -> tuple[int, int, int] | None:
    """Try to detect ONE symmetry pattern in the sorted tooth indices.

    Returns (rotational_symmetry, mirror_symmetry, base_index) or None.

    Tries in order:
      1. Pure rotational (constant step between consecutive indices)
      2. Mirror + rotational (constant step on even-indexed and odd-indexed subsets)
      3. Two-facet mirror (mirror pair with no rotational pattern)
    """
    n = len(indices)
    if n == 0:
        return None

    # --- 1. Pure rotational: constant step between all consecutive indices ---
    step = _step_between(indices, gear, 0, 1)
    if step is not None:
        return (n, 0, indices[0])

    # --- 2. Mirror + rotational: constant step on alternate indices ---
    if n >= 4 and n % 2 == 0:
        step_even = _step_between(indices, gear, 0, 2)
        step_odd = _step_between(indices, gear, 1, 2)
        if step_even is not None and step_odd is not None and step_even == step_odd:
            gap = (indices[1] - indices[0]) % gear
            short_gap = min(gap, gear - gap)
            mirror = short_gap // 2
            if gap <= gear // 2:
                center = (indices[0] + mirror) % gear
            else:
                center = (indices[1] + mirror) % gear
            rot = n // 2
            return (rot, mirror, center)

    # --- 3. Two facets only: mirror symmetry ---
    if n == 2:
        gap = (indices[1] - indices[0]) % gear
        short_gap = min(gap, gear - gap)
        mirror = short_gap // 2
        if gap <= gear // 2:
            center = (indices[0] + mirror) % gear
        else:
            center = (indices[1] + mirror) % gear
        return (1, mirror, center)

    return None


def _decompose_merged_tier(
    indices: list[int], gear: int
) -> list[tuple[int, int, int]]:
    """Decompose a merged/list-of-indices tier into separate symmetry groups.

    Tries to find groups whose indices form valid rotational patterns
    (step = gear / group_size).  Greedy: picks the largest valid group
    from each remaining start index.

    Returns a list of (rotational_symmetry, mirror_symmetry, base_index).
    """
    idx_set = set(indices)
    found: list[tuple[int, int, int]] = []
    used: set[int] = set()

    for start_idx in indices:
        if start_idx in used:
            continue

        best_n = 1
        remaining = [i for i in indices if i not in used]
        for n in range(len(remaining), 1, -1):
            if gear % n != 0:
                continue
            step = gear // n
            ok = True
            pos = start_idx
            for _ in range(n):
                if pos not in idx_set:
                    ok = False
                    break
                pos = (pos + step) % gear
            if ok:
                best_n = n
                break

        if best_n > 1:
            step = gear // best_n
            pos = start_idx
            for _ in range(best_n):
                used.add(pos)
                pos = (pos + step) % gear
            found.append((best_n, 0, start_idx))
        else:
            used.add(start_idx)
            found.append((1, 0, start_idx))

    return found


def _reconstruct_symmetry(
    facets: list[dict[str, Any]], gear: int, *, is_asc: bool = True
) -> list[tuple[int, int, int]]:
    """Reconstruct symmetry groups from a flat list of facet dicts.

    Each facet dict must have an 'index' key (0-based tooth position).

    Returns a list of (rotational_symmetry, mirror_symmetry, base_index) tuples,
    one per detected symmetry group.
    """
    if is_asc:
        indices = _facet_tooth_indices_asc(facets, gear)
    else:
        indices = _facet_tooth_indices_gem(facets, gear)

    n = len(indices)
    if n == 0:
        return [(1, 0, 1)]

    # Try single symmetry pattern first
    single = _detect_single_symmetry(indices, gear)
    if single is not None:
        return [single]

    # Fallback: merged tier — decompose into individual patterns
    return _decompose_merged_tier(indices, gear)


# ============================================================================
# Tier data conversion (ASC / GEM → gem-designer format)
# ============================================================================

def _gemcad_angle_to_side(gemcad_angle: float) -> str:
    """Determine CROWN vs PAVILION from a GemCAD tier angle.

    GemCAD convention: negative = pavilion (below girdle),
                       positive = crown (above girdle).
    Angles near ±90° are the girdle and go to pavilion.
    """
    if abs(abs(gemcad_angle) - 90.0) < 0.1:
        return "PAVILION"
    if gemcad_angle < 0:
        return "PAVILION"
    return "CROWN"


def _convert_gemcad_angle(gemcad_angle: float, side: str) -> float:
    """Convert GemCAD angle to gem-designer angle convention.

    GemCAD:     negative = pavilion, positive = crown
    gem-designer: crown 0°–90° (0=table, 90=girdle),
                  pavilion 0°–90° (0=girdle, 90=culet)

    For crown:  angle = gemcad_angle (already correct: 35° crown → 35°)
    For pavilion: angle = abs(gemcad_angle) (already correct: -40° → 40°)
    Girdle (±90°): angle = 90°, side = pavilion
    """
    if abs(abs(gemcad_angle) - 90.0) < 0.1:
        return 90.0
    if side == "PAVILION":
        return abs(gemcad_angle)
    return gemcad_angle


def _convert_asc_tiers(asc_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert ASC-parsed tiers to gem-designer format."""
    gear: int = asc_data["index"]["gear"]
    tiers_out: list[dict[str, Any]] = []

    for tier in asc_data["tiers"]:
        facets = tier.get("facets", [])
        if not facets:
            continue

        gemcad_angle: float = tier["angle"]
        side: str = _gemcad_angle_to_side(gemcad_angle)
        angle: float = _convert_gemcad_angle(gemcad_angle, side)

        groups = _reconstruct_symmetry(facets, gear, is_asc=True)

        for gi, (rot_sym, mirror, base_idx) in enumerate(groups):
            # Try to pick up a meaningful name from the facets
            name = ""
            if len(groups) == 1:
                named = [f["name"] for f in facets if f.get("name")]
                if named:
                    name = named[0]

            if len(groups) > 1:
                suffix = chr(ord("a") + gi)
                name = f"{name}{suffix}" if name else f"T{chr(ord('a') + gi)}"

            tiers_out.append({
                "name": name,
                "side": side,
                "base_index": base_idx,
                "rotational_symmetry": rot_sym,
                "mirror_symmetry": mirror,
                "angle": angle,
                "height": tier["distance"],
                "frosted": 0.0,
                "enabled": True,
                "active": False,
            })

    _assign_default_tier_names(tiers_out)
    return tiers_out


def _convert_gem_tiers(gem_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert GEM-parsed tiers to gem-designer format.

    The GEM angle computation (acos-based, matching the C# code) already
    produces values in the same convention as ASC: negative = pavilion,
    positive = crown, relative to the girdle.  So we can reuse the same
    _convert_gemcad_angle helper used for ASC.
    """
    gear: int = gem_data["index"]["gear"]
    tiers_out: list[dict[str, Any]] = []

    for tier in gem_data["tiers"]:
        if tier.get("is_preform"):
            continue

        facets = tier.get("facets", [])
        if not facets:
            continue

        gemcad_angle: float = tier.get("angle", 0.0)
        side: str = _gemcad_angle_to_side(gemcad_angle)
        angle: float = _convert_gemcad_angle(gemcad_angle, side)

        groups = _reconstruct_symmetry(facets, gear, is_asc=False)

        for gi, (rot_sym, mirror, base_idx) in enumerate(groups):
            name = ""
            if len(groups) == 1:
                named = [f["name"] for f in facets if f.get("name")]
                if named:
                    name = named[0]

            if len(groups) > 1:
                suffix = chr(ord("a") + gi)
                name = f"{name}{suffix}" if name else f"T{chr(ord('a') + gi)}"

            tiers_out.append({
                "name": name,
                "side": side,
                "base_index": base_idx,
                "rotational_symmetry": rot_sym,
                "mirror_symmetry": mirror,
                "angle": angle,
                "height": tier.get("distance", 0.0),
                "frosted": 0.0,
                "enabled": True,
                "active": False,
            })

    _assign_default_tier_names(tiers_out)
    return tiers_out


def _assign_default_tier_names(tiers: list[dict[str, Any]]) -> None:
    """Fill in sensible default names for any tiers that lack them.

    Rules:
      - angle ≈ 0°, side = CROWN  →  "Table"
      - angle ≈ 90°               →  "Girdle" (numbered if multiple)
      - otherwise, unnamed on each side get numbered "Crown N" / "Pavilion N"
    """
    # Count how many girdle tiers need numbering
    girdle_count = sum(
        1 for t in tiers if not t["name"] and abs(t["angle"] - 90.0) < 0.5
    )
    girdle_seen = 0

    # Count unnamed non-girdle, non-table tiers per side
    crown_unnamed = [
        t for t in tiers
        if not t["name"]
        and t["side"] == "CROWN"
        and abs(t["angle"]) > 1.0
    ]
    pavilion_unnamed = [
        t for t in tiers
        if not t["name"]
        and t["side"] == "PAVILION"
        and abs(t["angle"] - 90.0) >= 0.5
    ]

    crown_idx = 0
    pavilion_idx = 0

    for tier in tiers:
        if tier["name"]:
            continue

        angle = tier["angle"]
        side = tier["side"]

        if side == "CROWN" and abs(angle) < 1.0:
            tier["name"] = "Table"
        elif abs(angle - 90.0) < 0.5:
            girdle_seen += 1
            tier["name"] = f"Girdle {girdle_seen}" if girdle_count > 1 else "Girdle"
        elif side == "CROWN":
            crown_idx += 1
            tier["name"] = f"Crown {crown_idx}" if len(crown_unnamed) > 1 else "Crown"
        else:
            pavilion_idx += 1
            tier["name"] = (
                f"Pavilion {pavilion_idx}" if len(pavilion_unnamed) > 1 else "Pavilion"
            )


# ============================================================================
# Material creation
# ============================================================================

def _apply_gemcad_material(
    obj: bpy.types.Object,
    metadata: dict[str, Any],
    *,
    label: str = "GemCAD",
) -> None:
    """Create or reuse a gem material from GemCAD metadata.

    Tries to auto-detect the gem type from the design title and headers.
    Falls back to a simple glass BSDF with the file's refractive index.
    """
    from ..data.materials import GEMS, detect_gem_type, DEFAULT_RENDER_DISPERSION
    from ..utils.node_utils import create_gem_material

    title: str = metadata.get("title", "")
    headers: list[str] = metadata.get("headers", [])

    # Build a search string from title + headers
    search_text = title
    for h in headers:
        search_text += " " + h

    gem_type = detect_gem_type(search_text)

    if gem_type and gem_type in GEMS:
        gem_data = GEMS[gem_type]
        try:
            mat = create_gem_material(
                gem_name=gem_type,
                main_ior=gem_data["main_ior"],
                birefringence_ior=gem_data["birefringence_ior"],
                dispersion=gem_data["dispersion"],
                color=gem_data["color"],
                color_density=gem_data.get("color_density", 5.0),
                has_birefringence=gem_data.get("has_birefringence", "No birefringence"),
                render_dispersion=gem_data.get(
                    "render_dispersion", DEFAULT_RENDER_DISPERSION
                ),
            )
        except Exception:
            mat = _make_fallback_gemcad_glass(metadata, label, title)
    else:
        mat = _make_fallback_gemcad_glass(metadata, label, title)

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def _make_fallback_gemcad_glass(
    metadata: dict[str, Any],
    label: str,
    title: str,
) -> bpy.types.Material:
    """Create a simple glass BSDF as a fallback for unrecognized GemCAD files."""
    refractive_index = metadata.get("refractive_index", 1.54)

    mat_name = f"{label}_{title}" if title else f"{label}_Import"
    mat_name = mat_name[:60]

    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")

    if bsdf:
        bsdf.inputs["Base Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.0
        bsdf.inputs["IOR"].default_value = refractive_index
        bsdf.inputs["Transmission Weight"].default_value = 1.0

    return mat


# ============================================================================
# Shared import pipeline
# ============================================================================

def _create_gem_object(
    context: Context,
    filepath: str,
    name: str,
    gear: int,
    tiers_out: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    label: str = "GEMCAD",
    source_format: str = "",
) -> bpy.types.Object:
    """Create the gem-designer object with modifiers, material, and bake.

    Shared between ASC and GEM importers.  Both produce the same
    tier data shape for the gem-designer pipeline.
    """
    from ..utils.node_utils import load_node_group, sync_modifiers, bake_all_tiers

    load_node_group()

    bpy.ops.mesh.primitive_cube_add(size=5.0, location=(0, 0, 0))
    obj: bpy.types.Object = context.active_object  # type: ignore[assignment]
    obj.hide_viewport = True
    obj.name = name
    obj["gem_designer"] = True
    obj["gem_index_gear"] = gear
    obj["gem_designer_tiers"] = json.dumps(tiers_out)

    # Source metadata for reference
    obj["gemcad_source"] = source_format
    obj["gemcad_title"] = metadata.get("title", "")
    obj["gemcad_headers"] = json.dumps(metadata.get("headers", []))
    obj["gemcad_footnotes"] = json.dumps(metadata.get("footnotes", []))

    # Material
    _apply_gemcad_material(obj, metadata, label=label)

    # Geometry-node modifiers
    sync_modifiers(obj, tiers_out, gear, active_tier_idx=0)

    # Bake
    bake_all_tiers(obj)

    # Populate scene tier list
    from ..utils.properties import scene_tiers_from_object

    tier_list = context.scene.gem_tier_list
    scene_tiers_from_object(obj, tier_list)
    tier_list.active_tier_index = len(tiers_out) - 1
    tier_list.index_gear = gear

    # Select and show
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    context.view_layer.objects.active = obj
    obj.hide_viewport = False

    return obj


# ============================================================================
# Operator: Import .asc
# ============================================================================

class GEM_OT_import_asc(bpy.types.Operator):
    """Import a GemCAD .asc design file"""

    bl_idname = "gem.import_asc"
    bl_label = "Import GemCAD ASC Design"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: bpy.props.StringProperty(  # type: ignore[valid-type]
        default="*.asc",
        options={"HIDDEN"},
    )

    def execute(self, context: Context) -> set[str]:
        filepath: str = self.filepath
        if not filepath:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}

        try:
            data = load_asc(filepath)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Failed to read ASC file: {e}")
            return {"CANCELLED"}

        name: str = data["info"]["title"] or Path(filepath).stem
        gear: int = data["index"]["gear"]

        tiers_out = _convert_asc_tiers(data)
        if not tiers_out:
            self.report({"ERROR"}, "No tiers with facet data found")
            return {"CANCELLED"}

        _create_gem_object(
            context,
            filepath,
            name,
            gear,
            tiers_out,
            metadata={
                "title": data["info"]["title"],
                "headers": data["info"]["headers"],
                "footnotes": data["info"]["footnotes"],
                "refractive_index": data["render"]["refractive_index"],
            },
            label="GemCAD_ASC",
            source_format="asc",
        )

        self.report(
            {"INFO"},
            f"Imported '{name}' — {len(tiers_out)} tiers, "
            f"{gear}-tooth gear, RI={data['render']['refractive_index']}",
        )
        return {"FINISHED"}

    def invoke(self, context: Context, event: Any) -> set[str]:
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


# ============================================================================
# Operator: Import .gem
# ============================================================================

class GEM_OT_import_gem(bpy.types.Operator):
    """Import a GemCAD .gem binary design file"""

    bl_idname = "gem.import_gem"
    bl_label = "Import GemCAD GEM Design"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: bpy.props.StringProperty(  # type: ignore[valid-type]
        default="*.gem",
        options={"HIDDEN"},
    )

    def execute(self, context: Context) -> set[str]:
        filepath: str = self.filepath
        if not filepath:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}

        try:
            data = load_gem(filepath)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Failed to read GEM file: {e}")
            return {"CANCELLED"}

        name: str = data["info"]["title"] or Path(filepath).stem
        gear: int = data["index"]["gear"]

        tiers_out = _convert_gem_tiers(data)
        if not tiers_out:
            self.report({"ERROR"}, "No non-preform tiers with facet data found")
            return {"CANCELLED"}

        _create_gem_object(
            context,
            filepath,
            name,
            gear,
            tiers_out,
            metadata={
                "title": data["info"]["title"],
                "headers": data["info"]["headers"],
                "footnotes": data["info"]["footnotes"],
                "refractive_index": data["render"]["refractive_index"],
            },
            label="GemCAD_GEM",
            source_format="gem",
        )

        self.report(
            {"INFO"},
            f"Imported '{name}' — {len(tiers_out)} tiers, "
            f"{gear}-tooth gear, RI={data['render']['refractive_index']}",
        )
        return {"FINISHED"}

    def invoke(self, context: Context, event: Any) -> set[str]:
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


# ============================================================================
# File > Import menu
# ============================================================================

def _menu_import_asc(self: Any, context: Context) -> None:
    self.layout.operator(GEM_OT_import_asc.bl_idname, text="GemCAD ASCII (.asc)")


def _menu_import_gem(self: Any, context: Context) -> None:
    self.layout.operator(GEM_OT_import_gem.bl_idname, text="GemCAD Binary (.gem)")


# ============================================================================
# Registration (called from __init__.py; class registration handled there)
# ============================================================================

def register() -> None:
    """Add both File > Import menu entries."""
    bpy.types.TOPBAR_MT_file_import.append(_menu_import_asc)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import_gem)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import_asc)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import_gem)
