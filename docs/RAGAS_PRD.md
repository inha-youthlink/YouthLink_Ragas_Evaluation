# 답변 품질 평가 PRD (RAGAS)

답변 품질 평가(RAGAS 채점) 기능만 둔다. 평가 종류 전체와 역할·흐름·RAG 연동 계약·코드 구조·실행 환경은 `docs/PRD.md`.

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
- 상세: "베이스라인 모드", "오프라인 모드" 섹션, `docs/PRD.md` "RAG 연동 계약"

## RAGAS 입력

| 필드 | RAGAS 인자 | 출처 | 시점 |
| --- | --- | --- | --- |
| `question` | `user_input` | `TestsetGenerator` `user_input` | 데이터셋 생성 |
| `ground_truth` | `reference` | `TestsetGenerator` `reference` → 검수 | 데이터셋 생성 |
| `contexts` | `retrieved_contexts` | RAG `trace.contexts` (LLM에 넘긴 근거 블록) | 평가 (UPDATE) |
| `answer` | `response` | RAG `answer` | 평가 (UPDATE) |

## 데이터셋 생성

golden은 RAG가 검색해서 정답 정책을 찾아야만 답할 수 있는 정책 질문으로만 구성한다.

| 구성 기준 | 내용 |
| --- | --- |
| 질문 | 정책 문서 하나의 구체적인 내용(지원 금액, 자격 조건, 신청 방법 등)을 묻는 질문. 일반 상식으로 답할 수 있는 질문은 넣지 않음 |
| 정답 | 그 정책 원문에서 뽑은 문장 |
| 기대 경로 | 모든 샘플이 검색 핸들러(`trace.handler = "search"`)로 가서 근거로 답하는 것. 그래서 샘플마다 기대 유형 필드를 두지 않음 |
| 기대 경로와 다른 응답 | 짧은 답변(`short`)은 RAG 분류 오류, 되묻기(`clarify`)는 프로필·조건 추출 또는 질문 모호성 문제로 보고 "되묻기 응답"·"짧은 답변" 기준으로 처리 |
| 검수에서 제외 | 모호한 질문, 같은 이름의 정책이 여러 개인 질문, 정답이 틀린 질문 |
| 검색 필요성 확인 | `baseline`(RAG 없이 LLM만) 점수가 `rag`보다 크게 낮아야 함. baseline 점수가 높은 샘플은 상식으로 답할 수 있는 질문인지 검토 |
| 범위 밖 | 질문 유형 분류(인사·범위 밖·비교 등)는 `routing_vN`(기대 유형 포함, `docs/RETRIEVE_PRD.md`), 되묻기가 정답인 질문은 아직 없음 |

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
| `profile` | object | 생성 시 `{}`, `profile` 명령으로 채움 ("프로필 채우기"). 키는 RAG `Profile` 필드만 허용 (`age`, `gender`, `region_code` 5자리, `marriage_status_code`, `annual_income` 원 단위, `major_code`, `job_code`, `school_code`, `special_target_code`). 모르는 키·형식 오류는 로드 실패, 빈 값은 RAG 요청에서 뺌 |
| `source_updated_at` | str (ISO 8601) | `Document.metadata` |
| `reviewed` | bool | 검수 |

## 프로필 채우기 (`profile`)

RAG는 정책을 프로필로만 거르고, 질문의 지역·나이가 프로필과 다르면 되묻는다. 프로필이 빈 샘플은 1차 필터·되묻기를 거치지 않는 경로만 재므로, 정답 정책 조건에 맞춘 프로필을 채운다. 새 데이터셋은 `generate` 후보에 프로필을 붙인 뒤 검수해 한 번에 확정한다 (`generate` → `profile` → 검수 → `golden_vN`). 이미 프로필 없이 확정된 버전(`golden_v1`)은 확정본에 프로필을 붙여 다음 버전으로 만든다.

| 항목 | 값 |
| --- | --- |
| 명령 | `python -m ragas_eval profile --golden datasets/golden_candidates.jsonl [--out datasets/golden_profile_candidates.jsonl]` (프로필 없는 확정본에 붙일 때는 `--golden datasets/golden_vN.jsonl`) |
| 입력 | golden 형식 jsonl(`generate` 후보 또는 확정본) + DB `policy`(`min_age`, `max_age`), `policy_region`(`region_code` 앞 2자리). 읽기 전용 |
| 출력 | 같은 형식의 후보 jsonl (`profile`만 채움). 확정본(`golden_vN.jsonl`)에는 쓰지 않음 |
| 채우는 항목 | `region_code`, `age`만. 취업 상태·학력 등은 질문 속 페르소나와 어긋나면 답변 끝에 차이 안내가 붙어 정답과 멀어지므로 채우지 않음 |
| 지역 | 시도 전체 코드(`XX000`). 로컬 DB에 `region` 테이블이 없어 시군구는 쓰지 않음 (시군구 필터는 검증 범위 밖) |
| 지역 선택 | ① 질문에 시도 이름이 하나 있고 정책 지역 안이면 그 시도 ② 정책 시도가 하나면 그 시도 ③ 여러 시도(전국 포함)면 샘플 순서로 돌려 씀 |
| 지역 비움 | 질문의 시도가 정책 지역 밖이거나 여러 개, `광주시`(시도를 정할 수 없음), 여러 시도 정책인데 질문에 `~시/군/구` 꼴 단어가 있음, 정책 시도를 RAG가 모름 |
| 나이 | 기본 25세를 정책 나이 범위에 맞춤. 질문에 `N0대 초반/중반/후반`이 있으면 N2/N5/N8세. 하한 1세 이하·상한 99세 이상은 제한 없음 |
| 나이 비움 | 질문의 나이대가 정책 나이 범위와 겹치지 않음, 질문에 나이대가 여러 개, 정책 하한 나이 > 상한 나이. `120대`처럼 앞이 숫자면 나이대로 보지 않음 |
| 기존 프로필 | `region_code`·`age`만 바꾸고 다른 필드는 그대로 둠. 출력 경로가 입력 golden과 같으면 거부 |
| 프로필 없음 | 정답이 "자격이 안 된다" 판정(`대상이 아닙니다` 등), DB에 정책 없음 |
| 검수 표시 | 위의 비움·없음, 나이대를 범위에 맞춰 바꿈, 질문에 `NN세`(자격 범위인지 사용자 나이인지 검수에서 판단. 사용자 나이면 프로필과 같아야 되묻지 않음) → `reviewed = false` + 이유 출력. 후보는 원래 모두 `reviewed = false`라 검수 대상은 출력된 이유 목록으로 구분 |
| 확정 | `ragas-dataset-review` 스킬 검수에서 질문·정답과 검수 표시 샘플의 프로필을 함께 판정하고, 사용자 승인 후 `golden_vN.jsonl`로 확정 (프로필 없는 확정본에서 시작했으면 `golden_vN+1.jsonl`) |
| 비용 | LLM 호출 없음 |

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

## Answer Coverage

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

| 측정 데이터셋 | 값 |
| --- | --- |
| golden_v1 | 측정하지 않음 (기존 run 재채점 명령을 만들지 않음) |
| golden_v2 | 검수 후 새 run에서 지표 7개와 함께 측정 |

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
- 재개: `answer IS NULL` → 3부터, 지표 전부 NULL → 4부터 (되묻기 응답은 제외, 아래 "되묻기 응답")
- 재개 대상: `RUNNING`으로 남은 run만 (Pod 강제 종료·시간 초과). `FAILED` run은 재개하지 않고 새 run으로 다시 실행 (이전 run 수정 금지, run 1개 = 같은 조건 1회 실행)
- 재개 방법: `run --resume <run_id>` 명시 (`--repeat`와 함께 불가). 채점만 남은 샘플은 저장된 응답 재사용
- 재개 조건: `mode`, `dataset_version`, `judge_model`, `embedding_model` (baseline은 `BASELINE_MODEL`, `prompt_version`도), golden 질문·정답이 run 시작 때와 같을 것. 다르면 새 run
- RAG 재시도: 연결 오류·타임아웃·5xx만 2회 (1초, 2초 대기). 4xx·계약 위반은 재시도 없음. baseline·채점은 OpenAI SDK 재시도 2회
- 응답 실패 (재시도 후): `answer` NULL 유지, `sample_id`·에러 종류만 로그 → 나머지 계속 → 미완료가 있으면 집계하지 않고 `RUNNING`으로 남김 (종료 코드 1, 남은 반복 중단)
- 예기치 못한 오류 (DB·코드): 진행 중 작업 취소 후 `FAILED`
- RAG 설정 변경 감지: 응답의 `chat_model`·`retriever`·`top_k`·`embedding_model`이 run에 기록된 값(`rag_*` 컬럼, `metadata.rag_embedding_model`)과 다르면 (RAG 재배포) `FAILED`. 답변 프롬프트(`trace.prompt_version`)는 답변 종류마다 달라 감지하지 않고 샘플 `metadata.prompt_version`에 남긴다
- 커밋 단위: run·samples INSERT 1회, 샘플별 응답 UPDATE·채점 UPDATE마다 (중단돼도 진행분 보존)
- 성적표: `ragas_evaluation_run.avg_*` (실행별), `ragas_evaluation_samples` (샘플별)

### 되묻기 응답

RAG가 검색하지 않고 고정 문구로 되묻는 응답(`trace.handler = "clarify"`)은 근거·카드가 없어 정답과 비교할 답변이 아니다.

| 항목 | 처리 |
| --- | --- |
| 종류 (`trace.clarify_reason`) | `condition`: 질문의 지역·나이가 프로필과 달라 되물음. `policy`: 가리킨 정책이 애매해 고르게 함 |
| 저장 | 3단계(응답)는 그대로 저장 (`metadata.handler`·`clarify_reason`) |
| 채점 | 4단계를 건너뜀. RAGAS 지표·검색 적중률 모두 NULL, `metadata.errors`에 남기지 않음 (실패가 아님) |
| 집계 | `avg_*`는 NULL을 빼므로 자동 제외. `sample_count`에는 포함 |
| 재개 | 지표 전부 NULL이어도 채점 대상으로 보지 않음 |
| 보고서 | "되묻기 응답" 섹션에 이유별 건수·목록. 점수 낮은 샘플에서 제외 |

프로필이 질문 내용과 맞게 만들어진 데이터셋이라면 조건 되묻기는 0건이어야 한다. 나오면 프로필(데이터셋) 또는 RAG의 조건 추출을 확인한다.

### 짧은 답변

질문 분석이 인사·범위 밖으로 분류하면 RAG는 검색 없이 근거 없는 짧은 답변을 낸다 (`trace.handler = "short"`, `contexts` = `[]`). golden은 정책 질문뿐이라 나오면 분석 오분류다.

| 항목 | 처리 |
| --- | --- |
| 채점 | 답변 지표(`answer_relevancy`·`factual_correctness`·`reference_faithfulness`·`answer_coverage`)만. 검색 결과로 재는 `context_precision`·`context_recall`·`faithfulness`는 NULL, `metadata.errors`에 남기지 않음. 검색 결과 0건(`contexts` = `[]`, 검색 핸들러)은 그대로 채점 |
| 검색 적중률 | 계산하지 않음 (검색하지 않음) |
| 재개 | 저장된 `metadata.handler`로 같은 기준 적용 |
| 보고서 | "짧은 답변" 섹션에 목록. 답변 지표가 있어 점수 낮은 샘플에는 포함 |

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
| 내용 | 요약 (상태·시각·샘플 수), 실행 조건 (모델·검색·RAG 임베딩 모델·프롬프트 버전. `rag`는 샘플의 답변 프롬프트 조합을 모아 보임), 지표 평균 6개, 검색 적중률 (`rag` run만, `docs/RETRIEVE_PRD.md`), 되묻기 응답 (있을 때만), 짧은 답변 (있을 때만), 같은 `dataset_version` 완료 run의 모드별 평균과 `rag - baseline` 차이, 미응답·채점 실패 수, `factual_correctness` 낮은 샘플 10건 (되묻기 제외) |
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
| mode | VARCHAR(20) | NOT NULL | 'rag' | CHECK IN ('rag', 'baseline', 'offline', 'routing') |
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
| `mode`에 `routing` 추가 (`DROP CONSTRAINT IF EXISTS` → `ADD CONSTRAINT`) | 라우팅 평가 run 저장 (`docs/RETRIEVE_PRD.md`). 제약만 다시 만들고 행은 바꾸지 않음. `judge_model`·`embedding_model`은 `'none'` |

## 구현 체크리스트

- [x] 2-4. `scorer` + 테스트 (`AsyncMock`, NaN → NULL)
- [x] 2-6. `scorer` 지표 6개 확장 + 모드별 적용 지표
- [x] 6-1. `answer_coverage` 컬럼 추가 (`db/ragas_schema.sql`, `docs/Schema.md`, PRD "DB 추가 테이블"), 임시 PostgreSQL 2회 적용 확인
- [x] 6-2. `scorer` 커버리지 지표 + 테스트 (덧붙임 1.0, 절반 누락 0.5, 주장 0개 NULL, 분해 옵션 high/high 전달)
- [x] 6-3. `repository`·`runner` 저장·집계·재개 반영 + 테스트
- [x] 6-4. `report` 지표 7개 + 테스트
- [x] 6-5. `scoring_prompt` 버전 올림, 이전 run 재개 차단 테스트
- [ ] 6-6. golden_v2 `run --offline`으로 커버리지 1 근접 확인 (채점 비용, 사용자 확인 후)
- [x] 7-1. 되묻기 응답 채점 제외 (`runner`), 재개 대상 제외 (`repository`), 보고서 섹션 (`report`) + 테스트
- [x] 7-2. 7-1 DB 통합 테스트를 임시 PostgreSQL에서 실행
- [x] 7-3. 짧은 답변(`handler = "short"`) 검색 지표·검색 적중률 제외 (`scorer`·`runner`, 재개 포함), 보고서 섹션 + 테스트 (임시 PostgreSQL)
- [x] 7-4. RAG 버전 기록: 샘플 `metadata.prompt_version`, `run.metadata.rag_embedding_model` + 재배포 감지, 보고서 실행 조건 + 테스트 (임시 PostgreSQL, DDL 변경 없음)
- [x] 8-1. `GoldenProfile`: golden `profile`을 RAG `Profile` 필드로 검사 (모르는 키·형식 오류 로드 실패), RAG 요청에는 채운 필드만 + 테스트
- [x] 8-2. `profile` 명령: 정답 정책의 시도·나이로 프로필 채운 후보 생성, 검수 표시 + 테스트
- [ ] 8-3. golden_v1 프로필 후보 검수 → `golden_v2.jsonl` 확정 (사용자 승인 후)
