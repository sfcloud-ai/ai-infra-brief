"""构建与安装回归：运行时不能依赖源码目录。"""

from email.parser import Parser
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from zipfile import ZipFile

from ai_brief.contracts import RESOURCE_ROOT
from ai_brief.pipeline import implementation_fingerprint


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def copy_installed_distribution(name, target_site, expected_version=None):
    """从当前解释器离线复制依赖，而不是从其基础 Python 环境取依赖。

    待测应用必须通过 pip 安装 wheel，不能用此方式复制。这里只复制明确指定的
    已安装依赖，不复制用户配置、路径注入文件、命令入口或来源 URL 元数据。
    """
    installed = distribution(name)
    if expected_version and installed.version != expected_version:
        raise AssertionError(f"{name}: expected {expected_version}, found {installed.version}")
    if installed.files is None:
        raise AssertionError(f"{name} has no installed file record; cannot seed it offline")
    for entry in installed.files:
        relative = Path(str(entry))
        if relative.is_absolute() or ".." in relative.parts:
            continue  # 例如 ../../../bin/jsonschema，运行时导入不需要这些命令入口。
        if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pth"} or relative.name == "direct_url.json":
            continue
        source = Path(installed.locate_file(entry))
        if not source.is_file():
            raise AssertionError(f"Missing dependency file: {name}: {relative}")
        destination = target_site / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return installed.version


def runtime_requirements():
    requirements = {}
    for line in (RESOURCE_ROOT / "requirements.lock").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, version = line.split("==", 1)
            requirements[name] = version
    return requirements


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="sfcloud-packaging-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.base = Path(cls.temporary.name)
        source = cls.base / "build-source"
        shutil.copytree(SOURCE_ROOT, source, ignore=shutil.ignore_patterns(
            "var", "__pycache__", "*.pyc", ".git", ".venv", "build", "dist", "*.egg-info", ".env", ".env.*"
        ))
        cls.dist = cls.base / "dist"
        cls.dist.mkdir()
        cls.home = cls.base / "home"
        cls.home.mkdir()
        # 不继承用户配置、令牌或 PYTHONPATH。
        cls.environment = {"HOME": str(cls.home), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                           "PYTHONNOUSERSITE": "1", "SOURCE_DATE_EPOCH": "1700000000"}
        cls.checked([sys.executable, "-I", "-B", "-c",
                     "from setuptools.build_meta import build_wheel; "
                     f"build_wheel({str(cls.dist)!r})"], cwd=source)
        cls.wheel = next(cls.dist.glob("*.whl"))
        repeated_source = cls.base / "other-clean-source"
        shutil.copytree(SOURCE_ROOT, repeated_source, ignore=shutil.ignore_patterns(
            "var", "__pycache__", "*.pyc", ".git", ".venv", "build", "dist", "*.egg-info", ".env", ".env.*"
        ))
        for path in repeated_source.rglob("*"):
            if path.is_file():
                os.utime(path, (1800000000, 1800000000))
        repeated_dist = cls.base / "repeated-dist"
        repeated_dist.mkdir()
        cls.checked([sys.executable, "-I", "-B", "-c",
                     "from setuptools.build_meta import build_wheel; "
                     f"build_wheel({str(repeated_dist)!r})"], cwd=repeated_source)
        cls.repeated_wheel = next(repeated_dist.glob("*.whl"))
        cls.venv = cls.base / "venv"
        # 嵌套 venv 不能继承 system-site-packages，否则会回到基础 Python，
        # 悄悄遗漏只安装在 CI 父 venv 中的依赖。
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(cls.venv)
        cls.python = cls.venv / "bin" / "python"
        cls.site = Path(cls.checked([str(cls.python), "-I", "-B", "-c",
                                     "import sysconfig; print(sysconfig.get_path('purelib'))"], cls.base).strip())
        cls.requirements = runtime_requirements()
        for name, version in cls.requirements.items():
            copy_installed_distribution(name, cls.site, version)
        cls.checked([str(cls.python), "-I", "-B", "-m", "pip", "--isolated",
                     "--disable-pip-version-check", "install", "--no-index", "--no-deps", str(cls.wheel)], cwd=cls.base)
        shutil.rmtree(source)  # 删除构建源码，检测运行时是否偷偷回退到该目录。
        shutil.rmtree(repeated_source)
        cls.outside = cls.base / "unrelated-cwd"
        cls.outside.mkdir()

    @classmethod
    def checked(cls, command, cwd):
        result = subprocess.run(command, cwd=cwd, env=cls.environment,
                                text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise AssertionError(f"Command failed ({result.returncode}): {command}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def test_wheel_contains_runtime_resources_and_apache_license(self):
        with ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
            resources = {f"ai_brief/resources/{path.relative_to(RESOURCE_ROOT).as_posix()}"
                         for path in RESOURCE_ROOT.rglob("*") if path.is_file()}
            self.assertTrue(resources <= names, resources - names)
            self.assertFalse(any("__pycache__" in name or "/var/" in name or name.endswith(".pyc") for name in names))
            metadata = Parser().parsestr(archive.read(next(name for name in names if name.endswith("/METADATA"))).decode())
            self.assertEqual(metadata["Name"], "sfcloud-ai")
            self.assertEqual(metadata["License-Expression"], "Apache-2.0")
            self.assertEqual(metadata["License-File"], "LICENSE")
            license_text = archive.read(next(name for name in names if name.endswith("/licenses/LICENSE"))).decode()
            self.assertIn("Version 2.0, January 2004", license_text)
            self.assertIn("END OF TERMS AND CONDITIONS", license_text)

    def test_clean_source_rebuild_with_different_mtimes_is_identical(self):
        self.assertEqual(self.wheel.read_bytes(), self.repeated_wheel.read_bytes())

    def test_installed_relay_entry_includes_resume_and_pause_control_without_source(self):
        with ZipFile(self.wheel) as archive:
            self.assertTrue({"ai_brief_relay/relay.py", "ai_brief_relay/images.py",
                             "ai_brief_relay/resume.py", "ai_brief_relay/runtime.py"} <= set(archive.namelist()))
        output = self.checked([str(self.venv / "bin/ai-brief-relay"), "capabilities"], self.outside)
        self.assertEqual(json.loads(output), {"relay": True, "resume": True, "pause_activate": True, "messages_sent": 0})

    def test_runtime_dependencies_are_isolated_from_base_python(self):
        output = self.checked([str(self.python), "-I", "-B", "-c",
                               "import json,jsonschema,sys; from importlib.metadata import distribution; "
                               f"names={list(self.requirements)!r}; "
                               "print(json.dumps({'jsonschema':jsonschema.__file__, 'paths':sys.path, "
                               "'dependencies':{name:{'root':str(distribution(name).locate_file('')), "
                               "'version':distribution(name).version} for name in names}}))"], self.outside)
        data = json.loads(output)
        self.assertTrue(Path(data["jsonschema"]).is_relative_to(self.venv))
        for name, info in data["dependencies"].items():
            self.assertTrue(Path(info["root"]).is_relative_to(self.venv), info)
            self.assertEqual(info["version"], self.requirements[name])
        for path in data["paths"]:
            if "site-packages" in path or "dist-packages" in path:
                self.assertTrue(Path(path).is_relative_to(self.venv), path)

    def test_installed_control_entry_runs_offline_without_source(self):
        output = self.checked([str(self.venv / "bin/ai-brief-control"), "capabilities"], self.outside)
        self.assertEqual(json.loads(output), {"commands": ["help", "ping", "status"],
                                            "read_only": True, "messages_sent": 0})

    def test_installed_cli_runs_outside_checkout(self):
        output = self.checked([str(self.python), "-I", "-B", "-c",
                               "import json,ai_brief; from ai_brief.contracts import RESOURCE_ROOT; "
                               "from ai_brief.pipeline import implementation_fingerprint; "
                               "print(json.dumps({'module':ai_brief.__file__, 'resources':str(RESOURCE_ROOT), "
                               "'fingerprint':implementation_fingerprint(RESOURCE_ROOT)}))"], self.outside)
        imported = json.loads(output)
        self.assertTrue(Path(imported["module"]).is_relative_to(self.venv))
        self.assertTrue(Path(imported["resources"]).is_relative_to(self.venv))
        self.assertEqual(imported["fingerprint"], implementation_fingerprint(RESOURCE_ROOT))
        executable = str(self.venv / "bin" / "ai-brief")
        command = [executable, "--state-dir", str(self.outside / "state"), "run",
                   "--as-of", "2026-10-30T08:00:00+08:00", "--allow-backfill"]
        first = json.loads(self.checked(command, self.outside))
        second = json.loads(self.checked(command, self.outside))
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        directory = Path(first["run_dir"])
        self.assertEqual(json.loads((directory / "brief.json").read_text())["status"], "zero_items")
        self.assertTrue((directory / "brief.md").is_file())
        self.assertTrue((directory / "overview.svg").is_file())
        delivery = [executable, "--state-dir", str(self.outside / "state"), "simulate-delivery", "--date", "2026-10-30"]
        sent = json.loads(self.checked(delivery, self.outside))
        repeated = json.loads(self.checked(delivery, self.outside))
        self.assertFalse(sent["reused"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["ledger"]["attempts"], 1)
        self.assertEqual(first["manifest"]["delivery"]["real_status"], "not_sent")

    def test_installed_module_still_rejects_live_adapter(self):
        result = subprocess.run([str(self.python), "-I", "-B", "-m", "ai_brief", "run", "--adapter", "live"],
                                cwd=self.outside, env=self.environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("NOT CONNECTED", result.stderr)


if __name__ == "__main__":
    unittest.main()
