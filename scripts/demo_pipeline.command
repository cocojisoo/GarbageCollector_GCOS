#!/bin/bash
# GCOS M4 데모 — producer/consumer 파이프라인 (IPC: pipe + message bus)
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
clear

cat <<'BANNER'
==================================================================
  GCOS — M4: Producer / Consumer Pipeline  (IPC)
  researcher --(pipe / message bus)--> writer
==================================================================

  PID 1 researcher : 주제에 대한 사실 3개 생성
  PID 2 writer     : WAITING 으로 시작 (input_from=1)
                     -> 버스로 들어온 {INPUT} 치환 후 haiku 생성

BANNER

echo "------------------------------------------------------------------"
echo "  \$ gcos pipeline \"operating system processes\""
echo "------------------------------------------------------------------"
python -m gcos pipeline "operating system processes" 2>&1 | grep -vE 'httpx: HTTP Request'

echo
echo "=================================================================="
echo "  핵심: 'pipe: PID 1 -> PID 2 (N chars)' = 버스가 실제 내용을 운반."
echo "        writer의 haiku가 upstream 사실을 반영 = end-to-end IPC 증명."
echo "=================================================================="
echo
