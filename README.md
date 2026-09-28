# Belief Engine

Belief Engine is a deterministic, CPU-only store for statements, cited evidence, bounded confidence bookkeeping, review flags, decay, and an append-only audit trail. It preserves how a belief changed instead of silently replacing a value.

## Quickstart

Requires Python 3.10 or newer. The core uses only the Python standard library. Install pytest if it is not already available to run the test suite.

```powershell
python -m belief_engine init
python -m belief_engine add "The weekly report is due Monday." `
  --source "synthetic-fixture: planning note" --direction supporting --weight 0.8 `
  --at 2026-09-28T09:00:00Z --note "Example evidence citation"
python -m belief_engine list
python -m belief_engine review
python -m belief_engine demo
python -m pytest -q
```

The `--store` option selects a JSON store path. Its audit trail defaults to `be-audit.jsonl` beside that file. Use `--audit` to select another JSONL path. Evidence operations require an explicit timezone-aware timestamp so that the same inputs produce the same stored state.

```text
python -m belief_engine --store beliefs.json config --grace-days 2 --interval-days 1 --retention 0.8
python -m belief_engine --store beliefs.json add "A statement" --source "message-17" --direction supporting --weight 0.7 --at 2026-09-28T09:00:00Z
python -m belief_engine --store beliefs.json evidence BELIEF_ID --source "message-18" --direction contradicting --weight 0.6 --at 2026-09-28T11:00:00Z
python -m belief_engine --store beliefs.json decay --at 2026-10-02T09:00:00Z
python -m belief_engine --store beliefs.json show BELIEF_ID
```

## Architecture

```text
Incoming evidence
       |
       v
 Evidence validation -----> source, timestamp, weight, direction, note
       |
       v
 Bounded update rule -----> append evidence and before/after history
       |                           |
       +---- opposite evidence ----+----> NEEDS_REVIEW and neutralizing adjustment
       |
       +---- elapsed age ----------+----> configured decay toward neutral
       |
       v
 JSON belief store <----------> JSONL audit snapshots and replay
       |
       +----> list, show, review, deterministic fixture-week demo
```

The `belief_engine.core` module validates and stores the data. `belief_engine.cli` exposes store operations. `belief_engine.demo` runs the fixed synthetic week in a temporary store and reports the actual changes produced by the same update, conflict, decay, and audit code used by the CLI.

## Honesty boundary

All incoming messages in the demo are synthetic fixtures. Confidence numbers are internal bookkeeping weights for ranking and review prioritisation, not calibrated probabilities and not calibrated uncertainty. This is a deterministic bookkeeping engine for belief provenance. It makes no Bayesian inference claim and no claim of calibrated real-world confidence. There is no claim of real-world deployment. The CLI output and demo header repeat this boundary.

## Update rule and parameters

Confidence `c` is bounded to `[0, 1]`. The neutral point is `n = 0.5`, and the fixed evidence update rate is `alpha = 0.25`. Each evidence weight `w` is also bounded to `[0, 1]`.

```text
supporting:    c' = clamp(c + alpha * w * (1 - c), 0, 1)
contradicting: c' = clamp(c - alpha * w * c,       0, 1)
```

The store rounds each result to 12 decimal places for stable JSON output. A new belief starts at neutral and must include its first evidence citation, so it has no unsupported initial confidence. Returned belief records are detached snapshots, and their confidence field is read-only. Later confidence changes only through evidence or the documented decay operation. Every change records old confidence, new confidence, timestamp, reason, and related evidence IDs in both the belief history and audit log.

When both supporting and contradicting evidence exist, the belief is flagged `NEEDS_REVIEW`. After applying the incoming evidence update, the store reduces distance from neutral using `c' = n + 0.5 * (c - n)`. The conflict record cites all evidence currently attached to the belief. The store does not automatically clear review status.

## Decay schedule

The default persisted schedule is a two-day grace period, one full day per decay interval, and a retention factor of `0.8`. For each full interval beyond the grace period:

```text
c' = n + retention * (c - n)
```

This moves bookkeeping confidence toward neutral, rather than toward zero. A supporting evidence event sets `last_refresh_at` to its timestamp and resets the number of applied decay intervals. Contradicting evidence changes the score but does not refresh the age clock. The `config` command updates the schedule; `grace_days`, `interval_days`, and `retention` are persisted with the store. Decay is idempotent at a fixed timestamp and applies only intervals not previously applied since the last support refresh.

## Evidence model and provenance

Every belief has at least one evidence entry. Each entry records a stable evidence ID, a source label, an aware timestamp normalized to UTC, a weight from zero to one, a `supporting` or `contradicting` direction, and an optional note. A belief also stores its statement, bounded confidence, creation and last-refresh timestamps, review state, conflicting evidence IDs, applied decay interval count, and ordered update history.

The `be-audit.jsonl` log is append-only. Each add, evidence update, refresh, conflict flag, and decay operation writes an entry with a sequence number, timestamp, action, old and new confidence, reason, related evidence IDs, and a full after-event belief snapshot. `replay_audit(path)` checks the sequence and belief IDs, then reconstructs the final belief records from those snapshots.

## Deterministic demo

The fixture week tracks 24 beliefs from a fixed list and applies fixed synthetic evidence and decay timestamps. The fixture seed is `0`; no random number generator is used. Running `python -m belief_engine demo` twice produces identical before/after movement views, including the evidence and decay reasons for each row.
