"""Tier data model for gem facet design."""

from __future__ import annotations

from typing import Any, Optional
import json

TIER_PROPERTY_KEY = "gem_designer_tiers"

DEFAULT_TIER: dict[str, Any] = {
    "name": "New Tier",
    "side": "CROWN",
    "base_index": 96,  # 1-based (faceting convention; wraps to gear)
    "rotational_symmetry": 8,
    "mirror_symmetry": 2,  # teeth — ±N from base index (0 = single facet)
    "angle": 45.0,  # degrees, gem diagram convention
    "height": 1.0,
    "frosted": 0.0,  # 0.0 = polished, 0.5 = satin, 1.0 = fully frosted
    "enabled": True,
    "active": False,
}


def expand_tier(tier: dict[str, Any], gear: int = 96) -> list[dict[str, Any]]:
    """Given a tier dict and index gear, return a flat list of individual facet definitions.

    Each facet has: index_deg (rotation around Z in degrees), angle, height.

    Mirror N: facets at base_index + N and base_index - N (modulo gear).
    Mirror 0: single facet at base_index.
    Rotational symmetry: evenly spaced copies around 360°.
    """
    rot_sym: int = tier["rotational_symmetry"]
    mirror_sym: int = tier["mirror_symmetry"]
    base_1based: int = tier["base_index"]  # 1-based (faceting convention)
    angle: float = tier["angle"]
    height: float = tier["height"]

    deg_per_tooth = 360.0 / max(gear, 1)
    facets: list[dict[str, Any]] = []

    if rot_sym <= 0:
        return facets

    step = gear // rot_sym if rot_sym > 0 else gear
    base_0based = base_1based % gear  # tooth 96 = 0°, tooth 24 = 90°

    for i in range(rot_sym):
        center = (base_0based + i * step) % gear

        if mirror_sym > 0:
            left = (center + mirror_sym) % gear
            right = (center - mirror_sym) % gear
            facets.append({"index_deg": left * deg_per_tooth, "angle": angle, "height": height})
            facets.append({"index_deg": right * deg_per_tooth, "angle": angle, "height": height})
        else:
            facets.append({"index_deg": center * deg_per_tooth, "angle": angle, "height": height})

    return facets


def get_tiers(obj: Any) -> list[dict[str, Any]]:
    raw = obj.get(TIER_PROPERTY_KEY, "[]")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def set_tiers(obj: Any, tiers: list[dict[str, Any]]) -> None:
    obj[TIER_PROPERTY_KEY] = json.dumps(tiers)


def add_tier(obj: Any, tier: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    tiers = get_tiers(obj)
    new_tier = dict(DEFAULT_TIER)
    if tier:
        new_tier.update(tier)
    new_tier["name"] = f"Tier {len(tiers) + 1}"
    tiers.append(new_tier)
    set_tiers(obj, tiers)
    return new_tier


def remove_tier(obj: Any, index: int) -> None:
    tiers = get_tiers(obj)
    if 0 <= index < len(tiers):
        tiers.pop(index)
        set_tiers(obj, tiers)


def move_tier(obj: Any, from_idx: int, to_idx: int) -> None:
    tiers = get_tiers(obj)
    if 0 <= from_idx < len(tiers) and 0 <= to_idx < len(tiers):
        tier = tiers.pop(from_idx)
        tiers.insert(to_idx, tier)
        set_tiers(obj, tiers)
