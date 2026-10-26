import json
import sqlite3
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from pathlib import Path

import server


class QuietTrialsHandler(server.TrialsHandler):
    def log_message(self, format_string, *args):
        return


class ApiTestCase(unittest.TestCase):
    """Shared setup: temp database, live HTTP server, coordinator login, request helpers."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database = server.DATABASE
        server.DATABASE = Path(self.temporary_directory.name) / "test.db"
        server.initialize_database()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietTrialsHandler)
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = self.login_as("coordinator@trialsync.local", "Coordinator123!")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(timeout=2)
        server.DATABASE = self.original_database
        self.temporary_directory.cleanup()

    def login_as(self, email, password):
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        request = Request(
            self.base_url + "/api/auth/login",
            data=json.dumps({"email": email, "password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            json.loads(response.read())
        return opener

    def request_json(self, path, payload=None, method="GET", opener=None):
        opener = opener or self.opener
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def add_candidate(self, name, stage="Coding"):
        status, candidate = self.request_json(
            "/api/candidates",
            {
                "name": name,
                "program": "B.Tech",
                "batch": "2027",
                "stage": stage,
            },
            "POST",
        )
        self.assertEqual(status, 201)
        return candidate


class SchedulerApiTests(ApiTestCase):
    def test_auto_plan_respects_interviewer_and_room_conflicts(self):
        with server.connect_database() as connection:
            interviewers = connection.execute("SELECT id FROM interviewers").fetchall()
            rooms = connection.execute("SELECT id FROM rooms").fetchall()
            connection.execute(
                """UPDATE resource_availability SET available = 0
                   WHERE weekday = 0 AND interview_time = '09:00'"""
            )
            connection.execute(
                """UPDATE resource_availability SET available = 1
                   WHERE resource_type = 'interviewer' AND resource_id = ?
                     AND weekday = 0 AND interview_time = '09:00'""",
                (interviewers[0]["id"],),
            )
            connection.execute(
                """UPDATE resource_availability SET available = 1
                   WHERE resource_type = 'room' AND resource_id = ?
                     AND weekday = 0 AND interview_time = '09:00'""",
                (rooms[0]["id"],),
            )

        first_candidate = self.add_candidate("Candidate One")
        second_candidate = self.add_candidate("Candidate Two")
        status, first = self.request_json(
            f"/api/candidates/{first_candidate['id']}",
            {"date": "2026-10-26", "time": "09:00"},
            "PATCH",
        )
        self.assertEqual(status, 200)
        status, second = self.request_json(
            f"/api/candidates/{second_candidate['id']}",
            {"date": "2026-10-26", "time": "09:00"},
            "PATCH",
        )
        self.assertEqual(status, 400)
        self.assertIn("No conflict-free slot", second["error"])
        with server.connect_database() as connection:
            scheduled = connection.execute(
                """SELECT interviewer_id, room_id, interview_time FROM candidates
                   WHERE id IN (?, ?) AND status = 'scheduled'""",
                (first_candidate["id"], second_candidate["id"]),
            ).fetchall()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["interviewer_id"], first["interviewer_id"])
        self.assertEqual(scheduled[0]["room_id"], first["room_id"])

    def test_booking_evaluation_advances_round_and_records_outcome(self):
        candidate = self.add_candidate("Pipeline Candidate")
        status, link = self.request_json(
            f"/api/candidates/{candidate['id']}/booking-link", {}, "POST"
        )
        self.assertEqual(status, 201)
        status, _ = self.request_json(
            f"/api/booking/{link['token']}/availability",
            {"date": "2026-10-26", "times": ["09:00", "10:00"]},
            "POST",
        )
        self.assertEqual(status, 200)
        status, slots = self.request_json(
            f"/api/booking/{link['token']}?date=2026-10-26"
        )
        self.assertEqual(status, 200)
        self.assertTrue(slots["available"])
        self.assertLessEqual({slot["time"] for slot in slots["available"]}, {"09:00", "10:00"})

        status, booked = self.request_json(
            f"/api/booking/{link['token']}",
            {"date": "2026-10-26", "time": slots["available"][0]["time"]},
            "POST",
        )
        self.assertEqual(status, 200)
        self.assertEqual(booked["status"], "scheduled")
        interview_id = booked["interview_id"]

        status, progressed = self.request_json(
            f"/api/interviews/{interview_id}/evaluate",
            {"score": 5, "feedback": "Clear, correct solution.", "decision": "pass"},
            "POST",
        )
        self.assertEqual(status, 200)
        self.assertEqual(progressed["stage"], "Technical")
        self.assertEqual(progressed["current_round"], 2)
        self.assertEqual(progressed["status"], "waiting")

        status, result = self.request_json(
            "/api/auto-plan", {"date": "2026-10-26"}, "POST"
        )
        self.assertEqual(status, 200)
        status, day = self.request_json("/api/candidates?date=2026-10-26")
        self.assertEqual(status, 200)
        next_interview = next(
            event for event in day["scheduled"] if event["id"] == candidate["id"]
        )
        status, rejected = self.request_json(
            f"/api/interviews/{next_interview['interview_id']}/evaluate",
            {"score": 2, "feedback": "Needs more depth.", "decision": "fail"},
            "POST",
        )
        self.assertEqual(status, 200)
        self.assertEqual(rejected["outcome"], "rejected")
        status, rankings = self.request_json("/api/rankings")
        self.assertEqual(status, 200)
        ranked = next(row for row in rankings["rankings"] if row["id"] == candidate["id"])
        self.assertEqual(ranked["average_score"], 3.5)

    def test_authentication_and_role_permissions(self):
        anonymous = build_opener(HTTPCookieProcessor(CookieJar()))
        status, _ = self.request_json(
            "/api/auth/login",
            {"email": "coordinator@trialsync.local", "password": "not-the-password"},
            "POST",
            anonymous,
        )
        self.assertEqual(status, 401)
        status, result = self.request_json(
            "/api/candidates?date=2026-10-26", opener=anonymous
        )
        self.assertEqual(status, 401)
        self.assertEqual(result["code"], "authentication_required")

        candidate = self.login_as("candidate@trialsync.local", "Candidate123!")
        status, result = self.request_json("/api/me/dashboard", opener=candidate)
        self.assertEqual(status, 200)
        self.assertEqual(result["candidate"]["name"], "Poulami Maji")
        self.assertEqual(result["user"]["role"], "candidate")

        status, result = self.request_json(
            "/api/candidates",
            {"name": "Unauthorized", "program": "B.Tech", "batch": "2027", "stage": "Coding"},
            "POST",
            candidate,
        )
        self.assertEqual(status, 403)

        interviewer = self.login_as("interviewer@trialsync.local", "Interviewer123!")
        status, result = self.request_json(
            "/api/candidates?date=2026-10-26", opener=interviewer
        )
        self.assertEqual(status, 403)
        status, result = self.request_json(
            "/api/me/dashboard?date=2026-10-26", opener=interviewer
        )
        self.assertEqual(status, 200)
        self.assertTrue(all(item["interviewer"] == "Kavya Rao" for item in result["scheduled"]))

        status, _ = self.request_json("/api/auth/logout", {}, "POST", candidate)
        self.assertEqual(status, 200)
        status, result = self.request_json("/api/me/dashboard", opener=candidate)
        self.assertEqual(status, 401)

    def test_login_page_is_default_and_private_files_are_not_served(self):
        with self.opener.open(self.base_url + "/", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Welcome back", response.read().decode("utf-8"))
        for path in ("/trials.db", "/server.py", "/tests/test_scheduler.py"):
            request = Request(self.base_url + path)
            with self.assertRaises(HTTPError) as context:
                self.opener.open(request, timeout=5)
            self.assertEqual(context.exception.code, 404)
            context.exception.close()

    def test_database_has_related_round_resource_tables_and_rejects_unknown_rounds(self):
        with server.connect_database() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"candidates", "interviewers", "rooms", "interview_rounds", "interviews"}.issubset(tables)
            )
            rounds = connection.execute(
                "SELECT round_number, name FROM interview_rounds ORDER BY round_number"
            ).fetchall()
            self.assertEqual(
                [(row["round_number"], row["name"]) for row in rounds],
                [(1, "Coding"), (2, "Technical"), (3, "HR")],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO interviews
                       (candidate_id, round_number, stage, interview_date, interview_time,
                        interviewer_id, room_id)
                       VALUES (1, 99, 'Unknown', '2026-10-26', '09:00', 1, 1)"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO candidates
                       (name, role, program, stage, batch, current_round)
                       VALUES ('Invalid Round Candidate', 'B.Tech', 'B.Tech', 'Unknown', '2027', 99)"""
                )

        status, response = self.request_json("/api/interview-rounds")
        self.assertEqual(status, 200)
        self.assertEqual(
            [row["name"] for row in response["rounds"]],
            ["Coding", "Technical", "HR"],
        )

    def test_schema_upgrade_preserves_existing_candidate_and_schedule(self):
        database_path = self.temporary_directory.name + "\\legacy.db"
        original_database = server.DATABASE
        try:
            server.DATABASE = Path(database_path)
            connection = sqlite3.connect(server.DATABASE)
            try:
                connection.execute(
                    """CREATE TABLE candidates (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       name TEXT NOT NULL,
                       role TEXT NOT NULL DEFAULT '',
                       stage TEXT NOT NULL,
                       priority INTEGER NOT NULL DEFAULT 0,
                       status TEXT NOT NULL DEFAULT 'waiting',
                       interviewer TEXT NOT NULL DEFAULT '',
                       interview_date TEXT NOT NULL DEFAULT '',
                       interview_time TEXT NOT NULL DEFAULT '',
                       remote INTEGER NOT NULL DEFAULT 0,
                       batch TEXT NOT NULL DEFAULT '',
                       program TEXT NOT NULL DEFAULT ''
                    )"""
                )
                connection.execute(
                    """INSERT INTO candidates
                       (id, name, role, stage, priority, status, interviewer,
                        interview_date, interview_time, remote, batch, program)
                       VALUES (701, 'Existing Candidate', 'B.Tech', 'Technical screen', 1,
                               'scheduled', 'Kavya Rao', '2026-10-26', '10:00', 1, '2027', 'B.Tech')"""
                )
                connection.execute(
                    "CREATE TABLE interviewers (id INTEGER PRIMARY KEY, name TEXT UNIQUE, skills TEXT)"
                )
                connection.execute(
                    "INSERT INTO interviewers (id, name, skills) VALUES (1, 'Kavya Rao', '[]')"
                )
                connection.execute(
                    "CREATE TABLE rooms (id INTEGER PRIMARY KEY, name TEXT UNIQUE)"
                )
                connection.execute(
                    "INSERT INTO rooms (id, name) VALUES (1, 'Interview suite')"
                )
                connection.execute(
                    """CREATE TABLE interviews (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       candidate_id INTEGER NOT NULL,
                       round_number INTEGER NOT NULL,
                       stage TEXT NOT NULL,
                       interview_date TEXT NOT NULL,
                       interview_time TEXT NOT NULL,
                       interviewer_id INTEGER NOT NULL,
                       room_id INTEGER NOT NULL,
                       status TEXT NOT NULL DEFAULT 'scheduled',
                       UNIQUE(candidate_id, round_number)
                    )"""
                )
                connection.execute(
                    """INSERT INTO interviews
                       (id, candidate_id, round_number, stage, interview_date,
                        interview_time, interviewer_id, room_id)
                       VALUES (801, 701, 1, 'Technical screen', '2026-10-26',
                               '10:00', 1, 1)"""
                )
                connection.commit()
            finally:
                connection.close()

            server.initialize_database()
            with server.connect_database() as connection:
                candidate = connection.execute(
                    "SELECT id, name, status, interview_time FROM candidates WHERE id = 701"
                ).fetchone()
                migrated_interview = connection.execute(
                    """SELECT round_number, interview_time FROM interviews
                       WHERE candidate_id = 701"""
                ).fetchone()
                self.assertEqual(candidate["name"], "Existing Candidate")
                self.assertEqual(candidate["status"], "scheduled")
                self.assertEqual(candidate["interview_time"], "10:00")
                self.assertEqual(migrated_interview["round_number"], 1)
                self.assertEqual(migrated_interview["interview_time"], "10:00")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """INSERT INTO interviews
                           (candidate_id, round_number, stage, interview_date,
                            interview_time, interviewer_id, room_id)
                           VALUES (701, 99, 'Invalid', '2026-10-26', '11:00', 1, 1)"""
                    )

            server.initialize_database()
            with server.connect_database() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM candidates WHERE id = 701"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM interviews WHERE candidate_id = 701"
                    ).fetchone()[0],
                    1,
                )
        finally:
            server.DATABASE = original_database

    def test_candidate_must_mark_availability_before_booking(self):
        candidate = self.login_as("candidate@trialsync.local", "Candidate123!")
        status, result = self.request_json(
            "/api/me/booking-options?date=2026-10-26", opener=candidate
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["available"], [])

        status, result = self.request_json(
            "/api/me/availability",
            {"date": "2026-10-26", "times": ["09:00", "11:00"]},
            "POST",
            candidate,
        )
        self.assertEqual(status, 200)
        status, result = self.request_json(
            "/api/me/booking-options?date=2026-10-26", opener=candidate
        )
        self.assertTrue(result["available"])
        self.assertLessEqual(
            {slot["time"] for slot in result["available"]}, {"09:00", "11:00"}
        )
        status, result = self.request_json(
            "/api/me/booking",
            {"date": "2026-10-26", "time": "10:00"},
            "POST",
            candidate,
        )
        self.assertEqual(status, 400)
        self.assertIn("mark this hour as available", result["error"])


if __name__ == "__main__":
    unittest.main()
