#!/usr/bin/env bash
# setup_vm.sh — provision a Linux >= 6.12 (sched_ext) VM + the scx/libbpf
# toolchain so GCOS's own kernel scheduler (scx_gcos) can actually be loaded.
#
# WHY: our dev box (colima) runs Linux 6.8, which lacks sched_ext (needs 6.12+).
# This boots a SEPARATE throwaway VM (Ubuntu 25.04 = kernel 6.14, sched_ext on)
# via Lima — works on Apple Silicon (sched_ext is arch-independent). Loading a
# CPU scheduler is risky on a machine you care about, hence a throwaway VM.
#
# The same build+load is verified on every push by the CI `scx-ext` job (which
# runs on a sched_ext-capable hosted runner); this VM is the local reproduction
# for machines without a 6.12+ kernel. Then: limactl shell scx-gcos, cd to the
# repo, run scripts/scx/run.sh.
set -euo pipefail

VM=scx-gcos
IMG_URL="https://cloud-images.ubuntu.com/releases/25.04/release/ubuntu-25.04-server-cloudimg-arm64.img"

command -v limactl >/dev/null || { echo "Install Lima first: brew install lima"; exit 1; }

cat > /tmp/${VM}.yaml <<YAML
# Ubuntu 25.04 (kernel 6.14) — has CONFIG_SCHED_CLASS_EXT (sched_ext).
images:
  - location: "${IMG_URL}"
    arch: "aarch64"
mounts:
  - location: "${PWD}"          # mount the repo so run.sh sees scx_gcos.bpf.c
    writable: false
cpus: 4
memory: "4GiB"
provision:
  - mode: system
    script: |
      #!/bin/bash
      set -eux
      apt-get update
      # scx/libbpf toolchain: clang, libbpf, bpftool, headers, and the scx headers.
      apt-get install -y clang llvm libbpf-dev libelf-dev zlib1g-dev \
                         linux-tools-common linux-tools-generic make pkg-config git
      # sched-ext/scx provides scx/common.bpf.h + scx/common.h used by scx_gcos.
      if ! ls /usr/include/scx/common.bpf.h 2>/dev/null; then
        # v1.1.0 targets kernels <= 6.17 (newer scx 'main' needs 6.18+ BTF).
        git clone --depth 1 --branch v1.1.0 https://github.com/sched-ext/scx /opt/scx || true
        # headers live under scx/scheds/include — symlink onto the include path
        ln -sf /opt/scx/scheds/include/scx /usr/include/scx || true
      fi
YAML

echo ">> creating VM '${VM}' (Ubuntu 25.04 / kernel 6.14, sched_ext)…"
limactl start --name="${VM}" --tty=false /tmp/${VM}.yaml

echo ">> kernel + sched_ext check inside the VM:"
limactl shell "${VM}" -- bash -lc 'uname -r; ls -d /sys/kernel/sched_ext 2>/dev/null \
  && echo "sched_ext: PRESENT" || echo "sched_ext: ABSENT (kernel too old / not enabled)"'

cat <<EOF

VM ready. To build + load GCOS's scheduler:
    limactl shell ${VM}
    cd "${PWD##*/}"        # the mounted repo
    sudo ./scripts/scx/run.sh

Tear down when done:  limactl delete -f ${VM}
EOF
