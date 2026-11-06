"""Tests for the employee editor (spec 2026-11-05 §9, §11; mockup screens K to M), over real databases."""

# Standard library imports
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

# Third party imports
from nicegui import ui
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

# First party imports
from timeclock_backend.web import text

if TYPE_CHECKING:
  # Third party imports
  from page_sim import DbPage


def _widget(page: DbPage, marker: str) -> Any:
  [found] = page.user.find(marker=marker).elements
  return found


def _set(page: DbPage, marker: str, *, value: object) -> None:
  """A select or switch moved as the browser would."""
  assert page.user.client is not None
  with page.user.client:
    _widget(page, marker).set_value(value)


def _row(page: DbPage, employee: int, *columns: str) -> tuple[Any, ...]:
  with page.db.admin() as conn:
    query = sql.SQL("SELECT {} FROM employees WHERE id = %s").format(sql.SQL(", ").join(map(sql.Identifier, columns)))
    row = conn.execute(query, (employee,)).fetchone()
  assert row is not None
  return tuple(row)


async def test_editing_a_field_lists_it_and_saving_writes_it(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  await db_page.user.should_see("Bob Lee")
  await db_page.user.should_see("None yet.")
  db_page.user.find(marker="field-phone").type("555.555.0142")
  await db_page.user.should_see("Phone: empty to (555) 555-0142")
  await db_page.user.should_see("Was empty")
  db_page.user.find(marker="note").type("new phone")
  db_page.user.find(marker="save").click()
  await db_page.user.should_see("Saved.")
  assert _row(db_page, bob, "phone") == ("+15555550142",)
  await db_page.user.should_see(marker="history-line", content="Phone: empty to (555) 555-0142")
  await db_page.user.should_see(marker="history-line", content='"new phone"')
  await db_page.user.should_see("None yet.")


async def test_discard_puts_every_field_back(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-first_name").clear().type("Robert")
  _set(db_page, "field-role", value="manager")
  await db_page.user.should_see("First name: Bob to Robert")
  await db_page.user.should_see("Role: Employee to Manager")
  db_page.user.find(marker="discard").click()
  await db_page.user.should_see("None yet.")
  assert (_widget(db_page, "field-first_name").value, _widget(db_page, "field-role").value) == ("Bob", "employee")


async def test_undo_in_your_changes_puts_that_field_back(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-first_name").clear().type("Robert")
  db_page.user.find(marker="field-last_name").clear().type("Leigh")
  await db_page.user.should_see("First name: Bob to Robert")
  db_page.user.find(marker="undo-first_name").click()
  await db_page.user.should_not_see("First name: Bob to Robert")
  await db_page.user.should_see("Last name: Lee to Leigh")
  assert _widget(db_page, "field-first_name").value == "Bob"


async def test_a_bad_phone_is_refused_under_the_field(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-phone").type("555-0142")
  await db_page.user.should_see(text.PHONE_INVALID)
  await db_page.user.should_see("Fix the fields marked in red to save.")
  assert not _widget(db_page, "save").enabled


async def test_clearing_optional_fields_stores_null(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET middle_name = 'Q', phone = '+15555550142', email = 'bob@example.com' WHERE id = %s", (bob,))
  await db_page.user.open(f"/employee?id={bob}")
  for field in ("middle_name", "phone", "email"):
    db_page.user.find(marker=f"field-{field}").clear()
  await db_page.user.should_see("Email: bob@example.com to empty")
  db_page.user.find(marker="save").click()
  await db_page.user.should_see("Saved.")
  assert _row(db_page, bob, "middle_name", "phone", "email") == (None, None, None)


async def test_your_own_record_locks_work_fields(db_page: DbPage) -> None:
  ann = db_page.db.employees[0]  # the signed-in account's
  await db_page.user.open(f"/employee?id={ann}")
  await db_page.user.should_see(text.OWN_RECORD)
  locked = {f: _widget(db_page, f"field-{f}").enabled for f in ("group_id", "role", "fingerprint_exempt", "active")}
  assert locked == dict.fromkeys(locked, False)
  db_page.user.find(marker="field-first_name").clear().type("Annie")
  await db_page.user.should_see("First name: Ann to Annie")  # the redraw that turns Save on runs in the background
  db_page.user.find(marker="save").click()
  await db_page.user.should_see("Saved.")
  assert _row(db_page, ann, "first_name") == ("Annie",)


async def test_switching_active_off_explains_it(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  await db_page.user.should_not_see(text.switch_off_note("Bob"))
  _set(db_page, "field-active", value=False)
  await db_page.user.should_see(text.switch_off_note("Bob"))
  await db_page.user.should_see("Status: Active to Switched off")


async def test_history_lists_earlier_changes(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET last_name = 'Leigh' WHERE id = %s", (bob,))
  await db_page.user.open(f"/employee?id={bob}")
  await db_page.user.should_see(marker="history-line", content="Last name: Lee to Leigh")
  await db_page.user.should_see(marker="history-line", content=str(conninfo_to_dict(db_page.db.dsn)["user"]))


async def test_technical_details_show_stored_values(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  office = db_page.db.group("Office")
  print_id = uuid4()
  with db_page.db.admin() as conn:
    conn.execute(
      "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id, finger)"
      " VALUES (%s, %s, '\\x00', %s, 'front', 'right_index')",
      (print_id, bob, datetime(2026, 10, 28, 18, 10, tzinfo=UTC)),
    )
  await db_page.user.open(f"/employee?id={bob}")
  await db_page.user.should_see("Right index, registered Oct 28, 2:10 PM")
  db_page.user.find(marker="pref-technical").click()
  await db_page.user.should_see(f"employee id {bob} · username 34")
  await db_page.user.should_see("role employee")
  await db_page.user.should_see(f"{str(print_id)[:8]}… · right_index · registered 2026-10-28 14:10:00 · instance front")
  _set(db_page, "field-group_id", value=office)
  await db_page.user.should_see(f"group_id 1 → {office}")
  await db_page.user.should_see(f"will write: employees {bob} group_id = {office} · 1 change log row")


async def test_an_unknown_employee_says_so(db_page: DbPage) -> None:
  await db_page.user.open("/employee?id=999999")
  await db_page.user.should_see(text.EMPLOYEE_GONE)
  await db_page.user.should_see(marker="crumb")
  await db_page.user.should_not_see(kind=ui.input)
