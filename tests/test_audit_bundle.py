"""Offline tests for the read-only local bundle audit tool."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audit_bundle as audit


class ScannerTests(unittest.TestCase):
    def test_literals_ignore_comments_regex_and_decode_escapes(self):
        source = r'''// "Ignored Comment"
/* 'Ignored Block' */ const a="Don\\'t \u0046ail"; const b='A\x20Button';
const c=/["']nope["']/g; const d=`plain \` quote`;'''
        result = audit._scan_bundle(source)
        texts = [item["text"] for item in result]
        self.assertEqual(texts, ["Don\\'t Fail", "A Button", "plain ` quote"])

    def test_regex_after_control_parenthesis_is_skipped(self):
        for keyword in ("if", "while"):
            with self.subTest(keyword=keyword):
                source = f'{keyword} (ok) /["\\\']secret["\\\']/.test(value); const after="After";'
                result = audit._scan_bundle(source)
                self.assertEqual([item["text"] for item in result], ["After"])

    def test_regex_after_arrow_and_inside_template_expression_is_skipped(self):
        source = '''const matcher = value => /["']secret["']/.test(value);
const template = `${items.map(value => /["']hidden["']/.test(value))}`;
const after = "After";'''
        result = audit._scan_bundle(source)
        strings = [item["text"] for item in result if item["kind"] == "string"]
        self.assertEqual(strings, ["After"])
        self.assertEqual([item["kind"] for item in result], ["template", "string"])

    def test_unterminated_regex_fails_closed(self):
        with self.assertRaisesRegex(audit.AuditError, "unterminated JavaScript regular expression"):
            audit._scan_bundle('if (ok) /unterminated\nconst after="After";')

    def test_template_extraction_handles_nested_expressions_and_static_parts(self):
        source = "const a=`Hello ${user.name}!`; const b=`${`nested ${x}`} left ${fn({a: 1})} right`;"
        result = audit._scan_bundle(source)
        self.assertEqual([item["kind"] for item in result], ["template", "template"])
        self.assertEqual(result[0]["parts"], ["Hello ", "!"])
        self.assertEqual(result[1]["parts"], [" left ", " right"])

    def test_context_line_column_and_unicode_are_reported(self):
        result = audit._scan_bundle('x=1;\nbutton={label:"Save & Close"};')
        self.assertEqual(result[0]["evidence"], "ui-property:label")
        self.assertEqual((result[0]["line"], result[0]["column"]), (2, 15))

    def test_unterminated_strings_comments_and_templates_fail_closed(self):
        for source in ('const x="unfinished', '/* unfinished', 'const x=`unfinished'):
            with self.subTest(source=source), self.assertRaises(audit.AuditError):
                audit._scan_bundle(source)

    def test_character_escape_decoding_handles_surrogate_pair(self):
        self.assertEqual(audit._decode_string(r"\uD83D\uDE80"), "🚀")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dict_dir = self.root / "dicts"
        self.dict_dir.mkdir()
        self.dict_file = self.dict_dir / "ui.json"
        self.dict_file.write_text(json.dumps({
            "Save": "保存",
            "Clear conversation": "清空对话",
            "Playback speed (< / >)": "播放速度（< / >）",
            "% of the customization budget is available.": "% 的自定义预算可用。",
            "Message subagent, @ to mention, / for actions": "给子智能体发消息，@ 提及，/ 执行操作",
            "Agent Behavior": "智能体行为",
            "Agent Market": "智能体市场",
            "Upload ${fileName} to {workspace}": "上传 ${fileName} 到 {workspace}",
        }, ensure_ascii=False), encoding="utf-8")
        self.bundle = self.root / "main.js"
        self.bundle.write_text('''
// "Comment Only"
const ui={label:"Save",title:"Clear conversation!!!",description:"Agent Behavior"};
el.createElement("div",null,"New Panel!");
const template=`Upload ${fileName} to {workspace}`;
const shape=/title:"ignore this"/g;
const marker="Example @ mention & Terms of Service!";
''', encoding="utf-8")

    def run_audit(self, **kwargs):
        return audit.audit(self.bundle, "2.99.0", self.dict_dir, **kwargs)

    def source_manifest(self, keys, version="2.99.0", platforms=None):
        path = self.root / "sources.json"
        source = {"schema": 1, "sources": [{
            "id": "fixture-layout-copy", "kind": "official-config", "last_verified_version": version,
            "platforms": ["windows"] if platforms is None else platforms, "reviewed_on": "2026-11-16", "keys": keys,
        }], "reviews": []}
        path.write_text(json.dumps(source), encoding="utf-8")
        return path

    def test_bundle_fingerprint_and_dictionary_fingerprints_are_recorded(self):
        report = self.run_audit()
        raw = self.bundle.read_bytes()
        self.assertEqual(report["bundle"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(report["bundle"]["bytes"], len(raw))
        self.assertEqual(report["bundle"]["version_source"], "user-supplied; not inferred")
        self.assertEqual(report["dictionaries"][0]["sha256"], hashlib.sha256(self.dict_file.read_bytes()).hexdigest())

    def test_two_way_diff_classifies_rewrite_and_new_candidate_with_evidence(self):
        report = self.run_audit()
        rewritten = next(item for item in report["possible_rewrites"] if item["text"] == "Clear conversation!!!")
        self.assertEqual(rewritten["nearby_dictionary_entries"][0]["key"], "Clear conversation")
        self.assertEqual(rewritten["classification"], "possible-rewrite")
        self.assertEqual(len(rewritten["locations"]), 1)
        self.assertEqual(rewritten["locations"][0]["line"], 3)
        new = next(item for item in report["new_candidates"] if item["text"] == "New Panel!")
        self.assertIn("direct-text", new["evidence"])
        old = next(item for item in report["dictionary_not_found"] if item["key"] == "Clear conversation")
        self.assertTrue(old["possible_rewrite_seen"])
        self.assertIn("do not delete", old["review_note"].lower())

    def test_dynamic_templates_and_sensitive_punctuation_are_retained_for_review(self):
        report = self.run_audit()
        self.assertEqual(report["dynamic_templates"][0]["parts"], ["Upload ", " to {workspace}"])
        values = {item["text"] for item in report["new_candidates"]}
        self.assertIn("Example @ mention & Terms of Service!", values)

    def test_report_roundtrips_javascript_lone_surrogates_as_valid_utf8(self):
        self.bundle.write_text(r'const label=`New ${value} \uD800`; const tail=`End ${value} \uDC00`;', encoding="utf-8")
        report = self.run_audit()
        output = self.root / "report.json"
        self.assertTrue(audit._write_report(report, output))
        raw = output.read_bytes()
        self.assertIn(b"\\ud800", raw)
        self.assertIn(b"\\udc00", raw)
        self.assertEqual(json.loads(raw.decode("utf-8")), report)

    def test_placeholder_review_supports_only_explicit_formats(self):
        report = self.run_audit()
        keys = {item["key"] for item in report["placeholder_reviews"]}
        self.assertNotIn("Playback speed (< / >)", keys)
        self.assertNotIn("% of the customization budget is available.", keys)
        self.assertNotIn("Upload ${fileName} to {workspace}", keys)

    def test_possible_rewrite_similarity_threshold_is_not_lowered(self):
        candidate = {"normalized": "Clear conversation", "_tokens": {"clear", "conversation"},
                     "key": "Clear conversation", "file": "ui.json"}
        self.assertEqual(audit._nearby_matches("Clear conversation", [candidate], 0.85)[0]["similarity"], 1)
        self.assertEqual(audit._nearby_matches("Clear conversation", [candidate], 1.01), [])

    def test_filter_reasons_are_auditable_without_rejecting_punctuation_copy(self):
        self.assertEqual(audit._candidate_filter("ASK_ANYTHING", "ui-property:title"), "enum-or-constant")
        self.assertEqual(audit._candidate_filter("Start Settings", "string-literal"), "no-capital-letter"
                         if not any(c.isupper() for c in "Start Settings") else "")
        self.assertEqual(audit._candidate_filter("Example @ mention & Terms of Service!", "direct-text"), "")

    def test_candidate_filter_withholds_email_urls_and_paths(self):
        for value in (
            "Contact foo@example.com for help",
            "Read https://example.test/help for details",
            r"Open C:\Users\Alice\notes.txt",
            "Open ~/private/notes.txt",
        ):
            with self.subTest(value=value):
                self.assertEqual(audit._candidate_filter(value, "direct-text"), "private-data")

    def test_term_exception_requires_a_word_boundary(self):
        entries = [
            {"file": "ui.json", "key": "Agent Market", "value": "智能体市场"},
            {"file": "ui.json", "key": "Agent Marketing", "value": "代理营销"},
            {"file": "ui.json", "key": "Agent MarketXYZ", "value": "代理市场XYZ"},
        ]
        findings = audit._terminology(entries)
        self.assertEqual([item["key"] for item in findings], ["Agent Marketing", "Agent MarketXYZ"])

    def test_audit_never_modifies_dictionaries(self):
        before = self.dict_file.read_bytes()
        self.run_audit()
        self.assertEqual(self.dict_file.read_bytes(), before)

    def test_expected_hash_mismatch_is_rejected_before_a_report(self):
        with self.assertRaisesRegex(audit.AuditError, "does not match"):
            self.run_audit(expected_sha256="0" * 64)

    def test_bundle_size_and_encoding_limits_fail_closed(self):
        with mock.patch.object(audit, "MAX_BUNDLE_BYTES", 2):
            with self.assertRaisesRegex(audit.AuditError, "64 MiB"):
                self.run_audit()
        self.bundle.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(audit.AuditError, "UTF-8"):
            self.run_audit()

    def test_duplicate_raw_and_normalized_dictionary_keys_are_reported(self):
        self.dict_file.write_text('''{"Save":"保存","Save":"存档","save":"保存"}''', encoding="utf-8")
        report = self.run_audit()
        kinds = {item["kind"] for item in report["dictionary_integrity"]["problems"]}
        self.assertIn("duplicate-json-key", kinds)
        self.assertIn("duplicate-normalized-key", kinds)

    def test_config_source_covers_a_dictionary_entry_absent_from_bundle(self):
        manifest = self.source_manifest(["Playback speed (< / >)"])
        report = self.run_audit(source_manifest=manifest, platform="windows")
        entry = next(item for item in report["dictionary_not_found"] if item["key"] == "Playback speed (< / >)")
        self.assertEqual(entry["coverage"], "reviewed-source")
        self.assertEqual(entry["last_verified_version"], "2.99.0")
        self.assertTrue(entry["sources"][0]["applies_to_audit"])
        self.assertNotIn(entry["key"], {item["key"] for item in report["dictionary_unconfirmed"]})
        self.assertEqual(report["summary"]["reviewed_source_dictionary_matches"], 1)
        self.assertEqual(report["source_manifest"]["sha256"], hashlib.sha256(manifest.read_bytes()).hexdigest())

    def test_config_source_requires_matching_version_and_explicit_platform(self):
        for version, platform in (("2.98.0", "windows"), ("2.99.0", "macos"), ("2.99.0", None)):
            with self.subTest(version=version, platform=platform):
                manifest = self.source_manifest(["Playback speed (< / >)"], version)
                report = self.run_audit(source_manifest=manifest, platform=platform)
                entry = next(item for item in report["dictionary_unconfirmed"] if item["key"] == "Playback speed (< / >)")
                self.assertEqual(entry["coverage"], "unconfirmed")
                self.assertIsNone(entry["last_verified_version"])
                self.assertFalse(entry["sources"][0]["applies_to_audit"])
                self.assertEqual(report["summary"]["reviewed_source_dictionary_matches"], 0)

    def test_source_labels_follow_dictionary_key_normalization(self):
        manifest = self.source_manifest(["  Playback   speed (< / >)  "], platforms=["all"])
        report = self.run_audit(source_manifest=manifest)
        self.assertEqual(report["summary"]["reviewed_source_dictionary_matches"], 1)

    def test_short_case_source_variant_covers_the_existing_dictionary_entry(self):
        manifest = self.source_manifest(["playback SPEED (< / >)"])
        report = self.run_audit(source_manifest=manifest, platform="windows")
        entry = next(item for item in report["dictionary_not_found"] if item["key"] == "Playback speed (< / >)")
        self.assertEqual(entry["coverage"], "reviewed-source")
        self.assertEqual(report["summary"]["reviewed_source_dictionary_matches"], 1)
        self.assertNotIn(entry["key"], {item["key"] for item in report["dictionary_unconfirmed"]})
        self.assertFalse(any(item["text"] == "playback SPEED (< / >)" for item in report["new_candidates"]))

    def test_new_config_label_is_a_review_candidate_without_a_bundle_location(self):
        manifest = self.source_manifest(["New Layout Label"])
        report = self.run_audit(source_manifest=manifest, platform="windows")
        candidate = next(item for item in report["new_candidates"] if item["text"] == "New Layout Label")
        self.assertEqual(candidate["locations"], [])
        self.assertEqual(candidate["evidence"], ["reviewed-source:official-config"])
        self.assertEqual(candidate["sources"][0]["id"], "fixture-layout-copy")

    def test_short_case_variant_is_covered_instead_of_reported_as_a_rewrite(self):
        self.bundle.write_text('const ui={label:"sAvE"};', encoding="utf-8")
        report = self.run_audit()
        self.assertFalse(any(item["text"] == "sAvE" for item in report["new_candidates"] + report["possible_rewrites"]))
        entry = next(item for item in report["dictionary_not_found"] if item["key"] == "Save")
        self.assertEqual(entry["coverage"], "short-case-match")
        self.assertEqual(report["summary"]["short_case_dictionary_matches"], 1)
        self.assertNotIn("Save", {item["key"] for item in report["dictionary_unconfirmed"]})

    def test_long_case_variant_is_not_treated_as_short_case_coverage(self):
        key = "Message subagent, @ to mention, / for actions"
        self.bundle.write_text("const ui={label:" + json.dumps(key.upper()) + "};", encoding="utf-8")
        report = self.run_audit()
        self.assertIn(key, {item["key"] for item in report["dictionary_unconfirmed"]})
        self.assertEqual(report["summary"]["short_case_dictionary_matches"], 0)
        self.assertIn(key.upper(), {item["text"] for item in report["possible_rewrites"]})

    def test_conflicting_short_case_translations_do_not_prove_coverage(self):
        self.dict_file.write_text('{"Save":"保存","SAVE":"存档"}', encoding="utf-8")
        self.bundle.write_text('const ui={label:"save"};', encoding="utf-8")
        report = self.run_audit()
        self.assertEqual(report["summary"]["short_case_dictionary_matches"], 0)
        self.assertEqual({item["key"] for item in report["dictionary_unconfirmed"]}, {"Save", "SAVE"})

    def test_source_manifest_rejects_private_values_and_full_config_dumps(self):
        for key in ("Contact foo@example.com for help", r"Open C:\Users\example\note.txt", "Read https://example.test"):
            with self.subTest(key=key):
                manifest = self.source_manifest([key])
                with self.assertRaises(audit.AuditError):
                    self.run_audit(source_manifest=manifest, platform="windows")
        manifest = self.source_manifest(["New Layout Label"])
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["runtime_config"] = {"account": "private"}
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(audit.AuditError, "only curated"):
            self.run_audit(source_manifest=manifest)

    def test_invalid_or_oversized_source_manifest_fails_closed(self):
        manifest = self.source_manifest(["New Layout Label"])
        with mock.patch.object(audit, "MAX_SOURCE_BYTES", 2), self.assertRaisesRegex(audit.AuditError, "1 MiB"):
            self.run_audit(source_manifest=manifest)
        for source in ('{"schema":2}', '{"schema":true}', '{"schema":1,"sources":{}}', '{"schema":1,"reviews":[null]}'):
            manifest.write_text(source, encoding="utf-8")
            with self.subTest(source=source), self.assertRaises(audit.AuditError):
                self.run_audit(source_manifest=manifest)

    def test_duplicate_manifest_fields_and_normalized_labels_are_rejected(self):
        manifest = self.source_manifest(["New Layout Label", " New  Layout Label "])
        with self.assertRaisesRegex(audit.AuditError, "duplicate normalized"):
            self.run_audit(source_manifest=manifest)
        manifest.write_text('{"schema":1,"schema":1,"sources":[]}', encoding="utf-8")
        with self.assertRaisesRegex(audit.AuditError, "duplicate source manifest field"):
            self.run_audit(source_manifest=manifest)

    def test_review_ledger_is_informational_and_does_not_remove_dictionary_keys(self):
        manifest = self.source_manifest(["New Layout Label"])
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["reviews"] = [{
            "file": "ui.json", "key": "Save", "translation": "保存", "action": "remove-equivalent",
            "replacement": {"file": "other.json", "key": "SAVE"}, "verification": "offline-engine",
            "last_verified_version": "2.99.0", "platforms": ["all"], "reviewed_on": "2026-11-16",
            "reason": "Review only; never edit dictionaries.",
        }]
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        before = self.dict_file.read_bytes()
        report = self.run_audit(source_manifest=manifest)
        self.assertEqual(report["dictionary_reviews"][0]["key"], "Save")
        self.assertEqual(self.dict_file.read_bytes(), before)
        self.assertIn("Save", json.loads(self.dict_file.read_text(encoding="utf-8")))

    def test_cli_loads_default_manifest_and_prevents_overwriting_its_inputs(self):
        manifest = self.source_manifest(["Playback speed (< / >)"])
        output = self.root / "report.json"
        arguments = ["--bundle", str(self.bundle), "--version", "2.99.0", "--dict-dir", str(self.dict_dir),
                     "--platform", "windows"]
        with mock.patch.object(audit, "DEFAULT_SOURCES", manifest), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(audit.main(arguments + ["--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["summary"]["reviewed_source_dictionary_matches"], 1)
            for destination in (self.bundle, self.dict_file, manifest, self.dict_dir / "new.json"):
                before = destination.read_bytes() if destination.exists() else None
                with self.subTest(destination=destination):
                    self.assertEqual(audit.main(arguments + ["--output", str(destination)]), 1)
                    self.assertEqual(destination.read_bytes() if destination.exists() else None, before)


if __name__ == "__main__":
    unittest.main()
