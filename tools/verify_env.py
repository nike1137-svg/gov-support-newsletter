"""필수 키가 비었을 때 시작 전에 멈추는지 확인한다. 파이프라인은 돌지 않는다 — LLM 호출 · 발행 없음.

    .venv\\Scripts\\python.exe tools\\verify_env.py

키가 비어도 파이프라인이 대체 경로로 끝까지 돌아 "정상 실행됐습니다"를 알린 문제를 막는 가드를 본다 (클론 점검에서 발견).

시험 방법 — 키를 공백 한 칸으로 둔다. 변수가 '존재'해야 load_dotenv 가 .env 의 진짜 값으로 덮어쓰지 않는다.
(PowerShell 에서 $env:X = '' 는 비우는 게 아니라 지우는 것이라 .env 의 진짜 키가 읽힌다 — 실제로 그렇게 시험을 잘못 해 파이프라인이 돈 적이 있다)
전송 시험은 토큰이 비어 있어 가드가 실패해도 보낼 수 없다. 실행마다 60초 제한.

1. OpenAI 키가 비면 --dry-run 도 시작 전에 멈춘다 (종료 코드 1, 키 이름 안내)
2. 텔레그램 키가 비면 --send 는 시작 전에 멈춘다
3. 텔레그램 키가 비어도 dry-run 에는 요구하지 않는다
4. 기업마당 키가 비면 수집 검증이 '통과'가 아니라 '검사하지 못함'으로 실패한다 (전에는 라벨 0/0 으로 통과했다)
"""

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
BLANK = " "
results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"[{'통과' if ok else '실패'}] {name} — {detail}")


def run(args, blank):
    env = {**os.environ, "PYTHONUTF8": "1", **{k: BLANK for k in blank}}
    try:
        p = subprocess.run([PY, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return "timeout", "60초 안에 멈추지 않았다"


code, out = run(["run.py", "--dry-run"], ["OPENAI_API_KEY"])
check("OpenAI 키 없음 · dry-run", code == 1 and "OPENAI_API_KEY" in out and "⊙" not in out,
      f"종료 코드 {code} · {out.strip().splitlines()[0] if out.strip() else ''}")

code, out = run(["run.py", "--send"], ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
check("텔레그램 키 없음 · send", code == 1 and "TELEGRAM_BOT_TOKEN" in out and "TELEGRAM_CHAT_ID" in out and "⊙" not in out,
      f"종료 코드 {code} · {out.strip().splitlines()[0] if out.strip() else ''}")

env = {**os.environ, "TELEGRAM_BOT_TOKEN": BLANK, "TELEGRAM_CHAT_ID": BLANK}
p = subprocess.run([PY, "-c", "import graph, run; print(run.missing_env(sending=False), run.missing_env(sending=True))"],
                   cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
got = p.stdout.strip()
check("텔레그램 키 없음 · dry-run 은 막지 않음", got == "[] ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']", f"dry-run / send → {got}")

code, out = run(["tools/verify_collect.py"], ["BIZINFO_API_KEY"])
check("기업마당 키 없음 · 수집 검증", code == 1 and "검사하지 못함" in out,
      f"종료 코드 {code} · {next((l for l in out.splitlines() if '라벨·주소' in l), '')[:70]}")

print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
