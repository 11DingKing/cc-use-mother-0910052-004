"""交易日历：市态判定、时区归一化、版本链不可变、哈希防篡改。"""

import json
from datetime import date, datetime
from datetime import timezone

import pytest

from app.calendar import (
    CalendarStore,
    CalendarValidationError,
    ImmutableVersionError,
    TradingCalendar,
)
from app.calendar.models import CalendarVersion, SpecialSession, build_view

CONFIG_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / "data" / "calendar"


@pytest.fixture
def store(tmp_path):
    return CalendarStore(config_dir=CONFIG_DIR, state_dir=tmp_path / "state")


@pytest.fixture
def cn(store):
    return store.get_calendar("CN")


class TestTradingDays:
    def test_weekend_closed(self, cn):
        # 2024-01-06 是周六
        assert cn.is_trading_day(date(2024, 1, 5)) is True
        assert cn.is_trading_day(date(2024, 1, 6)) is False
        assert cn.is_trading_day(date(2024, 1, 7)) is False

    def test_holiday_closed_with_reason(self, cn):
        assert cn.is_trading_day(date(2024, 2, 9)) is False  # 春节
        reason = cn.reason_for_day(date(2024, 2, 9))
        assert "春节" in reason

    def test_spring_festival_window_next_session(self, cn):
        # 2/9~2/18 整体休市，下一交易日为 2/19
        assert cn.next_trading_day(date(2024, 2, 9)) == date(2024, 2, 19)

    def test_previous_trading_day(self, cn):
        assert cn.previous_trading_day(date(2024, 2, 19)) == date(2024, 2, 8)

    def test_trading_days_in_range(self, cn):
        days = cn.trading_days(date(2024, 2, 5), date(2024, 2, 20))
        # 仅 2/5(一)~2/8(四) 与 2/19(一)~2/20(二)
        assert days == [
            date(2024, 2, 5), date(2024, 2, 6),
            date(2024, 2, 7), date(2024, 2, 8),
            date(2024, 2, 19), date(2024, 2, 20),
        ]

    def test_resolve_explains_shift(self, cn):
        r = cn.resolve(date(2024, 2, 10))
        assert r.shifted is True
        assert r.trading_day == date(2024, 2, 19)
        assert r.reason in ("closed", "weekend")
        assert r.event_name


class TestTimezone:
    def test_naive_datetime_is_local_wall_time(self, cn):
        assert cn.local_date(datetime(2024, 1, 2, 9, 30)) == date(2024, 1, 2)

    def test_aware_datetime_converted_to_market_zone(self, tmp_path):
        us = CalendarStore(config_dir=CONFIG_DIR, state_dir=tmp_path / "state").get_calendar("US")
        # 2024-01-15 是纽交所 MLK 休市日。UTC 1/16 03:00 == 上海 1/16 11:00 == 纽约 1/15 22:00
        moment = datetime(2024, 1, 16, 3, 0, tzinfo=timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
        )
        local = us.local_date(moment)
        assert local == date(2024, 1, 15)
        assert us.is_trading_day(local) is False

    def test_half_day_session_close(self, tmp_path):
        us = CalendarStore(config_dir=CONFIG_DIR, state_dir=tmp_path / "state").get_calendar("US")
        from zoneinfo import ZoneInfo
        # 2024-07-03 半日市 13:00 ET 收市
        assert us.is_trading_day(date(2024, 7, 3)) is True
        before_close = datetime(2024, 7, 3, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        after_close = datetime(2024, 7, 3, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        assert us.is_session_moment(before_close) is True
        assert us.is_session_moment(after_close) is False


class TestVersionChain:
    def test_seed_versions_loaded(self, store):
        versions = store.list_versions("CN")
        assert [v["version"] for v in versions] == ["v1"]
        assert versions[0]["content_hash"]

    def test_publish_appends_new_version(self, store):
        head = store.get_version("CN")
        payload = head.canonical_payload()
        payload["special_sessions"] = payload["special_sessions"] + [{
            "day": "2024-03-01", "name": "临时休市测试",
            "session_type": "closed", "reason": "极端天气",
        }]
        new_v = store.publish("CN", payload, description="补录临时休市")
        assert new_v.version != "v1"
        assert new_v.parent_version == "v1"
        # 旧版本仍在且内容不变
        old = store.get_version("CN", "v1")
        assert old.content_hash() == head.content_hash()
        # 新版本生效
        cal = store.get_calendar("CN")
        assert cal.is_trading_day(date(2024, 3, 1)) is False
        assert "临时休市测试" in cal.reason_for_day(date(2024, 3, 1))

    def test_republish_same_content_is_idempotent(self, store):
        head = store.get_version("CN")
        payload = head.canonical_payload()
        first = store.publish("CN", payload, version="v9", description="x")
        second = store.publish("CN", payload, version="v9", description="x")
        assert first.content_hash() == second.content_hash()
        assert len(store.list_versions("CN")) == 2

    def test_republish_different_content_rejected(self, store):
        head = store.get_version("CN")
        payload = head.canonical_payload()
        store.publish("CN", payload, version="v9", description="x")
        payload["holidays"] = payload["holidays"] + [{"day": "2024-03-02", "name": "篡改"}]
        with pytest.raises(ImmutableVersionError):
            store.publish("CN", payload, version="v9", description="x")

    def test_persistence_roundtrip(self, store):
        head = store.get_version("CN")
        payload = head.canonical_payload()
        payload["special_sessions"] = payload["special_sessions"] + [{
            "day": "2024-03-01", "name": "临时休市持久化",
            "session_type": "closed", "reason": "测试",
        }]
        store.publish("CN", payload, description="持久化补录")
        reloaded = CalendarStore(config_dir=CONFIG_DIR, state_dir=store._state_dir)
        versions = [v["version"] for v in reloaded.list_versions("CN")]
        assert versions == ["v1", "v2"]
        cal = reloaded.get_calendar("CN")
        assert cal.is_trading_day(date(2024, 3, 1)) is False

    def test_makeup_opens_weekend(self, store):
        # 港股式补班：周末补开市
        payload = {
            "tzname": "Asia/Shanghai", "weekend": [5, 6],
            "holidays": [],
            "special_sessions": [{
                "day": "2024-01-07", "name": "周日补班",
                "session_type": "makeup", "reason": "春节调休",
            }],
        }
        store.publish("HK", payload, version="v1")
        cal = store.get_calendar("HK")
        assert cal.is_trading_day(date(2024, 1, 7)) is True
        assert "补班" in cal.reason_for_day(date(2024, 1, 7))

    def test_tampered_state_file_detected(self, tmp_path):
        state_dir = tmp_path / "state"
        s1 = CalendarStore(config_dir=CONFIG_DIR, state_dir=state_dir)
        head = s1.get_version("CN")
        s1.publish("CN", head.canonical_payload(),
                   description="临时", version="v-tamper")
        # 手改已发布文件但保留旧哈希
        path = state_dir / "CN.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["versions"][-1]["description"] = "被人偷偷改了"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(CalendarValidationError) as exc:
            CalendarStore(config_dir=CONFIG_DIR, state_dir=state_dir)
        assert "哈希" in str(exc.value)

    def test_conflicting_events_rejected(self):
        v = CalendarVersion(
            market="XX", version="v1", tzname="UTC", weekend=frozenset({5, 6}),
            holidays=frozenset(),
            special_sessions=frozenset([
                SpecialSession(day=date(2024, 1, 2), name="休", session_type="closed"),
                SpecialSession(day=date(2024, 1, 2), name="开", session_type="makeup"),
            ]),
        )
        with pytest.raises(CalendarValidationError):
            build_view(v)


class TestPermissiveCalendar:
    def test_every_day_is_session(self):
        cal = TradingCalendar.permissive()
        assert cal.is_trading_day(date(2024, 2, 10)) is True
        assert cal.manifest()["version"] == "builtin-permissive-v1"
