"""Battery charge, external power and battery wear, read from /sys/class/power_supply."""

from typing import Any, Dict, List, Optional

from vigil.plugins.base.signal_plugin import SignalPlugin, worst_status
from vigil.core.connectors.types import CmdResult, CollectResult, Command, Status
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import level_for

# Kernel health values that mean the battery is failing rather than just outside its comfort zone.
_FAILED_HEALTH = {'Dead', 'Overheat', 'Over voltage', 'Over current',
                  'Unspecified failure', 'No battery'}
_GOOD_HEALTH = {'Good', 'Unknown'}


def _sanitize(name: str) -> str:
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name.lower())


def _parse_uevents(stdout: str) -> List[Dict[str, str]]:
    """Split concatenated power_supply uevent files into one dict per supply, keyed without the POWER_SUPPLY_ prefix."""
    supplies: List[Dict[str, str]] = []
    for line in stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep or not key.startswith('POWER_SUPPLY_'):
            continue
        key = key[len('POWER_SUPPLY_'):]
        if key == 'NAME':
            supplies.append({})
        if supplies:
            supplies[-1][key] = value.strip()
    return supplies


def _number(supply: Dict[str, str], key: str) -> Optional[float]:
    try:
        return float(supply[key])
    except (KeyError, ValueError):
        return None


def _ratio_pct(now: Optional[float], full: Optional[float]) -> Optional[float]:
    return now / full * 100.0 if now is not None and full else None


def _charge_pct(battery: Dict[str, str]) -> Optional[float]:
    """Current charge, from the kernel's own percentage or else from energy or charge counters."""
    capacity = _number(battery, 'CAPACITY')
    if capacity is not None:
        return capacity
    return (_ratio_pct(_number(battery, 'ENERGY_NOW'), _number(battery, 'ENERGY_FULL'))
            or _ratio_pct(_number(battery, 'CHARGE_NOW'), _number(battery, 'CHARGE_FULL')))


def _capacity_health_pct(battery: Dict[str, str]) -> Optional[float]:
    """How much of the factory capacity the battery still holds, if its gauge reports a learned full capacity."""
    return (_ratio_pct(_number(battery, 'ENERGY_FULL'), _number(battery, 'ENERGY_FULL_DESIGN'))
            or _ratio_pct(_number(battery, 'CHARGE_FULL'), _number(battery, 'CHARGE_FULL_DESIGN')))


def _is_battery(supply: Dict[str, str]) -> bool:
    return supply.get('TYPE') == 'Battery' and supply.get('PRESENT', '1') != '0'


def _primary_battery(batteries: List[Dict[str, str]], name: Optional[str]) -> Optional[Dict[str, str]]:
    """The named battery, else the first one powering the system itself rather than an accessory like a keyboard case."""
    if name:
        return next((b for b in batteries if b.get('NAME') == name), None)
    system = [b for b in batteries if b.get('SCOPE') != 'Device']
    return (system or batteries or [None])[0]


def _plugged_in(supplies: List[Dict[str, str]], battery: Dict[str, str]) -> bool:
    """Whether any charger or USB input is live, or the battery says it is being charged."""
    if any(s.get('TYPE') != 'Battery' and s.get('ONLINE') == '1' for s in supplies):
        return True
    return battery.get('STATUS') in ('Charging', 'Full', 'Not charging')


class Power(SignalPlugin):
    """Battery charge, whether the device is plugged in, and how worn the battery is. A host with no
    battery stays online with no metric rather than reporting a problem it cannot see."""

    _QUERY = ("for f in /sys/class/power_supply/*/uevent; do "
              "[ -r \"$f\" ] && cat \"$f\"; done; true")

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.battery: Optional[str] = config.get('battery')
        self.charge_warning     = float(config.get('charge_warning',     20))
        self.charge_threshold   = float(config.get('charge_threshold',   10))
        self.capacity_warning   = float(config.get('capacity_warning',   60))
        self.capacity_threshold = float(config.get('capacity_threshold', 40))

    @property
    def _setting_prefix(self) -> str:
        return f"power:{self.id}"

    def _floor_level(self, value: Optional[float], warning: float, threshold: float) -> str:
        """Level for a reading where lower is worse."""
        if value is None:
            return 'online'
        return level_for(-value, -warning, -threshold)

    def _health(self, kernel_health: Optional[str], capacity_pct: Optional[float]) -> tuple:
        """One overall verdict and its level: the kernel's own complaint if it has one, else how worn the cells are."""
        if kernel_health and kernel_health not in _GOOD_HEALTH:
            return kernel_health, 'failed' if kernel_health in _FAILED_HEALTH else 'warning'
        if capacity_pct is None:
            return kernel_health or 'Unknown', 'online'
        level = self._floor_level(capacity_pct, self.capacity_warning, self.capacity_threshold)
        return {'online': 'Good', 'warning': 'Worn', 'failed': 'Replace'}[level], level

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command(self._QUERY)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.unavailable(f"Failed to read /sys/class/power_supply: {stderr}")

        supplies = _parse_uevents(stdout)
        batteries = [s for s in supplies if _is_battery(s)]
        battery = _primary_battery(batteries, self.battery)
        if battery is None:
            if self.battery:
                return CollectResult.unavailable(f"Battery '{self.battery}' not found")
            return CollectResult(logs=[("No battery found — skipping", "INFO")], status='online')

        charge = _charge_pct(battery)
        capacity = _capacity_health_pct(battery)
        plugged = _plugged_in(supplies, battery)
        health, health_level = self._health(battery.get('HEALTH'), capacity)

        metrics: Dict[str, float] = {'plugged_in': 1.0 if plugged else 0.0}
        if charge is not None:
            metrics['charge_pct'] = charge
        if capacity is not None:
            metrics['capacity_health_pct'] = capacity
        for b in batteries:
            pct = _charge_pct(b)
            if pct is not None:
                metrics[f"battery_{_sanitize(b.get('NAME', ''))}_charge_pct"] = pct

        charge_level = 'online' if plugged else self._floor_level(
            charge, self.charge_warning, self.charge_threshold)
        status = worst_status([charge_level, health_level])

        charge_text = '?' if charge is None else f"{charge:.0f}%"
        capacity_text = 'unreported' if capacity is None else f"{capacity:.0f}% of design"
        state = battery.get('STATUS', 'Unknown')
        return CollectResult(
            metrics=metrics,
            logs=[(f"{battery.get('NAME')}: {charge_text}, {state.lower()}, "
                   f"{'plugged in' if plugged else 'on battery'}; health {health}, capacity {capacity_text}",
                   Status(status).log_level)],
            status=status,
            settings={f"{self._setting_prefix}:health": health,
                      f"{self._setting_prefix}:health_level": health_level,
                      f"{self._setting_prefix}:state": state},
        )

    @property
    def health_text(self) -> str:
        return self.data.get_setting(f"{self._setting_prefix}:health") or 'Checking...'

    @property
    def health_level(self) -> Optional[str]:
        return self.data.get_setting(f"{self._setting_prefix}:health_level")

    @property
    def state_text(self) -> str:
        return self.data.get_setting(f"{self._setting_prefix}:state") or 'Checking...'

    @staticmethod
    def _plugged_text(value: Optional[float]) -> str:
        if value is None:
            return 'Checking...'
        return 'Plugged in' if value > 0.5 else 'On battery'

    def _charge_color(self, value: Optional[float]) -> Optional[str]:
        """Low charge only matters while running on the battery."""
        if value is None:
            return None
        plugged = self.data.latest_metric('plugged_in')
        if plugged is not None and plugged.value > 0.5:
            return 'online'
        return self._floor_level(value, self.charge_warning, self.charge_threshold)

    def _capacity_color(self, value: Optional[float]) -> Optional[str]:
        return None if value is None else self._floor_level(
            value, self.capacity_warning, self.capacity_threshold)

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'charge_card': {'metric': 'charge_pct', 'title': 'CHARGE', 'format': 'percent0',
                            'color': self._charge_color},
            'power_card': {'metric': 'plugged_in', 'title': 'POWER', 'format': self._plugged_text},
            'state_card': {'title': 'STATE', 'value_attr': 'state_text', 'refresh': True},
            'health_card': {'title': 'HEALTH', 'value_attr': 'health_text',
                            'color_attr': 'health_level'},
            'capacity_card': {'metric': 'capacity_health_pct', 'title': 'CAPACITY LEFT',
                              'format': 'percent0', 'color': self._capacity_color},
            'batteries': {
                'repeat': {
                    'source': 'metrics_prefix',
                    'metrics_prefix': 'battery_', 'metrics_suffix': '_charge_pct',
                    'item_format': 'percent0',
                    'label_transform': 'spaces_upper',
                    'container': 'cards',
                    'empty_text': 'No battery found',
                },
            },
        }

    def card_row(self) -> List[str]:
        return ['charge_card', 'power_card', 'state_card', 'health_card', 'capacity_card']

    def rows(self) -> List[List[str]]:
        return [['batteries']]

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'charge_chart': {'metric': 'charge_pct', 'title': 'CHARGE (%)'},
            'capacity_chart': {'metric': 'capacity_health_pct', 'title': 'CAPACITY LEFT (%)'},
        }
