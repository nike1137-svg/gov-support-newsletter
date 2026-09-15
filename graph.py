"""1인 창조기업 운영자를 위한 매일 아침 뉴스레터 — LangGraph 워크플로

    수집 → 선별 → 요약 → 검수 → 발행

지금 구현된 노드: 수집
독자 · 기준 · 제외 조건은 audience.yaml, 소스 채택 근거는 docs/source-criteria.md
"""

import operator
import os
import pathlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Annotated, TypedDict
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
import yaml
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
AUDIENCE = yaml.safe_load((ROOT / "audience.yaml").read_text(encoding="utf-8"))

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (gov-support-newsletter)"}
HOURS = 24                          # 매일 아침 발행 — 직전 24시간
TIMEOUT = 30


# ---------------------------------------------------------------- State
def merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class Brief(TypedDict):
    hours: int                                  # 수집 시간 창
    collected: list                             # 수집한 기사 (중복 제거 후)
    stats: Annotated[dict, merge]               # 단계별 수치 — store/metrics.jsonl 로 간다
    log: Annotated[list, operator.add]          # 사람이 읽는 실행 기록


# ---------------------------------------------------------------- 수집
def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_bizinfo():
    """기업마당 지원사업 API. 선별 라벨로 쓸 지원대상·신청기간·분야를 함께 받는다"""
    r = requests.get("https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do",
                     params={"crtfcKey": os.environ["BIZINFO_API_KEY"], "dataType": "rss", "searchCnt": 200},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        try:
            at = datetime.strptime((it.findtext("pubDate") or "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except ValueError:
            continue                                # 날짜 없는 항목은 창으로 자를 수 없다
        out.append({"title": (it.findtext("title") or "").strip(),
                    "url": (it.findtext("link") or "").strip(),
                    "at": at,
                    "summary": strip_tags(it.findtext("description"))[:500],
                    "meta": {"지원대상": (it.findtext("trgetNm") or "").strip(),
                             "신청기간": (it.findtext("reqstDt") or "").strip(),
                             "분야": (it.findtext("lcategory") or "").strip(),
                             "소관기관": (it.findtext("author") or "").strip()}})
    return out


def rss(url):
    def fetch():
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        out = []
        for e in feedparser.parse(r.content).entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            if not t:
                continue
            out.append({"title": e.get("title", "").strip(), "url": e.get("link", "").strip(),
                        "at": datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST),
                        "summary": strip_tags(e.get("summary", ""))[:500], "meta": {}})
        return out
    fetch.url = url
    return fetch


# 채택 소스 — docs/source-criteria.md v2 측정(2026-09-15)에서 C1~C4 를 통과한 4곳.
# 앞에 있을수록 제목 중복 시 남는다. 기업마당이 라벨(지원대상·신청기간)을 가장 많이 준다.
SOURCES = [
    ("기업마당",        fetch_bizinfo),
    ("중기부 사업공고", rss("https://www.mss.go.kr/rss/smba/board/310.do")),
    ("중기부 보도자료", rss("https://www.mss.go.kr/rss/smba/board/86.do")),
    ("스타트업투데이",  rss("https://www.startuptoday.kr/rss/allArticle.xml")),
]


def check_excluded(sources):
    hosts = AUDIENCE["exclude"]["source_hosts"]
    for name, fetch in sources:
        host = urlparse(getattr(fetch, "url", "")).hostname or ""
        if any(host == h or host.endswith("." + h) for h in hosts):
            raise SystemExit(f"제외 목록 소스가 SOURCES 에 있다: {name} ({host})")


def url_key(url):
    """주소 중복 비교용. 추적 꼬리표(utm_*)만 떼고 쿼리는 남긴다 —
    기업마당은 공고 ID 가 쿼리(pblancId)에 있어서 쿼리를 통째로 떼면 전부 같은 주소가 된다."""
    p = urlparse(url.strip())
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")])
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", q, ""))


def title_key(title):
    """제목 중복 비교용. 기업마당이 중기부 공고를 다시 싣기 때문에 소스가 달라도 같은 공고가 온다"""
    return re.sub(r"[\W_]+", "", title).lower()


def collect(s: Brief) -> dict:
    check_excluded(SOURCES)
    now = datetime.now(KST)
    cutoff = now - timedelta(hours=s["hours"])
    items, dead, by_source = [], {}, {}
    seen_url, seen_title = set(), set()
    dup_url = dup_title = 0

    for name, fetch in SOURCES:
        try:
            got = fetch()
        except Exception as ex:                     # 한 곳이 죽어도 나머지는 계속
            dead[name] = f"{type(ex).__name__}: {ex}"[:120]
            continue
        fresh = [it for it in got if cutoff <= it["at"] <= now]
        kept = 0
        for it in fresh:
            uk, tk = url_key(it["url"]), title_key(it["title"])
            if uk in seen_url:
                dup_url += 1
                continue
            if tk in seen_title:
                dup_title += 1
                continue
            seen_url.add(uk)
            seen_title.add(tk)
            items.append({**it, "source": name, "at": it["at"].isoformat(timespec="minutes")})
            kept += 1
        by_source[name] = {"fetched": len(got), "in_window": len(fresh), "kept": kept}

    items.sort(key=lambda x: x["at"], reverse=True)
    stats = {"collect": {"hours": s["hours"], "collected": len(items), "by_source": by_source,
                         "dup_url": dup_url, "dup_title": dup_title, "dead": dead}}
    line = (f"⊙ 수집  {s['hours']}시간 창 · {len(items)}건 ("
            + " · ".join(f"{k} {v['kept']}" for k, v in by_source.items()) + ")"
            + f" · 중복 제거 주소 {dup_url} 제목 {dup_title}"
            + (f" · 응답 없음 {list(dead)}" if dead else ""))
    return {"collected": items, "stats": stats, "log": [line]}


# ---------------------------------------------------------------- 그래프
def build():
    g = StateGraph(Brief)
    g.add_node("collect", collect)
    g.add_edge(START, "collect")
    g.add_edge("collect", END)
    return g.compile()


def run(hours: int = HOURS) -> dict:
    return build().invoke({"hours": hours, "collected": [], "stats": {}, "log": []})
