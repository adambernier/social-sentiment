import json
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).parents[1]


def test_prometheus_scrapes_alpaca_and_loads_producer_alerts():
    config = yaml.safe_load((ROOT / "prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}

    assert "news-producer" not in jobs
    assert jobs["alpaca-producer"]["static_configs"] == [
        {"targets": ["alpaca-producer:8003"]}
    ]
    assert config["rule_files"] == ["/etc/prometheus/alerts.yml"]

    rules = yaml.safe_load((ROOT / "prometheus-alerts.yml").read_text())
    alert_names = {
        rule["alert"] for group in rules["groups"] for rule in group["rules"]
    }
    assert alert_names == {"ProducerMetricsTargetDown", "ProducerRateLimitBurst"}


def test_system_health_dashboard_distinguishes_zero_from_missing_metrics():
    dashboard = json.loads(
        (ROOT / "grafana/provisioning/dashboards/SystemHealth.json").read_text()
    )
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    events = panels["Rate-Limit Events"]
    assert "$__rate_interval" in events["targets"][0]["expr"]
    assert events["fieldConfig"]["defaults"]["noValue"] == "0"
    assert events["fieldConfig"]["defaults"]["custom"]["drawStyle"] == "bars"

    health = panels["Producer Scrape Health"]
    assert health["targets"][0]["expr"] == 'up{job=~".*-producer"}'
    assert health["fieldConfig"]["defaults"]["noValue"] == "NO TARGET"

    warnings = panels["Active Producer Warnings"]
    assert "ALERTS" in warnings["targets"][0]["expr"]
