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
                self.assertNotRegex(
                    text, r"专用 Support|专用执行环境|专用执行器|专用环境|执行隔离"
                )
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
            "Support Request Contributor",
            "重新复核并确认",
            "稳定",
            "supportTicketId",
            "只有确认 Azure 返回该资源不存在",
            "不盲目重复创建",
            "其他 Project",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        reference = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        self.assertIn("--tenant <tenant-uuid>", reference)
        self.assertIn("403、401", reference)
        self.assertIn("串行化", reference)
        self.assertIn("shell=False", reference)

    def test_restricted_grant_requests_owner_approval_instead_of_environment_switch(
        self,
    ):
        text = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        for required in (
            "purpose",
            "audience",
            "POST /v1/keychain/asks",
            "pending ask",
            "等待所有者审批",
            "不要修改授权记录",
            "execute.credentials",
            "execution handle",
            "interactive-login",
            "历史上下文",
            "待凭据用途审批",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_standing_grants_and_parameter_evidence_are_explicit(self):
        text = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        for required in (
            'requestedMode: "standing"',
            'mode == "standing"',
            'status == "active"',
            "expiresAt",
            "直到撤销",
            "长期工作范围",
            "不重新申请凭据授权",
            "capture_azure_error.py",
            "lastError",
            "detailsMissing",
            "释放",
            "ISO 3166-1 alpha-3",
            "这三个示例本身是有效格式",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertTrue(
            (ROOT / "azure-support-ticket/scripts/capture_azure_error.py").is_file()
        )

    def test_project_grants_are_not_requester_personal_credentials(self):
        text = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        for required in (
            "三个不同角色",
            "audienceScopeId",
            "原申请人的姓名或 requesterId",
            "不为每个成员重复申请",
            "以最新选择为准",
            "个人 RBAC",
            "普通工单创建不要求先具备关闭工单能力",
            "测试后必须关闭",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_toolchain_preflight_and_all_azure_examples_use_packaged_entry(self):
        skill = (ROOT / "azure-support-ticket/SKILL.md").read_text()
        reference = (ROOT / "azure-support-ticket/references/submission.md").read_text()
        for host in (
            "pypi.org",
            "files.pythonhosted.org",
            "azcliprod.blob.core.windows.net",
        ):
            self.assertIn(f"egress:{host}", skill)
        self.assertIn("azure_cli.sh --check", reference)
        self.assertIn("azure_cli.sh --ensure", reference)
        self.assertNotRegex(reference, r"(?m)^az ")
        self.assertNotIn("缺少 extension 时让运营方安装", reference)
        self.assertIn("不构成重新创建授权", reference)

    def test_bundle_links_exist(self):
        for path in (ROOT / "azure-support-ticket").rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if "://" not in target and not target.startswith("#"):
                    self.assertTrue(
                        (path.parent / target.split("#")[0]).is_file(), target
                    )


if __name__ == "__main__":
    unittest.main()
