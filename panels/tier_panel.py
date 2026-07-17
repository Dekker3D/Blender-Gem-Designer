"""N-panel UI — Crown and Pavilion sections with tier lists."""

from __future__ import annotations

from typing import Optional

import bpy
from bpy.types import Context, UILayout

from ..utils.properties import GemTierList, GemTierProperty


def _get_gem_object(context: Context) -> Optional[bpy.types.Object]:
    obj = context.active_object
    if obj and obj.get("gem_designer"):
        return obj
    return None


def _draw_tier_list(
    layout: UILayout,
    context: Context,
    tiers: list[GemTierProperty],  # CollectionProperty acts as list
    tier_indices: list[int],
    active_index: int,
) -> None:
    """Draw a single tier list section. tier_indices maps display order → flat index."""
    gem = _get_gem_object(context)
    if gem is None:
        return

    for display_i, flat_i in enumerate(tier_indices):
        tier = tiers[flat_i]
        is_active = (flat_i == active_index)

        box = layout.box()
        row = box.row()

        # Active toggle
        icon = 'RADIOBUT_ON' if is_active else 'RADIOBUT_OFF'
        op = row.operator("gem.set_active_tier", text="", icon=icon, emboss=False)
        op.tier_index = flat_i

        # Name
        row.prop(tier, "name", text="")  # type: ignore[arg-type]

        # Move up/down within flat list
        sub = row.row(align=True)
        op = sub.operator("gem.move_tier_up", text="", icon='TRIA_UP')
        op.tier_index = flat_i
        op = sub.operator("gem.move_tier_down", text="", icon='TRIA_DOWN')
        op.tier_index = flat_i

        # Move to other side
        op = sub.operator("gem.move_tier_side", text="", icon='ARROW_LEFTRIGHT')
        op.tier_index = flat_i

        # Remove
        op = sub.operator("gem.remove_tier", text="", icon='X')
        op.tier_index = flat_i

        # Parameters for active tier
        if is_active:
            col = box.column(align=True)
            col.prop(tier, "base_index")  # type: ignore[arg-type]
            col.prop(tier, "rotational_symmetry")  # type: ignore[arg-type]
            col.prop(tier, "mirror_symmetry")  # type: ignore[arg-type]
            col.prop(tier, "angle")  # type: ignore[arg-type]
            col.prop(tier, "height")  # type: ignore[arg-type]
            col.prop(tier, "frosted")  # type: ignore[arg-type]


class GEM_PT_main_panel(bpy.types.Panel):
    bl_label = "Gem Designer"
    bl_idname = "GEM_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gem"

    def draw(self, context: Context) -> None:
        layout = self.layout
        obj = context.active_object
        all_tiers: GemTierList = context.scene.gem_tier_list

        if obj is None or not obj.get("gem_designer"):
            col = layout.column(align=True)
            col.operator("gem.setup_gem", text="Setup New Gem", icon='OUTLINER_OB_MESH')
            col.operator("gem.import_gcs", text="Import GCS Design", icon='IMPORT')
            return

        # Header
        row: UILayout = layout.row()
        row.label(text=f"Gem: {obj.name}", icon='OUTLINER_OB_MESH')
        row.operator("gem.import_gcs", text="", icon='IMPORT')
        row.operator("gem.bake_tiers", text="", icon='RENDER_STILL')
        row.operator("gem.refresh_modifiers", text="", icon='FILE_REFRESH')

        # Index gear
        gear_row = layout.row(align=True)
        gear_row.label(text=f"Index Gear: {all_tiers.index_gear}")
        gear_row.operator("gem.set_index_gear", text="", icon='SETTINGS')

        # Split tiers into crown / pavilion
        crown_indices: list[int] = [
            i for i, t in enumerate(all_tiers.tiers) if t.side == 'CROWN'
        ]
        pavilion_indices: list[int] = [
            i for i, t in enumerate(all_tiers.tiers) if t.side == 'PAVILION'
        ]
        active: int = all_tiers.active_tier_index

        # -- Crown --
        layout.separator()
        row = layout.row()
        row.label(text=f"Crown / Table ({len(crown_indices)})", icon='TRIA_UP')
        op = row.operator("gem.add_tier", text="", icon='ADD')
        op.side = 'CROWN'

        if crown_indices:
            _draw_tier_list(layout, context, all_tiers.tiers, crown_indices, active)  # type: ignore[arg-type]
        else:
            layout.label(text="  No crown tiers yet", icon='BLANK1')

        # -- Pavilion --
        layout.separator()
        row = layout.row()
        row.label(text=f"Pavilion / Culet ({len(pavilion_indices)})", icon='TRIA_DOWN')
        op = row.operator("gem.add_tier", text="", icon='ADD')
        op.side = 'PAVILION'

        if pavilion_indices:
            _draw_tier_list(layout, context, all_tiers.tiers, pavilion_indices, active)  # type: ignore[arg-type]
        else:
            layout.label(text="  No pavilion tiers yet", icon='BLANK1')


class GEM_PT_material_panel(bpy.types.Panel):
    bl_label = "Material"
    bl_idname = "GEM_PT_material_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gem"
    bl_parent_id = "GEM_PT_main_panel"

    def draw(self, context: Context) -> None:
        layout = self.layout
        obj = _get_gem_object(context)
        if obj is None:
            return

        from ..data.materials import GEM_NAMES
        layout.label(text="Gem Material Preset")
        col = layout.column(align=True)
        for gem_name, _ in GEM_NAMES:
            op = col.operator("gem.apply_material", text=gem_name)
            op.gem_type = gem_name


class GEM_PT_scene_setup(bpy.types.Panel):
    bl_label = "Scene Setup"
    bl_idname = "GEM_PT_scene_setup"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gem"
    bl_parent_id = "GEM_PT_main_panel"

    def draw(self, context: Context) -> None:
        layout = self.layout
        obj = _get_gem_object(context)

        col = layout.column(align=True)
        op = col.operator("gem.render_preview", text="Preview Render Settings", icon='RENDER_STILL')
        op = col.operator("gem.render_full_gi", text="Full GI Light Paths", icon='LIGHT_AREA')
        col.separator()

        row = col.row(align=True)
        op = row.operator("gem.camera_setup", text="Camera from View + DoF", icon='CAMERA_DATA')
        if obj is None:
            row.enabled = False

        col.separator()
        op = col.operator("gem.world_background", text="Studio World Background", icon='WORLD')
