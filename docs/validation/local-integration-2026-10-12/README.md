# Local integration record — 2026-10-12 (maintenance commit E)

Docs-only maintenance evidence for the S1 local merge and the bounded cleanup.
This directory is added by the one-commit maintenance `E` on
`feat/grpo-rag-platform-v1`:

> `chore: retire stale S1 handoff and record local integration`

It records **actual** pre-merge, merge, cleanup and post-cleanup facts (taken
from the author's own runs and the independent reviewer's summary). It replaces
nothing: historical reports, ADRs and architecture documents keep their old
facts, and the outdated handoff contract `docs/opencode-s1-task.md` is retired
(its original bytes remain recoverable from the Git `A` history and the frozen
`A.patch.gz`). No business source, tests, schema, lock, provider, budget,
egress, `.gitignore` or frozen-release manifest is touched by `E`.

Authoritative evidence is referenced, not duplicated: the immutable
[S1 RC package](../s1-release-candidate-2026-10-11/README.md) inside the tree and
the persistent external backup
`/home/avaarg/.local/share/grpo-rag/releases/s1-local-20261011T161256Z/`.

## Baseline verified before any write

| Item | Value |
|---|---|
| main HEAD | `e244035d1164ddccb0b8e7562258c5e5484332e2` (D) |
| feature HEAD | `e244035d1164ddccb0b8e7562258c5e5484332e2` (D) |
| shared tree | `0257ef938aac95bede8b946ff77391291792f401` |
| both worktrees | clean (`git status --porcelain` = 0 lines) |
| feature worktree | registered `/home/avaarg/.local/share/grpo-rag/worktrees/s1-local-20261012` |

## Commit chain (all local, relative to frozen base)

| Commit | SHA | Message |
|---|---|---|
| base | `f0914adf7d97dc0271f80ba3e1db79dccdd44f99` | S0 verified baseline |
| A | `841f9765477e26608d2670243d6f7f077e82588e` | feat(s1): enforce model-profile authorization boundary + explicit stub gate + char evidence budget |
| B | `200eb817da236dcb68c88f17e014d54c466037b9` | feat(s1.1): require provider admission before key acquisition |
| C | `cef5e7a7c8adb5bddbcebe89e8e2f8615513d930` | feat(s1.3): refuse raw credential intake; whitelist model-center 422 fields |
| D | `e244035d1164ddccb0b8e7562258c5e5484332e2` | docs: archive immutable S1 RC evidence |
| E | recorded externally (no self-reference in committed manifest) | chore: retire stale S1 handoff and record local integration |

Trees per commit match the release account: A `e7c6c96a…`, B `c2d17414…`,
C `b3a5790e…`, D `0257ef93…`.

## Actual local merge into main

`git merge --ff-only feat/grpo-rag-platform-v1` on the shared repo
`/home/avaarg/GRPO-Rag`:

- result: `Updating f0914adf7d97..e244035d1164  Fast-forward`, exit 0
- main HEAD after = `e244035d1164…` (D), tree `0257ef93…`, status clean
- main is ahead of `origin/main` by 4; nothing pushed, no PR, no second merge
- reflog top: `merge feat/grpo-rag-platform-v1: Fast-forward`

## Test / lint / whitespace facts (author actual logs + independent review)

- Author full suite: **832 passed, 21 skipped**, exit 0. The 21 skips are the
  PG integration tests (`11 test_index_integration` + `10 test_storage_integration`),
  reason: `RAGOS_TEST_PG_DSN must explicitly identify a disposable local
  PostgreSQL DB` (DSN unset) — they are **not** counted as passed.
- Installed vs working-tree source digests: `INSTALLED_SHA_OK=True`, 170 `.py` files.
- Independent review (Codex): targeted C regression **98 passed**, verdict
  LOCAL PASS, explicitly stating no commit/merge authority is inferred. Summary
  committed at [codex-review](../s1-3-secret-intake/codex-review.md) and archived
  in `raw-evidence/bundle/{codex-review.md,codex-composition-review.json}`.
- Whole-repo ruff: **117 pre-existing errors** (116 × E501 + 1 × I001, all in
  migrations `0001_pg_scaffold.py`, `0004_model_gateway.py`,
  `0005_page_anchored_eval.py`); **0 new** this round; targeted ruff on changed
  files: all pass.
- Whitespace: historical `C` vs base `git diff --check` exits **2** (two RED
  logs, 16 trailing-whitespace lines, bytes preserved in the RC archive);
  `D` vs base and `D` vs `C` both exit **0**. `E` staged check: exit **0**.
- No Python change in `E`; the 832-suite was **not** re-run for `E` (not
  needed, and not presented as re-run).

## Bounded FS cleanup (actual, 2026-10-12)

Scope: shared main worktree only; **gitignored + untracked** paths; no symlink
followed; no install env, dependency cache, source, model, secret, other repo
or branch touched; `/tmp/opencode` untouched; feature worktree untouched.

| Target | Result |
|---|---|
| `build/` | deleted (119 files, 639,115 B) |
| `.pytest_cache/` | deleted (5 files, 77,404 B) |
| `.ruff_cache/` | deleted (38 files, 19,748 B) |
| `__pycache__` under `apps/packages/tests/scripts` | deleted (22 dirs, 361 files, 6,532,052 B) |
| `/tmp/grpo-rag-*` authorized targets (8 paths) | already absent — no-op, verified before run |
| `infra/migrations/{,versions}/__pycache__` | out of scope — left untouched (2 dirs) |
| `ragos.egg-info/` | not in authorized list — left untouched |

Totals: **523 files / 7,268,319 apparent bytes** removed. This is a cleanup
operation tally, **not** a performance benchmark and **not** a guarantee of
immediate disk-space reclaim. Per-target pre/post checks (ownership, git
non-tracked, registered worktrees, `/proc` cwd/exe/fd/maps) are logged in the
external `release/…/cleanup/` dir.

## NOT RUN / blocked (recorded, never reported as PASS)

- Real provider (Qwen/OpenAI) calls — not run.
- PostgreSQL / RLS live-database verification — not run.
- Qdrant server journey — not run.
- Browser/UI journey, migration execution — not run.
- **R1 still blocked**; no S1.4 / R1 / any new implementation.
- Feature worktree removal — **NOT RUN** (deferred to Codex after `E` merge).

## Machine-readable record

See [cleanup-manifest.json](cleanup-manifest.json) for the JSON form of the
above facts, including per-target deletion bytes.