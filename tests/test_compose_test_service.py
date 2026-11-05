"""Static guards for the opt-in `test` service in docker-compose.yml.

A lost profile would start the runner on every `compose up`; a missing Azurite
connection string would make conftest try to launch a container from inside one.
"""

from pathlib import Path
from urllib.parse import urlsplit

import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
SERVICES = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]
TEST = SERVICES["test"]


def test_test_service_is_opt_in_only():
    assert TEST["profiles"] == ["test"]
    for name in ("web", "scheduler", "azurite", "db", "db-init"):
        assert "profiles" not in SERVICES[name]


def test_test_service_waits_for_db_and_azurite_only():
    assert set(TEST["depends_on"]) == {"db", "azurite"}
    for dependency in TEST["depends_on"].values():
        assert dependency["condition"] == "service_healthy"


def test_test_service_environment_targets_compose_network():
    env = TEST["environment"]
    assert env["TEST_DATABASE_URL"] == env["DATABASE_URL"]
    url = urlsplit(env["TEST_DATABASE_URL"])
    assert url.scheme == "mssql+pyodbc"
    assert url.hostname == "db"
    assert url.path == "/medcover_test"
    assert "BlobEndpoint=http://azurite:10000/" in env["BACKUP_STORAGE_CONNECTION_STRING"]


def test_test_service_has_no_env_file():
    assert "env_file" not in TEST
