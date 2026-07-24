import pytest
from vigil.plugins.base.plugin_helpers import parse_duration, format_duration, format_age


class TestParseDuration:
    def test_integer_passthrough(self):
        assert parse_duration(60) == 60

    def test_float_is_truncated(self):
        assert parse_duration(3600.9) == 3600

    def test_plain_integer_string(self):
        assert parse_duration("120") == 120

    def test_seconds(self):
        assert parse_duration("30s") == 30

    def test_minutes(self):
        assert parse_duration("5m") == 300

    def test_hours(self):
        assert parse_duration("2h") == 7200

    def test_days(self):
        assert parse_duration("3d") == 3 * 86400

    def test_weeks(self):
        assert parse_duration("1w") == 604800

    def test_compound_hours_minutes(self):
        assert parse_duration("2h30m") == 2 * 3600 + 30 * 60

    def test_compound_days_hours(self):
        assert parse_duration("1d12h") == 86400 + 12 * 3600

    def test_compound_all_units(self):
        expected = 604800 + 86400 + 3600 + 60 + 1
        assert parse_duration("1w1d1h1m1s") == expected

    def test_case_insensitive(self):
        assert parse_duration("1H") == 3600
        assert parse_duration("2M") == 120

    def test_leading_trailing_whitespace(self):
        assert parse_duration("  1h  ") == 3600

    def test_invalid_string_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("invalid")

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("")


class TestFormatDuration:
    def test_zero_seconds(self):
        assert format_duration(0) == "0 Seconds"

    def test_negative_treated_as_zero(self):
        assert format_duration(-100) == "0 Seconds"

    def test_one_second(self):
        assert format_duration(1) == "1 Second"

    def test_plural_seconds(self):
        assert format_duration(45) == "45 Seconds"

    def test_one_minute(self):
        assert format_duration(60) == "1 Minute"

    def test_plural_minutes(self):
        assert format_duration(120) == "2 Minutes"

    def test_one_hour(self):
        assert format_duration(3600) == "1 Hour"

    def test_plural_hours(self):
        assert format_duration(7200) == "2 Hours"

    def test_one_day(self):
        assert format_duration(86400) == "1 Day"

    def test_one_week(self):
        assert format_duration(604800) == "1 Week"

    def test_shows_two_most_significant_units(self):
        result = format_duration(3661)
        assert result == "1 Hour 1 Minute"

    def test_complex_value(self):
        result = format_duration(86400 + 7200)
        assert result == "1 Day 2 Hours"


class TestFormatAge:
    def test_never_for_negative(self):
        assert format_age(-1) == "Never"
        assert format_age(-9999) == "Never"

    def test_zero_seconds_ago(self):
        assert format_age(0) == "0 Seconds ago"

    def test_one_hour_ago(self):
        assert format_age(3600) == "1 Hour ago"

    def test_complex_age(self):
        result = format_age(86400 + 7200)
        assert result == "1 Day 2 Hours ago"
