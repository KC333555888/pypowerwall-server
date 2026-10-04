"""History page contracts with the API and the shared chart script.

Browser behavior is checked by hand (see the PR); these pin what the page
relies on from the server and the shared-code rule in DESIGN.md §6.6.
"""

import asyncio
import re

import pytest

from app.core.timeseries import TimeSeriesStore


@pytest.fixture
def daily_store(tmp_path, monkeypatch):
    """A file store with daily rows, served by /api/timeseries/daily."""
    import app.api.timeseries as api

    store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
    asyncio.run(store.get_daily_energy())  # create schema
    conn = store._ensure_conn()
    for i in range(1, 13):
        conn.execute(
            "INSERT INTO daily_energy (gateway_id, day, solar_kwh, updated_at) "
            "VALUES ('gw1', ?, 1.0, 0)",
            (f"2099-01-{i:02d}",),
        )
    conn.commit()
    monkeypatch.setattr(api, "get_timeseries_store", lambda: store)
    yield store
    asyncio.run(store.stop())


class TestPresetDailyQuery:
    def test_start_only_returns_every_later_day(self, client, daily_store):
        # Presets send only `start`: the gateway's current day can be ahead
        # of the browser's, so nothing after start may be cut off, and the
        # default 7-day limit must not apply
        body = client.get("/api/timeseries/daily?start=2099-01-02").json()
        days = [d["day"] for d in body["days"]]
        assert days == [f"2099-01-{i:02d}" for i in range(12, 1, -1)]

    def test_all_range_start(self, client, daily_store):
        # The All preset asks from 1970-01-01
        body = client.get("/api/timeseries/daily?start=1970-01-01").json()
        assert len(body["days"]) == 12


class TestSharedChartCode:
    def test_trend_status_text_lives_in_charts_js(self, client):
        # DESIGN.md §6.6: the Energy Trend's status line is written once
        texts = (
            "Not enough raw samples",
            "needs local time-series storage",
            "Trend data unavailable",
            "resolution \\u00b7",
        )
        charts = client.get("/static/js/charts.js").text
        assert "trendNote," in charts  # exported on window.PWCharts
        for text in texts:
            assert text in charts, text
        for page in ("/console", "/history"):
            html = client.get(page).text
            assert "trendNote(" in html, page
            for text in texts + ("resolution ·",):
                assert text not in html, (page, text)

    def test_one_time_format_in_tooltips(self, client):
        # Locale-dependent dates made the two chart types disagree
        assert "toLocaleDateString" not in client.get("/static/js/charts.js").text

    def test_errors_do_not_show_urls(self, client):
        page = client.get("/history").text
        assert "throw new Error(`HTTP ${resp.status}`)" in page
        assert "returned ${resp.status}" not in page

    def test_range_label_is_not_a_form_label(self, client):
        page = client.get("/history").text
        assert re.search(r'<span[^>]*id="range-label"', page)
        assert 'aria-describedby\', \'energy-table\'' not in page

    def test_default_range_is_24h_and_last_preset_is_remembered(self, client):
        # Plain /history opens at 24h, or the last preset picked in this
        # browser; a range in the URL still wins (bookmarks)
        page = client.get("/history").text
        assert "const DEFAULT_RANGE = '1d';" in page
        assert "storageSet(RANGE_KEY, b.dataset.range)" in page
        assert "else if (PRESETS.has(range)) applyPreset(range);" in page
