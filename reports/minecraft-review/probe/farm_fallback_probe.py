"""Reviewer-only probe: the farm 'no plantable soil' fallback is not reached.

Run from the repository root:

    py -3.13 reports\\minecraft-review\\probe\\farm_fallback_probe.py

The resident claims `_ensure_project` falls back to a shelter project when no
plantable soil is in sight. `_farm_soil` instead returns a y-1 fallback with
block "unknown" whenever y-1 is inside the world, so `_project_steps` is never
empty and `_ensure_project` keeps a farm project whose child coordinates may be
stone/gravel/air. No game/world is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))

import pawbot.minecraft_resident as mr  # noqa: E402


def make_resident():
    resident = object.__new__(mr.MinecraftResident)
    resident.clock = lambda: 1000.0
    resident.state = SimpleNamespace(data={
        "projects": [], "promises": [], "drives": {"care_urge": .5},
        "personality": {"curiosity": .2, "orderliness": .5,
                        "industriousness": .5},
        "next_project_id": 2, "last_project_complete_at": 0,
    })
    return resident


def main():
    resident = make_resident()
    obs = {
        "health": 20.0, "food": 18.0,
        "position": {"x": 0.0, "y": 64.0, "z": 0.0},
        "dimension": "minecraft:overworld",
        # No farmland/dirt/grass_block: only stone under the feet.
        "nearby_blocks": [
            {"name": "stone", "position": {"x": 0, "y": 63, "z": 0}},
        ],
    }
    resident._ensure_project(obs)
    projects = resident.state.data["projects"]
    if not projects:
        print("no project created")
        return 1
    project = projects[-1]
    print("kind =", project["kind"])
    print("steps =", project["steps"])
    farm = [row for row in project["steps"] if row["skill"] == "farm"]
    if project["kind"] == "farm" and farm:
        print("FARM_ON_UNPLANTABLE_GROUND: project kept a farm step instead of "
              "falling back to shelter")
        return 1
    print("fallback behaved as claimed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
