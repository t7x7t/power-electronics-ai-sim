# ngspice Environment and Version Lock (v0)

**Status:** environment input for the real-backend slice. It records how the
external simulator is discovered and identified. It does not create a
cross-platform or physical-reproducibility claim.

## Two version statements

As in `docs/28_release_toolchain_policy_v0.md`, the project separates:

- **minimum compatible version** — the oldest executable the adapter may try;
- **verified version** — the exact executable used for recorded acceptance.

Current declaration:

| Statement | Value |
| --- | --- |
| Minimum compatible major | `40` |
| Verified version | `41` (conda-forge `ngspice` package, console `ngspice_con.exe`) |
| Verified executable SHA-256 | `41a6bda700d9160089f2a39a14f94408172f7748215b68b81d39ea1bc26e300c` |
| Verified executable size | 6,766,080 bytes |
| Verified platform | Windows x86_64 |
| Verified date | 2026-10-21 |

The Windows package also ships `ngspice.exe`, a GUI build that can open a
window and ignore batch output. Discovery prefers the sibling
`ngspice_con.exe` and records the substitution. Passing the GUI executable
directly is accepted only with an explicit limitation.

Until a verified version is recorded, the real-backend surface remains
`Partial` in `docs/00_project_backlog_v0.md`.

## Discovery order

The adapter resolves the executable in this order:

1. explicit `--ngspice <path>` on `pe-sim run`;
2. `PE_SIM_NGSPICE` environment variable;
3. user-level `backends.toml` outside the checkout (see `docs/33`);
4. `ngspice` on `PATH`.

If none resolves, the CLI rejects the configuration before creating a run
directory. It does not fall back to the Fake backend.

Use `pe-sim backend doctor --name ngspice` to print the resolved path, source,
version, SHA-256, solver settings, and limitations. Use `pe-sim backend list`
to see registered backends, `pe-sim backend guide --name ngspice` for
installation recipes, and `pe-sim backend setup --name ngspice --write-config`
to persist an existing installation outside the checkout. Use
`pe-sim backend scan --root <directory>` to locate an existing installation
without writing any file. The full onboarding flow is in `docs/35`.

## Windows installation (recommended)

1. Obtain the official ngspice Windows archive from the ngspice project.
2. Extract it to a non-system drive, for example
   `D:\tools\ngspice-44.2\`.
3. Confirm the executable exists at
   `D:\tools\ngspice-44.2\Spice64\bin\ngspice.exe`.
4. Record the path and version before the first acceptance run:

```powershell
$env:PE_SIM_NGSPICE = "D:\tools\ngspice-44.2\Spice64\bin\ngspice.exe"
& $env:PE_SIM_NGSPICE --version
Get-FileHash -Algorithm SHA256 $env:PE_SIM_NGSPICE
```

The extracted directory must not be committed to this repository. ngspice is
GPL-licensed and is not relicensed by the project's MIT license.

## Recorded evidence

`NgspiceExecutableAdapter` records:

- `name = ngspice`;
- parsed `version` (for example `44.2`);
- `executable_name` (basename only);
- `executable_sha256` and `executable_size_bytes`;
- `solver_settings` (from `NgspiceSolverSettings`);
- `returncode` and `version_output_sha256` of the version query;
- `limitations` for unparsed versions, non-zero exits, missing hashes, or a
  major version below the minimum.

A local absolute path is intentionally not stored in portable evidence.

## CI policy

Ordinary push/PR CI does not install or run ngspice. Real-backend acceptance is
opt-in through `PE_SIM_NGSPICE` and is expected to run locally before a release
claim that mentions ngspice. The release check may record a supplied executable
with `-NgspiceExecutable`; an absent executable is recorded as `not_run`, never
as a pass.

## Upgrade rule

Changing the verified ngspice version, its SHA-256, the solver settings, or the
netlist template requires:

1. a clean local re-run of `tests/test_ngspice_real_backend.py`;
2. refreshed provenance evidence in the release record;
3. a review of the netlist template digest change and its effect on
   `plant.identity`;
4. no silent expansion of the physical, thermal, or hardware claim.
