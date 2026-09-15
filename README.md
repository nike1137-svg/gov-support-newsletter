# 1인 창업 지원 뉴스 브리핑 에이전트

혼자 사업을 준비하거나 막 시작한 **1인 창조기업 운영자**에게, 매일 아침 07:30 지원사업 공고와 창업 소식을
**수집 → 선별 → 요약·인사이트 → 자동 검수 → 텔레그램 발행**하는 LangGraph 에이전트.

> 「나만의 뉴스레터 에이전트 구축하기」 과제 제출작 · 설계 판단과 근거 수치는 **[REPORT.md](REPORT.md)**

| 바로 보기 | |
|---|---|
| 📄 보고서 | [REPORT.md](REPORT.md) — 분야·독자 / 소스 채택표 / 선별 로직 / 구조도 / 실행 기록 / 회고 |
| ▶️ 최종 발행 실행 | [GitHub Actions run 34934828691](https://github.com/nike1137-svg/gov-support-newsletter/actions/runs/34934828691) — 수집부터 발행까지 성공 · 텔레그램 수신 확인 |
| ✅ 검수 증명 | [store/verify_proof.json](store/verify_proof.json) — 틀리게 바꾼 요약 4종 모두 불합격 · 8/8 |

## 받아 보는 메시지

기사마다 **요약 · 할 일 · 확인할 조건 · 얻는 것**을 붙이고, 본문 확인과 자동 검수를 통과한 것만 보낸다.

<p>
  <img src="docs/telegram-1.png" alt="텔레그램 수신 화면 1" width="330">
  <img src="docs/telegram-2.png" alt="텔레그램 수신 화면 2" width="330">
</p>

<sub>캡처는 이름을 정하기 전이라 머리말이 "1인 창조기업 지원 브리핑"이다. 지금은 "1인 창업 지원 뉴스 브리핑".</sub>

## 5단계 구성

| 단계 | 노드 (`graph.py`) | 하는 일 | 근거 · 검증 |
|---|---|---|---|
| 1. 수집 | `collect` | 채택 소스 4곳에서 직전 24시간 · 주소/제목 중복 제거 · 한 소스가 죽어도 계속 | [소스 기준·측정](docs/source-criteria.md) · `tools/verify_collect.py` |
| 2. 선별 | `prelim` → `final` | 예선: 코드 규칙(마감·대기업 전용) + LLM 15건 묶음 라벨링 → 본선: 5건 순위와 탈락 이유 | `store/runs/*.json` 의 기사별 라벨 · `tools/verify_select.py` |
| 3. 요약·인사이트 | `write` × N (병렬) | 본문 추출 → 인원 조건 코드 규칙 → 대상 판정(본문 인용) → 요약 · 할 일/확인/얻는 것 | `tools/verify_write.py` |
| 4. 검수·예외 처리 | `verify` | 코드 대조(인용·날짜·계산 금액) + 분리된 LLM 검수 → 불합격이면 1회 재생성 → 그래도 불합격·검수 불가면 스킵 | `tools/prove_verify.py` (실제 LLM, 8/8) · `tools/verify_verify.py` |
| 5. 발행 | `publish` | 텔레그램 HTML · 4096자 분할 · 429 재시도 · 0건이면 "없음" 알림 · 전달 실패 시 종료 코드 1 | Actions 실행 기록 |

### 채택 소스

13곳을 같은 기준으로 측정해 4곳을 골랐다 ([측정 결과](docs/source-criteria.md)).

| 소스 | 형태 | 역할 |
|---|---|---|
| 기업마당 지원사업정보 | API (인증키) | 지원사업 공고 — 지원대상 · 신청기간 · 분야 라벨 제공 |
| 중소벤처기업부 사업공고 | RSS | 원천 공고 |
| 중소벤처기업부 보도자료 | RSS | 정책 소식 |
| 스타트업투데이 | RSS | 창업 소식 |

과제 제외 소스 6곳(OpenAI · DeepMind · TechCrunch · The Verge · MIT TR · AI타임스)은 후보에 넣지 않았고, 수집 전에 코드가 주소를 검사해 걸리면 멈춘다.

## 기술 스택

| 영역 | 사용 |
|---|---|
| 워크플로 | `LangGraph` (StateGraph · Send 병렬 워커) |
| LLM | `gpt-4.1-mini` · temperature 0 · 구조화 출력 (`pydantic` 스키마) · HTTP 직접 호출 |
| 수집 · 본문 | `requests` · `feedparser` (RSS) · `trafilatura` (본문) · 기업마당 전용 추출기 |
| 설정 | `PyYAML` (`audience.yaml`) · `python-dotenv` |
| 실행 · 발행 | GitHub Actions (매일 07:30 KST) · Telegram Bot API |

## 프로젝트 구조

```
gov-support-newsletter/
├── graph.py                     LangGraph 워크플로 — State · 노드 6개 · 그래프
├── run.py                       실행 스크립트 → store/metrics.jsonl · store/runs/
├── audience.yaml                타깃 독자 · 중요도 기준 · 버릴 조건 · 제외 소스
├── requirements.txt             의존성 (버전 고정)
├── REPORT.md                    보고서
├── CLAUDE.md                    AI 코딩 에이전트용 프로젝트 규칙 · 인계 메모
├── .env.example                 환경변수 이름 (값은 .env 에 · 커밋 안 됨)
├── .github/workflows/daily.yml  매일 07:30 KST 실행 · 기록 커밋
│
├── store/
│   ├── metrics.jsonl            실행마다 단계별 수치 · 라벨 건수 · 토큰
│   ├── runs/                    실행마다 기사 · 라벨과 이유 · 검수 기록 · 보낸 메시지 (본문은 저작권상 제외)
│   ├── source_probe_v1.json     소스 측정 원자료 (기준 v1, 4곳)
│   ├── source_probe.json        소스 측정 원자료 (기준 v2, 13곳)
│   └── verify_proof.json        검수 증명 결과
│
├── docs/
│   ├── source-criteria.md       소스 채택 기준 v1 → v2 와 측정 결과표
│   ├── dev-log.md               2~5단계 구현 중 드러난 결함과 고친 방법
│   ├── ai-collab.md             AI 협업 기록 — 역할 분담 · 실제 요청 · AI 가 틀린 곳
│   └── telegram-1.png · telegram-2.png   수신 화면
│
└── tools/
    ├── probe_sources.py         소스 후보 측정 (기준 C1~C5)
    ├── prove_verify.py          검수가 틀린 요약을 걸러내는지 증명 (실제 LLM · 약 $0.02)
    ├── verify_env.py            필수 키가 비면 시작 전에 멈추는지 ┐
    ├── verify_collect.py        수집 검증 (기업마당 키·인터넷 필요) │
    ├── verify_select.py         선별 검증                        │ LLM 없음 · 비용 0
    ├── verify_write.py          요약·인사이트 검증                 │
    └── verify_verify.py         검수 코드 대조 검증                ┘
```

## 실행 방법

Windows PowerShell 기준.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env       # 메모장으로 열어 키를 채운다

.venv\Scripts\python.exe run.py --dry-run   # 보내지 않고 메시지만 출력
.venv\Scripts\python.exe run.py --send      # 텔레그램으로 발행

.venv\Scripts\python.exe tools\verify_select.py   # 오프라인 검증 (verify_env · collect · write · verify 도 같은 방식)
```

**필수 키가 하나라도 비어 있으면 `run.py` 는 시작 전에 멈추고 어떤 키가 없는지 알려준다** (종료 코드 1).
텔레그램 키는 `--send` 일 때만 요구한다. `tools/verify_collect.py` 는 LLM 은 부르지 않지만 기업마당 키와 인터넷이 필요하다.

### 환경변수

| 이름 | 필수 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | ○ | 선별 · 요약 · 검수 |
| `BIZINFO_API_KEY` | ○ | 기업마당 API 인증키 (기업마당에서 사용신청) |
| `TELEGRAM_BOT_TOKEN` | ○ | BotFather 에서 발급 |
| `TELEGRAM_CHAT_ID` | ○ | 받을 채팅의 숫자 ID |
| `KSTARTUP_API_KEY` | ✕ | 소스 측정 도구만 사용 (파이프라인에서는 탈락 소스) |
| `DRY_RUN` | ✕ | 기본 `1` = 보내지 않음. `run.py --send` 가 우선 |

GitHub Actions 에서는 필수 4개를 저장소 **Secrets** 에 같은 이름으로 넣는다.

## 진행 상황

| 단계 | 상태 | 확인 방법 |
|---|---|---|
| 1. 수집 — 소스 13곳 측정 · 4곳 채택 | ✅ | `store/source_probe.json` · `tools/verify_collect.py` 4/4 |
| 2. 선별 — 예선/본선 · 기사별 라벨 | ✅ | `store/runs/` · `tools/verify_select.py` 4/4 |
| 3. 요약·인사이트 — 대상 판정 · 할 일/확인/얻는 것 | ✅ | `tools/verify_write.py` 4/4 |
| 4. 검수·예외 처리 — 재생성 · 스킵 · 검수 불가 미발행 | ✅ | `tools/prove_verify.py` 8/8 · `tools/verify_verify.py` 4/4 |
| 5. 발행 — 텔레그램 · 매일 07:30 KST 자동 실행 | ✅ | Actions 엔드투엔드 성공 · 수신 확인 |
| 보고서 · 제출 | ✅ | `REPORT.md` · 2026-09-15 제출 |
| 제출 후 새 클론 점검 — 키 누락 시 가짜 "정상" 수정 | ✅ | `tools/verify_env.py` 4/4 · [dev-log](docs/dev-log.md#제출-후--새-클론-점검) |

알려진 한계와 보완 방향은 [REPORT.md 6장](REPORT.md#6-프로젝트-회고).

## 비용

1회 실행에 LLM 호출 약 17~20회 · 약 **$0.03** (`gpt-4.1-mini`). 매일 실행하면 한 달 약 $1.
기업마당 API · 중기부 RSS · 텔레그램 봇 · 공개 저장소의 GitHub Actions 는 무료.
