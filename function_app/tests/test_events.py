"""
Unit tests for the Service Hook gate + payload normalization. Stdlib unittest,
no ADO connection. Run with the rest via:

    cd function_app && python3 -m unittest discover -s tests -v
"""

import unittest

from healer import events

# Trimmed but representative ADO 'build.complete' Service Hook payload.
SAMPLE_EVENT = {
    "eventType": "build.complete",
    "resource": {
        "id": 4567,
        "result": "failed",
        "status": "completed",
        "sourceBranch": "refs/heads/main",
        "sourceVersion": "deadbeefcafe1234",
        "definition": {"id": 42, "name": "payments-api-ci"},
        "project": {"id": "proj-guid", "name": "Payments"},
        "repository": {"id": "repo-guid", "name": "payments-api", "type": "TfsGit"},
    },
}


class TestAllowlist(unittest.TestCase):
    def test_parse_allowlist_handles_commas_spaces_and_empty(self):
        self.assertEqual(events.parse_allowlist("42, 58 103"),
                         {"42", "58", "103"})
        self.assertEqual(events.parse_allowlist(""), set())
        self.assertEqual(events.parse_allowlist("  "), set())

    def test_empty_allowlist_monitors_everything(self):
        self.assertTrue(events.is_monitored(999, "anything", set()))

    def test_match_by_id_or_name(self):
        allow = {"42", "billing-ci"}
        self.assertTrue(events.is_monitored(42, "payments-api-ci", allow))   # by id
        self.assertTrue(events.is_monitored(7, "billing-ci", allow))         # by name
        self.assertFalse(events.is_monitored(7, "unlisted-ci", allow))       # neither


class TestFailedBuildGate(unittest.TestCase):
    def test_failed_build_detected(self):
        self.assertTrue(events.is_failed_build(SAMPLE_EVENT))

    def test_non_failure_ignored(self):
        ok = {"resource": {"result": "succeeded"}}
        self.assertFalse(events.is_failed_build(ok))
        self.assertFalse(events.is_failed_build({}))  # malformed → not failed


class TestNormalize(unittest.TestCase):
    def test_maps_all_fields_and_strips_ref(self):
        payload = events.normalize_service_hook(
            SAMPLE_EVENT, "https://dev.azure.com/contoso/")
        self.assertEqual(payload, {
            "org": "https://dev.azure.com/contoso/",
            "project": "Payments",
            "repo": "repo-guid",
            "buildId": 4567,
            "commit": "deadbeefcafe1234",
            "branch": "main",                 # refs/heads/ stripped
            "pipelineId": 42,
            "pipelineName": "payments-api-ci",
        })

    def test_feature_branch_ref_stripped_to_full_name(self):
        ev = {"resource": {"sourceBranch": "refs/heads/feature/x"}}
        self.assertEqual(
            events.normalize_service_hook(ev, "org")["branch"], "feature/x")

    def test_missing_pieces_do_not_raise(self):
        payload = events.normalize_service_hook({}, "org")  # empty event
        self.assertEqual(payload["org"], "org")
        self.assertIsNone(payload["buildId"])
        self.assertEqual(payload["branch"], "main")  # default when no branch


if __name__ == "__main__":
    unittest.main()
