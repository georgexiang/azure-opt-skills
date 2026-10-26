"""Documentation contract checks; these do not execute Azure or prove agent behavior."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SupportBundleTests(unittest.TestCase):
    def test_no_dedicated_environment_gate(self):
        for relative in (
            "azure-support-ticket/SKILL.md",
            "azure-support-ticket/references/submission.md",
            "azure-support-ticket/tests/cases.md",
            "azure-ops/SKILL.md",
            "azure-ops/references/support-case.md",
            "README.md",
        ):
            text = (ROOT / relative).read_text()
            with self.subTest(path=relative):
                self.assertNotRegex(text, r"专用 Support|专用执行环境|专用执行器|专用环境|执行隔离")
                self.assertNotIn("xiaomi-europe-support", text)

    def test_current_project_credentials_are_discovered_without_memory(self):
        text = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        self.assertIn("即使没有 memory 映射", text)
        self.assertIn("$HOME/.azure", text)
        self.assertIn("AZURE_CONFIG_DIR", text)
        self.assertIn("当前 Project", text)

    def test_submission_retains_confirmation_and_recovery(self):
        text = (ROOT / "azure-support-ticket/SKILL.md").read_text()
        for required in (
            "Support Request Contributor", "重新复核并确认", "稳定", "supportTicketId",
            "只有确认 Azure 返回该资源不存在", "不盲目重复创建", "其他 Project",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        reference = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        self.assertIn("--tenant <tenant-uuid>", reference)
        self.assertIn("403、401", reference)
        self.assertIn("串行化", reference)
        self.assertIn("shell=False", reference)

    def test_restricted_grant_requests_owner_approval_instead_of_environment_switch(self):
        text = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        for required in (
            "purpose", "audience", "POST /v1/keychain/asks", "pending ask",
            "等待所有者审批", "不要修改授权记录", "POST /v1/keychain/use",
            "历史上下文", "待凭据用途审批",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_bundle_links_exist(self):
        for path in (ROOT / "azure-support-ticket").rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if "://" not in target and not target.startswith("#"):
                    self.assertTrue((path.parent / target.split("#")[0]).is_file(), target)


if __name__ == "__main__":
    unittest.main()
