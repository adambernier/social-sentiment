from prometheus_client import generate_latest

from shared.metrics import initialize_rate_limit_metrics


def test_rate_limit_metric_is_exposed_before_first_event():
    initialize_rate_limit_metrics("fixture-zero")

    metrics = generate_latest().decode()
    assert 'rate_limits_hit_total{platform="fixture-zero"} 0.0' in metrics
