"""Every setting an operator is told about in .env.example reaches the containers.

Found by the final review: Compose substitutes only the variables docker-compose.yml names, so a setting written
in .env but missing there (ALLOW_PRIVATE_TARGETS, METRICS_TOKEN, the judge thresholds...) silently did nothing."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_documented_setting_is_passed_to_the_containers():
    names = re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.MULTILINE)
    compose = (ROOT / "docker-compose.yml").read_text()
    missing = [n for n in names if "${" + n not in compose]
    assert not missing, f"in .env.example but not passed through by docker-compose.yml: {missing}"
    for hardening in ("METRICS_TOKEN", "ORG_MAX_QUEUED_RUNS", "ALLOW_PRIVATE_LLM_URLS", "GLOBAL_DAILY_COST_USD"):
        assert hardening in names, f"{hardening} is a setting operators need; document it in .env.example"


def test_every_service_that_touches_documents_shares_the_files_volume():
    """Found when the demo data was first loaded: migrate wrote documents to its own disk, the worker could not
    read them, and every document went dead."""
    import yaml

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("migrate", "api", "worker"):
        assert "files:/data/files" in services[name].get("volumes", []), name


def test_every_published_port_is_bound_to_loopback_unless_the_operator_opens_the_web_port():
    """The UI is published on the local machine only by default: WEB_BIND=0.0.0.0 is the operator's choice to make."""
    import yaml

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name, service in services.items():
        for port in service.get("ports", []):
            if name == "web":
                assert port == "${WEB_BIND:-127.0.0.1}:3300:80", port
            else:
                assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond this machine"
    names = re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.MULTILINE)
    assert "WEB_BIND" in names
