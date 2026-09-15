"""1인 창조기업 운영자를 위한 매일 아침 뉴스레터 — LangGraph 워크플로

    수집 → 선별 → 요약 → 검수 → 발행

지금 구현된 노드: 수집(collect) → 예선(prelim) → 본선(final)
독자 · 기준 · 제외 조건은 audience.yaml, 소스 채택 근거는 docs/source-criteria.md
"""

import operator
import os
import pathlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, TypedDict
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
import yaml
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

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
    survivors: list                             # 예선 통과 — 기준 라벨이 붙은 후보
    picked: list                                # 본선 선택 3~5건
    screened: Annotated[list, operator.add]     # 기사마다 붙은 라벨과 이유 — 선별 기준이 동작한 근거
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
    for n, it in enumerate(items):
        it["id"] = f"a{n:03d}"                      # 이번 실행 안에서 기사를 가리키는 번호 — 라벨 기록이 이걸로 이어진다
    stats = {"collect": {"hours": s["hours"], "collected": len(items), "by_source": by_source,
                         "dup_url": dup_url, "dup_title": dup_title, "dead": dead}}
    line = (f"⊙ 수집  {s['hours']}시간 창 · {len(items)}건 ("
            + " · ".join(f"{k} {v['kept']}" for k, v in by_source.items()) + ")"
            + f" · 중복 제거 주소 {dup_url} 제목 {dup_title}"
            + (f" · 응답 없음 {list(dead)}" if dead else ""))
    return {"collected": items, "stats": stats, "log": [line]}


# ---------------------------------------------------------------- LLM 호출
# SDK 대신 HTTP 로 직접 부른다 — 의존성을 줄이고, 재시도·토큰 집계를 한곳에서 다루려고.
MODEL = "gpt-4.1-mini"
TRIES = 3                                           # 네트워크·429·5xx·형식 오류에 최대 세 번


class LLMError(Exception):
    """재시도를 다 쓰고도 받지 못했을 때. 노드는 이걸 잡아 대체 경로로 간다"""


def ask(schema: type[BaseModel], system: str, user: str, check=None) -> tuple[BaseModel, dict]:
    """구조화 출력으로 받아 pydantic 으로 검증한다. check(out) 가 문자열을 돌려주면 그 이유로 다시 요청한다."""
    body = {"model": MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema.__name__, "strict": True,
                                                "schema": schema.model_json_schema()}}}
    usage = {"calls": 0, "input": 0, "output": 0, "retries": 0}
    last = ""
    base = list(body["messages"])
    for attempt in range(TRIES):
        if attempt:
            usage["retries"] += 1
            time.sleep(2 ** attempt)
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions",
                              headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                              json=body, timeout=90)
            usage["calls"] += 1
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                continue
            r.raise_for_status()
            data = r.json()
            usage["input"] += data["usage"]["prompt_tokens"]
            usage["output"] += data["usage"]["completion_tokens"]
            content = data["choices"][0]["message"]["content"]
            out = schema.model_validate_json(content)
        except (requests.RequestException, ValueError, KeyError) as ex:
            last = f"{type(ex).__name__}: {str(ex)[:120]}"
            continue
        problem = check(out) if check else None
        if problem:
            last = f"검증 실패: {problem}"
            # 같은 요청을 되풀이하면 temperature 0 에서는 같은 답이 온다 — 무엇이 틀렸는지 알려주고 다시 받는다
            body["messages"] = base + [{"role": "assistant", "content": content},
                                       {"role": "user", "content": f"응답에 문제가 있습니다: {problem}\n전체를 다시 작성하세요."}]
            continue
        return out, usage
    raise LLMError(f"{last} (시도 {TRIES}회)")


def add_usage(total: dict, u: dict) -> dict:
    return {k: total.get(k, 0) + u.get(k, 0) for k in ("calls", "input", "output", "retries")}


# ---------------------------------------------------------------- 선별 — 예선
CRITERIA = AUDIENCE["importance"]
DROPS = AUDIENCE["drop"]
KEEP_IDS = [c["id"] for c in CRITERIA]
LLM_DROP_IDS = [d["id"] for d in DROPS if "llm" in d["how"]]
BATCH = 15          # 예선 묶음 크기 — 이유는 REPORT.md 「선별 로직 설계」
TARGET_MIN, TARGET_MAX = 3, 5

Label = Literal[tuple(KEEP_IDS + LLM_DROP_IDS)]
Keep = Literal[tuple(KEEP_IDS)]


# 기사는 목록 순번이 아니라 기사 ID(a012 등)로 가리키고, 제목을 함께 적게 한다.
# 2026-09-15 순번으로 받았을 때 본선에서 번호가 한 칸씩 밀려 다른 기사의 이유가 붙었다 — 제목 대조로 잡는다.
class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="목록 맨 앞의 기사 ID")
    title: str = Field(description="그 ID 의 제목을 목록에서 그대로 옮겨 적는다")
    label: Label
    reason: str = Field(description="그 라벨을 붙인 근거 한 문장. 목록에 적힌 내용만 근거로 쓴다")


class Screen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[Tag]


def reader_brief():
    lines = [f"독자: {AUDIENCE['reader']['persona']}", "독자가 원하는 것: " + ", ".join(AUDIENCE["reader"]["needs"]),
             "", "중요도 기준 (위가 우선):"]
    lines += [f"- {c['id']}: {c['text']}" for c in CRITERIA]
    lines += ["", "버릴 것:"] + [f"- {d['id']}: {d['text']}" for d in DROPS if "llm" in d["how"]]
    lines += ["", "판정 주의:"] + [f"- {n}" for n in AUDIENCE.get("notes", [])]
    return "\n".join(lines)


def listing(items, with_label=False):
    rows = []
    for it in items:
        meta = " | ".join(f"{k}: {v}" for k, v in it["meta"].items() if v and k in ("지원대상", "신청기간", "분야"))
        head = f"{it['id']} " + (f"({it['label']}) " if with_label else "") + f"[{it['source']}] {it['title']}"
        rows.append(head + (f" | {meta}" if meta else "")
                    + (f"\n   요약: {it['summary'][:160]}" if it["summary"] else ""))
    return "\n".join(rows)


def same_title(given, actual):
    """LLM 이 옮긴 제목이 그 ID 의 제목인지. 앞에 [소스명]을 붙이거나 뒤를 줄여 옮겨도 같은 기사로 본다"""
    g = title_key(re.sub(r"^\s*\[[^\]]*\]\s*", "", given))
    a = title_key(actual)
    head = min(15, len(a))
    return bool(g) and (a[:head] in g or g[:head] in a)


def id_problems(answers, items, what="라벨"):
    """ID 가 목록과 정확히 맞는지, 제목이 그 ID 의 제목인지 본다. 문제가 없으면 None"""
    by_id = {it["id"]: it for it in items}
    got = [a.id for a in answers]
    unknown = [i for i in got if i not in by_id]
    missing = [i for i in by_id if i not in got]
    dup = sorted({i for i in got if got.count(i) > 1})
    shifted = [a.id for a in answers if a.id in by_id and hasattr(a, "title")
               and not same_title(a.title, by_id[a.id]["title"])]
    probs = []
    if unknown:
        probs.append(f"목록에 없는 ID {unknown}")
    if missing:
        probs.append(f"{what}이 빠진 ID {missing}")
    if dup:
        probs.append(f"두 번 나온 ID {dup}")
    if shifted:
        ex = next(a for a in answers if a.id in shifted)
        probs.append(f"ID 와 제목이 맞지 않는 항목 {shifted} — ID 옆 제목을 그대로 옮기세요 "
                     f"(예: {ex.id} 에 '{ex.title[:30]}' 라고 적었지만 실제 제목은 '{by_id[ex.id]['title'][:30]}')")
    return " / ".join(probs) or None


PERIOD = re.compile(r"(\d{4})[-.](\d{2})[-.](\d{2})\s*~\s*(\d{4})[-.](\d{2})[-.](\d{2})")
BIG_ONLY = re.compile(r"중견|대기업")
SMALL_OK = re.compile(r"중소|소상공인|창업|1인|예비|개인|청년|스타트업")


def rule_drop(it, today):
    """코드로 판정할 수 있는 버림 규칙. 근거가 필드에 그대로 있을 때만 쓴다"""
    m = PERIOD.search(it["meta"].get("신청기간", ""))
    if m:
        end = datetime(int(m[4]), int(m[5]), int(m[6]), tzinfo=KST).date()
        if end < today:
            return "버림1", f"신청기간이 {end} 에 끝났다"
    target = it["meta"].get("지원대상", "")
    if target and BIG_ONLY.search(target) and not SMALL_OK.search(target):
        return "버림2", f"지원대상이 '{target}' 뿐이다"
    return None


def record(it, stage, label, reason):
    return {"id": it["id"], "source": it["source"], "title": it["title"], "stage": stage,
            "label": label, "reason": reason}


def prelim(s: Brief) -> dict:
    today = datetime.now(KST).date()
    tags, survivors, usage = [], [], {}
    rule_n = {}

    pending = []
    for it in s["collected"]:
        hit = rule_drop(it, today)
        if hit:
            tags.append(record(it, "예선-규칙", *hit))
            rule_n[hit[0]] = rule_n.get(hit[0], 0) + 1
        else:
            pending.append(it)

    fallback, missing = False, 0
    system = (reader_brief() + "\n\n아래 목록의 기사 하나하나에 라벨을 정확히 한 번씩 붙이세요.\n"
              "각 줄 맨 앞이 기사 ID 입니다. ID 와 그 줄의 제목을 그대로 옮겨 적으세요.\n"
              "라벨은 기준 id 또는 버림 id 중 하나입니다. 근거는 그 줄에 적힌 제목·지원대상·신청기간·요약에서만 찾으세요.")
    for b in range(0, len(pending), BATCH):
        chunk = pending[b:b + BATCH]

        def covers_all(out, chunk=chunk):
            probs = id_problems(out.tags, chunk)
            if probs:
                return probs
            # 버림2 는 지원대상에 중소·소상공인·창업이 적혀 있으면 붙일 수 없다 (audience.yaml notes)
            by_id = {it["id"]: it for it in chunk}
            wrong = [t.id for t in out.tags
                     if t.label == "버림2" and SMALL_OK.search(by_id[t.id]["meta"].get("지원대상", ""))]
            if wrong:
                return f"{wrong} 는 지원대상에 중소기업·소상공인·창업이 있어 버림2 가 될 수 없습니다"
            return None

        try:
            out, u = ask(Screen, system, listing(chunk), check=covers_all)
            usage = add_usage(usage, u)
            by_id = {t.id: t for t in out.tags}
        except LLMError as ex:
            # 예선 LLM 이 끝내 실패하면 규칙만 통과한 채 본선으로 넘긴다 — 멈추지 않는다
            fallback = True
            for it in chunk:
                tags.append(record(it, "예선-LLM", "미판정", f"LLM 실패로 규칙 통과분을 그대로 넘김 ({ex})"))
                survivors.append({**it, "label": "미판정", "why_kept": "예선 LLM 실패"})
            continue
        for it in chunk:
            t = by_id.get(it["id"])
            if t is None:                           # covers_all 이 통과했으면 오지 않는다
                missing += 1
                tags.append(record(it, "예선-LLM", "라벨누락", "LLM 이 이 번호에 라벨을 붙이지 않았다"))
                continue
            tags.append(record(it, "예선-LLM", t.label, t.reason))
            if t.label in KEEP_IDS:
                survivors.append({**it, "label": t.label, "why_kept": t.reason})

    counts = {}
    for t in tags:
        counts[t["label"]] = counts.get(t["label"], 0) + 1
    stats = {"prelim": {"input": len(s["collected"]), "rule_dropped": rule_n, "llm_batches": -(-len(pending) // BATCH),
                        "batch_size": BATCH, "labels": counts, "survivors": len(survivors),
                        "label_missing": missing, "fallback": fallback, "usage": usage}}
    line = (f"⊙ 예선  {len(s['collected'])} → 규칙 제외 {sum(rule_n.values())} {rule_n or ''}"
            f" → LLM 묶음 {stats['prelim']['llm_batches']}개({BATCH}건씩) → 통과 {len(survivors)}"
            f" · 라벨 {counts}" + (" · ⚠ LLM 실패로 대체" if fallback else ""))
    return {"survivors": survivors, "screened": tags, "stats": stats, "log": [line]}


# ---------------------------------------------------------------- 선별 — 본선
class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="후보 줄 맨 앞의 기사 ID")
    title: str = Field(description="그 ID 의 제목을 그대로 옮겨 적는다")
    label: Keep
    reason: str = Field(description="이 독자에게 오늘 보낼 이유 한 문장. 그 기사 줄의 내용만 근거로 쓴다")


class Drop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str = Field(description="그 ID 의 제목을 그대로 옮겨 적는다")
    reason: str = Field(description="오늘 보내지 않는 이유 한 문장. 다른 후보와 비교해서")


class Final(BaseModel):
    model_config = ConfigDict(extra="forbid")
    picks: list[Pick] = Field(description="보낼 순서대로")
    drops: list[Drop]


def rank_by_rule(cands):
    """본선 LLM 이 실패했을 때의 대체 순위 — 기준 우선순위, 그다음 최신순"""
    order = {k: n for n, k in enumerate(KEEP_IDS)}
    newest_first = sorted(cands, key=lambda c: c["at"], reverse=True)
    return sorted(newest_first, key=lambda c: order.get(c["label"], len(order)))    # 안정 정렬이라 최신순이 유지된다


def final(s: Brief) -> dict:
    cands = s["survivors"]
    tags, usage, fallback = [], {}, False

    if len(cands) <= TARGET_MIN:                    # 고를 게 없으면 LLM 을 부르지 않는다
        picked = [{**c, "rank": n + 1, "why_pick": c["why_kept"]} for n, c in enumerate(cands)]
        tags = [record(c, "본선", c["label"], "후보가 최소 발행 수 이하라 모두 보냄") for c in cands]
    else:
        want_max = min(TARGET_MAX, len(cands))

        def valid(out):
            if not TARGET_MIN <= len(out.picks) <= want_max:
                return f"{TARGET_MIN}~{want_max}건을 골라야 하는데 {len(out.picks)}건"
            return id_problems(out.picks + out.drops, cands, what="선택 또는 탈락")

        system = (reader_brief() + f"\n\n예선을 통과한 후보입니다. 오늘 아침 이 독자에게 보낼 {TARGET_MIN}~{want_max}건을 "
                  "중요한 순서대로 고르세요.\n기준 우선순위를 따르되, 같은 사업·같은 사건을 다룬 기사는 하나만 고르세요.\n"
                  "각 줄 맨 앞이 기사 ID 입니다. ID 와 그 줄의 제목을 그대로 옮겨 적고, 이유는 그 줄의 내용만 근거로 쓰세요.\n"
                  "고르지 않은 후보도 모두 drops 에 넣고 이유를 적으세요.")
        by_id = {c["id"]: c for c in cands}
        try:
            out, usage = ask(Final, system, listing(cands, with_label=True), check=valid)
            picked = [{**by_id[p.id], "label": p.label, "rank": r + 1, "why_pick": p.reason}
                      for r, p in enumerate(out.picks)]
            tags = [record(by_id[p.id], "본선", "선택", f"{r + 1}위 · {p.label} · {p.reason}")
                    for r, p in enumerate(out.picks)]
            tags += [record(by_id[d.id], "본선", "탈락", d.reason) for d in out.drops]
        except LLMError as ex:
            fallback = True
            ranked = rank_by_rule(cands)[:TARGET_MAX]
            picked = [{**c, "rank": n + 1, "why_pick": f"대체 규칙(기준 순위·최신) — {c['why_kept']}"}
                      for n, c in enumerate(ranked)]
            tags = [record(c, "본선", "선택", f"LLM 실패로 대체 규칙 적용 ({ex})") for c in ranked]

    by_label = {}
    for p in picked:
        by_label[p["label"]] = by_label.get(p["label"], 0) + 1
    stats = {"final": {"candidates": len(cands), "picked": len(picked), "by_label": by_label,
                       "fallback": fallback, "usage": usage}}
    line = (f"⊙ 본선  {len(cands)} → {len(picked)}건 {by_label}" + (" · ⚠ LLM 실패로 대체 규칙" if fallback else ""))
    return {"picked": picked, "screened": tags, "stats": stats, "log": [line]}


# ---------------------------------------------------------------- 그래프
def build():
    g = StateGraph(Brief)
    g.add_node("collect", collect)
    g.add_node("prelim", prelim)
    g.add_node("final", final)
    g.add_edge(START, "collect")
    g.add_edge("collect", "prelim")
    g.add_edge("prelim", "final")
    g.add_edge("final", END)
    return g.compile()


def run(hours: int = HOURS) -> dict:
    return build().invoke({"hours": hours, "collected": [], "survivors": [], "picked": [],
                           "screened": [], "stats": {}, "log": []})
