import server

try:
    from test_scheduler import ApiTestCase as _Base
except ImportError:  # run as tests.test_status_board
    from tests.test_scheduler import ApiTestCase as _Base

DAY = "2026-10-27"


class StatusBoardTests(_Base):
    def test_board_splits_scheduled_pending_and_finished(self):
        status, board = self.request_json("/api/status-board")
        self.assertEqual(status, 200)
        self.assertEqual(
            board["counts"],
            {name: len(board[name]) for name in ("scheduled", "pending", "passed", "rejected")},
        )
        scheduled_before = board["counts"]["scheduled"]
        candidate = self.add_candidate("Board Test")
        status, board = self.request_json("/api/status-board")
        self.assertIn(candidate["id"], [item["id"] for item in board["pending"]])

        status, _ = self.request_json(
            f"/api/candidates/{candidate['id']}", {"date": DAY, "time": "09:00"}, "PATCH"
        )
        self.assertEqual(status, 200)
        status, board = self.request_json("/api/status-board")
        self.assertEqual(board["counts"]["scheduled"], scheduled_before + 1)
        self.assertNotIn(candidate["id"], [item["id"] for item in board["pending"]])
        row = next(item for item in board["scheduled"] if item["id"] == candidate["id"])
        self.assertEqual((row["interview_date"], row["interview_time"]), (DAY, "09:00"))
        self.assertTrue(row["interviewer"] and row["room"])

    def test_completed_candidates_move_to_finished_lists(self):
        candidate = self.add_candidate("Finisher")
        self.request_json(f"/api/candidates/{candidate['id']}", {"date": DAY, "time": "09:00"}, "PATCH")
        with server.connect_database() as connection:
            interview_id = connection.execute(
                "SELECT id FROM interviews WHERE candidate_id = ?", (candidate["id"],)
            ).fetchone()["id"]
        status, _ = self.request_json(
            f"/api/interviews/{interview_id}/evaluate",
            {"score": 1, "feedback": "Not ready.", "decision": "fail"},
            "POST",
        )
        self.assertEqual(status, 200)
        status, board = self.request_json("/api/status-board")
        self.assertIn(candidate["id"], [item["id"] for item in board["rejected"]])
        self.assertNotIn(candidate["id"], [item["id"] for item in board["scheduled"] + board["pending"]])

    def test_only_coordinators_can_read_the_board(self):
        candidate_opener = self.login_as("candidate@trialsync.local", "Candidate123!")
        interviewer_opener = self.login_as("interviewer@trialsync.local", "Interviewer123!")
        for opener in (candidate_opener, interviewer_opener):
            status, _ = self.request_json("/api/status-board", opener=opener)
            self.assertEqual(status, 403)
        status, _ = self.request_json("/api/status-board", opener=self.make_anonymous_opener())
        self.assertEqual(status, 401)

    def make_anonymous_opener(self):
        from http.cookiejar import CookieJar
        from urllib.request import HTTPCookieProcessor, build_opener
        return build_opener(HTTPCookieProcessor(CookieJar()))

    def test_status_page_is_served(self):
        from urllib.request import urlopen
        with urlopen(self.base_url + "/status.html", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Interview status board", response.read())
