"""Failure signal weights; repeated occurrences do not multiply weights."""
SIGNAL_WEIGHTS = {
    "test_failure": 5,
    "build_failure": 5,
    "git_revert": 5,
    "explicit_user_correction": 4,
    "same_task_repeated": 3,
    "rapid_reedit": 2,
    "change_overwritten": 2,
    "negative_language": 1,
}


def confidence(score: int) -> str:
    if score < 0:
        raise ValueError("score cannot be negative")
    return "LOW" if score <= 3 else "MEDIUM" if score <= 7 else "HIGH"


def score_signals(signals: list[str]) -> int:
    return sum(SIGNAL_WEIGHTS[name] for name in set(signals))
