#!/usr/bin/env bash
# Pin or restore the CPU frequency policy of the hypervisor that runs BENCH_NODE's VM.
# Run on that host; needs sudo.
#
#   pin      save the current policy, then set the performance governor and disable turbo, so
#            every core runs at its fixed base clock and CPU time is proportional to cycles
#   restore  put back the policy saved by pin
#   show     print the current policy
set -euo pipefail
state=${CPUFREQ_STATE:-$HOME/.cmbench-cpufreq.saved}
c=/sys/devices/system/cpu

show() {
  echo "governor=$(cat $c/cpu0/cpufreq/scaling_governor 2>/dev/null || echo NA)"
  echo "no_turbo=$(cat $c/intel_pstate/no_turbo 2>/dev/null || echo NA)"
  echo "boost=$(cat $c/cpufreq/boost 2>/dev/null || echo NA)"
}
set_all() { # governor no_turbo boost
  echo "$1" | sudo tee $c/cpu*/cpufreq/scaling_governor >/dev/null
  if [[ -e $c/intel_pstate/no_turbo && $2 != NA ]]; then echo "$2" | sudo tee $c/intel_pstate/no_turbo >/dev/null; fi
  if [[ -e $c/cpufreq/boost && $3 != NA ]]; then echo "$3" | sudo tee $c/cpufreq/boost >/dev/null; fi
}

case ${1:-} in
  pin)
    [[ -f $state ]] || show > "$state"   # keep the original if pin runs twice
    set_all performance 1 0
    show ;;
  restore)
    [[ -f $state ]] || { echo "nothing saved at $state" >&2; exit 1; }
    # shellcheck disable=SC1090
    source "$state"
    set_all "$governor" "$no_turbo" "$boost"
    rm -f "$state"
    show ;;
  show) show ;;
  *) echo "usage: $0 pin|restore|show" >&2; exit 2 ;;
esac
