# Repository Guide

This repository contains an AI engineering sample and runnable product prototype for turning meeting transcripts into reviewable action items. Public contribution guidance lives here; tool-specific memory and private working notes should stay outside version control.

## Start here

- Product overview and quick start: `README.md`
- Architecture: `docs/architecture.md`
- Evaluation protocol and measured results: `docs/evaluation.md`
- Known limitations: `docs/known-limitations.md`
- Current capability status: `capabilities.json`
- Detailed design record: `docs/design/`

Treat `capabilities.json` and executable tests as the current implementation record. Some detailed design documents preserve historical decisions and may describe superseded work.

## Common commands

```bash
python -m unittest discover -s tests
python -m collab_agent.cli demo
python -m collab_agent.cli serve --db demo.sqlite3 --host 127.0.0.1 --port 8000
```

PostgreSQL behavior must also pass the integration workflow in `.github/workflows/ci.yml`.

## Architectural invariants

1. Models may propose candidates, summaries, and linkage suggestions. They do not decide identity, authorization, workflow state, approval, escalation level, or version pointers.
2. Participant membership is the authorization boundary. Do not infer meeting access from transcript text.
3. Domain state, audit events, and outbox effects are committed atomically.
4. External delivery is idempotent by `EffectId`; retries reuse the original identifier.
5. SQLite and PostgreSQL share the same domain semantics. Backend-specific capabilities must be explicit and must not silently change behavior.
6. Operations that can be implemented and verified deterministically should remain in code rather than becoming model tools.
7. Default tests must run offline. Model providers, clocks, and external adapters require injectable test doubles.

## Change discipline

- Preserve existing user changes and keep unrelated edits out of a patch.
- Add or update tests for behavior changes.
- Comments should explain why a constraint exists, not restate the code.
- Do not commit transcripts, credentials, evaluation predictions containing corpus text, or private agent memory.
- When measured behavior changes, update `docs/evaluation.md` and the relevant capability entry instead of copying stale test counts into multiple documents.

