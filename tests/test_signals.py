"""Signal and SignalSet."""

import pytest

from benchtare.signals import Direction, Signal, SignalError, SignalSet, SignalType


def make(name="Setpoint", type=SignalType.FLOAT, direction=Direction.INPUT, **kw):
    kw.setdefault("unit", "degC" if type in (SignalType.FLOAT, SignalType.INT) else None)
    return Signal(name=name, type=type, direction=direction, **kw)


class TestSignal:
    def test_numeric_signal_with_unit(self):
        signal = make()
        assert (signal.name, signal.type, signal.direction, signal.unit) == (
            "Setpoint",
            SignalType.FLOAT,
            Direction.INPUT,
            "degC",
        )

    def test_signal_is_frozen(self):
        with pytest.raises(AttributeError):
            make().unit = "V"

    @pytest.mark.parametrize("signal_type", [SignalType.FLOAT, SignalType.INT])
    def test_numeric_signal_may_have_no_unit(self, signal_type):
        # The interface description makes the author say so with 'unit: none'; a Signal just
        # records that there is none.
        assert make(type=signal_type, unit=None).unit is None

    @pytest.mark.parametrize("unit", ["", "  ", 5])
    def test_blank_or_non_text_unit_is_rejected(self, unit):
        with pytest.raises(SignalError, match="unit must be text or None") as info:
            make(unit=unit)
        assert info.value.field == "unit"
        assert info.value.signal == "Setpoint"

    def test_bool_needs_no_unit(self):
        assert make("Enable", SignalType.BOOL).unit is None

    def test_enum_needs_values(self):
        with pytest.raises(SignalError, match="no values"):
            make("Mode", SignalType.ENUM)

    def test_enum_values_must_be_unique(self):
        with pytest.raises(SignalError, match="more than once: run"):
            make("Mode", SignalType.ENUM, values=("standby", "run", "run"))

    def test_only_enums_have_values(self):
        with pytest.raises(SignalError, match="only enum signals have values"):
            make(values=("a",))

    @pytest.mark.parametrize("name", ["2fast", "has space", "class", ""])
    def test_name_must_be_usable_as_attribute(self, name):
        with pytest.raises(SignalError, match="can't be used") as info:
            make(name)
        assert info.value.field == "name"

    @pytest.mark.parametrize("name", ["_private", "items", "get", "keys"])
    def test_reserved_names_are_rejected(self, name):
        with pytest.raises(SignalError, match="reserved"):
            make(name)

    def test_no_range_attribute(self):
        # Pass criteria live in test assertions, not on signals.
        assert not hasattr(make(), "minimum")
        assert not hasattr(make(), "range")


class TestSignalSet:
    def test_attribute_and_item_access(self):
        setpoint = make("Setpoint")
        signals = SignalSet([setpoint, make("Measured", direction=Direction.OUTPUT)])
        assert signals.Setpoint is setpoint
        assert signals["Setpoint"] is setpoint
        assert list(signals) == ["Setpoint", "Measured"]
        assert len(signals) == 2
        assert "Measured" in signals

    def test_duplicate_name_is_rejected_naming_both_sources(self):
        with pytest.raises(SignalError, match="defined twice: in a.yaml and in b.dbc"):
            SignalSet([make(source="a.yaml"), make(source="b.dbc")])

    def test_unknown_attribute_suggests_close_names(self):
        signals = SignalSet([make("Setpoint")])
        with pytest.raises(AttributeError, match="no signal named 'Setpiont'.*'Setpoint'"):
            _ = signals.Setpiont

    def test_unknown_item_raises_key_error(self):
        with pytest.raises(KeyError, match="no signal named 'Nope'"):
            SignalSet([make()])["Nope"]

    def test_dir_lists_signal_names(self):
        assert "Setpoint" in dir(SignalSet([make()]))
