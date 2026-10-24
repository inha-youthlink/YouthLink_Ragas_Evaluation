# generate, run, route, report 서브커맨드를 정의하는 CLI
import argparse
from collections.abc import Sequence
from uuid import UUID

from ragas_eval.generate import generate
from ragas_eval.report import report
from ragas_eval.runner import runner


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("1 이상이어야 함")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ragas_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    generate_parser = sub.add_parser("generate", help="policy 테이블로 데이터셋 후보 생성")
    generate_parser.add_argument("--testset-size", type=int, default=100)
    generate_parser.add_argument("--out", default="datasets/golden_candidates.jsonl")
    generate_parser.add_argument(
        "--policy-limit", type=positive_int, default=None, help="대상 정책 중 고정 시드로 N건만 뽑아 생성 (분석 비용 제한)"
    )

    run_parser = sub.add_parser("run", help="확정 데이터셋으로 평가 실행")
    run_parser.add_argument("--golden", required=True)
    run_parser.add_argument("--repeat", type=int, default=1)
    mode = run_parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="RAG 대신 ground_truth·reference_contexts로 채점 경로 검증")
    mode.add_argument("--baseline", action="store_true", help="RAG 없이 BASELINE_MODEL에 직접 질문 (할루시네이션 기준선)")
    run_parser.add_argument("--resume", metavar="RUN_ID", help="RUNNING으로 남은 run의 미완료 샘플만 이어서 실행")

    route_parser = sub.add_parser("route", help="확정 라우팅 데이터셋으로 질문 유형 분류 평가")
    route_parser.add_argument("--routing", required=True)

    report_parser = sub.add_parser("report", help="run 결과를 마크다운 성적표로 저장")
    report_parser.add_argument("--run", required=True, type=UUID, metavar="RUN_ID")
    report_parser.add_argument("--out-dir", default=report.DEFAULT_OUT_DIR)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "generate":
        summary = generate.generate(testset_size=args.testset_size, out_path=args.out, policy_limit=args.policy_limit)
        print(f"후보 {summary.written}건 저장, 정책 매칭 실패·빈 값 {summary.dropped}건 제외: {args.out}")
    elif args.command == "run":
        outcomes = runner.run(
            golden_path=args.golden,
            repeat=args.repeat,
            offline=args.offline,
            baseline=args.baseline,
            resume=args.resume,
        )
        for o in outcomes:
            print(f"run {o.run_id}: {o.status} (미응답 {o.pending_answer}건, 미채점 {o.pending_scores}건)")
        # 미완료 run이 있으면 Job이 실패로 보이게 해 --resume이 필요함을 드러낸다
        if any(o.status != "SUCCEEDED" for o in outcomes):
            raise SystemExit(1)
    elif args.command == "route":
        o = runner.route(args.routing)
        print(f"run {o.run_id}: {o.status} (미응답 {o.pending_answer}건)")
        if o.status != "SUCCEEDED":
            raise SystemExit(1)
    elif args.command == "report":
        path = report.report(args.run, out_dir=args.out_dir)
        print(f"성적표 저장: {path}")
