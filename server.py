import json
import sqlite3
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "trials.db"
SAMPLE_CANDIDATES = [
    ("Yusuf Patel", "M.Tech", "Portfolio review", 0, "scheduled", "Kavya Rao", "2026-10-26", "10:00", 1, "2027"),
    ("Priya Nair", "B.Tech", "Technical screen", 0, "waiting", "", "", "", 0, "2027"),
    ("Poulami Maji", "B.Tech", "Technical screen", 1, "waiting", "", "", "", 0, "2026"),
    ("Ashwini Deshmukh", "M.Sc", "Portfolio review", 0, "waiting", "", "", "", 0, "2028"),
    ("Muskaan Kushwaha", "M.Sc", "Technical screen", 1, "waiting", "", "", "", 0, "2027"),
    ("Prachi Gupta", "MCA", "Technical screen", 0, "waiting", "", "", "", 0, "2026"),
    ("Anjali Kumari", "M.Tech", "Technical screen", 0, "waiting", "", "", "", 0, "2028"),
    ("Sejal Kesharwani", "B.Tech", "Portfolio review", 1, "waiting", "", "", "", 0, "2027"),
    ("Aanya Iyer", "M.Sc", "Portfolio review", 0, "waiting", "", "", "", 0, "2029"),
    ("Rohan Mehta", "MCA", "Technical screen", 1, "waiting", "", "", "", 0, "2026"),
    ("Isha Patel", "M.Tech", "Technical screen", 0, "waiting", "", "", "", 0, "2028"),
    ("Arjun Rao", "B.Tech", "Technical screen", 0, "waiting", "", "", "", 0, "2029"),
]
NON_INDIAN_DEMO_NAMES = ("Mira Chen", "Elena Rossi", "Theo Williams", "Hana Ito", "Amara Okafor", "Leo Fischer", "Jin Park", "Zara Ali")
FIELDS = "id, name, program, role, stage, batch, priority, status, interviewer, interview_date, interview_time, remote"


def connect_database():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with connect_database() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                program TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL,
                batch TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting'
                    CHECK (status IN ('waiting', 'scheduled')),
                interviewer TEXT NOT NULL DEFAULT '',
                interview_date TEXT NOT NULL DEFAULT '',
                interview_time TEXT NOT NULL DEFAULT '',
                remote INTEGER NOT NULL DEFAULT 0
            )"""
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(candidates)")}
        if "program" not in columns:
            connection.execute("ALTER TABLE candidates ADD COLUMN program TEXT NOT NULL DEFAULT ''")
        if "batch" not in columns:
            connection.execute("ALTER TABLE candidates ADD COLUMN batch TEXT NOT NULL DEFAULT ''")
        connection.executemany(
            "DELETE FROM candidates WHERE name = ?",
            ((name,) for name in NON_INDIAN_DEMO_NAMES),
        )
        connection.execute("UPDATE candidates SET program = role WHERE program = ''")
        connection.execute(
            "UPDATE candidates SET interviewer = ? WHERE name = ? AND interviewer = ?",
            ("Kavya Rao", "Yusuf Patel", "Sofia Ramirez"),
        )
        connection.executemany(
            "UPDATE candidates SET batch = ? WHERE name = ? AND batch = ''",
            ((candidate[9], candidate[0]) for candidate in SAMPLE_CANDIDATES),
        )
        connection.executemany(
            "UPDATE candidates SET role = ?, program = ? WHERE name = ? AND program = role",
            ((candidate[1], candidate[1], candidate[0]) for candidate in SAMPLE_CANDIDATES),
        )
        existing_names = {row[0] for row in connection.execute("SELECT name FROM candidates")}
        new_candidates = [candidate for candidate in SAMPLE_CANDIDATES if candidate[0] not in existing_names]
        if new_candidates:
            connection.executemany(
                """INSERT INTO candidates
                    (name, role, program, stage, batch, priority, status, interviewer,
                     interview_date, interview_time, remote)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(candidate[0], candidate[1], candidate[1], candidate[2], candidate[9], *candidate[3:9]) for candidate in new_candidates],
            )


class TrialsHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("Request body is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        request = urlparse(self.path)
        if request.path == "/api/health":
            return self.send_json(200, {"status": "ok"})
        if request.path == "/api/candidates":
            target_date = parse_qs(request.query).get("date", [date.today().isoformat()])[0]
            with connect_database() as connection:
                scheduled = connection.execute(
                    f"SELECT {FIELDS} FROM candidates WHERE status = 'scheduled' AND interview_date = ? ORDER BY interview_time",
                    (target_date,),
                ).fetchall()
                waiting = connection.execute(
                    f"SELECT {FIELDS} FROM candidates WHERE status = 'waiting' ORDER BY priority DESC, id"
                ).fetchall()
            return self.send_json(200, {
                "date": target_date,
                "scheduled": [dict(row) for row in scheduled],
                "queue": [dict(row) for row in waiting],
            })
        if request.path == "/":
            self.path = "/home.html"
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/candidates":
            return self.send_json(404, {"error": "Endpoint not found."})
        try:
            body = self.read_json()
            name = str(body.get("name", "")).strip()
            program = str(body.get("program", body.get("role", ""))).strip()
            batch = str(body.get("batch", "")).strip()
            stage = str(body.get("stage", "Technical screen")).strip()
            if not name or not program or not batch or not stage:
                return self.send_json(400, {"error": "Name, degree program, batch, and stage are required."})
            with connect_database() as connection:
                cursor = connection.execute(
                    "INSERT INTO candidates (name, role, program, stage, batch, priority) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, program, program, stage, batch, int(bool(body.get("priority", False)))),
                )
                candidate = connection.execute(
                    f"SELECT {FIELDS} FROM candidates WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            return self.send_json(201, dict(candidate))
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json(400, {"error": str(error)})

    def do_PATCH(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "candidates"]:
            return self.send_json(404, {"error": "Endpoint not found."})
        try:
            candidate_id = int(parts[2])
            body = self.read_json()
            interview_date = str(body.get("date", ""))
            interview_time = str(body.get("time", ""))
            interviewer = str(body.get("interviewer", "")).strip()
            if body.get("status") != "scheduled" or not interview_date or not interview_time or not interviewer:
                return self.send_json(400, {"error": "A date, time, interviewer, and scheduled status are required."})
            date.fromisoformat(interview_date)
            if interview_time not in {f"{hour:02d}:00" for hour in range(9, 17)}:
                return self.send_json(400, {"error": "Choose an available hour between 09:00 and 16:00."})
            with connect_database() as connection:
                occupied = connection.execute(
                    "SELECT id FROM candidates WHERE status = 'scheduled' AND interview_date = ? AND interview_time = ? AND id != ?",
                    (interview_date, interview_time, candidate_id),
                ).fetchone()
                if occupied:
                    return self.send_json(409, {"error": "That time slot is already occupied."})
                cursor = connection.execute(
                    """UPDATE candidates SET status = 'scheduled', interview_date = ?,
                       interview_time = ?, interviewer = ?, remote = ?
                       WHERE id = ? AND status = 'waiting'""",
                    (interview_date, interview_time, interviewer, int(bool(body.get("remote", False))), candidate_id),
                )
                if cursor.rowcount == 0:
                    exists = connection.execute("SELECT id FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
                    if not exists:
                        return self.send_json(404, {"error": "Candidate not found."})
                    return self.send_json(409, {"error": "Candidate is not waiting to be scheduled."})
                candidate = connection.execute(
                    f"SELECT {FIELDS} FROM candidates WHERE id = ?", (candidate_id,)
                ).fetchone()
            return self.send_json(200, dict(candidate))
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), TrialsHandler)
    print("Trials is running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()