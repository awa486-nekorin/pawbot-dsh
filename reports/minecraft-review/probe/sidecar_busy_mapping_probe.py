"""Reviewer-only probe: Node's -32008 "Guard busy" is not a resident pause.

Run from the repository root:

    py -3.13 reports\\minecraft-review\\probe\\sidecar_busy_mapping_probe.py

The sidecar pauses skill admission during a signed busy window and answers
JSON-RPC -32008 (`Guard busy`). The resident only recognizes `GuardBusy` from its
own guard client, so this sidecar error is recorded as `call_failed` and counts
against the failure streak instead of being a bounded pause. No game/world is
touched; the probe uses the contract test harness with a synthetic sidecar.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "tools"))

from pawbot.minecraft_sidecar import SidecarError  # noqa: E402
from pawbot.minecraft_resident import Decision  # noqa: E402
import test_minecraft_resident as suite  # noqa: E402


def main():
    case = suite.ResidentBase(methodName="runTest")
    case.setUp()
    try:
        resid = case.resident
        case.config["minecraft"]["write_enabled"] = True
        case.config["minecraft"]["decision_enabled"] = False
        resid.state.data["drives"]["energy"] = .1
        obs = suite.observation(food=3)
        original = resid.sidecar.request

        async def request(method, params=None, timeout=35):
            if method == "skill":
                raise SidecarError("skill_rejected", code=-32008)
            return await original(method, params, timeout)

        resid.sidecar.request = request
        before = resid.state.data["counters"]["failures"]
        outcome = asyncio.run(resid._execute(Decision("satisfy_drive"), obs))
        after = resid.state.data["counters"]["failures"]
        print("outcome =", outcome)
        print("failures_before =", before, "failures_after =", after)
        if outcome.get("reason") == "guard_busy" and after == before:
            print("sidecar busy mapped to a bounded pause")
            return 0
        print("SIDECAR_BUSY_COUNTED_AS_FAILURE: -32008 was not mapped to guard_busy")
        return 1
    finally:
        case.tearDown()


if __name__ == "__main__":
    sys.exit(main())
