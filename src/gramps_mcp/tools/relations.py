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
    fetch_person,
    format_error_response,
    json_response,
    person_ref,
    resolve_person_by_gramps_id,
    spouse_people,
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


def _result(relationship, code, confidence, distance, notes) -> Dict:
    """Assemble a relationship result object (path is always empty here)."""
    return {
        "relationship": relationship,
        "relationship_code": code,
        "confidence": confidence,
        "distance": distance,
        "path": [],
        "notes": notes or [],
    }


async def _native_relation(client, tree_id, h1, h2, language, depth):
    """Most-direct (blood) relationship of h2 relative to h1 via Gramps Web.

    Returns (relationship_string|None, distance_from_h1, distance_from_h2).
    """
    data = await client.make_api_call(
        api_call=ApiCalls.GET_RELATIONS,
        params=_RelationLocaleQuery(locale=language, depth=depth),
        tree_id=tree_id,
        handle1=h1,
        handle2=h2,
    )
    data = data or {}
    return (
        data.get("relationship_string") or None,
        data.get("distance_common_origin"),
        data.get("distance_common_other"),
    )


def _spouse_word(gender, language):
    if language == "ru":
        return "муж" if gender == 1 else "жена" if gender == 0 else "супруг(а)"
    return "husband" if gender == 1 else "wife" if gender == 0 else "spouse"


def _inlaw_from_spouse_blood(df, do, from_gender, to_gender, rel_str, language):
    """`to` is a blood relative (df, do) of `from`'s spouse → affinal label/code."""
    ru = language == "ru"
    # to is the spouse's PARENT -> parent-in-law (тесть/тёща/свёкор/свекровь)
    if df == 1 and do == 0:
        if ru:
            if from_gender == 1:
                label = (
                    "тесть" if to_gender == 1
                    else "тёща" if to_gender == 0 else "родитель супруги"
                )
            elif from_gender == 0:
                label = (
                    "свёкор" if to_gender == 1
                    else "свекровь" if to_gender == 0 else "родитель супруга"
                )
            else:
                label = "родитель супруга(и)"
        else:
            label = (
                "father-in-law" if to_gender == 1
                else "mother-in-law" if to_gender == 0 else "parent-in-law"
            )
        code = (
            "father_in_law" if to_gender == 1
            else "mother_in_law" if to_gender == 0 else "parent_in_law"
        )
        return label, code
    # to is the spouse's SIBLING -> sibling-in-law (брат/сестра жены/мужа)
    if df == 1 and do == 1:
        if ru:
            sib = (
                "брат" if to_gender == 1
                else "сестра" if to_gender == 0 else "сиблинг"
            )
            poss = (
                "жены" if from_gender == 1
                else "мужа" if from_gender == 0 else "супруга(и)"
            )
            label = f"{sib} {poss}"
        else:
            label = (
                "brother-in-law" if to_gender == 1
                else "sister-in-law" if to_gender == 0 else "sibling-in-law"
            )
        return label, "sibling_in_law"
    # other affinal -> compositional, honest
    if ru:
        poss = (
            "жены" if from_gender == 1
            else "мужа" if from_gender == 0 else "супруга(и)"
        )
        label = f"{rel_str} {poss}"
    else:
        label = f"{rel_str} (by marriage)"
    return label, "relative_by_marriage"


async def _affinal_relationship(
    client, tree_id, from_person, to_person, language, depth
):
    """Resolve an in-law/affinal relationship via spouses when no blood path exists."""
    from_handle = from_person.get("handle")
    to_handle = to_person.get("handle")
    from_gender = from_person.get("gender")
    to_gender = to_person.get("gender")

    from_spouses = await spouse_people(client, tree_id, from_person)

    # Direct spouse.
    for s in from_spouses:
        if s.get("handle") == to_handle:
            return _result(
                _spouse_word(to_gender, language), "spouse", "approximate", 1,
                ["Spouse."],
            )

    # Bridge A: `to` is a blood relative of `from`'s spouse (in-law).
    for s in from_spouses:
        rstr, df, do = await _native_relation(
            client, tree_id, s.get("handle"), to_handle, language, depth
        )
        if rstr:
            label, code = _inlaw_from_spouse_blood(
                df, do, from_gender, to_gender, rstr, language
            )
            dist = (
                df + do + 1
                if (isinstance(df, int) and isinstance(do, int) and df >= 0 and do >= 0)
                else None
            )
            return _result(label, code, "approximate", dist, ["Via spouse (affinal)."])

    # Bridge B: `to` is the spouse of someone `from` is blood-related to
    # (e.g. cousin's husband).
    to_spouses = await spouse_people(client, tree_id, to_person)
    for s in to_spouses:
        rstr, df, do = await _native_relation(
            client, tree_id, from_handle, s.get("handle"), language, depth
        )
        if rstr:
            if df == 0 and do == 1:  # to is from's child's spouse -> зять/невестка
                if language == "ru":
                    label = (
                        "зять" if to_gender == 1
                        else "невестка" if to_gender == 0
                        else f"{_spouse_word(to_gender, language)} {rstr}"
                    )
                else:
                    label = (
                        "son-in-law" if to_gender == 1
                        else "daughter-in-law" if to_gender == 0
                        else f"spouse of {rstr}"
                    )
                code = (
                    "son_in_law" if to_gender == 1
                    else "daughter_in_law" if to_gender == 0 else "child_in_law"
                )
            else:
                label = (
                    f"{_spouse_word(to_gender, language)} {rstr}"
                    if language == "ru" else f"spouse of {rstr}"
                )
                code = "relative_by_marriage"
            return _result(
                label, code, "approximate", None, ["Spouse of a relative (affinal)."]
            )

    return None


async def compute_relationship(
    client,
    tree_id: str,
    from_handle: str,
    to_handle: str,
    *,
    to_gender: Optional[int] = None,
    from_person: Optional[Dict] = None,
    to_person: Optional[Dict] = None,
    language: str = "ru",
    max_depth: int = 8,
) -> Dict:
    """
    Relationship of ``to_handle`` relative to ``from_handle`` as the spec's
    relationship result object (callers attach the ``from``/``to`` blocks).

    Tries the native Gramps calculator (blood/consanguineous) first; on no match,
    falls back to a spouse-bridge that resolves in-law/affinal relationships.
    ``path`` is always empty — the native endpoint returns no path.
    """
    if from_handle == to_handle:
        return _result("сам" if language == "ru" else "self", "self", "exact", 0, [])

    rel_str, d_from, d_other = await _native_relation(
        client, tree_id, from_handle, to_handle, language, max_depth
    )

    if rel_str:
        is_blood = (
            isinstance(d_from, int) and isinstance(d_other, int)
            and d_from >= 0 and d_other >= 0
        )
        base = _blood_code(d_from, d_other, to_gender)
        code = _refine_code(rel_str, base, to_gender)
        distance = (d_from + d_other) if is_blood else None
        confidence = "exact" if is_blood else "approximate"
        return _result(rel_str, code, confidence, distance, [])

    # No blood path → try affinal via spouses (needs full person dicts).
    if from_person is None:
        from_person = await fetch_person(client, tree_id, from_handle)
    if to_person is None:
        to_person = await fetch_person(client, tree_id, to_handle)
    if from_person and to_person:
        affinal = await _affinal_relationship(
            client, tree_id, from_person, to_person, language, max_depth
        )
        if affinal:
            return affinal

    return _result(
        None, None, "unknown", None,
        ["No relationship found within the searched depth."],
    )


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
            from_person=from_person,
            to_person=to_person,
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
