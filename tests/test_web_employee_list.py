"""Tests for the Employees tab's list (spec 2026-11-05 §9, mockup screen J), over real databases."""

# Standard library imports
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

# Third party imports
from nicegui import ui
from page_sim import EMPLOYEE_SQL

if TYPE_CHECKING:
  # Third party imports
  import pytest
  from page_sim import DbPage


def _rows(page: DbPage) -> list[int]:
  """The listed employees' ids, top to bottom."""
  rows = sorted(page.user.find(marker="employee-row").elements, key=lambda e: e.id)
  markers = [row._markers for row in rows]  # pyright: ignore[reportPrivateUsage]
  return [int(next(m for m in found if m.startswith("employee-row-")).removeprefix("employee-row-")) for found in markers]


def _print(page: DbPage, employee: int) -> None:
  with page.db.admin() as conn:
    conn.execute(
      "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id, finger)"
      " VALUES (%s, %s, '\\x00', %s, 'front', 'right_index')",
      (uuid4(), employee, datetime(2026, 10, 28, 18, tzinfo=UTC)),
    )


async def test_the_employees_tab_lists_everyone_by_last_name(db_page: DbPage) -> None:
  ann, bob = db_page.db.employees
  cy = db_page.db.employee("Cy", "Adams", "56")
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET phone = '+15555550142', email = 'bob@example.com' WHERE id = %s", (bob,))
  await db_page.user.open("/")
  db_page.user.find(marker="nav-employees").click()
  await db_page.user.should_see(marker="employee-search")
  assert _rows(db_page) == [cy, ann, bob]
  await db_page.user.should_see("(555) 555-0142")
  await db_page.user.should_see("bob@example.com")
  await db_page.user.should_see(marker=f"own-{ann}")  # your own row has a lock


async def test_search_matches_a_name_or_an_id(db_page: DbPage) -> None:
  ann, bob = db_page.db.employees
  cy = db_page.db.employee("Cy", "Adams", "56")
  await db_page.user.open("/employees")
  db_page.user.find(marker="employee-search").type("BOB")
  await db_page.user.should_not_see("Ann Lee")
  assert _rows(db_page) == [bob]
  db_page.user.find(marker="employee-search").clear().type("5")
  await db_page.user.should_see("Cy Adams")
  assert _rows(db_page) == [cy]
  db_page.user.find(marker="employee-search").clear().type("ann lee")
  await db_page.user.should_see("Ann Lee")
  assert _rows(db_page) == [ann]


async def test_switched_off_employees_show_only_when_asked(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET active = false WHERE id = %s", (bob,))
  await db_page.user.open("/employees")
  await db_page.user.should_see("Ann Lee")
  await db_page.user.should_not_see("Bob Lee")
  db_page.user.find(marker="show-switched-off").click()
  await db_page.user.should_see("Bob Lee")
  await db_page.user.should_see("Switched off")


async def test_the_bar_counts_who_cant_clock_in_with_a_finger(db_page: DbPage) -> None:
  ann, bob = db_page.db.employees
  _print(db_page, bob)
  exempt = db_page.db.employee("Cy", "Adams", "56")
  off = db_page.db.employee("Dee", "Ng", "78")
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET fingerprint_exempt = true WHERE id = %s", (exempt,))
    conn.execute("UPDATE employees SET active = false WHERE id = %s", (off,))
  await db_page.user.open("/employees?off=1")
  await db_page.user.should_see("1 employee can't clock in with a fingerprint: no fingerprint registered, and not exempt.")
  assert len(db_page.user.find(marker="no-fingerprint").elements) == 1
  await db_page.user.should_see(marker=f"no-fingerprint-{ann}")
  await db_page.user.should_see("1 registered")
  await db_page.user.should_see("Exempt (PIN only)")


async def test_a_row_opens_the_employee(db_page: DbPage, monkeypatch: pytest.MonkeyPatch) -> None:
  bob = db_page.db.employees[1]
  opened: list[str] = []
  monkeypatch.setattr(ui.navigate, "to", opened.append)
  await db_page.user.open("/employees")
  db_page.user.find(marker=f"employee-row-{bob}").click()
  assert opened == [f"/employee?id={bob}&back="]  # the list as it was left: no filters


async def test_the_list_updates_live(db_page: DbPage) -> None:
  await db_page.user.open("/employees")
  await db_page.user.should_see("Ann Lee")
  with db_page.db.admin() as conn:
    conn.execute(EMPLOYEE_SQL, ("Dee", "Ng", "78"))
  await db_page.user.should_see("Dee Ng", retries=50)


async def test_a_group_in_the_address_opens_filtered(db_page: DbPage) -> None:
  # Back to a filtered list, a reload or a bookmark; a group that's gone since shows everyone.
  ann, bob = db_page.db.employees
  warehouse = db_page.db.group("Warehouse")
  dee = db_page.db.employee("Dee", "Moss", "78", group_id=warehouse)
  await db_page.user.open(f"/employees?group={warehouse}")
  await db_page.user.should_see(marker=f"employee-row-{dee}")
  assert _rows(db_page) == [dee]
  await db_page.user.open("/employees?group=999999")
  await db_page.user.should_see(marker=f"employee-row-{dee}")
  assert _rows(db_page) == [ann, bob, dee]
