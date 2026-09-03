"""Read-only source provenance helpers.

The runner must never invent a source revision.  When Git is unavailable or
the requested path is not inside a repository, the result is explicitly
marked ``unknown`` so callers can apply their own publication policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class GitProvenance:
    """The minimum source identity needed to bind a run to its code."""

    source_commit: str = "unavailable"
    branch: str = "unavailable"
    working_tree_status: str = "unknown"
    status: str = "unknown"
    limitations: tuple[str, ...] = ("Git repository or source revision unavailable",)

    def to_manifest_fields(self) -> dict[str, object]:
        """Return fields suitable for the top-level manifest."""

        return {
            "source_commit": self.source_commit,
            "branch": self.branch,
            "working_tree_status": self.working_tree_status,
            "provenance": {
                "status": self.status,
                "producer": "pe_sim",
                "limitations": list(self.limitations),
            },
        }


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def collect_git_provenance(path: str | Path | None = None) -> GitProvenance:
    """Collect commit, branch and clean/dirty status without mutating Git.

    ``path`` defaults to the process working directory.  A clean repository is
    ``known``; a dirty repository is ``exploratory``; failures are ``unknown``.
    The distinction is intentionally conservative and is recorded in the
    manifest so a downstream tool can reject non-clean runs.
    """

    root = Path(path or Path.cwd()).resolve()
    try:
        commit = _git(root, "rev-parse", "HEAD")
        if not commit:
            raise RuntimeError("empty git revision")
        branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD") or "detached"
        porcelain = _git(root, "status", "--porcelain", "--untracked-files=all")
    except (FileNotFoundError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return GitProvenance(limitations=(f"Git provenance unavailable: {exc}",))

    dirty = bool(porcelain)
    status = "exploratory" if dirty else "known"
    limitations: tuple[str, ...] = (
        ("working tree contains uncommitted or untracked changes",) if dirty else ()
    )
    return GitProvenance(
        source_commit=commit,
        branch=branch,
        working_tree_status="dirty" if dirty else "clean",
        status=status,
        limitations=limitations,
    )

