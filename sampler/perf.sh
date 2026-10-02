#!/usr/bin/env bash
# Optional hardware-counter collector (ENABLE_PERF=true). Counts actual CPU cycles and
# instructions for the target process with `perf stat`, in fixed intervals.
# Needs a privileged container and a kernel/hypervisor that exposes PMU counters
# (on VMs, enable virtual CPU performance counters or cycles will read <not supported>).
set -uo pipefail
if [[ ${ENABLE_PERF:-false} != true ]]; then
  echo "perf disabled"; while :; do sleep 3600; done
fi
TARGET_COMM=${TARGET_COMM:?}
OUT_DIR=${OUT_DIR:-/data}
PERF_INTERVAL_MS=${PERF_INTERVAL_MS:-1000}
# Debian ships either a versioned perf_X.Y plus a wrapper, or a single /usr/bin/perf.
perf_bin=$(ls /usr/bin/perf_* 2>/dev/null | head -1)
[[ -x $perf_bin ]] || perf_bin=$(command -v perf || true)
[[ -x $perf_bin ]] || { echo "no perf binary found"; exit 1; }

pid=""
while [[ -z $pid ]]; do
  for d in /proc/[0-9]*; do
    read -r c 2>/dev/null < "$d/comm" || continue
    [[ $c == "$TARGET_COMM" ]] && { pid=${d#/proc/}; break; }
  done
  [[ -z $pid ]] && sleep 0.05
done

date +%s.%N > "$OUT_DIR/perf.start"
"$perf_bin" stat -x, -I "$PERF_INTERVAL_MS" \
  -e task-clock,cycles,instructions,context-switches,cpu-migrations,page-faults \
  -p "$pid" -o "$OUT_DIR/perf.csv"
echo "perf exited"
while :; do sleep 3600; done
