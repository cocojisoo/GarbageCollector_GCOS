# Build GCOS's sched_ext scheduler + loader. Run on a Linux >= 6.12 host with
# the scx/libbpf toolchain (clang>=16, libbpf-dev, bpftool, sched-ext/scx headers).
# The CI `scx-ext` job runs exactly this and verifies the scheduler attaches
# (/sys/kernel/sched_ext/state == enabled, ops == gcos). Our dev box (colima 6.8)
# lacks sched_ext — use setup_vm.sh to boot a 6.12+ VM, then run.sh.
#
#   make            # vmlinux.h -> scx_gcos.bpf.o -> scx_gcos.skel.h -> scx_gcos
#   sudo ./scx_gcos # attach the scheduler (needs root + sched_ext)

BPF_SRC   := ../../gcos/osprims/ebpf/scx_gcos.bpf.c
ARCH      := $(shell uname -m | sed 's/x86_64/x86/;s/aarch64/arm64/')
SCX_INC   ?= /usr/include            # scx/common.bpf.h + scx/common.h (sched-ext/scx)
CLANG     ?= clang
BPFTOOL   ?= bpftool
# libbpf for the loader. The generated skeleton uses struct_ops auto-attach
# (bpf_map_skeleton.link), which needs libbpf >= 1.5 — newer than Ubuntu's. Point
# these at the libbpf built alongside the source bpftool. Default: system libbpf.
LIBBPF_INC ?=
LIBBPF_A   ?= -lbpf

CFLAGS    := -g -O2 -Wall
# -mcpu=v3 enables the 32-bit atomics the scx UEI machinery needs (the scx build
# uses v3+); without it clang errors "unsupported atomic operation".
BPF_CFLAGS := -g -O2 -target bpf -mcpu=v3 -D__TARGET_ARCH_$(ARCH) -I$(SCX_INC) -Ibuild

.PHONY: all clean
all: scx_gcos

build:
	mkdir -p build

# Kernel type info for CO-RE — dumped from the running 6.12+ kernel's BTF.
build/vmlinux.h: | build
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

build/scx_gcos.bpf.o: $(BPF_SRC) build/vmlinux.h
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

build/scx_gcos.skel.h: build/scx_gcos.bpf.o
	$(BPFTOOL) gen skeleton $< name scx_gcos > $@

scx_gcos: loader.c build/scx_gcos.skel.h
	$(CLANG) $(CFLAGS) $(if $(LIBBPF_INC),-I$(LIBBPF_INC)) -I$(SCX_INC) -Ibuild \
		loader.c -o $@ $(LIBBPF_A) -lelf -lz

clean:
	rm -rf build scx_gcos
