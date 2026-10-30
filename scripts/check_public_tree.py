"""离线检查待公开源码，报告只含位置与类别，不回显疑似敏感值。"""

import json
from pathlib import Path
import re
import sys

PATTERNS = {
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{16,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "webhook_key": re.compile(r"qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[0-9a-fA-F-]{20,}"),
    "ipv4_literal": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


def scan_public_tree(root):
    root = Path(root)
    findings = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in {".git", ".venv", "__pycache__"} for part in relative.parts):
            continue
        if (path.name == "credentials.json" or path.name.startswith(".env")
                or path.suffix in {".key", ".pem", ".log", ".db", ".sqlite3"}
                or any(part in {"secrets", "state", "run", "var"} for part in relative.parts)):
            findings.append({"file": str(relative), "kind": "runtime_or_secret_file"})
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append({"file": str(relative), "kind": "unreviewed_binary"})
            continue
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append({"file": str(relative), "line": text[:match.start()].count("\n") + 1, "kind": kind})
        if path.name == "credentials.example.json":
            data = json.loads(text)
            if data != {"slack_user_token": "", "wecom_webhook": ""}:
                findings.append({"file": str(relative), "kind": "nonempty_credential_template"})
    return findings


if __name__ == "__main__":
    findings = scan_public_tree(Path(__file__).resolve().parents[1])
    print(json.dumps({"findings": findings, "scope": "current_source_tree_only"}, ensure_ascii=False))
    sys.exit(1 if findings else 0)
