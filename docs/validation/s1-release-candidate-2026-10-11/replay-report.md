# Replay report — BASE → A → B → C → D

This report describes an **evidence-only** replay performed in an isolated git
repository. Nothing was committed, and the shared working tree was not touched.

## Isolated repository

- Shared repository (read-only): `/home/avaarg/GRPO-Rag`
- Isolated repository (this package's root): `/tmp/grpo-rag-rc-packaging-20261011`
- Base fetched read-only from the shared repository:
  commit `f0914adf7d97dc0271f80ba3e1db79dccdd44f99`, tree
  `089c467d5ed9d68ef0d6b382ba395bfcb03b07db`
- The final D tree object was produced with `git write-tree`; **no commit was
  created** in the isolated repository (`HEAD` stays detached at the base
  commit).

## Replayed trees

| Step | Operation | Resulting git tree |
|---|---|---|
| BASE | `git checkout` of the fetched base commit | `089c467d5ed9d68ef0d6b382ba395bfcb03b07db` |
| A | `git apply --index patches/A.patch` on BASE | `e7c6c96a84a537a08b9daaf0ca533b884bb7773d` |
| B | `git apply --index patches/B.patch` on A | `c2d17414f6b0d3f22fb5151b8e9b05e2ae22df56` |
| C | `git apply --index patches/C.patch` on B | `b3a5790e64a49096b1e359b3529bfda03a3633fe` |
| D | this package: remove 2 loose RED logs, add this evidence directory | recorded in the external approval manifest |

Every `git apply --check` and `git apply --index` exited 0. The A/B/C trees match
the prior-round replay proof byte-for-byte (`raw-evidence/prior-freeze/`). At C
the tree holds 286 tracked + untracked non-ignored files.

## What D changes relative to C

- **Removes** `docs/validation/s1-3-secret-intake/red-run.txt`
- **Removes** `docs/validation/s1-3-secret-intake/rework-red-run.txt`
- **Adds** `docs/validation/s1-release-candidate-2026-10-11/` (this directory)
- Changes **nothing else**: no source, tests, schema, migration, lock file, or
  configuration content differs outside this evidence wrapper.

The three frozen patch bundles are added as **deterministic gzip wrappers**
(`patches/A.patch.gz` `B.patch.gz` `C.patch.gz`, produced with `gzip -n -9`,
MTIME = 0, no filename). `gzip -dc` restores the exact original patch bytes; the
original A/B/C sha256 is the SHA **after decompression** and is unchanged. The
wrappers carry their own independent sha256 (listed in `manifest.sha256`). No
`.gitattributes`, `.git/info/attributes`, or `core.whitespace` exception is used:
git's default binary detection classifies gzip data as binary.

`D.patch` (relative to C, full git binary patch) is emitted **outside** the
release directory, at the isolation root. Its digest, and the resulting D tree
digest, are recorded in the external approval manifest
`/tmp/grpo-rag-rc-packaging-20261011/approval-manifest.json` — deliberately not
in this file, to avoid the tree hash depending on a file that states it.

## Content equality (C → D, excluding the evidence wrapper)

Every file present in both C and D is byte-identical. The only paths that differ
are the two removed RED text logs and the new release-candidate directory. This
was verified by comparing `git ls-tree` blob hashes and work-tree sha256 digests.

## Staged whitespace checks

| Check | Result |
|---|---|
| `git diff --cached --check` (index = C, HEAD = BASE) | **exit 2** — the two loose RED logs (historical fact, preserved) |
| `git diff --cached --check` (index = D, HEAD = BASE) | exit 0 |
| `git diff --cached --check` (index = D, vs C tree) | exit 0 |

The exit-2 result is the *pre-fix* candidate; it is not relabelled. D fixes it by
moving the two raw RED logs into `raw-evidence.tar.gz`.

## Binary handling

The only binary content D introduces is `raw-evidence.tar.gz` (unchanged this
rework) and the three `patches/*.patch.gz` gzip wrappers. Both are detected as
binary by git's default heuristic, so `git diff --cached --check` is not
line-oriented over them and no whitespace rule is bent. This candidate defines
**no** attribute or config exception: there is no `.gitattributes` and no
`.git/info/attributes` in the tree, and the reproduction flow sets no
`core.whitespace`.

## Boundary

Local only. The `832 passed / 21 skipped` figure is not production readiness.
PostgreSQL / RLS, real model providers and Qdrant Server are **not verified**;
the 21 skipped tests are skipped, never passed. See `test-matrix.md`.
