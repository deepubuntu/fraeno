#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: test-external-runner.sh IMAGE" >&2
  exit 2
fi

image_id="$(docker image inspect --format '{{.Id}}' "$1")"
entrypoint="$(
  docker image inspect --format '{{join .Config.Entrypoint " "}}' "$image_id"
)"
if [[ "$entrypoint" != "/ros_entrypoint.sh python3 -m fraeno.cli" ]]; then
  echo "runner entrypoint does not initialize ROS" >&2
  exit 1
fi
docker run --rm --entrypoint /ros_entrypoint.sh "$image_id" \
  python3 -c 'import rclpy'

repository_fixture="$PWD/tests/fixtures/external_ros2_repository"
hostile_fixture="$PWD/tests/fixtures/hostile_candidate"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/fraeno-external-test.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT

cp -R "$repository_fixture" "$test_root/baseline"
cp -R "$repository_fixture" "$test_root/safe"
cp -R "$repository_fixture" "$test_root/hostile"
cp "$hostile_fixture/hostile.py" "$test_root/hostile/hostile.py"
printf 'best-effort\n' > "$test_root/hostile/robot-profile.txt"

if find "$test_root/baseline" -path '*/src/fraeno' -o -name 'Dockerfile.runner' |
  grep -q .; then
  echo "external fixture unexpectedly contains Fraeno source" >&2
  exit 1
fi

bash runner/run-isolated-validation.sh \
  "$image_id" \
  "$test_root/baseline" \
  "$test_root/safe" \
  "$test_root/baseline/.fraeno.yml" \
  "$test_root/safe-report.json"

python3 - "$test_root/safe-report.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
assert report["engine"] == {"name": "fraeno", "version": "0.1.0"}
assert report["outcome"] == "pass"
PY

set +e
bash runner/run-isolated-validation.sh \
  "$image_id" \
  "$test_root/baseline" \
  "$test_root/hostile" \
  "$test_root/baseline/.fraeno.yml" \
  "$test_root/hostile-report.json"
hostile_status=$?
set -e
test "$hostile_status" -eq 1

python3 - "$test_root/hostile-report.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
assert report["engine"] == {"name": "fraeno", "version": "0.1.0"}
assert report["outcome"] == "block"
attempts = report["candidate"]["observation"]["metadata"][
    "protected_write_attempts_succeeded"
]
assert attempts
assert set(attempts) == {
    "baseline_evidence",
    "candidate_evidence",
    "final_report",
    "trusted_observer",
    "validator",
}
assert not any(attempts.values())
assert any(
    finding["code"] in {"candidate-unhealthy", "topic-qos-incompatible"}
    for finding in report["comparison"]["findings"]
)
PY
