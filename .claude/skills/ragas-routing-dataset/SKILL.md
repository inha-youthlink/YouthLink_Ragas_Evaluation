---
name: ragas-routing-dataset
description: 라우팅 평가용 routing 데이터셋(datasets/routing_vN.jsonl)을 RAG 분석 프롬프트의 유형 정의에 맞춰 Claude가 직접 초안으로 쓰고, 점검·검수표 작성 후 사용자 승인을 받아 확정하는 절차와 작성 규칙. routing 데이터셋을 새로 만들거나 다음 버전을 만들 때, 질문 유형 정의가 바뀌어 기대 유형을 다시 맞출 때 사용한다.
---

# routing 데이터셋 작성

golden 데이터셋은 `ragas-golden-dataset-review` 스킬을 따른다.

## 절차

```
1. 기준 확인   RAG 분석 프롬프트의 유형 정의·하위 질문 규칙·예시 질문 확인, 버전(N) 결정
2. 초안 작성   유형별 건수에 맞춰 질문·기대 유형 작성 → datasets/routing_candidates.jsonl
3. 점검       유형별 건수, 중복, 프롬프트 예시와 겹침, 형식 확인
4. 검수표     docs/routing_vN/review.md 작성 (로컬 전용, 커밋하지 않음)
5. 승인       사용자에게 요약·경계 사례를 보고하고 결정을 받음
6. 확정       승인된 건만 datasets/routing_vN.jsonl로 저장, reviewed=true, 커밋
```

## 0. 규칙

| 항목 | 규칙 |
| --- | --- |
| 비용 | 초안은 Claude가 직접 쓴다. OpenAI 등 LLM API를 호출하지 않는다 |
| 확정본 | 이미 있는 `routing_vN.jsonl`은 수정하지 않는다. 바꿀 때는 `routing_vN+1.jsonl` |
| 승인 | 확정(6단계)은 사용자가 결정을 준 뒤에만 한다. 사용자가 직접 검수하지 않았는데 검수했다고 기록하지 않는다 |
| 초안 파일 | `datasets/routing_candidates.jsonl`은 gitignore 대상. 확정 후 지워도 된다 |
| 기록 | 판정 기준을 바꾸거나 경계 사례를 넣고 뺀 이유는 `docs/routing_vN/context-notes.md`에 남긴다 |

## 1. 기준 확인

| 확인 | 위치 |
| --- | --- |
| 유형 7종의 정의 | `../YouthLink_RAG/app/prompts/analyze_vN.md` "질문 유형 (intent)" (현재 `analyze_v6.md`) |
| 여러 하위 질문으로 나누는 기준, 최대 개수 | 같은 문서 "하위 질문 규칙", `ragas_eval/dataset/dataset.py` `MAX_SUB_QUERIES` |
| 지시어만 있는 질문의 처리 | 같은 문서 "정책 지목 규칙" |
| 유형 이름 | `ragas_eval/dataset/dataset.py` `Intent` (RAG와 같은 값) |

프롬프트 버전이 바뀌었으면 이전 버전과 유형 정의·규칙 절을 비교한다. 정의가 같으면 기존 `routing_vN`을 그대로 쓰고, 다르면 다음 버전을 만든다.

## 2. 작성 규칙

| 항목 | 규칙 |
| --- | --- |
| 건수 | 유형 7종(`policy_list`, `policy_detail`, `eligibility`, `comparison`, `chitchat`, `out_of_scope`, `unclear`) 각 10~15건 + 하위 질문이 여러 개인 질문 5~10건 |
| 기대 유형 | `expected_intent`는 하위 질문마다 1개 (list, 대부분 1개). 순서는 채점에 쓰지 않음 |
| 대화 없음 | 앞 턴·프로필 없이 보내므로 질문 하나만으로 유형이 정해져야 한다 |
| 정책 이름 | `policy_detail`·`eligibility`·`comparison`에는 정책 이름을 넣는다. 지시어만 있는 질문("그거 자세히")은 `unclear` |
| 경계 사례 | 프롬프트 규칙으로 정답이 분명한 것만 넣는다 (기준 vs 해당 여부, 인사 + 질문, 지시어만 있는 질문). 규칙끼리 충돌하거나 해석이 갈리면 넣지 않는다 |
| 넣지 않는 질문 | 프롬프트 예시에 있는 질문 (본 적 있는 질문이라 점수가 높게 나옴), 같은 질문의 말투만 바꾼 중복 |
| 다양성 | 분야(일자리·주거·교육·복지문화·참여권리), 지역, 말투(구어체·검색어형)를 섞는다 |
| `note` | 경계 사례에는 판정 근거를 한 줄 적는다 (검수용, 채점에 쓰지 않음) |

샘플 형식은 다음과 같다.

```json
{"sample_id": "route-000001", "question": "서울 청년 월세 지원 뭐 있어?", "expected_intent": ["policy_list"], "reviewed": false}
{"sample_id": "route-000002", "question": "청년도약계좌 가입 조건이랑 비슷한 적금 상품도 알려줘", "expected_intent": ["eligibility", "policy_list"], "reviewed": false, "note": "조건 질문 + 목록 질문 2개"}
```

## 3. 점검

```bash
# 유형별 건수, 중복 질문, 프롬프트 예시와 겹치는 질문, 형식(load_routing)을 확인한다
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe - <<'EOF'
import re
from collections import Counter
from pathlib import Path
from ragas_eval.dataset.dataset import load_routing

samples = load_routing("datasets/routing_candidates.jsonl")
single = Counter(s.expected_intent[0] for s in samples if len(s.expected_intent) == 1)
multi = sum(len(s.expected_intent) > 1 for s in samples)
print("유형별(하위 질문 1개):", dict(single), "/ 여러 개:", multi, "/ 전체:", len(samples))

norm = lambda q: re.sub(r"[\s?!.,~]", "", q)
dup = [q for q, n in Counter(norm(s.question) for s in samples).items() if n > 1]
print("중복:", dup)

prompt = Path("../YouthLink_RAG/app/prompts/analyze_v6.md").read_text(encoding="utf-8")
examples = {norm(q) for q in re.findall(r"^(?:\[질문\]|질문:)\s*(.+)$", prompt, re.M)}
print("프롬프트 예시와 겹침:", [s.sample_id for s in samples if norm(s.question) in examples])
EOF
```

| 결과 | 처리 |
| --- | --- |
| 유형 건수가 10 미만·15 초과 | 질문을 더하거나 뺀다 |
| 중복, 예시와 겹침 | 다른 질문으로 바꾼다 |
| `load_routing` 실패 | 유형 이름 오타, `expected_intent` 개수(1~`MAX_SUB_QUERIES`)를 고친다 |

## 4. 검수표 (`docs/routing_vN/review.md`)

```markdown
# routing_vN 검수표

## 요약
| 유형 | 건수 |
(유형 7종 + 하위 질문 여러 개, 기준 프롬프트 버전)

## 경계 사례
| sample_id | 질문 | 기대 유형 | 근거 |

## 전체 목록
| # | sample_id | 질문 | 기대 유형 | 비고 | 최종 |
```

- 표 셀의 `|`는 `\|`로, 줄바꿈은 공백으로 바꾼다.

## 5. 승인 요청

| 항목 | 내용 |
| --- | --- |
| 요약 | 유형별 건수, 기준 프롬프트 버전 |
| 경계 사례 | 질문, 기대 유형, 근거. 사용자가 다르게 판단하면 바꾸거나 뺀다 |
| 이전 버전과 차이 | 다음 버전이면 바뀐 유형 정의와 바꾼 질문 |

## 6. 확정

```python
# 승인된 제외 sample_id(EXCLUDED)와 기대 유형 수정(FIXES: sample_id → expected_intent)을 반영해 routing_vN.jsonl을 만든다
assert not out.exists()
kept = [dict(r, expected_intent=FIXES.get(r["sample_id"], r["expected_intent"]), reviewed=True)
        for r in rows if r["sample_id"] not in EXCLUDED]
out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8", newline="\n")
```

- `ragas_eval.dataset.dataset.load_routing`으로 다시 읽어 건수와 `reviewed`를 확인한다.
- 검수표 최종 열에 O/X를 채운다.
- `datasets/routing_vN.jsonl`만 커밋한다 (`feat: 검수 완료 라우팅 데이터셋 routing_vN M건 확정`). `docs/routing_vN/`과 초안은 커밋하지 않는다.
- 평가는 `python -m ragas_eval route --routing datasets/routing_vN.jsonl`로 한다.
