"""Deterministic synthetic fixture week for the Belief Engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

from .core import BeliefStore, DecayConfig


HONESTY_BANNER = (
    "HONESTY: This deterministic bookkeeping engine tracks belief provenance. "
    "All demo messages are synthetic fixtures. Confidence numbers are internal "
    "bookkeeping weights for ranking and review prioritisation, not calibrated "
    "probabilities or calibrated uncertainty. There is no Bayesian inference claim "
    "and no claim of real-world deployment."
)

STATEMENTS = [
    "The weekly report is due on Monday.",
    "The design review starts at 10:00.",
    "The service has a staging environment.",
    "The backup job runs every night.",
    "The team uses Python for the data pipeline.",
    "The launch checklist has an assigned owner.",
    "The dashboard includes an export option.",
    "The support rotation changes on Friday.",
    "The API has a documented rate limit.",
    "The release candidate passed a smoke check.",
    "The onboarding guide links to the runbook.",
    "The weekly sync is held remotely.",
    "The cache has a scheduled expiry policy.",
    "The project stores audit events as JSONL.",
    "The demo uses synthetic incoming messages.",
    "The data export includes a timestamp column.",
    "The service has a read-only status page.",
    "The team reviews open incidents each morning.",
    "The command line supports a review view.",
    "The nightly job writes a completion summary.",
    "The project targets CPU-only execution.",
    "The store keeps a history for each belief.",
    "The demo fixture set is deterministic.",
    "The user can configure the decay schedule.",
]


def _event_time(day: int, hour: int = 9) -> str:
    start = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    return (start + timedelta(days=day, hours=hour - 9)).isoformat().replace("+00:00", "Z")


def render_demo() -> str:
    """Run the fixture sequence in an isolated store and return stable output."""
    with tempfile.TemporaryDirectory(prefix="belief-engine-demo-") as directory:
        root = Path(directory)
        store = BeliefStore(
            root / "beliefs.json",
            audit_path=root / "be-audit.jsonl",
            decay_config=DecayConfig(grace_days=2, interval_days=1, retention=0.8),
        )
        opening: dict[str, float] = {}
        for index, statement in enumerate(STATEMENTS, start=1):
            belief = store.add_belief(
                statement,
                source=f"synthetic-fixture: baseline-{index:02d}",
                direction="supporting",
                weight=0.8,
                timestamp=_event_time(0),
                note="Synthetic opening message for the fixture week.",
            )
            opening[belief.belief_id] = belief.confidence

        for index, statement in enumerate(STATEMENTS, start=1):
            belief = store.find(statement)
            day = 1 + (index % 5)
            direction = "contradicting" if index % 4 in {0, 1} else "supporting"
            weight = 0.35 + (index % 4) * 0.1
            store.add_evidence(
                belief.belief_id,
                source=f"synthetic-fixture: week-message-{index:02d}",
                direction=direction,
                weight=weight,
                timestamp=_event_time(day, 11),
                note=f"Synthetic day {day} fixture, item {index:02d}.",
            )

        store.decay(timestamp=_event_time(7, 18))
        lines = [
            HONESTY_BANNER,
            "DEMO: deterministic synthetic fixture week, fixture seed 0, no randomness used.",
            f"Tracked beliefs: {len(STATEMENTS)}",
            "Opening confidence is after the day 0 baseline evidence. Closing confidence is after week messages and day 7 decay.",
            "",
            "BEFORE / AFTER MOVEMENT VIEW",
        ]
        for index, statement in enumerate(STATEMENTS, start=1):
            belief = store.find(statement)
            opening_confidence = opening[belief.belief_id]
            events = belief.history[1:]
            reasons = "; ".join(
                f"{event['event']}: {event['reason']}"
                for event in events
            ) or "No movement events."
            lines.append(
                f"B{index:02d} {statement} | {opening_confidence:.6f} -> "
                f"{belief.confidence:.6f} | {reasons}"
            )
        return "\n".join(lines)
