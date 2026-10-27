# rag_ragas_evaluation

YouthLink RAG를 RAGAS로 평가합니다. 상세 계획은 `docs/PRD.md`(공통), `docs/RAGAS_PRD.md`(RAGAS 채점), `docs/RETRIEVE_PRD.md`(검색·라우팅 평가), DB 스키마는 `docs/Schema.md`를 참고합니다.

## 설치

Python 3.12

```bash
py -3.12 -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
```

## DB

```bash
psql -d youthlink -f db/ragas_schema.sql
```

- 여러 번 적용해도 안전하다 (`IF NOT EXISTS`, 제약 재생성). 이미 테이블이 있는 DB도 다시 적용해야 새 컬럼·값이 반영된다
- 라우팅 평가(`route`)는 `mode = 'routing'`으로 저장하므로, 이 값이 추가된 스키마를 적용한 DB에서만 실행할 수 있다

## 실행

```bash
# DB policy에서 생성 대상 정책을 고정 시드로 150건 뽑아 분석 → 질문·정답 후보 130건 생성
# → datasets/golden_candidates.jsonl 저장
python -m ragas_eval generate --testset-size 130 --policy-limit 150

# 후보에 정답 정책의 시도·나이로 프로필을 채움 (LLM 없음, DB 읽기만)
# → datasets/golden_profile_candidates.jsonl 저장, 검수가 필요한 샘플과 이유 출력
# → ragas-dataset-review 스킬로 검수, 사용자 승인 후 golden_vN.jsonl로 확정
#   (프로필 없이 확정된 golden_vN에 붙일 때는 --golden datasets/golden_vN.jsonl)
python -m ragas_eval profile --golden datasets/golden_candidates.jsonl

# 확정 데이터셋으로 RAG 호출 → 채점 → 결과 DB 저장, 3회 반복
python -m ragas_eval run --golden datasets/golden_v1.jsonl --repeat 3

# RAG 없이 정답을 응답으로 넣어 채점 → DB 저장 경로만 검증
python -m ragas_eval run --golden datasets/golden_v1.jsonl --offline

# RAG 없이 BASELINE_MODEL에 직접 질문 → 할루시네이션 기준선
python -m ragas_eval run --golden datasets/golden_v1.jsonl --baseline --repeat 3

# 응답 실패·중단으로 RUNNING에 남은 run을 미완료 샘플만 이어서 실행 (같은 모드 옵션 함께 지정)
python -m ragas_eval run --golden datasets/golden_v1.jsonl --resume <run_id>

# 라우팅 평가: 대화 없는 질문 90건을 RAG에 보내 질문 유형 분류가 맞는지 채점 (채점 LLM 없음, RAG 호출 비용만)
python -m ragas_eval route --routing datasets/routing_v1.jsonl
```

- `run`(rag 모드)은 답변 품질과 함께 검색 적중률(카드·후보·검색·1차 필터 적중, Hit@k, 역순위)을 계산해 저장한다. 추가 비용은 없다
- `run`·`route`는 RAG와 LLM을 실제로 호출해 비용이 든다. `route`는 재개가 없어서, 미응답이 남으면 새로 다시 실행한다

`generate` 옵션

| 옵션 | 기본값 | 의미 |
| --- | --- | --- |
| `--testset-size` | 100 | 만들 질문·정답 후보 수. 정책을 특정할 수 없는 후보는 빠져서 조금 줄 수 있다. 검수에서 걸러낼 몫을 생각해 목표보다 넉넉히 잡는다 |
| `--policy-limit` | 없음 (대상 전체) | 생성기에 넣을 정책 수. 대상 정책을 `policy_no` 순으로 정렬한 뒤 고정 시드로 뽑으므로 다시 실행해도 같은 표본이 나온다 |
| `--out` | `datasets/golden_candidates.jsonl` | 후보 저장 경로. 확정본(`golden_vN.jsonl`)은 덮어쓰지 않는다 |

- 생성 대상: 설명이 있고, 마감(신청기간 구분 0057003)이 아니고, 종료일이 없거나 오늘 이후인 정책
- 정책 분석(정책당 LLM 약 4회)은 넣은 정책 수만큼 든다. `--policy-limit`을 빼면 대상 전체(로컬 약 970건)를 분석하므로 비용이 크게 는다
- 질문은 정책당 최대 `testset-size ÷ policy-limit`(올림)건이다. 130 ÷ 150이면 정책당 1건이라 질문이 한 정책에 몰리지 않는다
- 분석 결과는 `datasets/.cache/`에 저장되어, 같은 정책·모델로 다시 실행하면 분석 비용이 들지 않는다

후보 검수는 Claude Code skill `ragas-dataset-review`(`.claude/skills/ragas-dataset-review/SKILL.md`)로 한다.
Claude Code에서 "golden_candidates 검수해줘"처럼 요청하면 후보를 정책 원문과 전수 대조해 검수표(`docs/golden_vN/review.md`, 로컬 전용)를 만들고,
사용자가 제외 목록을 승인하면 `datasets/golden_vN.jsonl`로 확정한다. 판정 기준(모호한 질문, 정답 불일치, 페르소나 불일치, 시점 의존, 동명 정책 등)은 skill 문서에 있다.

## 동작 흐름

데이터셋 생성 → 사람 검수 → 평가 실행 순서다. DB는 `policy`만 읽고, 쓰기는 `repository.py`가 `ragas_evaluation_*` 테이블에만 한다.

### 1. 데이터셋 생성 (로컬, `generate`)

| 순서 | 파일 | 역할 | 입력 → 출력 |
|---|---|---|---|
| 1 | `cli.py` | 명령을 받아 `generate()` 호출, 건수 출력 | 인자 → 함수 호출 |
| 2 | `config.py` | `.env`에서 DB 주소·API 키·모델명 읽기 | `.env` → `Settings` |
| 3 | `policy_source/policy_source.py` | `policy`를 읽기만 해서 정책별 문서로 변환 | `policy` 행 → `Document` |
| 4 | `generate/generate.py` | ragas 생성기로 질문·정답·근거 생성 (LLM 비용) | `Document` → 생성 결과 |
| 5 | `generate/generate.py` | 근거로 정책 매칭, 실패분 제외 | 생성 결과 → golden 형식 레코드 |
| 6 | `generate/generate.py` | 후보 파일 저장 | → `datasets/golden_candidates.jsonl` |

### 2. 사람 검수

후보를 고치거나 지우고 `reviewed: true`로 표시한 뒤 `datasets/golden_vN.jsonl`로 커밋한다. 확정한 파일은 수정하지 않는다.

### 3. 평가 실행 (K8s Job, `run`)

| 순서 | 파일 | 역할 | 입력 → 출력 |
|---|---|---|---|
| 1 | `cli.py` | 명령을 받아 `runner.run()` 호출 | 인자 → 함수 호출 |
| 2 | `dataset/dataset.py` | `reviewed` 샘플만 고르고, 정책이 바뀌거나 삭제된 샘플 제외 | `golden_vN.jsonl` + `policy` → 평가 샘플 |
| 3 | `repository/repository.py` | run 행 생성, 샘플 행 INSERT | → `ragas_evaluation_run`, `ragas_evaluation_samples` |
| 4 | `rag_client/rag_client.py` | 질문마다 RAG `/internal/pipeline` 호출 (rag 모드) | 질문 → 답변·청크·지연시간·토큰 |
| 4' | `baseline_client/baseline_client.py` | RAG 없이 LLM 직접 호출 (baseline 모드) | 질문 → 답변 |
| 5 | `retrieval/retrieval.py` | 정답 정책이 1차 필터 → 검색 → 후보 → 카드 중 어디까지 남았는지 계산 (rag 모드, LLM 없음) | RAG trace → 검색 적중률 |
| 5' | `repository/repository.py` | 답변·청크·검색 적중률 저장 | → `samples.answer`, `contexts`, `metadata.retrieval` |
| 6 | `scorer/scorer.py` | 지표 7개 채점, 실패 지표는 NULL + 에러 기록 (LLM 비용) | 질문·답변·청크·정답 → 점수 |
| 7 | `repository/repository.py` | 점수 저장 | → `samples` 지표 컬럼 |
| 8 | `repository/repository.py` | 평균 집계 후 run 종료 (SUCCEEDED/FAILED) | → `run.avg_*`, `status` |
| — | `runner/runner.py` | 3~8 순서 조율, 반복, 중단된 run 재개 | |

### 4. 라우팅 평가 (K8s Job, `route`)

데이터셋은 `datasets/routing_vN.jsonl`(질문, 기대 유형 7종 중 1~3개)이다. 초안 `routing_candidates.jsonl`을 검수해 확정하고, 확정본은 수정하지 않는다.

| 순서 | 파일 | 역할 | 입력 → 출력 |
|---|---|---|---|
| 1 | `dataset/dataset.py` | `reviewed` 샘플만 로드, 유형 값·개수 검사 | `routing_vN.jsonl` → 샘플 |
| 2 | `repository/repository.py` | run(`mode = 'routing'`)과 샘플 행 INSERT | → `ragas_evaluation_run`, `ragas_evaluation_samples` |
| 3 | `rag_client/rag_client.py` | 질문만 RAG에 보냄 (프로필·앞 대화 없음) | 질문 → 답변·질문 분석 결과 |
| 4 | `routing/routing.py` | 응답 유형 묶음이 기대 유형과 같은지 채점 (순서 무관, 분석 실패는 0) | 분석 결과 → `intent_correct` |
| 5 | `repository/repository.py` | 샘플 채점 저장, 모두 응답했으면 정확도·유형별·혼동 행렬 집계 후 종료 | → `samples.metadata.routing`, `run.metadata.routing` |

## 배포 (K8s Job)

`Dockerfile`은 `ragas_eval/`과 `datasets/`만 이미지에 넣는다 (`.env`는 `.dockerignore`로 차단). 코드나 데이터셋을 바꾸면 이미지를 다시 빌드한다.

```bash
docker build -t youthlink/ragas-eval:latest .

# 비밀값은 Secret으로만 넣는다 (파일에 적지 않는다)
kubectl create secret generic ragas-eval-secrets \
  --from-literal=OPENAI_API_KEY='...' \
  --from-literal=DATABASE_URL='...'

# Job은 이름이 같으면 지우고 다시 만든다
kubectl delete job ragas-eval --ignore-not-found
kubectl apply -f k8s/ragas-eval-job.yaml
kubectl logs -f job/ragas-eval
```

`k8s/ragas-eval-job.yaml`에서 배포 환경에 맞게 채울 값은 다음과 같다.

| 값 | 위치 |
| --- | --- |
| 이미지 주소 (ECR 계정 ID·리전) | Job `image` |
| `RAG_BASE_URL` (RAG Service 주소) | ConfigMap `ragas-eval-config` |
| `OPENAI_BASE_URL` (bedrock 모델을 호출하는 프록시 주소) | ConfigMap 주석 해제 |
| 실행 명령 (`run`, `run --baseline`, `route` 등) | Job `args` |

## 결과 확인

채점 결과는 DB에 저장된다. `run`이 출력한 `run_id`로 마크다운 성적표를 만들거나 SQL로 직접 조회한다.

```bash
# results/<dataset_version>/<dataset_version>_<mode>_r<repeat_no>_<run_id>.md 생성 (LLM 비용 없음)
python -m ragas_eval report --run <run_id>
```

| run 종류 | 보고서 내용 |
| --- | --- |
| `rag` | 지표 평균, 검색 적중률(단계별 평균, 정답 정책이 빠진 단계별 샘플 수), 모드별 비교, 점수 낮은 샘플 |
| `baseline`·`offline` | 지표 평균, 모드별 비교, 점수 낮은 샘플 |
| `routing` | 정확도·분석 실패율, 유형별 정확도, 혼동 행렬, 틀린 샘플(기대·응답 유형) |

```sql
-- 실행별 성적표
SELECT run_id, mode, dataset_version, repeat_no, status, sample_count,
       avg_context_precision, avg_context_recall, avg_faithfulness,
       avg_answer_relevancy, avg_factual_correctness, avg_reference_faithfulness,
       rag_chat_model, judge_model, started_at, finished_at
FROM ragas_evaluation_run
ORDER BY started_at DESC;

-- baseline vs rag (같은 데이터셋, 반복 평균)
SELECT mode, COUNT(*) AS runs,
       ROUND(AVG(avg_factual_correctness), 3)    AS factual_correctness,
       ROUND(AVG(avg_reference_faithfulness), 3) AS reference_faithfulness,
       ROUND(AVG(avg_answer_relevancy), 3)       AS answer_relevancy
FROM ragas_evaluation_run
WHERE dataset_version = 'golden_v1' AND status = 'SUCCEEDED'
GROUP BY mode;

-- 한 run에서 점수가 낮은 샘플
SELECT sample_id, policy_no, question, answer, factual_correctness, reference_faithfulness,
       metadata->'errors' AS errors
FROM ragas_evaluation_samples
WHERE run_id = '<run_id>'
ORDER BY factual_correctness NULLS FIRST
LIMIT 20;

-- 한 rag run의 검색 적중률 평균 (NULL은 판단할 수 없는 샘플이라 평균에서 빠짐)
SELECT AVG((metadata->'retrieval'->>'card_hit')::int)        AS card_hit,
       AVG((metadata->'retrieval'->>'retrieval_hit')::int)   AS retrieval_hit,
       AVG((metadata->'retrieval'->>'reciprocal_rank')::float) AS mrr
FROM ragas_evaluation_samples
WHERE run_id = '<run_id>';

-- 라우팅 run 정확도와 틀린 샘플
SELECT metadata->'routing'->>'accuracy' AS accuracy, metadata->'routing'->>'fallback_rate' AS fallback_rate
FROM ragas_evaluation_run
WHERE run_id = '<run_id>';

SELECT sample_id, question, metadata->'routing'->'expected_intent' AS expected,
       metadata->'routing'->'predicted_intent' AS predicted
FROM ragas_evaluation_samples
WHERE run_id = '<run_id>' AND (metadata->'routing'->>'intent_correct')::int = 0;
```

## 테스트

```bash
pytest
```

- 실제 OpenAI·RAG는 호출하지 않는다 (mock)
- DB 통합 테스트(`db` 마커)는 `TEST_DATABASE_URL`이 있을 때만 돈다. 로컬 주소이고 DB 이름에 `test`가 들어간 임시 DB만 허용한다 (예: `postgresql://postgres@localhost:55432/ragas_test`). 없으면 건너뛴다