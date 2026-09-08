"""Native mnemonic to canonical name mapping.

Seeded from the reference flight THA935-1 (parameter map CS78761-1-0 v1.00).
Sample rates are as observed in that recording -- note that _IVV occupies
five word slots but carries one distinct value per second, so its true rate
is 1 Hz. Recording that correctly matters: treating it as 5 Hz inflates
Silver fivefold for no information.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CanonicalParameter:
    canonical: str
    native: str
    native_units: str
    canonical_units: str
    slots: int          # word positions allocated in the subframe
    rate_hz: float      # distinct samples per second
    description: str


# Observed in tests/fixtures/THA935-1_decoded.csv. Verify against the FAP
# before trusting rate_hz for any parameter not listed as confirmed.
SEED: tuple[CanonicalParameter, ...] = (
    CanonicalParameter("vertical_acceleration_g", "_VRTG", "G", "g", 10, 10.0,
                       "Confirmed 10 Hz. Load-bearing for hard-landing detection."),
    CanonicalParameter("pitch_angle_deg", "_PITCH", "degree", "deg", 5, 5.0,
                       "Confirmed multi-sample."),
    CanonicalParameter("vertical_speed_fpm", "_IVV", "ft/min", "ft/min", 5, 1.0,
                       "Five slots, one value. Do not store as 5 Hz."),
    CanonicalParameter("altitude_above_field_ft", "_ALTITUDE", "FEET", "ft", 1, 1.0, ""),
    CanonicalParameter("ground_speed_kt", "_GS", "kn", "kt", 1, 1.0, ""),
    CanonicalParameter("heading_deg", "_HEADING", "Deg.", "deg", 1, 1.0, ""),
    CanonicalParameter("n1_eng1_pct", "_N1_1", "percent", "pct", 1, 1.0, ""),
    CanonicalParameter("n1_eng2_pct", "_N1_2", "percent", "pct", 1, 1.0, ""),
)
