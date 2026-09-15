"""검수가 틀린 요약을 실제로 걸러내는지 증명한다. 실제 LLM 을 부른다 (약 10회 · $0.01 안팎).

    .venv\\Scripts\\python.exe tools\\prove_verify.py

결과는 store/verify_proof.json 에 남는다.

왜 필요한가 — 이전 프로젝트(newsletter-agent)는 검수 불합격이 여섯 번 연속 0건이었다.
그게 "요약이 정확해서"인지 "검수가 도장만 찍어서"인지 가를 방법이 없었다.
평소에 아무것도 걸러내지 않는 장치는 고장 나도 티가 나지 않으므로, 틀린 입력을 일부러 넣어 본다.

본문은 2026-09-15 기업마당 공고(부산 서구 청년 창업자 임차료 지원사업)를 bizinfo_body 로 뽑은 형태를 옮겼다.
"""

import copy
import json
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import graph  # noqa: E402

BODY = """소관부처·지자체: 부산광역시
사업수행기관: 기초자치단체
신청기간: 2026.09.08 ~ 2026.09.18
사업개요: 우리 구에서는 청년 창업자의 경제적 부담 완화 및 안정적인 창업 환경을 조성하고자 다음과 같이「2026년 서구 청년 창업자 임차료 지원사업」을 추진하니 많은 신청 바랍니다. ☞ 공고일 기준 사업장 월 임차료를 납부 중이고 1년 이상 서구에 주민등록을 둔 18세 이상 39세 이하인 청년 창업자 ※ 자세한 신청자격 공고문 참조 ☞ 사업장 임차료 월 30만 원 한도, 최대 6개월간 지원
사업신청 방법: 이메일 접수"""

GOOD = {
    "id": "p001", "rank": 1, "label": "기준1", "source": "기업마당", "status": "ok",
    "title": "[부산] 서구 2026년 청년 창업자 임차료 지원사업 참여 신청 재공고", "url": "https://www.bizinfo.go.kr/",
    "meta": {}, "why_pick": "", "body_source": "본문", "body_len": len(BODY), "body": BODY,
    "fit": "대상", "fit_quote": "18세 이상 39세 이하인 청년 창업자", "fit_reason": "청년 창업자가 지원대상입니다.",
    "headline": "부산 서구 청년 창업자 임차료 지원",
    "summary": "부산 서구가 청년 창업자의 임차료를 지원합니다. 신청기간은 9월 8일부터 9월 18일까지이며 이메일로 접수합니다. "
               "사업장 임차료를 월 30만 원 한도로 최대 6개월간 지원합니다.",
    "insight": {"action": "자격에 맞으면 9월 18일까지 이메일로 신청하세요.",
                "check": "공고일 기준 사업장 임차료를 내고 있고, 서구에 1년 이상 주민등록을 둔 만 18~39세 청년 창업자여야 합니다.",
                "gain": "월 30만 원씩 최대 6개월, 임차료를 최대 180만 원 줄일 수 있습니다."},
    "evidence": ["사업장 임차료 월 30만 원 한도, 최대 6개월간 지원", "신청기간: 2026.09.08 ~ 2026.09.18"],
}


def tamper(**changes):
    d = copy.deepcopy(GOOD)
    for path, value in changes.items():
        if path.startswith("insight_"):
            d["insight"][path[8:]] = value
        else:
            d[path] = value
    return d


CASES = [
    # 이름, 작성물, 기대 합격 여부, 무엇을 틀리게 했나
    ("A 정상", GOOD, True, "변조 없음"),
    ("B 금액 변조", tamper(summary=GOOD["summary"].replace("월 30만 원", "월 50만 원")), False, "30만 → 50만"),
    ("C 마감일 변조", tamper(summary=GOOD["summary"].replace("9월 18일까지", "9월 28일까지"),
                        insight_action="자격에 맞으면 9월 28일까지 이메일로 신청하세요."), False, "9/18 → 9/28"),
    ("D 없는 조건 추가", tamper(insight_check=GOOD["insight"]["check"] + " 사업자등록 후 3년 이내인 기업만 신청할 수 있습니다."),
     False, "본문에 없는 '업력 3년 이내' 조건"),
    ("E 나이 조건 변조", tamper(insight_check=GOOD["insight"]["check"].replace("만 18~39세", "만 20~45세")),
     False, "18~39세 → 20~45세"),
    ("F 계산된 금액 (오탐 확인)", GOOD, True, "월 30만 원 × 6개월 = 180만 원 — 본문에 그대로는 없지만 맞는 값"),
]


def main():
    started = datetime.now(graph.KST)
    rows, ok = [], 0
    for name, d, want, what in CASES:
        try:
            passed, rec, usage = graph.judge(d, BODY)
        except graph.LLMError as ex:
            passed, rec, usage = None, {"error": str(ex)}, {}
        hit = passed == want
        ok += hit
        rows.append({"case": name, "tampered": what, "expected_pass": want, "actual_pass": passed,
                     "as_expected": hit, "record": rec, "usage": usage})
        print(f"[{'맞음' if hit else '틀림'}] {name:<18} 기대 {'통과' if want else '불합격'} · 결과 "
              f"{'통과' if passed else '불합격' if passed is False else '오류'}"
              + (f" · 코드 {rec.get('code_fails')}" if rec.get("code_fails") else "")
              + (f" · 근거없음 {[u['claim'][:25] for u in rec.get('unsupported', [])]}" if rec.get("unsupported") else ""))

    # G — 불합격 → 재생성 → 재검수 경로가 끝까지 도는가. 최종 결과에 변조 값이 남아 있으면 안 된다
    g = graph.verify_one(copy.deepcopy(CASES[1][1]))
    final_text = graph.written(g) if g.get("headline") else ""
    g_ok = g["verify"]["result"] in ("재생성 후 통과", "불합격") and (
        g["status"] != "ok" or "50만" not in final_text)
    ok += g_ok
    rows.append({"case": "G 불합격 → 재생성 경로", "tampered": "B 를 verify_one 에 넣음",
                 "result": g["verify"]["result"], "status": g["status"], "leftover_tampered_value": "50만" in final_text,
                 "as_expected": g_ok, "trail": g["verify"]["trail"], "usage": g["verify"]["usage"]})
    print(f"[{'맞음' if g_ok else '틀림'}] G 불합격→재생성     결과 {g['verify']['result']} · 발행 {g['status']} · "
          f"변조값 남음 {'50만' in final_text}")

    # H — 검수 LLM 이 끝내 실패하면 발행하지 않는가 (LLM 을 가짜로 바꿔 비용 없이)
    orig = graph.ask

    def boom(*a, **k):
        raise graph.LLMError("시험용 강제 실패")

    graph.ask = boom
    h = graph.verify_one(copy.deepcopy(GOOD))
    graph.ask = orig
    h_ok = h["status"] == "skip" and h["verify"]["result"] == "검수 불가"
    ok += h_ok
    rows.append({"case": "H 검수 LLM 실패", "result": h["verify"]["result"], "status": h["status"], "as_expected": h_ok})
    print(f"[{'맞음' if h_ok else '틀림'}] H 검수 LLM 실패      결과 {h['verify']['result']} · 발행 {h['status']}")

    total = len(CASES) + 2
    tokens = {k: sum(r.get("usage", {}).get(k, 0) for r in rows) for k in ("calls", "input", "output")}
    out = {"run_at": started.isoformat(timespec="seconds"), "model": graph.MODEL, "as_expected": f"{ok}/{total}",
           "tokens": tokens, "cost_usd_est": round(tokens["input"] * 0.40 / 1e6 + tokens["output"] * 1.60 / 1e6, 4),
           "cases": rows}
    path = ROOT / "store" / "verify_proof.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{ok}/{total} 기대대로 · 호출 {tokens['calls']}회 · 약 ${out['cost_usd_est']} · 저장 {path.relative_to(ROOT)}")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
