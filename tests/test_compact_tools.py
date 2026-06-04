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
Offline tests for the compact JSON tools (birthdays, relationships, people).

Covers the tricky pure logic without any Gramps Web connection: registry/schema
wiring, Gramps date parsing, year-rollover birthday matching, and the
relationship-code mapping.
"""

from datetime import date

from src.gramps_mcp.handlers.date_handler import parse_gramps_date
from src.gramps_mcp.server import TOOL_REGISTRY
from src.gramps_mcp.tools.birthdays import _birthday_in_range
from src.gramps_mcp.tools.relations import _blood_code, _refine_code

NEW_TOOLS = [
    "get_birthdays",
    "get_relationship",
    "get_birthdays_with_relationships",
    "get_people_compact",
]


def test_new_tools_registered_with_buildable_schema():
    for name in NEW_TOOLS:
        assert name in TOOL_REGISTRY
        schema = TOOL_REGISTRY[name]["schema"].model_json_schema()
        assert isinstance(schema, dict) and "properties" in schema


def test_parse_exact_date():
    parts = parse_gramps_date(
        {"dateval": [15, 6, 1981, False], "modifier": 0, "quality": 0}
    )
    assert (parts["day"], parts["month"], parts["year"]) == (15, 6, 1981)
    assert parts["iso"] == "1981-06-15"
    assert parts["quality"] == "exact"


def test_parse_about_and_partial_and_empty():
    about = parse_gramps_date({"dateval": [0, 0, 2011, False], "modifier": 3})
    assert about["quality"] == "about" and about["iso"] is None

    partial = parse_gramps_date({"dateval": [27, 12, 0, False], "modifier": 0})
    assert (partial["day"], partial["month"], partial["year"]) == (27, 12, None)
    assert partial["quality"] == "partial"

    empty = parse_gramps_date({})
    assert empty["quality"] == "unknown" and empty["iso"] is None


def test_birthday_in_range_basic():
    assert _birthday_in_range(15, 6, date(2026, 6, 1), date(2026, 6, 30)) == date(
        2026, 6, 15
    )
    assert _birthday_in_range(15, 6, date(2026, 6, 4), date(2026, 6, 10)) is None


def test_birthday_in_range_year_rollover():
    assert _birthday_in_range(30, 12, date(2026, 12, 27), date(2027, 1, 3)) == date(
        2026, 12, 30
    )
    assert _birthday_in_range(1, 1, date(2026, 12, 27), date(2027, 1, 3)) == date(
        2027, 1, 1
    )


def test_birthday_feb29_clamped_in_non_leap_year():
    assert _birthday_in_range(29, 2, date(2027, 2, 25), date(2027, 3, 2)) == date(
        2027, 2, 28
    )


def test_blood_code_mapping():
    assert _blood_code(1, 0, 1) == "father"
    assert _blood_code(1, 0, 0) == "mother"
    assert _blood_code(2, 0, 1) == "grandfather"
    assert _blood_code(1, 1, 2) == "sibling"
    assert _blood_code(-1, -1, 1) is None  # affinal -> no blood code


def test_refine_code_in_law_and_side():
    assert _refine_code("тесть", None, 1) == "father_in_law"
    assert (
        _refine_code("дедушка по маме", "grandfather", 1) == "maternal_grandfather"
    )
