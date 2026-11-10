"""Browser checks that the table manager keeps its status and type filters across reloads."""

import pytest

DEFAULT_STATUSES = ["DRAFT", "PUBLISHED", "ASSIGNMENTS_OPEN", "ASSIGNMENTS_CLOSED"]
ALL_TYPES = ["MEDICAL_COVER", "TRAINING", "PRESENTATION"]


@pytest.fixture()
def errors(logged_in_page):
    collected = []
    logged_in_page.on("pageerror", lambda exc: collected.append(str(exc)))
    return collected


def open_table_manager(page, base_url, master_event_name):
    page.goto(f"{base_url}/master-events/")
    page.locator("tr", has_text=master_event_name).get_by_role("link", name="Tabulkový manažer").click()
    page.wait_for_url("**/table")
    assert page.locator("p.text-muted.small", has_text=master_event_name).count() == 1


def active(page, bar, attr):
    return page.locator(f"#tm-{bar}-filter .filter-btn.active").evaluate_all(
        f"buttons => buttons.map(button => button.dataset.{attr})"
    )


def test_filter_survives_reload_and_applies_to_other_master_events(logged_in_page, base_url, errors):
    page = logged_in_page
    open_table_manager(page, base_url, "Házenkářská liga")
    medical_rows = page.locator('#tm-table tbody tr[data-event-type="MEDICAL_COVER"]')
    assert medical_rows.count() > 0
    page.locator('#tm-type-filter [data-filter-type="MEDICAL_COVER"]').click()
    page.locator('#tm-status-filter [data-filter-status="COMPLETED"]').click()
    page.reload()
    assert active(page, "type", "filterType") == ["TRAINING", "PRESENTATION"]
    assert active(page, "status", "filterStatus") == DEFAULT_STATUSES + ["COMPLETED"]
    assert medical_rows.count() > 0
    assert page.locator('#tm-table tbody tr[data-event-type="MEDICAL_COVER"]:not(.d-none)').count() == 0
    open_table_manager(page, base_url, "Letní festival")
    assert active(page, "type", "filterType") == ["TRAINING", "PRESENTATION"]
    assert errors == []


def test_reset_restores_defaults_after_reload(logged_in_page, base_url, errors):
    page = logged_in_page
    open_table_manager(page, base_url, "Házenkářská liga")
    page.locator('#tm-type-filter [data-filter-type="MEDICAL_COVER"]').click()
    page.locator("#tm-filter-reset").click()
    page.reload()
    assert active(page, "type", "filterType") == ALL_TYPES
    assert active(page, "status", "filterStatus") == DEFAULT_STATUSES
    assert errors == []
