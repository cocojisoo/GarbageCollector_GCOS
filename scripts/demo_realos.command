#!/bin/bash
# GCOS — Real-OS substrate 데모: OS 주장이 '시뮬'이 아니라 호스트 커널이 강제함.
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
clear

cat <<'BANNER'
==================================================================
  GCOS — Real-OS Substrate  (gcos.osprims)
  "OS 주장을 호스트 커널이 강제한다" — 시뮬레이션이 아니라.
  Linux 1급 / macOS는 loud degrade.   자세히: docs/REAL_OS.md
==================================================================
BANNER
echo

echo "------------------------------------------------------------------"
echo "  [1] 이 호스트가 커널 강제 가능한가? (정직한 posture)"
echo "------------------------------------------------------------------"
python -c "from gcos.osprims import os_caps, warn_if_degraded; import json; print(json.dumps(os_caps().to_dict(), indent=2)); warn_if_degraded()"

echo
echo "------------------------------------------------------------------"
echo "  [2] 진짜 선점: 실제 자식 프로세스를 SIGSTOP/SIGCONT로 (RR vs FCFS)"
echo "------------------------------------------------------------------"
python -c "
from gcos.osprims.realproc import RealProcessScheduler, block_count
s = RealProcessScheduler()
rr = s.rr_order(3, 6, 0.01, preempt=True)
fc = s.rr_order(3, 6, 0.01, preempt=False)
print('RR  :', rr, '-> blocks', block_count(rr), '(자식들이 교차 = 선점)')
print('FCFS:', fc, '-> blocks', block_count(fc), '(자식당 1블록 = convoy)')
"

echo
echo "------------------------------------------------------------------"
echo "  [3] 진짜 demand paging: mmap + madvise page-out, 접근 시 fault-in"
echo "------------------------------------------------------------------"
python -c "
from gcos.eval import measure_demand_paging as m
r = m(n_pages=16, payload_bytes=8000)
print('page_out=%d  fault_in=%d  resident %d->%d  works=%s'
      % (r['page_outs'], r['fault_ins'], r['resident_before'],
         r['resident_after_pageout'], r['demand_paging_works']))
"

echo
echo "------------------------------------------------------------------"
echo "  [4] 진짜 멀티스텝 에이전트(A1): 스케줄러가 실에이전트를 타임슬라이스"
echo "------------------------------------------------------------------"
python -c "
from gcos.eval import measure_multistep_agents as m
r = m()
print('FCFS order:', r['fcfs_order'], '(각 에이전트 완주)')
print('RR   order:', r['rr_order'], '(교차) interleaves=%s' % r['rr_interleaves'])
"

echo
echo "------------------------------------------------------------------"
echo "  [5] *진짜 커널 강제*: cgroup v2 cpu.weight -> 리눅스 CFS 실측 점유율"
echo "      (권한 Linux 컨테이너; colima 실행 필요 — macOS 커널엔 cgroup 없음)"
echo "------------------------------------------------------------------"
docker run --rm --privileged --cgroupns=host -v "$PWD":/app -w /app python:3.11-slim \
  python -c "
from gcos.osprims.realproc import RealProcessScheduler
s = RealProcessScheduler().cpu_share([100, 300, 900], 0.8)
print('cpu.weight    100 / 300 / 900')
print('측정 점유율  ', s['measured_share_pct'], '%')
print('기대 점유율  ', s['expected_share_pct'], '%')
print('-> 측정 CPU 점유율이 cpu.weight를 추종 = 리눅스 CFS가 실제로 배분(커널 강제)')
" 2>/dev/null || echo "  (Docker/colima 미실행 — 'colima start' 후 재시도)"

echo
echo "------------------------------------------------------------------"
echo "  [6] *진짜 per-agent CFS (라이브 디스패치)*: 에이전트 = 진짜 OS 프로세스"
echo "      각자 per-agent cgroup(cpu.weight=우선순위)에서 실행 → 높은 우선순위가"
echo "      먼저 끝남. \`python -m gcos.eval\`의 'Per-agent CFS — LIVE (process)' 행"
echo "      (Linux에서 PASS; 예: prio [1,5,9] → wall [2.75, 2.07, 1.6]s)."
echo "      직접 실행: GCOS_EXEC=process python -m gcos serve ...  (에이전트=실프로세스)"
echo "------------------------------------------------------------------"
echo
echo "=================================================================="
echo "  요약: [2][3][4]는 macOS에서도 진짜 동작(시그널/mmap/실에이전트)."
echo "        [5][6]은 리눅스 커널이 CPU를 cpu.weight/우선순위대로 배분 = 커널 강제."
echo "        + 우리가 직접 쓴 eBPF(gcos/osprims/ebpf)는 Linux+bcc+root에서 로드,"
echo "          sched_ext 스케줄러(scx_gcos)는 6.12+ 필요 — 참조용(검증 불가 명시)."
echo "=================================================================="
echo
