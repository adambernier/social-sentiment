import json
import math
import pytest

from scripts.conformal_calibrate import (
    fit_calibration,
    horvitz_thompson_mean,
    main,
    split_conformal_quantile,
    split_conformal_rank,
)


def test_split_conformal_quantile_matches_exact_finite_sample_formula():
    assert split_conformal_rank(19, 0.10) == 18
    scores = [i / 20.0 for i in range(1, 20)]
    assert math.isclose(split_conformal_quantile(scores, 0.10), 18.0 / 20.0)
    # n=10, alpha=0.05 -> rank=11 > 10 -> vacuous fallback 1.0
    assert split_conformal_quantile(scores[:10], 0.05) == 1.0


def test_horvitz_thompson_mean_recovers_inverse_probability_weighted_mean():
    est = horvitz_thompson_mean([(1.0, 0.10), (-1.0, 0.50)])
    assert math.isclose(est, 2.0 / 3.0, rel_tol=1e-12)


def test_fit_calibration_computes_sentiment_topic_and_ht_bias(tmp_path):
    rows = []
    for i in range(20):
        # 18 confident correct positive predictions, 2 ambiguous ones
        p_pos = 0.85 if i < 18 else 0.45
        p_neu = 1.0 - p_pos
        rows.append(
            {
                "sentiment_model_hash": "a" * 64,
                "topic_model_hash": "b" * 64,
                "scores": {"positive": p_pos, "neutral": p_neu, "negative": 0.0},
                "true_sentiment": "positive",
                "inclusion_probability": 0.05 if i < 10 else 0.10,
                "true_topic_id": 3,
                "topic_entailment_probs": [0.1, 0.1, 0.1, 0.78 if i < 19 else 0.55, 0.1, 0.1, 0.1, 0.1],
            }
        )

    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out_file = tmp_path / "calibration.json"

    assert main(["fit", "--input", str(audit_file), "--alpha", "0.10", "--output", str(out_file)]) == 0

    report = json.loads(out_file.read_text(encoding="utf-8"))
    assert report["sentiment"]["sample_count"] == 20
    # ceil(21 * 0.90) = 19 -> 19th of 20 sorted nonconformity scores (18 at 0.15, 2 at 0.55) -> 0.55
    assert math.isclose(report["sentiment"]["q_hat"], 0.55, abs_tol=1e-9)
    assert report["sentiment"]["horvitz_thompson"]["audited_mean_signal"] == 1.0
    assert report["sentiment"]["horvitz_thompson"]["estimated_bias"] > 0.0

    # Topic: 19 at 0.22, 1 at 0.45 -> 19th of 20 -> 0.22 -> threshold 0.78
    assert math.isclose(report["topic"]["q_hat"], 0.22, abs_tol=1e-9)
    assert report["topic"]["recommended_env"]["TOPIC_ENTAILMENT_THRESHOLD"] == "0.780000"


def test_fit_calibration_rejects_invalid_alpha():
    with pytest.raises(ValueError):
        split_conformal_quantile([0.1, 0.2], 0.0)
    with pytest.raises(ValueError):
        fit_calibration(
            [
                {
                    "scores": {"positive": 0.8, "neutral": 0.1, "negative": 0.1},
                    "true_sentiment": "positive",
                }
            ],
            alpha=1.0,
        )
