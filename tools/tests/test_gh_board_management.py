import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gh_board  # noqa: E402

gh_board.SHARED_PROJECT = 46   # the test-queue project of these fixtures (config key GH_BOARD_TEST_QUEUE)


def issue(body="", state="OPEN", sub=None, boards=()):
    return {"body": body, "state": state, "subIssuesSummary": sub,
            "projectItems": {"nodes": [{"project": {"number": n}, "status": {"name": s}} for n, s in boards]}, "_self_project": 47}


class ManagementCompute(unittest.TestCase):
    def test_checkboxes_only(self):
        self.assertEqual(gh_board.compute_management(issue("- [x] a\n- [ ] b\n* [X] c\n1. [ ] d")), (50, "", "Todo"))

    def test_sub_issues_add_to_both_sides(self):
        self.assertEqual(gh_board.compute_management(issue("- [x] a\n- [ ] b", sub={"total": 2, "completed": 1}))[0], 50)
        self.assertEqual(gh_board.compute_management(issue("- [x] a\n- [ ] b", sub={"total": 2, "completed": 2}))[0], 75)

    def test_sub_issue_contributes_its_own_percent(self):
        child = issue("- [x] a\n- [ ] b\n- [ ] c\n- [ ] d")                       # 25%
        parent = issue("- [x] p1\n- [ ] p2", sub={"total": 3, "completed": 1})
        parent["subIssues"] = {"nodes": [child, issue("", state="CLOSED"), issue("")]}   # 25%, 100%, 0% (no checklist)
        self.assertEqual(gh_board.compute_management(parent)[0], round(100 * (1 + 0.25 + 1 + 0) / 5))   # 45%

    def test_umbrella_with_only_sub_issues_and_nesting(self):
        child = issue("", sub={"total": 1, "completed": 0})
        child["subIssues"] = {"nodes": [issue("- [x] a\n- [x] b")]}
        parent = issue("", sub={"total": 1, "completed": 0})
        parent["subIssues"] = {"nodes": [child]}
        self.assertEqual(gh_board.compute_management(parent)[0], 100)

    def test_deepest_level_counts_closed_open_and_extra_sub_issues_not_started(self):
        leaf = issue("", sub={"total": 2, "completed": 1})      # no fetched sub-issues: 1 of 2
        self.assertEqual(gh_board.compute_management(leaf)[0], 50)
        parent = issue("", sub={"total": 4, "completed": 1})
        parent["subIssues"] = {"nodes": [issue("", state="CLOSED"), issue("- [x] a")]}
        self.assertEqual(gh_board.compute_management(parent)[0], 50)   # 2 of 4, the two unfetched count as not started

    def test_no_checklist_is_zero_and_flagged(self):
        self.assertEqual(gh_board.compute_management(issue("only prose [ ] not a list")), (0, "checklist yok", "Todo"))
        self.assertEqual(gh_board.compute_management(issue(None))[1], "checklist yok")

    def test_code_fence_ignored(self):
        self.assertEqual(gh_board.checklist_counts("- [x] a\n```\n- [ ] sample\n```\n- [ ] b"), (1, 2))

    def test_closed_or_done_is_100(self):
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", state="CLOSED")), (100, "", "Done"))
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(9, "✅ Done")])), (100, "", "Done"))

    def test_status_from_product_board_ignores_own_project(self):
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(9, "👀 In review"), (47, "Done")]))[2], "In review")
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(11, "Yapay Zeka")]))[2], "Todo")
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(11, "In Progress")]))[2], "In Progress")

    def test_shared_queue_never_sets_status(self):
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(11, "In Progress"), (46, "In review")]))[2], "In Progress")
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(11, "⏳ Cevap bekleniyor")]))[2], "In Progress")
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(11, "In Progress"), (46, "Done")])), (0, "", "In Progress"))
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", boards=[(46, "In review")]))[2], "Todo")
        self.assertEqual(gh_board.compute_management(issue("- [ ] a", state="CLOSED", boards=[(46, "In review")]))[2], "Done")


if __name__ == "__main__":
    unittest.main()
