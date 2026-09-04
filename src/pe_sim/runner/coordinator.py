"""Low-friction public entry point for running an experiment."""

from __future__ import annotations

from typing import Any

from .policies import RunOptions


def run_experiment(spec: Any, plant: Any, controller: Any, options: RunOptions | None = None):
    """Run a Plant/Controller pair with standard defaults.

    Importing ``Runner`` lazily keeps the compatibility implementation in
    ``pe_sim.runtime`` independent from this facade and avoids a package
    initialization cycle.  Advanced callers can supply ``RunOptions``;
    ordinary callers need no policy configuration.
    """

    from ..runtime import Runner

    return Runner().run(spec, plant, controller, options=options)
