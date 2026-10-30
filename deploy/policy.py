"""管理员控制目录中的部署顺序、回滚比较条件和有限审计记录。"""

import json
from pathlib import Path
import re

if __package__:
    from .release import write_json
else:
    from release import write_json


class PolicyError(Exception):
    pass


class Policy:
    def __init__(self, root):
        self.path = Path(root) / "policy.json"
        self.value = json.loads(self.path.read_text()) if self.path.exists() else {
            "sequence": 0, "accepted": None, "active": None, "previous": None,
            "rollback_block": False, "history": [], "audit": []}

    def save(self, event, **details):
        self.value["audit"] = (self.value["audit"] + [{"event": event, **details}])[-100:]
        write_json(self.path, self.value)

    def accept(self, sequence, commit, checksum):
        if (type(sequence) is not int or not 0 < sequence < 2**63
                or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise PolicyError("invalid_deployment_identity")
        identity = {"commit": commit, "checksum": checksum}
        if sequence < self.value["sequence"]:
            raise PolicyError("stale_deployment_event")
        if sequence == self.value["sequence"]:
            if self.value["accepted"] != identity or self.value["rollback_block"]:
                raise PolicyError("conflicting_or_rolled_back_event")
            return
        self.value.update(sequence=sequence, accepted=identity, rollback_block=False)
        self.save("accepted", sequence=sequence, commit=commit)

    def completed(self, commit):
        before = self.value["active"]
        if before != commit:
            self.value.update(active=commit, previous=before)
        self.value["history"] = list(dict.fromkeys(self.value["history"] + [commit]))[-20:]
        self.save("activated", commit=commit, sequence=self.value["sequence"])

    def check_rollback(self, sequence, expected, target):
        if (sequence != self.value["sequence"] or expected != self.value["active"]
                or target not in self.value["history"] or target == expected
                or not re.fullmatch(r"[0-9a-f]{40}", target)):
            raise PolicyError("rollback_compare_or_history_failed")

    def rolled_back(self, target):
        if self.value["active"] != target:
            self.value.update(previous=self.value["active"], active=target)
        self.value["rollback_block"] = True
        self.save("explicit_rollback", commit=target, sequence=self.value["sequence"])

    def protected(self):
        return {item for item in (self.value["active"], self.value["previous"]) if item}
