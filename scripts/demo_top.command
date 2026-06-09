#!/bin/bash
# GCOS M5 데모 — OS 인터페이스 REPL: 에이전트 spawn 후 live `top` (htop처럼)
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
clear

cat <<'BANNER'
==================================================================
  GCOS — M5: OS 인터페이스 REPL  (ps / top / tree / kill)
  6개 에이전트를 우선순위 섞어 spawn -> live `top` (Ctrl-C 로 종료)
==================================================================
BANNER
sleep 1

# 시작 명령을 파이프로 주입: 6개 spawn 후 곧장 top 진입.
# top 은 while-True 루프라 stdin EOF 이후에도 라이브로 머무름 -> 캡처 후 Ctrl-C.
printf '%s\n' \
  'spawn crit_sched 9 Write 8 detailed paragraphs about CPU scheduling algorithms in operating systems.' \
  'spawn high_mem 7 Write 8 detailed paragraphs about virtual memory and demand paging.' \
  'spawn high_ipc 7 Write 8 detailed paragraphs about inter-process communication mechanisms.' \
  'spawn mid_fs 5 Write 8 detailed paragraphs about how file systems index and store data.' \
  'spawn low_boot 3 Write 8 detailed paragraphs about the operating system boot sequence.' \
  'spawn idle_log 1 Write 8 detailed paragraphs about kernel logging and tracing subsystems.' \
  'top' \
  | python -m gcos shell 2>/dev/null
