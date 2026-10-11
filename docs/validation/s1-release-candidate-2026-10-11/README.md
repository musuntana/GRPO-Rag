# S1 Release Candidate — immutable evidence package (2026-10-11)

This directory is the **evidence-only** packaging commit `D` for the S1 / S1.1 /
S1.3 release candidate. It is the only content D adds on top of stage C. D adds
**no** source code, tests, schema, migrations, prompts, parsers or dependency
changes; it only relocates raw evidence into a reproducible archive and adds
this immutable manifest set.

- Base commit (frozen): `f0914adf7d97dc0271f80ba3e1db79dccdd44f99`
- Base tree: `089c467d5ed9d68ef0d6b382ba395bfcb03b07db`
- Stage A tree (`HEAD` + patch A): `e7c6c96a84a537a08b9daaf0ca533b884bb7773d`
- Stage B tree (`A` + patch B): `c2d17414f6b0d3f22fb5151b8e9b05e2ae22df56`
- Stage C tree (`B` + patch C): `b3a5790e64a49096b1e359b3529bfda03a3633fe`

The **new D tree hash** and the **`D.patch` hash** are deliberately *not*
written in this directory. They are recorded only in the external approval
manifest (`approval-manifest.json`), which lives outside the git tree, so this
package never references the hash of the tree that contains it. There is no
self-reference.

## Why this directory exists

Stage C is byte-identical to the shared working tree (286 tracked + untracked
non-ignored files). Two raw RED logs are verbatim `pytest` output and therefore
intentionally contain trailing whitespace:

- `docs/validation/s1-3-secret-intake/red-run.txt`
- `docs/validation/s1-3-secret-intake/rework-red-run.txt`

While those two are loose text files, a staged check from base to C exits **2**
(`git diff --cached --check`; 8 trailing-whitespace lines each). D keeps the raw
bytes but moves them out of the line-oriented tree: it **removes** the two loose
text paths and stores their exact bytes inside `raw-evidence.tar.gz`. As a
result the staged diff from base and from C both pass (`exit 0`).

The frozen patch bundles are shipped as **deterministic gzip wrappers**
(`patches/*.patch.gz`). This is a **packaging decision, not a lint bypass, and
it adds no whitespace exemption**:

- This candidate contains **no `.gitattributes`, no `.git/info/attributes`, and
  no `core.whitespace` override** anywhere in its tree or reproduction flow.
- Git's **default binary detection** already classifies gzip data as binary, so
  no line-based whitespace lint applies to the wrappers — without any rule
  change.
- Gunzipping reproduces the **exact original patch bytes**, and the original
  A/B/C sha256 (the *decompressed* SHA) is preserved unchanged. See the
  decompression verification below.

## Contents

| Path | Purpose |
|---|---|
| `README.md` | This document |
| `manifest.sha256` | sha256 of every file here **except itself** (it declares that) |
| `replay-report.md` | BASE → A → B → C → D replay and file-equality report |
| `test-matrix.md` | Test / lint matrix and the local-only boundary |
| `patches/A.patch.gz` `B.patch.gz` `C.patch.gz` | Deterministic gzip wrappers — decompress to the exact original patch bytes |
| `raw-evidence.tar.gz` | Deterministic archive of the raw evidence (see below) |
| `raw-members.sha256` | Per-member sha256 of the regular files inside the archive |

### Patch wrappers: `.gz` is packaging only

Each wrapper was produced with `gzip -n -9` (header `MTIME = 0`, no original
filename, XFL = max compression, OS = Unix); rebuilding from the same input is
byte-identical. The **original patch SHA is the SHA after decompression**.

Original patch digests — **SHA of the decompressed bytes** (unchanged, identical
to the delivery bundle and to `raw-evidence/patches/*` inside the archive):

```
f5438412196e60eb4127a714961868ad8b69620fa121fbb76432e909d334b0d3  A, gzip -dc patches/A.patch.gz
5fdd7fa6983de4a0cf7374ee9adbf6992acefbc86f6d1b4f5b258de2fe4689c7  B, gzip -dc patches/B.patch.gz
9527418e2c8ed31eecdacd9736c1c7149cb8f0fbdc8348b2f4a13294f7312699  C, gzip -dc patches/C.patch.gz
```

Independent digests of the **gzip wrapper files themselves** (as listed in
`manifest.sha256`):

```
bfee2b7abf1027212cf6b3c15aa70a59c97f472af4824081b64efacf68c5fc42  patches/A.patch.gz
00639832ed31ab1202b8e0468fc1980861f2003daf078b8b1031c5ff75548925  patches/B.patch.gz
a3c93a8f9252fa8ad8fee24636e04fdb655ca8230cc9a19c5388f4d77a9a7648  patches/C.patch.gz
```

## raw-evidence.tar.gz

Deterministic: `tar --sort=name --owner=0 --group=0 --numeric-owner
--mtime=@1791676800 --format=gnu` piped to `gzip -n -9`. Rebuilding from the same
inputs yields the identical archive. All member paths are relative, contain no
`..`, and are regular files or directories only (no symlinks, no absolute
paths). Members:

- `raw-evidence/red-logs/red-run.txt`, `rework-red-run.txt` — exact original bytes
- `raw-evidence/bundle/` — the original delivery bundle report, `manifest.json`,
  reviewer reports, and **all** raw run logs under `logs/`
  (baseline/A/B/C `pytest`, ruff, frozen requirement exports, pip freeze, etc.)
- `raw-evidence/reports/` — in-tree author / reviewer reports
- `raw-evidence/prior-freeze/` — prior-round replay tree proof
  (`replay-result.json`, `final-diff.txt`)
- `raw-evidence/patches/A.patch` `B.patch` `C.patch` — the exact original patch
  bytes (uncompressed); same sha256 as the decompressed wrappers above

No virtualenv, cache, or scratch tree is archived.

## Safe extraction / verification

Run from this directory. Always inspect members before extracting; never extract
an untrusted archive as root.

```sh
# 1. verify every loose file here against the manifest (manifest excludes itself)
sha256sum -c manifest.sha256

# 2. verify the gzip wrappers decompress to the frozen original patch SHA
gzip -dc patches/A.patch.gz | sha256sum   # = f5438412…d334b0d3 (original A)
gzip -dc patches/B.patch.gz | sha256sum   # = 5fdd7fa6…fe4689c7 (original B)
gzip -dc patches/C.patch.gz | sha256sum   # = 9527418e…4f7312699 (original C)

# 3. list members and reject unsafe paths (absolute, "..", or symlinks)
tar -tzf raw-evidence.tar.gz
tar -tvzf raw-evidence.tar.gz | grep -E '^l' && { echo "UNSAFE: symlink"; exit 1; }
tar -tzf raw-evidence.tar.gz | grep -E '(^/|(^|/)\.\.(/|$))' && { echo "UNSAFE: path"; exit 1; }

# 4. extract into a fresh directory, then verify per-member digests
tmp=$(mktemp -d)
tar -xzf raw-evidence.tar.gz -C "$tmp"
( cd "$tmp" && sha256sum -c /absolute/path/to/raw-members.sha256 )
```

`raw-members.sha256` lists each extracted regular file as
`<sha256>  raw-evidence/<path>` and is itself covered by `manifest.sha256`.

## Boundaries — local only

The 832 passed / 21 skipped figure is **local, single-machine** evidence:

- 21 tests are **skipped, not passed** — they need
  `RAGOS_TEST_PG_DSN` pointing at a disposable local PostgreSQL database.
- **PostgreSQL / RLS is not verified** here.
- **No real provider (Qwen / OpenAI) call** and no network model dispatch.
- **Qdrant Server is not verified**.
- No browser / UI journey, no migration execution.
- **NOT RUN is never reported as PASS.**

## Historical fact preserved (not relabelled)

The original cached whitespace check on the unfixed candidate **exited 2**. That
remains recorded as exit 2 and is **not** rewritten to PASS. D removes the two
loose text offenders (which are archived here) so that the staged check from
base and from C both exit 0. This round independently reproduced the exit-2
result at C and the exit-0 result after D.

## Credentials

No real credential, key, token, `.env` file or object-store secret is stored in
this package. The only key-shaped strings are clearly fake test sentinels, e.g.
`sk-S13-SENTINEL-7f3a9c2e-do-not-persist`, `sk-AAAA…` and
`S13AlnumSecretSentinel9x`, which exist to prove that raw credential intake is
refused and never echoed.
