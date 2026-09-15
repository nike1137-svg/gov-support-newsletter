"""노트북 작업 스케줄러가 매일 부르는 진입점. run.py 를 돌리고 뒤처리까지 한다.

    .venv\\Scripts\\python.exe tools\\daily.py

GitHub Actions 예약을 쓰지 않는 이유 — 해외 실행 서버에서 bizinfo.go.kr · mss.go.kr 에 연결이 안 된다
(2026-09-15 17시 · 09-16 08시 ConnectTimeout, 같은 시각 한국 노트북에서는 0.6초에 응답).

하는 일
1. run.py --send 실행 (최대 20분)
2. store/daily-run.log 에 시각 · 종료 코드 · 마지막 로그를 남긴다 (이 로그는 커밋하지 않는다)
3. 실패하면 텔레그램으로 한 줄 알린다 — 조용히 실패하면 '오늘은 공고가 없음'과 구분되지 않는다
4. store/ 에 생긴 기록을 커밋 · push 한다 (수동 실행분과 겹치지 않게 pull --rebase 후)
"""

import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "store" / "daily-run.log"
KST = timezone(timedelta(hours=9))
TIMEOUT_SEC = 20 * 60


def log(line):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now(KST):%Y-%m-%d %H:%M} {line}\n")
    print(line)


def notify(text):
    """실패를 알린다. 알림 자체가 실패해도 이 스크립트는 계속 간다"""
    load_dotenv(ROOT / ".env")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("알림 못 보냄 — 텔레그램 값 없음")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=20)
        log(f"알림 전송 {'성공' if r.ok else '실패 ' + str(r.status_code)}")
    except requests.RequestException as ex:
        log(f"알림 전송 실패 {type(ex).__name__}")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def push_records():
    if not git("status", "--porcelain", "store").stdout.strip():
        log("올릴 기록 없음")
        return
    git("add", "store")
    git("-c", "user.name=brief-bot", "-c", "user.email=brief-bot@users.noreply.github.com",
        "commit", "-m", f"run: {datetime.now(KST):%Y-%m-%d_%H%M} KST (노트북)")
    pull = git("pull", "--rebase")
    push = git("push")
    log(f"기록 push {'성공' if push.returncode == 0 else '실패 ' + (push.stderr or pull.stderr).strip()[:120]}")


def main():
    started = datetime.now(KST)
    try:
        p = subprocess.run([str(PY), "run.py", "--send"], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", timeout=TIMEOUT_SEC)
        out, code = (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired:
        out, code = "", "timeout"

    tail = [x for x in out.splitlines() if x.strip()][-6:]
    log(f"실행 종료 코드 {code} · {(datetime.now(KST) - started).seconds}초")
    for line in tail:
        log("  " + line)

    if code != 0:
        notify("⚠️ 1인 창업 지원 뉴스 브리핑 실행 실패\n"
               f"{started:%Y-%m-%d %H:%M} · 종료 코드 {code}\n"
               + ("\n".join(tail[-3:]) if tail else "출력 없음"))
        push_records()
        sys.exit(1)

    # 성공했더라도 주력 소스가 전부 죽었으면 알린다 — '오늘은 공고가 없음'과 구분되지 않기 때문
    dead_line = next((x for x in tail if "응답 없음" in x), "")
    if dead_line:
        notify("⚠️ 수집 소스 일부가 응답하지 않았습니다\n" + dead_line.strip())

    push_records()


if __name__ == "__main__":
    main()
