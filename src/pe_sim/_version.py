"""Single in-code source of the package version.

``pyproject.toml`` keeps a static ``version`` because a build-isolated
metadata read of ``pe_sim`` would import the package and its dependencies.
``scripts/release_record.py check-identity`` fails when the two disagree.
"""

from __future__ import annotations

__version__ = "0.10.1"
