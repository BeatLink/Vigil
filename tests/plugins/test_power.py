import pytest


from vigil.plugins.power import Power, _parse_uevents
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-power", "id": "test-power", "ssh_config": {"host": "test.host"}}

# A laptop on its charger, with a gauge that reports learned full capacity.
LAPTOP = """DEVTYPE=power_supply
POWER_SUPPLY_NAME=ACAD
POWER_SUPPLY_TYPE=Mains
POWER_SUPPLY_ONLINE=1
DEVTYPE=power_supply
POWER_SUPPLY_NAME=BAT1
POWER_SUPPLY_TYPE=Battery
POWER_SUPPLY_STATUS=Full
POWER_SUPPLY_PRESENT=1
POWER_SUPPLY_ENERGY_FULL_DESIGN=45000000
POWER_SUPPLY_ENERGY_FULL=30070000
POWER_SUPPLY_ENERGY_NOW=30070000
POWER_SUPPLY_CAPACITY=100
"""

# A phone with a keyboard-case battery, whose gauges report only design capacity.
PHONE = """DEVTYPE=power_supply
OF_NAME=battery-power
POWER_SUPPLY_NAME=axp20x-battery
POWER_SUPPLY_TYPE=Battery
POWER_SUPPLY_STATUS=Discharging
POWER_SUPPLY_HEALTH=Good
POWER_SUPPLY_PRESENT=1
POWER_SUPPLY_CHARGE_FULL_DESIGN=2750000
POWER_SUPPLY_CAPACITY=43
POWER_SUPPLY_NAME=axp20x-usb
POWER_SUPPLY_TYPE=USB
POWER_SUPPLY_ONLINE=0
POWER_SUPPLY_NAME=ip5xxx-battery
POWER_SUPPLY_TYPE=Battery
POWER_SUPPLY_STATUS=Discharging
POWER_SUPPLY_HEALTH=Good
POWER_SUPPLY_CAPACITY=27
POWER_SUPPLY_SCOPE=Device
"""


def _unplugged(text: str) -> str:
    return text.replace("POWER_SUPPLY_ONLINE=1", "POWER_SUPPLY_ONLINE=0").replace(
        "STATUS=Full", "STATUS=Discharging")


def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == "test-power"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == "test-power") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Power, CFG)


class TestParseUevents:
    def test_one_dict_per_supply_without_the_prefix(self):
        supplies = _parse_uevents(PHONE)
        assert [s["NAME"] for s in supplies] == ["axp20x-battery", "axp20x-usb", "ip5xxx-battery"]
        assert supplies[0]["CAPACITY"] == "43"
        assert "OF_NAME" not in supplies[0]


@pytest.mark.asyncio
class TestCollection:
    async def test_a_laptop_on_its_charger(self, plugin, run_cycle):
        result = run_cycle(plugin, lambda c: CmdResult(0, LAPTOP, ""))
        assert _latest_status() == "online"
        assert _latest_metric("charge_pct") == pytest.approx(100.0)
        assert _latest_metric("plugged_in") == 1.0
        assert _latest_metric("capacity_health_pct") == pytest.approx(66.8, abs=0.1)
        assert result.settings["power:test-power:health"] == "Good"

    async def test_worn_cells_warn_and_then_fail(self, make_plugin, run_cycle):
        p = make_plugin(Power, dict(CFG, capacity_warning=70, capacity_threshold=67))
        result = run_cycle(p, lambda c: CmdResult(0, LAPTOP, ""))
        assert _latest_status() == "failed"
        assert result.settings["power:test-power:health"] == "Replace"

    async def test_the_phone_battery_is_the_headline_not_the_keyboard(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, PHONE, ""))
        assert _latest_metric("charge_pct") == pytest.approx(43.0)
        assert _latest_metric("battery_ip5xxx_battery_charge_pct") == pytest.approx(27.0)
        assert _latest_metric("plugged_in") == 0.0

    async def test_unreported_capacity_is_left_out(self, plugin, run_cycle):
        result = run_cycle(plugin, lambda c: CmdResult(0, PHONE, ""))
        assert _latest_metric("capacity_health_pct") is None
        assert result.settings["power:test-power:health"] == "Good"

    async def test_low_charge_matters_only_on_battery(self, plugin, run_cycle):
        low = LAPTOP.replace("CAPACITY=100", "CAPACITY=8")
        run_cycle(plugin, lambda c: CmdResult(0, low, ""))
        assert _latest_status() == "online"
        run_cycle(plugin, lambda c: CmdResult(0, _unplugged(low), ""))
        assert _latest_status() == "failed"
        run_cycle(plugin, lambda c: CmdResult(0, _unplugged(low.replace("CAPACITY=8", "CAPACITY=15")), ""))
        assert _latest_status() == "warning"

    async def test_a_charging_battery_counts_as_plugged_in(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, PHONE.replace(
            "STATUS=Discharging\nPOWER_SUPPLY_HEALTH=Good\nPOWER_SUPPLY_PRESENT=1",
            "STATUS=Charging\nPOWER_SUPPLY_HEALTH=Good\nPOWER_SUPPLY_PRESENT=1"), ""))
        assert _latest_metric("plugged_in") == 1.0

    async def test_kernel_reported_failure_fails(self, plugin, run_cycle):
        result = run_cycle(plugin, lambda c: CmdResult(0, PHONE.replace("HEALTH=Good", "HEALTH=Overheat", 1), ""))
        assert _latest_status() == "failed"
        assert result.settings["power:test-power:health"] == "Overheat"

    async def test_a_host_with_no_battery_stays_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "POWER_SUPPLY_NAME=ACAD\nPOWER_SUPPLY_TYPE=Mains\n", ""))
        assert _latest_status() == "online"
        assert _latest_metric("charge_pct") is None

    async def test_a_named_battery_that_is_missing_is_unavailable(self, make_plugin, run_cycle):
        p = make_plugin(Power, dict(CFG, battery="BAT0"))
        run_cycle(p, lambda c: CmdResult(0, LAPTOP, ""))
        assert _latest_status() == "unavailable"

    async def test_a_failed_read_is_unavailable(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "permission denied"))
        assert _latest_status() == "unavailable"


@pytest.mark.asyncio
class TestCards:
    async def test_the_charge_card_colors_low_charge_only_on_battery(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, LAPTOP, ""))
        assert plugin._charge_color(8.0) == "online"
        run_cycle(plugin, lambda c: CmdResult(0, _unplugged(LAPTOP), ""))
        assert plugin._charge_color(8.0) == "failed"


@pytest.mark.asyncio
class TestNamedBattery:
    async def test_a_named_battery_gets_its_own_page(self, make_plugin, run_cycle):
        p = make_plugin(Power, dict(CFG, battery="ip5xxx-battery"))
        run_cycle(p, lambda c: CmdResult(0, PHONE, ""))
        assert _latest_metric("charge_pct") == pytest.approx(27.0)
        assert _latest_metric("battery_ip5xxx_battery_charge_pct") == pytest.approx(27.0)
        assert _latest_metric("battery_axp20x_battery_charge_pct") is None

    async def test_an_accessory_battery_ignores_the_inputs_it_feeds(self, make_plugin, run_cycle):
        p = make_plugin(Power, dict(CFG, battery="ip5xxx-battery"))
        fed = PHONE.replace("POWER_SUPPLY_ONLINE=0", "POWER_SUPPLY_ONLINE=1")
        run_cycle(p, lambda c: CmdResult(0, fed, ""))
        assert _latest_metric("plugged_in") == 0.0
        run_cycle(p, lambda c: CmdResult(0, fed.replace(
            "STATUS=Discharging\nPOWER_SUPPLY_HEALTH=Good\nPOWER_SUPPLY_CAPACITY=27",
            "STATUS=Charging\nPOWER_SUPPLY_HEALTH=Good\nPOWER_SUPPLY_CAPACITY=27"), ""))
        assert _latest_metric("plugged_in") == 1.0
