#!/bin/bash
# GCOS 평가 — OS 메커니즘 4종 정량 측정 (Upstage 키 불필요, 재현 가능)
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
echo "  동시성 speedup / 우선순위 dispatch / 게이트 탐지율 / 컨텍스트 페이징"
echo "  -> 네 가지 OS 주장 각각에 수치 근거."
echo "=================================================================="
echo
