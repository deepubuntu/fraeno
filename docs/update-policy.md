# Update policy and pull request orchestration

Fraeno reads update policy from the trusted `.fraeno.yml` file. Discovery and
policy evaluation finish before a dependency file is changed.

```yaml
updates:
  allow:
    - dependency: "python:*"
      update_types: [minor, patch]
      cooldown_days: 7
    - dependency: "docker:*"
      update_types: [digest]
  ignore:
    - dependency: "python:legacy-driver"
  update_types: [major, minor, patch, digest, revision, unknown]
  cooldown_days: 3
  groups:
    - name: ROS runtime
      patterns:
        - "docker:ros"
        - "apt:ros-*"
  schedule:
    interval: weekly
    day: monday
  max_open_pull_requests: 5
```

## Selection rules

Dependency patterns are lowercase shell-style patterns matched against an
identity such as `python:requests` or `docker:ros`.

- An empty `allow` list allows every dependency that Fraeno can rewrite.
- A non-empty `allow` list requires at least one matching rule.
- A matching `ignore` rule wins over an allow rule.
- A rule can restrict its dependency to selected update types.
- A rule-specific cooldown adds to the global safety boundary. Fraeno uses the
  longest matching cooldown.
- If a cooldown is active and the registry release date is missing, Fraeno
  refuses the proposal.

Fraeno classifies comparable releases as `major`, `minor`, or `patch`.
Immutable Docker digests use `digest`, vcstool commit changes use `revision`,
and package formats that cannot be compared safely use `unknown`. A repository
must opt into any type it wants Fraeno to propose.

## Grouping

One dependency per pull request is the default. A dependency enters the first
configured group whose pattern matches. Every member of a selected group is
planned before Fraeno writes any manifest, so a failed rewrite cannot leave a
partial group in the working tree.

## Schedule and capacity

The trusted GitHub workflow wakes once a day. The repository policy decides
whether that date is eligible.

- `daily` runs every day.
- `weekly` requires a weekday.
- `monthly` requires a day from 1 through 28.
- `manual` runs only when a person dispatches the workflow.

A manual dispatch bypasses the calendar gate but keeps allow, ignore, update
type, cooldown, grouping, and open pull request limits.

`max_open_pull_requests` counts only open branches under `fraeno/update/`.
Refreshing an existing update pull request does not consume another slot.

## Duplicate and superseded proposals

Every dependency or explicit group has one deterministic branch. The pull
request body records a fingerprint of the dependency identities, old
resolutions, new resolutions, and manifests.

- The same fingerprint is skipped as a duplicate.
- A newer target refreshes the same branch and pull request.
- A dependency already managed by another open Fraeno branch is skipped.
- Branch refresh checks the planned pull request head and uses Git's lease
  check. A branch change after policy planning makes the workflow stop.

## Pull request evidence

Every generated pull request names:

- each changed manifest;
- every old and new resolution;
- the registry source, release date, and resolution evidence;
- the trusted build and observation scope;
- evidence that is still missing, including the physical hardware boundary.

Fraeno does not merge the pull request. The separate robot integration check
must finish with complete evidence before a maintainer decides to merge it.

## Repeatable local proof

Policy behavior can be tested without a registry or GitHub write:

```bash
fraeno propose-update fixtures/update_discovery/robot_repo \
  --config fraeno.example.yml \
  --catalog fixtures/update_discovery/catalog.json \
  --open-pull-requests open-pull-requests.json \
  --ros-distro humble \
  --os ubuntu \
  --os-version 22.04 \
  --architecture amd64 \
  --ignore-schedule \
  --dry-run \
  --output fraeno-update.json
```

The command accepts a JSON snapshot of open pull requests and produces a
complete deterministic plan. It does not contact GitHub or create a pull
request.
