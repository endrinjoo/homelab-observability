"""Static checks that catch broken wiring without needing Docker."""

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]

# The metrics netpulse exports. Histograms also expose _bucket, _count and _sum series.
KNOWN_METRICS = {
    "netpulse_target_up",
    "netpulse_check_latency_seconds_bucket",
    "netpulse_checks_total",
    "netpulse_tls_cert_days_remaining",
}


def load(relative: str) -> dict:
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


def metrics_in(expr: str) -> set[str]:
    return set(re.findall(r"\bnetpulse_[a-z0-9_]+", expr))


def alert_rules() -> list[dict]:
    return [r for g in load("prometheus/rules/netpulse.yml")["groups"] for r in g["rules"]]


def dashboard() -> dict:
    return json.loads((ROOT / "grafana/dashboards/netpulse.json").read_text(encoding="utf-8"))


def test_images_are_pinned():
    for name, service in SERVICES.items():
        if "image" in service:
            assert ":" in service["image"], name
            assert not service["image"].endswith(":latest"), name


def test_published_ports_only_listen_on_localhost():
    for name, service in SERVICES.items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), name


def test_local_mounts_exist():
    for name, service in SERVICES.items():
        for volume in service.get("volumes", []):
            source = volume.split(":")[0]
            if source.startswith("./"):
                assert (ROOT / source).exists(), f"{name}: {source}"


def test_named_volumes_are_declared():
    for service in SERVICES.values():
        for volume in service.get("volumes", []):
            source = volume.split(":")[0]
            if not source.startswith("./"):
                assert source in COMPOSE["volumes"]


def test_every_service_restarts():
    assert all(s["restart"] == "unless-stopped" for s in SERVICES.values())


def test_grafana_password_must_come_from_the_environment():
    password = SERVICES["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"]
    assert password.startswith("${GRAFANA_ADMIN_PASSWORD:?")


def test_prometheus_scrapes_services_that_exist():
    config = load("prometheus/prometheus.yml")
    for job in config["scrape_configs"]:
        for static in job["static_configs"]:
            for target in static["targets"]:
                host = target.split(":")[0]
                assert host == "localhost" or host in SERVICES


def test_alert_rules_use_real_metrics():
    used = set()
    for rule in alert_rules():
        used |= metrics_in(rule["expr"])
    assert used
    assert used <= KNOWN_METRICS


def test_alert_rules_have_severity_and_summary():
    for rule in alert_rules():
        assert rule["labels"]["severity"] in {"critical", "warning"}
        assert rule["annotations"]["summary"]


def test_every_alert_has_a_promtool_test_except_slow_latency():
    tested = {
        case["alertname"]
        for test in load("prometheus/tests/netpulse_test.yml")["tests"]
        for case in test["alert_rule_test"]
    }
    names = {rule["alert"] for rule in alert_rules()}
    assert names - tested == {"NetpulseHighLatency"}


def test_alertmanager_sends_to_the_logger_service():
    config = load("alertmanager/alertmanager.yml")
    assert config["route"]["receiver"] in {r["name"] for r in config["receivers"]}
    url = config["receivers"][0]["webhook_configs"][0]["url"]
    assert "alert-logger:9000" in url
    assert "alert-logger" in SERVICES


def test_dashboard_uses_real_metrics_and_the_provisioned_datasource():
    uid = load("grafana/provisioning/datasources/prometheus.yml")["datasources"][0]["uid"]
    panels = dashboard()["panels"]
    assert panels
    for panel in panels:
        assert panel["datasource"]["uid"] == uid
        for query in panel["targets"]:
            assert metrics_in(query["expr"]) <= KNOWN_METRICS


def test_dashboard_panels_do_not_overlap():
    def cells(panel):
        box = panel["gridPos"]
        return {
            (x, y)
            for x in range(box["x"], box["x"] + box["w"])
            for y in range(box["y"], box["y"] + box["h"])
        }

    seen = set()
    for panel in dashboard()["panels"]:
        assert not seen & cells(panel), panel["title"]
        assert panel["gridPos"]["x"] + panel["gridPos"]["w"] <= 24
        seen |= cells(panel)


def test_monitored_stack_services_exist():
    for target in load("netpulse/netpulse.yaml")["targets"]:
        match = re.match(r"http://([a-z-]+):", target.get("url", ""))
        if match:
            assert match.group(1) in SERVICES


@pytest.fixture
def alert_logger():
    spec = importlib.util.spec_from_file_location("server", ROOT / "alert-logger" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alert_logger_formats_a_real_payload(alert_logger):
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "NetpulseTargetDown", "severity": "critical"},
                "annotations": {"summary": "web is down"},
            }
        ]
    }
    assert alert_logger.format_alerts(payload) == [
        "[FIRING] critical NetpulseTargetDown: web is down"
    ]


def test_alert_logger_tolerates_missing_fields(alert_logger):
    assert alert_logger.format_alerts({}) == []
    assert alert_logger.format_alerts({"alerts": [{}]}) == [
        "[UNKNOWN] none unnamed: no summary"
    ]
