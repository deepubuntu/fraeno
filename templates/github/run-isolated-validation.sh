#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 5 ]]; then
  echo "usage: run-isolated-validation.sh IMAGE BASELINE CANDIDATE CONFIG OUTPUT" >&2
  exit 2
fi

image="$1"
baseline="$(cd "$2" && pwd)"
candidate="$(cd "$3" && pwd)"
config="$(cd "$(dirname "$4")" && pwd)/$(basename "$4")"
output_parent="$(cd "$(dirname "$5")" && pwd)"
output_name="$(basename "$5")"

if [[ "$image" != *@sha256:* && "$image" != sha256:* ]]; then
  echo "Fraeno runner image must be pinned by digest" >&2
  exit 2
fi
if [[ ! -f "$config" ]]; then
  echo "Fraeno config does not exist at $config" >&2
  exit 2
fi

temporary_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
run_root="$(mktemp -d "${temporary_root%/}/fraeno-run.XXXXXX")"
containers=()
cleanup() {
  for container in "${containers[@]}"; do
    docker rm --force --volumes "$container" >/dev/null 2>&1 || true
  done
  rm -rf "$run_root"
}
trap cleanup EXIT
chmod 0700 "$run_root"

common_sandbox=(
  --read-only
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=4g
  --mount type=volume,dst=/evidence,volume-nocopy
  --cap-drop ALL
  --cap-add CHOWN
  --cap-add DAC_OVERRIDE
  --cap-add SETUID
  --cap-add SETGID
  --security-opt no-new-privileges
  --pids-limit 1024
)

capture_workspace() {
  local phase="$1"
  local source="$2"
  local evidence="$3"
  local container
  container="$(
    docker create "${common_sandbox[@]}" \
      --mount "type=bind,src=$source,dst=/source,readonly" \
      --mount "type=bind,src=$baseline,dst=/trusted,readonly" \
      --mount "type=bind,src=$config,dst=/config/fraeno.yml,readonly" \
      "$image" \
      capture-workspace \
      --source /source \
      --trusted-root /trusted \
      --config /config/fraeno.yml \
      --phase "$phase" \
      --output /evidence/run.json
  )"
  containers+=("$container")
  docker start --attach "$container"
  docker cp "$container:/evidence/run.json" "$evidence"
  chmod 0444 "$evidence"
  docker rm --volumes "$container" >/dev/null
}

capture_workspace baseline "$baseline" "$run_root/baseline.json"
capture_workspace candidate "$candidate" "$run_root/candidate.json"

comparison_container="$(
  docker create \
  --read-only \
  --network none \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --mount type=volume,dst=/report,volume-nocopy \
  --mount "type=bind,src=$run_root/baseline.json,dst=/evidence/baseline.json,readonly" \
  --mount "type=bind,src=$run_root/candidate.json,dst=/evidence/candidate.json,readonly" \
  --mount "type=bind,src=$config,dst=/config/fraeno.yml,readonly" \
  "$image" \
  assemble-report \
  --baseline /evidence/baseline.json \
  --candidate /evidence/candidate.json \
  --config /config/fraeno.yml \
  --output "/report/$output_name"
)"
containers+=("$comparison_container")
set +e
docker start --attach "$comparison_container"
comparison_status=$?
set -e
docker cp \
  "$comparison_container:/report/$output_name" \
  "$output_parent/$output_name"
docker rm --volumes "$comparison_container" >/dev/null
exit "$comparison_status"
