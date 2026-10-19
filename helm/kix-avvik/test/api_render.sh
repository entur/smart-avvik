#!/usr/bin/env bash
# What the API release must render, in both of its shapes (U7, KTD9, KTD15).
#
# `helm lint` passes every misrender this script exists for. The chart turns three plausible values
# into something else without a word: `maxUnavailable: 0` meets the chart's `| default` and renders its default (25% since common 2.1.1), `hpa.spec`
# replaces the whole spec and renders neither minReplicas nor maxReplicas, and the HPA's minReplicas
# defaults to 2. And the rollout is two deploys per environment: step A (every environment file sets
# `deployment.forceReplicas: 1` -> Recreate, one replica, no HPA) and step B (the line deleted from
# one environment's file -> the base file's RollingUpdate and HPA). A `forceReplicas` back in the base
# file would flip prd together with dev on prd's next dispatch, so the base must never carry it.
#
# So this renders each committed environment file and asserts the shape it is in, and ALSO renders dev
# and prd with their `deployment:` block cut out, which is exactly what step B commits, so that shape
# is proven before anyone deletes it. tst keeps forceReplicas permanently (no store, no lease, no
# tables).
#
# It runs in CI (ci.yml's `helm-render` job) and locally: `bash helm/kix-avvik/test/api_render.sh`.
# It FAILS rather than skips when helm is absent.
set -euo pipefail

cd "$(dirname "$0")/../../.."
CHART=helm/kix-avvik
RELEASE=kix-avvik  # gha-helm's default release name: the repository's

if ! command -v helm >/dev/null 2>&1; then
  echo "FAIL: helm is not on PATH; this check cannot be skipped into passing" >&2
  exit 1
fi

fail() { echo "FAIL: $*" >&2; exit 1; }

# Same dependency fetch as worker_render.sh (the chart is fetched, not committed); see its comment.
if ! ls "$CHART"/charts/common-*.tgz >/dev/null 2>&1; then
  repo=$(awk '/repository:/ {gsub(/"/, "", $2); print $2; exit}' "$CHART/Chart.yaml")
  [ -n "$repo" ] || fail "no dependency repository in $CHART/Chart.yaml"
  out=$(helm repo add api-render-dep "$repo" --force-update 2>&1) \
    || fail "could not add $repo: $out"
  out=$(helm dependency build "$CHART" 2>&1) \
    || fail "could not fetch the chart pinned in $CHART/Chart.lock: $out"
fi
checks=0
check() { checks=$((checks + 1)); }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# One rendered document by kind (helm separates them with `---` lines).
doc() {
  awk -v k="kind: $1" '/^---/ { if (hit) printf "%s", buf; buf = ""; hit = 0; next }
                       { buf = buf $0 "\n"; if ($0 == k) hit = 1 }
                       END { if (hit) printf "%s", buf }'
}

# --- The base file never carries forceReplicas. ------------------------------------------------
check; if grep -qE '^[[:space:]]*forceReplicas:' "$CHART/values.yaml"; then
  fail "the base values.yaml sets forceReplicas: it belongs in each environment file (KTD15)"
fi

# What every API render shares, whichever strategy: probes, grace and the preStop hook.
common_shape() {
  local label=$1 rendered=$2 readiness=$3 dep
  dep=$(echo "$rendered" | doc Deployment)
  check; [ "$(echo "$rendered" | grep -c '^kind: Deployment$')" = "1" ] || fail "$label: expected exactly one Deployment"
  check; echo "$dep" | grep -A2 'readinessProbe:' | grep -q "path: $readiness$" || fail "$label: readiness is not $readiness"
  check; echo "$dep" | grep -A2 'livenessProbe:' | grep -q 'path: /health$' || fail "$label: liveness is not /health"
  # Equal to poller_lease.GRACE_S (test_notification_contract pins the values file to it). Without
  # it Kubernetes kills the pod after its 30 s default, mid-phase.
  check; echo "$dep" | grep -q 'terminationGracePeriodSeconds: 270$' || fail "$label: the grace period is not 270 s"
  check; echo "$dep" | grep -A6 'preStop:' | grep -q -- '- /usr/bin/python3$' || fail "$label: no preStop hook on the image's python"
  check; echo "$dep" | grep -A6 'preStop:' | grep -q 'time.sleep(5)' || fail "$label: the preStop does not sleep 5 s"
}

for env in dev tst prd; do
  values="$CHART/env/values-kub-ent-$env.yaml"
  [ -f "$values" ] || fail "$values is missing"
  readiness=/ready; [ "$env" = "tst" ] && readiness=/health

  # --- Step A, as committed: every environment file still forces one replica. ----------------
  # Step B on this branch is a later one-line PR per environment, so for now all three must keep it;
  # tst keeps it for good.
  check; grep -qE '^    forceReplicas: 1$' "$values" || fail "$env: the environment file does not set forceReplicas: 1"
  rendered=$(helm template "$RELEASE" "$CHART" -f "$values")
  dep=$(echo "$rendered" | doc Deployment)
  common_shape "$env" "$rendered" "$readiness"
  check; echo "$dep" | grep -q 'type: Recreate$' || fail "$env: not Recreate while forceReplicas is set"
  check; echo "$dep" | grep -q '^  replicas: 1$' || fail "$env: replicas is not 1 while forceReplicas is set"
  check; if echo "$rendered" | grep -q '^kind: HorizontalPodAutoscaler$'; then fail "$env: an HPA renders while forceReplicas is set"; fi

  [ "$env" = "tst" ] && continue

  # --- Step B, proven before it is committed: the file with its `deployment:` block deleted. --
  # The WHOLE block, which is the step-B edit: deleting only the forceReplicas line leaves
  # `deployment:` null, and Helm reads a null override as "delete the key", which drops the base
  # file's entire deployment map (minReplicas, maxUnavailable, the grace) and fails the render.
  step_b="$tmp/values-kub-ent-$env.yaml"
  awk '/^  deployment:$/ { skip = 1; next } skip && /^  [^ #]/ { skip = 0 } !skip' "$values" > "$step_b"
  check; if grep -q 'forceReplicas' "$step_b"; then fail "$env: could not cut the deployment block for the step-B render"; fi
  rendered=$(helm template "$RELEASE" "$CHART" -f "$step_b")
  dep=$(echo "$rendered" | doc Deployment)
  hpa=$(echo "$rendered" | doc HorizontalPodAutoscaler)
  pdb=$(echo "$rendered" | doc PodDisruptionBudget)
  common_shape "$env (rolling)" "$rendered" "$readiness"
  check; echo "$dep" | grep -q 'type: RollingUpdate$' || fail "$env (rolling): not RollingUpdate"
  # The string "0%": an integer 0 meets the chart's `| default` and renders its default.
  check; echo "$dep" | grep -q 'maxUnavailable: 0%$' || fail "$env (rolling): maxUnavailable is not 0% (an integer 0 renders the chart default)"
  check; echo "$dep" | grep -q 'maxSurge: 1$' || fail "$env (rolling): maxSurge is not 1"
  check; if echo "$dep" | grep -q '^  replicas:'; then fail "$env (rolling): replicas is rendered, so the HPA and the Deployment fight"; fi
  check; [ "$(echo "$rendered" | grep -c '^kind: HorizontalPodAutoscaler$')" = "1" ] || fail "$env (rolling): expected exactly one HPA"
  # hpa.spec replaces the chart's spec and renders neither bound; the chart's own minimum is 2.
  check; echo "$hpa" | grep -q '^  minReplicas: 1$' || fail "$env (rolling): the HPA minimum is not 1"
  check; echo "$hpa" | grep -q '^  maxReplicas: 3$' || fail "$env (rolling): the HPA maximum is not 3"
  check; echo "$hpa" | grep -q 'averageUtilization: 80$' || fail "$env (rolling): the CPU target is not 80 %"
  check; echo "$hpa" | grep -A3 'scaleTargetRef:' | grep -q "name: $RELEASE$" || fail "$env (rolling): the HPA does not target $RELEASE"
  check; echo "$hpa" | grep -A6 'scaleUp:' | grep -q 'periodSeconds: 60$' || fail "$env (rolling): scale-up is not one pod per 60 s"
  check; echo "$hpa" | grep -A1 'scaleDown:' | grep -q 'stabilizationWindowSeconds: 600$' || fail "$env (rolling): scale-down is not stabilised 600 s"
  check; echo "$pdb" | grep -q 'minAvailable: 0%$' || fail "$env (rolling): the PDB is not minAvailable 0%"
done

echo "ok  API release renders Recreate in dev, tst and prd, and RollingUpdate+HPA in dev and prd without forceReplicas ($checks checks)"
