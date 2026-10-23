"""Structural checks for the canonical Case / On-call skill bundle.

These tests validate repository packaging, not platform import authorization or
live project_case_upload execution.
"""

from pathlib import Path
import re
import unittest


BUNDLE = Path(__file__).resolve().parents[1]
ROOT = BUNDLE.parent
NAME = "case-summary-oncall-schedule"


class BundleTests(unittest.TestCase):
    def test_directory_matches_canonical_name(self):
        self.assertEqual(BUNDLE.name, NAME)
        self.assertFalse((ROOT / "case-summery-oncall-schedule").exists())

    def test_frontmatter_name(self):
        raw = (BUNDLE / "SKILL.md").read_text()
        frontmatter, body = raw.removeprefix("---\n").split("\n---\n", 1)
        self.assertEqual(re.findall(r"^name: (.+)$", frontmatter, re.M), [NAME])
        self.assertIn("description:", frontmatter)
        self.assertTrue(body.strip())

    def test_canonical_name_is_unique(self):
        names = []
        for path in ROOT.glob("*/SKILL.md"):
            names.extend(re.findall(r"^name: (.+)$", path.read_text(), re.M))
        self.assertEqual(names.count(NAME), 1)
        self.assertEqual(len(names), len(set(names)))

    def test_readme_links_and_subset(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn(f'["{NAME}"]', readme)
        for path in re.findall(r"\]\((case-summary-oncall-schedule/[^)]+)\)", readme):
            self.assertTrue((ROOT / path).is_file(), path)
        self.assertNotIn("](" + "case-summery-oncall-schedule/", readme)

    def test_case_checklist_title(self):
        cases = (BUNDLE / "tests/cases.md").read_text()
        self.assertTrue(cases.startswith("# Case Summary / On-call Schedule"))
        self.assertIn(NAME, cases)

    def test_migration_and_safety_contract_remain_explicit(self):
        skill = (BUNDLE / "SKILL.md").read_text()
        for required in [
            "project_case_upload", "artifactId", "confirmationId",
            "真实的新用户回复", "不从网址下载文件", "不等于平台已发布",
            "会话摘要排除", "不补发或改写旧邮件", "不绕过校验",
        ]:
            self.assertIn(required, skill)


if __name__ == "__main__":
    unittest.main()
