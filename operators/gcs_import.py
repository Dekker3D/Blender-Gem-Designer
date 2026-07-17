"""GCS (Gem Cut Studio) file importer.

Parses .gcs XML files and creates a gem-designer object with geometry-node
tiers, using the add-on's native modifier pipeline instead of building a raw
poly mesh.  The input is a unit cube; the node groups do the cutting.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import bpy
from bpy.types import Context


def _parse_float(elem: ET.Element, attr: str) -> float:
    """Parse an attribute as float, returning 0.0 on failure."""
    try:
        return float(elem.get(attr, "0"))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def load_gcs(filepath: str) -> dict[str, Any]:
    """Parse a .gcs file and return structured data.

    Returns a dict with:
        info: {title, author, date}
        render: {material, refractive_index, dispersion, clarity, density,
                 lighting_model, color: (r, g, b)}
        index: {gear, base, symmetry, mirror}
        tiers: [{name, angle, depth, visible, guide,
                 facets: [{nx, ny, nz, index_angle,
                           vertices: [(x, y, z), ...]}]}]
    """
    tree = ET.parse(filepath)
    root = tree.getroot()

    # --- Info ---
    info_elem = root.find("info")
    info: dict[str, str] = {"title": "", "author": "", "date": ""}
    if info_elem is not None:
        info["title"] = info_elem.get("title", "")
        info["author"] = info_elem.get("author", "")
        info["date"] = info_elem.get("date", "")

    # --- Render / material ---
    render_elem = root.find("render")
    render: dict[str, Any] = {
        "material": "Quartz",
        "refractive_index": 1.54,
        "dispersion": 0.0,
        "clarity": 100,
        "density": 1.0,
        "lighting_model": "Random",
        "color": (1.0, 1.0, 1.0),
    }
    if render_elem is not None:
        render["material"] = render_elem.get("material", "Quartz")
        render["refractive_index"] = _parse_float(render_elem, "refractive_index") or 1.54
        render["dispersion"] = _parse_float(render_elem, "dispersion")
        render["clarity"] = _parse_float(render_elem, "clarity") or 100
        render["density"] = _parse_float(render_elem, "density") or 1.0
        render["lighting_model"] = render_elem.get("lighting_model", "Random")
        color_elem = render_elem.find("color")
        if color_elem is not None:
            render["color"] = (
                _parse_float(color_elem, "r") or 1.0,
                _parse_float(color_elem, "g") or 1.0,
                _parse_float(color_elem, "b") or 1.0,
            )

    # --- Index ---
    index_elem = root.find("index")
    index_data: dict[str, int] = {"gear": 96, "base": 0, "symmetry": 1, "mirror": 0}
    if index_elem is not None:
        index_data["gear"] = int(_parse_float(index_elem, "gear") or 96)
        index_data["base"] = int(_parse_float(index_elem, "base") or 0)
        index_data["symmetry"] = int(_parse_float(index_elem, "symmetry") or 1)
        index_data["mirror"] = int(_parse_float(index_elem, "mirror") or 0)

    # --- Tiers ---
    tiers: list[dict[str, Any]] = []
    for tier_elem in root.findall("tier"):
        tier: dict[str, Any] = {
            "name": tier_elem.get("name", ""),
            "angle": _parse_float(tier_elem, "angle"),
            "depth": _parse_float(tier_elem, "depth"),
            "visible": tier_elem.get("visible", "true") == "true",
            "guide": tier_elem.get("guide", "false") == "true",
            "facets": [],
        }
        for facet_elem in tier_elem.findall("facet"):
            facet: dict[str, Any] = {
                "nx": _parse_float(facet_elem, "nx"),
                "ny": _parse_float(facet_elem, "ny"),
                "nz": _parse_float(facet_elem, "nz"),
                "index_angle": _parse_float(facet_elem, "index_angle"),
                "frosting": _parse_float(facet_elem, "frosting"),
                "vertices": [],
            }
            for vert_elem in facet_elem.findall("vertex"):
                vx = _parse_float(vert_elem, "x")
                vy = _parse_float(vert_elem, "y")
                vz = _parse_float(vert_elem, "z")
                facet["vertices"].append((vx, vy, vz))
            tier["facets"].append(facet)
        tiers.append(tier)

    return {
        "info": info,
        "render": render,
        "index": index_data,
        "tiers": tiers,
    }


# ---------------------------------------------------------------------------
# Material creation from GCS render data
# ---------------------------------------------------------------------------

def _apply_gcs_material(obj: bpy.types.Object, render: dict[str, Any]) -> None:
    """Create or reuse a gem material from GCS render data.

    Tries to auto-detect the gem type from the GCS material name and apply the
    full Gem (Birefringent) shader.  Falls back to a simple glass BSDF if the
    material name isn't recognized.
    """
    from ..data.materials import GEMS, detect_gem_type, DEFAULT_RENDER_DISPERSION
    from ..utils.node_utils import create_gem_material

    material_name: str = render.get("material", "")
    gem_type = detect_gem_type(material_name)

    if gem_type and gem_type in GEMS:
        gem_data = GEMS[gem_type]
        # Use the GCS file's colour, fall back to GEMS default
        file_color: tuple[float, float, float] = render.get("color", (1.0, 1.0, 1.0))
        try:
            mat = create_gem_material(
                gem_name=gem_type,
                main_ior=gem_data["main_ior"],
                birefringence_ior=gem_data["birefringence_ior"],
                dispersion=gem_data["dispersion"],
                color=file_color,
                color_density=gem_data.get("color_density", 5.0),
                has_birefringence=gem_data.get("has_birefringence", "No birefringence"),
                render_dispersion=gem_data.get(
                    "render_dispersion", DEFAULT_RENDER_DISPERSION
                ),
            )
        except Exception:
            mat = _make_fallback_glass(obj, render, material_name)
    else:
        mat = _make_fallback_glass(obj, render, material_name)

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def _make_fallback_glass(
    obj: bpy.types.Object,
    render: dict[str, Any],
    material_name: str,
) -> bpy.types.Material:
    """Create a simple glass BSDF as a fallback material."""
    mat_name = f"GCS_{material_name}" if material_name else "GCS_Import"
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")

    if bsdf:
        r, g, b = render["color"]
        bsdf.inputs["Base Color"].default_value = (r, g, b, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.0
        bsdf.inputs["IOR"].default_value = render["refractive_index"]
        bsdf.inputs["Transmission Weight"].default_value = 1.0

    return mat


# ---------------------------------------------------------------------------
# Tier data conversion (GCS → gem-designer format)
# ---------------------------------------------------------------------------

def _gcs_tier_side(gcs_angle: float) -> str:
    """Determine CROWN vs PAVILION from the GCS tier angle.

    Angles near 90° (girdle) go to pavilion side — the gem designer
    needs them there for the geometry nodes to cut correctly.
    """
    if 89.9 <= gcs_angle <= 90.1:
        return "PAVILION"
    if gcs_angle > 90.0:
        return "PAVILION"
    return "CROWN"


def _convert_gcs_angle(gcs_angle: float, side: str) -> float:
    """Convert GCS gem-diagram angle to the addon's convention.

    GCS uses the full 0°–180° gem-diagram range:
      0° = table/culet, 90° = girdle, 180° = culet.

    The addon expects crown (0°–90°) and pavilion (0°–90° measured from
    girdle).  Pavilion angles in GCS (90°–180°) are flipped:
      180° − gcs_angle  →  addon pavilion angle.

    Girdle angles near 90° are capped at exactly 90° and treated as
    pavilion for correct geometry-node cutting.
    """
    if 89.9 <= gcs_angle <= 90.1:
        return 90.0
    if side == "PAVILION":
        return 180.0 - gcs_angle
    return gcs_angle


def _angle_to_base_index(index_angle: float, gear: int) -> int:
    """Convert a GCS index_angle (degrees) to a tooth index.

    Both GCS and the gem designer use 0-based modulo — 0° = tooth 0.
    The gem designer's sync_modifiers() calls ``% gear`` before use,
    so any value works; we just compute the 0-based tooth.
    """
    deg_per_tooth = 360.0 / max(gear, 1)
    return round(index_angle / deg_per_tooth) % gear


def _facet_tooth_indices(facets: list[dict[str, Any]], gear: int) -> list[int]:
    """Convert facet index_angle values to 0-based tooth indices, sorted."""
    deg_per_tooth = 360.0 / max(gear, 1)
    indices = [round(f["index_angle"] / deg_per_tooth) % gear for f in facets]
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
            # Use the shorter path around the gear as the mirror distance.
            short_gap = min(gap, gear - gap)
            mirror = short_gap // 2
            # Center needs to give the correct facet positions.
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

        # Try group sizes from largest to smallest
        best_n = 1
        remaining = [i for i in indices if i not in used]
        for n in range(len(remaining), 1, -1):
            if gear % n != 0:
                continue
            step = gear // n
            # Check if start_idx + k*step (mod gear) are all in the set
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
    facets: list[dict[str, Any]], gear: int
) -> list[tuple[int, int, int]]:
    """Reconstruct symmetry groups from a flat list of facets.

    Returns a list of (rotational_symmetry, mirror_symmetry, base_index) tuples,
    one per detected symmetry group.  Merged tiers are decomposed into
    separate groups.

    Pure tooth-index-based detection — does not use the file-level <index>
    element, which only reflects the last-saved symmetry settings.
    """
    n = len(facets)
    if n == 0:
        return [(1, 0, 1)]

    indices = _facet_tooth_indices(facets, gear)

    # Try single symmetry pattern first
    single = _detect_single_symmetry(indices, gear)
    if single is not None:
        return [single]

    # Fallback: merged tier — decompose into individual patterns
    return _decompose_merged_tier(indices, gear)


def _convert_gcs_tiers(gcs_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert GCS tiers to gem-designer format.

    Returns a list of tier dicts suitable for storage in gem_designer_tiers
    JSON and for feeding into sync_modifiers().
    """
    gear: int = gcs_data["index"]["gear"]
    tiers_out: list[dict[str, Any]] = []

    for tier in gcs_data["tiers"]:
        if tier["guide"]:
            continue

        facets: list[dict[str, Any]] = [
            f for f in tier["facets"] if len(f["vertices"]) >= 3
        ]
        n_facets = len(facets)
        if n_facets == 0:
            continue

        groups = _reconstruct_symmetry(facets, gear)
        gcs_angle: float = tier["angle"]
        side: str = _gcs_tier_side(gcs_angle)

        # Frosting amount from the first facet (0.0 if no frosting attribute)
        frosted: float = facets[0].get("frosting", 0.0) if facets else 0.0

        for gi, (rot_sym, mirror, base_idx) in enumerate(groups):
            # For multi-group tiers, suffix the name (e.g. "P2" -> "P2a", "P2b")
            name: str = tier["name"]
            if len(groups) > 1:
                name = f"{name}{chr(ord('a') + gi)}"

            tiers_out.append({
                "name": name,
                "side": side,
                "base_index": base_idx,
                "rotational_symmetry": rot_sym,
                "mirror_symmetry": mirror,
                "angle": _convert_gcs_angle(gcs_angle, side),
                "height": tier["depth"],
                "frosted": frosted,
                "enabled": tier["visible"],
                "active": False,
            })

    return tiers_out


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class GEM_OT_import_gcs(bpy.types.Operator):
    """Import a Gem Cut Studio .gcs design file"""

    bl_idname = "gem.import_gcs"
    bl_label = "Import GCS Design"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: bpy.props.StringProperty(  # type: ignore[valid-type]
        default="*.gcs",
        options={"HIDDEN"},
    )

    def execute(self, context: Context) -> set[str]:
        filepath: str = self.filepath
        if not filepath:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}

        # --- Parse GCS ---
        try:
            data = load_gcs(filepath)
        except ET.ParseError as e:
            self.report({"ERROR"}, f"XML parse error: {e}")
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Failed to read GCS file: {e}")
            return {"CANCELLED"}

        name: str = data["info"]["title"] or Path(filepath).stem
        gear: int = data["index"]["gear"]

        # --- Convert tiers ---
        tiers_out = _convert_gcs_tiers(data)
        if not tiers_out:
            self.report({"ERROR"}, "No visible tiers with facet data found")
            return {"CANCELLED"}

        # --- Create base object (unit cube, same as setup_gem) ---
        from ..utils.node_utils import load_node_group, sync_modifiers, bake_all_tiers

        load_node_group()  # ensure GemTierCutter is available

        # Hide while building modifiers to prevent re-evaluation per tier
        bpy.ops.mesh.primitive_cube_add(size=5.0, location=(0, 0, 0))
        obj: bpy.types.Object = context.active_object  # type: ignore[assignment]
        obj.hide_viewport = True
        obj.name = name
        obj["gem_designer"] = True
        obj["gem_index_gear"] = gear
        obj["gem_designer_tiers"] = json.dumps(tiers_out)

        # GCS metadata for reference
        obj["gcs_title"] = data["info"]["title"]
        obj["gcs_author"] = data["info"]["author"]
        obj["gcs_date"] = data["info"]["date"]
        obj["gcs_material"] = data["render"]["material"]

        # --- Apply material ---
        _apply_gcs_material(obj, data["render"])

        # --- Build geometry-node modifiers ---
        sync_modifiers(obj, tiers_out, gear, active_tier_idx=0)

        # --- Bake cutter meshes to avoid recomputation ---
        bake_all_tiers(obj)

        # --- Populate scene tier list so the panel shows tiers immediately ---
        from ..utils.properties import scene_tiers_from_object

        tier_list = context.scene.gem_tier_list
        scene_tiers_from_object(obj, tier_list)
        tier_list.active_tier_index = len(tiers_out) - 1
        tier_list.index_gear = gear

        # --- Select ---
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj
        obj.hide_viewport = False  # show after all modifiers + bakes are ready

        self.report(
            {"INFO"},
            f"Imported '{name}' — {len(tiers_out)} tiers, "
            f"{gear}-tooth gear, {data['render']['material']} material",
        )
        return {"FINISHED"}

    def invoke(self, context: Context, event: Any) -> set[str]:
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


# ---------------------------------------------------------------------------
# File > Import menu
# ---------------------------------------------------------------------------

def _menu_import(self: Any, context: Context) -> None:
    self.layout.operator(GEM_OT_import_gcs.bl_idname, text="Gem Cut Studio (.gcs)")


# ---------------------------------------------------------------------------
# Registration (called from __init__.py; class registration handled there)
# ---------------------------------------------------------------------------

def register() -> None:
    """Add File > Import menu entry (class is registered via _classes in __init__)."""
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
