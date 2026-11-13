// The shift editor's draft logic (editor/draft.js), under node:test; tests/test_web_js.py runs this file.
import assert from "node:assert/strict";
import { test } from "node:test";

import * as d from "../../src/timeclock_backend/web/editor/draft.js";
import { zonedTime } from "../../src/timeclock_backend/web/editor/rules.js";

const TZ = "America/New_York";
const OFFLINE = "The connection is interrupted. You can keep editing, and save your changes as soon as it's back.";
// Ids whose string order is their number's, and whose first 8 characters differ.
const id = (n) => `${String(n).padStart(8, "0")}-0000-4000-8000-000000000000`;
const at = (day, time) => zonedTime(`2026-11-${String(day).padStart(2, "0")}`, time, TZ);
const punch = (n, kind, day, time, extra = {}) => ({
  id: id(n),
  kind,
  at: at(day, time),
  automatic: false,
  originalKind: kind,
  originalAt: at(day, time),
  version: null,
  addedOnPage: false,
  instanceId: "main-office",
  dbLogin: "timeclock_clocks",
  receivedAt: null,
  changedBy: null,
  changedAt: null,
  ...extra,
});
const base = (punches, { focus = punches.map((p) => p.id), token = "t1", day = null } = {}) => ({
  employee: { id: 2, name: "Bob Lee", first: "Bob", username: "34" },
  own: false,
  tz: TZ,
  paidBreakMinutes: 15,
  now: at(6, "12:00"),
  week: [at(2, "00:00"), at(9, "00:00")],
  token,
  punches,
  focus,
  day,
});
// Add shift's base: Bob's week (a Wednesday morning shift) with nothing opened, for Wednesday.
const adding = () => d.newDraft(base([punch(1, "clock_in", 4, "07:55"), punch(2, "clock_out", 4, "12:10")], { focus: [], day: "2026-11-04" }));
const ids = (list) => list.map((p) => p.id);

const plain = [punch(1, "clock_in", 3, "07:00"), punch(2, "break_start", 3, "10:00"), punch(3, "break_end", 3, "10:15"), punch(4, "clock_out", 3, "15:30")];
const doubleIn = [punch(1, "clock_in", 3, "07:01"), punch(2, "clock_in", 3, "07:04"), punch(3, "clock_out", 3, "15:33")];
const withStray = [punch(1, "clock_in", 4, "07:00"), punch(2, "clock_out", 4, "15:30"), punch(3, "clock_out", 4, "17:02")];
const automatic = [punch(1, "clock_in", 3, "07:58"), punch(2, "clock_out", 3, "23:00", { automatic: true, dbLogin: "postgres" })];

test("each edit becomes its record", () => {
  let draft = d.newDraft(base(plain));
  draft = d.setTime(draft, id(4), at(3, "15:45"));
  draft = d.setKind(draft, id(2), "lunch_start");
  draft = d.remove(draft, id(3));
  draft = d.add(draft, "lunch_end", at(3, "10:30"), id(9));
  assert.deepEqual(d.recordsOf(draft), [
    { action: "amend", punchId: id(4), kind: null, at: at(3, "15:45") },
    { action: "amend", punchId: id(2), kind: "lunch_start", at: null },
    { action: "void", punchId: id(3), kind: null, at: null },
    { action: "insert", punchId: id(9), kind: "lunch_end", at: at(3, "10:30") },
  ]);
  assert.deepEqual(
    d.draftPunches(draft).find((p) => p.id === id(4)),
    { id: id(4), kind: "clock_out", at: at(3, "15:45"), removed: false, confirmed: false, version: null, new: false },
  );
});

test("undo, or an edit back to where it was, leaves nothing to save", () => {
  let draft = d.setTime(d.newDraft(base(plain)), id(4), at(3, "15:45"));
  assert.equal(d.undo(draft, id(4)).edits.size, 0);
  draft = d.setTime(draft, id(4), at(3, "15:30"));
  assert.equal(draft.edits.size, 0);
  assert.equal(d.remove(d.add(draft, "break_start", at(3, "12:00"), id(9)), id(9)).edits.size, 0);
});

test("a double clock-in shares the Clock in slot", () => {
  const draft = d.newDraft(base(doubleIn));
  const [entry] = d.shown(draft);
  const found = d.sections(draft, entry);
  assert.deepEqual(ids(found.clockIn.punches), [id(1), id(2)]);
  assert.deepEqual(ids(found.clockOut.punches), [id(3)]);
  assert.deepEqual(found.others, []);
});

test("a double break end shares the break's End slot, an extra start its Start slot", () => {
  const punches = [
    punch(1, "clock_in", 3, "07:00"),
    punch(2, "break_start", 3, "10:00"),
    punch(3, "break_start", 3, "10:02"),
    punch(4, "break_end", 3, "10:15"),
    punch(5, "break_end", 3, "10:16"),
    punch(6, "lunch_start", 3, "12:00"),
    punch(7, "lunch_end", 3, "12:30"),
    punch(8, "clock_out", 3, "15:30"),
  ];
  const draft = d.newDraft(base(punches));
  const found = d.sections(draft, d.shown(draft)[0]);
  assert.deepEqual(
    found.breaks.map((b) => [ids(b.start.punches), ids(b.end.punches)]),
    [[[id(2), id(3)], [id(4), id(5)]]],
  );
  assert.deepEqual(
    found.lunches.map((l) => [ids(l.start.punches), ids(l.end.punches)]),
    [[[id(6)], [id(7)]]],
  );
});

test("a punch no slot can take is listed apart", () => {
  const draft = d.newDraft(base([punch(1, "clock_in", 3, "07:00"), punch(2, "break_end", 3, "09:44"), punch(3, "clock_out", 3, "15:30")]));
  const found = d.sections(draft, d.shown(draft)[0]);
  assert.deepEqual(ids(found.others), [id(2)]);
  assert.deepEqual(found.breaks, []);
});

test("a stray that day is offered, and once moved in joins the slot of its kind", () => {
  let draft = d.newDraft(base(withStray, { focus: [id(1), id(2)] }));
  const [shift] = d.shown(draft);
  assert.deepEqual(
    d.sections(draft, shift).strays.map((e) => e.key),
    [id(3)],
  );
  draft = d.moveIn(draft, id(3), shift.key);
  assert.deepEqual(
    d.shown(draft).map((e) => e.key),
    [id(1), id(3)],
  );
  const found = d.sections(draft, d.shown(draft)[0]);
  assert.deepEqual(ids(found.clockOut.punches), [id(2), id(3)]);
  assert.deepEqual(found.strays, []);
});

test("a stray moved in from its own editor brings its shift into view", () => {
  const draft = d.moveIn(d.newDraft(base(withStray, { focus: [id(3)] })), id(3), id(1));
  assert.deepEqual(
    d.shown(draft).map((e) => e.key),
    [id(1), id(3)],
  );
  assert.deepEqual(
    d.changeList(d.remove(draft, id(2)), { tz: TZ, h24: false }),
    [{ id: id(2), text: "Removed the clock-out at 3:30 PM" }],
  );
});

test("a stray's own sections mark its missing clock-in", () => {
  const draft = d.newDraft(base(withStray, { focus: [id(3)] }));
  const [stray] = d.shown(draft);
  const found = d.sections(draft, stray);
  assert.equal(stray.kind, "stray");
  assert.equal(found.clockIn.missing, true);
  assert.equal(found.clockOut.missing, false);
  assert.deepEqual(ids(found.clockOut.punches), [id(3)]);
});

test("keeping one punch of a choice removes the others", () => {
  let draft = d.moveIn(d.newDraft(base(withStray, { focus: [id(1), id(2)] })), id(3), id(1));
  draft = d.keep(draft, d.sections(draft, d.shown(draft)[0]).clockOut, { id: id(3) });
  assert.deepEqual(d.recordsOf(draft), [{ action: "void", punchId: id(2), kind: null, at: null }]);
  const shift = d.shown(draft).find((e) => e.kind === "shift");
  assert.equal(shift.end, at(4, "17:02"));
});

test("keeping a choice at another time moves its first punch and removes the rest", () => {
  let draft = d.newDraft(base(doubleIn));
  draft = d.keep(draft, d.sections(draft, d.shown(draft)[0]).clockIn, { at: at(3, "07:00") });
  assert.deepEqual(d.recordsOf(draft), [
    { action: "amend", punchId: id(1), kind: null, at: at(3, "07:00") },
    { action: "void", punchId: id(2), kind: null, at: null },
  ]);
});

test("mark as correct writes the unchanged time", () => {
  const draft = d.confirm(d.newDraft(base(automatic)), id(2));
  assert.deepEqual(d.recordsOf(draft), [{ action: "amend", punchId: id(2), kind: null, at: at(3, "23:00") }]);
  assert.deepEqual(d.changes(draft, { tz: TZ, h24: false }), ["Marked the automatic clock-out at 11:00 PM as correct"]);
});

test("a clock-out changed to a break start joins two shifts", () => {
  const punches = [punch(1, "clock_in", 3, "07:00"), punch(2, "clock_out", 3, "11:00"), punch(3, "clock_in", 3, "11:30"), punch(4, "clock_out", 3, "15:30")];
  const before = d.newDraft(base(punches, { focus: [id(1), id(2)] }));
  const after = d.setKind(before, id(2), "break_start");
  const found = d.joinSplit(before, after);
  assert.equal(found.kind, "join");
  assert.deepEqual(
    found.entries.map((e) => e.key),
    [id(1)],
  );
  assert.deepEqual(
    d.shown(after).map((e) => e.punches.length),
    [4],
  );
});

test("a forgotten clock-out added splits a shift; moving it after is neither", () => {
  const punches = [punch(1, "clock_in", 2, "07:00"), punch(2, "clock_in", 3, "07:01"), punch(3, "clock_out", 3, "15:30")];
  const before = d.newDraft(base(punches));
  const after = d.add(before, "clock_out", at(2, "15:30"), id(9));
  const found = d.joinSplit(before, after);
  assert.equal(found.kind, "split");
  assert.deepEqual(
    found.entries.map((e) => e.key),
    [id(1), id(2)],
  );
  assert.equal(d.joinSplit(after, d.setTime(after, id(9), at(2, "15:35"))), null);
});

test("a lunch turned into a clock-out and a clock-in asks once, at the step that splits", () => {
  const punches = [
    punch(1, "clock_in", 3, "07:00"),
    punch(2, "lunch_start", 3, "11:00"),
    punch(3, "lunch_end", 3, "11:30"),
    punch(4, "clock_out", 3, "15:30"),
  ];
  const before = d.newDraft(base(punches));
  const half = d.setKind(before, id(2), "clock_out"); // the rest are strays for now: nothing split yet
  assert.equal(d.joinSplit(before, half), null);
  const after = d.setKind(half, id(3), "clock_in");
  const found = d.joinSplit(half, after);
  assert.equal(found.kind, "split");
  assert.deepEqual(
    found.entries.map((e) => e.key),
    [id(1), id(3)],
  );
  assert.equal(d.joinSplit(after, d.setTime(after, id(4), at(3, "15:45"))), null); // confirmed: later edits don't ask again
});

test("a punch someone else changed meanwhile is a conflict until a side is picked", () => {
  const b = base(doubleIn);
  const draft = d.setTime(d.newDraft(b), id(3), at(3, "15:45"));
  const theirs = { ...doubleIn[2], at: at(3, "16:00"), version: id(50), changedBy: "ed@example.com", changedAt: at(4, "09:00") };
  const rebased = d.rebase(draft, { ...b, token: "t2", punches: [doubleIn[0], doubleIn[1], theirs] });
  assert.deepEqual(
    rebased.conflicts.map((c) => [c.id, c.theirs.at, c.yours.at, c.by, c.at]),
    [[id(3), at(3, "16:00"), at(3, "15:45"), "ed@example.com", at(4, "09:00")]],
  );
  assert.deepEqual(rebased.outside, []);
  assert.ok(d.blockers(rebased.draft).includes("Pick theirs or yours for each punch marked in red."));
  const mine = d.resolve(rebased.draft, id(3), "mine");
  assert.deepEqual(d.clashes(mine), []);
  assert.deepEqual(d.recordsOf(mine), [{ action: "amend", punchId: id(3), kind: null, at: at(3, "15:45") }]);
  assert.equal(d.resolve(rebased.draft, id(3), "theirs").edits.size, 0);
});

test("a punch someone else removed meanwhile, kept as yours, is added back", () => {
  const b = base(doubleIn);
  const draft = d.setTime(d.newDraft(b), id(3), at(3, "15:45"));
  const rebased = d.rebase(draft, { ...b, token: "t2", punches: [doubleIn[0], doubleIn[1]] });
  assert.equal(rebased.conflicts[0].theirs, null);
  const mine = d.resolve(rebased.draft, id(3), "mine", id(9));
  assert.deepEqual(d.recordsOf(mine), [{ action: "insert", punchId: id(9), kind: "clock_out", at: at(3, "15:45") }]);
});

test("a change elsewhere in the shift is pointed out, not a conflict", () => {
  const draft = d.setTime(d.newDraft(base(doubleIn)), id(3), at(3, "15:45"));
  const late = [punch(7, "break_start", 3, "10:00"), punch(8, "break_end", 3, "10:15")];
  const moved = { ...doubleIn[1], at: at(3, "07:05"), version: id(51) };
  const rebased = d.rebase(draft, { ...base([doubleIn[0], moved, doubleIn[2], ...late]), token: "t2" });
  assert.deepEqual(rebased.conflicts, []);
  assert.deepEqual(rebased.outside, [
    "The clock-in at 7:04 AM was moved to 7:05 AM.",
    "A break start at 10:00 AM was added.",
    "A break end at 10:15 AM was added.",
  ]);
});

test("an added punch already saved is dropped from the draft", () => {
  const draft = d.add(d.newDraft(base(doubleIn)), "break_start", at(3, "10:00"), id(9));
  const saved = punch(9, "break_start", 3, "10:00", { addedOnPage: true, instanceId: "web" });
  const rebased = d.rebase(draft, { ...base([...doubleIn, saved]), token: "t2" });
  assert.equal(rebased.draft.edits.size, 0);
  assert.deepEqual(rebased.outside, []);
});

test("save is off, in plain words, until there's something it may save", () => {
  const twice = d.newDraft(base(doubleIn));
  assert.deepEqual(d.blockers(twice), ["Choose which clock-in to keep to save."]);
  const kept = d.keep(twice, d.sections(twice, d.shown(twice)[0]).clockIn, { id: id(1) });
  assert.deepEqual(d.blockers(kept), []);
  assert.deepEqual(d.blockers(kept, { connected: false }), [OFFLINE]);
  assert.deepEqual(d.blockers(kept, { readOnly: true }), ["This is your own shift. Another editor has to change it."]);
  assert.deepEqual(d.blockers(d.newDraft(base(plain))), ["Nothing to save yet."]);
  const ignored = d.newDraft(base([punch(1, "clock_in", 3, "07:00"), punch(2, "break_end", 3, "09:44"), punch(3, "clock_out", 3, "15:30")]));
  const still = d.setTime(ignored, id(3), at(3, "15:45"));
  assert.deepEqual(d.blockers(still), ["Fix the punches marked in red to save."]);
  assert.deepEqual(d.refused(still).punchIds, [id(2)]);
});

test("your changes, in plain words", () => {
  const punches = [punch(1, "clock_in", 3, "07:04"), punch(2, "clock_out", 3, "10:00"), punch(3, "clock_out", 3, "23:00"), punch(4, "lunch_start", 3, "12:00")];
  let draft = d.newDraft(base(punches));
  draft = d.setTime(draft, id(3), at(3, "17:05"));
  draft = d.setKind(draft, id(2), "break_start");
  draft = d.remove(draft, id(1));
  draft = d.add(draft, "break_end", at(3, "10:15"), id(9));
  draft = d.setTime(d.setKind(draft, id(4), "lunch_end"), id(4), at(3, "12:30"));
  assert.deepEqual(d.changes(draft, { tz: TZ, h24: false }), [
    "Removed the clock-in at 7:04 AM",
    "10:00 AM: Clock out to Break start",
    "Added a break end at 10:15 AM",
    "Lunch start at 12:00 PM to Lunch end at 12:30 PM",
    "Clock out: 11:00 PM to 5:05 PM",
  ]);
  assert.deepEqual(d.changes(draft, { tz: TZ, h24: true })[0], "Removed the clock-in at 07:04");
});

test("the records a save writes, for technical details", () => {
  let draft = d.newDraft(base(plain));
  draft = d.setTime(draft, id(4), at(3, "15:45"));
  draft = d.setKind(draft, id(2), "lunch_start");
  draft = d.remove(draft, id(3));
  draft = d.add(draft, "lunch_end", at(3, "10:30"), id(9));
  assert.deepEqual(d.writes(draft), [
    "correction (amend) on punch 00000004… · new_at 2026-11-03 20:45:00 UTC",
    "correction (amend) on punch 00000002… · new_kind lunch_start",
    "correction (void) on punch 00000003…",
    "punch (insert) 00000009… · kind lunch_end · at 2026-11-03 15:30:00 UTC · instance_id web",
  ]);
});

test("a punch's technical details: its id, clock, login and when the server got it", () => {
  const p = punch(1, "clock_in", 4, "08:15", { receivedAt: at(4, "08:15") + 3000 });
  assert.equal(d.punchDetail(p, { tz: TZ }), "punch 00000001… · clock main-office · written by timeclock_clocks · received Nov 4, 8:15:03 AM");
  assert.equal(d.punchDetail(p, { tz: TZ, h24: true }), "punch 00000001… · clock main-office · written by timeclock_clocks · received Nov 4, 08:15:03");
  const added = d.punches(d.add(d.newDraft(base([p])), "clock_out", at(4, "16:00"), id(9))).find((q) => q.id === id(9));
  assert.equal(d.punchDetail(added, { tz: TZ }), "punch 00000009… · new, written when you save");
});

test("the needs-fixing banner says what the hours do", () => {
  const banner = (punches, focus) => {
    const draft = d.newDraft(base(punches, focus === undefined ? {} : { focus }));
    return d.banner(d.shown(draft)[0], { tz: TZ, h24: false });
  };
  assert.equal(banner(doubleIn), "Two clock-ins, at 7:01 AM and 7:04 AM. The hours count from the first and ignore the second.");
  assert.equal(
    banner([...doubleIn, punch(4, "clock_in", 3, "07:10")]),
    "Three clock-ins, at 7:01 AM, 7:04 AM and 7:10 AM. The hours count from the first and ignore the rest.",
  );
  assert.equal(banner(withStray, [id(3)]), "A clock-out at 5:02 PM outside any shift. The hours ignore it.");
  assert.equal(
    banner([punch(1, "clock_in", 3, "07:00"), punch(2, "break_end", 3, "09:44"), punch(3, "lunch_end", 3, "13:02"), punch(4, "clock_out", 3, "15:30")]),
    "The hours ignore 2 punches in this shift: Break end at 9:44 AM, Lunch end at 1:02 PM.",
  );
  assert.equal(banner(plain), null);
});

test("a shift's span names its end's day when that's another day", () => {
  const span = (punches) => d.span(d.shown(d.newDraft(base(punches)))[0], { tz: TZ, h24: false });
  assert.equal(span(plain), "Tuesday, November 3, 7:00 AM to 3:30 PM");
  assert.equal(
    span([punch(1, "clock_in", 2, "08:00"), punch(2, "clock_out", 3, "15:30")]),
    "Monday, November 2, 8:00 AM to Tuesday, November 3, 3:30 PM",
  );
  assert.equal(span([punch(1, "clock_in", 6, "08:00")]), "Friday, November 6, 8:00 AM to working");
});

test("an added shift starts empty, on its day", () => {
  const draft = adding();
  const [entry] = d.shown(draft);
  const found = d.sections(draft, entry);
  assert.deepEqual([found.clockIn.missing, found.clockOut.missing, found.breaks, found.lunches, found.strays], [true, true, [], [], []]);
  assert.equal(d.placeTime(entry, "clock_out", { h: 16, m: 30 }, TZ), at(4, "16:30"));
  assert.deepEqual(d.changes(draft, { tz: TZ, h24: false }), []);
  assert.deepEqual(d.blockers(draft), ["Add a clock-in to save."]);
});

test("an added shift is its new punches, summed up in one line", () => {
  let draft = adding();
  draft = d.add(draft, "clock_out", at(4, "21:30"), id(9));
  const [lone] = d.shown(draft);
  assert.equal(d.sections(draft, lone).clockOut.punches[0].id, id(9));
  assert.deepEqual(d.blockers(draft), ["Add a clock-in to save."]);
  draft = d.add(draft, "clock_in", d.placeTime(lone, "clock_in", { h: 13, m: 0 }, TZ), id(3));
  draft = d.add(d.add(draft, "break_start", at(4, "15:00"), id(4)), "break_end", at(4, "15:15"), id(5));
  draft = d.add(d.add(draft, "lunch_start", at(4, "17:00"), id(6)), "lunch_end", at(4, "17:30"), id(7));
  const [entry] = d.shown(draft);
  const found = d.sections(draft, entry);
  assert.deepEqual([found.clockIn.punches[0].id, found.breaks.length, found.lunches.length], [id(3), 1, 1]);
  assert.deepEqual(d.changeList(draft, { tz: TZ, h24: false }), [{ id: null, text: "New shift: 1:00 PM to 9:30 PM, with 1 break and 1 lunch" }]);
  assert.deepEqual(d.blockers(draft), []);
  assert.equal(d.overlap(draft), null);
  assert.equal(d.joinSplit(adding(), draft), null);
});

test("an added shift left open has to start today, with nothing after it", () => {
  const needed = "Add the clock-out to save. Only a shift that started today, with nothing after it, can stay open.";
  const tuesday = d.add(adding(), "clock_in", at(3, "08:00")); // now is Friday the 6th
  assert.ok(d.blockers(tuesday).includes(needed));
  assert.ok(!d.blockers(d.add(tuesday, "clock_out", at(3, "16:00"))).includes(needed));
  const friday = (punches) => d.add(d.newDraft(base(punches, { focus: [], day: "2026-11-06" })), "clock_in", at(6, "08:00"));
  assert.deepEqual(d.blockers(friday([punch(1, "clock_in", 4, "07:55"), punch(2, "clock_out", 4, "12:10")])), []);
  assert.ok(d.blockers(friday([punch(1, "clock_in", 6, "09:00"), punch(2, "clock_out", 6, "10:00")])).includes(needed));
});

test("an added shift that overlaps one the employee has says which", () => {
  let draft = adding();
  draft = d.add(d.add(draft, "clock_in", at(4, "08:00"), id(3)), "clock_out", at(4, "16:30"), id(4));
  assert.deepEqual(d.overlap(draft), {
    text: "This overlaps Bob's shift on Wednesday, November 4, from 7:55 AM to 12:10 PM. Change the times, or open that shift instead.",
    open: `/shift?employee=2&punch=${id(1)}`,
  });
  assert.deepEqual(d.blockers(draft), ["Change the times so it doesn't overlap that shift."]);
  assert.equal(d.joinSplit(adding(), draft), null); // an overlap is refused, not joined
  assert.equal(d.changes(d.add(adding(), "clock_in", at(4, "13:00"), id(3)), { tz: TZ, h24: false })[0], "New shift: 1:00 PM, no clock-out");
});

test("typed digits: the last two are the minutes once there are three", () => {
  const read = [
    ["5", null, 5, 0],
    ["12", null, 12, 0],
    ["239", null, 2, 39],
    ["1705", null, 17, 5],
    ["0030", null, 0, 30],
    ["505", "PM", 17, 5],
    ["5", "PM", 17, 0],
    ["239", "AM", 2, 39],
    ["12", "AM", 0, 0],
    ["12", "PM", 12, 0],
    ["1230", "AM", 0, 30],
    ["1705", "AM", 17, 5], // already 24-hour, so AM or PM can't move it
    ["030", "PM", 0, 30],
  ];
  for (const [digits, half, h, m] of read) assert.deepEqual(d.readTime(digits, half), { h, m }, `${digits} ${half}`);
  for (const digits of ["", "24", "560", "2400", "12345", "5a", "5:05"]) assert.equal(d.readTime(digits, "PM"), null, digits);
});

test("a time typed without AM or PM takes the likelier one", () => {
  const [day] = d.shown(d.newDraft(base(plain))); // 7:00 AM to 3:30 PM
  assert.equal(d.guessHalf(day, "clock_out", { h: 4, m: 30 }, TZ), "PM");
  assert.equal(d.guessHalf(day, "break_start", { h: 10, m: 0 }, TZ), "AM");
  assert.equal(d.guessHalf(day, "lunch_start", { h: 12, m: 0 }, TZ), "PM");
  assert.equal(d.guessHalf(day, "clock_out", { h: 6, m: 59 }, TZ), "PM"); // the first 6:59 after the clock-in
  assert.equal(d.guessHalf(day, "clock_in", { h: 6, m: 55 }, TZ), "AM"); // a clock-in: the nearest
  const [evening] = d.shown(d.newDraft(base([punch(1, "clock_in", 3, "14:00"), punch(2, "clock_out", 3, "22:00")])));
  assert.equal(d.guessHalf(evening, "clock_out", { h: 11, m: 0 }, TZ), "PM");
  assert.equal(d.guessHalf(evening, "clock_out", { h: 1, m: 0 }, TZ), "AM");
  // A stray clock-out at 5:02 PM: a clock-in is the last before it, an end the first after.
  const [stray] = d.shown(d.newDraft(base(withStray, { focus: [id(3)] })));
  assert.equal(d.guessHalf(stray, "clock_in", { h: 8, m: 0 }, TZ), "AM");
  assert.equal(d.guessHalf(stray, "break_end", { h: 6, m: 0 }, TZ), "PM");
  // An added shift with nothing in it yet: 6 to 11 is the morning, 12 to 5 the afternoon.
  const [empty] = d.shown(adding());
  for (const [h, half] of [[6, "AM"], [11, "AM"], [12, "PM"], [1, "PM"], [5, "PM"]]) assert.equal(d.guessHalf(empty, "clock_in", { h, m: 0 }, TZ), half, h);
});

test("a typed time lands on the shift's day, or the next when it's before the clock-in", () => {
  const draft = d.newDraft(base([punch(1, "clock_in", 3, "22:00"), punch(2, "clock_out", 4, "06:00")]));
  const [overnight] = d.shown(draft);
  assert.equal(d.placeTime(overnight, "clock_out", { h: 6, m: 30 }, TZ), at(4, "06:30"));
  assert.equal(d.placeTime(overnight, "break_start", { h: 23, m: 15 }, TZ), at(3, "23:15"));
  assert.equal(d.placeTime(overnight, "clock_in", { h: 1, m: 0 }, TZ), at(3, "01:00"));
});

test("a draft survives a reload", () => {
  assert.equal(d.storageKey("ann@example.com", "production", 2, id(1)), `timeclock-draft:ann@example.com:production:2:${id(1)}`);
  let draft = d.newDraft(base(withStray, { focus: [id(1), id(2)] }));
  draft = d.setTime(d.moveIn(draft, id(3), id(1)), id(1), at(4, "06:55"));
  const back = d.restore(d.serialize(draft, { note: "Forgot to clock out", token: "t1" }));
  assert.equal(back.note, "Forgot to clock out");
  assert.equal(back.token, "t1");
  assert.deepEqual(d.recordsOf(back.draft), d.recordsOf(draft));
  assert.deepEqual([...back.draft.moved], [[id(3), id(1)]]);
  assert.deepEqual([...back.draft.focus], [id(1), id(2)]);
  assert.equal(d.restore("{nonsense"), null);
  assert.equal(d.restore(JSON.stringify({ v: 99 })), null);
});

test("break details show while breaks are on, or while the saved shift has a break punch", () => {
  const noBreak = [punch(1, "clock_in", 3, "07:00"), punch(4, "clock_out", 3, "15:30")];
  const off = (punches) => d.newDraft({ ...base(punches), breaksEnabled: false });
  const key = (draft) => d.shown(draft)[0].key;
  const on = d.newDraft({ ...base(noBreak), breaksEnabled: true });
  assert.equal(d.showsBreaks(on, key(on)), true);
  const without = off(noBreak);
  assert.equal(d.showsBreaks(without, key(without)), false);
  const withBreak = off(plain);
  assert.equal(d.showsBreaks(withBreak, key(withBreak)), true);
  // From the saved punches: removing the break in the draft keeps its details until it's saved.
  assert.equal(d.showsBreaks(d.remove(withBreak, id(2)), key(withBreak)), true);
  // A base saved before the field existed (localStorage) shows them; a shift being added has none saved.
  const old = d.newDraft(base(noBreak));
  assert.equal(d.showsBreaks(old, key(old)), true);
  assert.equal(d.showsBreaks(without, id(99)), false);
  // Without a key, the whole loaded week.
  assert.equal(d.showsBreaks(without), false);
  assert.equal(d.showsBreaks(withBreak), true);
});
