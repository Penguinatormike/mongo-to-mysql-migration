import time

import pytest

from conftest import BEFORE, CUT_OVER, DUAL_WRITE, MYSQL_PRIMARY, READ_MYSQL
from migrator.migration.flags import Flags, RuntimeSettings


@pytest.mark.parametrize(
    "flags,primary,shadow,reads",
    [
        (BEFORE, "mongo", None, "mongo"),
        (DUAL_WRITE, "mongo", "mysql", "mongo"),
        (READ_MYSQL, "mongo", "mysql", "mysql"),
        (MYSQL_PRIMARY, "mysql", "mongo", "mysql"),
        (CUT_OVER, "mysql", None, "mysql"),
        (Flags(read_mysql=True), "mongo", None, "mysql"),  # odd, but allowed
        (Flags(mysql_primary=True), "mysql", "mongo", "mongo"),  # odd, but allowed
    ],
    ids=lambda v: v.describe() if isinstance(v, Flags) else str(v),
)
def test_flag_routing(flags, primary, shadow, reads):
    assert (flags.primary, flags.shadow, flags.reads) == (primary, shadow, reads)


def test_state_store_round_trip(stores):
    _, _, state = stores
    assert state.flags() == BEFORE  # nothing set yet: the defaults
    state.set_flag("write_mysql", True)
    state.set_fault_rate(0.05)
    assert state.flags() == DUAL_WRITE
    assert state.all() == {"write_mysql": "on", "shadow_fault_rate": "0.05"}
    state.set_flag("write_mongo", False)
    assert state.flags().write_mongo is False


def test_unknown_flag_is_rejected(stores):
    _, _, state = stores
    with pytest.raises(ValueError, match="unknown flag"):
        state.set_flag("write_mysqll", True)


def test_runtime_settings_defaults_when_state_is_empty(stores):
    _, _, state = stores
    settings = RuntimeSettings(state, ttl=0)
    assert settings.flags() == BEFORE
    assert settings.fault_rate() == 0.0


def test_runtime_settings_caches_within_ttl_and_picks_up_changes_after(stores):
    _, _, state = stores
    state.set_flag("write_mysql", True)
    settings = RuntimeSettings(state, ttl=0.2)
    assert settings.flags() == DUAL_WRITE

    state.set_flag("read_mysql", True)
    state.set_fault_rate(0.5)
    assert settings.flags() == DUAL_WRITE  # still cached
    assert settings.fault_rate() == 0.0

    time.sleep(0.25)
    assert settings.flags() == READ_MYSQL
    assert settings.fault_rate() == 0.5
