"""소스 후보를 같은 시각에 기준 v2 로 재고 store/source_probe.json 에 남긴다.

기준은 docs/source-criteria.md (v2). 판정은 그 문서의 탈락 조건으로만 내린다.
v1 결과는 store/source_probe_v1.json 에 보관돼 있다.

    .venv\\Scripts\\python.exe tools\\probe_sources.py
"""

import json
import os
import pathlib
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
WINDOW_H = 72
PIPELINE_MIN_PER_DAY = 5
UA = {"User-Agent": "Mozilla/5.0 (gov-support-newsletter)"}
READER_WORDS = ("창업", "스타트업", "소상공인", "중소기업", "1인", "지원", "정책", "공고", "모집", "세금", "투자")
OUT = pathlib.Path("store/source_probe.json")

# 과제가 제외하라고 한 소스 — 후보 주소가 여기 걸리면 측정하지 않고 멈춘다
EXCLUDED_HOSTS = ("openai.com", "deepmind.google", "techcrunch.com", "theverge.com",
                  "technologyreview.com", "aitimes.com")

FREE_RSS = {"key": "불필요", "cost": "무료 (공개 RSS)", "limit": "표기 없음"}


# ---------------------------------------------------------------- 소스별 수집 — (status, [(title, dt, date_only)], total)
# total 은 소스가 "조건에 맞는 전체 건수"를 알려줄 때만 채운다. 없으면 None.
def window_start():
    return NOW - timedelta(hours=WINDOW_H)


def fetch_bizinfo():
    r = requests.get("https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do",
                     params={"crtfcKey": os.environ["BIZINFO_API_KEY"], "dataType": "rss", "searchCnt": 200},
                     headers=UA, timeout=30)
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        raw = (it.findtext("pubDate") or "").strip()          # 2026-09-14 15:38:58 — KST 로 본다
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except ValueError:
            dt = None
        items.append((it.findtext("title", "").strip(), dt, False))
    return r.status_code, items, None


def fetch_kstartup():
    # 정렬 파라미터가 없어 첫 페이지가 최신이라는 보장이 없다 (2026-09-15 확인: 최신순 아님).
    # swagger 의 날짜 필터 cond[pbanc_rcpt_bgng_dt::GTE] 는 넣어도 결과가 그대로라 쓰지 않는다.
    # 모집중(Y) 필터는 동작한다 — 모집중 전체를 받아 창 안쪽을 직접 센다.
    # totalCount 는 필터와 무관한 데이터셋 전체 건수(30,080), 필터 후 건수는 matchCount 다.
    r = requests.get("https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01",
                     params={"serviceKey": os.environ["KSTARTUP_API_KEY"], "page": 1, "perPage": 1000,
                             "returnType": "json", "cond[rcrt_prgs_yn::EQ]": "Y"},
                     headers=UA, timeout=60)
    body = r.json()
    items = []
    for it in body.get("data", []):
        raw = str(it.get("pbanc_rcpt_bgng_dt") or "")          # 20260909 — 접수 시작일, 시각 없음
        try:
            dt = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=KST)
        except ValueError:
            dt = None
        items.append((it.get("biz_pbanc_nm", "").strip(), dt, True))
    return r.status_code, items, body.get("matchCount")


def fetch_rss(url):
    def run():
        r = requests.get(url, headers=UA, timeout=30)
        feed = feedparser.parse(r.content)
        # 피드가 인코딩을 잘못 선언하면 한글이 깨진다 (비석세스). UTF-8 로 풀어서 한 번 더 시도한다
        if feed.entries and not any(re.search(r"[가-힣]", e.get("title", "")) for e in feed.entries[:5]):
            try:
                retry = feedparser.parse(r.content.decode("utf-8"))
                if any(re.search(r"[가-힣]", e.get("title", "")) for e in retry.entries[:5]):
                    feed = retry
            except UnicodeDecodeError:
                pass
        items = []
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            dt = datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST) if t else None
            items.append((e.get("title", "").strip(), dt, False))
        return r.status_code, items, None
    run.url = url
    return run


SOURCES = [
    # 이름, 형태, 수집 함수, C5 이용 조건
    ("기업마당",        "API", fetch_bizinfo,
     {"key": "필요 (crtfcKey)", "cost": "요금 문구 없음 · 결제 절차 없음", "limit": "표기 없음"}),
    ("K-Startup",       "API", fetch_kstartup,
     {"key": "필요 (serviceKey)", "cost": "무료 (공공데이터포털 표기)", "limit": "개발계정 10,000회/일"}),
    ("중기부 사업공고", "RSS", fetch_rss("https://www.mss.go.kr/rss/smba/board/310.do"), FREE_RSS),
    ("중기부 보도자료", "RSS", fetch_rss("https://www.mss.go.kr/rss/smba/board/86.do"), FREE_RSS),
    ("중소기업뉴스",    "RSS", fetch_rss("https://www.kbiznews.co.kr/rss/allArticle.xml"), FREE_RSS),
    ("중기이코노미",    "RSS", fetch_rss("https://www.junggi.co.kr/rss/allArticle.xml"), FREE_RSS),
    ("벤처스퀘어",      "RSS", fetch_rss("https://www.venturesquare.net/feed"), FREE_RSS),
    ("스타트업엔",      "RSS", fetch_rss("https://www.startupn.kr/rss/allArticle.xml"), FREE_RSS),
    ("스타트업투데이",  "RSS", fetch_rss("https://www.startuptoday.kr/rss/allArticle.xml"), FREE_RSS),
    ("비석세스",        "RSS", fetch_rss("https://besuccess.com/feed"), FREE_RSS),
    ("아웃스탠딩",      "RSS", fetch_rss("https://outstanding.kr/feed"), FREE_RSS),
    ("한국경제",        "RSS", fetch_rss("https://www.hankyung.com/feed/economy"), FREE_RSS),
    ("전자신문",        "RSS", fetch_rss("https://rss.etnews.com/Section901.xml"), FREE_RSS),
]


def check_excluded():
    for name, _, fetch, _ in SOURCES:
        url = getattr(fetch, "url", "")
        host = urlparse(url).hostname or ""
        if any(host == h or host.endswith("." + h) for h in EXCLUDED_HOSTS):
            raise SystemExit(f"제외 목록 소스가 후보에 있다: {name} {url}")


# ---------------------------------------------------------------- 측정
def measure(src):
    name, kind, fetch, terms = src
    row = {"source": name, "kind": kind, "url": getattr(fetch, "url", None), "terms": terms}
    try:
        status, items, total = fetch()
    except Exception as ex:
        row["c1"] = {"pass": False, "status": None, "count": 0, "error": f"{type(ex).__name__}: {ex}"[:200]}
        return row

    # C1 접근 — 제목이 한글로 읽히는지도 함께 본다
    readable = any(re.search(r"[가-힣]", t) for t, _, _ in items[:10])
    row["c1"] = {"pass": status == 200 and len(items) > 0 and readable,
                 "status": status, "count": len(items), "title_readable": readable}

    # C2 날짜
    dated = [(t, d) for t, d, _ in items if d]
    date_only = any(o for _, _, o in items)
    ratio = round(len(dated) / len(items), 2) if items else 0
    row["c2"] = {"pass": len(dated) > 0, "dated_ratio": ratio, "date_only": date_only}

    # C3 (v2) 살아 있음 — 72시간 창에 1건 이상. 하루 평균은 합계 판정에 쓴다
    start = window_start()
    if date_only:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    past = [d for _, d in dated if d <= NOW]
    in_win = [d for d in past if d >= start]
    oldest = min(past) if past else None
    seq = [d for _, d, _ in items if d]
    sorted_desc = all(a >= b for a, b in zip(seq, seq[1:]))
    # 창을 다 덮었나 — 전체 건수를 주면 다 받았는지로, 아니면 최신순일 때만 가장 오래된 항목으로 판단한다
    if total is not None:
        covers = len(items) >= total
    else:
        covers = sorted_desc and oldest is not None and oldest < start
    row["c3"] = {"pass": len(in_win) >= 1, "in_72h": len(in_win),
                 "per_day": round(len(in_win) / (WINDOW_H / 24), 1),
                 "per_day_is_lower_bound": not covers,           # 못 덮었으면 실제 공급량은 이보다 많다
                 "covers_window": covers, "sorted_desc": sorted_desc, "total_reported": total,
                 "newest": max(past).isoformat() if past else None,
                 "future_start": len(dated) - len(past)}

    # C4 (v2) 독자 적합 — 최신 10건 제목 중 독자 키워드 비율
    latest = [t for t, _ in sorted(dated, key=lambda x: x[1], reverse=True)[:10]]
    hits = [t for t in latest if any(w in t for w in READER_WORDS)]
    row["c4"] = {"pass": len(latest) > 0 and len(hits) / len(latest) >= 0.5,
                 "ratio": f"{len(hits)}/{len(latest)}", "hits": hits,
                 "miss": [t for t in latest if t not in hits]}
    return row


def mark(v):
    return {True: "통과", False: "탈락", None: "판정불가"}[v]


def main():
    check_excluded()
    with ThreadPoolExecutor(6) as ex:
        rows = list(ex.map(measure, SOURCES))
    for r in rows:
        r["adopted"] = all(r.get(c, {}).get("pass") is True for c in ("c1", "c2", "c3", "c4"))
        r["failed_on"] = [c.upper() for c in ("c1", "c2", "c3", "c4") if r.get(c, {}).get("pass") is not True]

    adopted = [r for r in rows if r["adopted"]]
    pipeline = {"adopted_count": len(adopted),
                "per_day_sum": round(sum(r["c3"]["per_day"] for r in adopted), 1),
                "min_per_day": PIPELINE_MIN_PER_DAY,
                "min_sources": 3,
                "note": "중복 제거 전 합계. 창을 못 덮은 소스는 하한값이다"}
    pipeline["pass"] = pipeline["adopted_count"] >= 3 and pipeline["per_day_sum"] >= PIPELINE_MIN_PER_DAY

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"criteria_version": "v2", "measured_at": NOW.isoformat(timespec="seconds"),
                               "window_hours": WINDOW_H, "criteria": "docs/source-criteria.md",
                               "excluded_hosts_checked": list(EXCLUDED_HOSTS),
                               "pipeline": pipeline, "sources": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"측정 시각 {NOW:%Y-%m-%d %H:%M} KST · 기준 v2 · 창 {WINDOW_H}시간 · 제외 목록 검사 통과\n")
    print(f"{'소스':<10}{'판정':<6}{'C1':<10}{'C2':<6}{'C3 72h/하루':<16}{'C4':<8}탈락 기준")
    for r in rows:
        c1 = r["c1"]
        if "c2" not in r:
            print(f"{r['source']:<10}{'탈락':<6}{'오류':<10} {c1.get('error', '')}")
            continue
        c3, c4 = r["c3"], r["c4"]
        c1s = f"{c1['status']}/{c1['count']}" + ("" if c1["title_readable"] else "깨짐")
        c3s = f"{c3['in_72h']}/{c3['per_day']}" + ("+" if c3["per_day_is_lower_bound"] else "")
        print(f"{r['source']:<10}{'채택' if r['adopted'] else '탈락':<6}{c1s:<10}{mark(r['c2']['pass']):<6}"
              f"{c3s:<16}{c4['ratio']:<8}{','.join(r['failed_on'])}")
    print(f"\n채택 {pipeline['adopted_count']}곳 · 하루 합계 {pipeline['per_day_sum']}건 (중복 제거 전, + 는 하한값)"
          f" → 파이프라인 {mark(pipeline['pass'])}")
    print(f"저장: {OUT}")


if __name__ == "__main__":
    main()
