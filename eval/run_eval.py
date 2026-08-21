"""Run the ADK model evaluation and emit submission-ready metrics.

Usage:
    python -m eval.run_eval
    python -m eval.run_eval --project my-google-cloud-project

The ADK CLI currently returns success even when inference fails, so this
wrapper reads the generated result file and exits non-zero unless every case
was actually evaluated and passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "eval"
HISTORY_DIR = EVAL_DIR / ".adk" / "eval_history"
RESULTS_DIR = EVAL_DIR / "results"
EVAL_SET = EVAL_DIR / "cross_session_recall.evalset.json"
CONFIG = EVAL_DIR / "test_config.json"

PASSED = 1
FAILED = 2


def _gcloud() -> str:
    executable = shutil.which("gcloud")
    if not executable:
        raise RuntimeError("gcloud is not installed or is not on PATH.")
    return executable


def _gcloud_project() -> str:
    result = subprocess.run(
        [_gcloud(), "config", "get-value", "project"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _check_cloud(project: str) -> str | None:
    """Return a concise preflight error without exposing credential material."""
    adc = subprocess.run(
        [_gcloud(), "auth", "application-default", "print-access-token"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if adc.returncode != 0:
        return "Google Cloud ADC is unavailable; run `gcloud auth application-default login`."

    enabled = subprocess.run(
        [
            _gcloud(),
            "services",
            "list",
            "--enabled",
            "--project",
            project,
            "--filter=config.name:aiplatform.googleapis.com",
            "--format=value(config.name)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if enabled.returncode != 0:
        detail = (enabled.stderr or enabled.stdout).strip().splitlines()
        reason = detail[-1].strip() if detail else "project lookup failed"
        return f"Cannot access Google Cloud project {project!r}: {reason}"
    if "aiplatform.googleapis.com" not in enabled.stdout:
        return (
            f"Vertex AI is not enabled in project {project!r}. Enable "
            "aiplatform.googleapis.com or select another project."
        )
    return None


def _metric_name(metric: dict[str, Any]) -> str:
    return str(metric.get("metric_name") or metric.get("metricName") or "")


def _metric_scores(cases: list[dict[str, Any]], name: str) -> list[float]:
    scores: list[float] = []
    for case in cases:
        metrics = case.get("overall_eval_metric_results") or []
        for metric in metrics:
            if _metric_name(metric) == name and isinstance(metric.get("score"), (int, float)):
                scores.append(float(metric["score"]))
    return scores


def _case_pass(cases: list[dict[str, Any]], eval_id: str) -> float | None:
    case = next((item for item in cases if item.get("eval_id") == eval_id), None)
    if case is None or not case.get("overall_eval_metric_results"):
        return None
    return 1.0 if case.get("final_eval_status") == PASSED else 0.0


def _summarize(
    result: dict[str, Any], *, elapsed: float, project: str, raw_log: Path
) -> dict[str, Any]:
    cases = result.get("eval_case_results") or []
    evaluated = [case for case in cases if case.get("overall_eval_metric_results")]
    passed = sum(case.get("final_eval_status") == PASSED for case in evaluated)
    failed = sum(case.get("final_eval_status") == FAILED for case in evaluated)
    trajectory = _metric_scores(cases, "tool_trajectory_avg_score")
    response = _metric_scores(cases, "response_match_score")

    # ADK evaluates the cases concurrently. This is an amortized wall-clock
    # latency, not a per-request percentile; the distinction is kept explicit
    # so submission material does not overstate the measurement.
    average_latency = elapsed / len(cases) if cases else None
    infrastructure_error = bool(cases) and not evaluated
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "project": project,
        "eval_set_id": result.get("eval_set_id"),
        "status": "infrastructure_error"
        if infrastructure_error
        else ("passed" if evaluated and passed == len(cases) else "failed"),
        "cases_total": len(cases),
        "cases_evaluated": len(evaluated),
        "cases_passed": passed,
        "cases_failed": failed,
        "trajectory_success_rate": statistics.fmean(trajectory) if trajectory else None,
        "memory_recall_consistency": _case_pass(cases, "run_2_declines_from_memory_alone"),
        "refusal_accuracy": _case_pass(cases, "run_2_declines_from_memory_alone"),
        "response_match_score": statistics.fmean(response) if response else None,
        "suite_wall_time_seconds": round(elapsed, 3),
        "average_latency_seconds": round(average_latency, 3) if average_latency else None,
        "latency_note": "Amortized suite wall time; ADK evaluates cases concurrently.",
        "raw_log": str(raw_log.relative_to(ROOT)).replace("\\", "/"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="Google Cloud project with Vertex AI enabled.")
    parser.add_argument(
        "--location",
        default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )
    args = parser.parse_args()

    try:
        project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT", "") or _gcloud_project()
    except RuntimeError as exc:
        print(f"Cloud preflight failed: {exc}", file=sys.stderr)
        return 2
    if not project:
        parser.error("Set GOOGLE_CLOUD_PROJECT or pass --project.")

    cloud_error = _check_cloud(project)
    if cloud_error:
        print(f"Cloud preflight failed: {cloud_error}", file=sys.stderr)
        return 2

    try:
        import google.adk.evaluation  # noqa: F401
    except ImportError:
        print('Install the evaluation extra: pip install -e ".[eval]"', file=sys.stderr)
        return 2

    env = os.environ.copy()
    env.update(
        {
            "GOOGLE_CLOUD_PROJECT": project,
            "GOOGLE_CLOUD_LOCATION": args.location,
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            "ARMCL_MEMORY_BACKEND": env.get("ARMCL_MEMORY_BACKEND", "memory"),
            # The evaluation measures agent behavior, not the separately tested
            # Model Armor service. Keep inference available without a template.
            "GUARDRAIL_BACKEND": env.get("GUARDRAIL_BACKEND", "regex"),
        }
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw_log = RESULTS_DIR / f"eval-{stamp}.log"
    command = [
        sys.executable,
        "-m",
        "google.adk.cli",
        "eval",
        "eval",
        str(EVAL_SET),
        "--config_file_path",
        str(CONFIG),
        "--print_detailed_results",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.time() - started
    output = completed.stdout + completed.stderr
    raw_log.write_text(output, encoding="utf-8")
    print(output, end="")

    candidates = [
        path
        for path in HISTORY_DIR.glob("*.evalset_result.json")
        if path.stat().st_mtime >= started - 1
    ]
    if not candidates:
        print(f"No ADK result file was produced. Raw output: {raw_log}", file=sys.stderr)
        return completed.returncode or 1

    result_path = max(candidates, key=lambda path: path.stat().st_mtime)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    summary = _summarize(result, elapsed=elapsed, project=project, raw_log=raw_log)
    summary_path = RESULTS_DIR / "latest_metrics.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print("\nSubmission metrics")
    print(json.dumps(summary, indent=2))
    print(f"Saved metrics to {summary_path}")
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
