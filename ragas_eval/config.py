# 평가에 필요한 환경변수를 읽는 설정
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # RAG .env와 같이 빈 값은 기본값으로 본다 (필수 값을 비우면 누락으로 실패)
        env_ignore_empty=True,
    )

    database_url: SecretStr
    openai_api_key: SecretStr
    # 비우면 OpenAI 기본 주소. OpenAI 호환 프록시를 쓸 때만 지정 (예: https://host/v1)
    openai_base_url: str | None = None
    rag_base_url: str = "http://localhost:8000"
    # RAG INTERNAL_API_KEY와 같은 값. /internal 호출에 X-Internal-Key로 보낸다. 비우면 헤더 없이 호출 (RAG 개발 모드)
    rag_internal_api_key: SecretStr | None = None

    gen_model: str
    judge_model: str
    embedding_model: str = "bedrock-titan-embed-v2"
    # run --baseline에서만 사용. RAG chat_model과 같은 모델로 둬야 공정 비교가 된다
    baseline_model: str | None = None
    # baseline·채점 OpenAI 호출. 공정 비교를 위해 RAG OPENAI_TIMEOUT·OPENAI_MAX_RETRIES 기본값과 맞춘다
    openai_timeout: float = Field(default=30.0, gt=0)
    openai_max_retries: int = Field(default=2, ge=0)
    # 채점·baseline LLM 동시 요청 상한. 샘플 동시 실행 × 지표 병렬 호출이 게이트웨이 키당 제한(8)을 넘어 429가 나므로 연결 수로 묶는다
    llm_max_concurrency: int = Field(default=6, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
