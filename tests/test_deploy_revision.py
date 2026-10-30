"""使用真实 Git 提交验证后续 Prompt 变更不会漏掉代码发布。"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from deploy.check_revision import same_deployment_inputs


class DeploymentRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1",
                            "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q")
        (self.root / "relay.py").write_text("before")
        self.initial = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Synthetic Test",
                                        "-c", "user.email=test@example.invalid", *args],
                                       cwd=self.root, env=self.environment, text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def test_prompt_and_documentation_commit_does_not_supersede_code(self):
        prompt = self.root / "ai_brief/resources/prompts/research.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("new prompt")
        (self.root / "README.md").write_text("new usage")
        current = self.commit()
        self.assertTrue(same_deployment_inputs(self.initial, current, self.root))

    def test_code_and_deployment_configuration_supersede_old_build(self):
        for relative in ("relay.py", "integrations/slack_wecom/relay.py", "deploy/relay-package.toml",
                         "deploy/build.lock", ".github/workflows/cd.yml", "LICENSE"):
            with self.subTest(path=relative):
                before = self.git("rev-parse", "HEAD")
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("new deployment input")
                current = self.commit()
                self.assertFalse(same_deployment_inputs(before, current, self.root))

    def test_unknown_revision_cannot_pass_comparison(self):
        with self.assertRaisesRegex(ValueError, "revision_comparison_failed"):
            same_deployment_inputs(self.initial, "e" * 40, self.root)
