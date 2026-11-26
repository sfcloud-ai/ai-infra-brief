"""在原提示词和历史输入不变的情况下，核验一次主机登记的整期审计迁移。"""

import hashlib
from dataclasses import dataclass
import re
import subprocess
from pathlib import Path

from .contracts import RESOURCE_ROOT
from .storage import BriefError


@dataclass(frozen=True)
class EditionMigration:
    """仅对本次原计划和指定备用 PDF 生效的主机回执。"""
    digest: str
    plan_sha256: str
    reserve_pdf: dict

    def retained_source(self, original, current):
        """向已核实的历史来源视图附加回执，冻结原件保持原字节。"""
        if (current is not None and current.get("archive_followup") == self.reserve_pdf
                and "archive_followup" not in original
                and {k: v for k, v in current.items() if k != "archive_followup"} == original):
            return current
        return original


def read_migration(path, metadata, original, parse):
    """绑定主机回执、冻结输入以及当前干净检出的校验器版本。"""
    error = BriefError("Invalid same-run edition audit migration")
    with Path(path).open("rb") as stream:
        raw = stream.read(16_385)
    if not raw or len(raw) > 16_384:
        raise error
    try:
        receipt = parse(raw, "migration receipt")
        plan = parse(receipt["plan"], "migration plan")
        inputs = metadata["inputs"]
        fields = {"schema_version", "request_id", "owner_id", "epoch", "revision", "plan",
                  "plan_sha256", "inputs_sha256", "checkpoint_sha256", "archive_head_sha256",
                  "state_sha256", "scope_sha256", "policy_sha256", "verifier_commit", "reserve_pdf"}
        policy = (RESOURCE_ROOT / "prompts" / "audit.md").read_bytes()
        pdf = receipt["reserve_pdf"]
        if (not isinstance(pdf, dict) or set(pdf) != {"operation_id", "result_sha256", "body_sha256", "bytes"}
                or not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}", pdf["operation_id"])
                or not all(re.fullmatch("[a-f0-9]{64}", pdf[k]) for k in ("result_sha256", "body_sha256"))
                or type(pdf["bytes"]) is not int or pdf["bytes"] <= 0):
            raise error
        if (set(receipt) != fields or receipt["schema_version"] != "edition_audit_migration_v2"
                or receipt["request_id"] != plan["request_id"]
                or plan["request_id"] != "morning-" + metadata["date"].replace("-", "") + "-r1"
                or plan["report_date"] != metadata["date"]
                or inputs["prompt_commit"] != plan["prompt_commit_sha"]
                or inputs["history_commit"] != plan["reports_commit_sha"]
                or plan["files"] != {"reports/index.json": inputs["history_sha256"], **{
                    "ai_brief/resources/prompts/" + name: value for name, value in inputs["prompt_hashes"].items()}}
                or hashlib.sha256(original).hexdigest() != "dba8eadebbdcd284a17db7f4f732bf7d7bda157f509f3842f70cf9bfbd7a8ba7"
                or receipt["plan_sha256"] != hashlib.sha256(receipt["plan"].encode()).hexdigest()
                or receipt["policy_sha256"] != hashlib.sha256(policy).hexdigest()
                or b"Evidence-Package: streamed_whole_edition_v1" not in policy.splitlines()
                or not all(re.fullmatch("[0-9a-f]{64}", receipt[key]) for key in fields if key.endswith("_sha256"))):
            raise error
        root = Path(__file__).resolve().parent.parent
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).strip()
        if (not re.fullmatch("[0-9a-f]{40}", receipt["verifier_commit"])
                or Path(git("rev-parse", "--show-toplevel").decode()).resolve() != root
                or git("rev-parse", "HEAD").decode() != receipt["verifier_commit"]
                or git("ls-files", "--error-unmatch", "--", "ai_brief/edition_migration.py") != b"ai_brief/edition_migration.py"
                or git("status", "--porcelain", "--untracked-files=all", "--", "ai_brief")):
            raise error
    except (KeyError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        raise error from exc
    return EditionMigration(hashlib.sha256(raw).hexdigest(), receipt["plan_sha256"], pdf)
