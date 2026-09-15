# 1인 창업 지원 뉴스 브리핑 에이전트

1인 창조기업 운영자에게 매일 아침 07:30, 지원사업 공고와 창업 소식을 **수집 → 선별 → 요약·인사이트 → 자동 검수 → 텔레그램 발행**하는 LangGraph 에이전트.

설계 판단 · 소스 채택 근거 · 실행 기록은 **[REPORT.md](REPORT.md)** 에 있다.

## 파일

```
graph.py              LangGraph 워크플로 (노드 6개 · State)
run.py                실행 스크립트 → store/metrics.jsonl · store/runs/
audience.yaml         타깃 독자 · 중요도 기준 · 제외 조건
requirements.txt      의존성
store/metrics.jsonl   실행마다 단계별 수치 · 토큰
REPORT.md             보고서

docs/source-criteria.md   소스 채택 기준 v1/v2 와 측정 결과
docs/dev-log.md           구현 중 드러난 결함과 고친 방법
tools/probe_sources.py    소스 후보 측정
tools/prove_verify.py     검수가 틀린 요약을 걸러내는지 증명 (실제 LLM)
tools/verify_*.py         단계별 오프라인 검증 (LLM 없음)
.github/workflows/daily.yml   매일 07:30 KST 실행 · 기록 커밋
```

## 실행

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # 키를 채운다
.venv\Scripts\python.exe run.py --dry-run   # 보내지 않고 메시지만 출력
.venv\Scripts\python.exe run.py --send      # 텔레그램으로 발행
```

GitHub Actions 에서는 저장소 Secrets(`OPENAI_API_KEY` · `BIZINFO_API_KEY` · `TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID`)로 같은 이름을 읽는다.

## 비용

LLM(`gpt-4.1-mini`) 1회 실행 약 $0.03. 기업마당 API · 중기부 RSS · 텔레그램 봇 · 공개 저장소 Actions 는 무료.
