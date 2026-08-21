# Try Fraeno without a robot repository

The public
[Fraeno demo robot](https://github.com/deepubuntu/fraeno-demo-robot) lets you
test the complete GitHub App flow without robot hardware or an existing ROS 2
project.

The repository contains a sensor driver and controller running on ROS 2 Humble,
Ubuntu 22.04, and `amd64`. You will create one harmless pull request that passes
and one simulated driver update that still builds but stops sensor data from
reaching the controller. Fraeno must block the second pull request.

## Start the trial

1. Open the [demo robot](https://github.com/deepubuntu/fraeno-demo-robot).
2. Click **Use this template** and create a repository under your GitHub
   account. You can also fork it.
3. Enable GitHub Actions if GitHub paused workflows on your copy.
4. [Request private-beta access](https://fraeno.com/#access) with your GitHub
   username.
5. [Install the Fraeno GitHub App](https://github.com/apps/fraeno-robotics) on
   only the copied repository.
6. Add the `FRAENO_RUNNER_IMAGE` repository variable shown in the demo
   repository README.
7. Follow its safe-update and dangerous-update commands.

Until the installation is approved, Fraeno creates a neutral
**Fraeno is in private beta** check and does not dispatch the robot test.

## Expected result

The safe pull request completes with a green **Fraeno / robot integration**
check.

The dangerous pull request changes the sensor publisher from reliable delivery
to best effort. The project still builds, but the controller receives no sensor
readings and `/robot/command` falls silent. Fraeno reports the regression and
blocks the pull request.

The check protects only the behaviors declared in `.fraeno.yml`. It does not
claim to prove every possible physical behavior safe.

See a real [safe external trial pass](https://github.com/Thabhelo/fraeno-demo-trial/actions/runs/32513196936)
and a [dangerous external update blocked](https://github.com/Thabhelo/fraeno-demo-trial/actions/runs/32513414015).

When you are ready to connect a real robot project, continue with the
[complete onboarding guide](onboarding.md).
