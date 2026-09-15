"""파이프라인 실행 스크립트.

    .venv\\Scripts\\python.exe run.py              # .env 의 DRY_RUN 을 따른다 (없으면 보내지 않음)
    .venv\\Scripts\\python.exe run.py --dry-run    # 보내지 않고 메시지만 출력
    .venv\\Scripts\\python.exe run.py --send       # 텔레그램으로 실제 발행
    .venv\\Scripts\\python.exe run.py --hours 72   # 수집 창을 바꿔 시험

끝나면 store/metrics.jsonl 에 한 줄, store/runs/ 에 이번 실행의 기사·라벨·검수 기록을 남긴다.
실제 발행에서 전달에 실패하면 종료 코드 1 — GitHub Actions 가 실패로 표시해 알림이 온다.
"""

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
METRICS = ROOT / "store" / "metrics.jsonl"
RUNS = ROOT / "store" / "runs"
KST = timezone(timedelta(hours=9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None, help="수집 시간 창 (기본 24)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="보내지 않는다")
    mode.add_argument("--send", action="store_true", help="텔레그램으로 보낸다")
    args = ap.parse_args()

    from graph import HOURS, MODEL, run         # .env 를 읽은 뒤 명령행 선택으로 덮어쓴다
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
    if args.send:
        os.environ["DRY_RUN"] = "0"

    started = datetime.now(KST)
    out = run(args.hours or HOURS)
    for line in out["log"]:
        print(line)

    pub = out["stats"].get("publish", {})
    if pub.get("dry_run"):
        print("\n── 보낼 메시지 (dry-run) ──")
        for m in out["messages"]:
            print(m, "\n", "─" * 40)

    run_id = started.strftime("%Y-%m-%d_%H%M")
    RUNS.mkdir(parents=True, exist_ok=True)
    run_file = RUNS / f"{run_id}.json"
    # 본문은 저장소에 올리지 않는다 (기사 저작권). 길이와 출처, 인용한 근거 문장만 남긴다
    reviewed = [{k: v for k, v in d.items() if k != "body"} for d in (out["reviewed"] or out["drafted"])]
    run_file.write_text(json.dumps({"run_id": run_id, "model": MODEL, "collected": out["collected"],
                                    "screened": out["screened"], "picked": out["picked"], "reviewed": reviewed,
                                    "messages": out["messages"]},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    row = {"run_id": run_id, "model": MODEL, "stats": out["stats"], "log": out["log"]}
    with METRICS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n기록: {METRICS.relative_to(ROOT)} · {run_file.relative_to(ROOT)}")

    if not pub.get("dry_run") and pub.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
