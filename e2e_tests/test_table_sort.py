"""Browser checks for report sorting, grouped subtotals, and existing table consumers."""

import json

import pytest


def load_table(page, base_url, html):
    response = page.request.get(f"{base_url}/static/js/table-sort.js")
    assert response.ok
    page.set_content(html)
    page.add_script_tag(content=response.text())


def headers(*keys):
    return (
        "<thead><tr>"
        + "".join(
            f'<th class="sortable" data-col="{key}" aria-sort="none">'
            f'<button type="button">{key} <span class="sort-icon">↕</span></button></th>'
            for key in keys
        )
        + "</tr></thead>"
    )


def row(identity, **values):
    return f"<tr id='{identity}' data-sv='{json.dumps(values)}'><td>{identity}</td></tr>"


def row_ids(page, selector):
    return page.locator(selector).evaluate_all("rows => rows.map(row => row.id)")


@pytest.mark.parametrize(
    "key,ascending,descending",
    [
        ("name", ["zero", "early", "missing", "late"], ["late", "missing", "early", "zero"]),
        ("date", ["early", "late", "zero", "missing"], ["zero", "late", "early", "missing"]),
        ("hours", ["zero", "late", "early", "missing"], ["early", "late", "zero", "missing"]),
    ],
)
def test_report_sorting_uses_raw_values_and_keeps_totals(page, base_url, key, ascending, descending):
    html = '<table id="report" data-sort-initial="none">' + headers("name", "date", "hours")
    html += (
        "<tbody>"
        + "".join(
            [
                row("late", name="Žaneta", date=1770000000, hours=2.5),
                row("early", name="Adam", date=1760000000, hours=10),
                row("missing", name="Čeněk", date=None, hours=None),
                row("zero", name="2 Beta", date=1780000000, hours=0),
            ]
        )
        + '</tbody><tfoot><tr id="total"><td>Celkem</td></tr></tfoot></table>'
    )
    load_table(page, base_url, html)
    assert row_ids(page, "tbody tr") == ["late", "early", "missing", "zero"]
    button = page.locator(f'th[data-col="{key}"] button')
    button.click()
    assert row_ids(page, "tbody tr") == ascending
    assert page.locator(f'th[data-col="{key}"]').get_attribute("aria-sort") == "ascending"
    button.focus()
    page.keyboard.press("Enter")
    assert row_ids(page, "tbody tr") == descending
    assert page.locator(f'th[data-col="{key}"]').get_attribute("aria-sort") == "descending"
    assert row_ids(page, "tfoot tr") == ["total"]


def test_work_summary_sorts_groups_and_keeps_each_subtotal(page, base_url):
    html = '<table id="summary" data-sort-initial="none" data-sort-group="user">' + headers("user", "hours")
    for group, name in (("z", "Žaneta"), ("a", "Adam")):
        html += f"<tbody id='{group}' data-sv='{json.dumps({'user': name})}'>"
        html += row(group + "1", hours=10) + row(group + "2", hours=2)
        html += f'<tr id="{group}-total" data-sort-fixed><td>Subtotal</td></tr></tbody>'
    html += '<tfoot><tr id="grand-total"><td>Grand total</td></tr></tfoot></table>'
    load_table(page, base_url, html)
    page.locator('th[data-col="user"] button').click()
    assert row_ids(page, "tbody") == ["a", "z"]
    page.locator('th[data-col="hours"] button').click()
    for group in ("a", "z"):
        assert row_ids(page, f"#{group} tr") == [group + "2", group + "1", group + "-total"]
    page.locator('th[data-col="hours"] button').click()
    for group in ("a", "z"):
        assert row_ids(page, f"#{group} tr") == [group + "1", group + "2", group + "-total"]
    page.locator('th[data-col="user"] button').click()
    page.locator('th[data-col="user"] button').click()
    assert row_ids(page, "tbody") == ["z", "a"]
    assert page.locator("table > :last-child").evaluate("element => element.tagName") == "TFOOT"


def test_existing_tables_keep_initial_sort_and_iso_dates_sort_chronologically(page, base_url):
    html = '<table id="events">' + headers("date") + "<tbody>"
    html += row("december", date="2026-12-01T09:00:00")
    html += row("january", date="2026-01-31T09:00:00")
    html += "</tbody></table>"
    load_table(page, base_url, html)
    assert row_ids(page, "tbody tr") == ["january", "december"]


def test_report_tables_sort_independently(page, base_url):
    html = ""
    for number in (1, 2):
        html += f'<table id="table{number}" data-sort-initial="none">' + headers("hours") + "<tbody>"
        html += row(f"high{number}", hours=10) + row(f"low{number}", hours=2)
        html += "</tbody></table>"
    load_table(page, base_url, html)
    page.locator("#table1 button").click()
    assert row_ids(page, "#table1 tbody tr") == ["low1", "high1"]
    assert row_ids(page, "#table2 tbody tr") == ["high2", "low2"]
