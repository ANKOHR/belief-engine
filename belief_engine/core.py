"""Belief persistence, evidence updates, decay, conflicts, and audit replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


NEUTRAL = 0.5
UPDATE_RATE = 0.25
CONFLICT_RETENTION = 0.5
SCHEMA_VERSION = 1


def normalize_timestamp(value: str | datetime) -> str:
    """Return an aware timestamp in stable UTC ISO 8601 form."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid ISO 8601 timestamp: {value!r}") from exc
    else:
        raise TypeError("timestamp must be an ISO 8601 string or datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class DecayConfig:
    """Confidence decay toward neutral after a grace window."""

    grace_days: float = 2.0
    interval_days: float = 1.0
    retention: float = 0.8

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.grace_days, self.interval_days, self.retention)):
            raise ValueError("decay parameters must be finite numbers")
        if self.grace_days < 0:
            raise ValueError("grace_days must be non-negative")
        if self.interval_days <= 0:
            raise ValueError("interval_days must be positive")
        if not 0.0 <= self.retention <= 1.0:
            raise ValueError("retention must be between 0 and 1")


@dataclass
class Evidence:
    evidence_id: str
    source: str
    timestamp: str
    weight: float
    direction: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.direction not in {"supporting", "contradicting"}:
            raise ValueError("direction must be supporting or contradicting")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError("evidence weight must be between 0 and 1")
        if not math.isfinite(self.weight):
            raise ValueError("evidence weight must be finite")
        if not self.source.strip():
            raise ValueError("evidence source must not be empty")
        self.timestamp = normalize_timestamp(self.timestamp)


@dataclass(frozen=True)
class Belief:
    belief_id: str
    statement: str
    confidence: float
    evidence: list[Evidence]
    history: list[dict[str, Any]]
    created_at: str
    last_refresh_at: str
    status: str = "ACTIVE"
    conflict_evidence_ids: list[str] = field(default_factory=list)
    decay_steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "belief_id": self.belief_id,
            "statement": self.statement,
            "confidence": self.confidence,
            "evidence": [asdict(item) for item in self.evidence],
            "history": self.history,
            "created_at": self.created_at,
            "last_refresh_at": self.last_refresh_at,
            "status": self.status,
            "conflict_evidence_ids": self.conflict_evidence_ids,
            "decay_steps": self.decay_steps,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Belief":
        return cls(
            belief_id=value["belief_id"],
            statement=value["statement"],
            confidence=float(value["confidence"]),
            evidence=[Evidence(**item) for item in value["evidence"]],
            history=deepcopy(value["history"]),
            created_at=normalize_timestamp(value["created_at"]),
            last_refresh_at=normalize_timestamp(value["last_refresh_at"]),
            status=value.get("status", "ACTIVE"),
            conflict_evidence_ids=list(value.get("conflict_evidence_ids", [])),
            decay_steps=int(value.get("decay_steps", 0)),
        )


def _rounded(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 12)


def _confidence_after_evidence(confidence: float, evidence: Evidence) -> float:
    """Apply the bounded linear evidence update with a fixed rate of 0.25."""
    if evidence.direction == "supporting":
        return _rounded(confidence + UPDATE_RATE * evidence.weight * (1.0 - confidence))
    return _rounded(confidence - UPDATE_RATE * evidence.weight * confidence)


class BeliefStore:
    """JSON-backed store with an append-only JSONL audit trail."""

    def __init__(
        self,
        path: str | Path = "beliefs.json",
        *,
        audit_path: str | Path | None = None,
        decay_config: DecayConfig | None = None,
    ) -> None:
        self.path = Path(path)
        self.audit_path = Path(audit_path) if audit_path else self.path.with_name("be-audit.jsonl")
        self._beliefs: dict[str, Belief] = {}
        self._audit_sequence = 0
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("Unsupported belief store schema version")
            self.decay_config = DecayConfig(**payload["decay_config"])
            self._beliefs = {
                item["belief_id"]: Belief.from_dict(item) for item in payload["beliefs"]
            }
            if decay_config is not None and decay_config != self.decay_config:
                raise ValueError("decay_config differs from the persisted store configuration")
        else:
            self.decay_config = decay_config or DecayConfig()
            self._save()
        if self.audit_path.exists():
            with self.audit_path.open("r", encoding="utf-8") as stream:
                self._audit_sequence = sum(1 for line in stream if line.strip())

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "update_rate": UPDATE_RATE,
            "neutral": NEUTRAL,
            "conflict_retention": CONFLICT_RETENTION,
            "decay_config": asdict(self.decay_config),
            "beliefs": [self._beliefs[key].to_dict() for key in sorted(self._beliefs)],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _record(
        self,
        belief: Belief,
        *,
        action: str,
        timestamp: str,
        old_confidence: float,
        reason: str,
        evidence_ids: Iterable[str] = (),
    ) -> None:
        self._audit_sequence += 1
        entry = {
            "sequence": self._audit_sequence,
            "timestamp": timestamp,
            "belief_id": belief.belief_id,
            "action": action,
            "old_confidence": old_confidence,
            "new_confidence": belief.confidence,
            "reason": reason,
            "evidence_ids": list(evidence_ids),
            "state": belief.to_dict(),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self._save()

    def add_belief(
        self,
        statement: str,
        *,
        source: str,
        direction: str,
        weight: float,
        timestamp: str | datetime,
        note: str = "",
    ) -> Belief:
        """Create a belief at neutral, then cite and apply its first evidence."""
        clean_statement = statement.strip()
        if not clean_statement:
            raise ValueError("statement must not be empty")
        at = normalize_timestamp(timestamp)
        belief_id = hashlib.sha256(clean_statement.encode("utf-8")).hexdigest()
        if belief_id in self._beliefs:
            raise ValueError(f"Belief already exists: {clean_statement}")
        Evidence(
            evidence_id="validation",
            source=source,
            timestamp=at,
            weight=float(weight),
            direction=direction,
            note=note,
        )
        belief = Belief(
            belief_id=belief_id,
            statement=clean_statement,
            confidence=NEUTRAL,
            evidence=[],
            history=[],
            created_at=at,
            last_refresh_at=at,
        )
        self._beliefs[belief_id] = belief
        self._apply_evidence(
            belief,
            source=source,
            direction=direction,
            weight=weight,
            timestamp=at,
            note=note,
            action="add",
        )
        return Belief.from_dict(belief.to_dict())

    def add_evidence(
        self,
        belief_id: str,
        *,
        source: str,
        direction: str,
        weight: float,
        timestamp: str | datetime,
        note: str = "",
    ) -> Belief:
        try:
            belief = self._beliefs[belief_id]
        except KeyError as exc:
            raise KeyError(f"Unknown belief id: {belief_id}") from exc
        self._apply_evidence(
            belief,
            source=source,
            direction=direction,
            weight=weight,
            timestamp=normalize_timestamp(timestamp),
            note=note,
            action="update",
        )
        return Belief.from_dict(belief.to_dict())

    def _apply_evidence(
        self,
        belief: Belief,
        *,
        source: str,
        direction: str,
        weight: float,
        timestamp: str,
        note: str,
        action: str,
    ) -> None:
        evidence = Evidence(
            evidence_id=f"{belief.belief_id[:12]}-e{len(belief.evidence) + 1:04d}",
            source=source,
            timestamp=timestamp,
            weight=float(weight),
            direction=direction,
            note=note,
        )
        old = belief.confidence
        belief.evidence.append(evidence)
        object.__setattr__(belief, "confidence", _confidence_after_evidence(belief.confidence, evidence))
        reason = (
            f"{source} supplied {direction} evidence with weight {evidence.weight:.3f}; "
            f"bounded update rate {UPDATE_RATE:.2f} applied."
        )
        self._append_history(belief, action, timestamp, old, belief.confidence, reason, [evidence.evidence_id])
        self._record(
            belief,
            action=action,
            timestamp=timestamp,
            old_confidence=old,
            reason=reason,
            evidence_ids=[evidence.evidence_id],
        )

        if action == "add":
            object.__setattr__(belief, "last_refresh_at", timestamp)
        elif direction == "supporting":
            old = belief.confidence
            object.__setattr__(belief, "last_refresh_at", timestamp)
            object.__setattr__(belief, "decay_steps", 0)
            refresh_reason = f"Supporting evidence from {source} refreshed the evidence age clock."
            self._append_history(belief, "refresh", timestamp, old, old, refresh_reason, [evidence.evidence_id])
            self._record(
                belief,
                action="refresh",
                timestamp=timestamp,
                old_confidence=old,
                reason=refresh_reason,
                evidence_ids=[evidence.evidence_id],
            )

        supporting = [item for item in belief.evidence if item.direction == "supporting"]
        contradicting = [item for item in belief.evidence if item.direction == "contradicting"]
        if supporting and contradicting:
            old = belief.confidence
            object.__setattr__(
                belief,
                "confidence",
                _rounded(NEUTRAL + (old - NEUTRAL) * CONFLICT_RETENTION),
            )
            object.__setattr__(belief, "status", "NEEDS_REVIEW")
            object.__setattr__(
                belief,
                "conflict_evidence_ids",
                [item.evidence_id for item in belief.evidence],
            )
            conflict_reason = (
                "Supporting and contradicting evidence coexist; confidence distance from neutral "
                f"was reduced by retention {CONFLICT_RETENTION:.2f}. Review the cited evidence."
            )
            self._append_history(
                belief,
                "conflict",
                timestamp,
                old,
                belief.confidence,
                conflict_reason,
                belief.conflict_evidence_ids,
            )
            self._record(
                belief,
                action="conflict",
                timestamp=timestamp,
                old_confidence=old,
                reason=conflict_reason,
                evidence_ids=belief.conflict_evidence_ids,
            )

    @staticmethod
    def _append_history(
        belief: Belief,
        event: str,
        timestamp: str,
        old: float,
        new: float,
        reason: str,
        evidence_ids: list[str],
    ) -> None:
        belief.history.append(
            {
                "event": event,
                "timestamp": timestamp,
                "old_confidence": old,
                "new_confidence": new,
                "reason": reason,
                "evidence_ids": list(evidence_ids),
            }
        )

    def decay(self, *, timestamp: str | datetime) -> list[Belief]:
        at = normalize_timestamp(timestamp)
        changed: list[Belief] = []
        grace_seconds = self.decay_config.grace_days * 86400.0
        interval_seconds = self.decay_config.interval_days * 86400.0
        for belief_id in sorted(self._beliefs):
            belief = self._beliefs[belief_id]
            age_seconds = max(0.0, (_moment(at) - _moment(belief.last_refresh_at)).total_seconds())
            due_steps = math.floor(max(0.0, age_seconds - grace_seconds) / interval_seconds)
            extra_steps = due_steps - belief.decay_steps
            if extra_steps <= 0:
                continue
            old = belief.confidence
            for _ in range(extra_steps):
                object.__setattr__(
                    belief,
                    "confidence",
                    _rounded(NEUTRAL + (belief.confidence - NEUTRAL) * self.decay_config.retention),
                )
            object.__setattr__(belief, "decay_steps", due_steps)
            reason = (
                f"Evidence age exceeded {self.decay_config.grace_days:g} day grace by "
                f"{extra_steps} full interval(s); confidence moved toward neutral with "
                f"retention {self.decay_config.retention:.2f}."
            )
            self._append_history(belief, "decay", at, old, belief.confidence, reason, [])
            self._record(belief, action="decay", timestamp=at, old_confidence=old, reason=reason)
            changed.append(Belief.from_dict(belief.to_dict()))
        return changed

    def configure_decay(self, config: DecayConfig) -> None:
        """Persist the decay schedule used by future decay operations."""
        self.decay_config = config
        self._save()

    def get(self, belief_id: str) -> Belief:
        try:
            return Belief.from_dict(self._beliefs[belief_id].to_dict())
        except KeyError as exc:
            raise KeyError(f"Unknown belief id: {belief_id}") from exc

    def find(self, statement: str) -> Belief:
        belief_id = hashlib.sha256(statement.strip().encode("utf-8")).hexdigest()
        return self.get(belief_id)

    def all(self) -> list[Belief]:
        return [Belief.from_dict(self._beliefs[key].to_dict()) for key in sorted(self._beliefs)]

    def review(self) -> list[Belief]:
        return [belief for belief in self.all() if belief.status == "NEEDS_REVIEW"]

    def snapshot(self) -> list[dict[str, Any]]:
        return [belief.to_dict() for belief in self.all()]


def replay_audit(path: str | Path) -> dict[str, Belief]:
    """Replay the state snapshots in an audit log and return final beliefs."""
    beliefs: dict[str, Belief] = {}
    expected_sequence = 1
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid audit JSON on line {line_number}") from exc
            if entry.get("sequence") != expected_sequence:
                raise ValueError(f"Unexpected audit sequence on line {line_number}")
            belief = Belief.from_dict(entry["state"])
            if belief.belief_id != entry.get("belief_id"):
                raise ValueError(f"Audit belief id mismatch on line {line_number}")
            beliefs[belief.belief_id] = belief
            expected_sequence += 1
    return beliefs
