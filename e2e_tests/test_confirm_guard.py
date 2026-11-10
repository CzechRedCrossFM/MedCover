"""E2E tests for the data-confirm guard (guardedConfirm in app-init.js).

Duplicate-dialog suppression: on some input devices (macOS trackpads, assistive
tech) a single physical click dispatches multiple click/submit events; the
guard prompts at most once per element and lets a valid submission through once.

Reset: when a confirmed submission is cancelled (native validation, a submit
listener calling preventDefault) or the page is restored from the bfcache, the
guard returns to its fresh state so the user can retry.
"""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

BACKUP_BTN = "#uploadRestoreForm button[type=submit]"
REGEN_FORM = 'form[action$="/calendar/regenerate"][data-confirm]'
REGEN_BTN = REGEN_FORM + " button[type=submit]"
DUMMY_ZIP = {"name": "e2e-dummy.zip", "mimeType": "application/zip", "buffer": b"PK\x05\x06" + b"\x00" * 18}

BUTTON_FRESH_JS = (
    "sel => { const b = document.querySelector(sel); return b.dataset.confirmed === undefined && !b.disabled; }"
)
FORM_FRESH_JS = """([formSel, btnSel]) => {
      return document.querySelector(formSel).dataset.confirmed === undefined
        && !document.querySelector(btnSel).disabled;
    }"""
BULK_FRESH_JS = """() => {
      const buttons = Array.from(document.querySelectorAll('[data-bulk-action]'));
      return buttons.length > 0 && buttons.every((b) => !b.disabled)
        && document.querySelector('#bulk-form').dataset.confirmed === undefined;
    }"""


def _wait_for_state(page, js, failure_message, arg=None):
    try:
        page.wait_for_function(js, arg=arg, timeout=3000)
    except PlaywrightTimeoutError:
        raise AssertionError(failure_message) from None


def _record_dialogs(page):
    """Handle every dialog; return the list of seen messages and a mutable accept/dismiss switch."""
    seen: list[str] = []
    mode = {"accept": True}

    def handle(dialog):
        seen.append(dialog.message)
        if mode["accept"]:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", handle)
    return seen, mode


def _route_posts(page, pattern):
    """Answer requests matching `pattern` with an empty page and record them."""
    posts: list[str] = []

    def handle(route):
        posts.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="")

    page.route(pattern, handle)
    return posts


def _sink_form(page, selector):
    """Send the form's submission into a hidden iframe so the top document stays in place."""
    page.evaluate(
        """(sel) => {
              const frame = document.createElement('iframe');
              frame.name = 'e2e-sink';
              frame.hidden = true;
              document.body.appendChild(frame);
              document.querySelector(sel).target = 'e2e-sink';
            }""",
        selector,
    )


def _dispatch_pageshow(page, persisted):
    page.evaluate(
        """(p) => {
              const e = new Event('pageshow');
              Object.defineProperty(e, 'persisted', { value: p });
              window.dispatchEvent(e);
            }""",
        persisted,
    )


def _fill_backup_form(page):
    page.locator("#backup_file").set_input_files(DUMMY_ZIP)
    page.locator("#upload_confirmation").fill("RESTORE")


def _open_backup(page, base_url):
    page.goto(f"{base_url}/admin/backup/")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector(BACKUP_BTN, state="attached")


def _open_profile(page, base_url):
    page.goto(f"{base_url}/users/profile")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector(REGEN_FORM, state="attached")


def test_guarded_confirm_form_prompts_only_once_on_rapid_submits(logged_in_page, base_url):
    """Rapid submit events on a data-confirm form must trigger confirm() only once."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")
    page.wait_for_load_state("domcontentloaded")
    # The calendar regenerate form always renders on the profile page.
    page.wait_for_selector('form[action$="/calendar/regenerate"][data-confirm]', state="attached")

    result = page.evaluate("""() => {
          const form = document.querySelector('form[action$="/calendar/regenerate"][data-confirm]');
          let confirmCount = 0;
          window.confirm = () => { confirmCount += 1; return true; };
          // Block the actual navigation so the test does not leave /users/profile.
          form.addEventListener('submit', (e) => e.preventDefault(), true);
          for (let i = 0; i < 3; i++) {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
          }
          return { confirmCount, confirmedFlag: form.dataset.confirmed };
        }""")
    assert result["confirmCount"] == 1, f"confirm() called {result['confirmCount']}× (expected 1)"
    assert result["confirmedFlag"] == "1"


def test_guarded_confirm_form_cancel_burst_prompts_only_once(logged_in_page, base_url):
    """Cancelling the first dialog must still suppress duplicate prompts from the same burst."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector('form[action$="/calendar/regenerate"][data-confirm]', state="attached")

    result = page.evaluate("""() => {
          const form = document.querySelector('form[action$="/calendar/regenerate"][data-confirm]');
          let confirmCount = 0;
          window.confirm = () => { confirmCount += 1; return false; };
          form.addEventListener('submit', (e) => e.preventDefault(), true);
          for (let i = 0; i < 3; i++) {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
          }
          return { confirmCount, confirmedFlag: form.dataset.confirmed };
        }""")
    assert result["confirmCount"] == 1, f"confirm() called {result['confirmCount']}× (expected 1)"
    # Cancel path must not mark the element as permanently confirmed.
    assert result["confirmedFlag"] is None


def test_guarded_confirm_helper_returns_false_on_repeat(logged_in_page, base_url):
    """Direct check of the helper: after acceptance, further calls return false without prompting."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_function('typeof guardedConfirm === "function"', timeout=5000)

    result = page.evaluate("""() => {
          const btn = document.createElement('button');
          document.body.appendChild(btn);
          let confirmCount = 0;
          window.confirm = () => { confirmCount += 1; return true; };
          const first = guardedConfirm(btn, 'x');
          const second = guardedConfirm(btn, 'x');
          const third = guardedConfirm(btn, 'x');
          return { confirmCount, first, second, third };
        }""")
    assert result["first"] is True
    assert result["second"] is False
    assert result["third"] is False
    assert result["confirmCount"] == 1


def test_confirm_button_usable_after_native_validation_blocks_submit(logged_in_page, base_url):
    """A click blocked by native validation must not leave the button dead."""
    page = logged_in_page
    seen, mode = _record_dialogs(page)
    posts = _route_posts(page, "**/admin/backup/upload-restore")
    _open_backup(page, base_url)

    page.locator(BACKUP_BTN).click()
    page.wait_for_timeout(300)
    assert page.url.endswith("/admin/backup/"), f"navigated away from the backup page: {page.url}"
    assert posts == [], "an empty form must not be submitted"
    _wait_for_state(
        page,
        BUTTON_FRESH_JS,
        "button still marked confirmed or disabled after the submit was blocked by native validation",
        arg=BACKUP_BTN,
    )

    _fill_backup_form(page)
    seen.clear()
    mode["accept"] = False
    page.locator(BACKUP_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 1, f"expected one confirm dialog on retry after fixing the form, got {len(seen)}"
    assert posts == [], "a dismissed dialog must not submit the form"
    assert page.url.endswith("/admin/backup/")
    assert page.evaluate(BUTTON_FRESH_JS, BACKUP_BTN), "button not fresh after dismissing the dialog"


def test_confirm_button_resets_when_submit_listener_cancels(logged_in_page, base_url):
    """A submit listener cancelling an accepted button confirm must reset the guard."""
    page = logged_in_page
    seen, _ = _record_dialogs(page)
    posts = _route_posts(page, "**/admin/backup/upload-restore")
    _open_backup(page, base_url)
    _fill_backup_form(page)
    page.evaluate("document.querySelector('#uploadRestoreForm').addEventListener('submit', (e) => e.preventDefault())")

    page.locator(BACKUP_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 1, f"expected one confirm dialog on first click, got {len(seen)}"
    _wait_for_state(
        page,
        BUTTON_FRESH_JS,
        "button still confirmed or disabled after a listener cancelled the submit",
        arg=BACKUP_BTN,
    )

    page.locator(BACKUP_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 2, f"expected a second confirm dialog after the cancelled submit, got {len(seen)}"
    assert posts == [], "the cancelled submit must not reach the server"


def test_confirm_form_resets_when_submit_listener_cancels(logged_in_page, base_url):
    """A later submit listener cancelling an accepted form confirm must re-enable the button."""
    page = logged_in_page
    seen, _ = _record_dialogs(page)
    posts = _route_posts(page, "**/calendar/regenerate")
    _open_profile(page, base_url)
    page.evaluate(f"document.querySelector('{REGEN_FORM}').addEventListener('submit', (e) => e.preventDefault())")

    page.locator(REGEN_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 1, f"expected one confirm dialog on first click, got {len(seen)}"
    _wait_for_state(
        page,
        FORM_FRESH_JS,
        "button still disabled or form still confirmed after the submit was cancelled",
        arg=[REGEN_FORM, REGEN_BTN],
    )

    page.locator(REGEN_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 2, f"expected a second confirm dialog after the cancelled submit, got {len(seen)}"
    assert posts == [], "the cancelled submit must not reach the server"


def test_confirm_form_dismiss_then_retry_prompts_again(logged_in_page, base_url):
    """Dismissing the dialog leaves the form usable and prompts again on the next click."""
    page = logged_in_page
    seen, mode = _record_dialogs(page)
    mode["accept"] = False
    posts = _route_posts(page, "**/calendar/regenerate")
    _open_profile(page, base_url)

    page.locator(REGEN_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 1, f"expected one confirm dialog, got {len(seen)}"
    assert page.evaluate(FORM_FRESH_JS, [REGEN_FORM, REGEN_BTN]), "form not fresh after dismissing the dialog"
    assert posts == [], "a dismissed dialog must not submit the form"

    page.locator(REGEN_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 2, f"expected a second dialog after dismissing the first, got {len(seen)}"
    assert posts == []


def test_confirm_form_double_click_on_valid_form_submits_once(logged_in_page, base_url):
    """A real double click on a valid confirm form prompts once and submits once."""
    page = logged_in_page
    seen, _ = _record_dialogs(page)
    posts = _route_posts(page, "**/calendar/regenerate")
    _open_profile(page, base_url)
    _sink_form(page, REGEN_FORM)

    page.locator(REGEN_BTN).dblclick()
    page.wait_for_timeout(500)
    assert len(seen) == 1, f"double click produced {len(seen)} dialogs (expected 1)"
    assert len(posts) == 1, f"double click produced {len(posts)} submissions (expected 1)"
    state = page.evaluate(
        """([f, b]) => ({
              flag: document.querySelector(f).dataset.confirmed,
              disabled: document.querySelector(b).disabled,
            })""",
        [REGEN_FORM, REGEN_BTN],
    )
    assert state == {"flag": "1", "disabled": True}, f"guard was released during a proceeding submit: {state}"


def test_confirm_button_double_click_on_valid_form_submits_once(logged_in_page, base_url):
    """A real double click on a valid button-level confirm prompts once and submits once."""
    page = logged_in_page
    seen, _ = _record_dialogs(page)
    posts = _route_posts(page, "**/admin/backup/upload-restore")
    _open_backup(page, base_url)
    _sink_form(page, "#uploadRestoreForm")
    _fill_backup_form(page)

    page.locator(BACKUP_BTN).dblclick()
    page.wait_for_timeout(500)
    assert len(seen) == 1, f"double click produced {len(seen)} dialogs (expected 1)"
    assert len(posts) == 1, f"double click produced {len(posts)} submissions (expected 1)"
    flag = page.evaluate("sel => document.querySelector(sel).dataset.confirmed", BACKUP_BTN)
    assert flag == "1", "guard was released during a proceeding submit"


def test_confirm_button_burst_on_valid_form_prompts_once(logged_in_page, base_url):
    """Several clicks dispatched in one task on a valid form produce a single prompt."""
    page = logged_in_page
    seen, mode = _record_dialogs(page)
    mode["accept"] = False
    posts = _route_posts(page, "**/admin/backup/upload-restore")
    _open_backup(page, base_url)
    _fill_backup_form(page)

    page.evaluate("sel => { const b = document.querySelector(sel); b.click(); b.click(); b.click(); }", BACKUP_BTN)
    page.wait_for_timeout(300)
    assert len(seen) == 1, f"burst of 3 clicks produced {len(seen)} dialogs (expected 1)"
    assert posts == [], "a dismissed dialog must not submit the form"


def test_confirm_state_reset_on_bfcache_restore_profile(logged_in_page, base_url):
    """A page restored from the back/forward cache must not keep a stale confirmed form."""
    page = logged_in_page
    seen, mode = _record_dialogs(page)
    posts = _route_posts(page, "**/calendar/regenerate")
    _open_profile(page, base_url)
    _sink_form(page, REGEN_FORM)

    with page.expect_request("**/calendar/regenerate"):
        page.locator(REGEN_BTN).click()
    page.wait_for_timeout(300)
    assert len(posts) == 1
    assert not page.evaluate(FORM_FRESH_JS, [REGEN_FORM, REGEN_BTN]), "submission in flight should leave the guard set"

    _dispatch_pageshow(page, False)
    assert not page.evaluate(
        FORM_FRESH_JS, [REGEN_FORM, REGEN_BTN]
    ), "a normal page load (persisted=false) must not reset the guard"

    _dispatch_pageshow(page, True)
    _wait_for_state(
        page,
        FORM_FRESH_JS,
        "button still disabled or form still confirmed after a persisted pageshow",
        arg=[REGEN_FORM, REGEN_BTN],
    )

    seen.clear()
    mode["accept"] = False
    page.locator(REGEN_BTN).click()
    page.wait_for_timeout(300)
    assert len(seen) == 1, f"expected a dialog after the restore reset, got {len(seen)}"


def test_confirm_state_reset_on_bfcache_restore_bulk_events(logged_in_page, base_url):
    """The events bulk toolbar must be usable again after a back/forward cache restore."""
    page = logged_in_page
    seen, mode = _record_dialogs(page)
    posts = _route_posts(page, "**/events/bulk")
    page.goto(f"{base_url}/events/")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector("#bulk-form", state="attached")
    _sink_form(page, "#bulk-form")

    checkboxes = page.locator(".row-event-check")
    assert checkboxes.count() > 0, "the seeded e2e database should contain events"
    checkboxes.first.check()
    page.locator("#bulk-toolbar").wait_for(state="visible", timeout=5000)

    page.locator('[data-bulk-action="publish"]').click()
    page.wait_for_timeout(300)
    assert len(seen) == 1, f"expected one confirm dialog, got {len(seen)}"
    assert len(posts) == 1, f"expected one bulk submission, got {len(posts)}"
    assert not page.evaluate(BULK_FRESH_JS), "submission in flight should leave the bulk guard set"

    _dispatch_pageshow(page, True)
    _wait_for_state(
        page, BULK_FRESH_JS, "bulk buttons still disabled or form still confirmed after a persisted pageshow"
    )

    mode["accept"] = False
    page.locator('[data-bulk-action="publish"]').click()
    page.wait_for_timeout(300)
    assert len(seen) == 2, f"expected a second dialog after the restore reset, got {len(seen)}"
