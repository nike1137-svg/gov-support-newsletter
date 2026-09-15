"""파이프라인 실행 스크립트.

    .venv\\Scripts\\python.exe run.py              # 직전 24시간
    .venv\\Scripts\\python.exe run.py --hours 72   # 창을 바꿔 시험

끝나면 store/metrics.jsonl 에 한 줄, store/runs/ 에 이번 실행의 기사·라벨·선택 결과를 남긴다.
"""

import argparse
import json
import pathlib
from datetime import datetime, timedelta, timezone

from graph import HOURS, MODEL, run

ROOT = pathlib.Path(__file__).resolve().parent
METRICS = ROOT / "store" / "metrics.jsonl"
RUNS = ROOT / "store" / "runs"
KST = timezone(timedelta(hours=9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=HOURS, help="수집 시간 창 (기본 24)")
    args = ap.parse_args()

    started = datetime.now(KST)
    out = run(args.hours)
    for line in out["log"]:
        print(line)

    print("\n오늘 고른 기사")
    for p in out["picked"]:
        print(f"  {p['rank']}. [{p['label']}] {p['title'][:50]} ({p['source']})")
        print(f"     └ {p['why_pick']}")

    run_id = started.strftime("%Y-%m-%d_%H%M")
    RUNS.mkdir(parents=True, exist_ok=True)
    run_file = RUNS / f"{run_id}.json"
    run_file.write_text(json.dumps({"run_id": run_id, "model": MODEL, "collected": out["collected"],
                                    "screened": out["screened"], "picked": out["picked"]},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    row = {"run_id": run_id, "model": MODEL, "stats": out["stats"], "log": out["log"]}
    with METRICS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n기록: {METRICS.relative_to(ROOT)} · {run_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
