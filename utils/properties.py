"""Custom property definitions for the Gem Designer add-on."""

import math
from typing import Any, Callable, Optional

import bpy
from bpy.props import (
    IntProperty, FloatProperty, BoolProperty, StringProperty,
    EnumProperty, CollectionProperty,
)
from bpy.types import Context, Scene

from .tier_data import get_tiers, set_tiers

_sync_callback: Optional[Callable[[Context], None]] = None
_loading: bool = False  # guard against re-entry during bulk data load

SIDE_ITEMS: list[tuple[str, str, str]] = [
    ('CROWN', 'Crown / Table', 'Facets above the girdle'),
    ('PAVILION', 'Pavilion / Culet', 'Facets below the girdle'),
]


def _on_tier_changed(self: "GemTierProperty", context: Context) -> None:
    if _loading or _sync_callback is None:
        return
    _sync_callback(context)


class GemTierProperty(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Tier",
        update=_on_tier_changed)
    side: EnumProperty(name="Side", items=SIDE_ITEMS, default='CROWN',
        update=_on_tier_changed)
    base_index: IntProperty(
        name="Base Index",
        description="Index tooth for this tier (1 = first, wraps at gear)",
        default=96, soft_min=-24, soft_max=120,  # negative wraps to gear
        update=_on_tier_changed,
    )
    rotational_symmetry: IntProperty(
        name="Rotational Sym", description="Number of evenly-spaced copies around Z",
        default=8, min=0, soft_max=12,
        update=_on_tier_changed,
    )
    mirror_symmetry: IntProperty(
        name="Mirror", description="Offset in teeth from base index. 0 = single facet, N = ±N pair",
        default=2, min=0, soft_max=60,
        update=_on_tier_changed,
    )
    angle: FloatProperty(
        name="Angle",
        description="Gem diagram angle: 0° = table/culet, 90° = girdle",
        default=math.radians(45.0),
        min=0.0, max=math.radians(90.0),
        subtype='ANGLE',
        update=_on_tier_changed,
    )
    height: FloatProperty(
        name="Height", description="Distance from object origin along Z",
        default=1.0, min=0.0, soft_max=2.0,
        update=_on_tier_changed,
    )
    enabled: BoolProperty(name="Enabled", default=True,
        update=_on_tier_changed,
    )
    frosted: FloatProperty(
        name="Frosted",
        description="Frosting amount: 0 = polished, 0.5 = satin, 1 = fully frosted",
        default=0.0, min=0.0, max=1.0,
        update=_on_tier_changed,
    )

    def from_dict(self, d: dict[str, Any]) -> None:
        self.name = d.get("name", "Tier")
        self.side = d.get("side", "CROWN")
        self.base_index = d.get("base_index", 96)
        self.rotational_symmetry = d.get("rotational_symmetry", 8)
        self.mirror_symmetry = d.get("mirror_symmetry", 2)
        self.angle = math.radians(d.get("angle", 45.0))
        self.height = d.get("height", 1.0)
        self.frosted = d.get("frosted", 0.0)
        self.enabled = d.get("enabled", True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "side": self.side,
            "base_index": self.base_index,
            "rotational_symmetry": self.rotational_symmetry,
            "mirror_symmetry": self.mirror_symmetry,
            "angle": math.degrees(self.angle),
            "height": self.height,
            "frosted": self.frosted,
            "enabled": self.enabled,
            "active": False,
        }


class GemTierList(bpy.types.PropertyGroup):
    tiers: CollectionProperty(type=GemTierProperty)
    active_tier_index: IntProperty(default=-1)
    index_gear: IntProperty(
        name="Index Gear",
        description="Number of teeth on the index wheel (typically 96)",
        default=96, min=12, max=120,
    )


_last_active_object_name: Optional[str] = None


def scene_tiers_from_object(obj: bpy.types.Object, scene_tiers: GemTierList) -> None:
    global _last_active_object_name, _loading
    _last_active_object_name = obj.name
    _loading = True

    raw_tiers = get_tiers(obj)
    scene_tiers.tiers.clear()
    for i, td in enumerate(raw_tiers):
        item = scene_tiers.tiers.add()
        item.from_dict(td)
        if td.get("active"):
            scene_tiers.active_tier_index = i

    if scene_tiers.active_tier_index >= len(raw_tiers):
        scene_tiers.active_tier_index = -1

    gear: int = obj.get("gem_index_gear", 96)  # type: ignore[assignment]
    scene_tiers.index_gear = gear
    _loading = False


def scene_tiers_to_object(obj: bpy.types.Object, scene_tiers: GemTierList) -> None:
    tiers = [item.to_dict() for item in scene_tiers.tiers]
    active_idx: int = scene_tiers.active_tier_index
    for i, t in enumerate(tiers):
        t["active"] = (i == active_idx)
    set_tiers(obj, tiers)
    obj["gem_index_gear"] = scene_tiers.index_gear


def push_and_sync(context: Context) -> None:
    global _loading
    if _loading:
        return
    obj = context.active_object
    if obj is None or not obj.get("gem_designer"):
        return

    _loading = True
    tier_list: GemTierList = context.scene.gem_tier_list

    # Wrap base_index to 1..gear (faceting convention)
    gear: int = tier_list.index_gear
    for tier in tier_list.tiers:
        raw: int = tier.base_index
        if raw < 1 or raw > gear:
            wrapped = raw % gear
            tier.base_index = gear if wrapped == 0 else wrapped
        tier.mirror_symmetry = max(0, min(tier.mirror_symmetry, gear // 2))

    scene_tiers_to_object(obj, tier_list)

    from ..utils.node_utils import sync_modifiers, bake_all_except
    raw_tiers = get_tiers(obj)
    sync_modifiers(obj, raw_tiers, gear, active_tier_idx=tier_list.active_tier_index)

    # Maintain bake invariant: selected tier unbaked (live editing), rest baked
    active_idx: int = tier_list.active_tier_index
    if active_idx >= 0:
        bake_all_except(obj, active_idx)

    _loading = False


def maybe_pull_on_active_change(scene: Scene) -> None:
    global _last_active_object_name, _loading
    if _loading:
        return
    obj = bpy.context.active_object
    if obj is None:
        _last_active_object_name = None
        return
    if not obj.get("gem_designer"):
        _last_active_object_name = None
        return
    if obj.name != _last_active_object_name:
        tier_list: GemTierList = scene.gem_tier_list
        scene_tiers_from_object(obj, tier_list)
