"""Constraint-satisfaction scheduling core for TrialSync.

Two layers:
  * find_assignments(): reads availability from SQLite and returns every
    conflict-free (time, panel, room) option for one candidate.
  * solve(): pure backtracking search that picks one option per candidate so
    no interviewer or room is double-booked and no interviewer exceeds their
    daily capacity. It touches no database, so it can be unit-tested and
    benchmarked on plain data.

An option is a dict:
  time, room_id, room,
  interviewer_id / interviewer      -> the lead interviewer
  panel_ids / panel_names           -> every interviewer on the panel (lead first)
"""
import json
from datetime import date
from itertools import combinations, islice

TIMES = tuple(f"{hour:02d}:00" for hour in range(9, 17))
DEFAULT_NODE_LIMIT = 5000
DEFAULT_MAX_PER_DAY = 6
MAX_PANEL_OPTIONS_PER_SLOT = 12  # caps C(n, k) panel combinations tried per time slot


def candidate_is_available(connection, candidate_id, interview_date, interview_time):
    rows = connection.execute(
        "SELECT interview_time, available FROM candidate_availability "
        "WHERE candidate_id = ? AND interview_date = ?",
        (candidate_id, interview_date),
    ).fetchall()
    return not rows or any(row["interview_time"] == interview_time and row["available"] for row in rows)


def round_panel_size(connection, stage):
    """Number of interviewers a round needs (1 unless the coordinator raised it)."""
    row = connection.execute(
        "SELECT panel_size FROM interview_rounds WHERE name = ?", (stage,)
    ).fetchone()
    return max(1, int(row["panel_size"])) if row else 1


def interviewer_day_load(connection, interviewer_id, interview_date, ignore_candidate_id=None):
    """Scheduled interviews that day where the person is lead or a panelist."""
    return connection.execute(
        """SELECT COUNT(*) FROM interviews i
           WHERE i.status = 'scheduled' AND i.interview_date = ?
             AND i.candidate_id != COALESCE(?, -1)
             AND (i.interviewer_id = ?
                  OR EXISTS (SELECT 1 FROM interview_panelists p
                             WHERE p.interview_id = i.id AND p.interviewer_id = ?))""",
        (interview_date, ignore_candidate_id, interviewer_id, interviewer_id),
    ).fetchone()[0]


def remaining_capacity(connection, interview_date):
    """{interviewer_id: interviews they can still take on that date}."""
    return {
        row["id"]: max(0, row["max_per_day"] - interviewer_day_load(connection, row["id"], interview_date))
        for row in connection.execute("SELECT id, max_per_day FROM interviewers")
    }


def _interviewer_busy(connection, interviewer_id, interview_date, interview_time, ignore_candidate_id):
    as_lead = connection.execute(
        """SELECT 1 FROM candidates WHERE status = 'scheduled'
           AND interview_date = ? AND interview_time = ?
           AND id != COALESCE(?, -1) AND interviewer_id = ? LIMIT 1""",
        (interview_date, interview_time, ignore_candidate_id, interviewer_id),
    ).fetchone()
    if as_lead:
        return True
    as_panelist = connection.execute(
        """SELECT 1 FROM interview_panelists p JOIN interviews i ON i.id = p.interview_id
           WHERE i.status = 'scheduled' AND i.interview_date = ? AND i.interview_time = ?
             AND i.candidate_id != COALESCE(?, -1) AND p.interviewer_id = ? LIMIT 1""",
        (interview_date, interview_time, ignore_candidate_id, interviewer_id),
    ).fetchone()
    return bool(as_panelist)


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
    panel_size = round_panel_size(connection, candidate["stage"])
    interviewers = connection.execute(
        "SELECT id, name, skills, max_per_day FROM interviewers ORDER BY name"
    ).fetchall()
    rooms = connection.execute("SELECT id, name FROM rooms ORDER BY name").fetchall()
    skilled = [
        row for row in interviewers if skills.intersection(set(json.loads(row["skills"])))
    ]
    under_capacity = {
        row["id"]
        for row in skilled
        if interviewer_day_load(connection, row["id"], interview_date, ignore_candidate_id)
        < row["max_per_day"]
    }
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
        candidate_busy = connection.execute(
            """SELECT 1 FROM candidates WHERE status = 'scheduled'
               AND interview_date = ? AND interview_time = ?
               AND id != COALESCE(?, -1) AND id = ? LIMIT 1""",
            (interview_date, interview_time, ignore_candidate_id, candidate["id"]),
        ).fetchone()
        if candidate_busy:
            continue

        eligible = []
        for interviewer in skilled:
            if interviewer["id"] not in under_capacity:
                continue
            if ("interviewer", interviewer["id"], interview_time) in reserved:
                continue
            availability = connection.execute(
                """SELECT available FROM resource_availability
                   WHERE resource_type = 'interviewer' AND resource_id = ?
                     AND weekday = ? AND interview_time = ?""",
                (interviewer["id"], day.weekday(), interview_time),
            ).fetchone()
            if not availability or not availability["available"]:
                continue
            if _interviewer_busy(
                connection, interviewer["id"], interview_date, interview_time, ignore_candidate_id
            ):
                continue
            eligible.append(interviewer)
        if len(eligible) < panel_size:
            continue

        free_rooms = []
        for room in rooms:
            if ("room", room["id"], interview_time) in reserved:
                continue
            availability = connection.execute(
                """SELECT available FROM resource_availability
                   WHERE resource_type = 'room' AND resource_id = ?
                     AND weekday = ? AND interview_time = ?""",
                (room["id"], day.weekday(), interview_time),
            ).fetchone()
            if not availability or not availability["available"]:
                continue
            room_busy = connection.execute(
                """SELECT 1 FROM candidates WHERE status = 'scheduled'
                   AND interview_date = ? AND interview_time = ?
                   AND id != COALESCE(?, -1) AND room_id = ? LIMIT 1""",
                (interview_date, interview_time, ignore_candidate_id, room["id"]),
            ).fetchone()
            if not room_busy:
                free_rooms.append(room)

        panels = islice(combinations(eligible, panel_size), MAX_PANEL_OPTIONS_PER_SLOT)
        for panel in panels:
            for room in free_rooms:
                lead = panel[0]
                assignments.append({
                    "time": interview_time,
                    "interviewer_id": lead["id"],
                    "interviewer": lead["name"],
                    "panel_ids": [member["id"] for member in panel],
                    "panel_names": [member["name"] for member in panel],
                    "room_id": room["id"],
                    "room": room["name"],
                })
    return assignments


def option_interviewer_ids(option):
    return option.get("panel_ids") or [option["interviewer_id"]]


def solve(option_map, node_limit=DEFAULT_NODE_LIMIT, capacity=None):
    """Assign at most one option per candidate without resource clashes.

    option_map: {candidate_id: [option, ...]}.
    capacity:   optional {interviewer_id: interviews still allowed that day};
                interviewers missing from the dict are unlimited.
    Candidates with the fewest options are placed first (most-constrained-first),
    then depth-first backtracking tries each option. A candidate may be skipped
    if no option fits. Search stops early on a full assignment or after
    node_limit nodes (best partial result so far is kept).
    Returns (assignments, nodes_visited).
    """
    ordered_ids = sorted(option_map, key=lambda cid: len(option_map[cid]))
    reserved = set()
    used = {}
    assignments = {}
    best = {}
    nodes = 0

    def fits(option):
        time_slot = option["time"]
        if ("room", option["room_id"], time_slot) in reserved:
            return False
        for interviewer_id in option_interviewer_ids(option):
            if ("interviewer", interviewer_id, time_slot) in reserved:
                return False
            if capacity is not None and interviewer_id in capacity:
                if used.get(interviewer_id, 0) >= capacity[interviewer_id]:
                    return False
        return True

    def take(option, sign):
        time_slot = option["time"]
        keys = [("room", option["room_id"], time_slot)]
        keys += [("interviewer", i, time_slot) for i in option_interviewer_ids(option)]
        for key in keys:
            if sign > 0:
                reserved.add(key)
            else:
                reserved.discard(key)
        for interviewer_id in option_interviewer_ids(option):
            used[interviewer_id] = used.get(interviewer_id, 0) + sign

    def search(index):
        nonlocal nodes, best
        nodes += 1
        if len(assignments) > len(best):
            best = dict(assignments)
        if index == len(ordered_ids) or nodes > node_limit:
            return
        candidate_id = ordered_ids[index]
        for option in option_map[candidate_id]:
            if not fits(option):
                continue
            take(option, +1)
            assignments[candidate_id] = option
            search(index + 1)
            if len(best) == len(ordered_ids):
                return
            assignments.pop(candidate_id, None)
            take(option, -1)
            if nodes > node_limit:
                return
        search(index + 1)

    search(0)
    return best, nodes
