"""검수 단계의 코드 대조가 맞게 판정하는지 확인한다. LLM 을 부르지 않는다 — 비용 0.
실제 LLM 으로 틀린 요약을 걸러내는지는 tools/prove_verify.py 가 증명한다.

    .venv\\Scripts\\python.exe tools\\verify_verify.py

1. 인용 대조(in_body): 표 기호 | · 공백 차이는 같은 구절로, 글자가 다르면 다른 구절로 본다
2. 근거 구절(grounded): 날짜·금액 표기를 바꿔 옮긴 것은 인정, 없는 날짜·문장은 거부
3. 계산된 금액(facts_accounted): 30만 원 × 6개월 = 180만 원은 인정, 50만 원 · 없는 조건 숫자는 거부
4. 코드 불합격: 요약에 본문에 없는 날짜가 있으면 LLM 판정과 무관하게 불합격
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import graph  # noqa: E402
from prove_verify import BODY, GOOD, tamper  # noqa: E402

TABLE = "| 공고번호 | 제2026-556호 | 신청기간 | 2026-09-15 ~ 2026-10-06 | 담당부서 | 신산업기술창업과 |"
results = []


def group(name, cases):
    bad = [(n, got) for n, got, want in cases if got != want]
    results.append(not bad)
    print(f"[{'통과' if not bad else '실패'}] {name} — {len(cases) - len(bad)}/{len(cases)}" + (f" · 실패 {bad}" if bad else ""))


group("인용 대조", [
    ("표 기호 뺀 인용", graph.in_body("공고번호 제2026-556호 신청기간 2026-09-15", TABLE), True),
    ("공백만 다름", graph.in_body("사업장 임차료 월 30만원 한도", BODY), True),
    ("글자가 다름", graph.in_body("사업장 임차료 월 50만 원 한도", BODY), False),
    ("너무 짧음", graph.in_body("원", BODY), False),
])
group("근거 구절", [
    ("날짜 바꿔 옮김", graph.grounded("신청기간은 9월 8일부터 9월 18일까지", BODY), True),
    ("글자 그대로", graph.grounded("최대 6개월간 지원", BODY), True),
    ("없는 날짜", graph.grounded("9월 28일까지", BODY), False),
    ("없는 문장", graph.grounded("사업자등록 3년 이내 기업", BODY), False),
])
group("계산된 금액", [
    ("30만×6개월=180만", graph.facts_accounted("임차료를 최대 180만 원 줄일 수 있다", BODY), True),
    ("본문 금액 그대로", graph.facts_accounted("월 30만 원씩 최대 6개월", BODY), True),
    ("변조 50만", graph.facts_accounted("월 50만 원 한도로 지원", BODY), False),
    ("금액 없는 나이 주장", graph.facts_accounted("만 20~45세 청년", BODY), False),
    ("없는 숫자 3년 섞임", graph.facts_accounted("업력 3년 이내, 월 30만 원", BODY), False),
])
fails_good, _ = graph.code_check(GOOD, BODY)
fails_date, _ = graph.code_check(tamper(summary=GOOD["summary"].replace("9월 18일까지", "9월 28일까지")), BODY)
group("코드 불합격", [
    ("정상은 코드 통과", fails_good, []),
    ("없는 날짜는 불합격", any("9월 28일" in f for f in fails_date), True),
])

print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
