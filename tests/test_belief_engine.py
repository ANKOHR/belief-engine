from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json

import pytest

from belief_engine.cli import main
from belief_engine.core import (
    BeliefStore,
    DecayConfig,
    Evidence,
    _confidence_after_evidence,
    replay_audit,
)
from belief_engine.demo import render_demo


BASE = datetime(2026, 9, 21, 9, tzinfo=timezone.utc)


def at(days: int = 0, hours: int = 0) -> str:
    return (BASE + timedelta(days=days, hours=hours)).isoformat().replace("+00:00", "Z")


def create(store: BeliefStore, statement: str = "The report is due Monday."):
    return store.add_belief(
        statement,
        source="synthetic-fixture: baseline",
        direction="supporting",
        weight=0.8,
        timestamp=at(),
        note="Baseline citation.",
    )


def test_store_persists_evidence_history_and_identical_reload(tmp_path):
    path = tmp_path / "beliefs.json"
    audit = tmp_path / "be-audit.jsonl"
    store = BeliefStore(path, audit_path=audit)
    belief = create(store)
    store.add_evidence(
        belief.belief_id,
        source="synthetic-fixture: followup",
        direction="supporting",
        weight=0.4,
        timestamp=at(1),
        note="Second citation.",
    )

    before = store.snapshot()
    reloaded = BeliefStore(path, audit_path=audit)
    assert reloaded.snapshot() == before
    loaded = reloaded.get(belief.belief_id)
    assert loaded.statement == "The report is due Monday."
    assert 0.0 <= loaded.confidence <= 1.0
    assert [item.source for item in loaded.evidence] == [
        "synthetic-fixture: baseline",
        "synthetic-fixture: followup",
    ]
    assert loaded.history[0]["event"] == "add"
    assert loaded.history[1]["event"] == "update"
    with pytest.raises(FrozenInstanceError):
        loaded.confidence = 0.1
    loaded.history.clear()
    assert reloaded.get(belief.belief_id).history


def test_update_rule_uses_documented_amount_and_records_before_after(tmp_path):
    store = BeliefStore(tmp_path / "beliefs.json")
    belief = create(store)
    initial = belief.confidence
    expected_support = round(initial + 0.25 * 0.4 * (1.0 - initial), 12)
    belief = store.add_evidence(
        belief.belief_id,
        source="synthetic-fixture: support",
        direction="supporting",
        weight=0.4,
        timestamp=at(1),
    )
    assert belief.confidence == expected_support
    update = belief.history[1]
    assert update["old_confidence"] == initial
    assert update["new_confidence"] == expected_support
    assert "supporting evidence" in update["reason"]

    expected_contradiction = round(expected_support - 0.25 * 0.4 * expected_support, 12)
    belief = store.add_evidence(
        belief.belief_id,
        source="synthetic-fixture: contradiction",
        direction="contradicting",
        weight=0.4,
        timestamp=at(2),
    )
    assert belief.history[3]["event"] == "update"
    assert belief.history[3]["old_confidence"] == expected_support
    assert belief.history[3]["new_confidence"] == expected_contradiction
    assert belief.confidence < expected_contradiction


def test_update_rule_stays_inside_inclusive_bounds(tmp_path):
    store = BeliefStore(tmp_path / "beliefs.json")
    belief = create(store)
    for index in range(160):
        belief = store.add_evidence(
            belief.belief_id,
            source=f"synthetic-fixture: repeated-{index}",
            direction="supporting",
            weight=1.0,
            timestamp=at(1, index),
        )
    assert 0.0 <= belief.confidence <= 1.0
    assert _confidence_after_evidence(
        1.0,
        Evidence("upper", "fixture", at(), 1.0, "supporting"),
    ) == 1.0
    assert _confidence_after_evidence(
        0.0,
        Evidence("lower", "fixture", at(), 1.0, "contradicting"),
    ) == 0.0
    with pytest.raises(ValueError):
        Evidence("bad", "fixture", at(), 1.1, "supporting")


def test_conflict_moves_toward_neutral_and_flags_evidence_for_review(tmp_path):
    store = BeliefStore(tmp_path / "beliefs.json")
    belief = create(store)
    before_conflict = belief.confidence
    belief = store.add_evidence(
        belief.belief_id,
        source="synthetic-fixture: conflicting note",
        direction="contradicting",
        weight=0.4,
        timestamp=at(1),
        note="Opposite direction fixture.",
    )
    conflict = belief.history[-1]
    assert conflict["event"] == "conflict"
    assert belief.status == "NEEDS_REVIEW"
    assert abs(belief.confidence - 0.5) < abs(before_conflict - 0.5)
    assert len(belief.conflict_evidence_ids) == 2
    assert set(belief.conflict_evidence_ids) == {item.evidence_id for item in belief.evidence}
    assert store.review() == [belief]


def test_decay_schedule_and_supporting_refresh(tmp_path):
    config = DecayConfig(grace_days=1, interval_days=1, retention=0.8)
    path = tmp_path / "beliefs.json"
    store = BeliefStore(path, decay_config=config)
    belief = create(store)
    opening = belief.confidence

    assert store.decay(timestamp=at(1)) == []
    changed = store.decay(timestamp=at(2))
    assert len(changed) == 1
    belief = changed[0]
    assert belief.confidence == round(0.5 + (opening - 0.5) * 0.8, 12)
    decayed = belief.confidence

    belief = store.add_evidence(
        belief.belief_id,
        source="synthetic-fixture: fresh support",
        direction="supporting",
        weight=0.8,
        timestamp=at(2),
    )
    assert belief.last_refresh_at == at(2)
    assert belief.confidence > decayed
    assert belief.history[-1]["event"] == "refresh"
    refreshed = belief.confidence
    assert store.decay(timestamp=at(3)) == []
    assert belief.confidence == refreshed
    belief = store.decay(timestamp=at(4))[0]
    assert belief.confidence == round(0.5 + (refreshed - 0.5) * 0.8, 12)
    assert BeliefStore(path).decay_config == config


def test_audit_sequence_replays_add_update_conflict_decay(tmp_path):
    path = tmp_path / "beliefs.json"
    audit = tmp_path / "be-audit.jsonl"
    store = BeliefStore(
        path,
        audit_path=audit,
        decay_config=DecayConfig(grace_days=0, interval_days=1, retention=0.8),
    )
    belief = create(store)
    store.add_evidence(
        belief.belief_id,
        source="synthetic-fixture: confirming followup",
        direction="supporting",
        weight=0.2,
        timestamp=at(0, 1),
    )
    store.add_evidence(
        belief.belief_id,
        source="synthetic-fixture: opposite message",
        direction="contradicting",
        weight=0.4,
        timestamp=at(0, 2),
    )
    store.decay(timestamp=at(2))

    entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    actions = [entry["action"] for entry in entries]
    assert {"add", "update", "refresh", "conflict", "decay"}.issubset(actions)
    assert all("old_confidence" in item and "new_confidence" in item for item in entries)
    replayed = replay_audit(audit)
    assert replayed[belief.belief_id].to_dict() == store.get(belief.belief_id).to_dict()


def test_demo_has_24_reasons_and_is_byte_for_byte_deterministic():
    first = render_demo()
    second = render_demo()
    rows = [line for line in first.splitlines() if line.startswith("B") and " | " in line]
    assert first == second
    assert "All demo messages are synthetic fixtures." in first
    assert "not calibrated probabilities or calibrated uncertainty" in first
    assert "no claim of real-world deployment" in first
    assert len(rows) == 24
    assert all(" -> " in line and "| " in line for line in rows)
    assert any("conflict:" in line for line in rows)
    assert any("decay:" in line for line in rows)


def test_cli_smoke_all_subcommands(tmp_path, capsys):
    store_path = str(tmp_path / "cli-beliefs.json")
    common = ["--store", store_path]

    def invoke(arguments):
        assert main(common + arguments) == 0
        output = capsys.readouterr().out
        assert "HONESTY:" in output
        return output

    invoke(["init"])
    invoke(["config", "--grace-days", "1", "--interval-days", "1", "--retention", "0.8"])
    created_output = invoke(
        [
            "add",
            "The CLI fixture statement.",
            "--source",
            "synthetic-fixture: cli-add",
            "--direction",
            "supporting",
            "--weight",
            "0.8",
            "--at",
            at(),
        ]
    )
    belief = BeliefStore(store_path).find("The CLI fixture statement.")
    assert belief.belief_id in created_output
    invoke(["list"])
    invoke(
        [
            "evidence",
            belief.belief_id,
            "--source",
            "synthetic-fixture: cli-opposite",
            "--direction",
            "contradicting",
            "--weight",
            "0.4",
            "--at",
            at(1),
        ]
    )
    review_output = invoke(["review"])
    assert "NEEDS_REVIEW" in review_output
    invoke(["show", belief.belief_id])
    invoke(["decay", "--at", at(4)])
    demo_output = invoke(["demo"])
    assert "BEFORE / AFTER MOVEMENT VIEW" in demo_output
