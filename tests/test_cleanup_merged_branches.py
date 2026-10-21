"""Guardrails for the manual-only stale branch cleanup command."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_merged_branches.py"
_spec = importlib.util.spec_from_file_location("cleanup_merged_branches", _SCRIPT)
assert _spec is not None and _spec.loader is not None
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)


def test_merged_candidates_exclude_active_unmerged_and_protected(monkeypatch) -> None:
    names = [
        "main", "feat/merged", "feat/active", "fix/unmerged",
        "test/protected", "personal/no-pr",
    ]
    def fake_gh(*args):
        if args[:2] == ("api", "--paginate"):
            return "\n".join(names)
        if args[:2] == ("pr", "list"):
            branch = args[args.index("--head") + 1] if "--head" in args else ""
            state = args[args.index("--state") + 1]
            if state == "open" and branch == "feat/active":
                return "1"
            if state == "merged" and branch in {"feat/merged", "test/protected"}:
                return "1"
            return "0"
        if args[0] == "api" and "/branches/" in args[1]:
            return json.dumps({
                "protected": "protected" in args[1],
                "commit": {"sha": "a" * 40},
            })
        if args[0] == "api" and "/compare/" in args[1]:
            return json.dumps({"ahead_by": 0})
        raise AssertionError(args)

    monkeypatch.setattr(cleanup, "_gh", fake_gh)
    assert cleanup.merged_branch_candidates(
        "musuntana/GRPO-Rag", default_branch="main"
    ) == [("feat/merged", "a" * 40)]


def test_default_cli_is_dry_run_and_never_deletes(monkeypatch, capsys) -> None:
    calls = []
    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("api", "repos/musuntana/GRPO-Rag"):
            return "main"
        raise AssertionError("No mutation or remote branch call should be needed")

    monkeypatch.setattr(cleanup, "_gh", fake_gh)
    monkeypatch.setattr(
        cleanup, "merged_branch_candidates",
        lambda repo, *, default_branch: [("feat/merged", "b" * 40)],
    )
    assert cleanup.main(["--repo", "musuntana/GRPO-Rag"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert not any("DELETE" in call for call in calls)


def test_delete_requires_exact_repo_confirmation() -> None:
    with pytest.raises(SystemExit):
        cleanup.main([
            "--repo", "musuntana/GRPO-Rag", "--apply",
            "--confirm-repo", "other/repository",
        ])
