# 생성 결과 → policy_no 매칭 → 후보 jsonl 변환·저장 테스트 (TestsetGenerator·DB는 대체, 실제 LLM 호출 없음)
import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from pydantic import SecretStr

from ragas_eval.dataset.dataset import load_golden
from ragas_eval.generate import generate
from ragas_eval.generate.generate import YOUTH_PERSONAS, build_records, match_policy, sample_documents, write_jsonl

UPDATED_AT = "2026-09-30T00:00:00+09:00"


def doc(policy_no, content, updated_at=UPDATED_AT):
    return Document(page_content=content, metadata={"policy_no": policy_no, "source_updated_at": updated_at})


DOCS = [
    doc("TEST-0001", "정책명: 테스트 청년 월세 지원\n지원 연령: 만 19세 ~ 34세\n지원 내용: 월 최대 20만원"),
    doc("TEST-0002", "정책명: 테스트 청년 취업 지원\n지원 연령: 만 18세 ~ 39세\n지원 내용: 교육비 지원"),
]


def row(contexts, question="가짜 질문", reference="가짜 정답"):
    return {
        "user_input": question,
        "reference": reference,
        "reference_contexts": contexts,
        "synthesizer_name": "single_hop_specific_query_synthesizer",
    }


@pytest.mark.parametrize(
    ("contexts", "expected"),
    [
        pytest.param(["지원 연령: 만 19세 ~ 34세"], "TEST-0001", id="single-chunk"),
        pytest.param(["정책명: 테스트 청년 취업 지원", "지원 내용: 교육비 지원"], "TEST-0002", id="split-chunks"),
        pytest.param(["  지원 연령: 만 19세 ~ 34세\n"], "TEST-0001", id="surrounding-whitespace"),
        # ragas HeadlineSplitter는 섹션을 " "로 이어 붙이거나 줄바꿈을 공백으로 바꾼다
        pytest.param(["지원 연령: 만 19세 ~ 34세\n 지원 내용: 월 최대 20만원"], "TEST-0001", id="merged-sections"),
        pytest.param(["지원 연령: 만 18세 ~ 39세 지원 내용: 교육비 지원"], "TEST-0002", id="newline-collapsed"),
    ],
)
def test_match_policy_finds_document_containing_all_contexts(contexts, expected):
    matched = match_policy(contexts, DOCS)

    assert matched.metadata["policy_no"] == expected


@pytest.mark.parametrize(
    "contexts",
    [
        pytest.param(["존재하지 않는 문장"], id="no-match"),
        pytest.param(["지원 연령: 만 19세 ~ 34세", "교육비 지원"], id="contexts-from-two-documents"),
        pytest.param(["정책명: 테스트 청년"], id="ambiguous"),
        pytest.param([], id="empty"),
        pytest.param(["   "], id="blank-only"),
    ],
)
def test_match_policy_returns_none_when_not_exactly_one(contexts):
    assert match_policy(contexts, DOCS) is None


def test_build_records_numbers_samples_and_fills_fields():
    rows = [row(["지원 연령: 만 19세 ~ 34세"]), row(["교육비 지원"], question="두 번째 질문")]

    records, dropped = build_records(rows, DOCS)

    assert dropped == 0
    assert [r["sample_id"] for r in records] == ["single-000001", "single-000002"]
    assert records[0] == {
        "sample_id": "single-000001",
        "policy_no": "TEST-0001",
        "question": "가짜 질문",
        "ground_truth": "가짜 정답",
        "reference_contexts": ["지원 연령: 만 19세 ~ 34세"],
        "synthesizer_name": "single_hop_specific_query_synthesizer",
        "profile": {},
        "source_updated_at": UPDATED_AT,
        "reviewed": False,
    }
    assert records[1]["policy_no"] == "TEST-0002"


def test_build_records_drops_unmatched_and_empty_rows_without_gaps_in_ids():
    rows = [
        row(["존재하지 않는 문장"]),
        row(["지원 연령: 만 19세 ~ 34세"], question=" "),
        row(["지원 연령: 만 19세 ~ 34세"], reference=""),
        row(["교육비 지원"]),
    ]

    records, dropped = build_records(rows, DOCS)

    assert dropped == 3
    assert [(r["sample_id"], r["policy_no"]) for r in records] == [("single-000001", "TEST-0002")]


def test_build_records_keeps_null_source_updated_at():
    records, _ = build_records([row(["교육비 지원"])], [doc("TEST-0002", "교육비 지원", updated_at=None)])

    assert records[0]["source_updated_at"] is None


def test_written_candidates_load_as_golden_samples(tmp_path):
    records, _ = build_records([row(["지원 연령: 만 19세 ~ 34세"])], DOCS)
    path = tmp_path / "golden_candidates.jsonl"

    write_jsonl(records, path)

    (sample,) = load_golden(path)
    assert (sample.sample_id, sample.policy_no, sample.reviewed) == ("single-000001", "TEST-0001", False)
    assert "지원 연령" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name", ["golden_v1.jsonl", "golden_v12.jsonl", "GOLDEN_V1.jsonl", "golden_v1.JSONL", "golden_v1.jsonl. "]
)
def test_write_jsonl_refuses_confirmed_dataset(tmp_path, name):
    with pytest.raises(ValueError, match="golden_vN"):
        write_jsonl([], tmp_path / name)


def fake_settings():
    return SimpleNamespace(gen_model="test-gen-model", embedding_model="test-emb")


def test_generate_writes_candidates_and_returns_counts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(generate, "get_settings", fake_settings)
    monkeypatch.setattr(generate, "_load_target_documents", lambda settings: DOCS)

    def fake_run(documents, testset_size, settings):
        calls.append((len(documents), testset_size, settings.gen_model))
        return [row(["지원 연령: 만 19세 ~ 34세"]), row(["존재하지 않는 문장"])]

    monkeypatch.setattr(generate, "_run_generator", fake_run)
    out = tmp_path / "golden_candidates.jsonl"

    summary = generate.generate(testset_size=10, out_path=str(out))

    assert calls == [(2, 10, "test-gen-model")]
    assert (summary.written, summary.dropped) == (1, 1)
    assert [json.loads(line)["policy_no"] for line in out.read_text(encoding="utf-8").splitlines()] == ["TEST-0001"]


MANY_DOCS = [doc(f"TEST-{i:04d}", f"정책명: 테스트 정책 {i:04d}번") for i in range(20)]


def policy_nos(documents):
    return [d.metadata["policy_no"] for d in documents]


def test_sample_documents_is_reproducible_regardless_of_input_order():
    first = sample_documents(MANY_DOCS, 5)
    again = sample_documents(list(reversed(MANY_DOCS)), 5)

    assert len(first) == 5
    assert policy_nos(first) == sorted(policy_nos(first))
    assert policy_nos(first) == policy_nos(again)


@pytest.mark.parametrize("limit", [None, 20, 50])
def test_sample_documents_keeps_all_when_limit_is_not_smaller(limit):
    assert policy_nos(sample_documents(list(reversed(MANY_DOCS)), limit)) == policy_nos(MANY_DOCS)


def test_generate_passes_only_sampled_policies_to_generator(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(generate, "get_settings", fake_settings)
    monkeypatch.setattr(generate, "_load_target_documents", lambda settings: MANY_DOCS)

    def fake_run(documents, testset_size, settings):
        seen.extend(documents)
        return [row([documents[0].page_content])]

    monkeypatch.setattr(generate, "_run_generator", fake_run)

    summary = generate.generate(testset_size=3, out_path=str(tmp_path / "c.jsonl"), policy_limit=5)

    assert policy_nos(seen) == policy_nos(sample_documents(MANY_DOCS, 5))
    assert summary.written == 1


def test_youth_personas_have_unique_names_and_descriptions():
    names = [p.name for p in YOUTH_PERSONAS]

    assert len(names) >= 3
    assert len(set(names)) == len(names)
    assert all(p.role_description.strip() for p in YOUTH_PERSONAS)


def test_run_generator_uses_youth_personas(monkeypatch):
    captured = {}

    class FakeGenerator:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def generate(self, **kwargs):
            captured["generate"] = kwargs
            return SimpleNamespace(to_list=lambda: [])

    async def fake_synthesizer(api_key, model, base_url=None):
        return SimpleNamespace(llm=None)

    monkeypatch.setattr(generate, "TestsetGenerator", FakeGenerator)
    monkeypatch.setattr(generate, "_korean_synthesizer", fake_synthesizer)
    monkeypatch.setattr(generate, "build_knowledge_graph", lambda documents, **kwargs: "kg")
    monkeypatch.setattr(generate, "make_llm", lambda model, client: "llm")
    monkeypatch.setattr(generate, "OpenAIEmbeddings", lambda **kwargs: "emb")
    settings = SimpleNamespace(
        openai_api_key=SecretStr("test-key"), openai_base_url=None, gen_model="m", embedding_model="e"
    )

    generate._run_generator(DOCS, 3, settings)

    assert captured["init"]["persona_list"] == list(YOUTH_PERSONAS)
    assert captured["generate"]["num_personas"] == len(YOUTH_PERSONAS)


def test_generate_without_target_policies_fails_before_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "get_settings", fake_settings)
    monkeypatch.setattr(generate, "_load_target_documents", lambda settings: [])
    monkeypatch.setattr(generate, "_run_generator", lambda *args: pytest.fail("LLM 생성기가 호출됨"))

    with pytest.raises(ValueError, match="대상 정책"):
        generate.generate(testset_size=10, out_path=str(tmp_path / "golden_candidates.jsonl"))


def test_generate_checks_out_path_before_loading(monkeypatch, tmp_path):
    monkeypatch.setattr(generate, "get_settings", lambda: pytest.fail("설정을 읽음"))

    with pytest.raises(ValueError, match="golden_vN"):
        generate.generate(testset_size=10, out_path=str(tmp_path / "golden_v1.jsonl"))


def test_generate_keeps_existing_file_when_all_rows_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "get_settings", fake_settings)
    monkeypatch.setattr(generate, "_load_target_documents", lambda settings: DOCS)
    monkeypatch.setattr(generate, "_run_generator", lambda *args: [row(["존재하지 않는 문장"])])
    out = tmp_path / "golden_candidates.jsonl"
    out.write_text("검수 중\n", encoding="utf-8")

    with pytest.raises(ValueError, match="모두 제외"):
        generate.generate(testset_size=10, out_path=str(out))
    assert out.read_text(encoding="utf-8") == "검수 중\n"


@pytest.fixture
def stub_openai_url():
    """임베딩 응답을 돌려주는 로컬 가짜 OpenAI 서버 (keep-alive 연결 유지)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps(
                {
                    "object": "list",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                    "model": "m",
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def test_generation_client_survives_new_event_loop_per_step(stub_openai_url):
    # ragas는 단계마다 asyncio.run으로 새 루프를 연다. 재시도 없이도 닫힌 루프의 연결을 재사용하지 않아야 한다
    client = generate._generation_client("sk-test").with_options(base_url=stub_openai_url, max_retries=0)

    async def batch():
        responses = await asyncio.gather(*(client.embeddings.create(input="a", model="m") for _ in range(20)))
        return len(responses)

    assert [asyncio.run(batch()) for _ in range(5)] == [20] * 5


# ---- 페르소나 이름 보정 ----


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("김도윤", "김도윤", id="exact"),
        pytest.param("김도윤 (재직 기술인력)", "김도윤", id="parenthesized-description"),
        pytest.param("  김도윤 ", "김도윤", id="surrounding-whitespace"),
        pytest.param("박서연", None, id="unknown"),
    ],
)
def test_resolve_persona_name(name, expected):
    assert generate.resolve_persona_name(name, {"김도윤", "이하은"}) == expected


def test_tolerant_synthesizer_keeps_matched_and_skips_unknown_personas():
    from ragas.testset.graph import Node, NodeType
    from ragas.testset.persona import Persona

    personas = [Persona(name="김도윤", role_description="재직 기술인력"), Persona(name="이하은", role_description="대학생")]
    concepts = {"김도윤 (재직 기술인력)": ["월세"], "박서연": ["월세"], "이하은": ["창업"]}
    synthesizer = generate.TolerantSingleHopSynthesizer(llm=None)

    (sample,) = synthesizer.prepare_combinations(Node(type=NodeType.CHUNK), ["월세"], personas, concepts)

    assert [p.name for p in sample["personas"]] == ["김도윤"]
    assert synthesizer.name == "single_hop_specific_query_synthesizer"


def test_tolerant_synthesizer_uses_all_personas_when_no_name_matches():
    from ragas.testset.graph import Node, NodeType
    from ragas.testset.persona import Persona

    personas = [Persona(name="대학생", role_description="대학생"), Persona(name="취업준비생", role_description="구직자")]
    concepts = {"University Student": ["월세"], "Job Seeker": ["월세"]}
    synthesizer = generate.TolerantSingleHopSynthesizer(llm=None)

    (sample,) = synthesizer.prepare_combinations(Node(type=NodeType.CHUNK), ["월세"], personas, concepts)

    assert [p.name for p in sample["personas"]] == ["대학생", "취업준비생"]


def half_translated_matching_prompt():
    from ragas.testset.persona import Persona
    from ragas.testset.synthesizers.prompts import (
        PersonaThemesMapping,
        ThemesPersonasInput,
        ThemesPersonasMatchingPrompt,
    )

    # ragas 한국어 변환 결과(진단으로 확인): 입력 이름만 번역되고 답변 키는 영어로 남는다
    prompt = ThemesPersonasMatchingPrompt()
    prompt.examples = [
        (
            ThemesPersonasInput(
                themes=["포용성", "원격 근무"],
                personas=[
                    Persona(name="인사 관리자", role_description="포용성에 집중"),
                    Persona(name="원격 팀 리더", role_description="원격 팀 관리"),
                ],
            ),
            PersonaThemesMapping(mapping={"HR Manager": ["포용성"], "Remote Team Lead": ["원격 근무"]}),
        )
    ]
    return prompt


def test_align_persona_example_names_uses_input_names_as_answer_keys():
    prompt = generate.align_persona_example_names(half_translated_matching_prompt())

    ((_, example_output),) = prompt.examples
    assert example_output.mapping == {"인사 관리자": ["포용성"], "원격 팀 리더": ["원격 근무"]}


def adapted_korean_synthesizer(monkeypatch):
    from ragas.testset.synthesizers.single_hop.prompts import QueryAnswerGenerationPrompt

    async def fake_adapt(self, language, llm):
        return {
            "themes_personas_matching_prompt": half_translated_matching_prompt(),
            "query_answer_generation_prompt": QueryAnswerGenerationPrompt(),
        }

    monkeypatch.setattr(generate.TolerantSingleHopSynthesizer, "adapt_prompts", fake_adapt)
    monkeypatch.setattr(generate, "make_llm", lambda model, client: None)
    return asyncio.run(generate._korean_synthesizer("test-key", "gen-model"))


def test_korean_synthesizer_aligns_matching_example_after_adaptation(monkeypatch):
    synthesizer = adapted_korean_synthesizer(monkeypatch)

    ((_, example_output),) = synthesizer.theme_persona_matching_prompt.examples
    assert list(example_output.mapping) == ["인사 관리자", "원격 팀 리더"]


def test_korean_synthesizer_adds_eligibility_instruction_once(monkeypatch):
    from ragas.testset.synthesizers.single_hop.prompts import QueryAnswerGenerationPrompt

    synthesizer = adapted_korean_synthesizer(monkeypatch)

    instruction = synthesizer.generate_query_reference_prompt.instruction
    assert instruction.startswith(QueryAnswerGenerationPrompt().instruction)
    assert instruction.count(generate.ELIGIBILITY_INSTRUCTION) == 1


# ---- 분석(지식 그래프) 캐시 ----


def test_cache_key_changes_with_content_and_models():
    base = generate.kg_cache_key(DOCS, "gen-a", "emb-a")

    assert base == generate.kg_cache_key(list(DOCS), "gen-a", "emb-a")
    assert base != generate.kg_cache_key(DOCS, "gen-b", "emb-a")
    assert base != generate.kg_cache_key(DOCS, "gen-a", "emb-b")
    assert base != generate.kg_cache_key([DOCS[0], doc("TEST-0002", "바뀐 정책 본문")], "gen-a", "emb-a")


def test_build_knowledge_graph_analyzes_once_then_loads_cache(tmp_path, monkeypatch):
    calls = []

    def fake_apply(kg, transforms, *args, **kwargs):
        calls.append(len(kg.nodes))
        kg.nodes[0].properties["summary"] = "분석 결과"

    monkeypatch.setattr(generate, "default_transforms", lambda **kwargs: ["fake-transform"])
    monkeypatch.setattr(generate, "apply_transforms", fake_apply)
    cache = tmp_path / "cache" / "kg.json"

    first = generate.build_knowledge_graph(DOCS, llm=None, embeddings=None, cache_path=cache)
    second = generate.build_knowledge_graph(DOCS, llm=None, embeddings=None, cache_path=cache)

    assert calls == [2]
    assert cache.exists()
    assert second.nodes[0].properties["summary"] == "분석 결과"
    assert second.nodes[0].properties["document_metadata"]["policy_no"] == first.nodes[0].properties["document_metadata"]["policy_no"]


def test_generation_client_uses_configured_base_url():
    client = generate._generation_client("sk-test", "https://proxy.example/v1")

    assert str(client.base_url) == "https://proxy.example/v1/"
