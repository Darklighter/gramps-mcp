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
Shared helpers for the compact JSON tools (birthdays, relationships, people).

These tools return STRUCTURED JSON (not the markdown the original tools emit) so
the agent can read discrete fields and compute on them. They are strictly
read-only: every call here hits a GET endpoint of the Gramps Web API.
"""

import json
import logging
from typing import Dict, List, Optional

from mcp.types import TextContent
from pydantic import BaseModel

from ..client import GrampsAPIError
from ..models.api_calls import ApiCalls

logger = logging.getLogger(__name__)

# Gramps gender ints -> spec gender strings.
GENDER_MAP = {0: "female", 1: "male", 2: "unknown"}


class PeopleQuery(BaseModel):
    """
    Free-form query params for GET /people.

    Passed as a BaseModel *instance* to ``client.make_api_call`` so it bypasses
    the strict BaseGetMultipleParams validation (which lacks ``keys``) and is
    dumped straight to query parameters. Only set fields are sent.
    """

    gramps_id: Optional[str] = None
    keys: Optional[str] = None
    extend: Optional[str] = None
    profile: Optional[str] = None
    page: Optional[int] = None
    pagesize: Optional[int] = None
    sort: Optional[str] = None
    gql: Optional[str] = None


def json_response(payload) -> List[TextContent]:
    """Serialize a payload to a single JSON TextContent (UTF-8, no ASCII escapes)."""
    return [
        TextContent(
            type="text",
            text=json.dumps(payload, ensure_ascii=False, default=str),
        )
    ]


def format_error_response(error: Exception, operation: str) -> List[TextContent]:
    """Format an error as a JSON error object (kept separate from normal results)."""
    if isinstance(error, GrampsAPIError):
        error_msg = str(error)
    else:
        error_msg = f"Unexpected error during {operation}: {str(error)}"
    logger.error(f"Tool error in {operation}: {error_msg}")
    return json_response({"error": error_msg, "operation": operation})


def person_name(person: Dict) -> str:
    """Build a display name 'Given Surname' from a person's primary_name."""
    primary = person.get("primary_name") or {}
    given = (primary.get("first_name") or "").strip()
    surname_list = primary.get("surname_list") or []
    surname = ""
    if surname_list:
        surname = (surname_list[0].get("surname") or "").strip()
    return f"{given} {surname}".strip()


def family_refs(person: Dict) -> Dict[str, List[str]]:
    """Map a person's family handle lists to the spec's family_refs shape."""
    return {
        "parent_family_handles": person.get("parent_family_list") or [],
        "spouse_family_handles": person.get("family_list") or [],
    }


async def fetch_people(
    client,
    tree_id: str,
    *,
    extend: Optional[str] = None,
    keys: Optional[str] = None,
    profile: Optional[str] = None,
    gramps_id: Optional[str] = None,
) -> List[Dict]:
    """
    Fetch people from GET /people, returning a plain list of person dicts.

    No page/pagesize is sent, so Gramps returns the full set in one call — fine
    for trees of a few hundred people and required for in-range birthday scans.
    """
    query = PeopleQuery(
        extend=extend, keys=keys, profile=profile, gramps_id=gramps_id
    )
    response = await client.make_api_call(
        api_call=ApiCalls.GET_PEOPLE, params=query, tree_id=tree_id
    )
    if isinstance(response, list):
        return response
    return response.get("data", []) or []


async def resolve_person_by_gramps_id(
    client, tree_id: str, gramps_id: str
) -> Optional[Dict]:
    """Look up a single person by gramps_id (e.g. 'I0005'); None if not found."""
    people = await fetch_people(
        client,
        tree_id,
        gramps_id=gramps_id,
        keys="handle,gramps_id,primary_name,gender",
    )
    return people[0] if people else None


def person_ref(person: Optional[Dict], gramps_id_fallback: str = "") -> Dict:
    """Compact {gramps_id, handle, name} reference for a person (or a stub)."""
    if not person:
        return {"gramps_id": gramps_id_fallback or None, "handle": None, "name": ""}
    return {
        "gramps_id": person.get("gramps_id"),
        "handle": person.get("handle"),
        "name": person_name(person),
    }
