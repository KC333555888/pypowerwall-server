"""Tests for Powerwall device-signal history and the /history page.

Covers:
    - extract_device_metrics (PW3 vitals + fan_speeds, PW2, bad values)
    - record_signal_sample interval gating, series creation, daily rollup
    - Duplicate timestamps never double count the daily rollup
    - Device sample pruning vs. daily rollup retention
    - Device recording disabled (PW_TIMESERIES_SIGNAL_RETENTION=-1)
    - get_signal_trend raw / daily / auto resolution
    - /api/timeseries/daily start/end range
    - /api/timeseries/signals and /api/timeseries/signal_trend endpoints
    - Poll-loop wiring and the /history route
"""

import time

import pytest

from app.core.timeseries import (
    DEVICE_METRICS,
    DEVICE_SIGNALS,
    TimeSeriesStore,
    extract_device_metrics,
)

POD = "TEPOD--1707000-11-J--TG1"
INV = "TEPINV--1707000-11-J--TG1"

PW3_VITALS = {
    POD: {
        "HVP_PackTempMax": 40.2,
        "HVP_PackTempMin": 35.5,
        "HVP_ShuntTemperature": 41.2,
        "BMS_LOG_tempOutOfBounds": 0,
    },
    INV: {"PCH_AmbientTemp": 47.0, "PCH_heatsinkTemp": 45.45, "PINV_Fout": 60.0},
}
PW3_FANS = {
    INV: {
        "PCH_FanSpeed_A": 1395,
        "PCH_FanSpeed_B": 1397,
        "PCH_FanDuty_A": 19.1,
        "PCH_FanDuty_B": 19.1,
    }
}


def store_for(tmp_path, **kwargs):
    return TimeSeriesStore(db_path=str(tmp_path / "ts.db"), **kwargs)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class TestExtract:
    def test_pw3_signals(self):
        m = extract_device_metrics(PW3_VITALS, PW3_FANS)
        assert m == {
            (POD, "pack_temp_max"): 40.2,
            (POD, "pack_temp_min"): 35.5,
            (POD, "shunt_temp"): 41.2,
            (INV, "inverter_ambient"): 47.0,
            (INV, "fan_a_rpm"): 1395.0,
            (INV, "fan_b_rpm"): 1397.0,
            (INV, "fan_a_duty"): 19.1,
            (INV, "fan_b_duty"): 19.1,
        }

    def test_heatsink_not_recorded(self):
        # Constant on current firmware - deliberately excluded
        assert "PCH_heatsinkTemp" not in DEVICE_SIGNALS

    def test_pw2_signals(self):
        m = extract_device_metrics(
            {"TETHC--1": {"THC_AmbientTemp": 25.5}},
            {"PVAC--1": {"PVAC_Fan_Speed_Actual_RPM": 2000}},
        )
        assert m == {("TETHC--1", "ambient_temp"): 25.5, ("PVAC--1", "fan_rpm"): 2000.0}

    def test_skips_missing_and_bad_values(self):
        m = extract_device_metrics(
            {
                POD: {
                    "HVP_PackTempMax": None,
                    "HVP_PackTempMin": "35",
                    "HVP_ShuntTemperature": float("nan"),
                },
                INV: {"PCH_AmbientTemp": True},
                "junk": "not-a-dict",
            },
            None,
        )
        assert m == {}

    def test_empty_inputs(self):
        assert extract_device_metrics(None, None) == {}
        assert extract_device_metrics("x", []) == {}

    def test_every_signal_has_catalog_entry(self):
        for metric in DEVICE_SIGNALS.values():
            assert metric in DEVICE_METRICS


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class TestRecordDevice:
    @pytest.mark.asyncio
    async def test_interval_gating(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        metrics = extract_device_metrics(PW3_VITALS, PW3_FANS)
        t0 = 1_700_000_000.0
        assert await store.record_signal_sample("gw1", t0, metrics) is True
        assert await store.record_signal_sample("gw1", t0 + 5, metrics) is False
        assert await store.record_signal_sample("gw1", t0 + 55, metrics) is False
        # Poll jitter: 58s still counts as the next minute
        assert await store.record_signal_sample("gw1", t0 + 58, metrics) is True
        # Gating is per gateway
        assert await store.record_signal_sample("gw2", t0 + 60, metrics) is True
        status = await store.status()
        assert status["signal_series"] == 16
        assert status["signal_samples"] == 24
        await store.stop()

    @pytest.mark.asyncio
    async def test_daily_rollup_min_max_avg(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        t0 = 1_700_006_400.0  # 00:00 UTC
        for i, value in enumerate([30.0, 40.0, 35.0]):
            await store.record_signal_sample(
                "gw1", t0 + i * 60, {(POD, "pack_temp_max"): value}, timezone="UTC"
            )
        trend = await store.get_signal_trend(
            start=t0 - 3600, end=t0 + 3600, resolution="daily"
        )
        (point,) = trend["series"][0]["points"]
        assert point["min"] == 30.0
        assert point["max"] == 40.0
        assert point["avg"] == pytest.approx(35.0)
        await store.stop()

    @pytest.mark.asyncio
    async def test_duplicate_ts_not_double_counted(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        t0 = 1_700_006_400.0
        m = {(POD, "pack_temp_max"): 30.0}
        await store.record_signal_sample("gw1", t0, m, timezone="UTC")
        store._signal_last.clear()  # simulate restart
        await store.record_signal_sample(
            "gw1", t0, {(POD, "pack_temp_max"): 90.0}, timezone="UTC"
        )
        status = await store.status()
        assert status["signal_samples"] == 1
        trend = await store.get_signal_trend(
            start=t0 - 60, end=t0 + 60, resolution="daily"
        )
        assert trend["series"][0]["points"][0]["max"] == 30.0
        await store.stop()

    @pytest.mark.asyncio
    async def test_rollup_uses_gateway_local_day(self, tmp_path):
        store = store_for(tmp_path)
        # 2023-11-15 03:00 UTC is still Nov 14 in Los Angeles
        ts = 1_700_017_200.0
        await store.record_signal_sample(
            "gw1",
            ts,
            {(POD, "pack_temp_max"): 30.0},
            timezone="America/Los_Angeles",
        )
        info = await store.get_signal_series()
        assert info["series"][0]["first_day"] == "2023-11-14"
        await store.stop()

    @pytest.mark.asyncio
    async def test_disabled(self, tmp_path):
        store = store_for(tmp_path, signal_retention="-1")
        assert store.enabled is True
        assert store.signals_enabled is False
        metrics = extract_device_metrics(PW3_VITALS, PW3_FANS)
        assert await store.record_signal_sample("gw1", time.time(), metrics) is False
        status = await store.status()
        assert status["signals_enabled"] is False
        await store.stop()

    @pytest.mark.asyncio
    async def test_disabled_with_subsystem(self, tmp_path):
        store = store_for(tmp_path, retention="-1")
        assert store.signals_enabled is False
        body = await store.get_signal_trend()
        assert body == {"enabled": False, "series": [], "resolution": None}
        await store.stop()

    @pytest.mark.asyncio
    async def test_pruning_keeps_daily(self, tmp_path):
        store = store_for(tmp_path, signal_retention="2h", signal_interval="60s")
        now = time.time()
        old = now - 3 * 86400
        await store.record_signal_sample("gw1", old, {(POD, "pack_temp_max"): 30.0})
        await store.record_signal_sample("gw1", now, {(POD, "pack_temp_max"): 31.0})
        await store.maintenance()
        status = await store.status()
        assert status["signal_samples"] == 1
        assert status["signal_daily_rows"] == 2
        await store.stop()


# ---------------------------------------------------------------------------
# Trend queries
# ---------------------------------------------------------------------------


class TestDeviceTrend:
    async def _seed(self, store, start, minutes, value=lambda i: 30.0 + i % 10):
        for i in range(minutes):
            await store.record_signal_sample(
                "gw1",
                start + i * 60,
                {(POD, "pack_temp_max"): value(i), (INV, "fan_a_rpm"): 1000.0 + i},
                timezone="UTC",
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "interval,window,expected",
        [
            ("30s", 1800, 30.0),  # short window: one point per sample
            ("30s", 3600, 30.0),  # never finer than the interval
            ("60s", 3600, 60.0),
            ("30s", 6 * 3600, 60.0),  # ~360 points, multiple of the interval
            ("30s", 86400, 240.0),  # a minute or more: whole minutes
            ("60s", 86400, 240.0),
        ],
    )
    async def test_raw_bucket_follows_interval(
        self, tmp_path, interval, window, expected
    ):
        store = store_for(tmp_path, signal_interval=interval)
        now = time.time()
        await self._seed(store, now - 600, 5)
        body = await store.get_signal_trend(
            metrics=["pack_temp_max"], start=now - window, end=now, resolution="raw"
        )
        assert body["bucket_seconds"] == expected

    @pytest.mark.asyncio
    async def test_raw_buckets_and_filter(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 3 * 3600, 180)
        body = await store.get_signal_trend(metrics=["pack_temp_max"], hours=4)
        assert body["resolution"] == "raw"
        assert body["bucket_seconds"] == 60.0
        (series,) = body["series"]
        assert series["metric"] == "pack_temp_max"
        assert series["unit"] == "°C"
        assert series["label"] == "Pack temp (max)"
        assert 170 <= len(series["points"]) <= 181
        p = series["points"][0]
        assert p["min"] <= p["avg"] <= p["max"]
        await store.stop()

    @pytest.mark.asyncio
    async def test_raw_long_window_buckets(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 10 * 3600, 600)
        body = await store.get_signal_trend(
            metrics=["fan_a_rpm"], start=now - 48 * 3600, end=now
        )
        assert body["resolution"] == "raw"
        assert body["bucket_seconds"] == 480.0  # 48h / 360 rounded to minutes
        assert len(body["series"][0]["points"]) <= 80
        await store.stop()

    @pytest.mark.asyncio
    async def test_auto_daily_for_long_windows(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 3600, 30)
        body = await store.get_signal_trend(start=now - 30 * 86400, end=now)
        assert body["resolution"] == "daily"
        assert body["bucket_seconds"] == 86400.0
        assert all("day" in p for s in body["series"] for p in s["points"])
        await store.stop()

    @pytest.mark.asyncio
    async def test_auto_raw_when_no_older_history(self, tmp_path):
        """A fresh install shows its first hour at full detail even when the
        window starts before recording began."""
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 3600, 30)
        body = await store.get_signal_trend(start=now - 7 * 86400, end=now)
        assert body["resolution"] == "raw"
        await store.stop()

    @pytest.mark.asyncio
    async def test_auto_daily_when_older_daily_history(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s", signal_retention="2h")
        now = time.time()
        await self._seed(store, now - 5 * 86400, 5)  # older, pruned below
        await self._seed(store, now - 1800, 20)
        await store.maintenance()
        body = await store.get_signal_trend(start=now - 7 * 86400, end=now)
        assert body["resolution"] == "daily"
        await store.stop()

    @pytest.mark.asyncio
    async def test_device_and_gateway_filters(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 600, 5)
        body = await store.get_signal_trend(devices=[INV], hours=1)
        assert {s["device"] for s in body["series"]} == {INV}
        body = await store.get_signal_trend(gateway="other", hours=1)
        assert body["series"] == []
        await store.stop()

    @pytest.mark.asyncio
    async def test_default_interval_is_one_minute(self, tmp_path):
        store = store_for(tmp_path)
        assert store._signal_interval == 60
        now = time.time()
        metrics = {("TEPOD--1", "pack_temp_max"): 25.0}
        stored = [
            await store.record_signal_sample("default", now + i * 5, metrics)
            for i in range(13)
        ]
        assert stored == [True] + [False] * 11 + [True]

    @pytest.mark.asyncio
    async def test_thirty_second_interval(self, tmp_path):
        store = store_for(tmp_path, signal_interval="30s")
        assert store._signal_interval == 30
        now = time.time()
        metrics = {("TEPOD--1", "pack_temp_max"): 25.0}
        stored = [
            await store.record_signal_sample("default", now + i * 5, metrics)
            for i in range(7)
        ]
        assert stored == [True] + [False] * 5 + [True]

    @pytest.mark.asyncio
    async def test_interval_below_minimum_is_raised(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            store = store_for(tmp_path, signal_interval="5s")
        assert store._signal_interval == 30
        assert "below the 30s minimum" in caplog.text
        now = time.time()
        metrics = {("TEPOD--1", "pack_temp_max"): 25.0}
        stored = [
            await store.record_signal_sample("default", now + i * 5, metrics)
            for i in range(7)
        ]
        assert stored == [True] + [False] * 5 + [True]

    @pytest.mark.asyncio
    async def test_series_listing(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 600, 5)
        info = await store.get_signal_series()
        assert info["signals_enabled"] is True
        assert info["interval_seconds"] == 60
        assert {s["metric"] for s in info["series"]} == {"pack_temp_max", "fan_a_rpm"}
        s = info["series"][0]
        assert s["first_ts"] <= s["last_ts"]
        assert s["first_day"] <= s["last_day"]
        assert "pack_temp_max" in info["metrics"]
        await store.stop()


# ---------------------------------------------------------------------------
# Daily energy date range
# ---------------------------------------------------------------------------


class TestDeviceTimezones:
    """Daily device rollups are keyed by gateway-local day."""

    @staticmethod
    def _ts(day, hour, tz):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        local = datetime.fromisoformat(f"{day}T{hour:02d}:00")
        return local.replace(tzinfo=ZoneInfo(tz)).timestamp()

    @pytest.mark.asyncio
    async def test_daily_window_uses_gateway_local_days(self, tmp_path):
        tz = "Australia/Sydney"  # UTC+10/+11: local midnight is the UTC day before
        store = store_for(tmp_path, signal_interval="60s")
        days = (("2026-03-09", 10.0), ("2026-03-10", 20.0), ("2026-03-11", 30.0))
        for day, value in days:
            await store.record_signal_sample(
                "gw1",
                self._ts(day, 12, tz),
                {(POD, "pack_temp_max"): value},
                timezone=tz,
            )
        body = await store.get_signal_trend(
            metrics=["pack_temp_max"],
            start=self._ts("2026-03-10", 0, tz),
            end=self._ts("2026-03-10", 23, tz),
            resolution="daily",
            timezones={"gw1": tz},
        )
        (series,) = body["series"]
        # Only the local day asked for: the UTC date of local midnight
        # (2026-03-09) must not pull in the previous day
        assert [p["day"] for p in series["points"]] == ["2026-03-10"]
        assert series["points"][0]["ts"] == self._ts("2026-03-10", 12, tz)

    @pytest.mark.asyncio
    async def test_auto_resolution_judges_days_in_local_time(self, tmp_path):
        tz = "America/Los_Angeles"  # evening local = next UTC day
        store = store_for(tmp_path, signal_interval="60s")
        # First samples ever: 20:00-21:00 local on 03-09 (03:00+ UTC on 03-10)
        first = self._ts("2026-03-09", 20, tz)
        for i in range(60):
            await store.record_signal_sample(
                "gw1", first + i * 60, {(POD, "pack_temp_max"): 25.0}, timezone=tz
            )
        # Window starts well before the first sample; there is no older daily
        # history, so auto must stay raw (in UTC the 03-09 rollup looked older)
        body = await store.get_signal_trend(
            metrics=["pack_temp_max"],
            start=first - 6 * 3600,
            end=first + 3600,
            timezones={"gw1": tz},
        )
        assert body["resolution"] == "raw"


class TestDailyRange:
    @pytest.mark.asyncio
    async def test_start_end(self, tmp_path):
        store = store_for(tmp_path)
        await store.get_daily_energy()  # create schema
        conn = store._ensure_conn()
        for day in ("2024-01-01", "2024-06-15", "2025-01-01", "2026-09-01"):
            conn.execute(
                "INSERT INTO daily_energy (gateway_id, day, solar_kwh, updated_at) "
                "VALUES ('gw1', ?, 1.0, 0)",
                (day,),
            )
        conn.commit()
        body = await store.get_daily_energy(start_day="2024-01-01", end_day="2025-01-01")
        assert [d["day"] for d in body["days"]] == [
            "2025-01-01",
            "2024-06-15",
            "2024-01-01",
        ]
        body = await store.get_daily_energy(end_day="2024-12-31")
        assert [d["day"] for d in body["days"]] == ["2024-06-15", "2024-01-01"]
        body = await store.get_daily_energy(start_day="2026-01-01")
        assert [d["day"] for d in body["days"]] == ["2026-09-01"]
        await store.stop()


# ---------------------------------------------------------------------------
# API + route
# ---------------------------------------------------------------------------


class TestHistoryAPI:
    def test_daily_range_params(self, client):
        resp = client.get("/api/timeseries/daily?start=2026-01-01&end=2026-01-31")
        assert resp.status_code == 200
        assert resp.json()["days"] == []
        assert client.get("/api/timeseries/daily?start=2026-1-1").status_code == 422
        assert client.get("/api/timeseries/daily?end=yesterday").status_code == 422

    def test_daily_rejects_impossible_dates(self, client):
        # Well-formed but not a real calendar date
        assert client.get("/api/timeseries/daily?start=2026-02-31").status_code == 422
        assert client.get("/api/timeseries/daily?end=2026-13-01").status_code == 422
        assert client.get("/api/timeseries/daily?start=2024-02-29").status_code == 200

    def test_signals_endpoint(self, client):
        body = client.get("/api/timeseries/signals").json()
        assert body["enabled"] is True
        assert body["signals_enabled"] is True
        assert body["series"] == []
        assert "pack_temp_max" in body["metrics"]

    def test_signal_trend_endpoint(self, client):
        resp = client.get(
            "/api/timeseries/signal_trend?metrics=pack_temp_max,fan_a_rpm&hours=6"
        )
        assert resp.status_code == 200
        assert resp.json()["series"] == []
        assert (
            client.get("/api/timeseries/signal_trend?resolution=weekly").status_code
            == 422
        )

    def test_status_reports_signal_fields(self, client):
        body = client.get("/api/timeseries/status").json()
        for key in (
            "signals_enabled",
            "signal_retention_seconds",
            "signal_interval_seconds",
            "signal_series",
            "signal_samples",
            "signal_daily_rows",
        ):
            assert key in body

    def test_history_page(self, client):
        resp = client.get("/history")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "{PROXY_BASE" not in resp.text
        assert "/api/timeseries/signal_trend" in resp.text

    def test_history_page_proxy_base(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "_proxy_base", "/pypowerwall")
        resp = client.get("/history")
        assert resp.status_code == 200
        assert 'var _BASE = "/pypowerwall"' in resp.text
        assert 'href="/pypowerwall/console"' in resp.text

    def test_console_links_to_history(self, client):
        assert 'href="/history"' in client.get("/console").text


# ---------------------------------------------------------------------------
# Poll-loop wiring
# ---------------------------------------------------------------------------


class TestPollWiring:
    @pytest.mark.asyncio
    async def test_poll_records_device_signals(
        self, tmp_path, monkeypatch, mock_gateway_manager, mock_pypowerwall
    ):
        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, GatewayStatus

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "wired.db"))
        ts_mod.reset_timeseries_store()
        mock_pypowerwall.vitals.return_value = PW3_VITALS
        mock_pypowerwall.tedapi.get_fan_speeds.return_value = PW3_FANS

        gw = Gateway(id="gw1", name="G1", host="1.2.3.4", gw_pwd="x", timezone="UTC")
        mock_gateway_manager.gateways["gw1"] = gw
        mock_gateway_manager.connections["gw1"] = mock_pypowerwall
        mock_gateway_manager.cache["gw1"] = GatewayStatus(gateway=gw, online=False)

        await mock_gateway_manager._poll_gateway("gw1")
        await mock_gateway_manager._poll_gateway("gw1")  # gated: same minute

        store = ts_mod.get_timeseries_store()
        info = await store.get_signal_series(gateway="gw1")
        assert {(s["device"], s["metric"]) for s in info["series"]} == set(
            extract_device_metrics(PW3_VITALS, PW3_FANS)
        )
        assert (await store.status())["signal_samples"] == 8
        ts_mod.reset_timeseries_store()

    @pytest.mark.asyncio
    async def test_poll_skips_when_disabled(
        self, tmp_path, monkeypatch, mock_gateway_manager, mock_pypowerwall
    ):
        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, GatewayStatus

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "off.db"))
        monkeypatch.setattr(settings, "timeseries_signal_retention", "-1")
        ts_mod.reset_timeseries_store()
        mock_pypowerwall.vitals.return_value = PW3_VITALS

        gw = Gateway(id="gw1", name="G1", host="1.2.3.4", gw_pwd="x", timezone="UTC")
        mock_gateway_manager.gateways["gw1"] = gw
        mock_gateway_manager.connections["gw1"] = mock_pypowerwall
        mock_gateway_manager.cache["gw1"] = GatewayStatus(gateway=gw, online=False)

        await mock_gateway_manager._poll_gateway("gw1")
        status = await ts_mod.get_timeseries_store().status()
        assert status["signal_samples"] == 0
        assert status["samples"] == 1  # power samples unaffected
        ts_mod.reset_timeseries_store()
