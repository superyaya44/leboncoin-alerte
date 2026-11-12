"""Planification par alerte : quand une recherche doit-elle tourner ?

Sans [search.schedule], une recherche tourne à chaque passage du daemon
(general.interval_minutes). Avec :
  every_minutes = 877                → toutes les 14 h 37
  at = ["08:00", "14:37"]            → chaque jour à ces heures (heure de Paris)
  at + weekdays = ["lun", "jeu"]     → ces jours de la semaine
  at + month_days = [1, 15]          → ces jours du mois (31 → ignoré les mois courts)
Le daemon se réveille pile à l'heure du prochain créneau (voir seconds_until_next).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Schedule, Search

try:
    TZ = ZoneInfo("Europe/Paris")
except ZoneInfoNotFoundError:  # image sans tzdata
    TZ = UTC


def _slots_between(sched: Schedule, start: datetime, end: datetime) -> list[datetime]:
    """Créneaux `at` dans ]start, end], filtrés par jours de semaine / du mois."""
    out: list[datetime] = []
    day = start.astimezone(TZ).date()
    last = end.astimezone(TZ).date()
    while day <= last:
        ok_week = not sched.weekdays or day.weekday() in sched.weekdays
        ok_month = not sched.month_days or day.day in sched.month_days
        if ok_week and ok_month:
            for hhmm in sched.at:
                h, m = (int(x) for x in hhmm.split(":"))
                slot = datetime.combine(day, time(h, m), tzinfo=TZ)
                if start < slot <= end:
                    out.append(slot)
        day += timedelta(days=1)
    return sorted(out)


def is_due(search: Search, now: datetime, last_run: datetime | None, tick_minutes: int) -> bool:
    sched = search.schedule
    if sched is None:
        # Sans planning : rythme général. Un réveil anticipé pour une alerte
        # planifiée ne relance pas les autres (pas de requêtes en plus).
        return last_run is None or now - last_run >= timedelta(minutes=max(tick_minutes - 1, 0))
    if sched.every_minutes:
        # Tolérance d'une minute : le réveil peut arriver une poignée de secondes tôt.
        return last_run is None or now - last_run >= timedelta(minutes=sched.every_minutes - 1)
    # Créneaux fixes : un créneau passé depuis le dernier passage. Au premier
    # lancement (pas d'historique), on ne rattrape que le dernier passage du daemon.
    since = last_run or now - timedelta(minutes=tick_minutes)
    return bool(_slots_between(sched, since, now))


def next_run(search: Search, now: datetime, last_run: datetime | None) -> datetime | None:
    """Prochain passage planifié (None = à chaque passage du daemon)."""
    sched = search.schedule
    if sched is None:
        return None
    if sched.every_minutes:
        return now if last_run is None else last_run + timedelta(minutes=sched.every_minutes)
    slots = _slots_between(sched, now, now + timedelta(days=62))
    return slots[0] if slots else None


def seconds_until_next(searches: list[Search], now: datetime,
                       last_runs: dict[str, datetime], default_seconds: int) -> int:
    """Attente avant le prochain réveil du daemon : le passage normal, ou plus tôt
    si une alerte planifiée tombe avant."""
    wait = default_seconds
    for s in searches:
        if s.schedule is None:
            continue
        nxt = next_run(s, now, last_runs.get(s.name))
        if nxt is not None:
            wait = min(wait, max(5, int((nxt - now).total_seconds()) + 1))
    return wait
