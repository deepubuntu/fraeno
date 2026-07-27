#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: test-onboarding.sh IMAGE" >&2
  exit 2
fi

image_id="$(docker image inspect --format '{{.Id}}' "$1")"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/fraeno-onboarding-test.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT

mkdir -p "$test_root/robot"
cp -R fixtures/ros2_qos_robot/src "$test_root/robot/src"
cp \
  fixtures/ros2_qos_robot/fraeno-fixture-version.txt \
  "$test_root/robot/fraeno-fixture-version.txt"

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "$test_root/robot:/workspace" \
  --workdir /workspace \
  "$image_id" \
  init . \
  --project-name onboarding-robot \
  --build-command "colcon build --event-handlers console_direct+" \
  --launch-command "ros2 launch fraeno_ros_fixture robot.launch.py" \
  --required-node /sensor_driver \
  --required-node /controller \
  --required-topic /sensor/reading \
  --required-topic /robot/command \
  --required-service /robot/health \
  --required-action /robot/move \
  --required-transform "base_link->sensor_link" \
  --required-diagnostic controller \
  --required-diagnostic sensor_driver \
  --rate-topic /sensor/reading \
  --rate-topic /robot/command \
  --diagnostics-topic /diagnostics \
  --transform-topic /tf_static

test -f "$test_root/robot/.fraeno.yml"
test -f "$test_root/robot/.github/workflows/fraeno-validation.yml"
test -f "$test_root/robot/.github/workflows/fraeno-updates.yml"
test -x "$test_root/robot/.github/fraeno/run-isolated-validation.sh"
grep -Fq \
  'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1' \
  "$test_root/robot/.github/workflows/fraeno-validation.yml"
grep -Fq \
  'actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a' \
  "$test_root/robot/.github/workflows/fraeno-validation.yml"

cp -R "$test_root/robot" "$test_root/baseline"
cp -R "$test_root/robot" "$test_root/candidate"

bash "$test_root/baseline/.github/fraeno/run-isolated-validation.sh" \
  "$image_id" \
  "$test_root/baseline" \
  "$test_root/candidate" \
  "$test_root/baseline/.fraeno.yml" \
  "$test_root/fraeno-report.json"

python3 - "$test_root/fraeno-report.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
assert report["outcome"] == "pass"
assert report["baseline"]["observation"]["healthy"] is True
assert report["candidate"]["observation"]["healthy"] is True
PY
