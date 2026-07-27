# Fraeno

Fraeno automatically manages and updates robot software dependencies, and tests the complete robotic system before changes are deployed.

<small><em>essentially, dependabot for robots + integration testing.</em></small>

Fraeno treats a dependency update as a robot-system transaction:

1. Discover dependencies across ROS, Python, CMake, Docker, APT, vcstool, and git submodules.
2. Change one supported dependency declaration at a time.
3. Build clean baseline and candidate environments.
4. Launch or probe the configured system.
5. Compare nodes, topics, services, actions, QoS, rates, transforms, and diagnostics.
6. Block the pull request when behavior regresses or evidence is incomplete.

## Current target

The first supported production target is ROS 2 Humble on Ubuntu 22.04, `amd64`, using a Docker-based GitHub Actions runner. Fraeno can scan more environments, but it does not claim that a passing simulation proves physical safety.

Fraeno reports the achieved validation level:

- `L1`: build and static checks
- `L2`: live graph and behavioral integration
- `L3`: bag replay or simulation
- `L4`: hardware-in-the-loop

The first release targets `L2`.

## Quick start

Install the local engine:

```bash
python3 -m pip install .
```

Scan and lock a repository:

```bash
fraeno scan . --output dependency-graph.json
fraeno lock . --output fraeno.lock.json
```

The lock records every manifest and declaration, the resolved or declared
artifacts, shared logical components, dependency edges, provenance, and the
target ROS distribution, operating system, architecture, and container stage.
Unknown resolutions remain explicit.

If the target cannot be inferred from a Docker base image, provide it:

```bash
fraeno lock . \
  --ros-distro humble \
  --os ubuntu \
  --os-version 22.04 \
  --architecture amd64
```

Explain what changed between two locks:

```bash
fraeno compare-locks \
  --baseline baseline.lock.json \
  --candidate candidate.lock.json \
  --output dependency-change.json
```

Apply one supported update:

```bash
fraeno update . --dependency python:requests --to 2.32.5
```

Run baseline and candidate validation:

```bash
fraeno validate \
  --baseline /path/to/base \
  --candidate /path/to/candidate \
  --config /path/to/base/.fraeno.yml
```

The contract must come from the trusted base commit. A candidate cannot weaken its own required checks.

## External repository runner

An external repository needs only a project contract and the thin files under
`templates/github/`. It does not copy Fraeno source or internal fixtures. The
workflow requires a runner image pinned by digest, runs baseline and candidate
code in separate containers, then creates the final report in a third trusted
container.

Every report identifies the exact Fraeno engine version. See
`docs/runner.md` for the runner contract and local proof.

## Safe and breaking proof

The committed ROS 2 Humble fixture runs a live sensor publisher and reliable
controller subscriber in separate processes. Fraeno observes the ROS graph,
endpoint QoS, message rates, services, and diagnostics through `rclpy`.

- `1.0.0` to `1.0.1` passes.
- `1.0.0` to `2.0.0` builds, but changes the publisher to best effort. The reliable controller stops receiving data, `/robot/command` falls silent, diagnostics worsen, and Fraeno blocks the update.

The same proof runs in GitHub Actions on every Fraeno pull request. This is the
first behavior ordinary dependency bots do not test.

## GitHub App

The app receives signed GitHub webhooks, durably queues each delivery, creates
`Fraeno / robot integration` checks, dispatches the trusted validation workflow,
and completes each check from the correlated workflow result.

Required repository permissions:

- Actions: read and write
- Checks: read and write
- Contents: read
- Pull requests: read

Automated update pull requests use the repository’s trusted scheduled workflow with narrowly scoped `contents: write` and `pull-requests: write` permissions. The GitHub App private key and webhook secret never enter customer test jobs.

## Update discovery

`fraeno outdated .` checks exact Python pins, Docker image tags, pinned APT
packages, and vcstool refs. The rosdep provider accepts target-specific current
and newer package evidence from a catalog. Until that evidence exists, it
returns an explicit refusal instead of pretending an unversioned `package.xml`
key is an installed package version.

Each proposed update includes the current value, target value, registry source,
release date, and the evidence used to choose it. Fraeno refuses unclear
choices instead of guessing between branches, package sources, or conflicting
pins.

For an offline or repeatable run, pass a catalog fixture:

```console
fraeno outdated fixtures/update_discovery/robot_repo \
  --catalog fixtures/update_discovery/catalog.json \
  --ros-distro humble \
  --os ubuntu \
  --os-version 22.04 \
  --architecture amd64
```

`fraeno update-next` uses the same discovery report and rewrites one managed
declaration. The scheduled update workflow then opens a pull request and lets
the complete robot integration check decide whether it can merge.

## Honest limits

- rosdep and CMake dependencies are discovered but not automatically rewritten in v1.
- Unpinned APT declarations are observed but not rewritten.
- Git submodule mutation requires the git index and is not yet automated.
- Vendor drivers, kernels, cameras, CAN devices, and GPUs require labeled hardware runners.
- A passing result means the configured target and declared probes passed. It does not mean every possible robot behavior is safe.
