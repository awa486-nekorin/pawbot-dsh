"""Reviewer-only probe: farm stand cell vs. the child's 4.5-block soil reach.

Run from the repository root:

    py -3.13 reports\\minecraft-review\\probe\\farm_stand_reach_probe.py

The resident picks every soil within two blocks of the *feet* and emits one
`move` to a stand cell followed by `farm` at each soil. The real Node adapter
requires the soil block to be within 4.5 blocks of the player at the moment of
the farm call (`adapter.js` `nearby(p, 4.5)`). Two soils in opposite corners are
both legal at generation time but one is out of reach from the chosen stand, so
the child rejects it. No game/world is touched.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))

import pawbot.minecraft_resident as mr  # noqa: E402


def main():
    obs = {
        "health": 20.0,
        "food": 18.0,
        "position": {"x": 0.0, "y": 64.0, "z": 0.0},
        "dimension": "minecraft:overworld",
        "nearby_blocks": [
            {"name": "dirt", "position": {"x": 2, "y": 63, "z": 2}},
            {"name": "dirt", "position": {"x": -2, "y": 63, "z": -2}},
        ],
    }
    resident = object.__new__(mr.MinecraftResident)
    steps = resident._project_steps("farm", {"x": 0, "y": 64, "z": 0}, obs)
    print("steps =", steps)
    move = next((row for row in steps if row["skill"] == "move"), None)
    farms = [row for row in steps if row["skill"] == "farm"]
    if move is None or not farms:
        print("no farm sequence generated")
        return 1
    stand = move["args"]
    worst = 0.0
    for row in farms:
        soil = row["args"]
        distance = math.dist((stand["x"], stand["y"], stand["z"]),
                             (soil["x"], soil["y"], soil["z"]))
        worst = max(worst, distance)
        print(f"stand={stand['x']},{stand['y']},{stand['z']} "
              f"soil={soil['x']},{soil['y']},{soil['z']} distance={distance:.3f} "
              f"child_limit=4.5 ok={distance <= 4.5}")
    if worst > 4.5:
        print(f"OUT_OF_REACH: farthest generated farm is {worst:.3f} > 4.5")
        return 1
    print("all generated farm steps are within child reach")
    return 0


if __name__ == "__main__":
    sys.exit(main())
