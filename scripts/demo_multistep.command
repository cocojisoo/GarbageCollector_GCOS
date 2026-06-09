#!/bin/bash
# GCOS — 멀티스텝 ReAct 에이전트 데모 (A1): 한 에이전트가 여러 LLM 호출로 도구 사용.
# (라이브 — Upstage 키 필요. 오프라인/키 없이 보려면 ./demo_realos.command [4])
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
clear

cat <<'BANNER'
==================================================================
  GCOS — Multi-step ReAct Agent  (A1)
  단일샷이 아니라:  think -> TOOL: calc -> OBSERVATION -> ... -> FINAL
  스텝마다 LLM 1회 호출  ->  스케줄러 quantum이 '실에이전트'를 타임슬라이스
  $ gcos spawn --multi-step "..."
==================================================================
BANNER
echo

python -m gcos spawn --multi-step \
  "Compute (17*23 + 145) / 2 with the calc tool, one step at a time, then give the final number." \
  2>&1 | grep -vE 'httpx: HTTP Request'

echo
echo "=================================================================="
echo "  핵심: 출력의 calls=N (N>1) = 한 에이전트가 여러 스텝을 밟음."
echo "        단일샷(calls=1)과 달리 이 에이전트는 RR quantum에 실제로 선점됨."
echo "        (single-shot 대비: \$ gcos spawn \"What is a process?\" 는 calls=1)"
echo "=================================================================="
echo
