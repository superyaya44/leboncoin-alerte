"""Planification par alerte : intervalle propre, heures fixes, jours semaine/mois."""

from datetime import datetime, timedelta

import pytest

from leboncoin_alerte.config import ConfigError, parse_config
from leboncoin_alerte.schedule import TZ, is_due, next_run, seconds_until_next

URL = "https://www.leboncoin.fr/recherche?text=x"


def search(schedule=None):
    raw = {"search": [{"name": "s", "url": URL, **({"schedule": schedule} if schedule else {})}],
           "notify": {"webhook": {"enabled": True, "url": "https://h"}}}
    return parse_config(raw).searches[0]


def paris(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def test_no_schedule_follows_general_interval():
    s = search()
    now = paris(2026, 11, 12, 10, 0)
    assert is_due(s, now, None, 15)
    assert not is_due(s, now, now - timedelta(minutes=5), 15)
    assert is_due(s, now, now - timedelta(minutes=15), 15)


def test_every_minutes_14h37():
    s = search({"every_minutes": 14 * 60 + 37})
    now = paris(2026, 11, 12, 20, 0)
    assert is_due(s, now, None, 15)
    assert not is_due(s, now, now - timedelta(hours=14), 15)
    assert is_due(s, now, now - timedelta(hours=14, minutes=37), 15)
    assert next_run(s, now, now) == now + timedelta(minutes=877)


def test_daily_at_fixed_time():
    s = search({"at": ["14:37"]})
    last = paris(2026, 11, 12, 14, 0)
    assert not is_due(s, paris(2026, 11, 12, 14, 36), last, 15)
    assert is_due(s, paris(2026, 11, 12, 14, 37), last, 15)
    # déjà passé après le créneau → pas de doublon
    assert not is_due(s, paris(2026, 11, 12, 15, 0), paris(2026, 11, 12, 14, 38), 15)
    assert next_run(s, paris(2026, 11, 12, 15, 0), None) == paris(2026, 11, 13, 14, 37)


def test_first_run_does_not_catch_up_old_slots():
    s = search({"at": ["08:00"]})
    assert not is_due(s, paris(2026, 11, 12, 15, 0), None, 15)
    assert is_due(s, paris(2026, 11, 12, 8, 5), None, 15)


def test_weekly_and_monthly():
    weekly = search({"at": ["09:00"], "weekdays": ["lun", "jeudi"]})
    assert weekly.schedule.weekdays == [0, 3]
    # 12/11/2026 = jeudi → prochain : lundi 16/11
    assert next_run(weekly, paris(2026, 11, 12, 10, 0), None) == paris(2026, 11, 16, 9, 0)
    monthly = search({"at": ["07:30"], "month_days": [1, 31]})
    # novembre n'a pas de 31 → 1er décembre
    assert next_run(monthly, paris(2026, 11, 12, 10, 0), None) == paris(2026, 12, 1, 7, 30)


def test_daemon_wakes_up_for_next_slot():
    s = search({"at": ["14:37"]})
    now = paris(2026, 11, 12, 14, 30)
    assert seconds_until_next([s], now, {}, 15 * 60) == 7 * 60 + 1
    assert seconds_until_next([search()], now, {}, 900) == 900


@pytest.mark.parametrize("bad", [
    {"at": ["25:00"]},
    {"at": ["9h"]},
    {"every_minutes": 60, "at": ["09:00"]},
    {"every_minutes": 60, "weekdays": ["lun"]},
    {"at": ["09:00"], "weekdays": ["lun"], "month_days": [1]},
    {"at": ["09:00"], "weekdays": ["funday"]},
    {"at": ["09:00"], "month_days": [32]},
])
def test_invalid_schedules(bad):
    with pytest.raises(ConfigError):
        search(bad)
