from collections import Counter
from datetime import datetime, timezone
import json
import re
from uuid import uuid4


class Problem(Exception):
    def __init__(self, code, message, status=422, details=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).timestamp()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value
        ):
            raise ValueError()
        date = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError()
        return date.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise Problem("invalid_time", "时间必须是带时区的 RFC3339 时间") from exc


TERMINAL = {"SUCCEEDED", "FAILED", "UNCERTAIN", "CANCELLED", "EXPIRED"}
DEFINITE = TERMINAL - {"UNCERTAIN"}
ACTIVE_ATTEMPTS = {"CREATED", "STARTING", "RUNNING"}
TASK_TRANSITIONS = {
    "QUEUED": {"CLAIMED", "CANCELLED", "EXPIRED"},
    "CLAIMED": {"VALIDATING", "RECOVERY_PENDING", "CANCELLED", "RETRY_WAIT", "FAILED"},
    "VALIDATING": {"RUNNING", "RECOVERY_PENDING", "CANCELLED", "RETRY_WAIT", "FAILED"},
    "RUNNING": {"SUCCEEDED", "RETRY_WAIT", "FAILED", "UNCERTAIN", "CANCELLED", "RECOVERY_PENDING"},
    "RETRY_WAIT": {"CLAIMED", "CANCELLED"},
    "RECOVERY_PENDING": {"SUCCEEDED", "RETRY_WAIT", "FAILED", "UNCERTAIN", "CANCELLED"},
    "UNCERTAIN": {"SUCCEEDED", "FAILED"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
    "EXPIRED": set(),
}
ATTEMPT_TRANSITIONS = {
    "CREATED": {"STARTING", "RETRYABLE_FAILURE", "PERMANENT_FAILURE", "CANCELLED"},
    "STARTING": {"RUNNING", "RETRYABLE_FAILURE", "PERMANENT_FAILURE", "CANCELLED", "UNCERTAIN"},
    "RUNNING": {"SUCCEEDED", "RETRYABLE_FAILURE", "PERMANENT_FAILURE", "CANCELLED", "UNCERTAIN"},
    **{
        x: set()
        for x in ("RETRYABLE_FAILURE", "PERMANENT_FAILURE", "SUCCEEDED", "UNCERTAIN", "CANCELLED")
    },
}


def transition(old, new, *, attempt=False):
    table = ATTEMPT_TRANSITIONS if attempt else TASK_TRANSITIONS
    if new not in table.get(old, set()):
        raise Problem("invalid_transition", f"不允许状态转换 {old} → {new}", 409)


def aggregate(states):
    counts = dict(Counter(states))
    if not counts:
        status = "NOT_SUBMITTED"
    elif "UNCERTAIN" in counts:
        status = "NEEDS_ATTENTION"
    elif any(s not in TERMINAL for s in counts):
        status = "IN_PROGRESS"
    elif set(counts) == {"SUCCEEDED"}:
        status = "SUCCEEDED"
    elif "SUCCEEDED" in counts:
        status = "PARTIAL_SUCCESS"
    else:
        status = "FINISHED_WITHOUT_SUCCESS"
    return {"status": status, "counts": counts}
