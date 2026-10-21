"""Review and optionally delete obsolete GitHub branches after merged PRs.

Defaults to DRY RUN. Requires exact --confirm-repo and --apply for deletion.
Uses the authenticated gh CLI; no Git credentials are logged.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from urllib.parse import quote

ALLOWED_PREFIXES = ("feat/", "fix/", "test/")


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=30, check=True
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError("GitHub branch inspection/action failed") from None
    return result.stdout.strip()


def _count_prs(repo: str, branch: str, state: str, *, as_base: bool = False) -> int:
    which = "--base" if as_base else "--head"
    value = _gh(
        "pr", "list", "--repo", repo, which, branch,
        "--state", state, "--json", "number", "--limit", "1",
        "--jq", "length",
    )
    return int(value)


def merged_branch_candidates(repo: str, *, default_branch: str) -> list[tuple[str, str]]:
    names = _gh(
        "api", "--paginate", f"repos/{repo}/branches?per_page=100",
        "--jq", ".[].name",
    ).splitlines()
    candidates = []
    for branch in sorted(set(names)):
        if branch == default_branch or not branch.startswith(ALLOWED_PREFIXES):
            continue
        if _count_prs(repo, branch, "open") or _count_prs(
            repo, branch, "open", as_base=True
        ):
            continue
        if not _count_prs(repo, branch, "merged"):
            continue
        info = json.loads(
            _gh("api", f"repos/{repo}/branches/{quote(branch, safe='')}")
        )
        if info.get("protected"):
            continue
        compare = json.loads(
            _gh("api", f"repos/{repo}/compare/{default_branch}...{branch}")
        )
        if compare.get("ahead_by") != 0:
            continue
        candidates.append((branch, info["commit"]["sha"]))
    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run merged branch cleanup")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-repo", default="")
    args = parser.parse_args(argv)
    if args.apply and args.confirm_repo != args.repo:
        parser.error("--apply requires exact --confirm-repo owner/repo")
    if args.repo.count("/") != 1 or any(not s for s in args.repo.split("/")):
        parser.error("expected GitHub owner/repo")
    try:
        default = _gh("api", f"repos/{args.repo}", "--jq", ".default_branch")
        candidates = merged_branch_candidates(args.repo, default_branch=default)
        if not candidates:
            print("No merged branches are eligible for cleanup.")
        for branch, original_sha in candidates:
            if not args.apply:
                print(f"DRY RUN: would delete {branch} ({original_sha[:12]})")
                continue
            current = json.loads(
                _gh("api", f"repos/{args.repo}/branches/{quote(branch, safe='')}")
            )
            if current["commit"]["sha"] != original_sha or current.get("protected"):
                raise RuntimeError("Branch changed after cleanup preflight; abort")
            if _count_prs(args.repo, branch, "open") or _count_prs(
                args.repo, branch, "open", as_base=True
            ):
                raise RuntimeError("Branch now participates in an open PR; abort")
            compare = json.loads(
                _gh("api", f"repos/{args.repo}/compare/{default}...{branch}")
            )
            if compare.get("ahead_by") != 0:
                raise RuntimeError("Branch has new unmerged commits; abort")
            _gh("api", "--method", "DELETE", f"repos/{args.repo}/git/refs/heads/{branch}")
            print(f"Deleted merged branch: {branch}")
    except (RuntimeError, ValueError, KeyError):
        print("Cleanup failed closed; check GitHub access and repo state.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
