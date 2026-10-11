# Test matrix — local-only evidence

All figures below are **local, single-machine** evidence gathered in isolated
copies. They are historical records of the S1 / S1.1 / S1.3 work; this packaging
round added no source code and therefore did not re-run the full suite. Nothing
here is production readiness.

## Environment (frozen, per group)

- Python `3.11.17` (uv-managed), uv `0.12.23`
- Frozen requirement export inside the candidate tree:
  `uv export --frozen --extra dev --no-hashes --no-emit-project --all-packages`
- Non-editable install: `uv pip install --no-deps --no-editable <tree>`, no
  `PYTHONPATH`
- Installed `site-packages` copies sha256-compared back to candidate sources:
  `INSTALLED_SHA_OK=True` (170 `.py` files each group)
- Frozen dependency content digest identical across groups:
  `d22e9136107e4bb0cccbafefed77d0c457a04fd5675980c5cab78a71d270be68`
  (lock files differ only by the `uv export -o <path>` header line)

## Full suite per stage

| Stage | Tree | pytest | exit | targeted ruff |
|---|---|---|---|---|
| baseline | `HEAD` | 754 passed, 21 skipped | 0 | All checks passed! |
| A | `HEAD` + A | 778 passed, 21 skipped | 0 | All checks passed! |
| B | `HEAD` + A + B | 791 passed, 21 skipped | 0 | All checks passed! |
| C | `HEAD` + A + B + C | 832 passed, 21 skipped | 0 | All checks passed! |

Independent reviewer targeted runs (separate from the author full suites):
A `35 passed`, B `42 passed`, C `98 passed`; every command exited 0.

## Skipped (never counted as passed)

21 tests are **skipped**, all for the same reason — `RAGOS_TEST_PG_DSN must
explicitly identify a disposable local PostgreSQL DB`:

- `tests/test_index_integration.py` — 11
- `tests/test_storage_integration.py` — 10

Identical skip set in baseline / A / B / C.

## Lint

- Whole-repo `ruff`: a constant **117 pre-existing errors** in every tree
  (116 × E501, 1 × I001) in `infra/migrations/versions/0001_pg_scaffold.py`,
  `0004_model_gateway.py`, `0005_page_anchored_eval.py`. Pre-existing, not fixed,
  not a new regression in any stage.
- Targeted `ruff` on each stage's own changed / new Python files: all pass.

## Historical vs. this round

| Metric | Historical (recorded earlier) | Independent replay this round |
|---|---|---|
| S1 selected regression / baseline | 120 / 11 | A full: 778 passed, 21 skipped |
| S1.1 full suite | 791 passed, 21 skipped | B full: 791 passed, 21 skipped |
| S1.3 rework full suite | 832 passed, 21 skipped | C full: 832 passed, 21 skipped |
| S1.3 review targeted | 98 passed | reviewer targeted: 98 passed |

## Packaging verification (this rework)

This round performed **packaging verification only** — no A/B/C source audit, no
full pytest re-run, no `git fsck`. The frozen source content and file modes are
unchanged (verified by comparing C against the shared working tree and by the
fresh replay reaching the same tree). Checks performed:

- `gzip -dc patches/{A,B,C}.patch.gz | sha256sum` equals the frozen original
  A/B/C patch SHA (`f5438412…`, `5fdd7fa6…`, `9527418e…`).
- Wrapper determinism: rebuilding each `*.patch.gz` with `gzip -n -9` is
  byte-identical (header MTIME = 0, no filename).
- No `.gitattributes` / `.git/info/attributes` / `core.whitespace` exception in
  the candidate; gzip is detected as binary by git's default heuristic.
- `git diff --cached --check` exits 0 both against BASE and against tree C.
- `sha256sum -c manifest.sha256` all OK; `raw-members.sha256` each member OK.
- Fresh `BASE → A → B → C → D.patch` replay reaches the same D tree.

## NOT RUN (never reported as PASS)

- No real provider (Qwen / OpenAI) call; no network model dispatch.
- No PostgreSQL / Redis / Qdrant service; 21 PG integration tests skipped.
- No RLS verification against a live database.
- No browser / UI journey; no migration execution.
- No S1.4 / R1 or any new implementation.
- No historical artefact rewritten; historical numbers remain history only.
