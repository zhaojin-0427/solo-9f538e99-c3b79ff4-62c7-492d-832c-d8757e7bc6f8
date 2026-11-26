"""Print the two requested verification scenarios using FastAPI's TestClient.

Run after installing requirements-dev.txt:

    python scripts/demo_scenarios.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def load_example(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def main() -> None:
    print("== 1. Fastest route cannot absorb the outage detour ==")
    response = client.post(
        "/api/guidance/plan",
        json=load_example("01-fastest-route-detour-too-slow.json"),
    )
    body = response.json()
    print("status:", response.status_code)
    print(
        {
            key: body.get(key)
            for key in (
                "guaranteed",
                "status",
                "reason_code",
                "session_id",
                "fastest_minutes",
                "fastest_path_points",
            )
        }
    )
    print(body["message"])

    safe = client.post(
        "/api/guidance/plan", json=load_example("02-safe-route-instead.json")
    ).json()
    print(
        "budget=10 alternative:",
        {
            key: safe.get(key)
            for key in ("guaranteed", "nominal_minutes", "path_points", "session_id")
        },
    )

    print("\n== 2. Same elevator used in two segmented rides ==")
    plan = client.post(
        "/api/guidance/plan", json=load_example("03-same-elevator-segmented.json")
    ).json()
    session_id = plan["session_id"]
    print(
        "plan:",
        plan["guaranteed"],
        plan["nominal_minutes"],
        [(item["elevator_id"], item["fallback_minutes"]) for item in plan["contingencies"]],
    )

    for label in ("confirm walk", "confirm first elevator segment"):
        state = client.post(f"/api/sessions/{session_id}/confirm", json={}).json()
        step = state["step"]
        print(
            label,
            "| at:",
            step["current_point"],
            "| remaining:",
            step["remaining_minutes"],
            "| next:",
            (step["next_segment"] or {}).get("segment_id"),
            "| can_report_outage:",
            step["can_report_outage"],
        )

    rejected = client.post(f"/api/sessions/{session_id}/outage", json={})
    print("outage after ride:", rejected.status_code, rejected.json()["detail"]["code"])

    for label in ("confirm second elevator segment", "confirm final walk"):
        state = client.post(f"/api/sessions/{session_id}/confirm", json={}).json()
        step = state["step"]
        print(
            label,
            "| status:",
            state["status"],
            "| at:",
            step["current_point"],
            "| remaining:",
            step["remaining_minutes"],
        )

    print("\n== 3. Outage before first ride does not deduct elevator time ==")
    outage_plan = client.post(
        "/api/guidance/plan", json=load_example("03-same-elevator-segmented.json")
    ).json()
    outage_sid = outage_plan["session_id"]
    client.post(f"/api/sessions/{outage_sid}/confirm", json={})
    outage = client.post(f"/api/sessions/{outage_sid}/outage", json={}).json()
    print(
        "after outage | at:",
        outage["current_point"],
        "| remaining:",
        outage["remaining_minutes"],
        "| route:",
        outage["route_points"],
    )
    final_state = None
    for _ in range(2):
        final_response = client.post(f"/api/sessions/{outage_sid}/confirm", json={})
        final_state = final_response.json()
    print(
        "after detour | status:",
        final_state["status"],
        "| at:",
        final_state["current_point"],
        "| remaining:",
        final_state["remaining_minutes"],
    )


if __name__ == "__main__":
    main()
