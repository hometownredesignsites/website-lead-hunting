"""Normalize CSV locations to include a full U.S. state name when available."""
from __future__ import annotations


STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
STATE_BY_NAME = {name.casefold(): name for name in STATE_NAMES.values()}


def _canonical_state(value: str) -> str:
    cleaned = (value or "").strip().rstrip(".")
    if len(cleaned) == 2:
        return STATE_NAMES.get(cleaned.upper(), "")
    return STATE_BY_NAME.get(cleaned.casefold(), cleaned)


def format_location(city_value: str, state_value: str = "") -> str:
    """Return ``City, State`` using a full state name when known."""
    raw = (city_value or "").strip().strip(",")
    state = _canonical_state(state_value)
    city = raw
    if "," in raw:
        prefix, suffix = raw.rsplit(",", 1)
        suffix_state = _canonical_state(suffix)
        if suffix_state:
            city = prefix.strip()
            state = state or suffix_state
    if not city:
        return state
    if state:
        return f"{city}, {state}"
    return city
