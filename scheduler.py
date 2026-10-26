"""Constraint-satisfaction scheduling core for TrialSync.

Two layers:
  * find_assignments(): reads availability from SQLite and returns every
    conflict-free (time, interviewer, room) option for one candidate.
  * solve(): pure backtracking search that picks one option per candidate so
    no interviewer or room is double-booked. It touches no database, so it can
    be unit-tested and benchmarked on plain data.
"""
import json
from datetime import date

TIMES = tuple(f"{hour:02d}:00" for hour in range(9, 17))
DEFAULT_NODE_LIMIT = 5000


def candidate_is_available(connection, candidate_id, interview_date, interview_time):
    rows = connection.execute(
        "SELECT interview_time, available FROM candidate_availability "
        "WHERE candidate_id = ? AND interview_date = ?",
        (candidate_id, interview_date),
    ).fetchall()
    return not rows or any(row["interview_time"] == interview_time and row["available"] for row in rows)


def find_assignments(
    connection,
    candidate,
    interview_date,
    preferred_time=None,
    reserved=None,
    ignore_candidate_id=None,
    require_candidate_availability=False,
):
    day = date.fromisoformat(interview_date)
    reserved = reserved or set()
    times = (preferred_time,) if preferred_time else TIMES
    skills = {candidate["stage"], candidate["stage"].replace(" screen", "")}
    interviewers = connection.execute("SELECT id, name, skills FROM interviewers ORDER BY name").fetchall()
    rooms = connection.execute("SELECT id, name FROM rooms ORDER BY name").fetchall()
    assignments = []
    for interview_time in times:
        if interview_time not in TIMES:
            continue
        if require_candidate_availability:
            rows = connection.execute(
                "SELECT interview_time, available FROM candidate_availability "
                "WHERE candidate_id = ? AND interview_date = ?",
                (candidate["id"], interview_date),
            ).fetchall()
            candidate_available = any(
                row["interview_time"] == interview_time and row["available"] for row in rows
            )
        else:
            candidate_available = candidate_is_available(
                connection, candidate["id"], interview_date, interview_time
            )
        if not candidate_available:
            continue
        for interviewer in interviewers:
            interviewer_skills = set(json.loads(interviewer["skills"]))
            if not skills.intersection(interviewer_skills):
                continue
            interviewer_key = ("interviewer", interviewer["id"], interview_time)
            if interviewer_key in reserved:
                continue
            interviewer_available = connection.execute(
                """SELECT available FROM resource_availability
                   WHERE resource_type = 'interviewer' AND resource_id = ?
                     AND weekday = ? AND interview_time = ?""",
                (interviewer["id"], day.weekday(), interview_time),
            ).fetchone()
            if not interviewer_available or not interviewer_available["available"]:
                continue
            for room in rooms:
                room_key = ("room", room["id"], interview_time)
                if room_key in reserved:
                    continue
                room_available = connection.execute(
                    """SELECT available FROM resource_availability
                       WHERE resource_type = 'room' AND resource_id = ?
                         AND weekday = ? AND interview_time = ?""",
                    (room["id"], day.weekday(), interview_time),
                ).fetchone()
                if not room_available or not room_available["available"]:
                    continue
                conflict = connection.execute(
                    """SELECT 1 FROM candidates WHERE status = 'scheduled'
                       AND interview_date = ? AND interview_time = ?
                       AND id != COALESCE(?, -1)
                       AND (interviewer_id = ? OR room_id = ? OR id = ?)
                       LIMIT 1""",
                    (interview_date, interview_time, ignore_candidate_id,
                     interviewer["id"], room["id"], candidate["id"]),
                ).fetchone()
                if conflict:
                    continue
                assignments.append({
                    "time": interview_time,
                    "interviewer_id": interviewer["id"],
                    "interviewer": interviewer["name"],
                    "room_id": room["id"],
                    "room": room["name"],
                })
    return assignments


def solve(option_map, node_limit=DEFAULT_NODE_LIMIT):
    """Assign at most one option per candidate without resource clashes.

    option_map: {candidate_id: [option, ...]} where each option is a dict with
    "time", "interviewer_id" and "room_id".
    Candidates with the fewest options are placed first (most-constrained-first),
    then depth-first backtracking tries each option. A candidate may be skipped
    if no option fits. Search stops early on a full assignment or after
    node_limit nodes (best partial result so far is kept).
    Returns (assignments, nodes_visited).
    """
    ordered_ids = sorted(option_map, key=lambda cid: len(option_map[cid]))
    reserved = set()
    assignments = {}
    best = {}
    nodes = 0

    def search(index):
        nonlocal nodes, best
        nodes += 1
        if len(assignments) > len(best):
            best = dict(assignments)
        if index == len(ordered_ids) or nodes > node_limit:
            return
        candidate_id = ordered_ids[index]
        for option in option_map[candidate_id]:
            interviewer_key = ("interviewer", option["interviewer_id"], option["time"])
            room_key = ("room", option["room_id"], option["time"])
            if interviewer_key in reserved or room_key in reserved:
                continue
            reserved.update((interviewer_key, room_key))
            assignments[candidate_id] = option
            search(index + 1)
            if len(best) == len(ordered_ids):
                return
            assignments.pop(candidate_id, None)
            reserved.discard(interviewer_key)
            reserved.discard(room_key)
            if nodes > node_limit:
                return
        search(index + 1)

    search(0)
    return best, nodes
