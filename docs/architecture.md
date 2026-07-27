# Fraeno architecture

## Product boundary

Fraeno is a dependency-update transaction engine for robotics repositories. It is not a general ROS test dashboard and it does not infer physical safety from compilation.

## Data plane

Customer code executes in the repository’s isolated GitHub Actions runner. The hosted Fraeno service never clones or executes customer source code.

The trusted workflow:

1. checks out the base commit and candidate commit separately;
2. installs the Fraeno engine from a trusted release or base commit;
3. loads the contract from the base commit;
4. builds and observes both workspaces;
5. exits successfully only when the candidate satisfies the contract and baseline invariants;
6. uploads the complete JSON evidence report.

## Control plane

The GitHub App:

1. validates the raw webhook body with HMAC-SHA256;
2. deduplicates `X-GitHub-Delivery`;
3. exchanges a short-lived app JWT for a repository-scoped installation token;
4. creates a queued Fraeno check;
5. dispatches the trusted default-branch workflow;
6. stores only repository IDs, commit SHAs, PR/check/run IDs, and timestamps;
7. completes the check after the correlated workflow finishes.

The service does not persist installation tokens or customer source.

## Dependency model

Fraeno retains duplicate entry paths instead of deduplicating only by package name. OpenCV, CUDA, protobuf, and similar libraries can enter a robot through APT, Python, source workspaces, Docker images, or CMake simultaneously.

Managed in v1:

- exact Python pins in `requirements*.txt` and quoted `pyproject.toml` dependencies;
- literal Docker `FROM` tags and digests;
- pinned APT packages in Dockerfiles;
- vcstool `.repos` refs.

Observed but unmanaged in v1:

- ROS logical keys and version conditions;
- dynamic CMake;
- unpinned APT packages;
- git submodule gitlinks;
- host-coupled drivers and hardware.

Unsupported declarations remain visible in reports. Fraeno never silently treats omitted evidence as safe.

## Result semantics

- `pass`: candidate behavior satisfies the trusted contract and permitted baseline deltas.
- `block`: a confirmed candidate regression or failed candidate execution.
- `error`: invalid baseline, unstable graph, malformed observation, or incomplete infrastructure evidence.

Both `block` and `error` produce a failing required check.
