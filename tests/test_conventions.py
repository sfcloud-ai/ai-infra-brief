"""长期文字与命名约定的回归检查。"""

import ast
import json
from pathlib import Path
import re
import tokenize
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHINESE = re.compile(r"[\u4e00-\u9fff]")


def python_files():
    return [path for directory in ("ai_brief", "tests", "integrations", "scripts") for path in (ROOT / directory).rglob("*.py")]


class ConventionTests(unittest.TestCase):
    def test_python_comments_and_docstrings_are_chinese(self):
        for path in python_files():
            with path.open("rb") as stream:
                for token in tokenize.tokenize(stream.readline):
                    if token.type == tokenize.COMMENT:
                        self.assertRegex(token.string, CHINESE, f"{path}:{token.start[0]}")
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    docstring = ast.get_docstring(node)
                    if docstring:
                        self.assertRegex(docstring, CHINESE, f"{path}:{getattr(node, 'lineno', 1)}")

    def test_python_identifiers_remain_ascii(self):
        for path in python_files():
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append(node.name)
                elif isinstance(node, ast.Name):
                    names.append(node.id)
                elif isinstance(node, ast.arg):
                    names.append(node.arg)
                elif isinstance(node, ast.Attribute):
                    names.append(node.attr)
                elif isinstance(node, ast.alias):
                    names.extend([node.name, node.asname or ""])
                for name in names:
                    self.assertTrue(name.isascii(), f"{path}: {name}")

    def test_configuration_keys_remain_ascii(self):
        def inspect(value, path):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertTrue(key.isascii(), f"{path}: {key}")
                    inspect(child, path)
            elif isinstance(value, list):
                for child in value:
                    inspect(child, path)
        paths = list((ROOT / "ai_brief" / "resources").rglob("*.json")) + list((ROOT / "integrations").rglob("*.json"))
        for path in paths:
            inspect(json.loads(path.read_text()), path)

    def test_readme_documents_language_policy(self):
        text = (ROOT / "README.md").read_text()
        self.assertIn("README 和代码注释使用中文", text)
        self.assertIn("变量、函数、类名和配置项使用英文", text)


if __name__ == "__main__":
    unittest.main()
