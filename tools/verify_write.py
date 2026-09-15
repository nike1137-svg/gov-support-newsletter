"""요약·인사이트 단계의 안전장치가 동작하는지 확인한다. LLM 을 부르지 않는다 — 비용 0.

    .venv\\Scripts\\python.exe tools\\verify_write.py

1. 기업마당 본문 추출: 페이지 앞쪽 다른 s_title 부터 메뉴·스크립트까지 삼키지 않는가 (첫 구현에서 6000자가 잡힘)
2. draft_ok 가 2026-09-15 실제로 나왔던 불량 출력을 모두 되돌려 보내는가
   - ~했다 · ~함 문체 / 빈말 조건 / 얻는 것 칸에 대상 이야기 / 본문에 없는 인용 / 추측 판정 / 5인 이상인데 대상
3. 본문도 요약도 없으면 LLM 을 부르지 않고 스킵하는가
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import graph  # noqa: E402

results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"[{'통과' if ok else '실패'}] {name} — {detail}")


# 1 — 실제 페이지 구조를 줄여 옮긴 것: 앞에 본문이 아닌 s_title 이 있고, 메뉴와 스크립트가 섞여 있다
page = """
<span class="s_title">화면크기</span><ul><li>가 작게</li></ul><script>var size = '';</script>
<nav>정책정보 지원사업 공고 로그인</nav>
<ul class="view_cont">
  <li><span class="s_title">소관부처·지자체</span><div class="txt"> 부산광역시 </div></li>
  <li><span class="s_title">신청기간</span><div class="txt"> 2026.09.08 ~ 2026.09.18 </div></li>
  <li><span class="s_title">사업개요</span><div class="txt"><p>청년 창업자 임차료 지원</p><p>☞ 월 30만 원 한도</p></div></li>
  <li><span class="s_title">첨부</span><div class="txt"> </div></li>
</ul>
"""
body = graph.bizinfo_body(page)
lines = body.splitlines()
check("기업마당 본문 추출", lines == ["소관부처·지자체: 부산광역시", "신청기간: 2026.09.08 ~ 2026.09.18",
                                    "사업개요: 청년 창업자 임차료 지원 ☞ 월 30만 원 한도"]
      and "var size" not in body and "로그인" not in body, f"{len(lines)}줄 · {body[:60]!r}")

# 2
D, I = graph.Draft, graph.Insight
b_ok = "신청기간: 2026.09.08 ~ 2026.09.18 / 사업개요: 18세 이상 39세 이하인 청년 창업자 ☞ 사업장 임차료 월 30만 원 한도"
b_5 = "상시근로자 5인 이상 1,000인 미만 기업이 대상이며, 기업당 최대 400만원"


def mk(**kw):
    ins = dict(action="자격이 맞으면 이메일로 신청하세요.", check="만 18~39세, 서구 주민등록 1년 이상이어야 합니다.",
               gain="월 30만 원씩 최대 6개월, 임차료를 줄일 수 있습니다.")
    ins.update(kw.pop("ins", {}))
    base = dict(fit="대상", fit_quote="18세 이상 39세 이하인 청년 창업자", fit_reason="청년 창업자가 대상입니다.",
                headline="부산 서구 청년 창업자 임차료 지원", summary="부산 서구가 임차료를 지원합니다. 신청은 9월 18일까지입니다.",
                insight=I(**ins), evidence=["18세 이상 39세 이하인 청년 창업자", "신청기간: 2026.09.08 ~ 2026.09.18"])
    base.update(kw)
    return D(**base)


cases = [
    ("정상", mk(), b_ok, None),
    ("~했다체", mk(summary="부산 서구는 임차료 지원사업을 추진한다. 월 30만 원을 지원한다."), b_ok, "합니다체"),
    ("~함 체", mk(summary="부산 서구에서 임차료를 지원함. 최대 6개월간 지원함."), b_ok, "합니다체"),
    ("빈말 조건", mk(ins={"check": "본문에 조건이 명확히 제시되어 있으므로 조건을 꼼꼼히 확인해야 합니다."}), b_ok, "빈말"),
    ("얻는 것에 대상 이야기", mk(ins={"gain": "1인 창조기업도 창업기업에 포함되므로 지원대상에 해당합니다."}), b_ok, "대상 여부"),
    ("본문에 없는 인용", mk(fit_quote="1인 창조기업 우대"), b_ok, "본문에 없습니다"),
    ("추측 판정", mk(fit="대상아님", fit_quote="청년 창업자", fit_reason="규모가 큰 사업임을 유추할 수 있습니다."), b_ok, "추측"),
    ("5인 이상인데 대상", mk(fit_quote="상시근로자 5인 이상"), b_5, "5인 이상"),
    ("5인 이상 · 대상아님", mk(fit="대상아님", fit_quote="상시근로자 5인 이상", fit_reason="5인 이상 고용 조건입니다."), b_5, None),
]
bad = []
for name, d, b, want in cases:
    got = graph.draft_ok(d, b)
    if not ((got is None) if want is None else (got is not None and want in got)):
        bad.append((name, got))
check("불량 출력 되돌림", not bad, f"{len(cases) - len(bad)}/{len(cases)}" + (f" · 실패 {bad}" if bad else ""))

# 3
orig_extract, orig_ask = graph.extract_body, graph.ask
graph.extract_body = lambda it: ("", "없음")
called = []
graph.ask = lambda *a, **k: called.append(1)
out = graph.write({"item": {"id": "a001", "rank": 1, "label": "기준1", "source": "x", "title": "t", "url": "u",
                            "meta": {}, "why_pick": "w", "summary": ""}})
graph.extract_body, graph.ask = orig_extract, orig_ask
d = out["drafted"][0]
check("본문 없음 스킵", d["status"] == "skip" and not called, f"status={d['status']} · LLM 호출 {len(called)}회 · {d['skip_reason']}")

print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
