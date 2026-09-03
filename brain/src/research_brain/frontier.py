"""Validation for compact Hot Frontier research objects."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


THREAD_STATUSES = {"active", "paused", "completed", "archived"}
QUESTION_STATUSES = {"open", "answered", "superseded", "closed"}
HYPOTHESIS_STATUSES = {"active", "supported", "weakened", "rejected", "superseded"}
TENSION_STATUSES = {"unresolved", "resolved", "superseded"}
USAGE_DISPOSITIONS = {
    "tried_and_failed",
    "considered_but_rejected",
    "not_applicable",
    "insufficient_evidence",
    "too_expensive",
    "did_not_discriminate_hypotheses",
    "contradicted_by_experiment",
    "superseded",
}

THREAD_LIST_FIELDS = {
    "known",
    "unknown",
    "constraints",
    "active_hypotheses",
    "tensions",
    "recent_observations",
    "open_questions",
    "pending_decisions",
    "pending_experiments",
}
THREAD_FIELDS = THREAD_LIST_FIELDS | {"goal", "status"}


def text(value: Any, label: str, *, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return result


def optional_text(value: Any, label: str, *, maximum: int = 20_000) -> str | None:
    if value is None:
        return None
    return text(value, label, maximum=maximum)


def strings(value: Sequence[str] | None, label: str, *, maximum_items: int = 100) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence of strings")
    if len(value) > maximum_items:
        raise ValueError(f"{label} exceeds {maximum_items} items")
    result = [text(item, f"{label} item", maximum=2_000) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def mapping(value: Mapping[str, Any] | None, label: str, *, required: bool = False) -> dict[str, Any]:
    if value is None:
        if required:
            raise ValueError(f"{label} is required")
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    result = dict(value)
    if required and not result:
        raise ValueError(f"{label} must not be empty")
    return result


def enum(value: str, label: str, allowed: set[str]) -> str:
    result = text(value, label, maximum=80)
    if result not in allowed:
        raise ValueError(f"unknown {label}: {result}")
    return result


def thread_payload(
    *,
    goal: str,
    status: str = "active",
    known: Sequence[str] = (),
    unknown: Sequence[str] = (),
    constraints: Sequence[str] = (),
    active_hypotheses: Sequence[str] = (),
    tensions: Sequence[str] = (),
    recent_observations: Sequence[str] = (),
    open_questions: Sequence[str] = (),
    pending_decisions: Sequence[str] = (),
    pending_experiments: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "schema": "ResearchThreadV1",
        "goal": text(goal, "goal"),
        "status": enum(status, "thread status", THREAD_STATUSES),
        "known": strings(known, "known"),
        "unknown": strings(unknown, "unknown"),
        "constraints": strings(constraints, "constraints"),
        "active_hypotheses": strings(active_hypotheses, "active_hypotheses"),
        "tensions": strings(tensions, "tensions"),
        "recent_observations": strings(recent_observations, "recent_observations"),
        "open_questions": strings(open_questions, "open_questions"),
        "pending_decisions": strings(pending_decisions, "pending_decisions"),
        "pending_experiments": strings(pending_experiments, "pending_experiments"),
    }


def update_thread_payload(current: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    unknown_fields = set(changes) - THREAD_FIELDS
    if unknown_fields:
        raise ValueError(f"unknown frontier fields: {', '.join(sorted(unknown_fields))}")
    result = dict(current)
    for key, value in changes.items():
        if key == "goal":
            result[key] = text(value, "goal")
        elif key == "status":
            result[key] = enum(value, "thread status", THREAD_STATUSES)
        else:
            result[key] = strings(value, key)
    result["schema"] = "ResearchThreadV1"
    return result
