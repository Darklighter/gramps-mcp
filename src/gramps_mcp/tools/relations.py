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
Read-only relationship tool (compact JSON).

Delegates kinship computation to the Gramps Web API's native relationship
calculator (GET /relations/{h1}/{h2}), which returns a locale-aware human label
plus the generational distances from each person to their common ancestor. We
layer a best-effort, stable ``relationship_code`` on top; the localized
``relationship`` string from Gramps remains authoritative.
"""

import logging
from typing import Dict, List, Optional

from mcp.types import TextContent
from pydantic import BaseModel, Field

from ..config import get_settings
from ..models.api_calls import ApiCalls
from .compact_common import (
    format_error_response,
    json_response,
    person_ref,
    resolve_person_by_gramps_id,
)
from .search_basic import with_client

logger = logging.getLogger(__name__)


class GetRelationshipParams(BaseModel):
    """Input schema for the get_relationship tool."""

    from_gramps_id: str = Field(..., description="Source person Gramps ID, e.g. I0000.")
    to_gramps_id: str = Field(..., description="Target person Gramps ID, e.g. I0005.")
    language: str = Field("ru", description="Label language: 'ru' or 'en'.")
    include_path: bool = Field(
        True, description="Reserved; the native calculator does not return a path."
    )
    max_depth: int = Field(
        8, ge=1, le=12, description="Max generations to search for a common ancestor."
    )


class _RelationLocaleQuery(BaseModel):
    """Query params for GET /relations (locale/depth; handles go in the URL)."""

    locale: Optional[str] = None
    depth: Optional[int] = None


def _blood_code(
    d_from: Optional[int], d_other: Optional[int], to_gender: Optional[int]
) -> Optional[str]:
    """
    Map (distance_from_source, distance_from_target) to the common ancestor into
    a blood-relation code, resolved by the TARGET person's gender. Returns None
    for affinal/derived relationships (no common ancestor → negative distances).
    """
    if d_from is None or d_other is None or d_from < 0 or d_other < 0:
        return None
    male = to_gender == 1
    female = to_gender == 0

    def pick(m, f, n):
        return m if male else f if female else n

    table = {
        (0, 0): "self",
        (1, 0): pick("father", "mother", "parent"),
        (0, 1): pick("son", "daughter", "child"),
        (2, 0): pick("grandfather", "grandmother", "grandparent"),
        (0, 2): pick("grandson", "granddaughter", "grandchild"),
        (3, 0): pick("great_grandfather", "great_grandmother", "great_grandparent"),
        (0, 3): pick("great_grandson", "great_granddaughter", "great_grandchild"),
        (1, 1): pick("brother", "sister", "sibling"),
        (2, 1): pick("uncle", "aunt", "parent_sibling"),
        (1, 2): pick("nephew", "niece", "sibling_child"),
        (2, 2): "cousin",
    }
    return table.get((d_from, d_other), "distant_relative")


def _refine_code(
    rel_str: Optional[str], base_code: Optional[str], to_gender: Optional[int]
) -> Optional[str]:
    """Fill/override relationship_code for common affinal and maternal/paternal
    cases using the localized label (RU + EN keywords)."""
    if not rel_str:
        return base_code
    s = rel_str.lower()

    if any(k in s for k in ("тесть", "свёкор", "свекор", "father-in-law")):
        return "father_in_law"
    if any(k in s for k in ("тёща", "теща", "свекровь", "mother-in-law")):
        return "mother_in_law"
    if any(k in s for k in ("зять", "son-in-law")):
        return "son_in_law"
    if any(k in s for k in ("невестка", "сноха", "daughter-in-law")):
        return "daughter_in_law"

    if base_code in ("grandfather", "grandmother", "grandparent"):
        if any(k in s for k in ("по маме", "по матери", "maternal")):
            return "maternal_" + base_code
        if any(k in s for k in ("по папе", "по отцу", "paternal")):
            return "paternal_" + base_code

    return base_code


async def compute_relationship(
    client,
    tree_id: str,
    from_handle: str,
    to_handle: str,
    *,
    to_gender: Optional[int] = None,
    language: str = "ru",
    max_depth: int = 8,
) -> Dict:
    """
    Relationship of ``to_handle`` relative to ``from_handle`` as the spec's
    relationship result object (callers attach the ``from``/``to`` blocks).
    ``path`` is always empty — the native most-direct endpoint returns no path.
    """
    if from_handle == to_handle:
        return {
            "relationship": "сам" if language == "ru" else "self",
            "relationship_code": "self",
            "confidence": "exact",
            "distance": 0,
            "path": [],
            "notes": [],
        }

    data = await client.make_api_call(
        api_call=ApiCalls.GET_RELATIONS,
        params=_RelationLocaleQuery(locale=language, depth=max_depth),
        tree_id=tree_id,
        handle1=from_handle,
        handle2=to_handle,
    )
    data = data or {}
    rel_str = data.get("relationship_string") or None
    d_from = data.get("distance_common_origin")
    d_other = data.get("distance_common_other")

    is_blood = (
        isinstance(d_from, int)
        and isinstance(d_other, int)
        and d_from >= 0
        and d_other >= 0
    )
    base_code = _blood_code(d_from, d_other, to_gender)
    code = _refine_code(rel_str, base_code, to_gender)

    distance = (d_from + d_other) if is_blood else None
    if is_blood and rel_str:
        confidence = "exact"
    elif rel_str:
        confidence = "approximate"
    else:
        confidence = "unknown"

    notes = []
    if not is_blood and rel_str:
        notes.append("Affinal/derived relationship; relationship_code is best-effort.")
    if rel_str is None:
        notes.append("No relationship found within the searched depth.")

    return {
        "relationship": rel_str,
        "relationship_code": code,
        "confidence": confidence,
        "distance": distance,
        "path": [],
        "notes": notes,
    }


@with_client
async def get_relationship_tool(client, arguments: Dict) -> List[TextContent]:
    """Return the relationship between two people by Gramps ID (compact JSON)."""
    try:
        from_id = arguments.get("from_gramps_id")
        to_id = arguments.get("to_gramps_id")
        language = arguments.get("language", "ru")
        max_depth = arguments.get("max_depth", 8)

        if not from_id or not to_id:
            raise ValueError("from_gramps_id and to_gramps_id are required")

        settings = get_settings()
        tree_id = settings.gramps_tree_id

        from_person = await resolve_person_by_gramps_id(client, tree_id, from_id)
        to_person = await resolve_person_by_gramps_id(client, tree_id, to_id)
        if not from_person:
            raise ValueError(f"Person not found: {from_id}")
        if not to_person:
            raise ValueError(f"Person not found: {to_id}")

        result = await compute_relationship(
            client,
            tree_id,
            from_person["handle"],
            to_person["handle"],
            to_gender=to_person.get("gender"),
            language=language,
            max_depth=max_depth,
        )

        payload = {
            "from": person_ref(from_person, from_id),
            "to": person_ref(to_person, to_id),
            **result,
        }
        return json_response(payload)

    except Exception as e:
        return format_error_response(e, "relationship lookup")
