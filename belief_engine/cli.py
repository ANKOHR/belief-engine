"""Command line interface for the deterministic belief bookkeeping engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .core import BeliefStore, DecayConfig
from .demo import HONESTY_BANNER, render_demo


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="belief-engine")
    parser.add_argument("--store", default="beliefs.json", help="belief store JSON path")
    parser.add_argument("--audit", default=None, help="audit JSONL path, defaults beside store")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create or open a store")
    config = commands.add_parser("config", help="show or update the decay schedule")
    config.add_argument("--grace-days", type=float)
    config.add_argument("--interval-days", type=float)
    config.add_argument("--retention", type=float)
    commands.add_parser("list", help="list beliefs and current review status")
    commands.add_parser("review", help="show flagged beliefs and their evidence trail")
    commands.add_parser("demo", help="run the deterministic synthetic fixture week")

    add = commands.add_parser("add", help="create a belief with its first evidence citation")
    add.add_argument("statement")
    _add_evidence_arguments(add)

    evidence = commands.add_parser("evidence", help="cite new evidence for a belief id")
    evidence.add_argument("belief_id")
    _add_evidence_arguments(evidence)

    decay = commands.add_parser("decay", help="apply configured age decay")
    decay.add_argument("--at", required=True, help="timezone-aware ISO 8601 timestamp")

    show = commands.add_parser("show", help="show one complete belief record")
    show.add_argument("belief_id")
    return parser


def _add_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, help="source label for the evidence")
    parser.add_argument(
        "--direction", required=True, choices=("supporting", "contradicting")
    )
    parser.add_argument("--weight", required=True, type=float)
    parser.add_argument("--at", required=True, help="timezone-aware ISO 8601 timestamp")
    parser.add_argument("--note", default="", help="short evidence note")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "demo":
        print(render_demo())
        return 0
    print(HONESTY_BANNER)

    store = BeliefStore(args.store, audit_path=args.audit)
    if args.command == "init":
        print(f"Initialized store: {Path(args.store)}")
        return 0
    if args.command == "config":
        current = store.decay_config
        if any(value is not None for value in (args.grace_days, args.interval_days, args.retention)):
            store.configure_decay(
                DecayConfig(
                    grace_days=args.grace_days if args.grace_days is not None else current.grace_days,
                    interval_days=args.interval_days if args.interval_days is not None else current.interval_days,
                    retention=args.retention if args.retention is not None else current.retention,
                )
            )
        print(_json(vars(store.decay_config)))
        return 0
    if args.command == "list":
        rows = [
            {
                "belief_id": belief.belief_id,
                "statement": belief.statement,
                "confidence": belief.confidence,
                "status": belief.status,
                "evidence_count": len(belief.evidence),
            }
            for belief in store.all()
        ]
        print(_json(rows))
        return 0
    if args.command == "review":
        rows = [belief.to_dict() for belief in store.review()]
        print(_json(rows))
        return 0
    if args.command == "add":
        belief = store.add_belief(
            args.statement,
            source=args.source,
            direction=args.direction,
            weight=args.weight,
            timestamp=args.at,
            note=args.note,
        )
        print(_json(belief.to_dict()))
        return 0
    if args.command == "evidence":
        belief = store.add_evidence(
            args.belief_id,
            source=args.source,
            direction=args.direction,
            weight=args.weight,
            timestamp=args.at,
            note=args.note,
        )
        print(_json(belief.to_dict()))
        return 0
    if args.command == "decay":
        changed = store.decay(timestamp=args.at)
        print(_json([belief.to_dict() for belief in changed]))
        return 0
    if args.command == "show":
        print(_json(store.get(args.belief_id).to_dict()))
        return 0
    parser.error(f"Unhandled command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
