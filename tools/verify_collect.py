"""수집 노드가 약속한 동작을 하는지 확인한다. 기록 파일은 건드리지 않는다.

    .venv\\Scripts\\python.exe tools\\verify_collect.py

1. 기업마당 항목에 선별 라벨(지원대상·신청기간·분야)이 채워지는가, 주소가 서로 다른가
2. 한 소스가 실패해도 나머지는 계속 수집하고, 실패를 기록하는가
3. 제외 목록 소스가 SOURCES 에 들어오면 수집 전에 멈추는가
4. 주소 중복(utm·대소문자·끝 슬래시)과 제목 중복(문장부호 차이)을 걸러내는가
"""

import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import graph  # noqa: E402

EMPTY = {"hours": graph.HOURS, "collected": [], "stats": {}, "log": []}
ORIG = list(graph.SOURCES)
results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"[{'통과' if ok else '실패'}] {name} — {detail}")


# 1
biz = graph.fetch_bizinfo()
labeled = sum(bool(x["meta"]["지원대상"] and x["meta"]["신청기간"]) for x in biz)
unique = len({graph.url_key(x["url"]) for x in biz})
check("라벨·주소", labeled == len(biz) and unique == len(biz),
      f"라벨 {labeled}/{len(biz)} · 고유 주소 {unique}/{len(biz)}")


# 2
def boom():
    raise ConnectionError("시험용 강제 실패")


graph.SOURCES = [("강제실패", boom)] + ORIG
out = graph.collect(EMPTY)
st = out["stats"]["collect"]
check("실패 격리", "강제실패" in st["dead"] and st["collected"] > 0,
      f"dead={list(st['dead'])} · 나머지 {st['collected']}건 수집")

# 3
graph.SOURCES = ORIG + [("TechCrunch", graph.rss("https://techcrunch.com/category/artificial-intelligence/feed/"))]
try:
    graph.collect(EMPTY)
    check("제외 목록", False, "멈추지 않았다")
except SystemExit as ex:
    check("제외 목록", True, str(ex))

# 4
now = datetime.now(graph.KST)
a = lambda: [{"title": "2026년 테스트 공고 모집", "url": "https://a.kr/x?id=1&utm_source=z", "at": now, "summary": "", "meta": {}}]
b = lambda: [{"title": "2026년 테스트 공고 모집!", "url": "https://b.kr/y", "at": now, "summary": "", "meta": {}},
             {"title": "다른 기사", "url": "https://A.kr/x/?id=1", "at": now, "summary": "", "meta": {}}]
graph.SOURCES = [("가", a), ("나", b)]
st = graph.collect(EMPTY)["stats"]["collect"]
check("중복 제거", st["collected"] == 1 and st["dup_url"] == 1 and st["dup_title"] == 1,
      f"3건 입력 → {st['collected']}건 · 주소 중복 {st['dup_url']} · 제목 중복 {st['dup_title']}")

graph.SOURCES = ORIG
print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
