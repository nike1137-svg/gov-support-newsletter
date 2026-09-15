"""1인 창조기업 운영자를 위한 매일 아침 뉴스레터 — LangGraph 워크플로

    수집 → 선별 → 요약 → 검수 → 발행

노드: 수집(collect) → 예선(prelim) → 본선(final) → 요약·인사이트(write, 기사마다 병렬) → 검수(verify) → 발행(publish)
독자 · 기준 · 제외 조건은 audience.yaml, 소스 채택 근거는 docs/source-criteria.md
"""

import html
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
import trafilatura
import yaml
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
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
    drafted: Annotated[list, operator.add]      # 요약·인사이트 — 워커들이 나눠 채운다
    reviewed: list                              # 검수 기록이 붙은 작성물 전체 (스킵 포함). 줄이는 키라 리듀서 없음
    verified: list                              # 검수를 통과해 발행할 것
    messages: list                              # 실제로 보낸(또는 dry-run 에서 보낼) 텔레그램 메시지
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
        # 검수에서 빠질 몫을 남겨 둔다 — 3건만 고르면 한 건만 불합격해도 최소 발행 수(3)를 못 채운다 (2026-09-15 실제로 2건)
        want = min(TARGET_MAX, len(cands))

        def valid(out):
            if len(out.picks) != want:
                return f"정확히 {want}건을 골라야 하는데 {len(out.picks)}건"
            return id_problems(out.picks + out.drops, cands, what="선택 또는 탈락")

        system = (reader_brief() + f"\n\n예선을 통과한 후보입니다. 오늘 아침 이 독자에게 보낼 기사 {want}건을 "
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


# ---------------------------------------------------------------- 요약 · 인사이트
MIN_BODY = 200          # 이보다 짧으면 본문을 확인했다고 볼 수 없다
BODY_MAX = 6000         # LLM 에 넣는 본문 길이 상한


def bizinfo_body(html_text):
    """기업마당 상세 페이지는 '항목명 + 내용' 쌍으로 되어 있어 trafilatura 가 메뉴만 뽑는다 (2026-09-15 확인, 298자).
    쌍을 직접 읽어 '항목: 내용' 줄로 만든다."""
    # 항목명에는 태그가 없고, 내용은 다음 <li> 를 넘지 않는다 — 넓게 잡으면 메뉴·스크립트까지 딸려온다 (첫 시도에서 6000자가 잡힘)
    pairs = re.findall(r'<span class="s_title">\s*([^<]{1,40}?)\s*</span>\s*<div class="txt"[^>]*>'
                       r'((?:(?!<li\b).){0,8000}?)</div>\s*</li>', html_text, re.S)
    lines = [f"{strip_tags(html.unescape(k))}: {strip_tags(html.unescape(v))}" for k, v in pairs]
    return "\n".join(line for line in lines if not line.endswith(": "))


def extract_body(it):
    """(본문, 출처). 본문을 못 뽑으면 RSS/API 요약으로 대신하고 출처에 그렇게 적는다"""
    try:
        r = requests.get(it["url"], headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        body = bizinfo_body(r.text) if "bizinfo.go.kr" in it["url"] else (
            trafilatura.extract(r.text, include_tables=True, favor_recall=True) or "")
    except requests.RequestException:
        body = ""
    if len(body) >= MIN_BODY:
        return body[:BODY_MAX], "본문"
    if len(it.get("summary", "")) >= MIN_BODY // 2:
        return it["summary"], "피드 요약(본문 추출 실패)"
    return "", "없음"


NO_COND = "본문에 자격 조건이 없어 공고 원문 확인이 필요합니다."


class Insight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(description="이 독자가 할 일 한 문장. ~하세요 로 끝낸다")
    check: str = Field(description="본문에 적힌 신청 자격·조건을 구체적으로(나이·지역·업력·인원·업종 등) 한 문장. "
                                   f"본문에 자격 조건이 전혀 없을 때만 '{NO_COND}'")
    gain: str = Field(description="이 사업으로 독자가 얻는 것(돈 · 시간 · 판로 · 역량)을 본문 숫자와 함께 한 문장. "
                                  "예: '월 30만 원씩 최대 6개월, 임차료를 최대 180만 원 줄일 수 있습니다.' "
                                  "대상 여부 이야기는 쓰지 않는다")


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 2026-09-15 첫 실행에서 '상시근로자 5인 이상' 공고에 1인 창조기업이 신청하라는 인사이트가 나왔다.
    # 선별은 목록만 보고 통과시키므로, 본문을 읽은 이 단계에서 대상 여부를 다시 판정한다.
    fit: Literal["대상", "대상아님", "불명"] = Field(
        description="본문 조건으로 볼 때 1인 창조기업·예비창업자가 신청하거나 활용할 수 있는가 (정의는 지시문)")
    fit_quote: str = Field(description="fit 판정의 근거가 된 본문 구절을 한 글자도 바꾸지 않고 그대로. 불명이면 빈 문자열")
    fit_reason: str = Field(description="그 구절로 왜 그렇게 판정했는지 한 문장. 본문에 없는 내용을 유추하지 않는다")
    headline: str = Field(description="30자 이내 한국어 헤드라인")
    summary: str = Field(description="무엇을 · 누구에게 · 언제까지를 담은 2~3문장. 모든 문장을 ~합니다체로")
    insight: Insight
    evidence: list[str] = Field(description="요약과 인사이트의 근거가 된 본문 문장을 고치지 않고 그대로 2~4개 인용")


def sys_write():
    return (f"독자: {AUDIENCE['reader']['persona']}\n\n"
            "아래 본문을 읽고 먼저 이 독자가 대상인지 판정한 뒤, 헤드라인 · 요약 · 인사이트를 쓰세요.\n"
            "fit 판정:\n"
            "- 대상: 본문의 지원대상에 1인 창조기업이 들어갈 수 있는 대상(창업자 · 예비창업자 · 청년 창업자 · 소상공인 · "
            "중소기업 · 영리기업 · 사업자 등)이 적혀 있고, 1인이 충족할 수 없는 필수 조건이 없다\n"
            "- 대상아님: 1인이 충족할 수 없는 필수 조건이 있다 (N인 이상 고용 · 중견기업 이상 · 직원 대상 제도 운영 등)\n"
            "- 불명: 본문에 지원대상 설명이 아예 없다\n"
            "지역 · 나이 · 업종 조건은 대상아님의 이유가 아닙니다. 그 조건은 check 에 쓰세요\n\n"
            "- 요약은 본문을 줄인 것이고, 인사이트는 이 독자가 무엇을 해야 하는지입니다. 둘을 섞지 마세요\n"
            "- 인사이트의 check 에는 본문에 적힌 조건을 그대로 구체적으로 쓰세요. '조건을 확인하세요' 같은 빈말은 안 됩니다\n"
            "- 본문에 없는 금액 · 날짜 · 대상 · 조건을 만들지 마세요\n"
            "- 모든 문장은 ~합니다 / ~하세요 로 끝내세요. '~했다 · ~이다' 체는 쓰지 마세요\n"
            "- '주목된다 · 기대를 모은다' 같은 기자체 표현은 쓰지 마세요\n"
            "- evidence 에는 본문 문장을 한 글자도 바꾸지 말고 옮기세요")


PLAIN_END = re.compile(r"(?<!니)다\.?$|(?:함|음|됨|임)\.?$")  # ~했다. ~이다. ~함. (단 ~합니다. 는 제외)
EMPTY_CHECK = re.compile(r"조건을\s*(꼼꼼히\s*)?확인|명확히 제시|부합하는지\s*(점검|확인)")
FIT_TALK = re.compile(r"대상에\s*해당|대상이\s*아니|지원\s*대상|대상으로\s*(판정|명시)|신청할\s*수\s*있")
GUESS = re.compile(r"유추|추정|것으로\s*보|판단됨|가능성이\s*있")
HEADCOUNT = re.compile(r"(\d+)\s*인\s*이상")


def sentences(text):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]


def squash(s):
    """글자와 숫자만 남긴다. 표 기호(|)·문장부호·공백 차이로 같은 구절을 다르다고 보지 않게 —
    2026-09-15 중기부 본문이 '| 공고번호 | 제2026-556호 |' 표였는데 LLM 이 | 를 빼고 인용해 오탐이 났다"""
    return re.sub(r"[\W_]+", "", s or "")


def in_body(quote, body):
    """인용이 본문에 실제로 있는가 — 글자·숫자의 순서가 그대로 이어져야 한다"""
    q = squash(quote)
    return len(q) >= 4 and q in squash(body)


def draft_ok(d: Draft, body=""):
    if not re.search(r"[가-힣]", d.summary + d.insight.action):
        return "요약과 인사이트를 한국어로 쓰세요"
    if len(d.headline) > 40:
        return f"헤드라인이 {len(d.headline)}자입니다. 30자 이내로 줄이세요"
    if not 2 <= len(d.evidence) <= 4:
        return f"evidence 를 2~4개 인용하세요 (지금 {len(d.evidence)}개)"
    plain = [x for f in (d.summary, d.insight.action, d.insight.gain) for x in sentences(f) if PLAIN_END.search(x)]
    if plain:
        s = plain[0][:50]
        return (f"~합니다체가 아닌 문장이 있습니다: '{s}'. 문장 끝을 바꾸세요 "
                "(예: '추진한다.' → '추진합니다.', '지원이다.' → '지원입니다.')")
    if d.insight.check != NO_COND and EMPTY_CHECK.search(d.insight.check):
        return f"check 가 빈말입니다: '{d.insight.check[:40]}'. 본문의 구체 조건을 쓰세요"
    if FIT_TALK.search(d.insight.gain):
        return f"gain 에 대상 여부 이야기가 들어갔습니다: '{d.insight.gain[:40]}'. 얻는 것(돈·시간·판로·역량)만 쓰세요"
    # 판정 근거는 본문 인용이어야 한다 — 2026-09-15 '유추할 수 있어' 로 대상아님을 판정한 사례
    if d.fit != "불명" and not in_body(d.fit_quote, body):
        return f"fit_quote '{d.fit_quote[:30]}' 가 본문에 없습니다. 본문 구절을 그대로 옮기세요"
    if GUESS.search(d.fit_reason):
        return f"fit_reason 이 추측입니다: '{d.fit_reason[:40]}'. 본문에 적힌 조건만으로 판정하세요"
    m = [int(n) for n in HEADCOUNT.findall(body)]
    if d.fit == "대상" and any(n >= 2 for n in m):
        return f"본문에 '{max(m)}인 이상' 조건이 있는데 1인 창조기업을 대상으로 판정했습니다. fit 을 다시 판정하세요"
    return None


class WriteIn(TypedDict):          # 워커가 받는 것은 기사 하나뿐
    item: dict


def fan_write(s: Brief):
    """본선 선택 건수만큼 워커를 펼친다. 고른 게 없으면 발행으로 바로 가서 '없음'을 알린다"""
    return [Send("write", {"item": p}) for p in s["picked"]] or ["publish"]


def compose(it, body, feedback=None):
    """요약·인사이트를 한 번 쓴다. feedback 이 있으면 검수가 지적한 문제를 고쳐 다시 쓴다 (4단계 재생성)"""
    user = f"[제목] {it['title']}\n[출처] {it['source']}\n\n[본문]\n{body}"
    if feedback:
        user += ("\n\n[이전 작성물이 검수에서 불합격한 이유]\n" + feedback +
                 "\n위 문제를 고쳐 처음부터 다시 쓰세요. 본문에 없는 내용은 빼세요.")
    return ask(Draft, sys_write(), user, check=lambda out: draft_ok(out, body))


def write(s: WriteIn) -> dict:
    it = s["item"]
    body, body_src = extract_body(it)
    base = {"id": it["id"], "rank": it["rank"], "label": it["label"], "source": it["source"],
            "title": it["title"], "url": it["url"], "meta": it["meta"], "why_pick": it["why_pick"],
            "body_source": body_src, "body_len": len(body)}
    if not body:
        return {"drafted": [{**base, "status": "skip", "skip_reason": "본문도 피드 요약도 없음"}],
                "log": [f"   요약 스킵 {it['id']} · 본문 없음 · {it['title'][:30]}"]}
    try:
        d, usage = compose(it, body)
    except LLMError as ex:
        return {"drafted": [{**base, "status": "skip", "skip_reason": f"요약 LLM 실패: {ex}"}],
                "log": [f"   요약 스킵 {it['id']} · LLM 실패 · {it['title'][:30]}"]}
    if d.fit == "대상아님":                         # 본문을 읽어 보니 독자가 신청할 수 없다 — 보내지 않는다
        return {"drafted": [{**base, "status": "skip", "skip_reason": f"본문 확인 결과 대상 아님: {d.fit_reason}",
                             **d.model_dump(), "usage": usage}],
                "log": [f"   요약 스킵 {it['id']} · 대상 아님 · {d.fit_reason[:40]}"]}
    return {"drafted": [{**base, "status": "ok", "body": body, **d.model_dump(), "usage": usage}],
            "log": [f"   요약 {it['id']} · {body_src} {len(body)}자 · {d.fit} · {d.headline}"]}


# ---------------------------------------------------------------- 검수
# 두 겹으로 본다.
#  1) 코드 대조 — 인용(evidence · fit_quote)이 본문에 글자 그대로 있는가, 요약의 날짜가 본문에 있는가 → 어기면 불합격
#                 금액은 파생 계산(월 30만 원 × 6개월 = 180만 원)이 있을 수 있어 불합격 대신 LLM 에게 넘기는 '표시'로만 쓴다
#  2) LLM 대조 — 작성과 분리된 호출이 주장마다 본문 근거를 찾는다. 근거로 댄 구절도 본문에 있는지 코드가 다시 본다
# 불합격이면 문제를 넣어 한 번 재생성 → 재검수 → 그래도 불합격이면 스킵. 검수 호출이 실패하면 검수 안 된 기사는 보내지 않는다.

DATE_FULL = re.compile(r"(?:\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
MONEY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(억|천만|백만|만|천)?\s*원")
UNIT = {"억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3, None: 1}


COUNT = re.compile(r"(\d+)\s*(?:개월|회|년간|명|건|차례)")


def dates_in(text):
    return {(int(m), int(d)) for m, d in DATE_FULL.findall(text) + DATE_MD.findall(text)}


def money_in(text):
    return {int(float(n.replace(",", "")) * UNIT[u or None]) for n, u in MONEY.findall(text)}


def derived_money(body):
    """본문 금액 × 본문의 기간·횟수로 나오는 값 (월 30만 원 × 6개월 = 180만 원).
    2026-09-15 증명에서 검수 LLM 이 '계산한 값'이라고 적고도 근거 없음으로 판정했다 — 계산은 코드가 확인한다."""
    return {a * int(n) for a in money_in(body) for n in COUNT.findall(body)}


def grounded(quote, body):
    """검수자가 댄 근거 구절이 본문에 있는가. 날짜·금액 표기를 바꿔 옮긴 것도 인정한다"""
    if in_body(quote, body):
        return True
    dq, mq = dates_in(quote), money_in(quote)
    return bool(dq or mq) and dq <= dates_in(body) and mq <= money_in(body)


def facts_accounted(claim, body):
    """주장의 날짜·금액·숫자가 전부 본문에 있거나 본문 수치로 계산되는가"""
    if not dates_in(claim) <= dates_in(body):
        return False
    if not money_in(claim) <= money_in(body) | derived_money(body):
        return False
    rest = MONEY.sub(" ", DATE_MD.sub(" ", DATE_FULL.sub(" ", claim)))
    body_nums = set(re.findall(r"\d+", body))
    return set(re.findall(r"\d+", rest)) <= body_nums and bool(money_in(claim))


def written(d):
    ins = d["insight"]
    return f"{d['headline']}\n{d['summary']}\n{ins['action']}\n{ins['check']}\n{ins['gain']}"


def code_check(d, body):
    """(불합격 사유 목록, LLM 에게 넘길 표시 목록)"""
    fails, flags = [], []
    lost = [q for q in d["evidence"] if not in_body(q, body)]
    if lost:
        fails.append(f"evidence 인용이 본문에 없음: {[q[:30] for q in lost]}")
    if d["fit"] != "불명" and not in_body(d["fit_quote"], body):
        fails.append(f"fit_quote 가 본문에 없음: '{d['fit_quote'][:30]}'")
    extra_dates = dates_in(written(d)) - dates_in(body)
    if extra_dates:
        fails.append(f"본문에 없는 날짜: {sorted(f'{m}월 {x}일' for m, x in extra_dates)}")
    extra_money = money_in(written(d)) - money_in(body)
    calc = extra_money & derived_money(body)
    if calc:
        flags.append(f"본문 수치로 계산되는 금액(코드 확인): {sorted(calc)}원 — 근거 있음으로 볼 것")
    if extra_money - calc:
        flags.append(f"본문에도 없고 계산으로도 안 나오는 금액: {sorted(extra_money - calc)}원 — 특히 확인할 것")
    return fails, flags


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(description="작성물에서 뽑은 사실 주장 하나 (날짜 · 금액 · 대상 · 조건 · 지원내용 · 기관)")
    supported: bool = Field(description="본문에 근거가 있으면 true. 본문 수치로 정확히 계산되는 값도 true")
    quote: str = Field(description="근거가 된 본문 구절을 그대로. 근거가 없으면 빈 문자열")
    problem: str = Field(description="근거가 없거나 틀렸다면 무엇이 틀렸는지. 문제가 없으면 빈 문자열")


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[Claim] = Field(description="판정 전에 주장을 먼저 모두 뽑는다")   # ok 보다 먼저 — 결론부터 정하지 않게
    ok: bool = Field(description="모든 사실 주장에 근거가 있으면 true")


SYS_VERIFY = ("당신은 뉴스레터 검수자입니다. 작성물이 본문에 근거하는지만 판정하세요.\n"
              "- 작성물에서 사실 주장(날짜 · 금액 · 대상 · 자격 조건 · 지원내용 · 기관 · 신청방법)을 모두 뽑으세요\n"
              "- 주장마다 본문에서 근거 구절을 찾아 그대로 옮기세요. 없으면 supported=false\n"
              "- '~하세요' 같은 조언 자체는 판정하지 않습니다. 조언 안에 들어간 사실(날짜 · 조건)은 판정합니다\n"
              "- 본문 수치로 정확히 계산되는 값(월 30만 원 × 6개월 = 180만 원)은 근거 있음입니다\n"
              "- 날짜 표기 차이(2026.09.18 과 9월 18일)나 문장을 바꿔 쓴 것은 문제가 아닙니다\n"
              "- 코드 대조 표시가 있으면 그 항목을 특히 확인하세요")


def verdict_ok(v: Verdict):
    # 인용 글자 일치는 여기서 강제하지 않는다 — 2026-09-15 증명에서 날짜를 '9월 8일'로 바꿔 옮긴 것까지 거부해
    # 세 번 재시도 끝에 정상 요약이 '검수 불가'가 됐다. 인용은 judge 에서 느슨하게 대조해 경고로 남긴다.
    if not v.claims:
        return "사실 주장을 하나 이상 뽑으세요"
    if v.ok != all(c.supported for c in v.claims):
        return "ok 는 모든 주장이 supported 일 때만 true 입니다"
    return None


def judge(d, body):
    """한 번 검수한다 → (합격 여부, 기록, 사용량). 검수 LLM 이 끝내 실패하면 LLMError"""
    fails, flags = code_check(d, body)
    user = (f"[본문]\n{body}\n\n[작성물]\n{written(d)}\n\n[대상 판정] {d['fit']} — {d['fit_quote']}"
            + (f"\n\n[코드 대조 표시]\n" + "\n".join(flags) if flags else ""))
    v, usage = ask(Verdict, SYS_VERIFY, user, check=verdict_ok)
    unsupported, corrected = [], []
    for c in v.claims:
        if c.supported:
            continue
        if facts_accounted(c.claim, body):          # LLM 은 근거 없다 했지만 숫자가 전부 본문·계산으로 맞는다
            corrected.append(c.claim)
        else:
            unsupported.append(c)
    weak_quotes = [c.claim for c in v.claims if c.supported and not grounded(c.quote, body)]
    passed = not fails and not unsupported
    record_ = {"passed": passed, "code_fails": fails, "code_flags": flags, "claims": len(v.claims),
               "unsupported": [{"claim": c.claim, "problem": c.problem} for c in unsupported],
               "corrected_by_code": corrected, "llm_ok": v.ok, "weak_quotes": weak_quotes}
    return passed, record_, usage


def feedback_of(rec):
    return "\n".join([f"- {x}" for x in rec["code_fails"]] +
                     [f"- '{u['claim']}': {u['problem']}" for u in rec["unsupported"]])


def verify_one(d):
    """검수 → 불합격이면 1회 재생성 → 재검수 → 그래도 불합격이면 스킵"""
    body, usage, trail = d["body"], {}, []
    try:
        passed, rec, u = judge(d, body)
        usage = add_usage(usage, u)
        trail.append({"attempt": "검수", **rec})
        if passed:
            return {**d, "verify": {"result": "통과", "trail": trail, "usage": usage}}
        try:
            new, u = compose(d, body, feedback=feedback_of(rec))
            usage = add_usage(usage, u)
        except LLMError as ex:
            return {**d, "status": "skip", "skip_reason": f"검수 불합격 · 재생성 실패: {ex}",
                    "verify": {"result": "불합격", "trail": trail, "usage": usage}}
        d2 = {**d, **new.model_dump()}
        if d2["fit"] == "대상아님":
            return {**d2, "status": "skip", "skip_reason": f"재생성에서 대상 아님: {d2['fit_reason']}",
                    "verify": {"result": "불합격", "trail": trail, "usage": usage}}
        passed, rec, u = judge(d2, body)
        usage = add_usage(usage, u)
        trail.append({"attempt": "재생성 후 재검수", **rec})
        if passed:
            return {**d2, "verify": {"result": "재생성 후 통과", "trail": trail, "usage": usage}}
        return {**d2, "status": "skip", "skip_reason": "검수 불합격 (재생성 후에도)",
                "verify": {"result": "불합격", "trail": trail, "usage": usage}}
    except LLMError as ex:
        # 검수를 못 했으면 맞는지 모르는 것이다 — 보내지 않는 쪽이 덜 나쁘다
        return {**d, "status": "skip", "skip_reason": f"검수 불가: {ex}",
                "verify": {"result": "검수 불가", "trail": trail, "usage": usage}}


def verify(s: Brief) -> dict:
    out, counts = [], {}
    for d in sorted(s["drafted"], key=lambda x: x["rank"]):
        if d["status"] != "ok":
            out.append(d)
            continue
        v = verify_one(d)
        out.append(v)
        counts[v["verify"]["result"]] = counts.get(v["verify"]["result"], 0) + 1
    passed = [d for d in out if d["status"] == "ok"]
    short = len(passed) < TARGET_MIN
    stats = {"verify": {"checked": sum(counts.values()), "results": counts, "publishable": len(passed),
                        "below_min": short,
                        "usage": {k: sum(d.get("verify", {}).get("usage", {}).get(k, 0) for d in out)
                                  for k in ("calls", "input", "output", "retries")}}}
    lines = [f"⊙ 검수  {sum(counts.values())} → 발행 가능 {len(passed)}건 · {counts}"
             + (f" · ⚠ 최소 발행 수 {TARGET_MIN}건 미달 — 있는 만큼 보낸다" if short else "")]
    lines += [f"   검수 {d['id']} · {d['verify']['result']}" + (f" · {d['skip_reason'][:50]}" if d["status"] != "ok" else "")
              for d in out if "verify" in d]
    return {"verified": passed, "reviewed": out, "stats": stats, "log": lines}


# ---------------------------------------------------------------- 발행
# 텔레그램 봇. DRY_RUN 기본값은 1(보내지 않음) — 실수로 보내는 것보다 안 보내는 쪽이 덜 나쁘다.
# 한 메시지 4096자 한도 — 기사를 버리지 않고 여러 메시지로 나눈다.
TG_MAX = 3800


def esc(s):
    return html.escape(s or "", quote=False)


def article_block(n, d):
    ins = d["insight"]
    period = d["meta"].get("신청기간", "")
    return "\n".join([
        f"<b>{n}. <a href=\"{html.escape(d['url'])}\">{esc(d['headline'])}</a></b>",
        esc(d["summary"]),
        f"👉 <b>할 일</b> {esc(ins['action'])}",
        f"✅ <b>확인</b> {esc(ins['check'])}",
        f"💰 <b>얻는 것</b> {esc(ins['gain'])}",
        f"<code>{esc(d['source'])}" + (f" · 신청 {esc(period)}" if period else "") + "</code>",
    ])


def build_messages(today, items, below_min):
    head = f"🗂 <b>{today} · 1인 창조기업 지원 브리핑</b>"
    if not items:
        return [head + "\n오늘은 검수를 통과한 공고가 없습니다. 파이프라인은 정상 실행됐습니다."]
    lead = f"{len(items)}건 · 본문 확인과 자동 검수를 통과한 소식만 보냅니다."
    if below_min:
        lead += f"\n(오늘은 조건에 맞는 공고가 적어 {TARGET_MIN}건을 채우지 못했습니다)"
    msgs, cur = [], head + "\n" + lead
    for n, d in enumerate(items, 1):
        block = article_block(n, d)
        if len(cur) + 2 + len(block) > TG_MAX:
            msgs.append(cur)
            cur = block
        else:
            cur += "\n\n" + block
    msgs.append(cur)
    return msgs


def send_telegram(text):
    """(성공 여부, message_id 또는 오류). 429 는 retry_after 만큼 기다려 다시 보낸다"""
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    body = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    last = ""
    for attempt in range(TRIES):
        try:
            r = requests.post(url, json=body, timeout=TIMEOUT)
            data = r.json()
            if data.get("ok"):
                return True, data["result"]["message_id"]
            last = f"{r.status_code} {data.get('description', '')[:100]}"
            wait = data.get("parameters", {}).get("retry_after")
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(wait or 2 ** attempt)
                continue
            return False, last                      # 400·401·403 은 다시 보내도 같다
        except (requests.RequestException, ValueError) as ex:
            last = type(ex).__name__
            time.sleep(2 ** attempt)
    return False, last


def publish(s: Brief) -> dict:
    items = sorted(s["verified"], key=lambda d: d["rank"])
    below = s["stats"].get("verify", {}).get("below_min", len(items) < TARGET_MIN)
    msgs = build_messages(datetime.now(KST).strftime("%Y-%m-%d"), items, below and bool(items))
    dry = os.environ.get("DRY_RUN", "1") != "0"
    sent, failed = [], []
    if not dry:
        for m in msgs:
            ok, info = send_telegram(m)
            (sent if ok else failed).append(info)
    stats = {"publish": {"dry_run": dry, "articles": len(items), "messages": len(msgs),
                         "chars": [len(m) for m in msgs], "message_ids": sent, "failed": failed}}
    where = "dry-run · 보내지 않음" if dry else f"텔레그램 {len(sent)}/{len(msgs)}개 전달" + (f" · 실패 {failed}" if failed else "")
    return {"messages": msgs, "stats": stats, "log": [f"⊙ 발행  {len(items)}건 · 메시지 {len(msgs)}개 · {where}"]}


# ---------------------------------------------------------------- 그래프
def build():
    g = StateGraph(Brief)
    g.add_node("collect", collect)
    g.add_node("prelim", prelim)
    g.add_node("final", final)
    g.add_node("write", write)
    g.add_node("verify", verify)
    g.add_node("publish", publish)
    g.add_edge(START, "collect")
    g.add_edge("collect", "prelim")
    g.add_edge("prelim", "final")
    g.add_conditional_edges("final", fan_write, ["write", "publish"])   # 고른 게 없어도 "없음" 메시지는 보낸다
    g.add_edge("write", "verify")                  # 워커가 모두 끝나면 한 번 모인다
    g.add_edge("verify", "publish")
    g.add_edge("publish", END)
    return g.compile()


def run(hours: int = HOURS) -> dict:
    return build().invoke({"hours": hours, "collected": [], "survivors": [], "picked": [], "drafted": [],
                           "reviewed": [], "verified": [], "messages": [], "screened": [], "stats": {}, "log": []})
