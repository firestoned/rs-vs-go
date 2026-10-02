#!/usr/bin/env bash
# Node-local, fork-free sampler for one target process and its container cgroup (cgroup v2).
#
# Writes to $OUT_DIR (an emptyDir on disk, collected with kubectl exec, so containerd log
# rotation can never truncate a multi-hour run):
#   samples.csv    one row per tick: CPU, memory, faults, scheduler, network
#   threads.csv    per-thread scheduler counters and thread name every THREAD_EVERY ticks
#   snapshots.csv  ts,phase for every phase change; each change also sends SIGUSR1 to the
#                  target, which makes it print one RTM line of runtime/allocator internals
#
# The hot loop uses bash builtins only. Its cost lands in the sampler's cgroup, not the
# target's, and is identical for both implementations.
set -uo pipefail

TARGET_COMM=${TARGET_COMM:?TARGET_COMM is required}
SAMPLE_MS=${SAMPLE_MS:-100}
THREAD_EVERY=${THREAD_EVERY:-10}
CGROUP_ROOT=${CGROUP_ROOT:-/host/cgroup}
CGROUP_FILTER=${CGROUP_FILTER:-*kubepods*}
PHASE_FILE=${PHASE_FILE:-/run/bench/phase}
OUT_DIR=${OUT_DIR:-/data}
SNAPSHOTS=${SNAPSHOTS:-true}

printf -v interval '%d.%03d' $((SAMPLE_MS / 1000)) $((SAMPLE_MS % 1000))
exec {nap_fd}<> <(:)
nap() { read -r -t "$interval" -u "$nap_fd" _ || true; }

mkdir -p "$OUT_DIR"
S=$OUT_DIR/samples.csv T=$OUT_DIR/threads.csv P=$OUT_DIR/snapshots.csv
[[ -f $PHASE_FILE ]] || echo pre > "$PHASE_FILE"
if [[ ! -f $CGROUP_ROOT/cgroup.controllers ]]; then
  echo "# ERROR host cgroup v2 not found at $CGROUP_ROOT" | tee -a "$S"; exit 1
fi

# 1. Wait for the target process.
pid=""
while [[ -z $pid ]]; do
  for d in /proc/[0-9]*; do
    read -r c 2>/dev/null < "$d/comm" || continue
    if [[ $c == "$TARGET_COMM" ]]; then pid=${d#/proc/}; break; fi
  done
  [[ -z $pid ]] && nap
done

# 2. Find its cgroup in the HOST tree (/proc/<pid>/cgroup is relative to our own cgroup ns).
cg=""
while IFS= read -r f; do
  while read -r p; do
    if [[ $p == "$pid" ]]; then cg=${f%/cgroup.procs}; break 2; fi
  done < "$f"
done < <(find "$CGROUP_ROOT" -path "$CGROUP_FILTER" -name cgroup.procs 2>/dev/null)
if [[ -z $cg ]]; then echo "# ERROR cgroup for pid $pid not found" | tee -a "$S"; exit 1; fi

read -r kernel < /proc/sys/kernel/osrelease
gov=NA; read -r gov 2>/dev/null < /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
{
  echo "# target_comm=$TARGET_COMM pid=$pid cgroup=${cg#"$CGROUP_ROOT"} kernel=$kernel governor=$gov sample_ms=$SAMPLE_MS"
  echo "ts,phase,cpu_usage_usec,cpu_user_usec,cpu_system_usec,nr_throttled,throttled_usec,mem_current,mem_peak,anon,file,kernel_stack,slab,sock,percpu,pgfault,pgmajfault,rss_kb,hwm_kb,threads,vol_cs,nonvol_cs,sched_run_ns,sched_wait_ns,timeslices,net_rx_bytes,net_rx_packets,net_tx_bytes,net_tx_packets,load1"
} >> "$S"
echo "ts,tid,run_ns,wait_ns,timeslices,comm" >> "$T"
echo "ts,phase" >> "$P"
echo "# sampling pid=$pid cgroup=${cg#"$CGROUP_ROOT"}"

read -r last_phase 2>/dev/null < "$PHASE_FILE" || last_phase=unknown
tick=0

# 3. Sample until the target exits.
while [[ -d /proc/$pid ]]; do
  ts=$EPOCHREALTIME
  read -r phase 2>/dev/null < "$PHASE_FILE" || phase=unknown
  if [[ $phase != "$last_phase" ]]; then
    echo "$ts,$phase" >> "$P"
    [[ $SNAPSHOTS == true ]] && kill -USR1 "$pid" 2>/dev/null
    last_phase=$phase
  fi

  cu=0 uu=0 su=0 nt=0 tu=0
  while read -r k v; do
    case $k in
      usage_usec) cu=$v ;; user_usec) uu=$v ;; system_usec) su=$v ;;
      nr_throttled) nt=$v ;; throttled_usec) tu=$v ;;
    esac
  done 2>/dev/null < "$cg/cpu.stat" || break

  read -r mc 2>/dev/null < "$cg/memory.current" || break
  mp=NA; [[ -r $cg/memory.peak ]] && read -r mp < "$cg/memory.peak"

  anon=0 file=0 ks=0 slab=0 sock=0 pcpu=0 pgf=0 pgmaj=0
  while read -r k v; do
    case $k in
      anon) anon=$v ;; file) file=$v ;; kernel_stack) ks=$v ;; slab) slab=$v ;;
      sock) sock=$v ;; percpu) pcpu=$v ;; pgfault) pgf=$v ;; pgmajfault) pgmaj=$v ;;
    esac
  done 2>/dev/null < "$cg/memory.stat"

  rss=0 hwm=0 thr=0 vcs=0 nvcs=0
  while read -r k v _; do
    case $k in
      VmRSS:) rss=$v ;; VmHWM:) hwm=$v ;; Threads:) thr=$v ;;
      voluntary_ctxt_switches:) vcs=$v ;; nonvoluntary_ctxt_switches:) nvcs=$v ;;
    esac
  done 2>/dev/null < "/proc/$pid/status" || break

  # Scheduler counters summed over all threads: run time (ns), runqueue wait (ns), and the
  # number of times any thread was scheduled onto a CPU (a direct count of wakeups).
  run=0 wait=0 slices=0
  dump=$((tick % THREAD_EVERY == 0))
  for t in /proc/"$pid"/task/*; do
    read -r r w n 2>/dev/null < "$t/schedstat" || continue
    run=$((run + r)) wait=$((wait + w)) slices=$((slices + n))
    if ((dump)); then
      tc=?; read -r tc 2>/dev/null < "$t/comm"
      echo "$ts,${t##*/},$r,$w,$n,${tc//,/_}" >> "$T"
    fi
  done

  # Pod network namespace counters (excluding lo): separates API chatter from runtime work.
  rxb=0 rxp=0 txb=0 txp=0
  while IFS=: read -r ifc rest; do
    [[ -z $rest || $rest == *'|'* ]] && continue
    ifc=${ifc// /}
    [[ $ifc == lo ]] && continue
    # shellcheck disable=SC2086
    set -- $rest
    rxb=$((rxb + $1)) rxp=$((rxp + $2)) txb=$((txb + $9)) txp=$((txp + ${10}))
  done 2>/dev/null < "/proc/$pid/net/dev"

  read -r l1 _ < /proc/loadavg

  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$ts" "$phase" "$cu" "$uu" "$su" "$nt" "$tu" "$mc" "$mp" "$anon" "$file" "$ks" "$slab" \
    "$sock" "$pcpu" "$pgf" "$pgmaj" "$rss" "$hwm" "$thr" "$vcs" "$nvcs" "$run" "$wait" "$slices" \
    "$rxb" "$rxp" "$txb" "$txp" "$l1" >> "$S"
  tick=$((tick + 1))
  nap
done
echo "# target exited" | tee -a "$S"
while :; do nap; done
