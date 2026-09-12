import codecs
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import repair


class RepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "skill" / "SKILL.md"
        self.source.parent.mkdir()
        self.source.write_text("Read first page.\n", encoding="utf-8")
        self.trace = self.root / "session.jsonl"
        self.trace.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"text": "Read all records."}]}}), encoding="utf-8")
        self.case = Path(repair.inspect_session(str(self.trace), self.root, self.root / "state")["case"])
        self.plan = self.root / "plan.json"
        repair.save(self.plan, [{"name": kind, "kind": kind, "check": "Check the expected records."}
                                for kind in ("source", "similar", "regression")])

    def prepare(self, sources=None):
        manifest = repair.stage(self.case, sources or [self.source], self.plan)
        for entry in manifest["files"]:
            candidate = self.case / "candidate" / entry["name"]
            candidate.write_text(candidate.read_text(encoding="utf-8-sig").replace("first", "every"), encoding="utf-8")
        revision = repair.seal(self.case)["revision"]
        return {"revision": revision, "reviewed": True, "checks": [
            {"name": kind, "before": [kind == "regression"], "after": [True], "evidence": "Recorded local check."}
            for kind in ("source", "similar", "regression")]}

    def test_cli_apply_and_rollback(self):
        before = self.source.read_bytes()
        results = self.prepare()
        file = self.root / "results.json"
        repair.save(file, results)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main(["apply", str(self.case), "--results", str(file)]), 0)
        self.assertIn("every", self.source.read_text())
        self.assertEqual(repair.update_files(self.case, rollback=True)["status"], "rolled_back")
        self.assertEqual(self.source.read_bytes(), before)

    def test_stage_preserves_relative_layout(self):
        helper = self.source.parent / "scripts" / "helper.py"
        helper.parent.mkdir()
        helper.write_text("value = 'first'", encoding="utf-8")
        self.prepare([self.source, helper])
        manifest = repair.read(self.case / "case.json")
        names = [e["name"] for e in manifest["files"]]
        self.assertEqual(names, ["00/SKILL.md", "00/scripts/helper.py"])
        self.assertIn("first", helper.read_text())

    def test_archive_uuid_and_private_reasoning(self):
        sid = "00000000-0000-0000-0000-000000000001"
        archive = self.root / "archived_sessions"
        archive.mkdir()
        log = archive / f"rollout-{sid}.jsonl"
        log.write_text(self.trace.read_text() + '\n' + json.dumps({"type": "response_item", "payload": {
            "type": "reasoning", "encrypted_content": "private-reasoning"}}) + '\n{broken', encoding="utf-8")
        result = repair.inspect_session(sid, self.root, self.root / "state")
        data = repair.read(Path(result["session"]))
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["gaps"][0]["reason"], "malformed_record")
        self.assertNotIn("private-reasoning", json.dumps(data))

    def test_no_changes_during_staging_and_failed_validation(self):
        before = self.source.read_bytes()
        results = self.prepare()
        results["checks"][0]["after"] = [False]
        with self.assertRaises(ValueError):
            repair.update_files(self.case, results)
        self.assertEqual(self.source.read_bytes(), before)

    def test_incomplete_or_regressed_results_cannot_apply(self):
        results = self.prepare()
        for kind in ("missing", "regression", "not_reviewed", "non_boolean", "no_evidence"):
            invalid = json.loads(json.dumps(results))
            if kind == "missing":
                invalid["checks"].pop()
            elif kind == "regression":
                invalid["checks"][2]["after"] = [False]
            elif kind == "not_reviewed":
                invalid["reviewed"] = False
            elif kind == "non_boolean":
                invalid["checks"][0]["after"] = [1]
            else:
                invalid["checks"][0]["evidence"] = ""
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                repair.update_files(self.case, invalid)

    def test_changed_source_and_changed_candidate_are_rejected(self):
        results = self.prepare()
        self.source.write_text("Another edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            repair.update_files(self.case, results)
        self.source.write_text("Read first page.\n", encoding="utf-8")
        (self.case / "candidate/00/SKILL.md").write_text("A different candidate", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Candidate changed"):
            repair.update_files(self.case, results)

    def test_reseal_and_changed_plan_invalidate_results(self):
        results = self.prepare()
        repair.seal(self.case)
        with self.assertRaisesRegex(ValueError, "revision"):
            repair.update_files(self.case, results)
        results["revision"] = repair.read(self.case / "case.json")["revision"]
        repair.save(self.case / "plan.json", [])
        with self.assertRaisesRegex(ValueError, "plan changed"):
            repair.update_files(self.case, results)

    def test_rollback_preserves_later_edits(self):
        repair.update_files(self.case, self.prepare())
        self.source.write_text("A later edit", encoding="utf-8")
        self.assertEqual(repair.update_files(self.case, rollback=True)["status"], "rollback_conflict")
        self.assertEqual(self.source.read_text(), "A later edit")

    def test_partial_write_failure_restores_written_files(self):
        other = self.source.parent / "helper.txt"
        other.write_text("first", encoding="utf-8")
        results = self.prepare([self.source, other])
        write = repair.write

        def fail(path, data, mode=None):
            if path == other:
                raise OSError("Simulated failure")
            return write(path, data, mode)

        with patch.object(repair, "write", side_effect=fail), self.assertRaises(OSError):
            repair.update_files(self.case, results)
        self.assertEqual(self.source.read_text(), "Read first page.\n")
        self.assertEqual(repair.read(self.case / "case.json")["status"], "rolled_back")

    def test_interrupted_apply_can_be_rolled_back(self):
        results = self.prepare()
        write = repair.write

        def interrupt(path, data, mode=None):
            write(path, data, mode)
            if path == self.source:
                raise KeyboardInterrupt()

        with patch.object(repair, "write", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            repair.update_files(self.case, results)
        self.assertEqual(repair.read(self.case / "case.json")["status"], "applying")
        self.assertEqual(repair.update_files(self.case, rollback=True)["status"], "rolled_back")
        self.assertEqual(self.source.read_text(), "Read first page.\n")

    def test_utf8_bom_survives_edit_and_application(self):
        self.source.write_bytes(codecs.BOM_UTF8 + "日本語 first\n".encode())
        repair.update_files(self.case, self.prepare())
        self.assertTrue(self.source.read_bytes().startswith(codecs.BOM_UTF8))
        self.assertEqual(self.source.read_text(encoding="utf-8-sig"), "日本語 every\n")

    def test_no_state_in_repository_or_credential_targets(self):
        with self.assertRaises(ValueError):
            repair.private(repair.REPO / "cases")
        secret = self.root / ".env"
        secret.write_text("private", encoding="utf-8")
        with self.assertRaises(ValueError):
            repair.stage(self.case, [secret], self.plan)

    def test_copy_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            repair.copy_file(self.case, "candidate", {"name": "../../outside.txt"})

    def test_same_target_cannot_be_locked_twice(self):
        with repair.lock(self.source):
            with self.assertRaises(OSError):
                with repair.lock(self.source):
                    self.fail("Lock was not exclusive")


if __name__ == "__main__":
    unittest.main()
