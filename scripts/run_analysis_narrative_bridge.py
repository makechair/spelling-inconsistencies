#!/usr/bin/env python3
"""Fetch one compact analysis report from S3, run local Qwen, return its sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from usstocks.corpus.narrative import enrich


def _aws(*args: str, profile: str) -> None:
    executable = shutil.which("aws")
    if executable is None and Path("/opt/homebrew/bin/aws").is_file():
        executable = "/opt/homebrew/bin/aws"
    if executable is None:
        raise RuntimeError("aws CLI is required")
    subprocess.run([executable, *args, "--profile", profile], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange-s3-uri", required=True)
    parser.add_argument("--aws-profile", default="usstocks-qwen")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("~/Library/Application Support/usstocks-qwen").expanduser(),
    )
    args = parser.parse_args()
    root = args.exchange_s3_uri.rstrip("/")
    if not root.startswith("s3://"):
        raise SystemExit("--exchange-s3-uri must start with s3://")

    with tempfile.TemporaryDirectory(prefix="usstocks-qwen-") as temporary:
        report_path = Path(temporary) / "report.json"
        digest_path = Path(temporary) / "ai_digest.json"
        _aws(
            "s3", "cp", f"{root}/input/latest/report.json", str(report_path),
            "--only-show-errors", profile=args.aws_profile,
        )
        report_digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        state_path = args.state_dir / "analysis-bridge-state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("report_digest") == report_digest:
                print(json.dumps({"status": "unchanged"}))
                return
        payload = enrich(
            report_path,
            digest_path,
            model=args.model,
            ollama_url=args.ollama_url,
            timeout=900,
        )
        report_date = str(payload["report_date"])
        _aws(
            "s3", "cp", str(digest_path),
            f"{root}/output/daily/date={report_date}/ai_digest.json",
            "--only-show-errors", profile=args.aws_profile,
        )
        args.state_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {"report_digest": report_digest, "report_date": report_date},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"report_date": report_date, "model": args.model}, ensure_ascii=False))


if __name__ == "__main__":
    main()
