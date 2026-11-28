import tempfile
import unittest
from pathlib import Path
from algoai.cli.arguments import parse_args
from algoai.cli.runner import _script_args
from algoai.utils.json_contract import validate_json_contract
from algoai.utils.skill_parser import discover_skills, parse_frontmatter

class FrontmatterTests(unittest.TestCase):
    def test_scalar_and_folded_description(self):
        metadata, body = parse_frontmatter("---\nname: sample\naccess: read-only\ndescription: >-\n  First line\n  second line\n---\n# Body\n")
        self.assertEqual(metadata["name"], "sample")
        self.assertEqual(metadata["access"], "read-only")
        self.assertEqual(metadata["description"], "First line second line")
        self.assertIn("# Body", body)
    def test_missing_frontmatter(self):
        metadata, body = parse_frontmatter("# Title\nText\n")
        self.assertEqual(metadata, {})
        self.assertEqual(body, "# Title\nText\n")

class DiscoveryTests(unittest.TestCase):
    def test_discovery_and_duplicate_precedence(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root1, root2 = Path(first), Path(second)
            (root1 / "alpha").mkdir()
            (root2 / "alpha").mkdir()
            (root1 / "alpha" / "SKILL.md").write_text("---\nname: alpha\ncategory: market-data\naccess: read-only\ndescription: First root\nexecution: direct-python\nscript: scripts/run.py\nresult_schema: schema.json\n---\n# A\n", encoding="utf-8")
            (root2 / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: Second root\n---\n# B\n", encoding="utf-8")
            skills = discover_skills([root1, root2])
            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].description, "First root")
            self.assertEqual(skills[0].access, "read-only")
            self.assertEqual(skills[0].category, "Market Data")
            self.assertEqual(skills[0].execution, "direct-python")
            self.assertEqual(skills[0].script, "scripts/run.py")
            self.assertEqual(skills[0].result_schema, "schema.json")

class ArgumentTests(unittest.TestCase):
    def test_legacy_prompt_is_preserved(self):
        args = parse_args(["--skip-permissions", "--prompt", "hello"])
        self.assertEqual(args.command, "prompt")
        self.assertEqual(args.prompt, "hello")
        self.assertTrue(args.skip_permissions)
    def test_run_result_format_after_skill_name(self):
        args = parse_args(["run", "upstox-pnl", "--result-format", "json"])
        self.assertEqual(args.skill_name, "upstox-pnl")
        self.assertEqual(args.result_format, "json")
    def test_run_options_before_skill_name(self):
        args = parse_args(["run", "--result-format", "json", "upstox-pnl"])
        self.assertEqual(args.skill_name, "upstox-pnl")
        self.assertEqual(args.result_format, "json")
    def test_script_args_json(self):
        args = parse_args(["run", "upstox-orders-fetch", "--result-format", "json",
                           "--script-args-json", '["--kind","normal","--status","open"]'])
        self.assertEqual(_script_args(args.script_args_json),
                         ["--kind", "normal", "--status", "open"])
    def test_script_args_json_rejects_non_string_array(self):
        with self.assertRaises(ValueError):
            _script_args('[ "--quantity", 5 ]')
    def test_run_command(self):
        args = parse_args(["run", "--allow-write", "sample", "do", "this"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.skill_name, "sample")
        self.assertEqual(args.prompt, "do this")
        self.assertTrue(args.allow_write)
    def test_help_and_version(self):
        self.assertEqual(parse_args(["--help"]).command, "help")
        self.assertEqual(parse_args(["--version"]).command, "version")

class JsonContractTests(unittest.TestCase):
    def test_valid_payload(self):
        schema = {"type":"object","required":["status","positions_count"],
                  "properties":{"status":{"type":"string"},"positions_count":{"type":"integer"}}}
        self.assertEqual(validate_json_contract({"status":"success","positions_count":0}, schema), [])
    def test_missing_and_wrong_types(self):
        schema = {"type":"object","required":["positions_count"],
                  "properties":{"positions_count":{"type":"integer"}}}
        errors = validate_json_contract({"positions_count":"0"}, schema)
        self.assertTrue(any("expected integer" in item for item in errors))
    def test_rejects_unexpected_fields(self):
        schema = {"type":"object","properties":{},"additionalProperties":False}
        self.assertTrue(validate_json_contract({"unexpected":1}, schema))

if __name__ == "__main__":
    unittest.main()
