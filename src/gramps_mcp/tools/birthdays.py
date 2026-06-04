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
Read-only birthday tools (compact JSON).

``get_birthdays`` returns people whose birthday falls inside a date range
(handles ranges that cross the year boundary), with age and alive/dead flags.
``get_birthdays_with_relationships`` additionally computes each person's
relationship to a set of anchor people, reusing the relationship calculator.
"""

import logging
from datetime import date
from typing import Dict, List, Optional

from mcp.types import TextContent
from pydantic import BaseModel, Field

from ..config import get_settings
from ..handlers.date_handler import parse_gramps_date
from .compact_common import (
    GENDER_MAP,
    family_refs,
    fetch_people,
    format_error_response,
    json_response,
    person_name,
    resolve_person_by_gramps_id,
)
from .relations import compute_relationship
from .search_basic import with_client

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Input schemas
# ----------------------------------------------------------------------------


class GetBirthdaysParams(BaseModel):
    """Input schema for get_birthdays."""

    start_date: str = Field(..., description="Start of range, inclusive, YYYY-MM-DD.")
    end_date: str = Field(..., description="End of range, inclusive, YYYY-MM-DD.")
    alive_only: bool = Field(True, description="Exclude people with a death event.")
    include_private: bool = Field(
        False, description="Include records marked private in Gramps."
    )
    limit: int = Field(100, ge=1, le=1000, description="Max records to return.")
    offset: int = Field(0, ge=0, description="Pagination offset after sorting.")
    sort: str = Field(
        "date", description="Sort order: 'date', 'name', or 'gramps_id'."
    )


class RelationshipAnchor(BaseModel):
    """An anchor person from whom relationships are computed."""

    label: str = Field(..., description="Display label, e.g. 'Паша'.")
    gramps_id: str = Field(..., description="Anchor person Gramps ID, e.g. I0000.")
    fallback_same_as: Optional[str] = Field(
        None,
        description="Optional label whose result is reused if this anchor is missing.",
    )


class GetBirthdaysWithRelationshipsParams(BaseModel):
    """Input schema for get_birthdays_with_relationships."""

    start_date: str = Field(..., description="Start of range, inclusive, YYYY-MM-DD.")
    end_date: str = Field(..., description="End of range, inclusive, YYYY-MM-DD.")
    alive_only: bool = Field(True, description="Exclude people with a death event.")
    relationship_anchors: List[RelationshipAnchor] = Field(
        default_factory=list, description="People from whom relationships are computed."
    )
    language: str = Field("ru", description="Label language: 'ru' or 'en'.")
    include_paths: bool = Field(
        False, description="Reserved; the native calculator returns no path."
    )
    limit: int = Field(100, ge=1, le=1000, description="Max records to return.")
    offset: int = Field(0, ge=0, description="Pagination offset after sorting.")


# ----------------------------------------------------------------------------
# Core helpers
# ----------------------------------------------------------------------------


def _birthday_in_range(
    day: int, month: int, start: date, end: date
) -> Optional[date]:
    """
    Find the occurrence of (day, month) inside [start, end], inclusive, allowing
    ranges that span the year boundary. Returns the first matching date or None.
    Feb 29 in a non-leap year is treated as Feb 28.
    """
    for year in range(start.year, end.year + 1):
        try:
            occ = date(year, month, day)
        except ValueError:
            if month == 2 and day == 29:
                occ = date(year, 2, 28)
            else:
                continue
        if start <= occ <= end:
            return occ
    return None


def _event_at(person: Dict, ref_index: int) -> Optional[Dict]:
    """Return the extended event object at ref_index (birth/death), or None."""
    events = (person.get("extended") or {}).get("events") or []
    if 0 <= ref_index < len(events):
        return events[ref_index]
    return None


def _build_birthday_record(person: Dict, occ: date, birth_parts: Dict) -> Dict:
    """Assemble one birthday record (without relationships)."""
    death_idx = person.get("death_ref_index", -1)
    is_alive = death_idx < 0

    death_date = None
    death_display = None
    death_event = _event_at(person, death_idx)
    if death_event:
        dd = parse_gramps_date(death_event.get("date") or {})
        death_date = dd["iso"]
        death_display = dd["display"]

    age = occ.year - birth_parts["year"] if birth_parts["year"] else None

    return {
        "gramps_id": person.get("gramps_id"),
        "handle": person.get("handle"),
        "name": person_name(person),
        "gender": GENDER_MAP.get(person.get("gender"), "unknown"),
        "birth_date": birth_parts["iso"],
        "birth_date_display": birth_parts["display"],
        "birth_day": birth_parts["day"],
        "birth_month": birth_parts["month"],
        "birth_year": birth_parts["year"],
        "date_quality": birth_parts["quality"],
        "birthday_date": occ.isoformat(),
        "age": age,
        "is_alive": is_alive,
        "death_date": death_date,
        "death_date_display": death_display,
        "family_refs": family_refs(person),
    }


async def _collect_birthdays(
    client,
    tree_id: str,
    start: date,
    end: date,
    alive_only: bool,
    include_private: bool,
) -> List[Dict]:
    """
    Scan all people once and return birthday records (with the raw person dict
    attached under ``_person`` for downstream relationship lookups). Sorted by
    birthday_date then name.
    """
    people = await fetch_people(client, tree_id, extend="event_ref_list")
    records: List[Dict] = []

    for person in people:
        if person.get("private") and not include_private:
            continue

        is_alive = person.get("death_ref_index", -1) < 0
        if alive_only and not is_alive:
            continue

        birth_event = _event_at(person, person.get("birth_ref_index", -1))
        if not birth_event:
            continue

        birth_parts = parse_gramps_date(birth_event.get("date") or {})
        if not (birth_parts["day"] and birth_parts["month"]):
            continue

        occ = _birthday_in_range(
            birth_parts["day"], birth_parts["month"], start, end
        )
        if occ is None:
            continue

        record = _build_birthday_record(person, occ, birth_parts)
        record["_person"] = person  # internal, stripped before returning to client
        records.append(record)

    records.sort(key=lambda r: (r["birthday_date"], r["name"]))
    return records


def _apply_sort(records: List[Dict], sort: str) -> List[Dict]:
    if sort == "name":
        return sorted(records, key=lambda r: r["name"])
    if sort == "gramps_id":
        return sorted(records, key=lambda r: (r["gramps_id"] or ""))
    # default: date (already sorted by birthday_date, name in _collect)
    return records


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------


@with_client
async def get_birthdays_tool(client, arguments: Dict) -> List[TextContent]:
    """Return people with a birthday inside the given date range (compact JSON)."""
    try:
        params = GetBirthdaysParams(**arguments)
        start = date.fromisoformat(params.start_date)
        end = date.fromisoformat(params.end_date)
        if end < start:
            raise ValueError("end_date must be on or after start_date")

        settings = get_settings()
        tree_id = settings.gramps_tree_id

        records = await _collect_birthdays(
            client, tree_id, start, end, params.alive_only, params.include_private
        )
        total = len(records)

        records = _apply_sort(records, params.sort)
        window = records[params.offset : params.offset + params.limit]
        birthdays = [{k: v for k, v in r.items() if k != "_person"} for r in window]

        payload = {
            "range": {
                "start_date": params.start_date,
                "end_date": params.end_date,
                "inclusive": True,
            },
            "alive_only": params.alive_only,
            "total": total,
            "limit": params.limit,
            "offset": params.offset,
            "birthdays": birthdays,
        }
        return json_response(payload)

    except Exception as e:
        return format_error_response(e, "get_birthdays")


@with_client
async def get_birthdays_with_relationships_tool(
    client, arguments: Dict
) -> List[TextContent]:
    """Birthdays in range plus each person's relationship to the given anchors."""
    try:
        params = GetBirthdaysWithRelationshipsParams(**arguments)
        start = date.fromisoformat(params.start_date)
        end = date.fromisoformat(params.end_date)
        if end < start:
            raise ValueError("end_date must be on or after start_date")

        settings = get_settings()
        tree_id = settings.gramps_tree_id

        # Resolve anchors up front (label -> person dict or None).
        anchors: List[Dict] = []
        for a in params.relationship_anchors:
            person = await resolve_person_by_gramps_id(client, tree_id, a.gramps_id)
            anchors.append(
                {
                    "label": a.label,
                    "gramps_id": a.gramps_id,
                    "fallback_same_as": a.fallback_same_as,
                    "person": person,
                }
            )

        records = await _collect_birthdays(
            client, tree_id, start, end, params.alive_only, include_private=False
        )
        total = len(records)
        window = records[params.offset : params.offset + params.limit]

        birthdays = []
        for record in window:
            target = record["_person"]
            target_handle = target["handle"]
            target_gender = target.get("gender")
            rel_map: Dict[str, Dict] = {}

            for anchor in anchors:
                label = anchor["label"]
                anchor_person = anchor["person"]

                if anchor_person is None:
                    # Anchor not in Gramps: reuse fallback label's result if set.
                    fb = anchor["fallback_same_as"]
                    if fb and fb in rel_map:
                        rel_map[label] = {**rel_map[fb], "confidence": "fallback"}
                    else:
                        rel_map[label] = {
                            "relationship": None,
                            "relationship_code": None,
                            "confidence": "unknown",
                            "distance": None,
                            "path": [],
                            "notes": [f"Anchor {anchor['gramps_id']} not found."],
                        }
                    continue

                result = await compute_relationship(
                    client,
                    tree_id,
                    anchor_person["handle"],
                    target_handle,
                    to_gender=target_gender,
                    from_person=anchor_person,
                    to_person=target,
                    language=params.language,
                )
                if not params.include_paths:
                    result = {k: v for k, v in result.items() if k != "path"}
                rel_map[label] = result

            clean = {k: v for k, v in record.items() if k != "_person"}
            clean["relationships"] = rel_map
            birthdays.append(clean)

        payload = {
            "range": {
                "start_date": params.start_date,
                "end_date": params.end_date,
                "inclusive": True,
            },
            "alive_only": params.alive_only,
            "total": total,
            "birthdays": birthdays,
        }
        return json_response(payload)

    except Exception as e:
        return format_error_response(e, "get_birthdays_with_relationships")
