"""
Unit tests for the orchestration logic. Stdlib `unittest` only — no pytest, no
Azure/ADO/LLM credentials, no third-party packages. Run:

    cd function_app && python3 -m unittest discover -s tests -v

External I/O (ADO REST, the model) is replaced with fakes, so these tests prove
the wiring and decision logic: failure parsing, code-file filtering, tolerant
JSON parsing, PR payload shapes, provider dispatch, the loop-breaker, and the
three orchestrate outcomes (PR opened / no confident fix / skipped).
"""

import unittest

from healer import core, providers


class FakeAdo:
    """Records calls and returns canned ADO responses."""

    def __init__(self, timeline=None, changes=None):
        self._timeline = timeline if timeline is not None else [
            {"type": "Task", "result": "failed", "name": "Run tests",
             "log": {"id": 7}},
        ]
        self._changes = changes if changes is not None else [
            {"changeType": "edit", "item": {"path": "/src/app.js"}},
        ]
        self.opened = []

    def get_failed_steps(self, build_id):
        return core.parse_failed_steps(self._timeline)

    def get_log_tail(self, build_id, log_id):
        return f"log for {log_id}: AssertionError line 42"

    def get_changed_files(self, commit):
        return self._changes

    def get_file_content(self, path, branch):
        return f"// contents of {path} on {branch}"

    def open_fix_pr(self, base_branch, base_commit, new_branch, files, summary):
        self.opened.append({"base": base_branch, "new": new_branch,
                            "files": files, "summary": summary})
        return "https://dev.azure.com/org/proj/_git/repo/pullrequest/123"


class TestPureHelpers(unittest.TestCase):
    def test_loop_breaker_detects_ai_branches(self):
        self.assertTrue(core.is_ai_branch("ai-fix/deadbeef"))
        self.assertTrue(core.is_ai_branch("refs/heads/ai-fix/deadbeef"))
        self.assertFalse(core.is_ai_branch("main"))
        self.assertFalse(core.is_ai_branch(""))

    def test_parse_failed_steps_keeps_only_failed_tasks(self):
        records = [
            {"type": "Task", "result": "failed", "name": "a"},
            {"type": "Task", "result": "succeeded", "name": "b"},
            {"type": "Stage", "result": "failed", "name": "c"},  # not a Task
        ]
        got = core.parse_failed_steps(records)
        self.assertEqual([r["name"] for r in got], ["a"])

    def test_filter_code_files_excludes_deletes_and_non_code(self):
        changes = [
            {"changeType": "edit", "item": {"path": "/src/app.py"}},
            {"changeType": "delete", "item": {"path": "/src/gone.py"}},
            {"changeType": "edit", "item": {"path": "/README.md"}},   # not code
            {"changeType": "add", "item": {"path": "/infra/main.bicep"}},
        ]
        self.assertEqual(core.filter_code_files(changes),
                         ["/src/app.py", "/infra/main.bicep"])

    def test_extract_fix_json_plain_fenced_and_bad(self):
        plain = core.extract_fix_json('{"summary": "fix", "files": []}')
        self.assertEqual(plain["summary"], "fix")

        fenced = core.extract_fix_json(
            '```json\n{"summary": "fix2", "files": [{"path": "a", "content": "x"}]}\n```')
        self.assertEqual(fenced["summary"], "fix2")
        self.assertEqual(len(fenced["files"]), 1)

        bad = core.extract_fix_json("sorry, I cannot help")
        self.assertEqual(bad["files"], [])  # never raises

        empty = core.extract_fix_json("")
        self.assertEqual(empty["files"], [])

    def test_extract_fix_json_coerces_bad_files_type(self):
        got = core.extract_fix_json('{"summary": "x", "files": "not a list"}')
        self.assertEqual(got["files"], [])

    def test_push_payload_shape(self):
        payload = core.build_push_payload(
            "ai-fix/abc", "SHA123",
            [{"path": "/a.py", "content": "print(1)"}], "fix it")
        self.assertEqual(payload["refUpdates"][0]["name"], "refs/heads/ai-fix/abc")
        self.assertEqual(payload["refUpdates"][0]["oldObjectId"], "SHA123")
        change = payload["commits"][0]["changes"][0]
        self.assertEqual(change["changeType"], "edit")
        self.assertEqual(change["item"]["path"], "/a.py")
        self.assertEqual(change["newContent"]["content"], "print(1)")

    def test_pr_payload_shape(self):
        pr = core.build_pr_payload("ai-fix/abc", "main", "fix it", "deadbeefcafe")
        self.assertEqual(pr["sourceRefName"], "refs/heads/ai-fix/abc")
        self.assertEqual(pr["targetRefName"], "refs/heads/main")
        self.assertIn("[AI self-heal]", pr["title"])
        self.assertIn("deadbeef", pr["description"])


class TestProviderDispatch(unittest.TestCase):
    def test_dispatch_selects_provider_without_importing_sdks(self):
        self.assertIs(providers.make_request_fix("azure_openai"),
                      providers._azure_openai_fix)
        self.assertIs(providers.make_request_fix("claude_foundry"),
                      providers._claude_foundry_fix)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            providers.make_request_fix("gpt5-turbo-ultra")


class TestOrchestrate(unittest.TestCase):
    def _notifier(self):
        sent = []
        return sent, (lambda text: sent.append(text))

    def test_happy_path_opens_pr(self):
        ado = FakeAdo()
        sent, notify = self._notifier()
        fix = {"summary": "pin dependency", "files": [{"path": "/src/app.js",
                                                        "content": "fixed"}]}
        result = core.orchestrate(
            {"buildId": 55, "commit": "deadbeefcafe", "branch": "main"},
            ado, lambda ctx: fix, notify)

        self.assertEqual(result["pr"],
                         "https://dev.azure.com/org/proj/_git/repo/pullrequest/123")
        self.assertEqual(len(ado.opened), 1)
        self.assertEqual(ado.opened[0]["new"], "ai-fix/deadbeef")  # short SHA branch
        self.assertTrue(sent and "PR opened" in sent[0])

    def test_context_passed_to_model_has_logs_and_sources(self):
        ado = FakeAdo()
        captured = {}

        def capture(ctx):
            captured.update(ctx)
            return {"summary": "no confident fix", "files": []}

        core.orchestrate({"buildId": 1, "commit": "abcdef123456", "branch": "main"},
                         ado, capture, lambda t: None)
        self.assertIn("Run tests", captured["logs"])
        self.assertIn("AssertionError", captured["logs"])
        self.assertIn("/src/app.js", captured["changedFiles"])
        self.assertEqual(captured["failedSteps"], ["Run tests"])

    def test_no_confident_fix_opens_no_pr(self):
        ado = FakeAdo()
        sent, notify = self._notifier()
        result = core.orchestrate(
            {"buildId": 9, "commit": "abcdef123456", "branch": "main"},
            ado, lambda ctx: {"summary": "no confident fix", "files": []}, notify)

        self.assertIsNone(result["pr"])
        self.assertEqual(ado.opened, [])
        self.assertTrue(sent and "Manual review" in sent[0])

    def test_loop_breaker_skips_ai_branch_without_touching_ado(self):
        ado = FakeAdo()
        sent, notify = self._notifier()

        def boom(ctx):
            raise AssertionError("model must not be called on an AI branch")

        result = core.orchestrate(
            {"buildId": 3, "commit": "abc", "branch": "ai-fix/deadbeef"},
            ado, boom, notify)

        self.assertEqual(result, {"skipped": "ai-branch"})
        self.assertEqual(ado.opened, [])
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
