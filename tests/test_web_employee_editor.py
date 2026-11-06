"""Tests for the employee editor (spec 2026-11-05 §9, §11; mockup screens K to M), over real databases."""

# Standard library imports
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

# Third party imports
import bcrypt
from nicegui import ui
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

# First party imports
from timeclock_backend.web import employee_editor, text

if TYPE_CHECKING:
  # Third party imports
  import pytest
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


async def test_an_outside_change_updates_an_untouched_field(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  await db_page.user.should_see("None yet.")
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET email = 'bob@example.com' WHERE id = %s", (bob,))
  await db_page.user.should_see(marker="history-line", content="Email: empty to bob@example.com", retries=50)
  assert _widget(db_page, "field-email").value == "bob@example.com"
  await db_page.user.should_see("None yet.")
  await db_page.user.should_not_see(marker="outside")


async def test_an_outside_change_to_a_drafted_field_asks_whose_to_keep(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-phone").type("5555550142")
  db_page.user.find(marker="field-email").type("mine@example.com")
  await db_page.user.should_see("Email: empty to mine@example.com")
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET phone = '+15555550199', email = 'theirs@example.com' WHERE id = %s", (bob,))
  await db_page.user.should_see(marker="theirs-phone", retries=50)
  await db_page.user.should_see("Pick theirs or yours for each field marked in red to save.")
  assert not _widget(db_page, "save").enabled
  db_page.user.find(marker="keep-theirs-email").click()
  db_page.user.find(marker="keep-mine-phone").click()
  await db_page.user.should_see("Phone: (555) 555-0199 to (555) 555-0142")
  await db_page.user.should_not_see(marker="theirs-phone")
  await db_page.user.should_not_see("Email: theirs@example.com to mine@example.com")
  assert _widget(db_page, "field-email").value == "theirs@example.com"
  db_page.user.find(marker="save").click()
  await db_page.user.should_see("Saved.")
  assert _row(db_page, bob, "phone", "email") == ("+15555550142", "theirs@example.com")


async def test_an_outside_change_to_a_field_being_typed_asks_whose_to_keep(db_page: DbPage) -> None:
  # Half a phone fails its check until the tenth digit; someone else's save meanwhile is still a pick once it's whole.
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-phone").type("555-555")
  await db_page.user.should_see(text.PHONE_INVALID)
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET phone = '+15555550199' WHERE id = %s", (bob,))
  await db_page.user.should_see(marker="history-line", content="Phone: empty to (555) 555-0199", retries=50)
  assert _widget(db_page, "field-phone").value == "555-555"  # what's typed stays
  db_page.user.find(marker="field-phone").type("-0142")
  await db_page.user.should_see(marker="theirs-phone")
  await db_page.user.should_see("Pick theirs or yours for each field marked in red to save.")


async def test_an_outside_change_elsewhere_asks_to_confirm_the_save(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-phone").type("5555550142")
  await db_page.user.should_see("Phone: empty to (555) 555-0142")
  with db_page.db.admin() as conn:
    conn.execute("UPDATE employees SET email = 'theirs@example.com' WHERE id = %s", (bob,))
  user = str(conninfo_to_dict(db_page.db.dsn)["user"])
  await db_page.user.should_see(f"{user} changed Email while you were editing.", retries=50)
  db_page.user.find(marker="save").click()
  await db_page.user.should_see("Save your changes on top of theirs?")
  assert _row(db_page, bob, "phone") == (None,)
  db_page.user.find(marker="confirm-save").click()
  await db_page.user.should_see("Saved.")
  assert _row(db_page, bob, "phone", "email") == ("+15555550142", "theirs@example.com")


async def test_a_save_that_lost_a_race_marks_the_field(db_page: DbPage, monkeypatch: pytest.MonkeyPatch) -> None:
  # Another save lands between this page's last read and its own: the wake hasn't come yet when Save is pressed.
  bob = db_page.db.employees[1]
  real = employee_editor.save_employee

  async def raced(conn: Any, **kwargs: Any) -> Any:
    with db_page.db.admin() as admin:
      admin.execute("UPDATE employees SET phone = '+15555550199' WHERE id = %s", (bob,))
    return await real(conn, **kwargs)

  monkeypatch.setattr(employee_editor, "save_employee", raced)
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-phone").type("5555550142")
  await db_page.user.should_see("Phone: empty to (555) 555-0142")
  db_page.user.find(marker="save").click()
  await db_page.user.should_see(text.EMPLOYEE_CHANGED)
  await db_page.user.should_see(marker="theirs-phone")
  assert _row(db_page, bob, "phone") == ("+15555550199",)


def _pin_hash(page: DbPage, employee: int) -> str:
  (stored,) = _row(page, employee, "pin_hash")
  return stored


async def _set_pin(page: DbPage, first: str, again: str) -> None:
  page.user.find(marker="set-pin").click()
  page.user.find(marker="new-pin").type(first)
  page.user.find(marker="new-pin-again").type(again)
  page.user.find(marker="set-pin-save").click()


async def test_setting_a_pin_saves_it_at_once_and_logs_pin_changed(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="field-first_name").clear().type("Robert")
  await db_page.user.should_see("First name: Bob to Robert")
  await _set_pin(db_page, "4827", "4827")
  await db_page.user.should_see("PIN set.", retries=50)  # bcrypt takes a moment
  stored = _pin_hash(db_page, bob)
  assert stored.startswith("$2a$")
  assert bcrypt.checkpw(b"4827", stored.encode())
  await db_page.user.should_see(marker="history-line", content="PIN changed")
  await db_page.user.should_see("First name: Bob to Robert")  # the draft stays a draft
  assert _row(db_page, bob, "first_name") == ("Bob",)


async def test_mismatched_pins_are_refused(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  before = _pin_hash(db_page, bob)
  await db_page.user.open(f"/employee?id={bob}")
  await _set_pin(db_page, "4827", "4828")
  await db_page.user.should_see(text.PIN_MISMATCH)
  assert _pin_hash(db_page, bob) == before


async def test_a_pin_keeps_its_leading_zero(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  await _set_pin(db_page, "0123", "0123")
  await db_page.user.should_see("PIN set.", retries=50)  # bcrypt takes a moment
  assert bcrypt.checkpw(b"0123", _pin_hash(db_page, bob).encode())


async def test_a_pin_of_other_digits_is_refused(db_page: DbPage) -> None:
  # Arabic-Indic digits pass str.isdecimal() but no clock keypad can type them.
  bob = db_page.db.employees[1]
  before = _pin_hash(db_page, bob)
  await db_page.user.open(f"/employee?id={bob}")
  await _set_pin(db_page, "\u0661\u0662\u0663\u0664", "\u0661\u0662\u0663\u0664")
  await db_page.user.should_see(text.PIN_DIGITS_ONLY)
  assert _pin_hash(db_page, bob) == before


async def test_make_one_up_shows_four_digits_once(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/employee?id={bob}")
  db_page.user.find(marker="set-pin").click()
  db_page.user.find(marker="make-pin").click()
  await db_page.user.should_see("Give this to Bob. It won't be shown again once you close this.")
  made = re.fullmatch(r"Made up for you: (\d{4})", _widget(db_page, "made-pin").text)
  assert made is not None
  pin = made[1]
  db_page.user.find(marker="set-pin-save").click()
  await db_page.user.should_see("PIN set.", retries=50)  # bcrypt takes a moment
  assert bcrypt.checkpw(pin.encode(), _pin_hash(db_page, bob).encode())  # both fields held it, or it would be refused
  db_page.user.find(marker="set-pin").click()
  await db_page.user.should_not_see(marker="made-pin")
  assert _widget(db_page, "new-pin").value == ""


async def test_you_can_set_your_own_pin(db_page: DbPage) -> None:
  ann = db_page.db.employees[0]  # the signed-in account's
  await db_page.user.open(f"/employee?id={ann}")
  await _set_pin(db_page, "2468", "2468")
  await db_page.user.should_see("PIN set.", retries=50)  # bcrypt takes a moment
  assert bcrypt.checkpw(b"2468", _pin_hash(db_page, ann).encode())
