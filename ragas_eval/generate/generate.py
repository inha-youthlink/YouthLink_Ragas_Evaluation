# TestsetGenerator로 데이터셋 후보(golden_candidates.jsonl)를 생성
import asyncio
import hashlib
import json
import logging
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
from langchain_core.documents import Document
from openai import DEFAULT_CONNECTION_LIMITS, AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic import ValidationError
from ragas.embeddings import OpenAIEmbeddings
from ragas.testset import TestsetGenerator
from ragas.testset.graph import KnowledgeGraph, Node, NodeType
from ragas.testset.persona import Persona
from ragas.testset.synthesizers.prompts import PersonaThemesMapping
from ragas.testset.synthesizers.single_hop.specific import SingleHopSpecificQuerySynthesizer
from ragas.testset.transforms import apply_transforms, default_transforms

from ragas_eval.config import Settings, get_settings
from ragas_eval.dataset.dataset import GoldenSample
from ragas_eval.policy_source.policy_source import load_documents
from ragas_eval.scorer.scorer import make_llm

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
LANGUAGE = "korean"
# ragas는 변환·생성 단계마다 새 이벤트 루프(asyncio.run)를 연다. 이전 루프에 묶인 연결을 재사용하면
# "Event loop is closed"로 실패하므로(실제 생성에서 발생) 연결을 요청마다 닫는다
NO_KEEPALIVE_LIMITS = type(DEFAULT_CONNECTION_LIMITS)(
    max_connections=DEFAULT_CONNECTION_LIMITS.max_connections, max_keepalive_connections=0
)
# 정책 분석 결과(ragas 지식 그래프) 캐시. 생성 실패 후 재시도·본 생성에서 분석 비용을 다시 쓰지 않는다
KG_CACHE_DIR = Path("datasets/.cache")
# 확정 데이터셋은 수정하지 않는다(CLAUDE.md). 생성 결과로 덮어쓰지 않도록 막는다
CONFIRMED_DATASET = re.compile(r"golden_v\d+\.jsonl", re.IGNORECASE)
# 정책 표본 시드. 정책 목록이 같으면 항상 같은 표본이 나와 분석 캐시를 다시 쓸 수 있다
SAMPLE_SEED = 20261010
# ragas 질문·정답 생성 프롬프트(영어 지시문)에 덧붙이는 대상 여부 지시
ELIGIBILITY_INSTRUCTION = (
    "4. **Eligibility**: If the persona does not appear to meet the target or eligibility conditions stated in "
    "the context (for example, a startup founder asking about a program for unemployed youth), begin the answer "
    "by stating, using only the context, whether the persona is eligible and why. Then answer the rest of the query.\n"
)
# ragas가 정책 내용으로 페르소나를 만들면 이름이 어긋나거나 말투가 어색해(golden_v0) 청년정책 사용자로 고정한다
YOUTH_PERSONAS = (
    Persona(
        name="대학생",
        role_description="20대 초반 대학생. 장학금, 주거, 교육 지원 정책의 신청 자격과 지원 금액을 알고 싶어 한다.",
    ),
    Persona(
        name="취업준비생",
        role_description="구직 중인 20대 후반 청년. 취업 지원, 직업 훈련, 구직활동 지원금을 찾고 신청 방법을 묻는다.",
    ),
    Persona(
        name="사회초년생",
        role_description="입사 1~2년 차 직장인. 월세, 자산 형성, 대출 지원 정책과 소득 기준 충족 여부를 확인하려 한다.",
    ),
    Persona(
        name="청년 창업자",
        role_description="창업을 준비하거나 사업을 운영하는 30대 초반 청년. 창업 자금, 임차료, 교육·컨설팅 지원을 찾는다.",
    ),
    Persona(
        name="신혼부부 청년",
        role_description="결혼한 지 얼마 안 된 30대 청년. 전세·주택 대출과 월세 지원의 조건과 지원 규모를 비교한다.",
    ),
)


class GenerateSummary(NamedTuple):
    written: int
    dropped: int


def match_policy(contexts: Sequence[str], documents: Iterable[Document]) -> Document | None:
    """모든 reference_context를 본문에 포함하는 정책이 정확히 하나일 때만 그 Document를 돌려준다."""
    # ragas HeadlineSplitter가 섹션을 공백으로 이어 붙이므로 공백을 정규화한 뒤 비교한다
    needles = [_squash(c) for c in contexts if c.strip()]
    if not needles:
        return None
    matched = [d for d in documents if all(n in _squash(d.page_content) for n in needles)]
    return matched[0] if len(matched) == 1 else None


def _squash(text: str) -> str:
    return " ".join(text.split())


def build_records(rows: Iterable[Mapping[str, Any]], documents: Sequence[Document]) -> tuple[list[dict], int]:
    """생성 결과를 golden 형식으로 바꾼다. 정책을 특정할 수 없거나 질문·정답이 비면 버린다."""
    records: list[dict] = []
    dropped = 0
    for row in rows:
        doc = match_policy(row.get("reference_contexts") or [], documents)
        if doc is None:
            logger.warning("생성 결과 제외: 정책을 하나로 특정할 수 없음")
            dropped += 1
            continue
        record = {
            "sample_id": f"single-{len(records) + 1:06d}",
            "policy_no": doc.metadata["policy_no"],
            "question": (row.get("user_input") or "").strip(),
            "ground_truth": (row.get("reference") or "").strip(),
            "reference_contexts": list(row["reference_contexts"]),
            "synthesizer_name": row.get("synthesizer_name") or "",
            "profile": {},
            "source_updated_at": doc.metadata["source_updated_at"],
            "reviewed": False,
        }
        try:
            GoldenSample.model_validate(record)
        except ValidationError as e:
            # 생성 원문은 남기지 않고 위치·종류만 남긴다
            details = ", ".join(
                f"{'.'.join(map(str, err['loc']))}:{err['type']}" for err in e.errors(include_input=False)
            )
            logger.warning("생성 결과 제외: policy_no=%s 검증 실패 %s", doc.metadata["policy_no"], details)
            dropped += 1
            continue
        records.append(record)
    return records, dropped


def write_jsonl(records: Iterable[Mapping[str, Any]], path: str | Path) -> None:
    path = Path(path)
    _check_out_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) + "\n" for r in records]
    path.write_text("".join(lines), encoding="utf-8")


def generate(testset_size: int, out_path: str, policy_limit: int | None = None) -> GenerateSummary:
    _check_out_path(Path(out_path))
    settings = get_settings()
    documents = sample_documents(_load_target_documents(settings), policy_limit)
    if not documents:
        raise ValueError("대상 정책이 없어 생성하지 않음 (description이 있고 신청 기간이 남은 정책 0건)")
    rows = _run_generator(documents, testset_size, settings)
    records, dropped = build_records(rows, documents)
    if not records:
        # 검수 중인 기존 후보 파일을 빈 파일로 덮어쓰지 않는다
        raise ValueError(f"생성 결과 {dropped}건이 모두 제외되어 저장하지 않음")
    write_jsonl(records, out_path)
    return GenerateSummary(written=len(records), dropped=dropped)


def sample_documents(documents: Sequence[Document], limit: int | None) -> list[Document]:
    """정책 번호순으로 정렬한 뒤 고정 시드로 limit건을 고른다. limit이 없거나 전체보다 크면 전부 쓴다."""
    ordered = sorted(documents, key=lambda d: d.metadata["policy_no"])
    if limit is None or limit >= len(ordered):
        return ordered
    picked = random.Random(SAMPLE_SEED).sample(ordered, limit)
    return sorted(picked, key=lambda d: d.metadata["policy_no"])


def _check_out_path(path: Path) -> None:
    # Windows는 대소문자를 구분하지 않고 끝의 공백·점을 지우므로 같은 파일로 취급되는 이름도 막는다
    if CONFIRMED_DATASET.fullmatch(path.name.rstrip(" .")):
        raise ValueError(f"확정 데이터셋(golden_vN)에는 쓰지 않음: {path.name}")


def _load_target_documents(settings: Settings) -> list[Document]:
    # policy는 SELECT만 한다. 읽기 전용 세션으로 열어 실수로 쓰는 것도 막는다
    with psycopg.connect(settings.database_url.get_secret_value()) as conn:
        conn.read_only = True
        return load_documents(conn, datetime.now(KST).date())


def resolve_persona_name(name: str, names: set[str]) -> str | None:
    """LLM이 페르소나 이름에 설명을 붙여 답해도(예: '김도윤 (재직 기술인력)') 원래 이름으로 되돌린다."""
    for candidate in (name.strip(), name.split("(")[0].strip()):
        if candidate in names:
            return candidate
    return None


class TolerantSingleHopSynthesizer(SingleHopSpecificQuerySynthesizer):
    """ragas는 페르소나 이름이 정확히 같아야 찾고, 다르면 KeyError로 생성 전체가 멈춘다.
    이름을 보정하고 끝내 못 찾은 페르소나만 건너뛴다. 하나도 못 찾으면 정책이 통째로 빠지므로 전원을 후보로 둔다."""

    def prepare_combinations(
        self, node: Node, terms: list[str], personas: list[Persona], persona_concepts: dict[str, list[str]]
    ) -> list[dict[str, Any]]:
        names = {p.name for p in personas}
        resolved: dict[str, list[str]] = {}
        for name, concepts in persona_concepts.items():
            matched = resolve_persona_name(name, names)
            if matched is None:
                logger.warning("페르소나 이름을 찾지 못해 건너뜀: %s", name)
                continue
            resolved.setdefault(matched, []).extend(concepts)
        if not resolved:
            logger.warning("일치하는 페르소나가 없어 전원을 후보로 둠")
            resolved = {p.name: list(terms) for p in personas}
        return super().prepare_combinations(node, terms, personas, resolved)


def kg_cache_key(documents: Sequence[Document], gen_model: str, embedding_model: str) -> str:
    """정책 본문·모델이 같을 때만 같은 캐시를 쓴다."""
    digest = hashlib.sha256(f"{gen_model}\n{embedding_model}".encode())
    for doc in documents:
        digest.update(f"\n{doc.metadata.get('policy_no')}\n{doc.page_content}".encode())
    return digest.hexdigest()[:16]


def build_knowledge_graph(
    documents: Sequence[Document], *, llm: Any, embeddings: Any, cache_path: Path
) -> KnowledgeGraph:
    """ragas generate_with_langchain_docs의 분석 단계와 같다. 결과를 파일로 남겨 다음 실행에서 재사용한다."""
    if cache_path.exists():
        logger.warning("정책 분석 캐시 사용 (분석 LLM 호출 없음): %s", cache_path)
        return KnowledgeGraph.load(cache_path)
    nodes = [
        Node(
            type=NodeType.DOCUMENT,
            properties={"page_content": doc.page_content, "document_metadata": doc.metadata},
        )
        for doc in documents
    ]
    kg = KnowledgeGraph(nodes=nodes)
    apply_transforms(kg, default_transforms(documents=list(documents), llm=llm, embedding_model=embeddings))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    kg.save(cache_path)
    return kg


def _run_generator(documents: Sequence[Document], testset_size: int, settings: Settings) -> list[dict]:
    api_key = settings.openai_api_key.get_secret_value()
    synthesizer = asyncio.run(_korean_synthesizer(api_key, settings.gen_model, settings.openai_base_url))

    client = _generation_client(api_key, settings.openai_base_url)
    llm = make_llm(settings.gen_model, client)
    embeddings = OpenAIEmbeddings(client=client, model=settings.embedding_model)
    synthesizer.llm = llm
    cache_path = KG_CACHE_DIR / f"kg_{kg_cache_key(documents, settings.gen_model, settings.embedding_model)}.json"
    kg = build_knowledge_graph(documents, llm=llm, embeddings=embeddings, cache_path=cache_path)
    generator = TestsetGenerator(
        llm=llm, embedding_model=embeddings, knowledge_graph=kg, persona_list=list(YOUTH_PERSONAS)
    )
    testset = generator.generate(
        testset_size=testset_size,
        query_distribution=[(synthesizer, 1.0)],
        num_personas=len(YOUTH_PERSONAS),
    )
    return testset.to_list()


def _generation_client(api_key: str, base_url: str | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=DefaultAsyncHttpxClient(limits=NO_KEEPALIVE_LIMITS))


async def _korean_synthesizer(api_key: str, model: str, base_url: str | None = None) -> TolerantSingleHopSynthesizer:
    async with AsyncOpenAI(api_key=api_key, base_url=base_url) as client:
        llm = make_llm(model, client)
        synthesizer = TolerantSingleHopSynthesizer(llm=llm)
        prompts = await synthesizer.adapt_prompts(LANGUAGE, llm=llm)
        matching = prompts["themes_personas_matching_prompt"]
        prompts["themes_personas_matching_prompt"] = align_persona_example_names(matching)
        query_answer = prompts["query_answer_generation_prompt"]
        prompts["query_answer_generation_prompt"] = add_eligibility_instruction(query_answer)
        synthesizer.set_prompts(**prompts)
    return synthesizer


def add_eligibility_instruction(prompt):
    """페르소나가 정책 대상이 아닌데(예: 창업자 + 미취업자 사업) 정답이 대상 여부를 말하지 않으면,
    이를 짚는 RAG 답변이 오히려 감점된다(golden_v1 피드백). 정답이 대상 여부부터 밝히도록 지시를 덧붙인다."""
    if ELIGIBILITY_INSTRUCTION not in prompt.instruction:
        prompt.instruction = prompt.instruction + ELIGIBILITY_INSTRUCTION
    return prompt


def align_persona_example_names(prompt):
    """ragas 한국어 변환은 예시 입력의 페르소나 이름만 번역하고 답변 키는 영어로 둔다.
    LLM이 이를 따라 우리 이름을 영어로 바꿔 답하므로(golden_v1 생성 130건 중 14건) 답변 키를 입력 이름으로 맞춘다."""
    aligned = []
    for example_input, example_output in prompt.examples:
        names = [p.name for p in example_input.personas]
        concepts = list(example_output.mapping.values())
        if len(names) == len(concepts):
            example_output = PersonaThemesMapping(mapping=dict(zip(names, concepts)))
        aligned.append((example_input, example_output))
    prompt.examples = aligned
    return prompt
