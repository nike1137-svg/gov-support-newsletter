"""선별(예선·본선)의 안전장치가 동작하는지 확인한다. LLM 을 부르지 않는다 — 비용 0.

    .venv\\Scripts\\python.exe tools\\verify_select.py

1. 규칙 버림: 신청기간이 지난 공고(버림1), 중견·대기업만 대상(버림2)을 코드가 거르는가. 중소기업이 함께 있으면 거르지 않는가
2. ID·제목 대조: 빠진 ID, 없는 ID, 중복 ID, 한 칸 밀린 제목을 잡는가 — 2026-09-15 본선에서 실제로 번호가 밀렸다
3. 버림2 오판정 가드: 지원대상이 중소기업인데 LLM 이 버림2 를 붙이면 되돌려 보내는가 — 같은 날 9건 오판정이 있었다
4. LLM 실패 대체: 예선·본선 LLM 이 끝내 실패해도 멈추지 않고 대체 경로로 3~5건을 내는가
"""

import pathlib
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import graph  # noqa: E402

results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"[{'통과' if ok else '실패'}] {name} — {detail}")


def item(i, title, target="", period="", source="기업마당", hours_ago=1):
    return {"id": f"a{i:03d}", "title": title, "source": source, "summary": "",
            "url": f"https://x.kr/{i}", "at": (datetime.now(graph.KST) - timedelta(hours=hours_ago)).isoformat(),
            "meta": {"지원대상": target, "신청기간": period} if target or period else {}}


today = datetime.now(graph.KST).date()
past = f"{today - timedelta(days=20)} ~ {today - timedelta(days=1)}"
open_ = f"{today - timedelta(days=3)} ~ {today + timedelta(days=10)}"

# 1
cases = [
    (item(0, "마감 지난 공고", "중소기업", past), "버림1"),
    (item(1, "중견기업 전용 공고", "중견기업", open_), "버림2"),
    (item(2, "중견·중소 함께", "중견기업,중소기업", open_), None),
    (item(3, "신청 중 1인 창업 공고", "1인 창조기업", open_), None),
    (item(4, "필드 없는 뉴스", source="스타트업투데이"), None),
]
got = [(graph.rule_drop(it, today) or (None,))[0] for it, _ in cases]
want = [w for _, w in cases]
check("규칙 버림", got == want, f"기대 {want} · 결과 {got}")

# 2
items = [item(i, t) for i, t in enumerate(["가나다 지원사업 공고", "라마바 창업 모집", "사아자 정책 변화"])]
T = graph.Tag
ok_ans = [T(id="a000", title="가나다 지원사업 공고", label="기준1", reason="r"),
          T(id="a001", title="[기업마당] 라마바 창업 모집", label="기준1", reason="r"),
          T(id="a002", title="사아자 정책 변화", label="기준2", reason="r")]
shifted = [T(id="a000", title="라마바 창업 모집", label="기준1", reason="r"),
           T(id="a001", title="사아자 정책 변화", label="기준1", reason="r"),
           T(id="a002", title="가나다 지원사업 공고", label="기준2", reason="r")]
broken = [T(id="a000", title="가나다 지원사업 공고", label="기준1", reason="r"),
          T(id="a000", title="가나다 지원사업 공고", label="기준1", reason="r"),
          T(id="a999", title="없는 기사", label="기준1", reason="r")]
p_ok, p_shift, p_broken = (graph.id_problems(x, items) for x in (ok_ans, shifted, broken))
check("ID·제목 대조", p_ok is None and "맞지 않는" in (p_shift or "")
      and all(k in (p_broken or "") for k in ("목록에 없는", "빠진", "두 번")),
      f"정상 → {p_ok} · 밀림 → {bool(p_shift)} · 누락/중복/없는ID → {bool(p_broken)}")


# 3 — prelim 안의 covers_all 을 그대로 쓰기 위해 ask 를 가짜로 바꿔 check 결과만 받아 본다
captured = {}


def fake_ask_capture(schema, system, user, check=None):
    tags = [T(id="a000", title="가나다 지원사업 공고", label="버림2", reason="중소기업이라 1인 아님")]
    captured["problem"] = check(graph.Screen(tags=tags))
    raise graph.LLMError("시험용 — 검증 결과만 확인")


orig_ask = graph.ask
graph.ask = fake_ask_capture
graph.prelim({"collected": [item(0, "가나다 지원사업 공고", "중소기업", open_)], "log": [], "stats": {}})
check("버림2 오판정 가드", "버림2 가 될 수 없습니다" in (captured.get("problem") or ""), captured.get("problem"))


# 4
def always_fail(*a, **k):
    raise graph.LLMError("시험용 강제 실패")


graph.ask = always_fail
pool = [item(i, f"공고 {i} 창업 지원", "중소기업", open_, hours_ago=i) for i in range(8)]
pre = graph.prelim({"collected": pool, "log": [], "stats": {}})
fin = graph.final({"survivors": pre["survivors"], "log": [], "stats": {}})
graph.ask = orig_ask
n = len(fin["picked"])
check("LLM 실패 대체", pre["stats"]["prelim"]["fallback"] and fin["stats"]["final"]["fallback"]
      and graph.TARGET_MIN <= n <= graph.TARGET_MAX and all(t["reason"] for t in pre["screened"] + fin["screened"]),
      f"예선 대체 {pre['stats']['prelim']['fallback']} · 본선 대체 {fin['stats']['final']['fallback']} · {n}건 선택 · 이유 기록됨")

print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
