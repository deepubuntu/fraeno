#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: test-update-workflow-sandbox.sh IMAGE" >&2
  exit 2
fi

image_id="$(docker image inspect --format '{{.Id}}' "$1")"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/fraeno-update-sandbox.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT
repository="$test_root/repository"

git init --quiet "$repository"
git -C "$repository" config user.name "Fraeno test"
git -C "$repository" config user.email "fraeno@example.com"
printf 'safe hook\n' > "$repository/.git/hooks/fraeno-guard"
config_before="$(git hash-object "$repository/.git/config")"
hook_before="$(git hash-object "$repository/.git/hooks/fraeno-guard")"

docker run --rm \
  --entrypoint bash \
  --user "$(id -u):$(id -g)" \
  --volume "$repository:/workspace" \
  --volume "$repository/.git:/workspace/.git:ro" \
  --workdir /workspace \
  "$image_id" \
  -euo pipefail -c '
    if printf "malicious hook\n" > .git/hooks/fraeno-guard; then
      echo "the container replaced a Git hook" >&2
      exit 1
    fi
    if printf "malicious config\n" >> .git/config; then
      echo "the container changed Git config" >&2
      exit 1
    fi
    if mv .git .git-shadow; then
      echo "the container replaced the protected Git directory" >&2
      exit 1
    fi
    printf "requests==2.32.5\n" > requirements.txt
  '

test -f "$repository/requirements.txt"
test ! -e "$repository/.git-shadow"
test "$(git hash-object "$repository/.git/config")" = "$config_before"
test \
  "$(git hash-object "$repository/.git/hooks/fraeno-guard")" \
  = "$hook_before"
