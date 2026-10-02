#!/usr/bin/env bash
# cmbench driver. Runs every trial strictly sequentially: at most one watcher exists at any time.
#
# Per trial (sampling is continuous; phases only label the timeline):
#   fresh namespace -> sampler pod -> watcher pod
#   startup (until SYNCED) -> warmup -> idle_pre (baseline, default 60 min)
#   -> create N CMs -> delete N CMs
#   -> post_near (0-10 min) -> post_mid (10-30 min) -> post_late (30-60 min)
#   collect data -> verify event counts -> tear down -> cooldown
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=../bench.env
source ./bench.env
: "${BENCH_NODE:?set BENCH_NODE to the k0s worker node to measure on}"

for bin in kubectl envsubst; do
  command -v "$bin" >/dev/null || { echo "missing required tool: $bin" >&2; exit 1; }
done

RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
OUT=results/$RUN_ID
mkdir -p "$OUT"
ln -sfn "$RUN_ID" results/latest

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT/driver.log" >&2; }

export BENCH_NS PROBE_NS BENCH_NODE SAMPLER_IMAGE SAMPLE_MS SNAPSHOTS CPU_LIMIT MEM_LIMIT LIST_MODE WORKERS WATCH_TIMEOUT_S
export ENABLE_PERF PERF_INTERVAL_MS PERF_CONTAINER_IMAGE GO_WATCHLIST GO_DISABLE_HTTP2 IMAGE IMPL TARGET_COMM
GO_WATCHLIST=$([[ $LIST_MODE == streaming ]] && echo true || echo false)
GO_DISABLE_HTTP2=$([[ $GO_HTTP2 == true ]] && echo "" || echo true)
PERF_CONTAINER_IMAGE=$([[ $ENABLE_PERF == true ]] && echo "$PERF_IMAGE" || echo "$SAMPLER_IMAGE")
VARS='${BENCH_NS} ${PROBE_NS} ${BENCH_NODE} ${SAMPLER_IMAGE} ${SAMPLE_MS} ${SNAPSHOTS} ${CPU_LIMIT} ${MEM_LIMIT} ${LIST_MODE} ${ENABLE_PERF} ${PERF_INTERVAL_MS} ${PERF_CONTAINER_IMAGE} ${GO_WATCHLIST} ${GO_DISABLE_HTTP2} ${IMAGE} ${IMPL} ${TARGET_COMM} ${WORKERS} ${WATCH_TIMEOUT_S}'
render() { envsubst "$VARS" < "$1"; }

BASE_NS=$BENCH_NS
cleanup() {
  kubectl -n "$BENCH_NS" delete pod cmwatch --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl -n "$PROBE_NS" delete pod sampler --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [[ $CORDON == true ]]; then kubectl uncordon "$BENCH_NODE" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

set_phase() {
  kubectl -n "$PROBE_NS" exec sampler -c sampler -- sh -c "echo $1 > /run/bench/phase"
  log "  phase=$1"
}

wait_gone() { # ns kind/name
  kubectl -n "$1" wait "$2" --for=delete --timeout=120s >/dev/null 2>&1 || true
}

wait_synced() {
  local deadline=$((SECONDS + 180))
  until kubectl -n "$BENCH_NS" logs cmwatch 2>/dev/null | grep -q '^SYNCED '; do
    ((SECONDS < deadline)) || { log "  watcher did not sync in time"; return 1; }
    sleep 1
  done
}

# ---------- preflight + provenance ----------
log "run $RUN_ID on node $BENCH_NODE"
kubectl get node "$BENCH_NODE" -o yaml > "$OUT/node.yaml"
kubectl version -o yaml > "$OUT/kubectl-version.yaml" 2>/dev/null || true
cp bench.env "$OUT/bench.env"
env | grep -E '^(BENCH_|PROBE_NS|RS_IMAGE|GO_IMAGE|SAMPLER_IMAGE|PERF_IMAGE|TRIALS|IMPLS|WARMUP_S|IDLE_PRE_S|SETTLE_S|POST_|COOLDOWN_S|CM_|SAMPLE_MS|SNAPSHOTS|ENABLE_PERF|PERF_INTERVAL_MS|CPU_LIMIT|MEM_LIMIT|WORKERS|WATCH_TIMEOUT_S|LIST_MODE|GO_HTTP2|CORDON)=' \
  | sort > "$OUT/effective.env" || true

# Host state (CPU frequency policy, load, co-located VMs). Meaningful when the driver runs on
# the hypervisor that hosts BENCH_NODE; recorded again at the start of every trial.
host_state() {
  local c=/sys/devices/system/cpu
  echo "time=$(date -u +%FT%TZ) host=$(hostname)"
  echo "governor=$(cat $c/cpu0/cpufreq/scaling_governor 2>/dev/null || echo NA) driver=$(cat $c/cpu0/cpufreq/scaling_driver 2>/dev/null || echo NA)"
  echo "governors_all=$(cat $c/cpu*/cpufreq/scaling_governor 2>/dev/null | sort | uniq -c | tr -s ' \n' ' ')"
  echo "no_turbo=$(cat $c/intel_pstate/no_turbo 2>/dev/null || echo NA) boost=$(cat $c/cpufreq/boost 2>/dev/null || echo NA)"
  echo "min_khz=$(cat $c/cpu0/cpufreq/scaling_min_freq 2>/dev/null || echo NA) max_khz=$(cat $c/cpu0/cpufreq/scaling_max_freq 2>/dev/null || echo NA)"
  echo "cur_mhz_hist=$(awk '/MHz/{print int($4/100)*100}' /proc/cpuinfo | sort -n | uniq -c | tr -s ' \n' ' ')"
  echo "loadavg=$(cat /proc/loadavg)"
  if command -v virsh >/dev/null; then
    echo "vms_running=$(virsh -c qemu:///system list --name 2>/dev/null | grep . | tr '\n' ' ')"
  fi
}
host_state > "$OUT/host.txt" 2>&1 || true
lscpu > "$OUT/host-lscpu.txt" 2>/dev/null || true

if [[ $CORDON == true ]]; then
  log "cordoning $BENCH_NODE"
  kubectl cordon "$BENCH_NODE" >/dev/null
fi
kubectl get pods -A --field-selector "spec.nodeName=$BENCH_NODE" -o wide > "$OUT/node-pods-before.txt"
log "non-benchmark pods on node: $(($(wc -l < "$OUT/node-pods-before.txt") - 1)) (see node-pods-before.txt)"

render deploy/probe-namespace.yaml | kubectl apply -f - >/dev/null
kubectl -n "$PROBE_NS" create configmap sampler-script --from-file=sampler/sampler.sh --from-file=sampler/perf.sh \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Pre-pull all images so no pull ever happens inside a measured trial. The prepull pod may
# exit or fail immediately (distroless images have no shell); we only care that imageID is set.
prepull() {
  local img=$1 name=prepull-$2 deadline reason
  kubectl -n "$PROBE_NS" delete pod "$name" --ignore-not-found --wait=true >/dev/null
  kubectl -n "$PROBE_NS" run "$name" --image="$img" --image-pull-policy=IfNotPresent --restart=Never \
    --overrides="{\"spec\":{\"nodeName\":\"$BENCH_NODE\"}}" >/dev/null
  deadline=$((SECONDS + 300))
  until kubectl -n "$PROBE_NS" get pod "$name" -o jsonpath='{.status.containerStatuses[0].imageID}' 2>/dev/null | grep -q .; do
    reason=$(kubectl -n "$PROBE_NS" get pod "$name" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)
    if [[ $reason == ErrImagePull || $reason == ImagePullBackOff || $reason == ErrImageNeverPull ]] || ((SECONDS > deadline)); then
      log "cannot pull $img on $BENCH_NODE ($reason)"; exit 1
    fi
    sleep 2
  done
  kubectl -n "$PROBE_NS" delete pod "$name" --wait=true >/dev/null 2>&1 || true
}
log "pre-pulling images"
prepull "$SAMPLER_IMAGE" sampler
prepull "$RS_IMAGE" rs
prepull "$GO_IMAGE" go
if [[ $ENABLE_PERF == true ]]; then prepull "$PERF_IMAGE" perf; fi

# ---------- one trial ----------
run_trial() {
  IMPL=$1
  local trial=$2 tdir
  case $IMPL in
    rs) IMAGE=$RS_IMAGE; TARGET_COMM=cmwatch-rs ;;
    go) IMAGE=$GO_IMAGE; TARGET_COMM=cmwatch-go ;;
    *) echo "unknown impl $IMPL" >&2; exit 1 ;;
  esac
  tdir=$OUT/$IMPL-$(printf '%02d' "$trial")
  mkdir -p "$tdir"
  SEQ=$((SEQ + 1))
  # Fresh watched namespace per trial: identical starting state (only kube-root-ca.crt) and a
  # name of identical length for every trial. The previous one is deleted without waiting, so
  # a namespace stuck in Terminating can never block the run.
  BENCH_NS=$BASE_NS-$(printf '%02d' "$SEQ")
  log "trial $IMPL #$trial in namespace $BENCH_NS"
  host_state > "$tdir/host.txt" 2>&1 || true

  kubectl delete ns "$BENCH_NS" --ignore-not-found --wait=false >/dev/null
  kubectl wait ns "$BENCH_NS" --for=delete --timeout=120s >/dev/null 2>&1 || true
  kubectl create ns "$BENCH_NS" >/dev/null
  render deploy/watcher-rbac.yaml | kubectl apply -f - >/dev/null
  until kubectl -n "$BENCH_NS" get sa cmwatch >/dev/null 2>&1; do sleep 1; done

  render deploy/sampler-pod.yaml | kubectl apply -f - >/dev/null
  kubectl -n "$PROBE_NS" wait pod/sampler --for=condition=Ready --timeout=120s >/dev/null
  set_phase startup

  render deploy/watcher-pod.yaml | kubectl apply -f - >/dev/null
  wait_synced
  set_phase warmup;    sleep "$WARMUP_S"
  set_phase idle_pre;  sleep "$IDLE_PRE_S"

  set_phase create
  local t0 t1
  t0=$(date +%s.%N)
  ./scripts/gen-configmaps.sh "$BENCH_NS" "$CM_COUNT" "$CM_SIZE_BYTES" | kubectl create -f - >/dev/null
  t1=$(date +%s.%N)
  local create_api_s; create_api_s=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.3f", b-a}')
  sleep "$SETTLE_S"

  set_phase delete
  t0=$(date +%s.%N)
  kubectl -n "$BENCH_NS" delete cm -l cmbench/churn=true --wait=true >/dev/null
  t1=$(date +%s.%N)
  local delete_api_s; delete_api_s=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.3f", b-a}')
  sleep "$SETTLE_S"

  set_phase post_near; sleep "$POST_NEAR_S"
  set_phase post_mid;  sleep "$POST_MID_S"
  set_phase post_late; sleep "$POST_LATE_S"
  set_phase done;      sleep 2

  # Collect from the sampler's disk emptyDir (not container logs, which rotate at 10 MiB).
  local f
  for f in samples.csv threads.csv snapshots.csv perf.csv perf.start; do
    kubectl -n "$PROBE_NS" exec sampler -c sampler -- cat "/data/$f" > "$tdir/$f" 2>/dev/null || rm -f "$tdir/$f"
  done
  # Container logs interleave stdout and stderr, so WATCH_ERROR lines land here too.
  kubectl -n "$BENCH_NS" logs cmwatch > "$tdir/watcher.log"
  kubectl -n "$BENCH_NS" get pod cmwatch -o yaml > "$tdir/watcher-pod.yaml"

  local applies deletes errors restarts image_id sync_ms valid=1
  applies=$(grep -c '^APPLY ' "$tdir/watcher.log" || true)
  deletes=$(grep -c '^DELETE ' "$tdir/watcher.log" || true)
  errors=$(grep -c 'WATCH_ERROR' "$tdir/watcher.log" || true)
  restarts=$(kubectl -n "$BENCH_NS" get pod cmwatch -o jsonpath='{.status.containerStatuses[0].restartCount}')
  image_id=$(kubectl -n "$BENCH_NS" get pod cmwatch -o jsonpath='{.status.containerStatuses[0].imageID}')
  sync_ms=$(grep -m1 '^SYNCED ' "$tdir/watcher.log" | sed -E 's/.*elapsed_ms=([0-9]+).*/\1/')
  [[ $applies -eq $CM_COUNT && $deletes -eq $CM_COUNT && $errors -eq 0 && $restarts -eq 0 ]] || valid=0
  # Both binaries must report the configured worker count and watch timeout.
  grep -m1 '^START ' "$tdir/watcher.log" | grep -q " workers=$WORKERS .*watch_timeout_s=$WATCH_TIMEOUT_S " || {
    log "  START line does not match WORKERS=$WORKERS WATCH_TIMEOUT_S=$WATCH_TIMEOUT_S"; valid=0; }
  grep -q '^# ERROR' "$tdir/samples.csv" && valid=0

  cat > "$tdir/meta.env" <<META
impl=$IMPL
trial=$trial
image=$IMAGE
image_id=$image_id
node=$BENCH_NODE
namespace=$BENCH_NS
workers=$WORKERS
watch_timeout_s=$WATCH_TIMEOUT_S
cm_count=$CM_COUNT
cm_size_bytes=$CM_SIZE_BYTES
applies=$applies
deletes=$deletes
watch_errors=$errors
restarts=$restarts
sync_ms=$sync_ms
rtm_snapshots=$(grep -c '^RTM ' "$tdir/watcher.log" || true)
start_line=$(grep -m1 '^START ' "$tdir/watcher.log")
create_api_s=$create_api_s
delete_api_s=$delete_api_s
valid=$valid
META
  log "  applies=$applies deletes=$deletes errors=$errors sync_ms=$sync_ms create_api_s=$create_api_s valid=$valid"

  kubectl -n "$BENCH_NS" delete pod cmwatch --wait=true >/dev/null
  kubectl -n "$PROBE_NS" delete pod sampler --wait=true >/dev/null
  wait_gone "$BENCH_NS" pod/cmwatch
  wait_gone "$PROBE_NS" pod/sampler
  kubectl delete ns "$BENCH_NS" --ignore-not-found --wait=false >/dev/null
  sleep "$COOLDOWN_S"
}

# ---------- counterbalanced schedule: A B B A A B ... ----------
SEQ=0
read -r -a impls <<< "$IMPLS"
for ((r = 1; r <= TRIALS; r++)); do
  if ((r % 2)); then order=("${impls[@]}"); else order=(); for ((i = ${#impls[@]} - 1; i >= 0; i--)); do order+=("${impls[i]}"); done; fi
  for impl in "${order[@]}"; do run_trial "$impl" "$r"; done
done

kubectl get pods -A --field-selector "spec.nodeName=$BENCH_NODE" -o wide > "$OUT/node-pods-after.txt"
log "done: $OUT"
log "analyze with: python3 scripts/analyze.py $OUT"
