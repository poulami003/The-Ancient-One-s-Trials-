"""Pure unit tests for scheduler.solve(): no database, no HTTP."""
import unittest

from scheduler import solve


def opt(time, interviewer_id, room_id, panel_ids=None):
    option = {"time": time, "interviewer_id": interviewer_id, "room_id": room_id}
    if panel_ids:
        option["panel_ids"] = panel_ids
    return option


class SolverTests(unittest.TestCase):
    def test_backtracks_so_everyone_is_placed(self):
        # Candidate 2 would grab 09:00 first, but candidate 1 can only use 09:00.
        options = {
            1: [opt("09:00", 1, 1)],
            2: [opt("09:00", 1, 1), opt("10:00", 1, 1)],
        }
        assignments, _ = solve(options)
        self.assertEqual(set(assignments), {1, 2})
        self.assertNotEqual(assignments[1]["time"], assignments[2]["time"])

    def test_returns_best_partial_when_no_full_solution_exists(self):
        options = {1: [opt("09:00", 1, 1)], 2: [opt("09:00", 1, 2)]}
        assignments, _ = solve(options)
        self.assertEqual(len(assignments), 1)

    def test_same_room_same_time_is_a_conflict(self):
        options = {1: [opt("09:00", 1, 1)], 2: [opt("09:00", 2, 1)]}
        assignments, _ = solve(options)
        self.assertEqual(len(assignments), 1)

    def test_daily_capacity_limits_one_interviewer(self):
        options = {
            1: [opt("09:00", 1, 1)],
            2: [opt("10:00", 1, 1)],
        }
        assignments, _ = solve(options, capacity={1: 1})
        self.assertEqual(len(assignments), 1)
        assignments, _ = solve(options, capacity={1: 2})
        self.assertEqual(len(assignments), 2)

    def test_capacity_pushes_work_to_another_interviewer(self):
        options = {
            1: [opt("09:00", 1, 1)],
            2: [opt("10:00", 1, 1), opt("10:00", 2, 1)],
        }
        assignments, _ = solve(options, capacity={1: 1, 2: 5})
        self.assertEqual(len(assignments), 2)
        self.assertEqual(assignments[2]["interviewer_id"], 2)

    def test_panel_reserves_every_member(self):
        panel = opt("09:00", 1, 1, panel_ids=[1, 2])
        options = {
            1: [panel],
            2: [opt("09:00", 2, 2)],  # interviewer 2 is already on the panel at 09:00
        }
        assignments, _ = solve(options)
        self.assertEqual(len(assignments), 1)

    def test_panel_counts_against_each_members_capacity(self):
        options = {
            1: [opt("09:00", 1, 1, panel_ids=[1, 2])],
            2: [opt("10:00", 2, 1)],
        }
        assignments, _ = solve(options, capacity={2: 1})
        self.assertEqual(len(assignments), 1)

    def test_node_limit_stops_search(self):
        options = {i: [opt(f"{9 + i % 8:02d}:00", i, i)] for i in range(40)}
        _, nodes = solve(options, node_limit=10)
        self.assertLessEqual(nodes, 12)

    def test_empty_input(self):
        self.assertEqual(solve({}), ({}, 1))


if __name__ == "__main__":
    unittest.main()
