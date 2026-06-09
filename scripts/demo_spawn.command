#!/bin/bash
# GCOS M1 데모 — 단일 에이전트 라이프사이클 (PCB · 상태 전이 · 토큰 회계)
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
clear

cat <<'BANNER'
==================================================================
  GCOS — M1: 단일 에이전트 end-to-end  (LLM 에이전트 = OS 프로세스)
  PID 부여 -> READY -> RUNNING -> DONE  +  토큰/벽시계 회계 (PCB)
  $ gcos spawn "In one sentence, what is a process?"
==================================================================
BANNER
echo

python -m gcos spawn "In one sentence, what is a process?" 2>&1 | grep -vE 'httpx: HTTP Request'

echo
echo "=================================================================="
echo "  step=1 = RUNNING / DONE = 종료.  state=DONE tokens=N wall=Xs 가 PCB 회계."
echo "=================================================================="
echo
