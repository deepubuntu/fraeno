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

## Safe and breaking proof

The committed deterministic fixture models a sensor publisher and a reliable controller subscriber.

- `1.0.0` to `1.0.1` passes.
- `1.0.0` to `2.0.0` builds, but changes the publisher to best effort. The reliable controller stops receiving data, `/robot/command` falls silent, diagnostics worsen, and Fraeno blocks the update.

This is the first behavior ordinary dependency bots do not test.

## GitHub App

The app receives signed GitHub webhooks, creates `Fraeno / robot integration` checks, dispatches the trusted validation workflow, and completes each check from the correlated workflow result.

Required repository permissions:

- Actions: read and write
- Checks: read and write
- Contents: read
- Pull requests: read

Automated update pull requests use the repository’s trusted scheduled workflow with narrowly scoped `contents: write` and `pull-requests: write` permissions. The GitHub App private key and webhook secret never enter customer test jobs.

## Honest limits

- `package.xml` and CMake dependencies are discovered but not automatically rewritten in v1.
- Unpinned APT declarations are observed but not rewritten.
- Git submodule mutation requires the git index and is not yet automated.
- Vendor drivers, kernels, cameras, CAN devices, and GPUs require labeled hardware runners.
- A passing result means the configured target and declared probes passed. It does not mean every possible robot behavior is safe.
