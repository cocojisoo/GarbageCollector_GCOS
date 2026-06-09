#!/bin/bash
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
# Docker sandbox (colima) — native arm64 daemon socket for the docker SDK
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
clear
cat <<'BANNER'
==========================================
  GCOS REPL — 직접 명령 입력해보세요
==========================================

예시:
  spawn a 9 What is fork()?
  spawn b 3 Tell me a long story in 5 paragraphs about distributed systems.
  ps
  top              # Ctrl-C로 나오기
  tree
  bus
  quota
  batcher
  mem 1            # PID 1의 context pager 통계
  dmesg 20
  kill 2
  help
  exit
BANNER
echo ""
python -m gcos shell
