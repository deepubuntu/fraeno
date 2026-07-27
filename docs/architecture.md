# Fraeno architecture

## Product boundary

Fraeno is a dependency-update transaction engine for robotics repositories. It is not a general ROS test dashboard and it does not infer physical safety from compilation.

## Data plane

Customer code executes in the repository’s isolated GitHub Actions runner. The hosted Fraeno service never clones or executes customer source code.

The trusted workflow:

1. checks out the base commit and candidate commit separately;
2. pulls a versioned Fraeno runner pinned by its image digest;
3. loads the contract and observer from the base commit;
4. copies the baseline into a disposable baseline sandbox;
5. copies the candidate into a different disposable candidate sandbox;
6. runs customer commands as an unprivileged user in each sandbox;
7. writes each observation after customer processes have stopped;
8. compares read-only evidence in a third trusted container;
9. uploads the complete JSON report.

The candidate container does not receive the baseline evidence or final report
mount. The installed validator and trusted observer are root-owned. The
candidate workspace runs as UID and GID 65532 with no new privileges. The final
report records the exact Fraeno engine version.

## Control plane

The GitHub App:

1. validates the raw webhook body with HMAC-SHA256;
2. creates a deterministic Cloud Task from `X-GitHub-Delivery`;
3. returns `202` before performing GitHub API work;
4. invokes a separate private worker with a service-account OIDC token;
5. deduplicates and tracks delivery state in Firestore;
6. exchanges a short-lived app JWT for a repository-scoped installation token;
7. creates a queued Fraeno check;
8. dispatches the trusted default-branch workflow;
9. stores only repository IDs, commit SHAs, PR/check/run IDs, and timestamps;
10. completes the check after the correlated workflow finishes.

The service does not persist installation tokens or customer source.

The public webhook service has the webhook secret and permission to enqueue
tasks, but no GitHub private key. The private worker has the GitHub private key
and Firestore access, but does not accept public traffic.

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
