# Fraeno GitHub App

The hosted App connects Fraeno to a robot repository. It starts the trusted
validation workflow when a pull request changes software and reports the result
as `Fraeno / robot integration`.

## What runs where

The App does not clone or run customer source code. Baseline and candidate code
run in separate containers inside the repository's GitHub Actions job. A third
trusted runner compares their evidence and writes the final report.

The hosted service stores only the identifiers and timestamps needed to match a
pull request, workflow run, and check. It does not put App credentials into the
customer workflow.

## Repository permissions

- Actions: read and write
- Checks: read and write
- Contents: read
- Pull requests: read

The trusted scheduled update workflow uses its own narrowly scoped repository
permissions when it creates an update pull request.

## Events

Fraeno listens for pull request and workflow run events. GitHub also sends App
installation lifecycle events so Fraeno can report whether a repository is
ready.

## Repository readiness

A repository is ready when its default branch contains:

- `.fraeno.yml`
- `.github/workflows/fraeno-validation.yml`
- `.github/workflows/fraeno-updates.yml`
- `.github/fraeno/run-isolated-validation.sh`

Run `fraeno init --open-pr` to propose these trusted files. Run `fraeno doctor`
after that pull request is merged to see any missing file, permission, event, or
runner setting.

The runner image must include its complete `@sha256:` digest. A tag alone is
rejected.

## Private beta

Only approved installations receive a dispatched robot validation run during
the private beta. Other installations receive a neutral readiness result and
can request access at [fraeno.com](https://fraeno.com/#access).

Use the [demo robot](https://github.com/deepubuntu/fraeno-demo-robot) if you do
not yet have a robot repository. The [demo guide](try-demo.md) shows a safe
update passing and a behavior-breaking update being blocked without hardware.

For a real repository, follow the [onboarding guide](onboarding.md). The
[runner guide](runner.md) explains the isolation and evidence contract.
