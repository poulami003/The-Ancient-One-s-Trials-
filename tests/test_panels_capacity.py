"""Panel interviews and per-interviewer daily capacity, through the real database/API."""
import unittest

import server
from scheduler import find_assignments, remaining_capacity

try:
    from test_scheduler import ApiTestCase as _Base
except ImportError:  # run as tests.test_panels_capacity
    from tests.test_scheduler import ApiTestCase as _Base

DAY = "2026-10-27"  # a Tuesday with all demo calendars open


class PanelCapacityTests(_Base):
    def set_panel_size(self, round_number, size):
        status, body = self.request_json(
            "/api/interview-rounds/panel-size",
            {"round_number": round_number, "panel_size": size},
            "POST",
        )
        self.assertEqual(status, 200, body)

    def waiting_candidate(self, name, stage="Coding"):
        return self.add_candidate(name, stage)

    def options_for(self, candidate_id):
        with server.connect_database() as connection:
            candidate = connection.execute(
                f"SELECT {server.FIELDS} FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return find_assignments(connection, candidate, DAY)

    def test_default_panel_is_a_single_interviewer(self):
        candidate = self.waiting_candidate("Solo")
        options = self.options_for(candidate["id"])
        self.assertTrue(options)
        self.assertTrue(all(len(option["panel_ids"]) == 1 for option in options))

    def test_panel_options_contain_distinct_skilled_interviewers(self):
        self.set_panel_size(1, 2)  # Coding round needs two interviewers
        candidate = self.waiting_candidate("Panel One")
        options = self.options_for(candidate["id"])
        self.assertTrue(options)
        for option in options:
            self.assertEqual(len(option["panel_ids"]), 2)
            self.assertEqual(len(set(option["panel_ids"])), 2)
            self.assertEqual(option["interviewer_id"], option["panel_ids"][0])

    def test_panel_larger_than_skilled_pool_is_unschedulable(self):
        self.set_panel_size(1, 3)  # only two interviewers know Coding
        candidate = self.waiting_candidate("Too Big")
        self.assertEqual(self.options_for(candidate["id"]), [])
        status, body = self.request_json(
            f"/api/candidates/{candidate['id']}", {"date": DAY, "time": "09:00"}, "PATCH"
        )
        self.assertEqual(status, 400)

    def test_booked_panel_blocks_every_member_at_that_hour(self):
        self.set_panel_size(1, 2)
        first = self.waiting_candidate("Panel A")
        second = self.waiting_candidate("Panel B")
        status, body = self.request_json(
            f"/api/candidates/{first['id']}", {"date": DAY, "time": "09:00"}, "PATCH"
        )
        self.assertEqual(status, 200, body)
        with server.connect_database() as connection:
            panelists = connection.execute("SELECT COUNT(*) FROM interview_panelists").fetchone()[0]
        self.assertEqual(panelists, 1)  # lead on interviews row + one panelist row
        # Both Coding interviewers are now busy at 09:00 for a 2-person panel.
        self.assertFalse([o for o in self.options_for(second["id"]) if o["time"] == "09:00"])
        self.assertTrue([o for o in self.options_for(second["id"]) if o["time"] == "10:00"])

    def test_auto_plan_never_double_books_panelists(self):
        self.set_panel_size(1, 2)
        for index in range(4):
            self.waiting_candidate(f"Auto {index}")
        status, body = self.request_json("/api/auto-plan", {"date": DAY}, "POST")
        self.assertEqual(status, 200, body)
        with server.connect_database() as connection:
            rows = connection.execute(
                """SELECT i.interview_time, i.interviewer_id AS who FROM interviews i
                   WHERE i.status = 'scheduled' AND i.interview_date = ?
                   UNION ALL
                   SELECT i.interview_time, p.interviewer_id FROM interview_panelists p
                   JOIN interviews i ON i.id = p.interview_id
                   WHERE i.status = 'scheduled' AND i.interview_date = ?""",
                (DAY, DAY),
            ).fetchall()
        slots = [(row["interview_time"], row["who"]) for row in rows]
        self.assertEqual(len(slots), len(set(slots)), "an interviewer was double-booked")

    def test_panelist_can_see_and_evaluate_the_interview(self):
        self.set_panel_size(1, 2)
        candidate = self.waiting_candidate("Shared")
        status, scheduled = self.request_json(
            f"/api/candidates/{candidate['id']}", {"date": DAY, "time": "09:00"}, "PATCH"
        )
        self.assertEqual(status, 200, scheduled)
        with server.connect_database() as connection:
            interview = connection.execute(
                "SELECT id, interviewer_id FROM interviews WHERE candidate_id = ?", (candidate["id"],)
            ).fetchone()
            names = {
                row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM interviewers")
            }
            panelist = connection.execute(
                "SELECT interviewer_id FROM interview_panelists WHERE interview_id = ?", (interview["id"],)
            ).fetchone()["interviewer_id"]
        self.assertIn("Kavya Rao", {names[interview["interviewer_id"]], names[panelist]})
        kavya = self.login_as("interviewer@trialsync.local", "Interviewer123!")  # display name: Kavya Rao
        status, dashboard = self.request_json(f"/api/me/dashboard?date={DAY}", opener=kavya)
        self.assertEqual(status, 200)
        self.assertIn(candidate["id"], [row["id"] for row in dashboard["scheduled"]])
        status, result = self.request_json(
            f"/api/interviews/{interview['id']}/evaluate",
            {"score": 4, "feedback": "Solid.", "decision": "pass"},
            "POST",
            opener=kavya,
        )
        self.assertEqual(status, 200, result)

    def test_daily_capacity_is_enforced(self):
        with server.connect_database() as connection:
            connection.execute("UPDATE interviewers SET max_per_day = 1")
        first = self.waiting_candidate("Cap A")
        second = self.waiting_candidate("Cap B")
        third = self.waiting_candidate("Cap C")
        fourth = self.waiting_candidate("Cap D")
        status, body = self.request_json("/api/auto-plan", {"date": DAY}, "POST")
        self.assertEqual(status, 200, body)
        with server.connect_database() as connection:
            loads = remaining_capacity(connection, DAY)
            per_person = connection.execute(
                """SELECT interviewer_id, COUNT(*) AS n FROM interviews
                   WHERE status = 'scheduled' AND interview_date = ? GROUP BY interviewer_id""",
                (DAY,),
            ).fetchall()
            scheduled = sum(row["n"] for row in per_person)
        # Nobody may be booked more than once; remaining capacity never goes negative.
        self.assertTrue(all(row["n"] <= 1 for row in per_person))
        self.assertTrue(all(left >= 0 for left in loads.values()))
        self.assertEqual(body["scheduled"], scheduled)
        self.assertGreater(scheduled, 0)

    def test_capacity_blocks_manual_assignment_too(self):
        with server.connect_database() as connection:
            connection.execute("UPDATE interviewers SET max_per_day = 1 WHERE name = 'Kavya Rao'")
        first = self.waiting_candidate("Cap One")
        second = self.waiting_candidate("Cap Two")
        for candidate in (first, second):
            status, body = self.request_json(
                f"/api/candidates/{candidate['id']}",
                {"date": DAY, "time": "11:00", "interviewer": "Kavya Rao"},
                "PATCH",
            )
            if candidate is first:
                self.assertEqual(status, 200, body)
            else:
                self.assertEqual(status, 400, body)

    def test_capacity_and_panel_endpoints_validate_input(self):
        status, _ = self.request_json("/api/interviewers/capacity", {"interviewer_id": 1, "max_per_day": 0}, "POST")
        self.assertEqual(status, 400)
        status, _ = self.request_json("/api/interviewers/capacity", {"interviewer_id": 999, "max_per_day": 3}, "POST")
        self.assertEqual(status, 404)
        status, _ = self.request_json("/api/interview-rounds/panel-size", {"round_number": 1, "panel_size": 9}, "POST")
        self.assertEqual(status, 400)
        candidate_opener = self.login_as("candidate@trialsync.local", "Candidate123!")
        status, _ = self.request_json(
            "/api/interview-rounds/panel-size", {"round_number": 1, "panel_size": 2}, "POST", opener=candidate_opener
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
