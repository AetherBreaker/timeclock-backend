"""Tests for the Employees menu's reads, saves and wording (spec 2026-11-05 §9, §11), over real databases."""

# Standard library imports
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import bcrypt
import pytest
from page_sim import page_settings
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr
from timeclock_schema.enums import Finger

# First party imports
from timeclock_backend.web import text
from timeclock_backend.web.db import editor_transaction
from timeclock_backend.web.employee_editor import hash_pin, normalize_phone
from timeclock_backend.web.gate import Gate
from timeclock_backend.web.store import (
  EmployeeChange,
  employee_row,
  load_context,
  load_employee_history,
  load_fingerprints,
  save_employee,
  set_pin,
)

if TYPE_CHECKING:
  # Standard library imports
  from contextlib import AbstractAsyncContextManager

  # Third party imports
  from page_sim import PagesDb
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow


def _as_badge(db: PagesDb) -> AbstractAsyncContextManager[AsyncConnection[DictRow]]:
  gate, storage, session = Gate(page_settings(), db.accounts, {}), {}, {}
  gate.start(storage, session, db.email)
  return editor_transaction(db.pools, gate, storage, session, test_database=False)


@pytest.mark.parametrize(
  "typed", ["(555) 555-0142", "555-555-0142", "555.555.0142", "5555550142", "1 555 555 0142", "+1 (555) 555-0142"]
)
def test_normalize_phone_takes_the_common_formats(typed: str) -> None:
  assert normalize_phone(typed) == "+15555550142"


@pytest.mark.parametrize("typed", ["555555014", "555-555-01a2", "+44 20 7946 0958", "٥٥٥٥٥٥٠١٤٢"])
def test_normalize_phone_refuses_anything_else(typed: str) -> None:
  with pytest.raises(ValueError, match="ten-digit"):
    normalize_phone(typed)


async def test_a_hashed_pin_is_2a_and_passes_the_check(pages_db: PagesDb) -> None:
  hashed = hash_pin(SecretStr("0123"))
  assert hashed.startswith("$2a$")
  assert bcrypt.checkpw(b"0123", hashed.encode())
  bob = pages_db.employees[1]
  async with _as_badge(pages_db) as conn:
    await set_pin(conn, employee_id=bob, pin_hash=hashed)
  with pages_db.admin() as admin:  # pgcrypto checks $2a$, as the owner would in DBeaver
    assert admin.execute("SELECT crypt('0123', pin_hash) = pin_hash FROM employees WHERE id = %s", (bob,)).fetchone() == (True,)


async def test_saving_writes_the_changed_columns_and_one_log_row_each(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  office = pages_db.group("Office")
  async with _as_badge(pages_db) as conn:
    base = await employee_row(conn, bob)
    assert base is not None
    moved = await save_employee(conn, employee_id=bob, base=base, changes={"phone": "+15555550142", "group_id": office}, note="moved")
  assert moved is None
  with pages_db.admin() as admin:
    assert admin.execute("SELECT phone, group_id FROM employees WHERE id = %s", (bob,)).fetchone() == ("+15555550142", office)
    logged = admin.execute(
      "SELECT c.field, c.old_value, c.new_value, c.db_login, e.note FROM employee_changes c JOIN edits e ON e.id = c.edit_id"
      " WHERE c.employee_id = %s ORDER BY c.field",
      (bob,),
    ).fetchall()
  assert logged == [("group_id", "1", str(office), pages_db.email, "moved"), ("phone", None, "+15555550142", pages_db.email, "moved")]


async def test_a_save_over_a_moved_field_returns_the_row(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  async with _as_badge(pages_db) as conn:
    base = await employee_row(conn, bob)
  assert base is not None
  with pages_db.admin() as admin:
    admin.execute("UPDATE employees SET phone = '+15555550199' WHERE id = %s", (bob,))
  async with _as_badge(pages_db) as conn:
    moved = await save_employee(conn, employee_id=bob, base=base, changes={"phone": "+15555550142"}, note="")
  assert moved is not None
  assert moved["phone"] == "+15555550199"
  with pages_db.admin() as admin:
    assert admin.execute("SELECT phone FROM employees WHERE id = %s", (bob,)).fetchone() == ("+15555550199",)
    assert admin.execute("SELECT count(*) FROM edits").fetchone() == (0,)


async def test_an_outside_change_to_another_field_is_kept(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  async with _as_badge(pages_db) as conn:
    base = await employee_row(conn, bob)
  assert base is not None
  with pages_db.admin() as admin:
    admin.execute("UPDATE employees SET email = 'bob@example.com' WHERE id = %s", (bob,))
  async with _as_badge(pages_db) as conn:
    assert await save_employee(conn, employee_id=bob, base=base, changes={"phone": "+15555550142"}, note="") is None
  with pages_db.admin() as admin:
    assert admin.execute("SELECT phone, email FROM employees WHERE id = %s", (bob,)).fetchone() == ("+15555550142", "bob@example.com")


async def test_saving_your_own_role_is_refused_by_the_database(pages_db: PagesDb) -> None:
  ann = pages_db.employees[0]  # the allowlist row's
  with pytest.raises(errors.InsufficientPrivilege):
    async with _as_badge(pages_db) as conn:
      base = await employee_row(conn, ann)
      assert base is not None
      await save_employee(conn, employee_id=ann, base=base, changes={"role": "manager"}, note="")
  with pages_db.admin() as admin:
    assert admin.execute("SELECT role FROM employees WHERE id = %s", (ann,)).fetchone() == ("employee",)


async def test_a_save_for_an_employee_who_is_gone_raises(pages_db: PagesDb) -> None:
  async with _as_badge(pages_db) as conn:
    with pytest.raises(LookupError):
      await save_employee(conn, employee_id=999_999, base={}, changes={"phone": None}, note="")
    with pytest.raises(LookupError):
      await set_pin(conn, employee_id=999_999, pin_hash=hash_pin(SecretStr("1")))


async def test_history_groups_a_save_and_says_pin_changed(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  async with _as_badge(pages_db) as conn:
    base = await employee_row(conn, bob)
    assert base is not None
    await save_employee(conn, employee_id=bob, base=base, changes={"role": "manager", "first_name": "Rob"}, note="promoted")
  async with _as_badge(pages_db) as conn:
    await set_pin(conn, employee_id=bob, pin_hash=hash_pin(SecretStr("4827")))
  async with _as_badge(pages_db) as conn:
    history = await load_employee_history(conn, bob)
  pin, save = history
  assert (pin.who, pin.note, pin.edit_id, [c.field for c in pin.changes]) == (pages_db.email, "", None, ["pin"])
  assert (save.who, save.note, [c.field for c in save.changes]) == (pages_db.email, "promoted", ["first_name", "role"])
  assert save.edit_id is not None
  assert pin.when > save.when
  groups = {1: "default"}
  assert [text.employee_change_line(c, groups) for c in (*pin.changes, *save.changes)] == [
    "PIN changed",
    "First name: Bob to Rob",
    "Role: Employee to Manager",
  ]


async def test_history_shows_an_owner_edit_as_its_own_item(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  with pages_db.admin() as admin:
    admin.execute("UPDATE employees SET first_name = 'Robert', last_name = 'Leigh' WHERE id = %s", (bob,))
  async with _as_badge(pages_db) as conn:
    [item] = await load_employee_history(conn, bob)
  assert (item.who, item.note, item.edit_id) == (conninfo_to_dict(pages_db.dsn)["user"], "", None)
  assert [(c.field, c.old_value, c.new_value) for c in item.changes] == [
    ("first_name", "Bob", "Robert"),
    ("last_name", "Lee", "Leigh"),
  ]


async def test_fingerprints_and_who_added_an_employee_load(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  at = datetime(2026, 10, 28, 18, 10, tzinfo=UTC)
  print_id = uuid4()
  with pages_db.admin() as admin:
    admin.execute(
      "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id, finger) VALUES (%s, %s, '\\x00', %s, 'front', 'right_index')",
      (print_id, bob, at),
    )
  owner = conninfo_to_dict(pages_db.dsn)["user"]
  async with _as_badge(pages_db) as conn:
    [found] = await load_fingerprints(conn, bob)
    context = await load_context(conn, default_tz=ZoneInfo("UTC"))
  assert (found.id, found.finger, found.registered_at, found.instance_id, found.db_login) == (
    print_id,
    Finger.RIGHT_INDEX,
    at,
    "front",
    owner,
  )
  assert context.people[bob].added_by == owner
  assert f"added by {owner}" in text.employee_details(context.people[bob], context.groups, ZoneInfo("UTC"), h24=False)


@pytest.mark.parametrize(
  ("field", "old", "new", "line"),
  [
    ("group_id", "1", "2", "Group: default to Office"),
    ("group_id", "9", "1", "Group: group 9 to default"),
    ("role", "employee", "admin", "Role: Employee to Admin"),
    ("phone", None, "+15555550142", "Phone: empty to (555) 555-0142"),
    ("email", "a@example.com", None, "Email: a@example.com to empty"),
    ("middle_name", "Q", None, "Middle name: Q to empty"),
    ("fingerprint_exempt", "true", "false", "Fingerprint exemption: on to off"),
    ("active", "true", "false", "Status: Active to Switched off"),
    ("pin_username", "12", "21", "pin_username: 12 to 21"),
  ],
)
def test_change_lines_read_in_plain_words(field: str, old: str | None, new: str | None, line: str) -> None:
  change = EmployeeChange(uuid4(), field, old, new)
  assert text.employee_change_line(change, {1: "default", 2: "Office"}) == line
