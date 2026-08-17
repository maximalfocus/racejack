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
  docker compose --profile vulnerable down --volumes --remove-orphans >/dev/null 2>&1 || true
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

step "concurrent load harness against the secure application"
install -d -m 0777 artifacts
docker compose run --rm --no-deps harness

step "containment: the vulnerable application is not started by the default path"
if docker compose ps --services | grep -qx 'vuln-a'; then
  echo "the vulnerable application was started by the default Compose path" >&2
  exit 1
fi
if docker compose config --services | grep -qx 'vuln-a'; then
  echo "the vulnerable application is not behind an opt-in profile" >&2
  exit 1
fi
echo "not selected without its opt-in profile"

step "containment: the opt-in profile alone is not an acknowledgement"
if docker compose --profile vulnerable run --rm --no-deps -T -e ALLOW_VULNERABLE_DEMO= vuln-a \
     python -c "import racejack.vulnerable.app" >/dev/null 2>&1; then
  echo "the vulnerable application started without ALLOW_VULNERABLE_DEMO=true" >&2
  exit 1
fi
echo "refused to start without ALLOW_VULNERABLE_DEMO=true"

step "starting the vulnerable application (both opt-in actions, and only now)"
ALLOW_VULNERABLE_DEMO=true docker compose --profile vulnerable up --detach --wait vuln-a vuln-b

step "vulnerable · deterministic mode — the defect is REQUIRED to reproduce"
docker compose run --rm --no-deps -T harness \
  python -m racejack.harness --variant vulnerable --mode deterministic

step "vulnerable · natural mode — no instrumentation at all; reports only what it observed"
docker compose run --rm --no-deps -T harness \
  python -m racejack.harness --variant vulnerable --mode natural

step "comparison across every scenario"
docker compose run --rm --no-deps -T compare

step "restoring the secure baseline before the suite runs"
docker compose run --rm --no-deps -T harness python -m racejack.seed --secure

step "ruff, mypy, and the test suite, through the same boundary"
RACEJACK_REQUIRE_VULNERABLE=1 docker compose run --rm --no-deps \
  -e RACEJACK_REQUIRE_VULNERABLE=1 verify

printf '\n==> verification complete\n'
