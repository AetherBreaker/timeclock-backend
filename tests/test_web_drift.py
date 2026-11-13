"""The drift test (spec 2026-11-05 §2): the editor's JavaScript rules give Python's answers, case for case.

The editor previews hours with `editor/rules.js` while Python's `web/rules.py`, over the schema's own walk, decides
what's saved. Every function both sides have runs here over generated and hand-picked cases; `drift_driver.mjs` is the
JavaScript side. Times cross as epoch milliseconds and ids as lowercase UUID strings.
"""

# Standard library imports
import json
import random
import shutil
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

# Third party imports
import pytest
from timeclock_schema.enums import PunchKind
from timeclock_schema.models import Settings
from timeclock_schema.shifts import week_bounds

# First party imports
from timeclock_backend.web import rules, text
from timeclock_backend.web.rules import DraftPunch, PagePunch

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DRIVER = Path(__file__).with_name("drift_driver.mjs")
ZONES = ("America/New_York", "America/Chicago", "America/Los_Angeles", "UTC")
# The weeks of both 2026 DST changes in the US, a plain week, and a year's end.
DAYS = (date(2026, 3, 7), date(2026, 3, 8), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 4), date(2026, 12, 31))
IN, OUT = PunchKind.CLOCK_IN, PunchKind.CLOCK_OUT
BS, BE, LS, LE = PunchKind.BREAK_START, PunchKind.BREAK_END, PunchKind.LUNCH_START, PunchKind.LUNCH_END
_VALID = {"off": (IN,), "working": (OUT, BS, LS), "break": (BE, OUT), "lunch": (LE, OUT)}
_NEXT = {IN: "working", OUT: "off", BS: "break", BE: "working", LS: "lunch", LE: "working"}
# How often the generator makes each thing happen.
_ODDS = {"same_instant": 0.15, "valid": 0.75, "corrected": 0.2, "automatic": 0.2, "page": 0.1, "received": 0.7, "after_all": 0.7}


def _ms(at: datetime) -> int:
  return (at - EPOCH) // timedelta(milliseconds=1)


def _dt(ms: int) -> datetime:
  return EPOCH + timedelta(milliseconds=ms)


def _punch(at: datetime, kind: PunchKind, **fields: Any) -> dict[str, Any]:
  values: dict[str, Any] = {
    "id": str(uuid4()),
    "kind": kind.value,
    "at": _ms(at),
    "automatic": False,
    "originalKind": kind.value,
    "originalAt": _ms(at),
    "version": None,
    "addedOnPage": False,
    "instanceId": "front",
    "dbLogin": "clock_front",
    "receivedAt": None,
    "changedBy": None,
    "changedAt": None,
  }
  return values | fields


def _page_punch(p: dict[str, Any]) -> PagePunch:
  return PagePunch(
    id=UUID(p["id"]),
    kind=PunchKind(p["kind"]),
    at=_dt(p["at"]),
    automatic=p["automatic"],
    original_kind=PunchKind(p["originalKind"]),
    original_at=_dt(p["originalAt"]),
    version=None if p["version"] is None else UUID(p["version"]),
    added_on_page=p["addedOnPage"],
    instance_id=p["instanceId"],
    db_login=p["dbLogin"],
    received_at=None if p["receivedAt"] is None else _dt(p["receivedAt"]),
    changed_by=p["changedBy"],
    changed_at=None if p["changedAt"] is None else _dt(p["changedAt"]),
  )


def _case(
  tz: str,
  punches: list[dict[str, Any]],
  *,
  as_of: datetime,
  week_of: datetime,
  draft: list[dict[str, Any]] = (),  # pyright: ignore[reportArgumentType]
  touched: list[str] = (),  # pyright: ignore[reportArgumentType]
  span: tuple[datetime, datetime] | None = None,
  paid_break_minutes: int = 30,
  break_limit_minutes: int = 15,
  max_breaks_per_day: int = 2,
  walls: list[tuple[str, str]] = (),  # pyright: ignore[reportArgumentType]
) -> dict[str, Any]:
  zone = ZoneInfo(tz)
  week = week_bounds(week_of, zone)
  span = span or (week_of, week_of + timedelta(hours=8))
  return {
    "tz": tz,
    "paidBreakMinutes": paid_break_minutes,
    "breakLimitMinutes": break_limit_minutes,
    "maxBreaksPerDay": max_breaks_per_day,
    "asOf": _ms(as_of),
    "week": [_ms(week[0]), _ms(week[1])],
    "punches": punches,
    "draft": list(draft),
    "touched": list(touched),
    "span": [_ms(span[0]), _ms(span[1])],
    "walls": list(walls),
  }


def _random_case(rng: random.Random) -> dict[str, Any]:  # noqa: C901 - one generator, read top to bottom
  tz = rng.choice(ZONES)
  zone = ZoneInfo(tz)
  day = rng.choice(DAYS)
  start = datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)
  offsets = sorted(rng.randrange(0, 72 * 3600) for _ in range(rng.randint(0, 14)))
  for i in range(1, len(offsets)):
    if rng.random() < _ODDS["same_instant"]:
      offsets[i] = offsets[i - 1]  # punches at one instant
  state, punches = "off", []
  for offset in offsets:
    # Mostly a punch the walk accepts, so there are shifts to hold the anomalies.
    kind = rng.choice(_VALID[state]) if rng.random() < _ODDS["valid"] else rng.choice(list(PunchKind))
    if kind in _VALID[state]:
      state = _NEXT[kind]
    at = start + timedelta(seconds=offset)
    corrected = rng.random() < _ODDS["corrected"]
    punches.append(
      _punch(
        at,
        kind,
        automatic=kind is OUT and rng.random() < _ODDS["automatic"],
        originalKind=(rng.choice(list(PunchKind)) if corrected else kind).value,
        originalAt=_ms(at - timedelta(minutes=rng.randint(0, 600) if corrected else 0)),
        version=str(uuid4()) if corrected else None,
        addedOnPage=rng.random() < _ODDS["page"],
        dbLogin=rng.choice(("clock_front", "postgres", "ann@example.com", None)),
        receivedAt=_ms(at + timedelta(seconds=rng.randint(0, 900))) if rng.random() < _ODDS["received"] else None,
        changedBy="ann@example.com" if corrected else None,
        changedAt=_ms(at + timedelta(hours=1)) if corrected else None,
      )
    )
  draft: dict[str, dict[str, Any]] = {}
  for _ in range(rng.randint(0, 3) if punches else 0):
    target = rng.choice(punches)
    d = draft.setdefault(
      target["id"],
      {
        "id": target["id"],
        "kind": target["kind"],
        "at": target["at"],
        "removed": False,
        "confirmed": False,
        "version": target["version"],
        "new": False,
      },
    )
    match rng.choice(("move", "retype", "remove", "confirm", "add", "conflict", "gone")):
      case "move":
        d["at"] += rng.randint(-300, 300) * 60_000
      case "retype":
        d["kind"] = rng.choice(list(PunchKind)).value
      case "remove":
        d["removed"] = True
      case "confirm":
        d["confirmed"] = True
      case "add":
        added = str(uuid4())
        at = _ms(start + timedelta(seconds=rng.randrange(0, 72 * 3600)))
        draft[added] = {"id": added, "kind": rng.choice(list(PunchKind)).value, "at": at, "removed": False}
        draft[added] |= {"confirmed": False, "version": None, "new": True}
      case "conflict":
        d["version"] = str(uuid4())
      case "gone":
        gone = str(uuid4())
        draft[gone] = d | {"id": gone}
  focus_start = rng.randrange(0, len(punches)) if punches else 0
  touched = [*draft, *(p["id"] for p in punches[focus_start : focus_start + rng.randint(0, 6)])]
  span_start = start + timedelta(seconds=rng.randrange(0, 72 * 3600))
  return _case(
    tz,
    punches,
    as_of=start + timedelta(hours=80) if rng.random() < _ODDS["after_all"] else start + timedelta(seconds=rng.randrange(0, 72 * 3600)),
    week_of=_dt(rng.choice(punches)["at"]) if punches else start,
    draft=list(draft.values()),
    touched=touched,
    span=(span_start, span_start + timedelta(hours=rng.randint(1, 10))),
    paid_break_minutes=rng.choice((0, 15, 30)),
    break_limit_minutes=rng.choice((0, 10, 15)),
    max_breaks_per_day=rng.choice((0, 1, 2)),
    walls=[
      ((day + timedelta(days=rng.randint(0, 2))).isoformat(), f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}") for _ in range(6)
    ],
  )


def _local(day: date, clock: str, tz: str = "America/New_York") -> datetime:
  return datetime.combine(day, time.fromisoformat(clock), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def _hand_picked() -> list[dict[str, Any]]:
  tue, later = date(2026, 11, 3), datetime(2026, 11, 20, tzinfo=UTC)
  double = [_punch(_local(tue, c), k) for c, k in (("07:01", IN), ("07:04", IN), ("09:30", BS), ("09:44", BE), ("15:33", OUT))]
  strays = [_punch(_local(tue, c), k) for c, k in (("07:00", IN), ("15:30", OUT), ("17:02", OUT), ("17:10", BS))]
  same_instant = [
    _punch(_local(tue, "07:00"), IN, id=str(UUID(int=2))),
    _punch(_local(tue, "07:00"), OUT, id=str(UUID(int=1))),
    _punch(_local(tue, "07:00"), BS, id=str(UUID(int=3))),
  ]
  overnight = [_punch(_local(date(2026, 11, 8), "22:00"), IN), _punch(_local(date(2026, 11, 9), "06:00"), OUT)]
  spring = [
    _punch(_local(date(2026, 3, 8), c), k)
    for c, k in (("00:30", IN), ("01:30", BS), ("03:10", BE), ("04:00", LS), ("04:45", LE), ("06:00", OUT))
  ]
  autumn = [_punch(_local(date(2026, 10, 31), c), k) for c, k in (("23:30", IN), ("23:50", BS))] + [
    _punch(_local(date(2026, 11, 1), c), k) for c, k in (("01:20", BE), ("05:00", OUT))
  ]
  mon, tues = date(2026, 11, 2), date(2026, 11, 3)
  two_days = [_punch(_local(d, c), k) for d in (mon, tues) for c, k in (("07:00", IN), ("15:00", OUT))]
  remove_monday_out = {**_draft_of(two_days[1]), "removed": True}
  three_breaks = [
    _punch(_local(tue, c), k)
    for c, k in (
      ("07:00", IN),
      ("09:00", BS),
      ("09:10", BE),
      ("11:00", BS),
      ("11:10", BE),
      ("14:00", BS),
      ("14:10", BE),
      ("16:00", OUT),
    )
  ]
  reviewed = [*three_breaks[:5], {**three_breaks[5], "version": str(uuid4())}, *three_breaks[6:]]
  return [
    _case("America/New_York", double, as_of=later, week_of=_local(tue, "12:00"), touched=[double[0]["id"]]),
    _case("America/New_York", strays, as_of=later, week_of=_local(tue, "12:00")),
    _case("America/New_York", double[:3], as_of=_local(tue, "12:00"), week_of=_local(tue, "12:00")),
    _case("America/New_York", same_instant, as_of=later, week_of=_local(tue, "12:00")),
    _case("America/New_York", overnight, as_of=later, week_of=_local(date(2026, 11, 8), "12:00")),
    _case("America/New_York", overnight, as_of=later, week_of=_local(date(2026, 11, 9), "12:00")),
    _case(
      "America/New_York",
      spring,
      as_of=later,
      week_of=_local(date(2026, 3, 8), "12:00"),
      walls=[("2026-03-08", "02:30"), ("2026-03-08", "01:59"), ("2026-03-08", "03:00")],
    ),
    _case(
      "America/New_York",
      autumn,
      as_of=later,
      week_of=_local(date(2026, 11, 1), "12:00"),
      walls=[("2026-11-01", "01:30"), ("2026-11-01", "00:59"), ("2026-11-01", "02:00")],
    ),
    _case(
      "America/New_York",
      two_days,
      as_of=later,
      week_of=_local(mon, "12:00"),
      draft=[remove_monday_out],
      touched=[two_days[0]["id"], two_days[1]["id"]],
    ),
    _case(
      "America/New_York",
      two_days,
      as_of=later,
      week_of=_local(mon, "12:00"),
      span=(_local(mon, "14:00"), _local(mon, "18:00")),
    ),
    _case("America/New_York", three_breaks, as_of=later, week_of=_local(tue, "12:00")),
    _case("America/New_York", reviewed, as_of=later, week_of=_local(tue, "12:00")),
    _case("America/New_York", three_breaks, as_of=later, week_of=_local(tue, "12:00"), max_breaks_per_day=0),
  ]


def _draft_of(p: dict[str, Any]) -> dict[str, Any]:
  return {"id": p["id"], "kind": p["kind"], "at": p["at"], "removed": False, "confirmed": False, "version": p["version"], "new": False}


def _draft_punch(d: dict[str, Any]) -> DraftPunch:
  return DraftPunch(
    id=UUID(d["id"]),
    kind=PunchKind(d["kind"]),
    at=_dt(d["at"]),
    removed=d["removed"],
    confirmed=d["confirmed"],
    version=None if d["version"] is None else UUID(d["version"]),
    new=d["new"],
  )


def _entry_json(e: rules.Entry) -> dict[str, Any]:
  return {
    "kind": e.kind.value,
    "key": str(e.key),
    "punches": [str(p.id) for p in e.punches],
    "ignored": sorted(str(i) for i in e.ignored),
    "start": _ms(e.start),
    "end": None if e.end is None else _ms(e.end),
  }


def _answer(case: dict[str, Any]) -> dict[str, Any]:
  """Python's answers, in the shape `drift_driver.mjs` prints."""
  tz, as_of = ZoneInfo(case["tz"]), _dt(case["asOf"])
  settings = Settings(paid_break_minutes=case["paidBreakMinutes"], break_limit_minutes=case["breakLimitMinutes"])
  punches = [_page_punch(p) for p in case["punches"]]
  base = {p.id: p for p in punches}
  draft = [_draft_punch(d) for d in case["draft"]]
  found = rules.entries(punches, as_of=as_of)
  over = rules.over_breaks(found, limit=case["maxBreaksPerDay"], tz=tz)
  records = rules.records(base, [d for d in draft if not rules.conflicts(base, [d])])
  after = rules.apply(punches, records)
  refused = rules.refusal(punches, after, {UUID(t) for t in case["touched"]}, as_of=as_of)
  overlap = rules.overlapping(punches, _dt(case["span"][0]), _dt(case["span"][1]), as_of=as_of)
  hours = [rules.entry_hours(e, punches, settings=settings, tz=tz, now=as_of) for e in found]
  week = rules.week_hours(punches, settings=settings, tz=tz, week=(_dt(case["week"][0]), _dt(case["week"][1])), now=as_of)
  millis = timedelta(milliseconds=1)
  return {
    "entries": [_entry_json(e) for e in found],
    "flags": [[f.value for f in rules.flags(e, over=over)] for e in found],
    "over": sorted([str(i), n] for i, n in over.items()),
    "problems": [rules.problem(e) for e in found],
    "hours": [None if h is None else [h.worked // millis, h.paid_break // millis, h.unpaid // millis] for h in hours],
    "week": [week.worked // millis, week.paid_break // millis, week.unpaid // millis],
    "conflicts": [str(i) for i in rules.conflicts(base, draft)],
    "records": [
      {
        "action": r.action.value,
        "punchId": str(r.punch_id),
        "kind": None if r.kind is None else r.kind.value,
        "at": None if r.at is None else _ms(r.at),
      }
      for r in records
    ],
    "after": [_entry_json(e) for e in rules.entries(after, as_of=as_of)],
    "refusal": None
    if refused is None
    else {
      "kind": refused.kind.value,
      "punchIds": [str(i) for i in refused.punch_ids],
      "entryKey": None if refused.entry_key is None else str(refused.entry_key),
    },
    "overlap": None if overlap is None else str(overlap.key),
    "durations": [None if h is None else text.hm(h.worked) for h in hours],
    "clocks": [
      [text.clock_time(p.at, tz, h24=False), text.clock_time(p.at, tz, h24=True), p.at.astimezone(tz).date().isoformat()]
      for p in punches
    ],
    # A wall time names the instant Python's fold=0 gives: the earlier offset in a gap or an overlap.
    "walls": [
      _ms(datetime.combine(date.fromisoformat(d), time.fromisoformat(t), tzinfo=tz).astimezone(UTC)) for d, t in case["walls"]
    ],
  }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_javascript_rules_agree_with_python() -> None:
  rng = random.Random(20261106)
  cases = [*_hand_picked(), *(_random_case(rng) for _ in range(400))]
  node = shutil.which("node")
  assert node is not None
  done = subprocess.run([node, str(DRIVER)], input=json.dumps(cases), capture_output=True, text=True, check=False, timeout=120)
  assert done.returncode == 0, done.stderr
  javascript = json.loads(done.stdout)
  for i, (case, js) in enumerate(zip(cases, javascript, strict=True)):
    python = _answer(case)
    assert js == python, f"case {i} differs\ncase: {json.dumps(case)}\npython: {json.dumps(python)}\njavascript: {json.dumps(js)}"
