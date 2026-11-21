"""Split-conformal calibration and Horvitz-Thompson debiasing CLI (Phase 3b).

Exports an unlabeled probability sample from `raw_post_archive_staging`
(`cohort = 'probability'`, pinned to a model SHA-256 hash) for fast human
auditing, and fits exact finite-sample conformal thresholds + Hájek
Horvitz-Thompson bias estimates from the labeled JSONL file.

Usage:
    # 1. Export up to 200 probability-cohort posts for labeling:
    python scripts/conformal_calibrate.py export \
        --output artifacts/audit-batch.jsonl --limit 200

    # 2. Label `true_sentiment` ("positive"|"neutral"|"negative") and/or
    #    `true_topic_id` (0..7 or -1) in artifacts/audit-batch.jsonl.

    # 3. Fit calibrated split-conformal quantiles and Horvitz-Thompson debiasing:
    python scripts/conformal_calibrate.py fit \
        --input artifacts/audit-batch.jsonl --alpha 0.10
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

SENTIMENT_LABELS = ("neutral", "positive", "negative")
SENTIMENT_TO_SIGNAL = {"negative": -1.0, "neutral": 0.0, "positive": 1.0}


def split_conformal_rank(n: int, alpha: float) -> int:
    """Return 1-based rank ceil((n + 1) * (1 - alpha)), or n + 1 if vacuous."""
    if n <= 0:
        raise ValueError("calibration set must be non-empty")
    if not math.isfinite(alpha) or not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must lie strictly in (0, 1), got {alpha}")
    rank = math.ceil((n + 1) * (1.0 - alpha))
    return max(1, min(n + 1, rank))


def split_conformal_quantile(scores: list[float], alpha: float) -> float:
    """Exact finite-sample split-conformal quantile in [0.0, 1.0].

    Matches `crates/social-sentiment-math/src/conformal.rs::split_conformal_quantile`.
    """
    if not scores:
        raise ValueError("calibration scores must be non-empty")
    for s in scores:
        if not math.isfinite(s) or not (0.0 <= s <= 1.0):
            raise ValueError(f"nonconformity score must be finite in [0, 1], got {s}")
    ordered = sorted(scores)
    rank = split_conformal_rank(len(ordered), alpha)
    if rank > len(ordered):
        return 1.0
    return ordered[rank - 1]


def horvitz_thompson_mean(samples: list[tuple[float, float]]) -> float:
    """Hájek self-normalized Horvitz-Thompson estimator for bounded signals in [-1, 1].

    Matches `crates/social-sentiment-math/src/conformal.rs::horvitz_thompson_mean`.
    """
    if not samples:
        raise ValueError("samples must be non-empty")
    weighted_num = 0.0
    weight_sum = 0.0
    for signal, inclusion_prob in samples:
        if not math.isfinite(signal) or not (-1.0 <= signal <= 1.0):
            raise ValueError(f"signal must be finite in [-1, 1], got {signal}")
        if not math.isfinite(inclusion_prob) or not (0.0 < inclusion_prob <= 1.0):
            raise ValueError(
                f"inclusion_probability must be finite in (0, 1], got {inclusion_prob}"
            )
        inv_pi = 1.0 / inclusion_prob
        weighted_num += signal * inv_pi
        weight_sum += inv_pi
    return max(-1.0, min(1.0, weighted_num / weight_sum))


def fit_calibration(rows: list[dict[str, Any]], alpha: float = 0.10) -> dict[str, Any]:
    """Fit sentiment/topic split-conformal quantiles and Horvitz-Thompson debiasing."""
    sentiment_scores: list[float] = []
    sentiment_probs_rows: list[dict[str, float]] = []
    sentiment_hashes: set[str] = set()
    ht_true_samples: list[tuple[float, float]] = []
    ht_pred_samples: list[tuple[float, float]] = []

    topic_scores: list[float] = []
    topic_hashes: set[str] = set()

    for row in rows:
        true_sent = row.get("true_sentiment")
        scores = row.get("scores")
        if true_sent in SENTIMENT_LABELS and isinstance(scores, dict):
            if all(lbl in scores for lbl in SENTIMENT_LABELS):
                p_true = float(scores[true_sent])
                sentiment_scores.append(max(0.0, min(1.0, 1.0 - p_true)))
                sentiment_probs_rows.append(
                    {lbl: float(scores[lbl]) for lbl in SENTIMENT_LABELS}
                )
                if row.get("sentiment_model_hash"):
                    sentiment_hashes.add(str(row["sentiment_model_hash"]))

                inc_p = row.get("inclusion_probability")
                if inc_p is not None and 0.0 < float(inc_p) <= 1.0:
                    y_true = SENTIMENT_TO_SIGNAL[true_sent]
                    y_pred = float(scores["positive"]) - float(scores["negative"])
                    ht_true_samples.append((y_true, float(inc_p)))
                    ht_pred_samples.append((max(-1.0, min(1.0, y_pred)), float(inc_p)))

        true_topic_id = row.get("true_topic_id")
        topic_entail_probs = row.get("topic_entailment_probs")
        if (
            isinstance(true_topic_id, int)
            and true_topic_id >= 0
            and isinstance(topic_entail_probs, list)
            and true_topic_id < len(topic_entail_probs)
        ):
            p_topic = float(topic_entail_probs[true_topic_id])
            topic_scores.append(max(0.0, min(1.0, 1.0 - p_topic)))
            if row.get("topic_model_hash"):
                topic_hashes.add(str(row["topic_model_hash"]))

    result: dict[str, Any] = {
        "alpha": alpha,
        "target_coverage": round(1.0 - alpha, 6),
        "total_rows": len(rows),
    }

    if sentiment_scores:
        q_hat = split_conformal_quantile(sentiment_scores, alpha)
        min_prob = max(0.0, min(1.0, 1.0 - q_hat))
        set_sizes = [
            sum(1 for lbl in SENTIMENT_LABELS if probs[lbl] >= min_prob)
            for probs in sentiment_probs_rows
        ]
        singletons = sum(1 for sz in set_sizes if sz == 1)
        result["sentiment"] = {
            "sample_count": len(sentiment_scores),
            "model_hashes": sorted(sentiment_hashes),
            "q_hat": q_hat,
            "min_class_probability": min_prob,
            "singleton_rate": singletons / len(set_sizes),
            "avg_set_size": sum(set_sizes) / len(set_sizes),
        }
        if ht_true_samples:
            ht_true = horvitz_thompson_mean(ht_true_samples)
            ht_pred = horvitz_thompson_mean(ht_pred_samples)
            result["sentiment"]["horvitz_thompson"] = {
                "sample_count": len(ht_true_samples),
                "audited_mean_signal": ht_true,
                "model_mean_signal": ht_pred,
                "estimated_bias": ht_true - ht_pred,
            }

    if topic_scores:
        topic_q_hat = split_conformal_quantile(topic_scores, alpha)
        topic_threshold = max(0.0, min(1.0, 1.0 - topic_q_hat))
        result["topic"] = {
            "sample_count": len(topic_scores),
            "model_hashes": sorted(topic_hashes),
            "q_hat": topic_q_hat,
            "recommended_env": {
                "TOPIC_CONFORMAL_Q_HAT": f"{topic_q_hat:.6f}",
                "TOPIC_ENTAILMENT_THRESHOLD": f"{topic_threshold:.6f}",
            },
        }

    return result


def export_audit_batch(
    dsn: str,
    output: Path,
    *,
    limit: int = 200,
    sentiment_model_hash: str | None = None,
    topic_model_hash: str | None = None,
) -> int:
    """Export probability-cohort posts from PostgreSQL to a JSONL audit file."""
    import psycopg

    query = """
        SELECT
            platform,
            source_post_id,
            symbol,
            text,
            event_at,
            sentiment,
            scores,
            topic_id,
            topic_label,
            topic_model_version,
            topic_model_hash,
            sentiment_model_version,
            sentiment_model_hash,
            inclusion_probability,
            stratum
        FROM raw_post_archive_staging
        WHERE cohort = 'probability'
          AND (%s::TEXT IS NULL OR sentiment_model_hash = %s)
          AND (%s::TEXT IS NULL OR topic_model_hash = %s)
        ORDER BY staged_at DESC
        LIMIT %s
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur, output.open(
        "w", encoding="utf-8"
    ) as fh:
        cur.execute(
            query,
            (
                sentiment_model_hash,
                sentiment_model_hash,
                topic_model_hash,
                topic_model_hash,
                limit,
            ),
        )
        for row in cur.fetchall():
            record = {
                "platform": row[0],
                "source_post_id": row[1],
                "symbol": row[2],
                "text": row[3],
                "event_at": row[4].isoformat() if row[4] is not None else None,
                "predicted_sentiment": row[5],
                "scores": row[6],
                "predicted_topic_id": row[7],
                "predicted_topic_label": row[8],
                "topic_model_version": row[9],
                "topic_model_hash": row[10],
                "sentiment_model_version": row[11],
                "sentiment_model_hash": row[12],
                "inclusion_probability": row[13],
                "stratum": row[14],
                "true_sentiment": None,
                "true_topic_id": None,
            }
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export probability-cohort audit batches and fit conformal calibration."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Export unlabeled probability-cohort posts to JSONL")
    exp.add_argument(
        "--dsn",
        default=os.environ.get(
            "DATABASE_DSN", "postgresql://postgres:sentiment@localhost:5432/sentiment"
        ),
    )
    exp.add_argument("--output", type=Path, required=True)
    exp.add_argument("--limit", type=int, default=200)
    exp.add_argument("--sentiment-model-hash", default=None)
    exp.add_argument("--topic-model-hash", default=None)

    fit = sub.add_parser("fit", help="Fit conformal thresholds and HT bias from labeled JSONL")
    fit.add_argument("--input", type=Path, required=True)
    fit.add_argument("--alpha", type=float, default=0.10)
    fit.add_argument("--output", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "export":
        exported = export_audit_batch(
            args.dsn,
            args.output,
            limit=args.limit,
            sentiment_model_hash=args.sentiment_model_hash,
            topic_model_hash=args.topic_model_hash,
        )
        print(f"Exported {exported} probability-cohort posts to {args.output}")
        return 0

    rows = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = fit_calibration(rows, alpha=args.alpha)
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
