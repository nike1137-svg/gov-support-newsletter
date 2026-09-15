# gov-support-newsletter — 프로젝트 규칙

전역 규칙(`~/.claude/CLAUDE.md`)을 그대로 따르되, 이 프로젝트에만 해당하는 내용을 여기 적는다.

## 이 프로젝트는

1인 창조기업 운영자에게 매일 아침 지원사업 공고와 창업 소식을 수집 · 선별 · 요약 · 검수해 텔레그램으로 보내는 에이전트.
2026-09-15 아이펠 AI에이전트 1기 실습 「나만의 뉴스레터 에이전트 구축하기」 제출작.
전날 만든 `C:\Users\nike1\projects\active\newsletter-agent`(주간 운영용)의 5단계 구조를 바탕으로 **새로** 만들었다 — 두 저장소를 서로 맞추지 않는다.

- 저장소: https://github.com/nike1137-svg/gov-support-newsletter (**공개** — 제출이 깃헙 주소라서)
- 구조와 실행법은 [README.md](README.md), 설계 판단과 근거 수치는 [REPORT.md](REPORT.md). **먼저 읽을 것**
- 구현 중 결함과 고친 방법은 `docs/dev-log.md`, 소스 기준은 `docs/source-criteria.md`

## 지킬 것

- **과제 원문대로.** 필수 5단계 · 제출물 구조 · REPORT 6항목은 과제 이미지의 이름과 순서를 그대로 쓴다. 과제에 없는 것을 덧붙이지 않는다
- **키는 `.env` 에만.** 마커스님이 메모장으로 직접 넣는다. 확인할 때 값은 출력하지 않는다 (길이 · 형식 · 호출 결과만)
- **공개 저장소다.** 커밋 · push 전에 비밀값 검사를 한다 (`.env` 값과 키 패턴을 `git log -p --all` 에서 찾기)
- 기사 **본문은 커밋하지 않는다** (저작권). `run.py` 가 저장할 때 `body` 를 뺀다
- `DRY_RUN` 기본값 1. 실제 발행은 `run.py --send` 또는 Actions
- LLM 호출이 드는 시험 전에 호출 수와 비용을 먼저 말한다. 오프라인 검증(`tools/verify_*.py`)으로 먼저 거른다
- 실패한 실행의 `store/runs/*.json` 과 `metrics.jsonl` 줄은 지우고 다시 돌린다. 무엇이 틀렸는지는 `docs/dev-log.md` 에 남긴다
- LLM 판정을 믿지 말고 코드로 대조한다 — 순번 대신 ID+제목, 판정 근거는 본문 인용, 인원 조건·계산 금액은 코드
- **키 누락을 흉내 낼 때 PowerShell `$env:X = ''` 를 쓰지 않는다** — 변수를 지워 `.env` 의 진짜 키가 읽힌다. 공백 한 칸으로 두거나 `tools/verify_env.py` 를 쓴다
- `--send` 가 들어간 시험은 토큰을 비워 두는 등 **가드가 실패해도 보낼 수 없게** 설계한다

## 발행 경로

```
매일 08:10 KST (노트북 작업 스케줄러 → tools/daily.py)  →  텔레그램 @SoloBizBriefinggBot (g 두 개)
Run workflow (수동, GitHub)                            →  텔레그램 (dry_run 체크 시 보내지 않음)
로컬 run.py --send                                      →  텔레그램
```

**GitHub Actions 예약은 뺐다.** 해외 실행 서버에서 `bizinfo.go.kr` · `mss.go.kr` 에 연결이 안 된다
(2026-09-15 17시 · 09-16 08시 ConnectTimeout, 같은 시각 한국 노트북에서는 0.6초에 응답).
매일 발행은 노트북이 맡고, Actions 는 손으로 시험할 때만 쓴다. **둘 다 켜 두면 같은 날 두 번 발송된다.**

채팅 ID 는 `newsletter-agent` 와 같은 마커스님 개인 채팅. 봇은 다르다 (`newsletter-agent` 는 `@SoloBizBriefingBot`).
Secrets: `OPENAI_API_KEY` · `BIZINFO_API_KEY` · `TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID`

## 다음에 할 일 (REPORT 6장 한계에서)

1. 독자 유용성 판정 — 사실은 맞지만 쓸모없는 공고(뿌리산업 푸드트럭)가 발행된다
2. 기준별 최소 몫 — 본선이 매일 기준1(공고)만 고른다
3. 지역 필터 — `audience.yaml` 에 독자 지역
4. 며칠치 `store/metrics.jsonl` 로 단계별 통과율 추이 · 묶음 크기 10/20/40 비교
5. 평일 창으로 소스 재측정 (K-Startup · 중소기업뉴스 `中企`)
