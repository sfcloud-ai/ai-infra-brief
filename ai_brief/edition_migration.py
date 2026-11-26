"""在原提示词和历史输入不变的情况下，核验一次主机登记的整期审计迁移。"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .contracts import RESOURCE_ROOT
from .storage import BriefError


def migration_digest(path, metadata, original):
    """绑定主机回执、冻结输入以及当前干净检出的校验器版本。"""
    error = BriefError("Invalid same-run edition audit migration")
    with Path(path).open("rb") as stream:
        raw = stream.read(16_385)
    if not raw or len(raw) > 16_384:
        raise error
    try:
        receipt = json.loads(raw)
        plan = json.loads(receipt["plan"])
        inputs = metadata["inputs"]
        fields = {"schema_version", "request_id", "owner_id", "epoch", "revision", "plan",
                  "plan_sha256", "inputs_sha256", "checkpoint_sha256", "archive_head_sha256",
                  "state_sha256", "scope_sha256", "policy_sha256", "verifier_commit", "reserve_pdf"}
        policy = (RESOURCE_ROOT / "prompts" / "audit.md").read_bytes()
        if (set(receipt) != fields or receipt["schema_version"] != "edition_audit_migration_v1"
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
                or git("rev-parse", "HEAD").decode() != receipt["verifier_commit"]
                or git("status", "--porcelain", "--untracked-files=all", "--", "ai_brief")):
            raise error
    except (KeyError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        raise error from exc
    return hashlib.sha256(raw).hexdigest()
