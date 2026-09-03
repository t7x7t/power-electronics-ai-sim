"""Read-only Git worktree analysis and formal-comparison policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


_CATEGORIES = ("source", "config", "tests", "docs", "generated", "other")


@dataclass(frozen=True)
class DirtyAnalysis:
    """Machine-readable explanation of a repository's current worktree."""

    status: str
    source_commit: str = "unavailable"
    branch: str = "unavailable"
    repo_root: str | None = None
    paths: tuple[str, ...] = ()
    paths_by_category: dict[str, tuple[str, ...]] | None = None
    reasons: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "source_commit": self.source_commit,
            "branch": self.branch,
            "repo_root": self.repo_root,
            "paths": list(self.paths),
            "paths_by_category": {k: list(v) for k, v in (self.paths_by_category or {}).items()},
            "reasons": list(self.reasons),
            "suggestions": list(self.suggestions),
            "formal_comparison_allowed": self.status == "clean",
        }


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True, timeout=5
    ).stdout.rstrip("\r\n")


def _category(path: str) -> str:
    p = path.replace("\\", "/").lstrip("./")
    parts = p.split("/")
    name = parts[-1].lower()
    if any(part.startswith((".pytest_", ".ci-runs")) or part in {"runs", ".tmp", "tmp", "__pycache__", "build", "dist"} for part in parts):
        return "generated"
    if parts[0] in {"tests", "test"} or name.startswith("test_"):
        return "tests"
    if parts[0] in {"docs", "doc"} or name.lower().endswith((".md", ".rst", ".txt")):
        return "docs"
    if parts[0] in {"schemas", "config", "configs", "examples"} or name.endswith((".json", ".yaml", ".yml", ".toml")):
        return "config" if parts[0] in {"schemas", "config", "configs"} or name.endswith((".json", ".yaml", ".yml")) else "source"
    if parts[0] in {"src", "scripts", "examples"} or name.endswith((".py", ".ps1", ".sh")):
        return "source"
    return "other"


def analyze_git_worktree(path: str | Path | None = None) -> DirtyAnalysis:
    """Inspect Git without changing files, commits, index, or worktree."""

    root = Path(path or Path.cwd()).resolve()
    try:
        repo_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
        commit = _git(repo_root, "rev-parse", "HEAD")
        branch = _git(repo_root, "symbolic-ref", "--short", "-q", "HEAD") or "detached"
        porcelain = _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return DirtyAnalysis(
            status="unknown",
            reasons=(f"Git worktree could not be inspected: {exc}",),
            suggestions=("Run this command inside a Git repository and ensure Git is installed.", "For formal comparison, create and record a clean commit first."),
        )

    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line:
            continue
        value = line[3:] if len(line) >= 4 else line
        # Rename records have an old and new path; both are relevant evidence.
        paths.extend(part.strip('"') for part in value.split(" -> "))
    grouped = {category: tuple(sorted(p for p in paths if _category(p) == category)) for category in _CATEGORIES}
    if not paths:
        return DirtyAnalysis("clean", commit, branch, str(repo_root), (), grouped, (), ("Keep the commit unchanged for a formal comparison.",))
    reasons = ["working tree contains uncommitted or untracked paths"]
    suggestions = ["Review the categorized paths in this report."]
    if grouped["source"] or grouped["config"]:
        suggestions.append("Commit intended source/config changes, or move exploratory edits to a separate branch.")
    if grouped["generated"]:
        suggestions.append("Delete or ignore generated outputs; do not include them in a formal baseline.")
    if grouped["tests"] or grouped["docs"]:
        suggestions.append("Commit intentional test/documentation changes before formal comparison.")
    suggestions.append("Re-run the dirty analysis and require status=clean before formal comparison.")
    return DirtyAnalysis("dirty", commit, branch, str(repo_root), tuple(sorted(paths)), grouped, tuple(reasons), tuple(suggestions))


def require_formal_comparison(analysis: DirtyAnalysis) -> None:
    """Fail closed unless the source worktree is known and clean."""

    if analysis.status != "clean":
        raise FormalComparisonError(analysis)


class FormalComparisonError(RuntimeError):
    def __init__(self, analysis: DirtyAnalysis):
        self.analysis = analysis
        super().__init__(f"formal comparison requires a clean Git worktree (status={analysis.status})")
