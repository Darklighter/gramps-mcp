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
Read-only compact people list (JSON).

A universal, field-selectable bulk listing of people for tasks that should NOT
pull the large markdown output of find_type. Returns structured birth/death
parts so the agent can compute on them.
"""

import logging
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
)
from .search_basic import with_client

logger = logging.getLogger(__name__)

DEFAULT_FIELDS = ["gramps_id", "handle", "name", "birth", "death", "family_refs"]
ALLOWED_FIELDS = {
    "gramps_id",
    "handle",
    "name",
    "gender",
    "birth",
    "death",
    "family_refs",
    "event_refs",
    "private",
}


class GetPeopleCompactParams(BaseModel):
    """Input schema for get_people_compact."""

    fields: List[str] = Field(
        default_factory=lambda: list(DEFAULT_FIELDS),
        description="Fields to include per person. Subset of the allowed field set.",
    )
    alive_only: Optional[bool] = Field(
        None, description="true=living only, false=deceased only, null=all."
    )
    include_private: bool = Field(False, description="Include records marked private.")
    limit: int = Field(500, ge=1, le=5000, description="Max records to return.")
    offset: int = Field(0, ge=0, description="Pagination offset after sorting.")


def _event_parts(person: Dict, ref_index_key: str) -> Optional[Dict]:
    """Structured birth/death parts for the event at the given ref index, or None."""
    idx = person.get(ref_index_key, -1)
    events = (person.get("extended") or {}).get("events") or []
    if not (0 <= idx < len(events)):
        return None
    parts = parse_gramps_date(events[idx].get("date") or {})
    return {
        "date": parts["iso"],
        "date_display": parts["display"],
        "day": parts["day"],
        "month": parts["month"],
        "year": parts["year"],
        "quality": parts["quality"],
    }


def _project(person: Dict, fields: List[str]) -> Dict:
    """Project a person dict onto the requested fields."""
    out: Dict = {}
    if "gramps_id" in fields:
        out["gramps_id"] = person.get("gramps_id")
    if "handle" in fields:
        out["handle"] = person.get("handle")
    if "name" in fields:
        out["name"] = person_name(person)
    if "gender" in fields:
        out["gender"] = GENDER_MAP.get(person.get("gender"), "unknown")
    if "birth" in fields:
        out["birth"] = _event_parts(person, "birth_ref_index")
    if "death" in fields:
        out["death"] = _event_parts(person, "death_ref_index")
    if "family_refs" in fields:
        out["family_refs"] = family_refs(person)
    if "event_refs" in fields:
        out["event_refs"] = person.get("event_ref_list") or []
    if "private" in fields:
        out["private"] = bool(person.get("private"))
    return out


@with_client
async def get_people_compact_tool(client, arguments: Dict) -> List[TextContent]:
    """Return a compact, field-selectable list of people (JSON)."""
    try:
        params = GetPeopleCompactParams(**arguments)
        fields = [f for f in params.fields if f in ALLOWED_FIELDS] or list(
            DEFAULT_FIELDS
        )

        settings = get_settings()
        tree_id = settings.gramps_tree_id

        # extend only needed when birth/death parts are requested.
        need_events = "birth" in fields or "death" in fields
        people = await fetch_people(
            client, tree_id, extend="event_ref_list" if need_events else None
        )

        filtered: List[Dict] = []
        for person in people:
            if person.get("private") and not params.include_private:
                continue
            if params.alive_only is not None:
                is_alive = person.get("death_ref_index", -1) < 0
                if params.alive_only and not is_alive:
                    continue
                if not params.alive_only and is_alive:
                    continue
            filtered.append(person)

        total = len(filtered)
        window = filtered[params.offset : params.offset + params.limit]
        projected = [_project(p, fields) for p in window]

        payload = {
            "total": total,
            "limit": params.limit,
            "offset": params.offset,
            "people": projected,
        }
        return json_response(payload)

    except Exception as e:
        return format_error_response(e, "get_people_compact")
