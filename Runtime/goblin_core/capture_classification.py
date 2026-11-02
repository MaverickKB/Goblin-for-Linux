"""Local, deterministic classification of memory capture records. No model calls."""

import re


class CaptureClassifier:
    batch_items = 6

    def __init__(self, enabled=True, decision_log=None):
        self.enabled = bool(enabled)
        self.decision_log = decision_log

    def verify_output(self, message, reply, outputs):
        text = reply if isinstance(reply, str) else ""
        failed = False
        for item in outputs or []:
            if not isinstance(item, dict):
                continue
            if item.get("error") or item.get("blocked") or item.get("success") is False:
                failed = True
            result = item.get("result")
            if isinstance(result, dict):
                if result.get("error") or result.get("exit_code", 0) not in (None, 0):
                    failed = True
        claims_done = False
        if failed:
            lowered = text.lower()
            for phrase in ("done", "complete", "completed", "finished", "fixed", "succeeded", "all set"):
                if phrase in lowered:
                    claims_done = True
                    break
        completion = 1.0
        if not text.strip():
            completion = 0.0
        elif failed:
            completion = 0.6
        return {
            "completion": completion,
            "claims_done_but_failed": 0.6 if claims_done else 0.0,
            "source": "heuristic",
        }

    def memory(self, message, reply, outputs, verification, existing_layers=None):
        text = " ".join(str(reply or "").split()).lower()
        layer_type = "discovery"
        if re.search(r"\b(decided|decision|we will|chosen)\b", text):
            layer_type = "decision"
        if re.search(r"\b(fixed|corrected|instead|actually)\b", text):
            layer_type = "correction"
        is_correction = 0.6 if layer_type == "correction" else 0.0
        if verification.get("claims_done_but_failed", 0) >= 0.5:
            layer_type = "correction"
            is_correction = 0.6
        duplicate = self._duplicate_score(reply, existing_layers)
        return {
            "layer_type": layer_type,
            "is_correction": is_correction,
            "duplicate_of_existing_layer": duplicate,
            "source": "heuristic",
        }

    @staticmethod
    def _tokens(value):
        text = str(value or "").lower()
        return set(re.findall(r"[a-z0-9]+", text))

    @classmethod
    def _duplicate_score(cls, reply, existing_layers):
        """A high token overlap with an earlier outcome is a duplicate.

        The capture passes the last few distilled outcomes, so the comparison
        is against what the store would hold, not against the user's request.
        """
        current = cls._tokens(reply)
        if not current:
            return 0.0
        for existing in existing_layers or []:
            prior = cls._tokens(existing)
            if not prior:
                continue
            shared = len(current & prior)
            total = len(current | prior)
            if total == 0:
                continue
            overlap = shared / total
            if overlap >= 0.85:
                return 1.0
        return 0.0
