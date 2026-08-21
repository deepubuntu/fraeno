# Add Fraeno to a robot repository

Fraeno needs one contract, one trusted workflow, and one small runner script in
the robot repository. `fraeno init` writes those files without replacing
anything that already exists.

## Before you start

The supported private-beta target is ROS 2 Humble on Ubuntu 22.04 with the
`amd64` architecture. Other architectures are not part of the current support
promise.

You need:

- a Git repository hosted on GitHub
- the GitHub CLI authenticated for that repository
- Docker running locally
- a built ROS 2 workspace
- the command that launches the complete system
- approval for the Fraeno private beta

Do not launch a physical robot unless the work area and emergency stop are
ready. The normal doctor check does not launch it.

If you do not have a robot repository, stop here and follow the
[demo-robot guide](try-demo.md). It runs entirely in GitHub Actions.

## Request private-beta access

1. [Request access](https://fraeno.com/#access) with the GitHub username that
   owns the robot repository.
2. [Install the Fraeno GitHub App](https://github.com/apps/fraeno-robotics) on
   only the repository you want Fraeno to check.
3. Wait for the installation to be approved. Before approval, pull requests
   receive a neutral **Fraeno is in private beta** check.

## Create the onboarding pull request

Install the current public release in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install \
  "git+https://github.com/deepubuntu/fraeno.git@v0.2.3"
```

From the robot repository, run:

```bash
fraeno init . \
  --project-name warehouse-robot \
  --build-command "colcon build --event-handlers console_direct+" \
  --launch-command "ros2 launch warehouse_bringup system.launch.py" \
  --required-node /controller \
  --required-topic /robot/command \
  --rate-topic /robot/command \
  --runner-image "us-central1-docker.pkg.dev/deepubuntu-32f9e/fraeno-runner/runner@sha256:8f932a56209a0a8ecfbda3fff9958fd9b710d1d0065a9312486e9acd674fdcfc" \
  --open-pr
```

This creates a draft pull request containing:

```text
.fraeno.yml
.github/workflows/fraeno-validation.yml
.github/workflows/fraeno-updates.yml
.github/fraeno/run-isolated-validation.sh
```

It also sets the `FRAENO_RUNNER_IMAGE` repository variable to the immutable
image you supplied. The command stops if the working tree is not clean or any
generated file already exists.

Review the launch command, required graph entities, topic rates, and target.
Merge the onboarding pull request only when the contract describes the real
system.

## GitHub App permissions

The App needs:

- Actions read and write
- Checks read and write
- Contents read
- Metadata read
- Pull requests read

Fraeno subscribes to check run, pull request, and workflow run events.

## Check the repository

Run the safe check first:

```bash
fraeno doctor .
```

It checks the target, config, required files, executable commands, built
workspace, Docker, immutable runner image, trusted files on the default branch,
and GitHub access. It names every missing file and permission.

When it is safe to launch the complete system, run:

```bash
fraeno doctor . --run-observer
```

## Open the first test pull request

Create a harmless branch after the onboarding pull request is merged:

```bash
git switch -c fraeno/first-test
printf '\nFraeno first test\n' >> README.md
git add README.md
git commit -m "Test Fraeno"
git push --set-upstream origin fraeno/first-test
gh pr create --title "Test Fraeno" --body "This pull request proves the first Fraeno run."
```

Wait for `Fraeno / robot integration`, then run doctor with that pull request:

```bash
fraeno doctor . --pull-request NUMBER
```

The App check proves that GitHub delivered the pull request to Fraeno. Doctor
requires the check to finish through the trusted workflow. That round trip
proves Fraeno could receive the pull request, dispatch the workflow, read its
result, and finish the check for this repository. Doctor reports the robot
integration result separately. A completed failed check proves the App works,
but the repository is not ready.

Doctor also reads the permissions and events shown on the App identity. GitHub
reports the current App registration there, not the access accepted by this
repository installation. Doctor labels that information as registration
metadata and does not use it by itself as proof that the installation works.

The first test should pass because it changes only documentation. A passing
result proves the configured build and observation path on this repository. It
does not prove behavior on physical hardware.

## If doctor cannot prove the App installation

GitHub does not let a normal user token inspect a private GitHub App
installation directly. Fraeno therefore requires a completed round trip on the
current test pull request. If the check is missing or does not finish, doctor
tells you to install or grant the repository to Fraeno, accept any pending
access update, and reopen the pull request.
