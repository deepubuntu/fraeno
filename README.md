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

## Try Fraeno

No robot repository yet? Use the
[public demo robot](https://github.com/deepubuntu/fraeno-demo-robot) to watch a
safe update pass and a dangerous update get blocked. The
[ten-minute demo guide](docs/try-demo.md) requires no robot hardware.

To connect your own ROS 2 repository, request private-beta access at
[fraeno.com](https://fraeno.com/#access), then follow the
[complete onboarding guide](docs/onboarding.md).

The source and demo are public. Access to the hosted GitHub App remains private
while early teams are onboarded directly.

## Quick start for your repository

Install the current release:

```bash
python3 -m pip install \
  "git+https://github.com/deepubuntu/fraeno.git@v0.2.3"
```

Add Fraeno to a ROS 2 repository:

```bash
fraeno init . \
  --launch-command "ros2 launch my_robot_bringup system.launch.py" \
  --runner-image "us-central1-docker.pkg.dev/deepubuntu-32f9e/fraeno-runner/runner@sha256:8f932a56209a0a8ecfbda3fff9958fd9b710d1d0065a9312486e9acd674fdcfc" \
  --open-pr
```

After the onboarding pull request is merged, `fraeno doctor` names any missing
local command, GitHub file, runner setting, App permission, or App event. See
[the onboarding guide](docs/onboarding.md) for the complete first test.

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

## Generic ROS 2 observation

`fraeno observe-ros2` launches the system command in `.fraeno.yml`, waits for
the ROS graph to settle, measures configured topics more than once, and stops
the complete process group. It discovers nodes, topics, endpoint QoS, services,
actions, transforms, diagnostics, and process health without a custom observer
script.

The observer timing and evidence sources are explicit:

```yaml
validation:
  observe:
    command:
      - bash
      - -lc
      - >-
        source install/setup.bash &&
        fraeno observe-ros2 --config "$FRAENO_TRUSTED_ROOT/.fraeno.yml"
    ros2:
      launch_command: [ros2, launch, my_robot_bringup, system.launch.py]
      warmup_seconds: 2
      graph_stabilization_timeout_seconds: 15
      graph_stabilization_interval_seconds: 0.25
      graph_stabilization_samples: 3
      sample_seconds: 5
      measurement_repetitions: 3
      rate_topics: [/sensor/reading, /robot/command]
      diagnostics_topics: [/diagnostics]
      transform_topics: [/tf, /tf_static]
      shutdown_timeout_seconds: 5
```

Use `kind:name` entries such as `topic:/temporary/debug` in
`allowed_missing_baseline_entities` when a known baseline entity may disappear.
Allowed removals never override a required contract entity.

Fraeno treats missing or unreadable observer evidence as an infrastructure
error. Confirmed graph, health, QoS, rate, diagnostic, or process regressions
block the candidate.

## External repository runner

An external repository needs only a project contract and the thin trusted files
written by `fraeno init`. It does not copy Fraeno source or internal fixtures.
The workflow requires a runner image pinned by digest, runs baseline and
candidate code in separate containers, then creates the final report in a third
trusted container.

Every report identifies the exact Fraeno engine version. See
`docs/runner.md` for the runner contract and local proof.

Runner releases are built from reviewed commits, published directly to Google
Artifact Registry, and consumed only by digest. The manual fail-closed release
workflow and remaining production prerequisites are in `docs/releases.md`.

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

For production orchestration, `fraeno propose-update` applies the repository's
allow, ignore, update type, cooldown, grouping, schedule, and open pull request
limits. One dependency per pull request is the default. Deterministic branches
prevent duplicate and superseded proposals, and each pull request explains the
changed manifests, old and new resolutions, validation scope, and missing
evidence. See [the update policy guide](docs/update-policy.md).

## Honest limits

- rosdep and CMake dependencies are discovered but not automatically rewritten in v1.
- Unpinned APT declarations are observed but not rewritten.
- Git submodule mutation requires the git index and is not yet automated.
- Vendor drivers, kernels, cameras, CAN devices, and GPUs require labeled hardware runners.
- A passing result means the configured target and declared probes passed. It does not mean every possible robot behavior is safe.
