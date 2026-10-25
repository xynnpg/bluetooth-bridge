"""Regression checks for the dashboard's vibration/battery controls.

These are source-level checks: they guard the two mistakes that made the
controls feel broken in the browser.

* the poller wrote the saved config back into a switch while a change was
  still being saved, so the switch appeared to snap back;
* hiding the battery row hid the whole Controller card because the old
  code walked up the DOM with ``parentElement.parentElement``.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def _page() -> str:
    src = (ROOT / "windows" / "src" / "webui.py").read_text(encoding="utf-8")
    start = src.index('_PAGE = r"""') + len('_PAGE = r"""')
    end = src.rindex('"""')
    return src[start:end]


def test_battery_display_toggle_has_its_own_block():
    page = _page()
    # The battery row and bar live inside a dedicated wrapper, so the
    # toggle can hide just those two elements.
    assert 'id="d-battery"' in page
    assert 'id="d-batbar"' in page
    assert "parentElement.parentElement" not in page


def test_quick_toggles_are_not_overwritten_by_the_poller():
    page = _page()
    assert "quickDirty" in page
    assert 'quickDirty.has("quick-rumble")' in page
    assert 'quickDirty.has("quick-battery")' in page
    assert 'id="quick-rumble"' in page
    assert 'id="quick-battery"' in page


def test_test_motors_action_is_wired_end_to_end():
    main = (ROOT / "windows" / "src" / "main.py").read_text(encoding="utf-8")
    assert '"test_rumble"' in main
    assert "_test_rumble" in main
    assert 'id="rumble-test"' in _page()
