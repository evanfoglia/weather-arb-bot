"""
Reading validation - unit safety for weather observations.

The problem this solves:
    A temperature buffer (+1F, +2F, ...) defends against sensor noise.
    It cannot defend against systematic errors like unit mix-ups, which are
    tens of degrees off. Worse, DailyMaxTracker is monotonic: one poisoned
    reading latches the day's high, and every market evaluated after it in
    that city is wrong for the rest of the day.

    So each reading is validated BEFORE it can move the tracker:
      1. Unit-code assertion - when the API tells us the unit, believe it.
      2. Rate-of-change guard - ambient air temperature cannot jump 70F in an hour.
      3. Cross-source quorum  - independent "current" sources must agree.

    Fail direction is always closed: a rejected reading costs a possibly-missed
    trade; an accepted bad reading costs a false "CERTAIN" bet.
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Ambient temperature essentially never moves faster than ~20F/hour even in
# violent frontal passages. 30F/hour is deliberately generous.
MAX_PLAUSIBLE_DELTA_F_PER_HOUR = 30.0

# Two independent "current" readings of the same station must agree within this.
CROSS_SOURCE_TOLERANCE_F = 10.0


@dataclass
class _SourceBaseline:
    temp_f: float
    epoch: float  # seconds since epoch


class ReadingValidator:
    """
    Stateful validator. One instance lives on the WeatherClient and tracks
    the last accepted reading per (city, source) for rate-of-change checks.
    """

    def __init__(self) -> None:
        self._baselines: Dict[str, _SourceBaseline] = {}

    # ------------------------------------------------------------------
    # 1. Unit codes
    # ------------------------------------------------------------------
    @staticmethod
    def check_unit(unit_code: Optional[str], source: str, station_id: str) -> bool:
        """
        NWS labels its temperature unitCode. It is always supposed to be
        unit:degC, and our conversion assumes so. If the API ever says
        otherwise, fail closed instead of converting a value we don't understand.
        """
        if not unit_code:
            return True  # nothing to check; other guards still apply
        if "degC" in unit_code:
            return True
        logger.critical(
            "UNIT VIOLATION: %s/%s reported unitCode=%s (expected degC). "
            "Dropping reading instead of guessing.",
            source.upper(), station_id, unit_code,
        )
        return False

    # ------------------------------------------------------------------
    # 2. Rate of change (per source, against its own history)
    # ------------------------------------------------------------------
    def check_rate(self, key: str, temp_f: float, epoch: float,
                   source: str, station_id: str) -> bool:
        base = self._baselines.get(key)
        if base is None:
            return True  # first reading: nothing to compare against
        dt_hours = (epoch - base.epoch) / 3600.0
        if dt_hours <= 0:
            return True  # duplicate/out-of-order timestamp; quorum check covers it
        max_delta = MAX_PLAUSIBLE_DELTA_F_PER_HOUR * dt_hours
        # Floor so sub-hourly SPECI bursts don't false-trip on normal movement.
        max_delta = max(max_delta, 15.0)
        if abs(temp_f - base.temp_f) > max_delta:
            logger.critical(
                "IMPOSSIBLE JUMP: %s/%s moved %.1fF -> %.1fF in %.1fh "
                "(limit %.0fF). Dropping reading.",
                source.upper(), station_id, base.temp_f, temp_f, dt_hours, max_delta,
            )
            return False
        return True

    def accept_baseline(self, key: str, temp_f: float, epoch: float) -> None:
        self._baselines[key] = _SourceBaseline(temp_f=temp_f, epoch=epoch)

    # ------------------------------------------------------------------
    # 3a. Clean a time series (IEM daily lookback)
    # ------------------------------------------------------------------
    def clean_series(self, observations: List) -> List:
        """
        Walk the series oldest->newest, dropping points that imply a
        physically impossible jump from the last KEPT point. A unit flip
        mid-series (feed switches C<->F) shows up as exactly this signature.
        Dropped points never update the baseline, so a bad stretch can't
        poison later good points once the feed recovers.
        """
        if not observations:
            return []
        ordered = sorted(observations, key=lambda o: o.timestamp)
        kept = [ordered[0]]
        for obs in ordered[1:]:
            prev = kept[-1]
            dt_hours = (obs.timestamp - prev.timestamp).total_seconds() / 3600.0
            if dt_hours <= 0:
                continue
            max_delta = max(MAX_PLAUSIBLE_DELTA_F_PER_HOUR * dt_hours, 15.0)
            if abs(obs.temperature_f - prev.temperature_f) > max_delta:
                logger.critical(
                    "SERIES JUMP: %s/%s %.1fF -> %.1fF over %.1fh. "
                    "Dropping point (possible unit flip).",
                    obs.source.upper(), obs.station_id,
                    prev.temperature_f, obs.temperature_f, dt_hours,
                )
                continue
            kept.append(obs)
        if len(kept) != len(ordered):
            logger.warning(
                "Dropped %d/%d implausible points from %s series",
                len(ordered) - len(kept), len(ordered), ordered[0].source,
            )
        return kept

    # ------------------------------------------------------------------
    # 3b. Quorum for "current" readings (METAR vs NWS, same station)
    # ------------------------------------------------------------------
    def quorum_current(self, city: str, observations: List) -> List:
        """
        Each reading must pass its own rate-of-change check, then the
        survivors must agree with each other. If two independent reads of
        the same station disagree by more than tolerance, we trust neither
        for this cycle: a missed scan beats a false CERTAIN.
        """
        survivors = []
        for obs in observations:
            key = f"{city}:{obs.source}"
            epoch = obs.timestamp.timestamp()
            if not self.check_rate(key, obs.temperature_f, epoch,
                                   obs.source, obs.station_id):
                continue
            survivors.append(obs)

        if len(survivors) >= 2:
            temps = [o.temperature_f for o in survivors]
            if max(temps) - min(temps) > CROSS_SOURCE_TOLERANCE_F:
                desc = ", ".join(f"{o.source}={o.temperature_f:.1f}F" for o in survivors)
                logger.critical(
                    "SOURCE DISAGREEMENT in %s: %s (tolerance %.0fF). "
                    "Trusting neither this cycle.", city, desc, CROSS_SOURCE_TOLERANCE_F,
                )
                return []

        for obs in survivors:
            self.accept_baseline(
                f"{city}:{obs.source}", obs.temperature_f, obs.timestamp.timestamp()
            )
        return survivors
