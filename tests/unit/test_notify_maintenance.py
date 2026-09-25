"""Maintenance windows: when they apply, and what the engine does during and after one."""

from datetime import datetime, timedelta

import pytest

from vigil.core.notifications.maintenance import parse_windows, window

# 2026-09-27 is a Sunday.
SUNDAY = datetime(2026, 9, 27)


def at(day_offset, hour, minute=0):
    return SUNDAY + timedelta(days=day_offset, hours=hour, minutes=minute)


class TestSchedule:
    def test_a_daily_window(self):
        w = window({'name': 'n', 'from': '03:00', 'to': '05:00'})
        assert not w.active(at(0, 2, 59))
        assert w.active(at(0, 3)) and w.active(at(3, 4, 59))
        assert not w.active(at(0, 5))

    def test_weekdays_limit_it(self):
        w = window({'name': 'n', 'days': ['sun', 'Wednesday'], 'from': '03:00', 'to': '05:00'})
        assert w.active(at(0, 4)) and w.active(at(3, 4))
        assert not w.active(at(1, 4))

    def test_past_midnight_belongs_to_the_day_it_started(self):
        w = window({'name': 'n', 'days': 'sat', 'from': '23:00', 'to': '01:00'})
        assert w.active(at(-1, 23, 30))   # Saturday night
        assert w.active(at(0, 0, 30))     # the small hours of Sunday, still Saturday's window
        assert not w.active(at(0, 23, 30))

    def test_a_one_off_window(self):
        w = window({'name': 'n', 'start': '2026-10-01T09:00', 'end': '2026-10-01T17:00'})
        assert w.active(datetime(2026, 10, 1, 9)) and not w.active(datetime(2026, 10, 1, 17))
        assert not w.active(datetime(2026, 10, 2, 10))

    def test_monitors_and_their_groups(self):
        w = window({'name': 'n', 'monitors': ['storage'], 'from': '03:00', 'to': '05:00'})
        assert w.covers(['nas', 'storage']) and not w.covers(['web', 'services'])
        assert window({'name': 'n', 'from': '03:00', 'to': '05:00'}).covers(['anything'])

    def test_describe(self):
        assert window({'name': 'n', 'days': ['sun', 'mon'], 'from': '03:00', 'to': '05:30'}).describe() == 'Mon, Sun 03:00-05:30'
        assert window({'name': 'n', 'from': '03:00', 'to': '05:00'}).describe() == 'Daily 03:00-05:00'
        assert window({'name': 'n', 'start': '2026-10-01T09:00', 'end': '2026-10-01T17:00'}).describe() == '2026-10-01 09:00 to 2026-10-01 17:00'

    @pytest.mark.parametrize('entry, error', [
        ({'from': '03:00'}, 'give `from` and `to`'),
        ({'from': '3am', 'to': '05:00'}, 'time of day'),
        ({'from': '03:00', 'to': '03:00'}, 'must differ'),
        ({'days': ['funday'], 'from': '03:00', 'to': '05:00'}, 'weekday names'),
        ({'start': '2026-10-01T09:00', 'end': '2026-10-01T08:00'}, 'after `start`'),
        ({'start': 'soon', 'end': '2026-10-01T08:00'}, 'date and time'),
    ])
    def test_malformed_windows_are_rejected(self, entry, error):
        with pytest.raises(ValueError, match=error):
            window(entry)

    def test_parse_skips_bad_windows(self, caplog):
        windows = parse_windows([{'name': 'good', 'from': '03:00', 'to': '05:00'},
                                 {'name': 'bad', 'from': '03:00'}, 'not a mapping'])
        assert [w.name for w in windows] == ['good']
        assert "'bad' ignored" in caplog.text
