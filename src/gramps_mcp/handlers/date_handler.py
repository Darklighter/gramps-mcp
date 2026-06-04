# gramps-mcp - AI-Powered Genealogy Research & Management
# Copyright (C) 2025 cabout.me
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Date data handler for Gramps MCP operations.

Provides clean, consistent date formatting from Gramps date objects.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def parse_gramps_date(date_obj: dict) -> dict:
    """
    Parse a Gramps date object into structured, machine-readable parts.

    Companion to ``format_date`` for the JSON/compact tools: instead of a single
    display string it returns the discrete day/month/year, an ISO string when the
    date is a known full calendar date, and a stable ``quality`` code.

    Args:
        date_obj (dict): Gramps date object with ``dateval`` / ``modifier`` /
            ``quality`` (as returned inside an event's ``date`` field).

    Returns:
        dict: {
            "day": int|None, "month": int|None, "year": int|None,
            "iso": str|None,            # YYYY-MM-DD only for exact full dates
            "display": str,             # human-readable (reuses format_date)
            "quality": str,             # one of the date_quality codes
        }
    """
    display = format_date(date_obj)

    if not date_obj:
        return {
            "day": None,
            "month": None,
            "year": None,
            "iso": None,
            "display": display,
            "quality": "unknown",
        }

    dateval = date_obj.get("dateval") or []
    day = month = year = None
    if len(dateval) >= 3:
        d, m, y = dateval[0], dateval[1], dateval[2]
        day = int(d) if d and d > 0 else None
        month = int(m) if m and m > 0 else None
        year = int(y) if y and y > 0 else None

    modifier = date_obj.get("modifier", 0) or 0
    quality = date_obj.get("quality", 0) or 0

    # Map Gramps modifier/quality to the spec's date_quality codes. Modifier
    # wins over quality where both apply (e.g. an estimated range is "range").
    if modifier == 3:  # about
        quality_code = "about"
    elif modifier == 4:  # range (between)
        quality_code = "range"
    elif modifier in (5, 7, 8):  # span / from / to
        quality_code = "span"
    elif modifier == 6:  # textonly
        quality_code = "textonly"
    elif quality == 1:
        quality_code = "estimated"
    elif quality == 2:
        quality_code = "calculated"
    elif day and month and year:
        quality_code = "exact"
    elif day or month or year:
        quality_code = "partial"
    else:
        quality_code = "unknown"

    # ISO only for an unqualified, complete calendar date.
    iso = None
    if day and month and year and modifier == 0 and quality == 0:
        try:
            iso = datetime(year, month, day).date().isoformat()
        except (ValueError, TypeError):
            iso = None

    return {
        "day": day,
        "month": month,
        "year": year,
        "iso": iso,
        "display": display,
        "quality": quality_code,
    }


def format_date(date_obj: dict) -> str:
    """
    Format Gramps date object into human-readable string with fallback.

    Args:
        date_obj (dict): Gramps date object with dateval array

    Returns:
        str: Formatted date string or "date unknown" if invalid
    """
    if not date_obj:
        return "date unknown"

    # Try formatted string first
    formatted_date = date_obj.get("string", "")
    if formatted_date:
        return formatted_date

    # Try to extract from dateval
    dateval = date_obj.get("dateval")
    if not dateval or len(dateval) < 3:
        return "date unknown"

    # dateval format is [day, month, year, False]
    day, month, year = dateval[0], dateval[1], dateval[2]
    if year <= 0:
        return "date unknown"

    # Get quality and modifier
    quality = date_obj.get("quality", 0)
    modifier = date_obj.get("modifier", 0)

    # Format the base date
    try:
        if day > 0 and month > 0:
            date_dt = datetime(year, month, day)
            base_date = date_dt.strftime("%d %B %Y")
        elif month > 0:
            date_dt = datetime(year, month, 1)
            base_date = date_dt.strftime("%B %Y")
        else:
            base_date = str(year)
    except (ValueError, TypeError):
        base_date = str(year) if year > 0 else "date unknown"

    # Add modifier prefix
    modifier_prefixes = {
        0: "",  # regular
        1: "before ",
        2: "after ",
        3: "about ",
        4: "between ",  # range
        5: "from ",  # span
        6: "",  # textonly
        7: "from ",
        8: "to ",
    }

    # Add quality suffix
    quality_suffixes = {
        0: "",  # regular
        1: " (estimated)",
        2: " (calculated)",
    }

    prefix = modifier_prefixes.get(modifier, "")
    suffix = quality_suffixes.get(quality, "")

    return f"{prefix}{base_date}{suffix}"
