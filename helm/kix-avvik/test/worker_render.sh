#!/usr/bin/env bash
# What the worker release must and must not render (U11).
#
# `helm lint` answers "is this chart valid", which every one of these files was before any of the
# overrides below existed. What this script answers is the different question: does the SECOND
# release of a chart written for the API come out as a worker? The overrides that make it one are
# all NEGATIVE -- no Service, no Ingress, no VAPID keys, no SSO client secret -- and a negative that
# stops holding renders perfectly valid YAML. The one that would be visible from the internet is the
# ingress: the chart's base values set `enabled: true` with `trafficType: public`, so a worker file
# that forgot to turn it off publishes this workload on a public host.
#
# It runs in CI (ci.yml's `helm-render` job) and locally: `bash helm/kix-avvik/test/worker_render.sh`.
# It FAILS rather than skips when helm is absent -- a check that quietly passes where the binary is
# missing is a check that has never run anywhere it matters.
set -euo pipefail

cd "$(dirname "$0")/../../.."
CHART=helm/kix-avvik
RELEASE=kix-avvik-worker

if ! command -v helm >/dev/null 2>&1; then
  echo "FAIL: helm is not on PATH; this check cannot be skipped into passing" >&2
  exit 1
fi

fail() { echo "FAIL: $*" >&2; exit 1; }

# The common chart is FETCHED, not committed -- `.gitignore` excludes `helm/**/charts/` and
# `Chart.lock` is what is versioned -- so a fresh checkout has no dependency to template against and
# `helm template` fails with "found in Chart.yaml, but missing in charts/ directory". `build` and
# not `update`: build resolves exactly what Chart.lock pins, so CI renders the same chart version
# the deploy will, while `update` would silently re-resolve and could template a version nobody
# chose. Skipped where the archive is already there, which is the local case.
if ! ls "$CHART"/charts/common-*.tgz >/dev/null 2>&1; then
  helm dependency build "$CHART" >/dev/null 2>&1 \
    || fail "could not fetch the common chart pinned in $CHART/Chart.lock"
fi
checks=0
check() { checks=$((checks + 1)); }

for env in dev tst prd; do
  values="$CHART/env/worker-values-kub-ent-$env.yaml"
  [ -f "$values" ] || fail "$values is missing"
  rendered=$(helm template "$RELEASE" "$CHART" -f "$values")

  # --- Nothing this workload is not. ----------------------------------------------------------
  # Both are ON in the chart's base values.yaml for the API, so both have to be turned off in every
  # worker file. An ingress here would be a public host in front of a process with no HTTP API at
  # all except its own liveness probe.
  check; if echo "$rendered" | grep -q "^kind: Service$"; then fail "$env: the worker release renders a Service"; fi
  check; if echo "$rendered" | grep -q "^kind: Ingress$"; then fail "$env: the worker release renders an Ingress"; fi

  # --- Secrets: the omissions are the point. --------------------------------------------------
  # Delivery signs and therefore needs WEBHOOK_SIGNING_ROOT. It never verifies a client credential,
  # serves no browser and hosts no login, so the pepper, the two VAPID keys, the two SSO secrets and
  # the feedback pair must not reach this pod. Checked as a set rather than one by one: what has to
  # hold is that NOTHING but the signing root is here, which is also the check that sees a secret
  # added next year.
  for forbidden in NOTIFICATION_KEY_PEPPER VAPID_PRIVATE_KEY VAPID_PUBLIC_KEY \
                   SSO_AUTH0_CLIENT_SECRET SSO_SESSION_SECRET POSTHOG_PROJECT_TOKEN \
                   VARSEL_FEEDBACK_RECEIPT_SECRET; do
    check
    if echo "$rendered" | grep -q "$forbidden"; then fail "$env: the worker release names $forbidden"; fi
  done

  if [ "$env" = "tst" ]; then
    # tst holds neither notification secret, and the worker refuses to start without the signing
    # root -- so the release renders no workload at all rather than a guaranteed CrashLoopBackOff.
    check; if echo "$rendered" | grep -q "^kind: Deployment$"; then fail "tst: the worker must render no Deployment"; fi
    check; if echo "$rendered" | grep -q "kind: ExternalSecret"; then fail "tst: the worker must name no secret"; fi
    continue
  fi

  # --- One workload, and it is the worker. ----------------------------------------------------
  check; [ "$(echo "$rendered" | grep -c '^kind: Deployment$')" = "1" ] || fail "$env: expected exactly one Deployment"
  check; echo "$rendered" | grep -q "^kind: ConfigMap$" || fail "$env: no ConfigMap"
  check; echo "$rendered" | grep -q "name: $RELEASE-notifications" || fail "$env: the signing secret is not wired"
  check; echo "$rendered" | grep -q "WEBHOOK_SIGNING_ROOT" || fail "$env: WEBHOOK_SIGNING_ROOT is not named"
  # The entrypoint. Without it the release is a SECOND COPY OF THE API: same image, same CMD, a
  # second poller thread against one GCS state blob and one Firestore -- which is the failure
  # `deployment.forceReplicas: 1` exists to prevent on the API side, arriving by another door.
  check; echo "$rendered" | grep -q -- "- webhook_worker" || fail "$env: the worker entrypoint is not set"
  check; echo "$rendered" | grep -q "name: worker" || fail "$env: the container is not named worker"

  # --- Probes on the worker's own port, and the scrape on the exporter's. ---------------------
  # Three probes, all httpGet on 8081. The chart derives every probe port from service.internalPort,
  # so this is really a check that the file set it: left at the default the probes would dial 8080,
  # where this process listens to nothing, and the pod would never become Ready.
  check; [ "$(echo "$rendered" | grep -c 'port: 8081')" -ge 3 ] || fail "$env: probes are not on the worker's health port"
  check; echo "$rendered" | grep -q 'path: /health' || fail "$env: probes do not use /health"
  check; echo "$rendered" | grep -q 'containerPort: 8081' || fail "$env: the container port is not the health port"
  check; echo "$rendered" | grep -q 'prometheus.io/port: "9464"' || fail "$env: the scrape port is not stated"
  check; echo "$rendered" | grep -q 'prometheus.io/scrape: "true"' || fail "$env: the scrape is not enabled"

  # --- The grace period covers a round. -------------------------------------------------------
  # The loop reads its stop flag only between rounds, so below a round's worst case (~40 s plus the
  # stale sweep) SIGKILL lands mid-round. Safe -- U8's lease returns the claims -- but it re-sends,
  # and on every rollout.
  check; echo "$rendered" | grep -q 'terminationGracePeriodSeconds: 75' || fail "$env: the grace period does not cover a round"

  # --- The database, which three files now have to agree about. -------------------------------
  check; echo "$rendered" | grep -q 'NOTIFICATION_FIRESTORE_DATABASE: notifications' || fail "$env: the database is not named"
done

echo "ok  worker release renders as a worker in dev, tst and prd ($checks checks)"
