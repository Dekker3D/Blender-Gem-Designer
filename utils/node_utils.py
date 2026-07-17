"""Utilities for loading and managing the geometry node group from the asset .blend."""

from __future__ import annotations

import os
from typing import Any

import bpy

NODE_GROUP_NAME = "GemTierCutter"
ASSET_BLEND = "gem_tier_cutter.blend"
RUBY_MATERIAL_NAME = "Ruby"
SHADER_GROUP_NAME = "Gem (Birefringent)"
WORLD_ASSET_KEY = "gem_world_from_asset"

# Modifier name prefix for gem tier modifiers (Blender 5.2 dropped ID properties
# on NodesModifier, so we encode the tier index in the modifier's internal name).
TIER_MOD_PREFIX = "GemTier_"


def _mod_tier_index(mod: bpy.types.Modifier) -> int | None:
    """Extract tier index from a modifier whose internal name starts with
    ``GemTier_NNN``.  Returns None if the modifier isn't a gem tier."""
    name = mod.name
    if not name.startswith(TIER_MOD_PREFIX):
        return None
    try:
        return int(name[len(TIER_MOD_PREFIX):][:3])
    except (ValueError, IndexError):
        return None


def _is_gem_modifier(mod: bpy.types.Modifier) -> bool:
    """True if *mod* is a NodesModifier belonging to the gem designer."""
    return mod.type == 'NODES' and mod.name.startswith(TIER_MOD_PREFIX)


def get_addon_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_ruby_material() -> bpy.types.Material:
    """Load the Ruby template material from the asset blend.

    Returns the material (already loaded or freshly imported).
    Also pulls in dependent node groups (Gem (Birefringent),
    Dispersion Glass) and the packed fingerprint image.
    """
    mat = bpy.data.materials.get(RUBY_MATERIAL_NAME)
    if mat is not None:
        return mat

    asset_path = os.path.join(get_addon_dir(), ASSET_BLEND)
    if not os.path.exists(asset_path):
        raise FileNotFoundError(f"Asset blend not found: {asset_path}")

    with bpy.data.libraries.load(asset_path, link=False) as (data_from, data_to):
        if RUBY_MATERIAL_NAME in data_from.materials:
            data_to.materials = [RUBY_MATERIAL_NAME]
        else:
            available = data_from.materials[:]
            raise KeyError(
                f"Material '{RUBY_MATERIAL_NAME}' not found in {asset_path}. "
                f"Available: {available}"
            )

    mat = bpy.data.materials.get(RUBY_MATERIAL_NAME)
    if mat is None:
        raise RuntimeError(
            f"Failed to load '{RUBY_MATERIAL_NAME}' from {asset_path}"
        )
    return mat


def load_world_asset() -> bpy.types.World:
    """Load the world shader from the asset blend and set it as scene world.

    The asset world uses a Light Path node: black for camera rays,
    machine-shop HDRI for all other rays.  Returns the loaded world.
    """
    # Return existing if already loaded
    for world in bpy.data.worlds:
        if world.get(WORLD_ASSET_KEY):
            bpy.context.scene.world = world
            return world

    asset_path = os.path.join(get_addon_dir(), ASSET_BLEND)
    if not os.path.exists(asset_path):
        raise FileNotFoundError(f"Asset blend not found: {asset_path}")

    worlds_before: set[str] = {w.name for w in bpy.data.worlds}

    with bpy.data.libraries.load(asset_path, link=False) as (data_from, data_to):
        if not data_from.worlds:
            raise KeyError(f"No worlds found in {asset_path}")
        data_to.worlds = data_from.worlds

    # Find the newly loaded world
    for world in bpy.data.worlds:
        if world.name not in worlds_before:
            world[WORLD_ASSET_KEY] = True
            bpy.context.scene.world = world
            return world

    raise RuntimeError("World loaded but not found in bpy.data.worlds")


def create_gem_material(
    gem_name: str,
    main_ior: float,
    birefringence_ior: float,
    dispersion: float,
    color: tuple[float, float, float],
    color_density: float = 5.0,
    has_birefringence: str = "No birefringence",
    render_dispersion: str = "Full Dispersion",
) -> bpy.types.Material:
    """Create a gem material by copying the Ruby template and setting parameters.

    Returns the new material (not yet assigned to any object).
    """
    template = load_ruby_material()

    mat_name = f"Gem_{gem_name}"
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = template.copy()
        mat.name = mat_name
    else:
        # Reuse existing material — ensure it still has valid nodes
        if not mat.use_nodes or mat.node_tree is None:
            mat = template.copy()
            mat.name = mat_name

    # Find the 'Gem (Birefringent)' group node and set its inputs
    for node in mat.node_tree.nodes:
        if node.type != 'GROUP':
            continue
        if not node.node_tree:
            continue
        if node.node_tree.name != SHADER_GROUP_NAME:
            continue

        node.inputs["Colour"].default_value = (*color, 1.0)
        node.inputs["Colour Density"].default_value = color_density
        node.inputs["Main IOR"].default_value = main_ior
        node.inputs["Birefringence IOR"].default_value = birefringence_ior
        node.inputs["Has Birefringence"].default_value = has_birefringence
        node.inputs["Dispersion"].default_value = dispersion
        node.inputs["Render Dispersion"].default_value = render_dispersion
        break
    else:
        # Group node missing — re-copy from template
        new_mat = template.copy()
        new_mat.name = mat_name
        # Remove the old mat and replace
        bpy.data.materials.remove(mat)
        mat = new_mat
        for node in mat.node_tree.nodes:
            if (
                node.type == 'GROUP'
                and node.node_tree
                and node.node_tree.name == SHADER_GROUP_NAME
            ):
                node.inputs["Colour"].default_value = (*color, 1.0)
                node.inputs["Colour Density"].default_value = color_density
                node.inputs["Main IOR"].default_value = main_ior
                node.inputs["Birefringence IOR"].default_value = birefringence_ior
                node.inputs["Has Birefringence"].default_value = has_birefringence
                node.inputs["Dispersion"].default_value = dispersion
                node.inputs["Render Dispersion"].default_value = render_dispersion
                break

    return mat


def load_node_group() -> bpy.types.NodeGroup:
    ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if ng is not None:
        return ng

    asset_path = os.path.join(get_addon_dir(), ASSET_BLEND)
    if not os.path.exists(asset_path):
        raise FileNotFoundError(f"Asset blend not found: {asset_path}")

    with bpy.data.libraries.load(asset_path, link=False) as (data_from, data_to):
        if NODE_GROUP_NAME in data_from.node_groups:
            data_to.node_groups = [NODE_GROUP_NAME]
        else:
            available = data_from.node_groups[:]
            raise KeyError(
                f"Node group '{NODE_GROUP_NAME}' not found in {asset_path}. "
                f"Available: {available}"
            )

    ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if ng is None:
        raise RuntimeError(f"Failed to load '{NODE_GROUP_NAME}' from {asset_path}")
    return ng


def _get_socket_map(ng: bpy.types.NodeGroup) -> dict[str, str]:
    expected = {"Base Index", "Rotational Symmetry", "Mirror Symmetry", "Angle", "Height", "Frosted"}
    socket_map: dict[str, str] = {}
    for item in ng.interface.items_tree:
        if hasattr(item, "name") and item.name in expected:
            identifier = getattr(item, "identifier", "")
            if identifier:
                socket_map[item.name] = identifier
                expected.discard(item.name)
    if expected:
        print(f"[Gem Designer] WARNING: Missing sockets in '{ng.name}': {expected}")
        print(f"[Gem Designer] Available: {[getattr(s, 'name', '?') for s in ng.interface.items_tree]}")
    return socket_map


_socket_map_cache: dict[str, dict[str, str]] = {}


def apply_tier_modifier(
    obj: bpy.types.Object,
    tier_index: int,
    tier_data: dict[str, Any],
    gear: int = 96,
) -> bool:
    """Create or update a geometry-nodes modifier for one tier.

    Returns True if any input values actually changed (useful for avoiding
    unnecessary bake invalidation).
    """
    ng = load_node_group()

    # Find or create modifier (Blender 5.2: name-based index, no ID properties)
    mod: bpy.types.NodesModifier | None = None
    for m in obj.modifiers:
        if m.type == 'NODES' and _mod_tier_index(m) == tier_index:
            mod = m  # type: ignore[assignment]
            break

    if mod is None:
        internal_name = f"{TIER_MOD_PREFIX}{tier_index:03d}"
        mod = obj.modifiers.new(name=internal_name, type='NODES')  # type: ignore[assignment]
        mod.node_group = ng  # type: ignore[union-attr]

    mod.show_viewport = True  # type: ignore[union-attr]
    mod.show_render = True  # type: ignore[union-attr]

    if NODE_GROUP_NAME not in _socket_map_cache:
        _socket_map_cache[NODE_GROUP_NAME] = _get_socket_map(ng)
    socket_map = _socket_map_cache[NODE_GROUP_NAME]

    # Convert teeth → degrees for GN inputs
    deg_per_tooth = 360.0 / max(gear, 1)
    tooth_0based = tier_data.get("base_index", 96) % gear
    gn_base_index = tooth_0based * deg_per_tooth
    gn_mirror = tier_data.get("mirror_symmetry", 0) * deg_per_tooth

    side: str = tier_data.get("side", "CROWN")
    diagram_angle: float = tier_data.get("angle", 45.0)
    if side == "CROWN":
        gn_angle = 90.0 - diagram_angle
    else:
        gn_angle = diagram_angle - 90.0

    values: dict[str, float] = {
        "Base Index": gn_base_index,
        "Rotational Symmetry": tier_data.get("rotational_symmetry", 8),
        "Mirror Symmetry": gn_mirror,
        "Angle": gn_angle,
        "Height": tier_data.get("height", 0.0),
        "Frosted": tier_data.get("frosted", 0.0),
    }

    changed = False
    for name, value in values.items():
        identifier = socket_map.get(name)
        if identifier is None:
            continue
        try:
            input_prop = getattr(mod.properties.inputs, identifier)
            current = input_prop.value
            if abs(current - value) > 1e-6:
                input_prop.value = value
                changed = True
        except Exception as e:
            print(f"[Gem Designer] ERROR setting '{identifier}' = {value}: {e}")

    return changed


def sync_modifiers(
    obj: bpy.types.Object,
    tiers: list[dict[str, Any]],
    gear: int = 96,
    active_tier_idx: int = -1,
) -> None:
    kept_indices: set[int] = set()
    for i, tier in enumerate(tiers):
        if not tier.get("enabled", True):
            continue
        apply_tier_modifier(obj, i, tier, gear=gear)
        kept_indices.add(i)

    for mod in list(obj.modifiers):
        if mod.type != 'NODES':
            continue
        idx = _mod_tier_index(mod)
        if idx is not None and idx not in kept_indices:
            obj.modifiers.remove(mod)


def bake_tier_modifier(obj: bpy.types.Object, mod: bpy.types.Modifier) -> bool:
    """Bake the 'Bake' node inside a single geometry-nodes modifier.

    Returns True if a bake was triggered.
    """
    if not hasattr(mod, "bakes"):
        return False

    for bake in mod.bakes:
        if bake.node.name == "Bake":
            try:
                bpy.ops.object.geometry_node_bake_single(
                    session_uid=bake.id_data.session_uid,
                    modifier_name=mod.name,
                    bake_id=bake.bake_id,
                )
                return True
            except (AttributeError, RuntimeError) as e:
                print(f"[Gem Designer] Bake failed for '{mod.name}': {e}")
                return False

    return False


def _unbake_modifier(obj: bpy.types.Object, mod: bpy.types.Modifier) -> bool:
    """Delete bake data for the 'Bake' node in a single modifier.

    Returns True if bake data was deleted.
    """
    if not hasattr(mod, "bakes"):
        return False

    for bake in mod.bakes:
        if bake.node.name == "Bake":
            try:
                bpy.ops.object.geometry_node_bake_delete_single(
                    session_uid=bake.id_data.session_uid,
                    modifier_name=mod.name,
                    bake_id=bake.bake_id,
                )
                return True
            except (AttributeError, RuntimeError) as e:
                print(f"[Gem Designer] Unbake failed for '{mod.name}': {e}")
                return False

    return False


def bake_all_tiers(obj: bpy.types.Object) -> int:
    """Bake every 'Bake' node in all tier modifiers on the object.

    Returns the number of bake nodes successfully triggered.
    """
    baked = 0
    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        if _mod_tier_index(mod) is None:
            continue
        if bake_tier_modifier(obj, mod):
            baked += 1
    if baked:
        print(f"[Gem Designer] Baked {baked} tier(s)")
    return baked


def unbake_all_tiers(obj: bpy.types.Object) -> int:
    """Delete bake data for all tier modifiers on the object.

    Returns the number of bake nodes deleted.
    """
    deleted = 0
    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        if _mod_tier_index(mod) is None:
            continue
        if _unbake_modifier(obj, mod):
            deleted += 1
    if deleted:
        print(f"[Gem Designer] Unbaked {deleted} tier(s)")
    return deleted


def bake_all_except(obj: bpy.types.Object, skip_tier_idx: int) -> tuple[int, int]:
    """Bake all tier modifiers except the one at skip_tier_idx.
    Unbakes the skipped tier so its geometry updates live.

    Returns (baked, unbaked) counts.
    """
    baked = 0
    unbaked = 0
    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        idx = _mod_tier_index(mod)
        if idx is None:
            continue
        if idx == skip_tier_idx:
            if _unbake_modifier(obj, mod):
                unbaked += 1
        else:
            if bake_tier_modifier(obj, mod):
                baked += 1
    if baked or unbaked:
        print(f"[Gem Designer] Baked {baked}, unbaked {unbaked} tier(s)")
    return baked, unbaked
