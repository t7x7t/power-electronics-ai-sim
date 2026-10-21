# ADR 0002: ngspice Process Boundary

## Status

Accepted for the first real-backend slice.

## Context

The project needs a switching-level reference circuit without weakening its MIT
license boundary or its evidence rules.  Two integration paths were considered:

1. link the circuit to ngspice through the PySpice Python library;
2. invoke a separately installed ngspice executable in batch mode.

The checked facts are:

- PySpice is licensed under GPLv3, its last production release is 1.5.0
  (2021-05), and its documentation states support for Ngspice up to version 34;
- ngspice itself is GPL-licensed;
- this repository is MIT-licensed and explicitly excludes unreviewed
  third-party material from its license scope.

Linking GPLv3 code into the project runtime would create an avoidable license
coupling for every downstream user, and the library is not maintained against
current ngspice releases.  The executable path is also easier to supervise:
version, digest, timeout, exit code, and log are all observable at the process
boundary.

## Decision

1. The reviewed backend invokes a separately installed `ngspice` executable in
   batch mode.  The project does not bundle, link, or relicense ngspice or
   PySpice.
2. `PySpice` is not a runtime, build, or test dependency of the base package.
   A future optional adapter may use it only behind a separate extra and a
   recorded license review; it is not part of this slice.
3. The adapter solves one zero-order-hold control window per ngspice
   invocation.  Segment endpoints inside the window are served by
   interpolation, which preserves the Runner's timing contract without
   starting thousands of solver processes.
4. The executable name, SHA-256, parsed version, and explicit solver settings
   are recorded in `environment.json`/`manifest.json`.  A missing or
   unparseable version is `partial` or `unavailable`, never a fabricated
   `known`.
5. Every window solve records its generated netlist digest, output digest, log
   digest, exit code, and wall-clock duration.  A failed window is a
   `RUN_FAILED` run, not a partial success.

## Consequences

- Users must install ngspice separately and provide its path through
  `--ngspice`, `PE_SIM_NGSPICE`, or `PATH`.  `docs/32` defines the supported
  installation and version policy.
- Ordinary CI does not run real ngspice.  Real-backend acceptance is opt-in via
  `PE_SIM_NGSPICE`; the release gate may record it when an executable is
  supplied.
- The backend's numerical behavior remains subject to its own acceptance
  matrix.  This ADR does not claim physical, thermal, hardware, or product
  validity.
