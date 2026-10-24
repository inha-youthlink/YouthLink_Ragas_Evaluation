# CLI 서브커맨드 인자 파싱 테스트
from uuid import UUID

import pytest

from ragas_eval.generate import generate
from ragas_eval.report import report
from ragas_eval.runner import runner
from ragas_eval.runner.runner import RunOutcome
from ragas_eval.cli import build_parser, main


def test_generate_uses_default_options():
    args = build_parser().parse_args(["generate"])

    assert args.command == "generate"
    assert args.testset_size == 100
    assert args.out == "datasets/golden_candidates.jsonl"
    assert args.policy_limit is None


def test_generate_parses_policy_limit():
    args = build_parser().parse_args(["generate", "--policy-limit", "150"])

    assert args.policy_limit == 150


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_generate_rejects_non_positive_policy_limit(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["generate", "--policy-limit", value])


def test_run_parses_golden_and_repeat():
    args = build_parser().parse_args(["run", "--golden", "datasets/golden_v1.jsonl", "--repeat", "3"])

    assert args.command == "run"
    assert args.golden == "datasets/golden_v1.jsonl"
    assert args.repeat == 3


def test_run_requires_golden():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run"])


def test_run_offline_defaults_to_false():
    args = build_parser().parse_args(["run", "--golden", "datasets/golden_v1.jsonl"])

    assert args.offline is False


def test_run_parses_offline_flag():
    args = build_parser().parse_args(["run", "--golden", "datasets/golden_v1.jsonl", "--offline"])

    assert args.offline is True


def test_main_passes_offline_to_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run", lambda **kwargs: calls.append(kwargs) or [])

    main(["run", "--golden", "datasets/golden_v1.jsonl", "--offline"])

    assert calls == [
        {"golden_path": "datasets/golden_v1.jsonl", "repeat": 1, "offline": True, "baseline": False, "resume": None}
    ]


def test_run_parses_baseline_and_resume():
    args = build_parser().parse_args(["run", "--golden", "datasets/golden_v1.jsonl", "--baseline", "--resume", "abc"])

    assert (args.baseline, args.offline, args.resume) == (True, False, "abc")


def test_run_rejects_offline_with_baseline():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--golden", "datasets/golden_v1.jsonl", "--offline", "--baseline"])


def outcome(status, pending_answer=0):
    return RunOutcome(UUID(int=7), status, pending_answer, 0)


def test_main_prints_run_outcomes(monkeypatch, capsys):
    monkeypatch.setattr(runner, "run", lambda **kwargs: [outcome("SUCCEEDED")])

    main(["run", "--golden", "datasets/golden_v1.jsonl"])

    out = capsys.readouterr().out
    assert str(UUID(int=7)) in out and "SUCCEEDED" in out


def test_main_exits_nonzero_when_run_incomplete(monkeypatch, capsys):
    monkeypatch.setattr(runner, "run", lambda **kwargs: [outcome("RUNNING", pending_answer=2)])

    with pytest.raises(SystemExit) as exc:
        main(["run", "--golden", "datasets/golden_v1.jsonl"])

    assert exc.value.code == 1
    assert "RUNNING" in capsys.readouterr().out


def test_main_prints_generate_counts(monkeypatch, capsys):
    calls = []

    def fake_generate(**kwargs):
        calls.append(kwargs)
        return generate.GenerateSummary(written=3, dropped=1)

    monkeypatch.setattr(generate, "generate", fake_generate)

    main(["generate", "--testset-size", "10", "--out", "datasets/candidates.jsonl"])

    out = capsys.readouterr().out
    assert calls == [{"testset_size": 10, "out_path": "datasets/candidates.jsonl", "policy_limit": None}]
    assert "3건" in out and "1건" in out and "datasets/candidates.jsonl" in out


def test_report_parses_run_id_and_default_out_dir():
    args = build_parser().parse_args(["report", "--run", str(UUID(int=7))])

    assert (args.run, args.out_dir) == (UUID(int=7), "results")


def test_report_rejects_invalid_run_id():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["report", "--run", "not-a-uuid"])


def test_main_writes_report_and_prints_path(monkeypatch, capsys):
    calls = []

    def fake_report(run_id, out_dir):
        calls.append((run_id, out_dir))
        return f"{out_dir}/golden_v1_rag_r1_{run_id}.md"

    monkeypatch.setattr(report, "report", fake_report)

    main(["report", "--run", str(UUID(int=7)), "--out-dir", "out"])

    assert calls == [(UUID(int=7), "out")]
    assert f"out/golden_v1_rag_r1_{UUID(int=7)}.md" in capsys.readouterr().out


def test_route_requires_routing_dataset():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["route"])


def test_main_passes_routing_path_and_prints_outcome(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(runner, "route", lambda path: calls.append(path) or outcome("SUCCEEDED"))

    main(["route", "--routing", "datasets/routing_v1.jsonl"])

    assert calls == ["datasets/routing_v1.jsonl"]
    out = capsys.readouterr().out
    assert str(UUID(int=7)) in out and "SUCCEEDED" in out


def test_main_route_exits_nonzero_when_incomplete(monkeypatch):
    monkeypatch.setattr(runner, "route", lambda path: outcome("RUNNING", pending_answer=1))

    with pytest.raises(SystemExit) as exc:
        main(["route", "--routing", "datasets/routing_v1.jsonl"])

    assert exc.value.code == 1
