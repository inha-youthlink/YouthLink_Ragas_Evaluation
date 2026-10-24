# 환경변수 설정(Settings) 필수 값과 기본값 테스트
import pytest
from pydantic import ValidationError

from ragas_eval.config import Settings

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://user:pw@localhost:5432/test_db",
    "OPENAI_API_KEY": "test-key",
    "GEN_MODEL": "test-gen-model",
    "JUDGE_MODEL": "test-judge-model",
}


@pytest.fixture
def required_env(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("BASELINE_MODEL", "OPENAI_TIMEOUT", "OPENAI_MAX_RETRIES", "EMBEDDING_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


def test_baseline_model_defaults_to_none(required_env):
    settings = Settings(_env_file=None)

    assert settings.baseline_model is None


def test_baseline_model_is_read_from_env(required_env, monkeypatch):
    monkeypatch.setenv("BASELINE_MODEL", "test-baseline-model")

    settings = Settings(_env_file=None)

    assert settings.baseline_model == "test-baseline-model"


@pytest.mark.parametrize("key", list(REQUIRED_ENV))
def test_missing_required_value_fails(required_env, monkeypatch, key):
    monkeypatch.delenv(key)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_openai_call_settings_default_to_rag_values(required_env):
    settings = Settings(_env_file=None)

    assert (settings.openai_timeout, settings.openai_max_retries) == (30.0, 2)


def test_empty_optional_values_fall_back_to_defaults(required_env, monkeypatch):
    for key in ("OPENAI_TIMEOUT", "OPENAI_MAX_RETRIES", "EMBEDDING_MODEL", "BASELINE_MODEL"):
        monkeypatch.setenv(key, "")

    settings = Settings(_env_file=None)

    assert (settings.openai_timeout, settings.openai_max_retries) == (30.0, 2)
    assert (settings.embedding_model, settings.baseline_model) == ("text-embedding-3-small", None)


def test_empty_required_value_fails(required_env, monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", "")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(("key", "value"), [("OPENAI_TIMEOUT", "0"), ("OPENAI_MAX_RETRIES", "-1")])
def test_invalid_openai_call_settings_fail(required_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_openai_base_url_defaults_to_none(required_env):
    settings = Settings(_env_file=None)

    assert settings.openai_base_url is None


def test_openai_base_url_is_read_from_env(required_env, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")

    settings = Settings(_env_file=None)

    assert settings.openai_base_url == "https://proxy.example/v1"
