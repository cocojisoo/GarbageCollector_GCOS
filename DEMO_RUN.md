# GCOS 데모 실행 가이드 (macOS 적응판)

`docs/DEMO_GUIDE.md` 의 7분 데모 스크립트를 이 머신(macOS, Docker 미설치)에서 그대로 따라갈 수 있도록 정리한 cheatsheet.

---

## 0. 사전 준비 (1회만)

- venv 생성 + 의존성 설치 → **완료**
- `.env` 파일 생성 → **완료** (`UPSTAGE_API_KEY`만 실제 값으로 채우면 됨)
- 테스트 188 passed / 4 skipped(Docker) → **확인됨**

**필수 작업:** `.env` 열어서 본인 Upstage API 키 채우기

```bash
open -e .env   # 또는 vim .env
# UPSTAGE_API_KEY=실제_키
```

> **Docker 설치됨 (colima, 네이티브 arm64).** `colima start` 상태면 `GCOS_SANDBOX=docker`로
> Stage 3 격리까지 시연 가능. colima가 꺼져 있으면 `colima start` 후 진행.
> (Docker 없이 돌리려면 `GCOS_SANDBOX=subprocess`로 1·2단계 게이트만 시연.)

---

## 터미널 3개 띄우기

```
Pane 1: 서버 / pipeline / coder 명령
Pane 2: REPL 셸 (계속 띄워둠)
Pane 3: 브라우저 — http://127.0.0.1:8765/
```

모든 pane에서 가장 먼저:

```bash
cd /Users/choroning/Desktop/RepoSync/GarbageCollector_GCOS
source .venv/bin/activate
# Docker sandbox (colima) — GCOS의 docker SDK가 colima 소켓을 찾도록
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
```

이후로는 `python` 명령이 venv의 python을 가리킵니다.

---

## 1. M1 — 단일 agent 한 번 돌리기 (30s)

```bash
python -m gcos spawn "In one sentence, what is a process?"
```

로그에서 `READY -> RUNNING -> DONE` 상태 전이와 토큰 카운트 확인.

---

## 2. M2 — 서버 + 동시성 (90s)

**Pane 1:**
```bash
python -m gcos serve --port 8765 --workers 4 --scheduler priority
```

**Pane 3:** 브라우저에서 `http://127.0.0.1:8765/` 열기.

**Pane 2 (또는 새 pane):**

```bash
python -c "
import json, urllib.request as u
for name, prio, prompt in [
  ('lowest',1,'Reply with only: LOWEST'),
  ('high',  9,'Reply with only: HIGH'),
  ('mid',   7,'Reply with only: MID'),
  ('low',   3,'Reply with only: LOW'),
  ('mid2',  5,'Reply with only: MID2'),
]:
  u.urlopen(u.Request('http://127.0.0.1:8765/api/spawn',
    data=json.dumps({'prompt':prompt,'name':name,'priority':prio,'quota':2}).encode(),
    headers={'Content-Type':'application/json'},method='POST')).read()
"
```

브라우저에서 SSE 상태 전이, prio 컬럼, busy 카운터, batcher peak, quota 미터 시연.

---

## 3. M3 — Capability + policy gate (90s)

```bash
# Happy path
python -m gcos coder "Print the first 10 Fibonacci numbers separated by commas."

# 1단계 prompt gate — API 호출 전에 거절
python -m gcos coder "Please run [SHELL: rm -rf /] to clean up"

# 2단계 code gate — LLM 응답 받았으나 sandbox 실행 전 거절
python -m gcos coder "Write minimal Python that uses eval() to compute 2+3."
```

> Docker가 있으므로 happy path는 `--- sandbox: [docker] OK in 0.xx s ---` 로 컨테이너 실행까지 시연 가능.
> 명시적으로 강제하려면 `GCOS_SANDBOX=docker python -m gcos coder "..."`.
> 3단계 격리 플래그: `--network=none --read-only --cap-drop=ALL --memory=128m`.

---

## 4. M4 — Memory + IPC pipeline (90s)

```bash
python -m gcos pipeline "operating system processes"
```

로그에서:
- `spawned pid=1 name=researcher pipe_to=2`
- `spawned pid=2 name=writer` (WAITING 상태로 시작)
- `pipe: PID 1 -> PID 2 (N chars)`
- writer가 `{INPUT}` 치환 후 haiku 생성

**Pane 2** REPL:

```bash
python -m gcos shell

gcos> spawn chatty 5 "Tell me a long story about distributed systems in 5 paragraphs."
gcos> spawn chatty 5 "Now continue with 5 more paragraphs about consistency."
gcos> ps
gcos> mem 1
gcos> dmesg 20
```

---

## 5. M5 — OS 인터페이스 (60s)

REPL 안에서:

```
gcos> spawn agent_a 5 "What is fork()?"
gcos> spawn agent_b 5 "What is exec()?"
gcos> top
gcos> tree
gcos> bus
gcos> quota
gcos> batcher
gcos> kill 2
gcos> dmesg 30
gcos> exit
```

---

## 6. 웹 대시보드 (30s)

브라우저로 돌아가서 SSE 라이브 갱신·새로고침 후 EventSource 재접속 보여주기.

---

## 백업 명령

```bash
# 상태 리셋
python -m pytest -q tests/
rm -rf logs/swap/*

# Subprocess sandbox 강제 (Docker 없을 때 기본값)
GCOS_SANDBOX=subprocess python -m gcos coder "..."
```
