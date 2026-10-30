"""公开源码检查器的回归验证，不使用真实凭据或地址。"""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_tree_check", ROOT / "scripts/check_public_tree.py")
SCANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCANNER)


class PublicTreeTests(unittest.TestCase):
    def test_scanner_reports_locations_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.txt"
            secret = "xoxp-" + "A" * 32
            path.write_text(secret + "\n" + ".".join(map(str, [192, 0, 2, 1])))
            findings = SCANNER.scan_public_tree(temporary)
            self.assertEqual({row["kind"] for row in findings}, {"slack_token", "ipv4_literal"})
            self.assertNotIn(secret, json.dumps(findings))
