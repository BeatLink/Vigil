"""How a card spec's `color` is resolved into a rule."""

import pytest

from vigil.core.ui.spec import COLOR_RULES, _card_color_rule


class TestInlineThresholds:
    def test_a_threshold_pair_builds_the_banded_rule(self):
        rule = _card_color_rule({'color': {'warning': 70, 'threshold': 85}}, 'cpu_card')
        assert rule(50) == 'online'
        assert rule(70) == 'warning'
        assert rule(85) == 'failed'
        assert rule(None) is None

    def test_a_pair_missing_a_bound_names_the_card(self):
        with pytest.raises(KeyError, match='cpu_card'):
            _card_color_rule({'color': {'warning': 70}}, 'cpu_card')


class TestNamedAndCallableRules:
    def test_a_registered_name_resolves_to_its_rule(self):
        rule = _card_color_rule({'color': 'nonzero_failed'}, 'errors_card')
        assert rule is COLOR_RULES['nonzero_failed']

    def test_a_callable_is_used_as_the_rule(self):
        def _mine(v):
            return 'warning'

        assert _card_color_rule({'color': _mine}, 'own_card') is _mine

    def test_an_unknown_name_is_rejected(self):
        with pytest.raises(KeyError, match='register_color_rule'):
            _card_color_rule({'color': 'no_such_rule'}, 'own_card')

    def test_no_color_means_no_rule(self):
        assert _card_color_rule({'metric': 'cpu_pct'}, 'cpu_card') is None


class TestZeroFailed:
    def test_a_one_zero_verdict_maps_onto_online_and_failed(self):
        rule = COLOR_RULES['zero_failed']
        assert rule(1) == 'online'
        assert rule(0) == 'failed'
        assert rule(None) is None
