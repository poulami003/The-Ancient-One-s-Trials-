# TrialSync — Constraint-Satisfaction Recruitment Scheduler

TrialSync is a local campus-recruitment scheduling prototype. It coordinates interview rounds for candidates while checking interviewer skills and availability, candidate availability, and room availability.

## Run locally

1. Open this folder in VS Code.
2. In the integrated terminal, run:

   ```powershell
   python server.py
   ```

3. Open <http://127.0.0.1:8000/login.html> in a browser (or <http://127.0.0.1:8000/>).
4. To stop the server, focus the terminal and press `Ctrl+C`.

The Python standard library and SQLite are used; no package installation is required. Data is stored in `trials.db`. The server creates its tables and sample data on its first run and preserves existing candidate records when adding the scheduler tables.

Open the sign-in page directly at `/login.html`; the root URL `/` opens that same page. The public landing page remains available at `/home.html`.

## Database structure

The idempotent `initialize_database()` migration creates and seeds these core tables:

- `candidates` — candidate identity, program, batch, current pipeline round, status, and booking details.
- `interviewers` — interviewer identities and their round-skill lists.
- `rooms` — available interview room records.
- `interview_rounds` — the ordered active pipeline catalog (Coding → Technical → HR).
- `interviews` — a candidate's scheduled instance of a round; references the candidate, pipeline round, interviewer, and room.

The related `candidate_availability`, `resource_availability`, and `evaluations` tables store booking constraints and round results. Existing candidate-only SQLite files are upgraded in place; existing candidates and scheduled interviews are retained. Database foreign keys and migration-safe triggers reject scheduled rounds or resources that do not exist.

## Included workflows

- **Coordinator:** add candidates, inspect the day schedule, assign a conflict-free slot, run the constraint-based auto-planner, create candidate booking links, export an iCalendar (`.ics`) file, and cancel an interview to trigger automatic replacement-slot search.
- **Candidate:** sign in to their own recruitment dashboard, view their round history and status, set available hours for a date, see only compatible open slots, and book their next interviewer and room. Coordinator-issued booking links are also supported.
- **Interviewer:** sign in to their own interview desk, view only their assigned interviews, enter a 1–5 score and feedback, and pass or fail candidates in their rounds. A pass advances the candidate through Coding → Technical → HR. Failing a round marks the candidate as rejected; passing HR completes the pipeline.
- **Role-based login:** the sign-in identity is stored in a salted PBKDF2 password hash, with an eight-hour signed, HttpOnly, SameSite session cookie. Protected endpoints enforce the signed-in role; candidates and interviewers only see their own records.
- **Evaluation comparison:** view candidates ordered by their average evaluation score.
- **Availability management API:** set interviewer or room availability by weekday, in one-hour slots from 09:00 through 16:00.
- **Notifications:** scheduling, progression, completion, and rescheduling events are available in the in-app notification feed.

The sample interviewer and room calendars are initially open Monday through Friday. Auto-planning orders candidates by priority and the number of compatible options, then searches for assignments without overlapping an interviewer or room.

## Demo sign-in accounts

The local database provisions three accounts on first run:

| Role | Email | Password |
|---|---|---|
| Coordinator | `coordinator@trialsync.local` | `Coordinator123!` |
| Interviewer | `interviewer@trialsync.local` | `Interviewer123!` |
| Candidate | `candidate@trialsync.local` | `Candidate123!` |

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. Login creates an eight-hour, signed, HttpOnly, SameSite session cookie, and the API checks the role and resource ownership on protected endpoints. The sample credentials are public demo credentials, not suitable for deployment.

## API overview

- `GET /api/candidates?date=YYYY-MM-DD` — day schedule and active queue
- `GET /api/resources` — interviewer skills, rooms, and pipeline stages
- `GET /api/interviewers`, `GET /api/rooms`, `GET /api/interview-rounds` — persisted scheduling resources and round catalog
- `POST /api/auto-plan` — schedule as many waiting candidates as the constraints allow
- `POST /api/candidates` — add a candidate
- `PATCH /api/candidates/{id}` — assign a candidate to a date and optional time
- `POST /api/candidates/{id}/booking-link` — create a candidate booking link
- `GET /api/booking/{token}?date=YYYY-MM-DD` — list available booking slots
- `POST /api/booking/{token}/availability` — save candidate availability
- `POST /api/booking/{token}` — confirm a candidate booking
- `POST /api/availability` — update an interviewer's or room's weekday availability
- `POST /api/interviews/{id}/evaluate` — record a rubric score, feedback, and decision
- `POST /api/interviews/{id}/cancel` — cancel and search for a replacement slot
- `POST /api/interview-rounds/panel-size` — coordinator sets how many interviewers a round needs (`{"round_number": 1, "panel_size": 2}`; default 1)
- `POST /api/interviewers/capacity` — coordinator sets an interviewer's maximum interviews per day (`{"interviewer_id": 1, "max_per_day": 4}`; default 6)
- `GET /api/rankings` and `GET /api/notifications` — evaluation comparison and in-app alerts

For `/api/availability`, `weekday` uses Python's convention (Monday `0`, Sunday `6`), and `times` is a list such as `["09:00", "10:00"]`.

## Panels and capacity

A round can require a panel of several interviewers (`interview_rounds.panel_size`). The scheduler only offers slots where every panelist has the skill, is available and is not already booked (as lead or panelist) at that hour. Each interviewer also has a daily limit (`interviewers.max_per_day`); the auto-planner counts every panel seat against it. The lead interviewer is stored on `interviews`, the other panelists in `interview_panelists`.

## Verify

Run the focused backend integration tests from the project folder:

```powershell
python -m unittest discover -s tests -v
```

## Scope and limitations

This is a local demonstration, not a production deployment. The seeded demo accounts are publicly documented; there is no self-service registration, password reset, or production account administration. If `TRIALSYNC_SESSION_SECRET` is not set, the server generates a temporary signing key, so sessions expire on restart. Set a stable key before starting the server if you need sessions to survive a restart:

```powershell
$env:TRIALSYNC_SESSION_SECRET = "replace-with-a-long-random-secret"
python server.py
```

The seeded recurring availability is weekday-based; email delivery and external Google Calendar synchronization are not configured. Candidate booking uses an unguessable local-demo token, and the calendar feature downloads `.ics` files. Use HTTPS, rotate the demo credentials, and configure a strong signing key before any non-local deployment.
