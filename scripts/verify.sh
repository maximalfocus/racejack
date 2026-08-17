#!/usr/bin/env bash
# The complete verification boundary. Local runs and GitHub Actions invoke exactly this script, so
# "green on my machine" and "green in CI" mean the same thing.
#
#   bash scripts/verify.sh
#
# Requires Docker and nothing else: no PostgreSQL, no Python environment, no host tuning.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cleanup() {
  docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

step() { printf '\n==> %s\n' "$1"; }

step "building images"
docker compose build

step "starting the store (two replicas, one database, no egress)"
docker compose up --detach --wait app-a app-b

step "sequential demonstration, addressing two replicas"
demo_output="$(mktemp)"
docker compose run --rm --no-deps -T demo | tee "$demo_output"

summary_line="$(grep 'racejack-demo-summary:' "$demo_output" | tail -n 1)"
refusals="$(printf '%s' "$summary_line" | sed -n 's/.*"refusals":[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
if [ -z "${refusals}" ]; then
  echo "could not read the refusal count from the demonstration summary" >&2
  exit 1
fi

step "audit gate: exactly ${refusals} generic refusal events, and no token, in the app logs"
docker compose logs --no-log-prefix app-a app-b \
  | docker compose run --rm --no-deps -T verify python -m racejack.auditcheck --expected "$refusals"

step "sequential demonstration, addressing one replica (the run parameter is real)"
docker compose run --rm --no-deps -T -e RACEJACK_REPLICAS=1 demo >/dev/null
echo "one-replica run completed successfully"

step "concurrent load harness: genuine concurrent load against our own services only"
install -d -m 0777 artifacts
docker compose run --rm --no-deps harness

step "ruff, mypy, and the test suite, through the same boundary"
docker compose run --rm --no-deps verify

printf '\n==> verification complete\n'
