# Fraeno runner contract

The Fraeno runner lets a robotics repository use the validation engine without
copying Fraeno source or its internal fixtures.

## Files in the customer repository

Copy these release-matched templates:

```text
templates/github/fraeno-validation.yml
  to .github/workflows/fraeno-validation.yml

templates/github/run-isolated-validation.sh
  to .github/fraeno/run-isolated-validation.sh
```

Add `.fraeno.yml` at the repository root. Set the repository variable
`FRAENO_RUNNER_IMAGE` to the published runner reference with its immutable
digest:

```text
REGISTRY/IMAGE@sha256:FULL_DIGEST
```

The workflow rejects mutable tags.

Fraeno's own runner publisher also uses immutable Artifact Registry tags and
records the final digest, SBOM, provenance, and previous production digest.
See `docs/releases.md` for the release gate and rollback evidence.

## What the runner protects

The host starts three containers.

1. The baseline container gets only the baseline source, trusted source, config,
   and its own protected evidence volume.
2. The candidate container gets only the candidate source, read-only trusted
   source, config, and a different protected evidence volume.
3. The comparison container gets both evidence files as read-only inputs and
   the final report destination.

Source mounts are read-only. Each sandbox copies its source to temporary
storage, then runs project commands as UID and GID 65532. The installed Fraeno
engine, trusted observer, evidence directory, and final report stay outside
that user’s writable paths. The containers drop every capability except the
small set the root supervisor needs to prepare and enter the unprivileged
sandbox.

The candidate cannot write baseline evidence because it is never mounted. It
cannot write the final report because that destination exists only in the
comparison container.

## Trusted observation

Fraeno loads `.fraeno.yml` from the base commit for both runs. The runner exposes
the base source at `FRAENO_TRUSTED_ROOT`. The generic observer can use that
trusted contract after the workspace environment is loaded:

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
      rate_topics: [/sensor/reading]
      diagnostics_topics: [/diagnostics]
      transform_topics: [/tf, /tf_static]
      shutdown_timeout_seconds: 5
```

This prevents a candidate from weakening the observer settings in its own
commit. A custom observer remains supported for hardware-specific evidence.

## Local proof

Build the same versioned image used by CI, then run the independent ROS 2
fixture and hostile candidate:

```bash
docker build --file runner/Dockerfile --tag fraeno-runner:test .
bash tests/container/test-external-runner.sh fraeno-runner:test
```

The first candidate passes. The second candidate builds but breaks the robot
contract and tries to overwrite every protected area. Every write attempt is
denied and the final report blocks the change.
