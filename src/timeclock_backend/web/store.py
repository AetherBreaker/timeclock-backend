"""What the page reads and writes, each call inside one `editor_transaction`, so as the signed-in account's badge.

Badges read punches, corrections, saves, employees (no PIN hashes), groups and settings, and insert punches,
corrections and saves (timeclock-schema 0015); the database refuses an account's own punches itself.
"""

# Standard library imports
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

# Third party imports
from timeclock_schema.enums import EmployeeRole, PunchKind
from timeclock_schema.models import Settings

# Local folder imports
from .rules import PAGE_INSTANCE_ID, PagePunch, RecordAction

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterable, Mapping, Sequence
  from datetime import datetime
  from uuid import UUID

  # Third party imports
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow

  # Local folder imports
  from .rules import Record

# The advisory lock's first key, so the page's per-employee locks can't meet another lock's numbers.
_LOCK_SPACE = 7410


@dataclass(frozen=True, slots=True)
class Person:
  """An employee as the page shows them; `username` is what they type at the clock (spec §7)."""

  id: int
  first_name: str
  middle_name: str | None
  last_name: str
  username: str
  role: EmployeeRole
  active: bool
  group_id: int
  phone: str | None
  email: str | None
  fingerprint_exempt: bool
  updated_at: datetime
  fingerprints: int

  @property
  def name(self) -> str:
    """First and last name, as the clock shows it."""
    return f"{self.first_name} {self.last_name}"

  @property
  def sort_key(self) -> tuple[str, str, str, str]:
    """By last name (spec §7)."""
    return (self.last_name.casefold(), self.first_name.casefold(), (self.middle_name or "").casefold(), self.username)


@dataclass(frozen=True, slots=True)
class Context:
  """What every shift page needs besides punches: the hours' zone and settings, the groups and the people."""

  tz: ZoneInfo
  settings: Settings
  groups: Mapping[int, str]
  people: Mapping[int, Person]


async def load_context(conn: AsyncConnection[DictRow], *, default_tz: ZoneInfo) -> Context:
  """The hours' zone, settings, groups, and every employee with their fingerprint count.

  The zone is the backend's, which it writes to `backend.environment` at every start; `default_tz` before the first.
  """
  row = await (await conn.execute("SELECT tz FROM backend.environment")).fetchone()
  settings = await (await conn.execute("SELECT * FROM config.settings")).fetchone()
  groups = await (await conn.execute("SELECT id, name FROM groups")).fetchall()
  people = await (
    await conn.execute(
      "SELECT e.id, e.first_name, e.middle_name, e.last_name, e.pin_username AS username, e.role, e.active, e.group_id,"
      " e.phone, e.email, e.fingerprint_exempt, e.updated_at,"
      " (SELECT count(*) FROM fingerprints f WHERE f.employee_id = e.id) AS fingerprints"
      " FROM employees e"
    )
  ).fetchall()
  return Context(
    tz=default_tz if row is None else ZoneInfo(row["tz"]),
    settings=Settings() if settings is None else Settings.model_validate(settings),
    groups={g["id"]: g["name"] for g in groups},
    people={p["id"]: Person(**{**p, "role": EmployeeRole(p["role"])}) for p in people},
  )


# Each punch after its newest correction, voided ones dropped; then, per employee, the latest corrected clock-out before
# the range and the first at or after its end. After any clock-out the walk is off duty whatever came before (valid, it
# ends the shift; ignored, it was off already), so walking from just after the first anchor agrees with the whole
# history, and the second closes every shift that overlaps the range.
_PUNCHES = """
WITH newest AS (
  SELECT DISTINCT ON (c.punch_id) c.punch_id, c.id, c.action, c.new_at, c.new_kind, c.recorded_at, c.db_login
  FROM punch_corrections c
  ORDER BY c.punch_id, c.recorded_at DESC, c.id DESC
),
eff AS (
  SELECT p.id, p.employee_id, coalesce(n.new_kind, p.kind) AS kind, coalesce(n.new_at, p.at) AS at, p.automatic,
         p.kind AS original_kind, p.at AS original_at, n.id AS version, p.edit_id, p.instance_id, p.db_login,
         p.received_at, n.db_login AS changed_by, n.recorded_at AS changed_at
  FROM punches p LEFT JOIN newest n ON n.punch_id = p.id
  WHERE n.action IS DISTINCT FROM 'void' AND (%(employee)s::bigint IS NULL OR p.employee_id = %(employee)s::bigint)
),
lo AS (
  SELECT DISTINCT ON (employee_id) employee_id, at AS lo_at, id AS lo_id FROM eff
  WHERE kind = 'clock_out' AND at < %(start)s
  ORDER BY employee_id, at DESC, id DESC
),
hi AS (
  SELECT DISTINCT ON (employee_id) employee_id, at AS hi_at, id AS hi_id FROM eff
  WHERE kind = 'clock_out' AND at >= %(end)s
  ORDER BY employee_id, at, id
)
SELECT eff.* FROM eff LEFT JOIN lo USING (employee_id) LEFT JOIN hi USING (employee_id)
WHERE (lo.lo_at IS NULL OR (eff.at, eff.id) > (lo.lo_at, lo.lo_id))
  AND (%(end)s::timestamptz IS NULL OR hi.hi_at IS NULL OR (eff.at, eff.id) <= (hi.hi_at, hi.hi_id))
ORDER BY eff.employee_id, eff.at, eff.id
"""


async def punch_time(conn: AsyncConnection[DictRow], *, employee_id: int, punch_id: UUID) -> datetime | None:
  """The punch's corrected time; None when it isn't the employee's, or is voided."""
  row = await (
    await conn.execute(
      "SELECT coalesce(n.new_at, p.at) AS at, n.action FROM punches p LEFT JOIN LATERAL ("
      " SELECT c.action, c.new_at FROM punch_corrections c WHERE c.punch_id = p.id ORDER BY c.recorded_at DESC, c.id DESC LIMIT 1"
      ") n ON true WHERE p.id = %s AND p.employee_id = %s",
      (punch_id, employee_id),
    )
  ).fetchone()
  return None if row is None or row["action"] == "void" else row["at"]


async def load_punches(
  conn: AsyncConnection[DictRow], *, start: datetime, end: datetime | None, employee_id: int | None = None
) -> dict[int, list[PagePunch]]:
  """Each employee's (or the one's) punches by corrected time, enough to walk `[start, end)` as the full history would.

  From just after the latest corrected clock-out before `start` (else the first punch) through the first at or after
  `end` (else the last; everything when `end` is None), sorted by (at, id). Employees with no punch there are left out.
  """
  rows = await (await conn.execute(_PUNCHES, {"start": start, "end": end, "employee": employee_id})).fetchall()
  found: dict[int, list[PagePunch]] = {}
  for r in rows:
    found.setdefault(r["employee_id"], []).append(
      PagePunch(
        id=r["id"],
        kind=PunchKind(r["kind"]),
        at=r["at"],
        automatic=r["automatic"],
        original_kind=PunchKind(r["original_kind"]),
        original_at=r["original_at"],
        version=r["version"],
        added_on_page=r["edit_id"] is not None,
        instance_id=r["instance_id"],
        db_login=r["db_login"],
        received_at=r["received_at"],
        changed_by=r["changed_by"],
        changed_at=r["changed_at"],
      )
    )
  return found


def window_token(punches: Iterable[PagePunch]) -> str:
  """A fingerprint of which punches there are and which correction each is at: any save, by anyone, changes it."""
  return hashlib.sha256("\n".join(sorted(f"{p.id}:{p.version}" for p in punches)).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoryChange:
  """One punch's change: the punch's kind and time before it and after it (an INSERT has no before, a VOID no after)."""

  punch_id: UUID
  action: RecordAction
  old_kind: PunchKind | None
  old_at: datetime | None
  new_kind: PunchKind | None
  new_at: datetime | None
  correction_id: UUID | None


@dataclass(frozen=True, slots=True)
class HistoryItem:
  """A page save (`edit_id`), or one correction made outside the page, with who made it and its note."""

  when: datetime
  who: str
  note: str
  changes: tuple[HistoryChange, ...]
  edit_id: UUID | None


async def load_history(conn: AsyncConnection[DictRow], *, employee_id: int, start: datetime, end: datetime) -> list[HistoryItem]:
  """Every change to the employee's punches whose original or any corrected time lies in `[start, end)`, newest first.

  Within a save, changes go by the punch's original time.
  """
  punches = await (
    await conn.execute(
      "SELECT p.id, p.kind, p.at, p.edit_id FROM punches p WHERE p.employee_id = %(e)s AND (p.at >= %(s)s AND p.at < %(t)s"
      " OR EXISTS (SELECT FROM punch_corrections c WHERE c.punch_id = p.id AND c.new_at >= %(s)s AND c.new_at < %(t)s))"
      " ORDER BY p.at, p.id",
      {"e": employee_id, "s": start, "t": end},
    )
  ).fetchall()
  ids = [p["id"] for p in punches]
  corrections = await (
    await conn.execute(
      "SELECT c.id, c.punch_id, c.action, c.new_at, c.new_kind, c.note, c.recorded_at, c.db_login, c.edit_id"
      " FROM punch_corrections c WHERE c.punch_id = ANY(%s) ORDER BY c.recorded_at, c.id",
      (ids,),
    )
  ).fetchall()
  edits = await (
    await conn.execute(
      "SELECT id, note, db_login, recorded_at FROM edits WHERE id = ANY(%s)",
      ([*{p["edit_id"] for p in punches} | {c["edit_id"] for c in corrections}],),
    )
  ).fetchall()
  saves: dict[UUID, list[tuple[datetime, HistoryChange]]] = {}
  items: list[HistoryItem] = []
  by_punch: dict[UUID, list[dict[str, Any]]] = {}
  for c in corrections:
    by_punch.setdefault(c["punch_id"], []).append(c)
  for p in punches:
    original_kind, original_at = PunchKind(p["kind"]), p["at"]
    kind, at = original_kind, original_at
    if p["edit_id"] is not None:
      saves.setdefault(p["edit_id"], []).append((p["at"], HistoryChange(p["id"], RecordAction.INSERT, None, None, kind, at, None)))
    for c in by_punch.get(p["id"], []):
      action = RecordAction(c["action"])
      # Each correction replaces the one before, over the original (apply_corrections).
      new_kind = None if action is RecordAction.VOID else PunchKind(c["new_kind"] or original_kind)
      new_at = None if action is RecordAction.VOID else c["new_at"] or original_at
      change = HistoryChange(p["id"], action, kind, at, new_kind, new_at, c["id"])
      if action is RecordAction.AMEND:
        kind, at = new_kind, new_at
      if c["edit_id"] is None:
        items.append(HistoryItem(c["recorded_at"], c["db_login"], c["note"], (change,), None))
      else:
        saves.setdefault(c["edit_id"], []).append((p["at"], change))
  for e in edits:
    changes = tuple(change for _, change in sorted(saves.get(e["id"], []), key=lambda pair: pair[0]))
    items.append(HistoryItem(e["recorded_at"], e["db_login"], e["note"], changes, e["id"]))
  return sorted(items, key=lambda item: item.when, reverse=True)


async def lock_employee(conn: AsyncConnection[DictRow], employee_id: int) -> None:
  """Hold the employee's save lock to the end of the transaction, so two page saves for one employee take turns.

  Advisory: badges can't lock punch rows, which needs UPDATE.
  """
  await conn.execute("SELECT pg_advisory_xact_lock(%s, %s::int)", (_LOCK_SPACE, employee_id))


async def save_edit(conn: AsyncConnection[DictRow], *, employee_id: int, records: Sequence[Record], note: str) -> UUID:
  """Write one save: its `edits` row, then each record naming it; corrections carry the note too (punch_log shows it)."""
  row = await (await conn.execute("INSERT INTO edits (note) VALUES (%s) RETURNING id", (note,))).fetchone()
  assert row is not None  # RETURNING always returns the row
  edit: UUID = row["id"]
  for r in records:
    match r.action:
      case RecordAction.INSERT:
        await conn.execute(
          "INSERT INTO punches (id, employee_id, kind, at, instance_id, edit_id) VALUES (%s, %s, %s::punch_kind, %s, %s, %s)",
          (r.punch_id, employee_id, r.kind, r.at, PAGE_INSTANCE_ID, edit),
        )
      case RecordAction.AMEND | RecordAction.VOID:
        await conn.execute(
          "INSERT INTO punch_corrections (punch_id, action, new_at, new_kind, note, edit_id)"
          " VALUES (%s, %s::correction_action, %s, %s::punch_kind, %s, %s)",
          (r.punch_id, r.action, r.at, r.kind, note, edit),
        )
  return edit
