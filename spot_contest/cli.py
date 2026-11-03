import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .data import events, file_hash, prepare
from .engine import Judge
from .models import Config, dumps, fingerprint
from .sandbox import StrategyProcess


def submission_hash(path: Path) -> str:
    files = []
    total = 0
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError("submission symlinks are forbidden")
        if item.is_file():
            if "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            total += item.stat().st_size
            if total > 1024**3:
                raise ValueError("submission exceeds 1 GiB")
            files.append((str(item.relative_to(path)), file_hash(item)))
    if not files:
        raise ValueError("submission is empty")
    return fingerprint(files)


def evaluate(
    config_path: Path,
    dataset: Path,
    submission: Path,
    output: Path,
    runner: str = "docker",
    image: str = "spot-contest-strategy:0.1.0",
    runtime: str = "",
):
    config = Config.load(config_path)
    if output.exists():
        raise ValueError("output already exists; choose a new run directory")
    if not submission.is_dir():
        raise ValueError("submission must be a directory")
    # Never stage a directory containing the judge dataset, credentials or outputs.
    root = submission.resolve()
    if root in dataset.resolve().parents or root == dataset.resolve():
        raise ValueError("dataset must be outside the submission directory")
    if root == output.resolve() or root in output.resolve().parents:
        raise ValueError("output must be outside the submission directory")
    original_hash = submission_hash(root)
    dataset_digest = file_hash(dataset)
    output.mkdir(parents=True)
    metadata = {
        "evaluator_version": __version__,
        "evaluator_sha256": fingerprint(
            [(p.name, file_hash(p)) for p in sorted(Path(__file__).parent.glob("*.py"))]
        ),
        "rules": config.raw,
        "config_sha256": fingerprint(config.raw),
        "dataset_sha256": dataset_digest,
        "submission_sha256": original_hash,
        "runner": runner,
        "status": "running",
    }
    if runner == "docker":
        metadata["strategy_image"] = image
        metadata["docker_runtime"] = runtime or "default"
    (output / "run.json").write_text(dumps(metadata, indent=2) + "\n")
    try:
        with tempfile.TemporaryDirectory(prefix="spot-submission-") as temporary:
            staged = Path(temporary) / "submission"
            shutil.copytree(root, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if submission_hash(staged) != original_hash:
                raise ValueError("submission changed while staging")
            # The unprivileged container user must be able to read the staged model.
            staged.chmod(0o755)
            for item in staged.rglob("*"):
                item.chmod(0o755 if item.is_dir() else 0o644)
            with (output / "audit.jsonl").open("w") as log:

                def audit(row):
                    log.write(dumps(row, separators=(",", ":")) + "\n")

                with StrategyProcess(staged, config, runner, image, runtime) as strategy:
                    if strategy.image_id:
                        metadata["strategy_image_id"] = strategy.image_id
                    result = Judge(config, strategy, audit).run(events(dataset))
        if file_hash(dataset) != dataset_digest:
            raise ValueError("dataset changed during evaluation")
        (output / "result.json").write_text(dumps(result, indent=2) + "\n")
        # Numeric values, in the format expected by a leaderboard/scoring adapter.
        columns = (
            "score",
            "net_pnl",
            "max_drawdown",
            "turnover",
            "trading_fees",
            "terminal_fees",
            "peak_net_exposure",
            "orders",
            "filled_orders",
            "rejected_orders",
        )
        scores = {name: float(result[name]) for name in columns}
        (output / "scores.json").write_text(dumps(scores, indent=2) + "\n")
        metadata["status"] = "completed"
        metadata["audit_sha256"] = file_hash(output / "audit.jsonl")
        metadata["result_sha256"] = file_hash(output / "result.json")
        metadata["scores_sha256"] = file_hash(output / "scores.json")
        return result
    except Exception as exc:
        metadata.update(status="failed", error=str(exc))
        # Failed evaluations never leave a usable score behind.
        for name in ("result.json", "scores.json"):
            (output / name).unlink(missing_ok=True)
        raise
    finally:
        (output / "run.json").write_text(dumps(metadata, indent=2) + "\n")


def make_demo(directory: Path):
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("demo directory must be new or empty")
    directory.mkdir(parents=True, exist_ok=True)
    example = Path(__file__).resolve().parent.parent / "examples"
    if not example.is_dir():
        raise ValueError("demo assets require a source checkout / editable installation")
    shutil.copyfile(example / "config.json", directory / "config.json")
    shutil.copytree(example / "baseline", directory / "baseline")
    shutil.copytree(example / "hold", directory / "hold")
    dataset = directory / "market_frames.jsonl"
    # Explicitly synthetic schema-v4 frames, not purported exchange history.
    with dataset.open("w") as stream:
        for i in range(31):
            for name in ("binance", "bybit"):
                bid, ask = ("99.99", "100.00") if name == "binance" else ("100.80", "100.81")
                if i >= 25:
                    bid, ask = "100.10", "100.11"
                t = 1_000_000_000 + i * 100_000_000
                row = {
                    "schema_version": 4,
                    "exchange": name,
                    "market_type": 0,
                    "symbol": "SOLUSDT",
                    "window_start_ns": t - 100_000_000,
                    "window_end_ns": t,
                    "ts_exchange_ns": t - 2_000_000,
                    "ts_recv_ns": t - 1_000_000,
                    "ts_publish_ns": t,
                    "producer_session_id": 1,
                    "stream_epoch": 1,
                    "update_id": i + 1,
                    "valid": True,
                    "is_filled": False,
                    "book_age_ns": 2_000_000,
                    "bid_count": 1,
                    "ask_count": 1,
                    "bid_prices": [bid],
                    "bid_quantities": ["1000"],
                    "ask_prices": [ask],
                    "ask_quantities": ["1000"],
                    "trade_count": 5,
                    "buy_qty": "2",
                    "sell_qty": "3",
                    "trade_notional": "500",
                }
                stream.write(dumps(row) + "\n")
    manifest = directory / "sources.json"
    manifest.write_text(
        dumps({"sources": [{"kind": "market_frames", "path": dataset.name}]}, indent=2)
    )
    return prepare(manifest, directory / "events.jsonl")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Spot–spot taker–taker contest")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="generate an explicitly synthetic example dataset")
    demo.add_argument("--output", type=Path, required=True)
    prep = sub.add_parser("prepare", help="normalize and sort HFT JSONL/Parquet sources")
    prep.add_argument("--manifest", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate", help="validate prepared events against competition rules")
    validate.add_argument("--data", type=Path, required=True)
    validate.add_argument("--config", type=Path, required=True)
    run = sub.add_parser("evaluate", help="run a strategy; Docker is the default")
    for name in ("config", "data", "submission", "output"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument("--runner", choices=("docker", "subprocess"), default="docker")
    run.add_argument("--image", default="spot-contest-strategy:0.1.0")
    run.add_argument("--runtime", default="", help="optional Docker runtime, e.g. runsc")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = make_demo(args.output)
        elif args.command == "prepare":
            result = prepare(args.manifest, args.output)
        elif args.command == "validate":
            from .engine import Book

            config = Config.load(args.config)
            books = {name: Book(config.depth_fraction) for name in config.venues}
            count = 0
            kinds = set()
            for event in events(args.data):
                if event["symbol"] != config.symbol or event["exchange"] not in books:
                    raise ValueError("unconfigured instrument/venue")
                if event["event_type"] == "book":
                    books[event["exchange"]].apply(event)
                kinds.add(event["source"])
                count += 1
            if not count or not all(
                b.fresh(event["market_ts_ns"], config.max_book_age_ns) for b in books.values()
            ):
                raise ValueError("missing/invalid terminal books")
            result = {"events": count, "time_models": sorted(kinds), "sha256": file_hash(args.data)}
        else:
            if args.runner == "subprocess":
                print(
                    "LOCAL DEVELOPMENT ONLY: subprocess does not isolate participant code.",
                    file=sys.stderr,
                )
            result = evaluate(
                args.config,
                args.data,
                args.submission,
                args.output,
                args.runner,
                args.image,
                args.runtime,
            )
        print(dumps(result, indent=2))
        return 0
    except (ValueError, KeyError, OSError, ArithmeticError, TimeoutError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
