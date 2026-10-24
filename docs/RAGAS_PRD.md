# RAGAS 평가 PRD

## 개요

- 대상: `YouthLink_RAG` (`question` + `profile` → `answer` + `policies` + `trace`)
- 범위: 단일 정책 질문
- 데이터셋: DB `policy` → `TestsetGenerator` → 검수 → `golden_vN.jsonl` (git 고정)
- 평가: K8s Job → 질문 INSERT → RAG 응답·점수 UPDATE
- 지표: Context Precision, Context Recall, Faithfulness, Answer Relevancy + 할루시네이션 비교용 Factual Correctness, Reference Faithfulness
- 모드: `rag` (기본), `baseline` (RAG 없이 LLM 직접 호출, 할루시네이션 기준선), `offline` (채점 경로 검증)

## 실행 모드

| 항목 | `rag` | `baseline` | `offline` |
| --- | --- | --- | --- |
| 명령 | `run` | `run --baseline` | `run --offline` |
| `answer` | RAG 응답 | LLM 직접 응답 | `ground_truth` |
| `contexts` | RAG 검색 결과 | `[]` | `reference_contexts` |
| 답변 호출 | RAG 서버 | OpenAI | 없음 |
| 채점 LLM 호출 | 있음 | 있음 | 있음 |
| 목적 | RAG 성능 측정 | RAG 도입 효과(할루시네이션 감소) 비교 | 평가 코드 검증 |
| 기대 결과 | - | `rag`보다 낮음 | 지표 1에 근접 |

- `offline`: 정답을 답변으로 넣어 채점 → 1에 근접하면 데이터셋 로드·채점·DB 저장 경로 정상, 크게 낮으면 우리 코드 오류
- 실행 순서: `offline`으로 평가 코드 검증 → `baseline`·`rag` 점수 신뢰
- 상세: "베이스라인 모드", "오프라인 모드", "RAG 연동 계약" 섹션

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
| `youthlink-data-pipeline` | 수동 실행, 전체 수집, `policy`·`policy_region`·`policy_eligibility_code`만 적재, 삭제 정책 미반영 |
| `policy_chunk` | 적재 코드 없음 |
| `YouthLink_RAG` | `POST /internal/pipeline` 구현, `?debug=true`의 `trace.contexts`·`retrieved_chunks` 반영 완료 (PR #15). 가짜 정책으로만 확인, 실제 `policy_chunk` 적재 대기 |
| `rag_ragas_evaluation` | `generate`, `run`(`rag`·`baseline`·`offline`, 재개), `report` 구현. `golden_v0`(시험용 10건)으로 offline·baseline 측정 완료, rag 모드는 실제 데이터 대기 |

## RAGAS 입력

| 필드 | RAGAS 인자 | 출처 | 시점 |
| --- | --- | --- | --- |
| `question` | `user_input` | `TestsetGenerator` `user_input` | 데이터셋 생성 |
| `ground_truth` | `reference` | `TestsetGenerator` `reference` → 검수 | 데이터셋 생성 |
| `contexts` | `retrieved_contexts` | RAG `trace.contexts` (LLM에 넘긴 근거 블록) | 평가 (UPDATE) |
| `answer` | `response` | RAG `answer` | 평가 (UPDATE) |

## 데이터셋 생성

| 항목 | 값 |
| --- | --- |
| 대상 | `description` 비어 있지 않음(NULL·공백 제외), `application_period_type_code`가 마감(0057003) 아님(마감 정책은 종료일이 비어 있음), `application_end_date` NULL 또는 기준일 당일 이후 |
| 원천 텍스트 | `policy_name`, `description`, `support_content`, 지역(`v_policy_region_resolved`의 시도·시군구 이름. 시군구 3곳까지 나열, 넘으면 시도, 17개 시도 전부면 "전국". View가 없으면 생략), `min_age`/`max_age`(하한 1세 이하·상한 99세 이상은 API가 채운 "제한 없음" 값으로 보고 생략), `income_condition_code`(무관 / 연소득 `min_income`~`max_income` 만원 / 기타 `income_etc`), `application_start_date`/`application_end_date`(상시 0057002면 "상시"), `application_method`, `submission_documents`, `screening_method`, `additional_qualification`, `participation_exclusion` |
| 입력 변환 | 정책 1건 → `Document` 1개 (`metadata`: `policy_no`, `source_updated_at`) |
| 정책 표본 | `--policy-limit N`: 정책 번호순 정렬 후 고정 시드로 N건 추출 (분석 비용 제한, 재실행 시 같은 표본 → 분석 캐시 재사용) |
| 페르소나 | 청년정책 사용자 5명 고정 (대학생, 취업준비생, 사회초년생, 청년 창업자, 신혼부부 청년). 페르소나가 정책 대상이 아니면 정답이 대상 여부부터 밝히도록 질문·정답 생성 프롬프트에 지시 추가 |
| 생성기 | `TestsetGenerator(llm, embedding_model).generate_with_langchain_docs(docs, testset_size, query_distribution)` |
| 질문 유형 | `[(SingleHopSpecificQuerySynthesizer(llm), 1.0)]` |
| 한국어 | 생성기 프롬프트 한국어 adapt |
| 모델 | `GEN_MODEL`, `EMBEDDING_MODEL` (`bedrock-titan-embed-v2`) |
| `policy_no` 복원 | `reference_contexts`가 포함된 `Document.page_content` 매칭 (생성기가 문서를 분할할 수 있어 일치가 아닌 포함 검사) |
| 사전 검증 | 10건 시험 생성 (짧은 문서 누락, 한국어 품질) |
| 검수 | `reviewed = true`만 사용 |
| 규모 | 100건 내외 |
| 저장 | `datasets/golden_vN.jsonl` (git, DB 미저장) |

| jsonl 필드 | 타입 | 출처 |
| --- | --- | --- |
| `sample_id` | str | 생성 시 부여 (`single-000123`) |
| `policy_no` | str | 매칭 |
| `question` | str | `user_input` |
| `ground_truth` | str | `reference` |
| `reference_contexts` | list[str] | `reference_contexts` |
| `synthesizer_name` | str | `synthesizer_name` |
| `profile` | object | `{}` |
| `source_updated_at` | str (ISO 8601) | `Document.metadata` |
| `reviewed` | bool | 검수 |

## 평가 지표

| 지표 | 클래스 (`ragas.metrics.collections`, v0.4) | 입력 | 모델 |
| --- | --- | --- | --- |
| Context Precision | `ContextPrecision` | `user_input`, `retrieved_contexts`, `reference` | LLM |
| Context Recall | `ContextRecall` | `user_input`, `retrieved_contexts`, `reference` | LLM |
| Faithfulness | `Faithfulness` | `user_input`, `retrieved_contexts`, `response` | LLM |
| Answer Relevancy | `AnswerRelevancy` | `user_input`, `response` | LLM + 임베딩 |
| Factual Correctness | `FactualCorrectness` | `response`, `reference` | LLM |
| Reference Faithfulness | `Faithfulness` | `user_input`, `response`, `retrieved_contexts` ← `reference_contexts` | LLM |

- Factual Correctness: 답변 주장 vs `ground_truth` 주장 대조 (틀린 정보)
- Reference Faithfulness: 검색 결과 대신 정책 원문(`reference_contexts`) 기준 충실도 (지어낸 정보)
- 두 지표는 검색 여부와 무관해 `rag`·`baseline` 공통 비교 기준

| 지표 | `rag` | `baseline` | `offline` |
| --- | --- | --- | --- |
| Context Precision, Context Recall, Faithfulness | O | NULL (검색 없음, `errors` 미기록) | O |
| Answer Relevancy, Factual Correctness, Reference Faithfulness | O | O | O |

| 항목 | 값 |
| --- | --- |
| 채점 LLM | `llm_factory(<judge_model>, client=AsyncOpenAI())` |
| 호출 | `await metric.ascore(...)` → `result.value` |
| 실패 | 해당 지표 NULL, `metadata.errors` 기록 |
| 채점 프롬프트 | `answer_relevancy`는 고정 한국어 프롬프트 (영어 기본 프롬프트는 한국어 답변에서 영어 질문을 만들어 유사도가 낮아짐). LLM 번역(adapt)은 실행마다 문구가 달라져 쓰지 않음 |
| 프롬프트 버전 | `run.metadata.scoring_prompt` (`answer_relevancy-ko-v1`). 버전이 다른 run은 재개 불가, 보고서 모드별 비교에서 따로 집계 |

## Answer Coverage 추가 계획 (예정)

`factual_correctness`는 정답 밖 내용을 사실이어도 감점한다. 정답 내용을 다 말했는지만 보는 지표를 별도 컬럼으로 추가한다. 기존 지표는 유지한다.

| 근거 (golden_v1 재채점 실험, 3회씩) | 정답 그대로 | 정답 + 원문 사실 2개 | 정답 + 원문 사실 4개 |
| --- | --- | --- | --- |
| `factual_correctness` | 1.0 | 0.67 ~ 0.88 | 0.57 ~ 0.80 |
| 커버리지 (정답 → 답변만) | 1.0 | 1.0 | 1.0 |

- 답변 쪽 주장에 정답에 없는 사업명이 붙으면 `factual_correctness` 답변 쪽 판정이 전부 X → F1 0 (092, 102). 커버리지는 정답 쪽 판정만 써서 영향 없음
- ragas `mode="recall"`은 tp를 답변 쪽 판정에서 가져와(`metric.py`) 같은 문제가 남아 쓰지 않음

| 항목 | 값 |
| --- | --- |
| 지표명 | `answer_coverage` |
| 질문 | 답변이 정답(`ground_truth`)의 사실을 모두 말했는가 |
| 입력 | `response`, `reference` |
| 계산 | ① `reference`를 주장 단위로 분해 ② 각 주장이 `response`에서 직접 추론되는지 판정 (0/1) ③ 1의 개수 ÷ 주장 수 |
| 프롬프트 | ragas `ClaimDecompositionPrompt`, `NLIStatementPrompt` (`ragas.metrics.collections.factual_correctness.util`) 그대로. 새 프롬프트 작성 없음 |
| 분해 옵션 | `atomicity="high"`, `coverage="high"`. coverage low는 정답 정보 일부를 주장에서 버려 누락을 못 잡고, atomicity low는 묶인 주장 하나만 빠져도 통째로 X가 됨. `factual_correctness`는 기존 run과 비교를 위해 기본값(low/low) 유지 |
| 호출 | 샘플당 채점 LLM 2회 (`factual_correctness`는 4회) |
| 구현 | `scorer.py`에 프롬프트 클래스를 직접 호출하는 지표 클래스 추가. `FactualCorrectness`의 `_decompose_claims`·`_verify_claims`(내부 함수) 호출은 ragas 버전 변경에 깨질 수 있어 쓰지 않음 |
| 실패 | 주장 0개, 판정 수 불일치, 예외 → NULL + `metadata.errors` (0점 아님) |
| 모드 | `rag`, `baseline`, `offline` 모두 (검색 결과 불필요). `offline` 기대값 1 근접 |
| 짝 지표 | `reference_faithfulness`. 커버리지는 지어낸 덧붙임을 못 잡으므로 둘을 함께 해석 |

| 커버리지 | `reference_faithfulness` | 해석 |
| --- | --- | --- |
| 높음 | 높음 | 정답을 다 말했고 덧붙인 내용도 원문상 사실 |
| 높음 | 낮음 | 정답은 말했지만 지어낸 내용을 덧붙임 |
| 낮음 | 높음 | 사실만 말했지만 정답 내용을 빠뜨림 |
| 낮음 | 낮음 | 정답을 빠뜨리고 지어냄 |

| DB 변경 | 타입 | 제약 |
| --- | --- | --- |
| `ragas_evaluation_samples.answer_coverage` | NUMERIC(4,3) NULL | CHECK 0 ~ 1 |
| `ragas_evaluation_run.avg_answer_coverage` | NUMERIC(4,3) NULL | |

- 추가 방식: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (재실행 안전). 기존 행은 NULL
- 같은 커밋에서 `db/ragas_schema.sql`, `docs/Schema.md`, 이 PRD의 "DB 추가 테이블" 표 갱신

| 영향 범위 | 변경 |
| --- | --- |
| `scorer` | `METRIC_NAMES`, `METRICS_BY_MODE`, `ScoreResult` 지표 7개, `build_metrics`, `score_sample` 입력 |
| `repository` | 샘플 점수 UPDATE, run 집계 `avg_answer_coverage` |
| `runner` | 재개 시 "지표 전부 NULL" 판정에 포함 |
| `report` | 지표 평균 7개, 모드별 비교, `rag - baseline` 차이 |
| `tests` | 채점 LLM mock으로 "덧붙여도 1.0", "절반 누락 0.5", "주장 0개 → NULL", 모드별 적용 확인 |

| 비교·재개 규칙 | 값 |
| --- | --- |
| 채점 버전 | `run.metadata.scoring_prompt`를 `answer_relevancy-ko-v1+answer_coverage-v1`로 올림 |
| 재개 | 추가 전 run은 버전이 달라 재개 불가 (일부 샘플만 커버리지가 채워지는 것 방지) |
| 보고서 | 추가 전 run과 따로 집계. golden_v1 기존 run(`67905db0…`, `282df708…`)은 커버리지 NULL |
| 이전 run | 수정하지 않음. 커버리지가 필요하면 새 run으로 실행 |

| 결정 필요 | 선택지 |
| --- | --- |
| golden_v1 커버리지 측정 | ① 새 `rag`·`baseline` run 실행 (답변 LLM + 채점 비용) ② 저장된 답변으로 새 run을 만들어 채점만 하는 명령 추가 (답변 비용 없음, 기능 추가 필요) |

## 평가 실행 순서

| 순서 | 테이블 | 작업 | 컬럼 |
| --- | --- | --- | --- |
| 1 | `ragas_evaluation_run` | INSERT | `run_id`, `dataset_version`, `repeat_no`, 모델 정보, `status = 'RUNNING'` |
| 2 | `ragas_evaluation_samples` | INSERT (`reviewed = true`) | `sample_id`, `policy_no`, `question`, `ground_truth` |
| 3 | `ragas_evaluation_samples` | UPDATE (RAG 응답) | `answer`, `contexts`, `latency_ms`, `metadata.tokens`, `metadata.retrieved_chunks` |
| 4 | `ragas_evaluation_samples` | UPDATE (채점) | 모드별 지표 (최대 6개), `metadata.errors` |
| 5 | `ragas_evaluation_run` | UPDATE (집계) | `avg_*`, `sample_count`, `finished_at`, `status` |

- 3단계 응답 출처: `rag` → RAG 호출, `baseline` → LLM 직접 호출, `offline` → `ground_truth`

- `--repeat 3` → 1~5를 3회 (run 3행)
- 이전 run 덮어쓰기 없음
- 재개: `answer IS NULL` → 3부터, 지표 전부 NULL → 4부터
- 재개 대상: `RUNNING`으로 남은 run만 (Pod 강제 종료·시간 초과). `FAILED` run은 재개하지 않고 새 run으로 다시 실행 (이전 run 수정 금지, run 1개 = 같은 조건 1회 실행)
- 재개 방법: `run --resume <run_id>` 명시 (`--repeat`와 함께 불가). 채점만 남은 샘플은 저장된 응답 재사용
- 재개 조건: `mode`, `dataset_version`, `judge_model`, `embedding_model` (baseline은 `BASELINE_MODEL`, `prompt_version`도), golden 질문·정답이 run 시작 때와 같을 것. 다르면 새 run
- RAG 재시도: 연결 오류·타임아웃·5xx만 2회 (1초, 2초 대기). 4xx·계약 위반은 재시도 없음. baseline·채점은 OpenAI SDK 재시도 2회
- 응답 실패 (재시도 후): `answer` NULL 유지, `sample_id`·에러 종류만 로그 → 나머지 계속 → 미완료가 있으면 집계하지 않고 `RUNNING`으로 남김 (종료 코드 1, 남은 반복 중단)
- 예기치 못한 오류 (DB·코드): 진행 중 작업 취소 후 `FAILED`
- RAG 설정 변경 감지: 응답의 `chat_model`·`retriever`·`top_k`가 run에 기록된 값과 다르면 (RAG 재배포) `FAILED`
- 커밋 단위: run·samples INSERT 1회, 샘플별 응답 UPDATE·채점 UPDATE마다 (중단돼도 진행분 보존)
- 성적표: `ragas_evaluation_run.avg_*` (실행별), `ragas_evaluation_samples` (샘플별)

## RAG 연동 계약

| 항목 | 값 |
| --- | --- |
| 호출 | `POST {RAG_BASE_URL}/internal/pipeline?debug=true` (`contexts`·`retrieved_chunks` 같은 무거운 trace는 debug 요청에만 포함) |
| 요청 | `PipelineInput` = `{question, profile}` |
| 응답 | `PipelineOutput` = `{answer, policies[], trace}` |
| `trace` 필수 키 | `contexts` (list[str]), `retrieved_chunks` (`chunk_id`, `policy_no`, `score`, 선택 `content`), `latency_ms.total`, `tokens.prompt`·`tokens.completion`, `chat_model`, `retriever`, `top_k` |
| `retrieved_contexts` | `trace.contexts` 그대로 (RAG가 LLM에 넘긴 정책별 근거 블록, 검색 순위 순). 청크 본문만이 아니라 신청 상태·방법·서류·기관·링크까지 포함해야 Faithfulness가 정확함 |
| 저장 | `samples.contexts` = `[{"content": c} for c in trace.contexts]`, `samples.latency_ms` = `latency_ms.total`, `metadata.tokens` = `{prompt_tokens, completion_tokens}`, `metadata.retrieved_chunks` = `trace.retrieved_chunks` 원본 |
| run 기록 | `trace.chat_model`·`retriever`·`top_k` → `RagResult` → `ragas_evaluation_run.rag_chat_model`·`rag_retriever`·`rag_top_k` |

## 오프라인 모드 (`run --offline`)

| 항목 | 값 |
| --- | --- |
| 목적 | RAG 없이 데이터셋 → 채점 → DB 저장 경로 검증 |
| RAG 호출 | 없음 |
| `answer` | `ground_truth` |
| `contexts` | `reference_contexts` → `[{"content": c} for c in reference_contexts]` |
| `latency_ms` | NULL |
| run 기록 | `mode = 'offline'`, `rag_chat_model`·`rag_retriever`·`rag_top_k` NULL |
| 기대 결과 | 지표 6개 1에 근접. 크게 낮으면 채점·매핑 오류 |
| 비용 | 채점 LLM 호출 발생 (사용자 확인 후 실행) |

## 베이스라인 모드 (`run --baseline`)

| 항목 | 값 |
| --- | --- |
| 목적 | RAG 도입 전후 할루시네이션 비교 (같은 질문, 같은 LLM, 검색 유무만 다름) |
| RAG 호출 | 없음 (RAG 서버 미구현 상태에서도 실행 가능) |
| `answer` | `BASELINE_MODEL`에 질문만 전달한 응답 (`AsyncOpenAI` 직접 호출) |
| `BASELINE_MODEL` | RAG `chat_model`과 같은 모델 (공정 비교) |
| 프롬프트 | RAG 생성 프롬프트(`generate_v1.md`)에서 컨텍스트만 뺀 문구. RAG 프롬프트 확정 전에는 임시 문구 |
| `contexts` | `[]` |
| `latency_ms`, `metadata.tokens` | OpenAI 응답 기준 기록 |
| run 기록 | `mode = 'baseline'`, `rag_chat_model = BASELINE_MODEL`, `rag_retriever = 'none'`, `rag_top_k` NULL, `metadata.prompt_version` |
| 비교 | 같은 `dataset_version`의 `rag` run과 `avg_factual_correctness`, `avg_reference_faithfulness`, `avg_answer_relevancy` 비교 |
| 기대 결과 | `baseline` < `rag` (차이 = RAG의 할루시네이션 감소 효과) |
| 비용 | 답변 LLM + 채점 LLM 호출 발생 (사용자 확인 후 실행) |
| 제약 | `--offline`과 함께 쓸 수 없음 |

## 결과 내보내기 (`report`)

| 항목 | 값 |
| --- | --- |
| 목적 | 채점 결과를 노션·디스코드로 공유 |
| 입력 | `ragas_evaluation_run`, `ragas_evaluation_samples` (읽기만) |
| 명령 | `python -m ragas_eval report --run <run_id> [--out-dir results]` |
| 내용 | 요약 (상태·시각·샘플 수), 실행 조건 (모델·검색·프롬프트 버전), 지표 평균 6개, 같은 `dataset_version` 완료 run의 모드별 평균과 `rag - baseline` 차이, 미응답·채점 실패 수, `factual_correctness` 낮은 샘플 10건 |
| 출력 | `results/<dataset_version>/<dataset_version>_<mode>_r<repeat_no>_<run_id>.md` (같은 run은 덮어씀) |
| 노션·디스코드 | 추후 요청 시 연동 (토큰·웹훅 URL은 `.env` → `Settings`) |
| 비용 | LLM 호출 없음 |
| 직접 조회 | SQL (`README.md` 참고 쿼리) |

## DB 추가 테이블 (`db/ragas_schema.sql`)

### ragas_evaluation_run

| 컬럼 | 타입 | NULL | 기본값 | 제약 |
| --- | --- | --- | --- | --- |
| run_id | UUID | NOT NULL | gen_random_uuid() | PK |
| dataset_version | VARCHAR(50) | NOT NULL | | |
| repeat_no | INTEGER | NOT NULL | 1 | CHECK >= 1 |
| mode | VARCHAR(20) | NOT NULL | 'rag' | CHECK IN ('rag', 'baseline', 'offline') |
| rag_chat_model | VARCHAR(100) | NULL | | |
| rag_retriever | VARCHAR(50) | NULL | | |
| rag_top_k | INTEGER | NULL | | |
| judge_model | VARCHAR(100) | NOT NULL | | |
| embedding_model | VARCHAR(100) | NOT NULL | | |
| status | VARCHAR(20) | NOT NULL | 'RUNNING' | CHECK IN ('RUNNING', 'SUCCEEDED', 'FAILED') |
| sample_count | INTEGER | NULL | | |
| avg_context_precision | NUMERIC(4,3) | NULL | | |
| avg_context_recall | NUMERIC(4,3) | NULL | | |
| avg_faithfulness | NUMERIC(4,3) | NULL | | |
| avg_answer_relevancy | NUMERIC(4,3) | NULL | | |
| avg_factual_correctness | NUMERIC(4,3) | NULL | | |
| avg_reference_faithfulness | NUMERIC(4,3) | NULL | | |
| avg_answer_coverage | NUMERIC(4,3) | NULL | | |
| started_at | TIMESTAMPTZ | NOT NULL | NOW() | |
| finished_at | TIMESTAMPTZ | NULL | | |
| metadata | JSONB | NOT NULL | '{}'::jsonb | |

### ragas_evaluation_samples

| 컬럼 | 타입 | NULL | 기본값 | 제약 |
| --- | --- | --- | --- | --- |
| id | BIGSERIAL | NOT NULL | | PK |
| run_id | UUID | NOT NULL | | FK → ragas_evaluation_run.run_id (CASCADE), UNIQUE(run_id, sample_id) |
| sample_id | VARCHAR(50) | NOT NULL | | |
| policy_no | VARCHAR(30) | NULL | | FK 없음 |
| question | TEXT | NOT NULL | | |
| answer | TEXT | NULL | | |
| ground_truth | TEXT | NULL | | |
| contexts | JSONB | NOT NULL | '[]'::jsonb | 채점 근거 `[{"content": ...}]` |
| context_precision | NUMERIC(4,3) | NULL | | CHECK 0 ~ 1 |
| context_recall | NUMERIC(4,3) | NULL | | CHECK 0 ~ 1 |
| faithfulness | NUMERIC(4,3) | NULL | | CHECK 0 ~ 1 |
| answer_relevancy | NUMERIC(4,3) | NULL | | CHECK 0 ~ 1 |
| factual_correctness | NUMERIC(4,3) | NULL | | CHECK 0 ~ 1 |
| reference_faithfulness | NUMERIC(4,3) | NULL | | CHECK 0 ~ 1 |
| answer_coverage | NUMERIC(4,3) | NULL | | CHECK 0 ~ 1 |
| latency_ms | INTEGER | NULL | | |
| created_at | TIMESTAMPTZ | NOT NULL | NOW() | |
| metadata | JSONB | NOT NULL | '{}'::jsonb | |

| 인덱스 | 테이블 | 컬럼 |
| --- | --- | --- |
| idx_ragas_run_started | ragas_evaluation_run | started_at |
| idx_ragas_run_mode | ragas_evaluation_run | mode, dataset_version |
| idx_ragas_samples_run | ragas_evaluation_samples | run_id |
| idx_ragas_samples_policy | ragas_evaluation_samples | policy_no |

### 제시안 대비 변경

| 변경 | 이유 |
| --- | --- |
| `ragas_evaluation_run` 분리 + `run_id` FK | 실행 조건 비교, 반복 묶음 |
| `SERIAL` → `BIGSERIAL` | 행 누적 |
| `policy_no` 추가 (FK 없음) | 정책별 분석, 정책 변경과 무관하게 보존 |
| `contexts`에 채점 근거, `metadata.retrieved_chunks`에 검색 결과 원본 | 재채점·재개 시 같은 근거 사용, 검색 순위·점수 재분석 |
| 지표 CHECK 0 ~ 1, NULL 허용 | 채점 실패와 0점 구분 |
| `latency_ms` 추가 | 응답 속도 집계 |
| `mode` 컬럼 (`metadata.mode` 대신) | `rag`·`baseline` run 필터·비교 |
| `factual_correctness`, `reference_faithfulness` 추가 | 검색 없는 `baseline`과 공통 비교 지표 |
| `answer_coverage` 추가 (`ALTER ... ADD COLUMN IF NOT EXISTS`) | 정답 밖 사실을 감점하지 않는 정답 누락 지표. 기존 행은 NULL |

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
- [x] 2-4. `scorer` + 테스트 (`AsyncMock`, NaN → NULL)
- [x] 2-5. `repository` + 테스트 (mock, `db` 마커 통합, `mode`·지표 6개)
- [x] 2-6. `scorer` 지표 6개 확장 + 모드별 적용 지표
- [x] 2-7. `baseline_client` + 테스트 (`AsyncMock` OpenAI)
- [x] 3-1. `generate` + 테스트
- [x] 3-2. `runner` + 테스트 (`--repeat`, `--offline`, `--baseline`, 재개, 실패 시 `FAILED`)
- [ ] 4-1. `Dockerfile` (Python 3.12, `datasets/` 포함)
- [ ] 4-2. `k8s/ragas-eval-job.yaml`
- [x] 5-1. `report` 패키지 뼈대
- [x] 5-2. `report` 마크다운 성적표 + 테스트 (`results/`, `baseline` 비교, `report` 서브커맨드)
- [ ] 5-3. 노션·디스코드 전송 (요청 시)
- [x] 6-1. `answer_coverage` 컬럼 추가 (`db/ragas_schema.sql`, `docs/Schema.md`, PRD "DB 추가 테이블"), 임시 PostgreSQL 2회 적용 확인
- [x] 6-2. `scorer` 커버리지 지표 + 테스트 (덧붙임 1.0, 절반 누락 0.5, 주장 0개 NULL, 분해 옵션 high/high 전달)
- [ ] 6-3. `repository`·`runner` 저장·집계·재개 반영 + 테스트
- [ ] 6-4. `report` 지표 7개 + 테스트
- [ ] 6-5. `scoring_prompt` 버전 올림, 이전 run 재개 차단 테스트
- [ ] 6-6. `run --offline`으로 커버리지 1 근접 확인 (채점 비용, 사용자 확인 후)

## 운영 체크리스트

- [ ] 1. `db/ragas_schema.sql` 적용
- [ ] 2. `policy` → `Document` → `TestsetGenerator`로 `question`, `ground_truth` 생성
- [ ] 3. 검수 → `golden_v1.jsonl` 고정
- [ ] 4. `run --offline`으로 채점 경로 검증
- [ ] 4-1. `run --baseline --repeat 3`으로 할루시네이션 기준선 기록
- [ ] 5. run INSERT, 질문 samples INSERT
- [ ] 6. RAG 호출 → `answer`, `contexts` UPDATE
- [ ] 7. 채점 → 모드별 지표 UPDATE
- [ ] 8. 집계 → run UPDATE
- [ ] 9. 3회 반복 → 평균·노이즈 폭 기록
- [ ] 10. `baseline` vs `rag` 비교 (Factual Correctness, Reference Faithfulness, Answer Relevancy)
