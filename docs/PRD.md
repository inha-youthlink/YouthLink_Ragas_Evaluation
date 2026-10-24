# 평가 저장소 공통 PRD

평가 저장소 전체에 공통인 평가 종류·역할·흐름·RAG 연동 계약·코드 구조·실행 환경을 둔다.

## 평가 종류

| 평가 | 보는 것 | 데이터셋 | 채점 | 문서 |
| --- | --- | --- | --- | --- |
| 답변 품질 평가 | 최종 답변이 정확한가, 빠뜨리거나 지어내지 않았나 | `golden_vN` | RAGAS (LLM) | `docs/RAGAS_PRD.md` |
| 검색 적중률 평가 | 정답 정책을 찾아서 보여 줬나 | `golden_vN` (답변 품질과 같은 run) | 코드 | `docs/RETRIEVE_PRD.md` |
| 라우팅 평가 | 질문 종류를 맞게 이해했나 (유형, 카드 지목, 맥락 유지) | `routing_vN` | 코드 | `docs/RETRIEVE_PRD.md` |

- 1차 필터는 따로 평가하지 않고, 답변 품질·검색 적중률 결과를 질문에 조건이 있는지로 나눠 본다

```
질문 → [라우팅] → [검색 → 후보 → 카드] → [답변 생성] → 사용자
        라우팅 평가     검색 적중률 평가           답변 품질 평가 (끝에서 끝까지)
```

- 답변 품질 평가는 결과가 좋은지, 나머지 둘은 나쁠 때 어디서 틀렸는지를 본다
- 답변 품질 점수는 검색이 틀려도 함께 떨어진다. 생성 단계만 보려면 카드까지 적중한 샘플로 좁혀 본다
- 답변 품질의 `baseline` 비교로 RAG 도입 효과(RAG 없는 LLM 대비)를 잰다

| 라우팅 | 검색 적중 | 답변 품질 | 원인 |
| --- | --- | --- | --- |
| 틀림 | - | 낮음 | 질문 분석 |
| 맞음 | 빠짐 | 낮음 | 검색·청킹 |
| 맞음 | 카드까지 | 낮음 | 답변 생성 |
| 맞음 | 카드까지 | 높음 | 정상 |

## 역할 분담

| 역할 | 담당 | 작업 | DB |
| --- | --- | --- | --- |
| ETL | 홍용준 | 온통청년 API → `policy` UPSERT | 쓰기 |
| 데이터셋 | 박용우 | `policy` → 생성 → 검수 → `golden_vN.jsonl` | 읽기 `policy` |
| 평가 실행 | 박용우 | Job 실행 → 결과 저장 | 쓰기 `ragas_evaluation_*` |
| RAG | 이도경 | 개선 → 배포 → 평가 요청 | 읽기 `policy_chunk` |
| `policy_chunk` 적재 | 홍용준 | 청킹·임베딩 → `policy_chunk` | 쓰기 |

## 전체 흐름

| 단계 | 주체          | 빈도 | 입력 → 출력 |
| --- |-------------| --- | --- |
| 1. 정책 적재 | 홍용준         | 수시 | API → `policy` |
| 2. 데이터셋 생성 | 박용우 (로컬)    | 최초 1회, 버전업 시 | `policy` → `golden_candidates.jsonl` |
| 3. 검수·고정 | 박용우 (로컬 검수) | 2 직후 | → `golden_vN.jsonl` (git) |
| 4. RAG 개선·배포 | 이도경         | 개선 시마다 | → RAG 서버 (dev) |
| 5. 평가 실행 | K8s Job     | 요청 시 | `golden_vN.jsonl` + RAG 응답 → 점수 |
| 6. 결과 확인 | 이도경         | 5 직후 | `ragas_evaluation_run`, `ragas_evaluation_samples` |

## 의존 관계

| 연결 | 조건 |
| --- | --- |
| ETL → 데이터셋 | ETL 적재 완료 후 생성 |
| RAG 배포 → 평가 | Job은 `RAG_BASE_URL`에 배포된 버전을 평가 |
| ETL → 평가 | `source_updated_at` 변경 샘플 제외. 제외 증가 시 `golden_vN+1` |

## 현재 상태

| 구성 | 상태 |
| --- | --- |
| `youthlink-data-pipeline` | 수동 실행, 전체 수집, 삭제 정책 미반영. `policy_chunk`는 섹션 청크 `policy-v1` (정책당 최대 4개: SUMMARY·ELIGIBILITY·SUPPORT·APPLICATION) |
| `YouthLink_RAG` | `POST /internal/pipeline`. 질문 분석(intent·검색어·조건 추출), 1차 필터링, 지역·전국 분리 검색 반영. 핸들러는 `default_rag` 하나 (라우터·전용 핸들러 8주차 작업 중) |
| `rag_ragas_evaluation` | `generate`, `run`(`rag`·`baseline`·`offline`, 재개), `report` 구현. golden_v1으로 `rag`·`baseline` 1회 측정. `answer_coverage` 추가. 검색·라우팅 평가는 `docs/RETRIEVE_PRD.md` |
| 로컬 개발 DB `policy_chunk` | 정책당 1청크 임시 적재본 (golden_v1 측정 기준). 섹션 청크와 다름 |

## RAG 연동 계약

| 항목 | 값 |
| --- | --- |
| 호출 | `POST {RAG_BASE_URL}/internal/pipeline?debug=true` (`contexts`·`retrieved_chunks` 같은 무거운 trace는 debug 요청에만 포함) |
| 요청 | `PipelineInput` = `{question, profile}` |
| 응답 | `PipelineOutput` = `{answer, policies[], trace}` |
| `trace` 필수 키 | `contexts` (list[str]), `retrieved_chunks` (`chunk_id`, `policy_no`, `score`, 선택 `content`), `latency_ms.total`, `tokens.prompt`·`tokens.completion`, `chat_model`, `retriever`, `top_k` |
| `trace` 선택 키 | `cards` (`candidate_nos` list[str]), `analysis` (`tokens`), `handler` (str), `conditions`, `filtered_policy_nos` (list[str] 또는 null, debug 요청에만), 응답 `policies[].policy_no`. 있으면 형식만 확인 |
| `retrieved_contexts` | `trace.contexts` 그대로 (RAG가 LLM에 넘긴 정책별 근거 블록, 검색 순위 순). 청크 본문만이 아니라 신청 상태·방법·서류·기관·링크까지 포함해야 Faithfulness가 정확함 |
| 저장 | `samples.contexts` = `[{"content": c} for c in trace.contexts]`, `samples.latency_ms` = `latency_ms.total`, `metadata.tokens` = `{prompt_tokens, completion_tokens}` + 분석 토큰 `{analysis_prompt_tokens, analysis_completion_tokens}`, `metadata.retrieved_chunks` = `trace.retrieved_chunks` 원본, `metadata.cards`·`analysis`·`handler`·`conditions`·`filtered_policy_nos` = trace 원본, `metadata.card_policy_nos` = `policies[].policy_no` (`cards`가 있을 때만) |
| run 기록 | `trace.chat_model`·`retriever`·`top_k` → `RagResult` → `ragas_evaluation_run.rag_chat_model`·`rag_retriever`·`rag_top_k` |

## 코드 구조 (`rag_ragas_evaluation/`)

| 경로 | 역할 |
| --- | --- |
| `ragas_eval/__main__.py` | 진입점 |
| `ragas_eval/cli.py` | `generate`, `run` |
| `ragas_eval/config.py` | `DATABASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `RAG_BASE_URL`, `GEN_MODEL`, `JUDGE_MODEL`, `EMBEDDING_MODEL`, `BASELINE_MODEL`, `OPENAI_TIMEOUT`, `OPENAI_MAX_RETRIES` |
| `ragas_eval/policy_source/policy_source.py` | `policy` → `Document` |
| `ragas_eval/generate/generate.py` | `TestsetGenerator` → `policy_no` 복원 → `golden_candidates.jsonl` |
| `ragas_eval/dataset/dataset.py` | jsonl 로드, `reviewed` 필터, `source_updated_at` 검사, `GoldenSample` 타입 |
| `ragas_eval/rag_client/rag_client.py` | RAG 호출, `RagResult` 타입 |
| `ragas_eval/baseline_client/baseline_client.py` | LLM 직접 호출 → `RagResult` (`retrieved_contexts = []`) |
| `ragas_eval/scorer/scorer.py` | 모드별 지표 `ascore`, `ScoreResult` 타입 |
| `ragas_eval/repository/repository.py` | run/samples INSERT·UPDATE |
| `ragas_eval/runner/runner.py` | 실행 순서 1~5, `--repeat`, `--offline`, `--baseline`, `--resume` |
| `ragas_eval/report/report.py` | 결과 조회 → 마크다운 성적표 (`results/`) |
| `db/ragas_schema.sql` | DDL |
| `datasets/golden_vN.jsonl` | 확정 데이터셋 |
| `k8s/ragas-eval-job.yaml` | 평가 Job |
| `tests/` | 변환·저장 단위 테스트 |

## 명령어

- `ragas_eval`: 프로젝트 자체 패키지 (RAGAS 기본 명령어 아님)
- `python -m ragas_eval generate --testset-size 100 --out datasets/golden_candidates.jsonl`
- `python -m ragas_eval run --golden datasets/golden_v1.jsonl --repeat 3`
- `python -m ragas_eval run --golden datasets/golden_v1.jsonl --offline`
- `python -m ragas_eval run --golden datasets/golden_v1.jsonl --baseline --repeat 3`
- `python -m ragas_eval run --golden datasets/golden_v1.jsonl --resume <run_id>`
- `python -m ragas_eval report --run <run_id>`

## 실행 환경

| 항목 | 값 |
| --- | --- |
| 형태 | K8s Job (`generate`는 로컬) |
| 데이터셋 | Job 이미지에 `datasets/` 포함 |
| `backoffLimit` | 0 |
| `activeDeadlineSeconds` | 3600 |
| 시크릿 | `OPENAI_API_KEY`, `DATABASE_URL` |
| 의존 | RAG Service (`RAG_BASE_URL`, `rag` 모드만) |
| 동시성 | `asyncio.Semaphore(5)` |

## 선행 작업

| 항목 | 위치 | 담당 |
| --- | --- | --- |
| `policy_chunk` 청킹·임베딩 적재 | `youthlink-data-pipeline` | 홍용준 |
| 파이프라인 구현 (`runner`, `retrieval/vector`, `generator`) | `YouthLink_RAG` | 이도경 |
| `/internal/pipeline` trace (`contexts` 포함) | `YouthLink_RAG/app/api/internal.py` | 이도경 |
| `load_policies.py` `__main__` 들여쓰기 수정 | `youthlink-data-pipeline` | 홍용준 |

## 구현 계획

| 단계 | 작업 | 방식 | 선행 |
| --- | --- | --- | --- |
| 1. 기반 | `pytest.ini`, `conftest.py`, 공유 타입 | 순차 | - |
| 2. 독립 모듈 | `policy_source`, `dataset`, `rag_client`, `baseline_client`, `scorer`, `repository` | 병렬 6 | 1 |
| 3. 조합 모듈 | `generate`, `runner` | 병렬 2 | `generate` ← `policy_source`, `dataset` / `runner` ← `dataset`, `rag_client`, `baseline_client`, `scorer`, `repository` |
| 4. 배포 | `Dockerfile`, `k8s/ragas-eval-job.yaml` | 순차 | 3 |

| 공유 타입 | 위치 | 사용처 |
| --- | --- | --- |
| `GoldenSample` | `dataset/dataset.py` | `generate`, `runner` |
| `RagResult` (`answer`, `retrieved_contexts`, `chunks`, `retrieved_chunks`, `latency_ms`, `tokens`, `chat_model`, `retriever`, `top_k`) | `rag_client/rag_client.py` | `baseline_client`, `runner` |
| `ScoreResult` (지표 6개, `errors`) | `scorer/scorer.py` | `runner`, `repository` |

## 구현 체크리스트

- [x] 1-1. `pytest.ini` (`db`, `llm` 마커, 기본 `-m "not llm"`)
- [x] 1-2. `tests/conftest.py` (가짜 정책·샘플 fixture)
- [x] 1-3. `GoldenSample`, `RagResult`, `ScoreResult` 정의
- [x] 2-1. `policy_source` + 테스트
- [x] 2-2. `dataset` + 테스트
- [x] 2-3. `rag_client` + 테스트 (`httpx.MockTransport`)
- [x] 2-5. `repository` + 테스트 (mock, `db` 마커 통합, `mode`·지표 6개)
- [x] 2-7. `baseline_client` + 테스트 (`AsyncMock` OpenAI)
- [x] 3-1. `generate` + 테스트
- [x] 3-2. `runner` + 테스트 (`--repeat`, `--offline`, `--baseline`, 재개, 실패 시 `FAILED`)
- [ ] 4-1. `Dockerfile` (Python 3.12, `datasets/` 포함)
- [ ] 4-2. `k8s/ragas-eval-job.yaml`
- [x] 5-1. `report` 패키지 뼈대
- [x] 5-2. `report` 마크다운 성적표 + 테스트 (`results/`, `baseline` 비교, `report` 서브커맨드)
- [ ] 5-3. 노션·디스코드 전송 (요청 시)
