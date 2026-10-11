# S1.3 Codex independent review

Verdict: LOCAL PASS. No commit, push, merge, S1.4 or R1 execution.

Base HEAD: `f0914adf7d97dc0271f80ba3e1db79dccdd44f99`. Acceptance applies to the final uncommitted candidate identified by `source-digest-rework.txt`, not HEAD alone.

## Independent execution

Command (exit 0):

```sh
/tmp/opencode/s1-3/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_secret_intake.py tests/test_model_center_route.py tests/test_provider_admission.py tests/test_model_gateway.py tests/test_model_profile_authorization.py tests/test_provider_capability.py tests/test_gateway_evidence_budget.py
```

Result: **98 passed in 1.93s**. Includes raw credential rejection, malformed/invalid request redaction, captured-log checks, direct repository refusal and S1.1 regression.

Separate independent probe with a real SQLModel Session and SQLite engine: direct `KeyPoolRepository.create` returned domain validation status 422. Observed SQL statements=0, add=0, flush=0, commit=0. Exception response serialization did not contain the fictional sentinel. No database tables or persistent data were changed.

Independent dynamic-dictionary probe initially demonstrated that identifier-shaped secret keys leaked through the regular-expression location check. OpenCode reworked the handler using route-declared locations. Repeating the probe against the final installed candidate returned 422 with field=`body`, without the sentinel. OpenAPI now advertises only 422 for the disabled credential creation route.

All three implementation modules imported from the noneditable installation matched their working-tree SHA256 digests. Independent Ruff and `git diff --check` exited 0. Comparing the pre-task file hashes against the final candidate found zero changes outside the authorized implementation, tests and S1.3 delivery paths; existing S1/S1.1 WIP was preserved.

## Reviewed author evidence

OpenCode's final full-suite log: **832 passed, 21 skipped**. The 21 PostgreSQL integration tests were skipped because `RAGOS_TEST_PG_DSN` was not provided; they are not accepted as passed. The reviewer did not independently rerun the complete suite. Earlier 811-pass candidate evidence is superseded by the rework candidate.

## Limits

This closes unsafe raw credential intake only. It does not implement Secret Alias, rotation, budget, egress or real providers. Existing credentials were neither inspected nor changed. PostgreSQL/RLS, Qdrant Server and real provider execution remain unverified. Framework validation exceptions still hold input internally; the accepted guarantee covers outward responses and the currently tested log path, not arbitrary future logging of exception objects. Unsupported annotation/location shapes conservatively lose field detail rather than expose dynamic input.

Keep the fail-closed behavior on rollback. Do not restore plaintext creation, reset/clean the existing worktree, or infer authorization to commit or merge from this verdict.
