"""T4 转发包独立安装、可重复构建以及排除研究 Prompt 的回归。"""

from email.parser import Parser
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from zipfile import ZipFile

from deploy.build_relay import RUNTIME_FILES, build_relay
from deploy.bundle import make_bundle


ROOT = Path(__file__).resolve().parents[1]


class RelayPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="relay-package-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "source"
        for relative in (["integrations/slack_wecom/" + name for name in RUNTIME_FILES]
                         + ["deploy/relay-package.toml", "LICENSE"]):
            target = cls.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        prompt = cls.source / "ai_brief/resources/prompts/research.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("synthetic-prompt-before")
        cls.wheel = build_relay(cls.source, cls.root / "first")
        prompt.write_text("synthetic-prompt-after")
        cls.repeated = build_relay(cls.source, cls.root / "second")
        cls.environment = cls.root / "venv"
        venv.EnvBuilder(with_pip=True).create(cls.environment)
        cls.python = cls.environment / "bin/python"
        result = subprocess.run([str(cls.python), "-I", "-m", "pip", "--isolated", "install",
                                 "--no-index", "--no-deps", str(cls.wheel)], capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError("isolated_relay_install_failed")
        shutil.rmtree(cls.source)

    def test_prompt_edits_do_not_change_relay_wheel(self):
        self.assertEqual(self.wheel.read_bytes(), self.repeated.read_bytes())
        with ZipFile(self.wheel) as archive:
            names = archive.namelist()
            self.assertFalse(any("prompt" in name or name.startswith("ai_brief/") for name in names))
            metadata = Parser().parsestr(archive.read(next(n for n in names if n.endswith("/METADATA"))).decode())
            self.assertEqual(metadata["Name"], "sfcloud-ai-relay")
            self.assertEqual(metadata["License-Expression"], "Apache-2.0")
            self.assertFalse(metadata.get_all("Requires-Dist"))

    def test_installed_relay_capabilities_work_without_checkout_or_brief_dependencies(self):
        result = subprocess.run([str(self.environment / "bin/ai-brief-relay"), "capabilities"],
                                cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"relay": True, "resume": True, "pause_activate": True, "messages_sent": 0})

    def test_bundle_rejects_accidentally_embedded_prompt(self):
        directory = self.root / "invalid"
        directory.mkdir()
        wheel = directory / self.wheel.name
        shutil.copyfile(self.wheel, wheel)
        with ZipFile(wheel, "a") as archive:
            archive.writestr("ai_brief/resources/prompts/research.md", "synthetic")
        with self.assertRaisesRegex(ValueError, "unexpected_relay_wheel_content"):
            make_bundle("a" * 40, directory, ROOT / "deploy/runtime.lock", self.root / "invalid.tar.gz")
