from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.service import SESSIONS


client = TestClient(app)


def fastest_vs_detour_payload(budget: int) -> dict:
    return {
        "points": [
            "start",
            "hall",
            "floor2",
            "shuttle",
            "ramp1",
            "ramp2",
            "park",
        ],
        "start_point": "start",
        "destination_point": "shuttle",
        "available_minutes": budget,
        "segments": [
            {
                "segment_id": "start-to-hall",
                "from_point": "start",
                "to_point": "hall",
                "minutes": 2,
                "direction": "one_way",
            },
            {
                "segment_id": "fast-elevator",
                "from_point": "hall",
                "to_point": "floor2",
                "minutes": 1,
                "direction": "one_way",
                "elevator_id": "1",
            },
            {
                "segment_id": "floor2-to-shuttle",
                "from_point": "floor2",
                "to_point": "shuttle",
                "minutes": 2,
                "direction": "one_way",
            },
            {
                "segment_id": "detour-1",
                "from_point": "hall",
                "to_point": "ramp1",
                "minutes": 5,
                "direction": "one_way",
            },
            {
                "segment_id": "detour-2",
                "from_point": "ramp1",
                "to_point": "ramp2",
                "minutes": 4,
                "direction": "one_way",
            },
            {
                "segment_id": "detour-3",
                "from_point": "ramp2",
                "to_point": "shuttle",
                "minutes": 3,
                "direction": "one_way",
            },
            {
                "segment_id": "safe-1",
                "from_point": "start",
                "to_point": "park",
                "minutes": 4,
                "direction": "two_way",
            },
            {
                "segment_id": "safe-2",
                "from_point": "park",
                "to_point": "shuttle",
                "minutes": 5,
                "direction": "two_way",
            },
        ],
    }


def segmented_elevator_payload() -> dict:
    return {
        "points": ["start", "e-hall", "l2", "l3", "shuttle", "ramp"],
        "start_point": "start",
        "destination_point": "shuttle",
        "available_minutes": 9,
        "segments": [
            {
                "segment_id": "walk-to-e1",
                "from_point": "start",
                "to_point": "e-hall",
                "minutes": 1,
                "direction": "one_way",
            },
            {
                "segment_id": "e1-first-segment",
                "from_point": "e-hall",
                "to_point": "l2",
                "minutes": 2,
                "direction": "two_way",
                "elevator_id": "1",
            },
            {
                "segment_id": "e1-second-segment",
                "from_point": "l2",
                "to_point": "l3",
                "minutes": 2,
                "direction": "two_way",
                "elevator_id": "1",
            },
            {
                "segment_id": "last-walk",
                "from_point": "l3",
                "to_point": "shuttle",
                "minutes": 1,
                "direction": "one_way",
            },
            {
                "segment_id": "outage-detour-1",
                "from_point": "e-hall",
                "to_point": "ramp",
                "minutes": 4,
                "direction": "one_way",
            },
            {
                "segment_id": "outage-detour-2",
                "from_point": "ramp",
                "to_point": "shuttle",
                "minutes": 4,
                "direction": "one_way",
            },
        ],
    }


class GuidanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        SESSIONS.clear()

    def test_docs_and_health(self) -> None:
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        docs = client.get("/docs")
        self.assertEqual(docs.status_code, 200)
        self.assertIn("text/html", docs.headers["content-type"])

        openapi = client.get("/openapi.json")
        self.assertEqual(openapi.status_code, 200)
        self.assertIn("轮椅访客", openapi.json()["info"]["title"])

    def test_fastest_route_with_insufficient_detour_is_rejected(self) -> None:
        response = client.post(
            "/api/guidance/plan", json=fastest_vs_detour_payload(8)
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertFalse(body["guaranteed"])
        self.assertIsNone(body["session_id"])
        self.assertEqual(body["status"], "not_started")
        self.assertEqual(body["reason_code"], "NO_GUARANTEED_ROUTE")
        self.assertEqual(body["fastest_minutes"], 5)
        self.assertEqual(
            body["fastest_path_points"],
            ["start", "hall", "floor2", "shuttle"],
        )
        self.assertIn("无法保证", body["message"])

        # A slightly larger budget admits the 9-minute elevator-free safe route.
        response = client.post(
            "/api/guidance/plan", json=fastest_vs_detour_payload(10)
        )
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()
        self.assertTrue(plan["guaranteed"])
        self.assertEqual(plan["nominal_minutes"], 9)
        self.assertEqual(plan["path_points"], ["start", "park", "shuttle"])
        self.assertEqual(plan["contingencies"], [])
        self.assertIsNotNone(plan["session_id"])

    def test_same_elevator_can_be_segmented_and_cannot_be_reported_after_ride(
        self,
    ) -> None:
        plan_response = client.post(
            "/api/guidance/plan", json=segmented_elevator_payload()
        )
        self.assertEqual(plan_response.status_code, 200, plan_response.text)
        plan = plan_response.json()
        self.assertTrue(plan["guaranteed"], plan)
        self.assertEqual(plan["nominal_minutes"], 6)
        self.assertEqual(len(plan["contingencies"]), 1)
        self.assertEqual(plan["contingencies"][0]["elevator_id"], "1")
        self.assertEqual(plan["contingencies"][0]["fallback_minutes"], 8)

        session_id = plan["session_id"]

        first = client.post(f"/api/sessions/{session_id}/confirm", json={})
        self.assertEqual(first.status_code, 200, first.text)
        step = first.json()["step"]
        self.assertEqual(step["current_point"], "e-hall")
        self.assertEqual(step["remaining_minutes"], 8)
        self.assertEqual(step["next_segment"]["elevator_id"], "1")
        self.assertTrue(step["can_report_outage"])

        ride_first = client.post(f"/api/sessions/{session_id}/confirm", json={})
        self.assertEqual(ride_first.status_code, 200, ride_first.text)
        state = ride_first.json()
        self.assertEqual(state["current_point"], "l2")
        self.assertEqual(state["remaining_minutes"], 6)
        self.assertEqual(state["step"]["next_segment"]["segment_id"], "e1-second-segment")
        self.assertFalse(state["step"]["can_report_outage"])
        self.assertIn("同梯分段", state["step"]["instruction"])

        outage = client.post(
            f"/api/sessions/{session_id}/outage", json={"elevator_id": "1"}
        )
        self.assertEqual(outage.status_code, 409)
        detail = outage.json()["detail"]
        self.assertEqual(detail["code"], "ELEVATOR_ALREADY_USED")
        self.assertIn("不能再报告", detail["message"])

        # The rejected report must not move the visitor or deduct minutes.
        current = client.get(f"/api/sessions/{session_id}").json()
        self.assertEqual(current["current_point"], "l2")
        self.assertEqual(current["remaining_minutes"], 6)

        second_ride = client.post(f"/api/sessions/{session_id}/confirm", json={})
        self.assertEqual(second_ride.status_code, 200, second_ride.text)
        self.assertEqual(second_ride.json()["current_point"], "l3")
        self.assertEqual(second_ride.json()["remaining_minutes"], 4)

        finish = client.post(f"/api/sessions/{session_id}/confirm", json={})
        self.assertEqual(finish.status_code, 200, finish.text)
        finished = finish.json()
        self.assertEqual(finished["status"], "arrived")
        self.assertEqual(finished["current_point"], "shuttle")
        self.assertEqual(finished["remaining_minutes"], 3)
        self.assertIsNone(finished["step"]["next_segment"])

    def test_outage_at_first_boarding_keeps_position_and_elevator_time(self) -> None:
        plan = client.post(
            "/api/guidance/plan", json=segmented_elevator_payload()
        ).json()
        session_id = plan["session_id"]

        client.post(f"/api/sessions/{session_id}/confirm", json={})
        outage = client.post(f"/api/sessions/{session_id}/outage", json={})
        self.assertEqual(outage.status_code, 200, outage.text)
        state = outage.json()

        self.assertEqual(state["current_point"], "e-hall")
        self.assertEqual(state["remaining_minutes"], 8)
        self.assertEqual(state["reported_outage_elevator_id"], "1")
        self.assertEqual(state["route_points"], ["e-hall", "ramp", "shuttle"])
        self.assertTrue(
            all(segment["elevator_id"] is None for segment in state["route_segments"])
        )
        self.assertIn("未扣除", state["message"])
        self.assertFalse(state["step"]["can_report_outage"])

        first_detour = client.post(f"/api/sessions/{session_id}/confirm", json={})
        self.assertEqual(first_detour.json()["current_point"], "ramp")
        self.assertEqual(first_detour.json()["remaining_minutes"], 4)

        second_detour = client.post(f"/api/sessions/{session_id}/confirm", json={})
        self.assertEqual(second_detour.status_code, 200, second_detour.text)
        self.assertEqual(second_detour.json()["status"], "arrived")
        self.assertEqual(second_detour.json()["remaining_minutes"], 0)

    def test_limits_are_enforced(self) -> None:
        too_many_points = fastest_vs_detour_payload(10)
        existing = set(too_many_points["points"])
        for index in range(40 - len(existing) + 1):
            too_many_points["points"].append(f"extra-{index}")
        response = client.post("/api/guidance/plan", json=too_many_points)
        self.assertEqual(response.status_code, 422)
        self.assertIn("40", response.text)

        nine_elevators = {
            "points": ["a", "b"],
            "start_point": "a",
            "destination_point": "b",
            "available_minutes": 100,
            "segments": [
                {
                    "segment_id": f"e{i}",
                    "from_point": "a",
                    "to_point": "b",
                    "minutes": 1,
                    "direction": "one_way",
                    "elevator_id": str(i),
                }
                for i in range(9)
            ],
        }
        response = client.post("/api/guidance/plan", json=nine_elevators)
        self.assertEqual(response.status_code, 422)
        self.assertIn("8", response.text)

    def test_exact_point_and_elevator_limits_are_accepted(self) -> None:
        points = [f"p{i}" for i in range(40)]
        segments = []
        for i in range(39):
            segments.append(
                {
                    "segment_id": f"line-{i}",
                    "from_point": f"p{i}",
                    "to_point": f"p{i + 1}",
                    "minutes": 1,
                    "direction": "one_way",
                    **({"elevator_id": str(i + 1)} if i < 8 else {}),
                }
            )
        for i in range(8):
            segments.append(
                {
                    "segment_id": f"bypass-{i}",
                    "from_point": f"p{i}",
                    "to_point": f"p{i + 1}",
                    "minutes": 2,
                    "direction": "one_way",
                }
            )

        response = client.post(
            "/api/guidance/plan",
            json={
                "points": points,
                "segments": segments,
                "start_point": "p0",
                "destination_point": "p39",
                "available_minutes": 100,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["guaranteed"], body)
        self.assertLessEqual(len(body["contingencies"]), 8)

    def test_unknown_session_returns_404(self) -> None:
        response = client.get("/api/sessions/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "SESSION_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
