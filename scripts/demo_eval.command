#!/bin/bash
# GCOS 평가 — OS 메커니즘 정량 측정 (Upstage 키 불필요, 재현 가능)
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"
clear

cat <<'BANNER'
==================================================================
  GCOS — Evaluation  (offline, 키 불필요, 재현 가능)
  LLM 품질이 아니라 'OS 메커니즘'을 정량 측정
  $ python -m gcos.eval
==================================================================
BANNER
echo

python -m gcos.eval 2>/dev/null

echo
echo "=================================================================="
echo "  상단(유저스페이스 오케스트레이션): speedup / 우선순위 / 무중복(A1) /"
echo "       선점(C8) / 쿼터(A3) / 게이트 / 컨텍스트 페이징"
echo "  하단('진짜 커널' 기질, gcos.osprims): 멀티스텝 에이전트(A1) / 실프로세스"
echo "       선점 / mmap demand paging / cgroup CFS share"
echo "  * macOS에선 cgroup 행이 DEGRADED — 커널 강제는 Linux/CI에서."
echo "    리눅스 커널 강제 라이브 데모: ./demo_realos.command"
echo "=================================================================="
echo
