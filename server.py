import json
import hashlib
import hmac
import secrets
import sqlite3
import os
import re
import time
from contextlib import contextmanager
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from scheduler import (
    DEFAULT_MAX_PER_DAY, TIMES, candidate_is_available, find_assignments,
    remaining_capacity, solve,
)


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "trials.db"
SESSION_SECRET = os.environ.get("TRIALSYNC_SESSION_SECRET")
if not SESSION_SECRET:
    # Keep a stable local key so sessions survive a server restart (demo convenience).
    _secret_file = ROOT / ".session_secret"
    if _secret_file.exists():
        SESSION_SECRET = _secret_file.read_text().strip()
    if not SESSION_SECRET:
        SESSION_SECRET = secrets.token_hex(32)
        _secret_file.write_text(SESSION_SECRET)
SESSION_MAX_AGE = 60 * 60 * 8
PASSWORD_ITERATIONS = 310_000
ROUND_DEFINITIONS = (
    (1, "Coding", "Programming and problem-solving assessment"),
    (2, "Technical", "Technical knowledge and project discussion"),
    (3, "HR", "Communication, culture, and role-fit discussion"),
)
DEMO_USERS = (
    ("coordinator@trialsync.local", "Coordinator123!", "coordinator", "Aarav Mehta"),
    ("interviewer@trialsync.local", "Interviewer123!", "interviewer", "Kavya Rao"),
    ("candidate@trialsync.local", "Candidate123!", "candidate", "Poulami Maji"),
)
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
FIELDS = (
    "id, name, program, role, stage, batch, priority, status, interviewer, "
    "interview_date, interview_time, remote, current_round, outcome, booking_token"
)
CANDIDATE_FIELDS = ", ".join(
    f"c.{field.strip()} AS {field.strip()}" for field in FIELDS.split(",")
)
DEFAULT_INTERVIEWERS = (
    ("Aarav Mehta", ("Coding", "Technical", "Technical screen", "Team conversation", "Final interview", "HR")),
    ("Kavya Rao", ("Coding", "Technical", "Technical screen", "Portfolio review")),
    ("Ananya Desai", ("HR", "Final interview", "Team conversation", "Portfolio review")),
)
ROOMS = ("Interview suite", "Room 2", "Room 3")


INTERVIEWER_PASSWORD = "Interviewer123!"
CANDIDATE_PASSWORD = "Candidate123!"


def _email_slug(name):
    parts = "".join(ch if ch.isalnum() else " " for ch in name.lower()).split()
    return ".".join(parts) or "user"


def _insert_user(connection, email, password, role, display_name, candidate_id=None):
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    connection.execute(
        """INSERT INTO users (email, password_hash, password_salt, role, display_name, candidate_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (email, password_hash.hex(), salt.hex(), role, display_name, candidate_id),
    )


def seed_role_accounts(connection):
    """Give every interviewer and candidate row in the database a login (idempotent)."""
    for row in connection.execute("SELECT id, name FROM interviewers").fetchall():
        exists = connection.execute(
            "SELECT 1 FROM users WHERE role = 'interviewer' AND display_name = ?", (row["name"],)
        ).fetchone()
        if not exists:
            email = f"{_email_slug(row['name'])}@trialsync.local"
            if connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                email = f"{_email_slug(row['name'])}.{row['id']}@trialsync.local"
            _insert_user(connection, email, INTERVIEWER_PASSWORD, "interviewer", row["name"])
    for row in connection.execute("SELECT id, name FROM candidates").fetchall():
        exists = connection.execute(
            "SELECT 1 FROM users WHERE candidate_id = ?", (row["id"],)
        ).fetchone()
        if not exists:
            email = f"{_email_slug(row['name'])}@trialsync.local"
            if connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                email = f"{_email_slug(row['name'])}.{row['id']}@trialsync.local"
            _insert_user(connection, email, CANDIDATE_PASSWORD, "candidate", row["name"], row["id"])


@contextmanager
def connect_database():
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_rounds(connection, active_only=False):
    query = "SELECT round_number, name, description, active, panel_size FROM interview_rounds"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY round_number"
    return connection.execute(query).fetchall()


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
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(candidates)")}
        additions = {
            "program": "TEXT NOT NULL DEFAULT ''",
            "batch": "TEXT NOT NULL DEFAULT ''",
            "current_round": "INTEGER NOT NULL DEFAULT 1",
            "outcome": "TEXT NOT NULL DEFAULT 'active'",
            "booking_token": "TEXT NOT NULL DEFAULT ''",
            "interviewer_id": "INTEGER NOT NULL DEFAULT 0",
            "room_id": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in additions.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE candidates ADD COLUMN {column} {definition}")

        connection.executescript(
            """CREATE TABLE IF NOT EXISTS interviewers (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL UNIQUE,
                   skills TEXT NOT NULL DEFAULT '[]'
               );
               CREATE TABLE IF NOT EXISTS users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                   password_hash TEXT NOT NULL,
                   password_salt TEXT NOT NULL,
                   role TEXT NOT NULL CHECK (role IN ('coordinator', 'interviewer', 'candidate')),
                   display_name TEXT NOT NULL,
                   candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL
               );
               CREATE TABLE IF NOT EXISTS user_sessions (
                   token_hash TEXT PRIMARY KEY,
                   user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   expires_at INTEGER NOT NULL
               );
               CREATE TABLE IF NOT EXISTS rooms (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL UNIQUE
               );
               CREATE TABLE IF NOT EXISTS interview_rounds (
                   round_number INTEGER PRIMARY KEY CHECK (round_number > 0),
                   name TEXT NOT NULL UNIQUE,
                   description TEXT NOT NULL DEFAULT '',
                   active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
               );
               CREATE TABLE IF NOT EXISTS resource_availability (
                   resource_type TEXT NOT NULL CHECK (resource_type IN ('interviewer', 'room')),
                   resource_id INTEGER NOT NULL,
                   weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                   interview_time TEXT NOT NULL,
                   available INTEGER NOT NULL DEFAULT 1,
                   PRIMARY KEY (resource_type, resource_id, weekday, interview_time)
               );
               CREATE TABLE IF NOT EXISTS candidate_availability (
                   candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                   interview_date TEXT NOT NULL,
                   interview_time TEXT NOT NULL,
                   available INTEGER NOT NULL DEFAULT 1,
                   PRIMARY KEY (candidate_id, interview_date, interview_time)
               );
               CREATE TABLE IF NOT EXISTS interviews (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                   round_number INTEGER NOT NULL REFERENCES interview_rounds(round_number),
                   stage TEXT NOT NULL,
                   interview_date TEXT NOT NULL,
                   interview_time TEXT NOT NULL,
                   interviewer_id INTEGER NOT NULL REFERENCES interviewers(id),
                   room_id INTEGER NOT NULL REFERENCES rooms(id),
                   status TEXT NOT NULL DEFAULT 'scheduled',
                   UNIQUE (candidate_id, round_number)
               );
               CREATE TABLE IF NOT EXISTS evaluations (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   interview_id INTEGER NOT NULL UNIQUE REFERENCES interviews(id) ON DELETE CASCADE,
                   score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
                   feedback TEXT NOT NULL,
                   decision TEXT NOT NULL CHECK (decision IN ('pass', 'fail')),
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               );
               CREATE TABLE IF NOT EXISTS notifications (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
                   message TEXT NOT NULL,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   read INTEGER NOT NULL DEFAULT 0
               );
               CREATE INDEX IF NOT EXISTS interviews_slot
                   ON interviews (interview_date, interview_time, status);
               CREATE INDEX IF NOT EXISTS candidates_schedule
                   ON candidates (status, interview_date, interview_time);
               CREATE TRIGGER IF NOT EXISTS candidates_round_insert
                   BEFORE INSERT ON candidates
                   WHEN NOT EXISTS (
                       SELECT 1 FROM interview_rounds
                       WHERE round_number = NEW.current_round AND active = 1
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'Candidate round must reference an active interview round.');
                   END;
               CREATE TRIGGER IF NOT EXISTS candidates_round_update
                   BEFORE UPDATE OF current_round ON candidates
                   WHEN NOT EXISTS (
                       SELECT 1 FROM interview_rounds
                       WHERE round_number = NEW.current_round AND active = 1
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'Candidate round must reference an active interview round.');
                   END;
               CREATE TRIGGER IF NOT EXISTS interviews_round_insert
                   BEFORE INSERT ON interviews
                   WHEN NOT EXISTS (
                       SELECT 1 FROM interview_rounds WHERE round_number = NEW.round_number
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'Interview round does not exist.');
                   END;
               CREATE TRIGGER IF NOT EXISTS interviews_round_update
                   BEFORE UPDATE OF round_number ON interviews
                   WHEN NOT EXISTS (
                       SELECT 1 FROM interview_rounds WHERE round_number = NEW.round_number
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'Interview round does not exist.');
                   END;
               CREATE TRIGGER IF NOT EXISTS interviews_resources_insert
                   BEFORE INSERT ON interviews
                   WHEN NOT EXISTS (SELECT 1 FROM candidates WHERE id = NEW.candidate_id)
                     OR NOT EXISTS (SELECT 1 FROM interviewers WHERE id = NEW.interviewer_id)
                     OR NOT EXISTS (SELECT 1 FROM rooms WHERE id = NEW.room_id)
                   BEGIN
                       SELECT RAISE(ABORT, 'Interview must reference an existing candidate, interviewer, and room.');
                   END;
               CREATE TRIGGER IF NOT EXISTS interviews_resources_update
                   BEFORE UPDATE OF candidate_id, interviewer_id, room_id ON interviews
                   WHEN NOT EXISTS (SELECT 1 FROM candidates WHERE id = NEW.candidate_id)
                     OR NOT EXISTS (SELECT 1 FROM interviewers WHERE id = NEW.interviewer_id)
                     OR NOT EXISTS (SELECT 1 FROM rooms WHERE id = NEW.room_id)
                   BEGIN
                       SELECT RAISE(ABORT, 'Interview must reference an existing candidate, interviewer, and room.');
                   END;"""
        )
        connection.executemany(
            """INSERT OR IGNORE INTO interview_rounds
               (round_number, name, description, active) VALUES (?, ?, ?, 1)""",
            ROUND_DEFINITIONS,
        )
        # Panel interviews and per-interviewer daily capacity (idempotent migrations).
        round_columns = {row["name"] for row in connection.execute("PRAGMA table_info(interview_rounds)")}
        if "panel_size" not in round_columns:
            connection.execute(
                "ALTER TABLE interview_rounds ADD COLUMN panel_size INTEGER NOT NULL DEFAULT 1"
            )
        interviewer_columns = {row["name"] for row in connection.execute("PRAGMA table_info(interviewers)")}
        if "max_per_day" not in interviewer_columns:
            connection.execute(
                f"ALTER TABLE interviewers ADD COLUMN max_per_day INTEGER NOT NULL DEFAULT {DEFAULT_MAX_PER_DAY}"
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS interview_panelists (
                   interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                   interviewer_id INTEGER NOT NULL REFERENCES interviewers(id),
                   PRIMARY KEY (interview_id, interviewer_id)
               )"""
        )
        connection.execute("UPDATE candidates SET program = role WHERE program = ''")
        connection.execute(
            "UPDATE candidates SET batch = ? WHERE name = ? AND batch = ''",
            ("2027", "Yusuf Patel"),
        )
        existing_names = {
            row["name"] for row in connection.execute("SELECT name FROM candidates")
        }
        existing_names = {
            row["name"] for row in connection.execute("SELECT name FROM candidates")
        }
        for candidate in SAMPLE_CANDIDATES:
            if candidate[0] in existing_names:
                continue
            if candidate[0] in existing_names:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO candidates
                   (name, role, program, stage, batch, priority, status, interviewer,
                    interview_date, interview_time, remote)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate[0], candidate[1], candidate[1], candidate[2], candidate[9], *candidate[3:9]),
            )

        for name, skills in DEFAULT_INTERVIEWERS:
            connection.execute(
                "INSERT OR IGNORE INTO interviewers (name, skills) VALUES (?, ?)",
                (name, json.dumps(skills)),
            )
        for email, password, role, display_name in DEMO_USERS:
            candidate_id = None
            if role == "candidate":
                candidate = connection.execute(
                    "SELECT id FROM candidates WHERE name = ?", (display_name,)
                ).fetchone()
                candidate_id = candidate["id"] if candidate else None
            existing = connection.execute(
                "SELECT id FROM users WHERE email = ?", (email,)
            ).fetchone()
            if not existing:
                salt = secrets.token_bytes(16)
                password_hash = hashlib.pbkdf2_hmac(
                    "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
                )
                connection.execute(
                    """INSERT INTO users
                       (email, password_hash, password_salt, role, display_name, candidate_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (email, password_hash.hex(), salt.hex(), role, display_name, candidate_id),
                )
        legacy_names = connection.execute(
            "SELECT DISTINCT interviewer FROM candidates WHERE interviewer != ''"
        ).fetchall()
        all_skills = json.dumps(list(dict.fromkeys(skill for _, skills in DEFAULT_INTERVIEWERS for skill in skills)))
        for row in legacy_names:
            connection.execute(
                "INSERT OR IGNORE INTO interviewers (name, skills) VALUES (?, ?)",
                (row["interviewer"], all_skills),
            )
        for name in ROOMS:
            connection.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (name,))
        seed_role_accounts(connection)

        for interviewer in connection.execute("SELECT id FROM interviewers"):
            for weekday in range(5):
                connection.executemany(
                    """INSERT OR IGNORE INTO resource_availability
                       (resource_type, resource_id, weekday, interview_time, available)
                       VALUES ('interviewer', ?, ?, ?, 1)""",
                    ((interviewer["id"], weekday, time) for time in TIMES),
                )
        for room in connection.execute("SELECT id FROM rooms"):
            for weekday in range(5):
                connection.executemany(
                    """INSERT OR IGNORE INTO resource_availability
                       (resource_type, resource_id, weekday, interview_time, available)
                       VALUES ('room', ?, ?, ?, 1)""",
                    ((room["id"], weekday, time) for time in TIMES),
                )

        connection.execute(
            """UPDATE candidates SET interviewer_id = (
                   SELECT id FROM interviewers WHERE interviewers.name = candidates.interviewer
               ) WHERE interviewer_id = 0 AND interviewer != ''"""
        )
        connection.execute(
            "UPDATE candidates SET room_id = (SELECT id FROM rooms WHERE name = 'Interview suite') WHERE room_id = 0"
        )
        scheduled = connection.execute(
            "SELECT id, current_round, stage, interview_date, interview_time, interviewer_id, room_id "
            "FROM candidates WHERE status = 'scheduled'"
        ).fetchall()
        for candidate in scheduled:
            connection.execute(
                """INSERT OR IGNORE INTO interviews
                   (candidate_id, round_number, stage, interview_date, interview_time,
                    interviewer_id, room_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled')""",
                (candidate["id"], candidate["current_round"], candidate["stage"],
                 candidate["interview_date"], candidate["interview_time"],
                 candidate["interviewer_id"], candidate["room_id"]),
            )


def save_panel(connection, candidate_id, round_number, panel_ids):
    """Store the non-lead panelists for a candidate's round (lead lives on interviews)."""
    interview = connection.execute(
        "SELECT id, interviewer_id FROM interviews WHERE candidate_id = ? AND round_number = ?",
        (candidate_id, round_number),
    ).fetchone()
    if not interview:
        return
    connection.execute("DELETE FROM interview_panelists WHERE interview_id = ?", (interview["id"],))
    connection.executemany(
        "INSERT OR IGNORE INTO interview_panelists (interview_id, interviewer_id) VALUES (?, ?)",
        ((interview["id"], member) for member in panel_ids if member != interview["interviewer_id"]),
    )


def user_is_on_interview(connection, interview_id, display_name):
    """True if the named interviewer is the lead or a panelist on the interview."""
    return connection.execute(
        """SELECT 1 FROM interviews i
           LEFT JOIN interviewers lead ON lead.id = i.interviewer_id
           WHERE i.id = ? AND (lead.name = ? OR EXISTS (
                 SELECT 1 FROM interview_panelists p JOIN interviewers pv ON pv.id = p.interviewer_id
                 WHERE p.interview_id = i.id AND pv.name = ?))""",
        (interview_id, display_name, display_name),
    ).fetchone() is not None


def schedule_candidate(
    connection,
    candidate_id,
    interview_date,
    preferred_time=None,
    requested_interviewer=None,
    requested_room_id=None,
    requested_panel_ids=None,
):
    date.fromisoformat(interview_date)
    candidate = connection.execute(
        f"SELECT {FIELDS} FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if not candidate:
        raise LookupError("Candidate not found.")
    if candidate["status"] != "waiting" or candidate["outcome"] != "active":
        raise ValueError("Candidate is not waiting for an interview.")
    options = find_assignments(connection, candidate, interview_date, preferred_time)
    if requested_interviewer:
        options = [option for option in options if requested_interviewer in option["panel_names"]]
    if requested_panel_ids:
        wanted = set(requested_panel_ids)
        options = [option for option in options if set(option["panel_ids"]) == wanted]
    if requested_room_id is not None:
        options = [option for option in options if option["room_id"] == requested_room_id]
    if not options:
        raise ValueError("No conflict-free slot matches this candidate's availability and interview requirements.")
    assignment = options[0]
    connection.execute(
        """UPDATE candidates SET status = 'scheduled', interviewer = ?,
           interviewer_id = ?, room_id = ?, interview_date = ?, interview_time = ?, remote = 0
           WHERE id = ?""",
        (assignment["interviewer"], assignment["interviewer_id"], assignment["room_id"],
         interview_date, assignment["time"], candidate_id),
    )
    connection.execute(
        """INSERT INTO interviews
           (candidate_id, round_number, stage, interview_date, interview_time,
            interviewer_id, room_id, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled')
           ON CONFLICT(candidate_id, round_number) DO UPDATE SET
             stage = excluded.stage, interview_date = excluded.interview_date,
             interview_time = excluded.interview_time, interviewer_id = excluded.interviewer_id,
             room_id = excluded.room_id, status = 'scheduled'""",
        (candidate_id, candidate["current_round"], candidate["stage"], interview_date,
         assignment["time"], assignment["interviewer_id"], assignment["room_id"]),
    )
    save_panel(connection, candidate_id, candidate["current_round"], assignment["panel_ids"])
    panel_text = ", ".join(assignment["panel_names"])
    connection.execute(
        "INSERT INTO notifications (candidate_id, message) VALUES (?, ?)",
        (candidate_id, f"{candidate['stage']} interview scheduled for {interview_date} at {assignment['time']} with {panel_text}."),
    )
    return assignment


def candidate_payload(connection, candidate_id):
    return connection.execute(
        f"""SELECT {CANDIDATE_FIELDS}, i.id AS interview_id, r.name AS room,
                   e.score, e.feedback, e.decision
            FROM candidates c
            LEFT JOIN interviews i ON i.candidate_id = c.id AND i.round_number = c.current_round
            LEFT JOIN rooms r ON r.id = c.room_id
            LEFT JOIN evaluations e ON e.interview_id = i.id
            WHERE c.id = ?""",
        (candidate_id,),
    ).fetchone()


def password_matches(password, salt_hex, expected_hash):
    actual_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PASSWORD_ITERATIONS
    )
    return hmac.compare_digest(actual_hash.hex(), expected_hash)


class TrialsHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.response_headers = []
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in self.response_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.response_headers = []

    def current_user(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get("trialsync_session")
            if value is None:
                return None
            raw_token, separator, signature = value.value.partition(".")
            if not separator:
                return None
            expected = hmac.new(
                SESSION_SECRET.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            with connect_database() as connection:
                return connection.execute(
                    """SELECT u.id, u.email, u.role, u.display_name, u.candidate_id
                       FROM user_sessions s JOIN users u ON u.id = s.user_id
                       WHERE s.token_hash = ? AND s.expires_at > ?""",
                    (token_hash, int(time.time())),
                ).fetchone()
        except (sqlite3.Error, ValueError):
            return None

    def require_roles(self, *roles):
        user = self.current_user()
        if user is None:
            self.send_json(401, {"error": "Sign in to continue.", "code": "authentication_required"})
            return None
        if roles and user["role"] not in roles:
            self.send_json(403, {"error": "Your account does not have permission to do that.", "code": "role_forbidden"})
            return None
        return user

    def set_session_cookie(self, value, max_age):
        self.response_headers.append((
            "Set-Cookie",
            f"trialsync_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}",
        ))

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("Request body is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        request = urlparse(self.path)
        path = request.path
        if path == "/api/auth/session":
            user = self.current_user()
            return self.send_json(200, {"user": dict(user) if user else None})
        if path == "/api/health":
            return self.send_json(200, {"status": "ok"})
        if path == "/api/me/dashboard":
            user = self.require_roles("coordinator", "interviewer", "candidate")
            if user is None:
                return
            requested_date = parse_qs(request.query).get("date", [date.today().isoformat()])[0]
            try:
                date.fromisoformat(requested_date)
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
            if user["role"] == "candidate":
                if not user["candidate_id"]:
                    return self.send_json(404, {"error": "No candidate record is linked to this account."})
                with connect_database() as connection:
                    candidate = connection.execute(
                        """SELECT c.id, c.name, c.program, c.batch, c.stage, c.current_round,
                                  c.status, c.outcome, c.interview_date, c.interview_time,
                                  c.interviewer, r.name AS room, i.id AS interview_id,
                                  e.score, e.feedback, e.decision
                           FROM candidates c
                           LEFT JOIN rooms r ON r.id = c.room_id
                           LEFT JOIN interviews i ON i.candidate_id = c.id
                             AND i.round_number = c.current_round
                           LEFT JOIN evaluations e ON e.interview_id = i.id
                           WHERE c.id = ?""",
                        (user["candidate_id"],),
                    ).fetchone()
                    rounds = connection.execute(
                        """SELECT i.round_number, i.stage, i.interview_date, i.interview_time,
                                  i.status, iv.name AS interviewer, r.name AS room,
                                  (SELECT group_concat(pv.name, ', ') FROM interview_panelists p
                                   JOIN interviewers pv ON pv.id = p.interviewer_id
                                   WHERE p.interview_id = i.id) AS panel,
                                  e.score, e.decision
                           FROM interviews i
                           LEFT JOIN interviewers iv ON iv.id = i.interviewer_id
                           LEFT JOIN rooms r ON r.id = i.room_id
                           LEFT JOIN evaluations e ON e.interview_id = i.id
                           WHERE i.candidate_id = ? ORDER BY i.round_number""",
                        (user["candidate_id"],),
                    ).fetchall()
                return self.send_json(200, {
                    "user": {"role": user["role"], "display_name": user["display_name"]},
                    "candidate": dict(candidate) if candidate else None,
                    "rounds": [dict(row) for row in rounds],
                })
            today = requested_date
            with connect_database() as connection:
                if user["role"] == "interviewer":
                    schedule = connection.execute(
                        f"""SELECT {CANDIDATE_FIELDS}, c.interview_time AS time,
                                   i.id AS interview_id, r.name AS room, e.score, e.feedback, e.decision
                            FROM candidates c
                            JOIN interviews i ON i.candidate_id = c.id
                              AND i.round_number = c.current_round AND i.status = 'scheduled'
                            LEFT JOIN rooms r ON r.id = c.room_id
                            LEFT JOIN evaluations e ON e.interview_id = i.id
                            WHERE c.status = 'scheduled' AND c.interview_date = ?
                              AND (c.interviewer = ? OR EXISTS (
                                   SELECT 1 FROM interview_panelists p
                                   JOIN interviewers pv ON pv.id = p.interviewer_id
                                   WHERE p.interview_id = i.id AND pv.name = ?))
                            ORDER BY c.interview_time""",
                        (today, user["display_name"], user["display_name"]),
                    ).fetchall()
                    return self.send_json(200, {
                        "user": {"role": user["role"], "display_name": user["display_name"]},
                        "date": today,
                        "scheduled": [dict(row) for row in schedule],
                    })
            return self.send_json(200, {
                "user": {"role": user["role"], "display_name": user["display_name"]},
                "date": today,
            })
        if path == "/api/me/booking-options":
            user = self.require_roles("candidate")
            if user is None:
                return
            if not user["candidate_id"]:
                return self.send_json(404, {"error": "No candidate record is linked to this account."})
            try:
                target_date = parse_qs(request.query).get("date", [date.today().isoformat()])[0]
                date.fromisoformat(target_date)
                with connect_database() as connection:
                    candidate = connection.execute(
                        f"SELECT {FIELDS} FROM candidates WHERE id = ?",
                        (user["candidate_id"],),
                    ).fetchone()
                    available = (
                        find_assignments(
                            connection, candidate, target_date,
                            require_candidate_availability=True,
                        )
                        if candidate and candidate["status"] == "waiting" and candidate["outcome"] == "active"
                        else []
                    )
                return self.send_json(200, {
                    "candidate": dict(candidate) if candidate else None,
                    "date": target_date,
                    "available": available,
                })
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
        if path == "/api/candidates":
            if self.require_roles("coordinator") is None:
                return
            try:
                target_date = parse_qs(request.query).get("date", [date.today().isoformat()])[0]
                date.fromisoformat(target_date)
                with connect_database() as connection:
                    scheduled = connection.execute(
                        f"""SELECT {CANDIDATE_FIELDS}, c.interview_time AS time, c.interviewer_id,
                                   c.room_id, r.name AS room, i.id AS interview_id,
                                   e.score, e.feedback, e.decision
                            FROM candidates c
                            LEFT JOIN rooms r ON r.id = c.room_id
                            LEFT JOIN interviews i ON i.candidate_id = c.id
                              AND i.round_number = c.current_round AND i.status = 'scheduled'
                            LEFT JOIN evaluations e ON e.interview_id = i.id
                            WHERE c.status = 'scheduled' AND c.interview_date = ?
                            ORDER BY c.interview_time, c.id""",
                        (target_date,),
                    ).fetchall()
                    waiting = connection.execute(
                        f"SELECT {FIELDS} FROM candidates WHERE status = 'waiting' AND outcome = 'active' ORDER BY priority DESC, id"
                    ).fetchall()
                return self.send_json(200, {
                    "date": target_date,
                    "scheduled": [dict(row) for row in scheduled],
                    "queue": [dict(row) for row in waiting],
                })
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
        if path == "/api/resources":
            if self.require_roles("coordinator", "interviewer") is None:
                return
            with connect_database() as connection:
                interviewers = connection.execute("SELECT id, name, skills FROM interviewers ORDER BY name").fetchall()
                rooms = connection.execute("SELECT id, name FROM rooms ORDER BY name").fetchall()
                stages = get_rounds(connection, active_only=True)
            return self.send_json(200, {
                "interviewers": [{**dict(row), "skills": json.loads(row["skills"])} for row in interviewers],
                "rooms": [dict(row) for row in rooms],
                "stages": [row["name"] for row in stages],
            })
        if path == "/api/interviewers":
            if self.require_roles("coordinator", "interviewer") is None:
                return
            with connect_database() as connection:
                rows = connection.execute(
                    """SELECT i.id, i.name, i.skills, i.max_per_day,
                              COUNT(DISTINCT x.id) AS scheduled_interviews
                       FROM interviewers i LEFT JOIN interviews x
                         ON x.status = 'scheduled'
                        AND (x.interviewer_id = i.id OR EXISTS (
                             SELECT 1 FROM interview_panelists p
                             WHERE p.interview_id = x.id AND p.interviewer_id = i.id))
                       GROUP BY i.id ORDER BY i.name"""
                ).fetchall()
            return self.send_json(200, {
                "interviewers": [
                    {**dict(row), "skills": json.loads(row["skills"])}
                    for row in rows
                ]
            })
        if path == "/api/rooms":
            if self.require_roles("coordinator", "interviewer") is None:
                return
            with connect_database() as connection:
                rows = connection.execute(
                    """SELECT r.id, r.name,
                              COUNT(DISTINCT i.id) AS scheduled_interviews
                       FROM rooms r LEFT JOIN interviews i
                         ON i.room_id = r.id AND i.status = 'scheduled'
                       GROUP BY r.id ORDER BY r.name"""
                ).fetchall()
            return self.send_json(200, {"rooms": [dict(row) for row in rows]})
        if path == "/api/interview-rounds":
            if self.require_roles("coordinator", "interviewer") is None:
                return
            with connect_database() as connection:
                rounds = connection.execute(
                    """SELECT r.round_number, r.name, r.description, r.active, r.panel_size,
                              COUNT(DISTINCT i.id) AS scheduled_interviews,
                              COUNT(DISTINCT c.id) AS active_candidates
                       FROM interview_rounds r
                       LEFT JOIN interviews i ON i.round_number = r.round_number
                         AND i.status = 'scheduled'
                       LEFT JOIN candidates c ON c.current_round = r.round_number
                         AND c.outcome = 'active'
                       GROUP BY r.round_number ORDER BY r.round_number"""
                ).fetchall()
            return self.send_json(200, {"rounds": [dict(row) for row in rounds]})
        if path == "/api/notifications":
            user = self.require_roles("coordinator", "interviewer", "candidate")
            if user is None:
                return
            with connect_database() as connection:
                if user["role"] == "candidate":
                    notifications = connection.execute(
                        """SELECT id, candidate_id, message, created_at, read FROM notifications
                           WHERE candidate_id = ? ORDER BY id DESC LIMIT 30""",
                        (user["candidate_id"],),
                    ).fetchall()
                else:
                    notifications = connection.execute(
                        "SELECT id, candidate_id, message, created_at, read FROM notifications ORDER BY id DESC LIMIT 30"
                    ).fetchall()
            return self.send_json(200, {"notifications": [dict(row) for row in notifications]})
        if path == "/api/status-board":
            if self.require_roles("coordinator") is None:
                return
            with connect_database() as connection:
                rows = connection.execute(
                    """SELECT c.id, c.name, c.program, c.batch, c.stage, c.current_round,
                              c.priority, c.status, c.outcome, c.interview_date, c.interview_time,
                              c.interviewer, r.name AS room,
                              (SELECT group_concat(pv.name, ', ')
                                 FROM interviews i
                                 JOIN interview_panelists p ON p.interview_id = i.id
                                 JOIN interviewers pv ON pv.id = p.interviewer_id
                                WHERE i.candidate_id = c.id AND i.round_number = c.current_round
                                  AND i.status = 'scheduled') AS panel,
                              EXISTS (SELECT 1 FROM candidate_availability a
                                       WHERE a.candidate_id = c.id AND a.available = 1) AS has_availability
                       FROM candidates c LEFT JOIN rooms r ON r.id = c.room_id
                       ORDER BY c.interview_date, c.interview_time, c.priority DESC, c.name"""
                ).fetchall()
            board = {"scheduled": [], "pending": [], "passed": [], "rejected": []}
            for row in rows:
                item = dict(row)
                item["has_availability"] = bool(item["has_availability"])
                if item["outcome"] == "passed":
                    board["passed"].append(item)
                elif item["outcome"] == "rejected":
                    board["rejected"].append(item)
                elif item["status"] == "scheduled":
                    board["scheduled"].append(item)
                else:
                    board["pending"].append(item)
            board["pending"].sort(key=lambda item: (-item["priority"], item["name"]))
            return self.send_json(200, {
                "counts": {name: len(items) for name, items in board.items()},
                **board,
            })
        if path == "/api/rankings":
            if self.require_roles("coordinator", "interviewer") is None:
                return
            with connect_database() as connection:
                rows = connection.execute(
                    """SELECT c.id, c.name, c.program, c.batch, c.outcome,
                              ROUND(AVG(e.score), 2) AS average_score,
                              COUNT(e.id) AS evaluation_count
                       FROM candidates c LEFT JOIN interviews i ON i.candidate_id = c.id
                       LEFT JOIN evaluations e ON e.interview_id = i.id
                       GROUP BY c.id ORDER BY average_score DESC, c.name"""
                ).fetchall()
            return self.send_json(200, {"rankings": [dict(row) for row in rows]})
        if path.startswith("/api/booking/"):
            token = path.rsplit("/", 1)[-1]
            requested_date = parse_qs(request.query).get("date", [date.today().isoformat()])[0]
            try:
                date.fromisoformat(requested_date)
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
            if date.fromisoformat(requested_date).weekday() > 4:
                return self.send_json(200, {"candidate": None, "date": requested_date, "available": []})
            with connect_database() as connection:
                candidate = connection.execute(
                    f"SELECT {FIELDS} FROM candidates WHERE booking_token = ? AND status = 'waiting' AND outcome = 'active'",
                    (token,),
                ).fetchone()
                if not candidate:
                    return self.send_json(404, {"error": "This booking link is invalid or no longer active."})
                availability = []
                for option in find_assignments(
                    connection, candidate, requested_date,
                    require_candidate_availability=True,
                ):
                    availability.append(option)
            return self.send_json(200, {
                "candidate": dict(candidate),
                "date": requested_date,
                "available": availability,
            })
        if path == "/":
            self.path = "/login.html"
        static_path = (ROOT / unquote(path).lstrip("/")).resolve()
        try:
            static_path.relative_to(ROOT)
        except ValueError:
            return self.send_error(404)
        if (
            static_path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".py", ".pyc"}
            or any(part in {"tests", "__pycache__"} for part in static_path.parts)
        ):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/auth/login":
                email_value = body.get("email", "")
                password_value = body.get("password", "")
                if not isinstance(email_value, str) or not isinstance(password_value, str):
                    return self.send_json(400, {"error": "Enter a valid email address and password."})
                email = email_value.strip().lower()
                password = password_value
                with connect_database() as connection:
                    user = connection.execute(
                        "SELECT * FROM users WHERE email = ?", (email,)
                    ).fetchone()
                    valid_password = password_matches(
                        password,
                        user["password_salt"] if user else "00" * 16,
                        user["password_hash"] if user else "00" * 32,
                    )
                    if not user or not valid_password:
                        return self.send_json(401, {"error": "Email or password is incorrect."})
                    raw_token = secrets.token_urlsafe(32)
                    signature = hmac.new(
                        SESSION_SECRET.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256
                    ).hexdigest()
                    expires_at = int(time.time()) + SESSION_MAX_AGE
                    connection.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (int(time.time()),))
                    connection.execute(
                        "INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                        (hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), user["id"], expires_at),
                    )
                self.set_session_cookie(f"{raw_token}.{signature}", SESSION_MAX_AGE)
                return self.send_json(200, {
                    "user": {
                        "id": user["id"],
                        "email": user["email"],
                        "role": user["role"],
                        "display_name": user["display_name"],
                    }
                })
            if path == "/api/auth/register":
                name = re.sub(r"\s+", " ", str(body.get("name", ""))).strip()
                email = str(body.get("email", "")).strip().lower()
                password = body.get("password", "")
                program = str(body.get("program", "")).strip()
                batch = str(body.get("batch", "")).strip()
                if not 2 <= len(name) <= 80:
                    return self.send_json(400, {"error": "Enter your full name (2-80 characters)."})
                if len(email) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    return self.send_json(400, {"error": "Enter a valid email address."})
                if not isinstance(password, str) or not 8 <= len(password) <= 128:
                    return self.send_json(400, {"error": "Password must be 8-128 characters."})
                if program not in ("MCA", "B.Tech", "M.Tech", "M.Sc"):
                    return self.send_json(400, {"error": "Choose a degree program."})
                if batch not in ("2026", "2027", "2028", "2029", "2030"):
                    return self.send_json(400, {"error": "Choose a graduating batch."})
                with connect_database() as connection:
                    if connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                        return self.send_json(409, {"error": "An account with this email already exists. Sign in instead."})
                    first_round = connection.execute(
                        "SELECT round_number, name FROM interview_rounds WHERE active = 1 ORDER BY round_number LIMIT 1"
                    ).fetchone()
                    cursor = connection.execute(
                        """INSERT INTO candidates
                           (name, role, program, stage, batch, priority, current_round)
                           VALUES (?, ?, ?, ?, ?, 0, ?)""",
                        (name, program, program, first_round["name"], batch, first_round["round_number"]),
                    )
                    _insert_user(connection, email, password, "candidate", name, cursor.lastrowid)
                    user_id = connection.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
                    raw_token = secrets.token_urlsafe(32)
                    signature = hmac.new(
                        SESSION_SECRET.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256
                    ).hexdigest()
                    connection.execute(
                        "INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                        (hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), user_id,
                         int(time.time()) + SESSION_MAX_AGE),
                    )
                self.set_session_cookie(f"{raw_token}.{signature}", SESSION_MAX_AGE)
                return self.send_json(201, {
                    "user": {"id": user_id, "email": email, "role": "candidate", "display_name": name}
                })
            if path == "/api/auth/logout":
                user = self.current_user()
                if user is not None:
                    cookie = SimpleCookie()
                    cookie.load(self.headers.get("Cookie", ""))
                    raw_token = cookie["trialsync_session"].value.partition(".")[0]
                    with connect_database() as connection:
                        connection.execute(
                            "DELETE FROM user_sessions WHERE token_hash = ?",
                            (hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),),
                        )
                self.set_session_cookie("", 0)
                return self.send_json(200, {"message": "Signed out."})

            user = None
            if path.startswith("/api/booking/"):
                pass
            elif path in ("/api/candidates", "/api/auto-plan", "/api/availability",
                          "/api/interviewers/capacity", "/api/interview-rounds/panel-size"):
                user = self.require_roles("coordinator")
                if user is None:
                    return
            elif path in ("/api/me/availability", "/api/me/booking"):
                user = self.require_roles("candidate")
                if user is None:
                    return
            elif path.startswith("/api/candidates/") and path.endswith("/booking-link"):
                user = self.require_roles("coordinator")
                if user is None:
                    return
            elif path.startswith("/api/interviews/") and path.endswith("/evaluate"):
                user = self.require_roles("coordinator", "interviewer")
                if user is None:
                    return
            elif path.startswith("/api/interviews/") and path.endswith("/cancel"):
                user = self.require_roles("coordinator", "interviewer")
                if user is None:
                    return
            else:
                return self.send_json(404, {"error": "Endpoint not found."})

            if path == "/api/candidates":
                name = str(body.get("name", "")).strip()
                program = str(body.get("program", body.get("role", ""))).strip()
                batch = str(body.get("batch", "")).strip()
                stage = str(body.get("stage", "Coding")).strip()
                with connect_database() as connection:
                    round_definition = connection.execute(
                        """SELECT round_number, name FROM interview_rounds
                           WHERE name = ? AND active = 1""",
                        (stage,),
                    ).fetchone()
                    if not name or not program or not batch or not round_definition:
                        return self.send_json(400, {"error": "Name, degree program, batch, and a valid pipeline stage are required."})
                    cursor = connection.execute(
                        """INSERT INTO candidates
                           (name, role, program, stage, batch, priority, current_round)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (name, program, program, stage, batch,
                         int(bool(body.get("priority", False))), round_definition["round_number"]),
                    )
                    candidate = candidate_payload(connection, cursor.lastrowid)
                return self.send_json(201, dict(candidate))

            if path == "/api/auto-plan":
                target_date = str(body.get("date", ""))
                date.fromisoformat(target_date)
                with connect_database() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    candidates = connection.execute(
                        f"SELECT {FIELDS} FROM candidates WHERE status = 'waiting' AND outcome = 'active' ORDER BY priority DESC, id"
                    ).fetchall()
                    option_map = {
                        candidate["id"]: find_assignments(connection, candidate, target_date)
                        for candidate in candidates
                    }
                    assignments, _nodes = solve(
                        option_map, capacity=remaining_capacity(connection, target_date)
                    )
                    for candidate_id, option in assignments.items():
                        schedule_candidate(
                            connection,
                            candidate_id,
                            target_date,
                            option["time"],
                            option["interviewer"],
                            option["room_id"],
                            option["panel_ids"],
                        )
                    return self.send_json(200, {
                        "scheduled": len(assignments),
                        "unscheduled": len(candidates) - len(assignments),
                        "message": f"Scheduled {len(assignments)} candidate(s) without resource conflicts.",
                    })

            if path == "/api/interviewers/capacity":
                interviewer_id = int(body.get("interviewer_id", 0))
                max_per_day = int(body.get("max_per_day", 0))
                if not 1 <= max_per_day <= len(TIMES):
                    return self.send_json(400, {"error": f"max_per_day must be between 1 and {len(TIMES)}."})
                with connect_database() as connection:
                    updated = connection.execute(
                        "UPDATE interviewers SET max_per_day = ? WHERE id = ?", (max_per_day, interviewer_id)
                    ).rowcount
                if not updated:
                    return self.send_json(404, {"error": "Interviewer not found."})
                return self.send_json(200, {"message": "Daily capacity updated."})

            if path == "/api/interview-rounds/panel-size":
                round_number = int(body.get("round_number", 0))
                panel_size = int(body.get("panel_size", 0))
                if not 1 <= panel_size <= 5:
                    return self.send_json(400, {"error": "panel_size must be between 1 and 5."})
                with connect_database() as connection:
                    updated = connection.execute(
                        "UPDATE interview_rounds SET panel_size = ? WHERE round_number = ?",
                        (panel_size, round_number),
                    ).rowcount
                if not updated:
                    return self.send_json(404, {"error": "Interview round not found."})
                return self.send_json(200, {"message": "Panel size updated."})

            if path == "/api/availability":
                resource_type = str(body.get("resource_type", ""))
                resource_id = int(body.get("resource_id", 0))
                weekday = int(body.get("weekday", -1))
                selected_times = body.get("times")
                if resource_type not in ("interviewer", "room") or weekday not in range(7) or not isinstance(selected_times, list):
                    return self.send_json(400, {"error": "Provide a valid resource, weekday (0-6), and list of available times."})
                if any(time not in TIMES for time in selected_times):
                    return self.send_json(400, {"error": "Availability times must be between 09:00 and 16:00."})
                table = "interviewers" if resource_type == "interviewer" else "rooms"
                with connect_database() as connection:
                    if not connection.execute(f"SELECT id FROM {table} WHERE id = ?", (resource_id,)).fetchone():
                        return self.send_json(404, {"error": "Scheduling resource not found."})
                    for interview_time in TIMES:
                        connection.execute(
                            """INSERT INTO resource_availability
                               (resource_type, resource_id, weekday, interview_time, available)
                               VALUES (?, ?, ?, ?, ?)
                               ON CONFLICT(resource_type, resource_id, weekday, interview_time)
                               DO UPDATE SET available = excluded.available""",
                            (resource_type, resource_id, weekday, interview_time, int(interview_time in selected_times)),
                        )
                return self.send_json(200, {"message": "Availability updated."})

            if path == "/api/me/availability":
                interview_date = str(body.get("date", ""))
                available_times = body.get("times")
                date.fromisoformat(interview_date)
                if not user["candidate_id"]:
                    return self.send_json(404, {"error": "No candidate record is linked to this account."})
                if not isinstance(available_times, list) or any(time not in TIMES for time in available_times):
                    return self.send_json(400, {"error": "Provide available times between 09:00 and 16:00."})
                with connect_database() as connection:
                    for interview_time in TIMES:
                        connection.execute(
                            """INSERT INTO candidate_availability
                               (candidate_id, interview_date, interview_time, available)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(candidate_id, interview_date, interview_time)
                               DO UPDATE SET available = excluded.available""",
                            (user["candidate_id"], interview_date, interview_time, int(interview_time in available_times)),
                        )
                return self.send_json(200, {"message": "Candidate availability saved."})

            if path == "/api/me/booking":
                interview_date = str(body.get("date", ""))
                interview_time = str(body.get("time", ""))
                date.fromisoformat(interview_date)
                if not user["candidate_id"]:
                    return self.send_json(404, {"error": "No candidate record is linked to this account."})
                with connect_database() as connection:
                    candidate = connection.execute(
                        f"SELECT {FIELDS} FROM candidates WHERE id = ?",
                        (user["candidate_id"],),
                    ).fetchone()
                    if not candidate or not candidate_is_available(
                        connection, user["candidate_id"], interview_date, interview_time
                    ):
                        return self.send_json(400, {"error": "You must mark this hour as available before booking."})
                    assignment = schedule_candidate(
                        connection, user["candidate_id"], interview_date, interview_time
                    )
                    result = dict(candidate_payload(connection, user["candidate_id"]))
                result.update(assignment)
                return self.send_json(200, result)

            if path.startswith("/api/booking/") and path.endswith("/availability"):
                parts = path.strip("/").split("/")
                interview_date = str(body.get("date", ""))
                available_times = body.get("times")
                date.fromisoformat(interview_date)
                if not isinstance(available_times, list) or any(time not in TIMES for time in available_times):
                    return self.send_json(400, {"error": "Provide available times between 09:00 and 16:00."})
                with connect_database() as connection:
                    token = parts[2]
                    candidate = connection.execute(
                        "SELECT id FROM candidates WHERE booking_token = ? AND status = 'waiting' AND outcome = 'active'",
                        (token,),
                    ).fetchone()
                    if not candidate:
                        return self.send_json(404, {"error": "Waiting candidate not found."})
                    for interview_time in TIMES:
                        connection.execute(
                            """INSERT INTO candidate_availability
                               (candidate_id, interview_date, interview_time, available)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(candidate_id, interview_date, interview_time)
                               DO UPDATE SET available = excluded.available""",
                            (candidate["id"], interview_date, interview_time, int(interview_time in available_times)),
                        )
                return self.send_json(200, {"message": "Candidate availability saved."})

            if path.startswith("/api/candidates/") and path.endswith("/booking-link"):
                candidate_id = int(path.split("/")[-2])
                token = secrets.token_urlsafe(24)
                with connect_database() as connection:
                    cursor = connection.execute(
                        "UPDATE candidates SET booking_token = ? WHERE id = ? AND status = 'waiting' AND outcome = 'active'",
                        (token, candidate_id),
                    )
                    if not cursor.rowcount:
                        return self.send_json(404, {"error": "Waiting candidate not found."})
                return self.send_json(201, {"token": token, "url": f"/api/booking/{token}"})

            if path.startswith("/api/booking/"):
                token = path.rsplit("/", 1)[-1]
                interview_date = str(body.get("date", ""))
                interview_time = str(body.get("time", ""))
                date.fromisoformat(interview_date)
                with connect_database() as connection:
                    candidate = connection.execute(
                        f"SELECT {FIELDS} FROM candidates WHERE booking_token = ? AND status = 'waiting' AND outcome = 'active'",
                        (token,),
                    ).fetchone()
                    if not candidate:
                        return self.send_json(404, {"error": "This booking link is invalid or no longer active."})
                    if not candidate_is_available(
                        connection, candidate["id"], interview_date, interview_time
                    ):
                        return self.send_json(400, {"error": "You must mark this hour as available before booking."})
                    assignment = schedule_candidate(connection, candidate["id"], interview_date, interview_time)
                    connection.execute("UPDATE candidates SET booking_token = '' WHERE id = ?", (candidate["id"],))
                    result = dict(candidate_payload(connection, candidate["id"]))
                result.update(assignment)
                return self.send_json(200, result)

            if path.startswith("/api/interviews/") and path.endswith("/evaluate"):
                interview_id = int(path.split("/")[-2])
                score = int(body.get("score", 0))
                feedback = str(body.get("feedback", "")).strip()
                decision = str(body.get("decision", ""))
                if score not in range(1, 6) or not feedback or decision not in ("pass", "fail"):
                    return self.send_json(400, {"error": "Provide a score from 1 to 5, feedback, and a pass/fail decision."})
                with connect_database() as connection:
                    interview = connection.execute(
                        """SELECT i.*, c.current_round, c.name, c.interviewer FROM interviews i
                           JOIN candidates c ON c.id = i.candidate_id
                           WHERE i.id = ? AND i.status = 'scheduled'""",
                        (interview_id,),
                    ).fetchone()
                    if not interview:
                        return self.send_json(404, {"error": "Scheduled interview not found."})
                    if user["role"] == "interviewer" and not user_is_on_interview(connection, interview_id, user["display_name"]):
                        return self.send_json(403, {"error": "Interviewers may only evaluate their own assigned interviews."})
                    connection.execute(
                        """INSERT INTO evaluations (interview_id, score, feedback, decision)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(interview_id) DO UPDATE SET score = excluded.score,
                             feedback = excluded.feedback, decision = excluded.decision,
                             created_at = CURRENT_TIMESTAMP""",
                        (interview_id, score, feedback, decision),
                    )
                    connection.execute("UPDATE interviews SET status = 'completed' WHERE id = ?", (interview_id,))
                    if decision == "fail":
                        connection.execute(
                            "UPDATE candidates SET status = 'waiting', outcome = 'rejected' WHERE id = ?",
                            (interview["candidate_id"],),
                        )
                        message = f"{interview['name']} was marked as not progressing after {interview['stage']}."
                    else:
                        pipeline_rounds = get_rounds(connection, active_only=True)
                        next_round_definition = next(
                            (
                                round_row for round_row in pipeline_rounds
                                if round_row["round_number"] > interview["current_round"]
                            ),
                            None,
                        )
                    if decision == "pass" and next_round_definition:
                        next_round = next_round_definition["round_number"]
                        next_stage = next_round_definition["name"]
                        connection.execute(
                            """UPDATE candidates SET status = 'waiting', stage = ?,
                               current_round = ?, interviewer = '', interviewer_id = 0,
                               room_id = 0, interview_date = '', interview_time = ''
                               WHERE id = ?""",
                            (next_stage, next_round, interview["candidate_id"]),
                        )
                        message = f"{interview['name']} passed {interview['stage']} and is ready for {next_stage}."
                    elif decision == "pass":
                        connection.execute(
                            "UPDATE candidates SET status = 'waiting', outcome = 'passed' WHERE id = ?",
                            (interview["candidate_id"],),
                        )
                        message = f"{interview['name']} completed all interview rounds."
                    connection.execute(
                        "INSERT INTO notifications (candidate_id, message) VALUES (?, ?)",
                        (interview["candidate_id"], message),
                    )
                    candidate = candidate_payload(connection, interview["candidate_id"])
                return self.send_json(200, dict(candidate))

            if path.startswith("/api/interviews/") and path.endswith("/cancel"):
                interview_id = int(path.split("/")[-2])
                with connect_database() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    interview = connection.execute(
                        """SELECT i.*, c.name, c.status AS candidate_status, c.interviewer
                           FROM interviews i JOIN candidates c ON c.id = i.candidate_id
                           WHERE i.id = ? AND i.status = 'scheduled'""",
                        (interview_id,),
                    ).fetchone()
                    if not interview:
                        return self.send_json(404, {"error": "Scheduled interview not found."})
                    if user["role"] == "interviewer" and not user_is_on_interview(connection, interview_id, user["display_name"]):
                        return self.send_json(403, {"error": "Interviewers may only cancel their own assigned interviews."})
                    connection.execute("UPDATE interviews SET status = 'cancelled' WHERE id = ?", (interview_id,))
                    connection.execute(
                        "UPDATE candidates SET status = 'waiting' WHERE id = ?",
                        (interview["candidate_id"],),
                    )
                    candidate = connection.execute(
                        f"SELECT {FIELDS} FROM candidates WHERE id = ?", (interview["candidate_id"],)
                    ).fetchone()
                    replacement = find_assignments(connection, candidate, interview["interview_date"])
                    if replacement:
                        option = replacement[0]
                        connection.execute(
                            """UPDATE candidates SET status = 'scheduled', interviewer = ?,
                               interviewer_id = ?, room_id = ?, interview_time = ?
                               WHERE id = ?""",
                            (option["interviewer"], option["interviewer_id"], option["room_id"],
                             option["time"], interview["candidate_id"]),
                        )
                        connection.execute(
                            """UPDATE interviews SET interview_time = ?, interviewer_id = ?,
                               room_id = ?, status = 'scheduled' WHERE id = ?""",
                            (option["time"], option["interviewer_id"], option["room_id"], interview_id),
                        )
                        save_panel(connection, interview["candidate_id"], interview["round_number"], option["panel_ids"])
                        message = f"{interview['name']}'s interview was rescheduled to {option['interview_time'] if 'interview_time' in option else option['time']}."
                        response_message = "Interview automatically rescheduled to a conflict-free slot."
                    else:
                        connection.execute(
                            """UPDATE candidates SET interviewer = '', interviewer_id = 0,
                               room_id = 0, interview_date = '', interview_time = ''
                               WHERE id = ?""",
                            (interview["candidate_id"],),
                        )
                        connection.execute("DELETE FROM interview_panelists WHERE interview_id = ?", (interview_id,))
                        message = f"{interview['name']}'s interviewer cancelled. The candidate is back in the booking queue."
                        response_message = "No replacement slot was available; candidate returned to the queue."
                    connection.execute(
                        "INSERT INTO notifications (candidate_id, message) VALUES (?, ?)",
                        (interview["candidate_id"], message),
                    )
                return self.send_json(200, {"message": response_message})

            return self.send_json(404, {"error": "Endpoint not found."})
        except (ValueError, json.JSONDecodeError, TypeError) as error:
            return self.send_json(400, {"error": str(error)})

    def do_PATCH(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "candidates"]:
            return self.send_json(404, {"error": "Endpoint not found."})
        if self.require_roles("coordinator") is None:
            return
        try:
            candidate_id = int(parts[2])
            body = self.read_json()
            interview_date = str(body.get("date", ""))
            interview_time = str(body.get("time", ""))
            date.fromisoformat(interview_date)
            with connect_database() as connection:
                assignment = schedule_candidate(
                    connection, candidate_id, interview_date, interview_time,
                    str(body.get("interviewer", "")).strip() or None,
                    int(body["room_id"]) if body.get("room_id") else None,
                )
                candidate = dict(candidate_payload(connection, candidate_id))
            candidate.update(assignment)
            return self.send_json(200, candidate)
        except LookupError as error:
            return self.send_json(404, {"error": str(error)})
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
