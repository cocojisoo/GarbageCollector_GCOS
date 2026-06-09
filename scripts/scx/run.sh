#!/usr/bin/env bash
# run.sh — ON A Linux >= 6.12 HOST: build GCOS's sched_ext scheduler, load it so
# OUR code dispatches every task on the box, and run GCOS agents under it. This is
# the capability that sets GCOS apart — our own ring-0 CPU scheduler, actually
# running the machine.
#
# Verified on every push by the CI `scx-ext` job (sched_ext-capable runner). This
# script reproduces the same build+load+verify locally; on a dev box without
# sched_ext, boot a 6.12+ VM first (scripts/scx/setup_vm.sh). The preflight checks
# below refuse honestly and tell you exactly what's missing.
set -euo pipefail
cd "$(dirname "$0")"

# --- preflight: refuse honestly if the host can't run sched_ext ---------------
KVER=$(uname -r); KMAJ=${KVER%%.*}; KMIN=$(echo "$KVER" | cut -d. -f2)
if [ "$KMAJ" -lt 6 ] || { [ "$KMAJ" -eq 6 ] && [ "$KMIN" -lt 12 ]; }; then
  echo "REFUSING: sched_ext needs Linux >= 6.12; this host is $KVER."
  echo "Boot a 6.12+ VM first:  ./setup_vm.sh"
  exit 2
fi
[ -d /sys/kernel/sched_ext ] || { echo "REFUSING: CONFIG_SCHED_CLASS_EXT not enabled (/sys/kernel/sched_ext absent)"; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "REFUSING: loading a scheduler needs root (sudo $0)"; exit 2; }

echo ">> building scx_gcos + loader…"
make

echo ">> loading GCOS's scheduler into the kernel…"
./scx_gcos &           # attaches gcos_ops; backgrounds while we measure
LOADER=$!
sleep 2

echo ">> active sched_ext scheduler (state should be 'enabled', ops 'gcos'):"
cat /sys/kernel/sched_ext/state 2>/dev/null || true
find /sys/kernel/sched_ext -maxdepth 2 -name ops -exec sh -c 'echo "ops: $(cat "$1")"' _ {} \; 2>/dev/null || true
if ! grep -qi enabled /sys/kernel/sched_ext/state 2>/dev/null; then
  echo "scx_gcos did NOT attach"; kill "$LOADER" 2>/dev/null || true; exit 1
fi

echo ">> with OUR scheduler in control of every CPU, run GCOS agents under it:"
# Run the exact same check the CI scx-ext job runs: confirm the live executor
# dispatches and runs every GCOS agent as a real process while scx_gcos owns the
# CPU. (Per-priority CPU *share* is the cgroup CFS-share gate's job; scx_gcos
# doesn't read cgroup weight, so we don't assert a priority split here.)
( cd ../.. && python3 scripts/ci/verify_live_cfs.py )

echo ">> detaching scheduler…"
kill "$LOADER" 2>/dev/null || true
wait "$LOADER" 2>/dev/null || true
echo "done."
