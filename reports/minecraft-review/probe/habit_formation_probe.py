"""Reviewer-only probe: are the semantic habit branches ever formed?

Run from the repository root:

    py -3.13 reports\\minecraft-review\\probe\\habit_formation_probe.py

The resident reads ``habits["return_home"]``, ``habits["tend_farm"]`` and
``habits["greet_known"]`` to add habit-driven candidates, but every successful
skill is recorded under its skill name (``home``/``farm``/``follow``). This probe
replays successful home/farm/follow actions through the same ``_success`` path
and shows the three semantic keys stay at count 0, so the branches are dead.
No game, world, credential or repository file is touched.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))

from pawbot.minecraft_resident import MinecraftResident, ResidentState  # noqa: E402


def main() -> int:
    root = tempfile.mkdtemp(prefix="pawbot-habit-probe-")
    resident = object.__new__(MinecraftResident)
    resident.state = ResidentState(os.path.join(root, "resident.json"),
                                   clock=lambda: 1000.0)
    resident.clock = lambda: 1000.0
    resident._failure_streak = 0
    resident._action_log = []
    for _ in range(3):
        resident._success("rest", "home", {}, {"project_id": "", "promise_id": ""}, {})
        resident._success("continue_project", "farm", {},
                          {"project_id": "", "promise_id": ""}, {})
        resident._success("socialize", "follow",
                          {"target_uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
                          {"project_id": "", "promise_id": ""}, {})

    habits = resident.state.data["habits"]
    print("habit_keys=", sorted(habits))
    for key in ("return_home", "tend_farm", "greet_known"):
        print(f"{key} count=", habits.get(key, {}).get("count", 0))
    reachable = any(habits.get(key, {}).get("count", 0) >= 2
                    for key in ("return_home", "tend_farm", "greet_known"))
    print("candidate_branches_reachable=", reachable)
    return 0 if reachable else 1


if __name__ == "__main__":
    sys.exit(main())
