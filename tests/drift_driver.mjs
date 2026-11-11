// The drift test's JavaScript side (tests/test_web_drift.py): reads its cases as JSON on stdin, answers each with the
// editor's rules, and prints the answers as JSON. The shape of an answer is the Python side's `_answer`.
import * as rules from "../src/timeclock_backend/web/editor/rules.js";

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const cases = JSON.parse(Buffer.concat(chunks).toString("utf8"));

const entryJson = (e) => ({
  kind: e.kind,
  key: e.key,
  punches: e.punches.map((p) => p.id),
  ignored: [...e.ignored].sort(),
  start: e.start,
  end: e.end,
});

const answers = cases.map((c) => {
  const opts = { paidBreakMinutes: c.paidBreakMinutes, breakLimitMinutes: c.breakLimitMinutes, tz: c.tz, now: c.asOf };
  const found = rules.entries(c.punches, c.asOf);
  const base = Object.fromEntries(c.punches.map((p) => [p.id, p]));
  const clean = c.draft.filter((d) => !rules.conflicts(base, [d]).length);
  const records = rules.records(base, clean);
  const after = rules.applyRecords(c.punches, records);
  const refused = rules.refusal(c.punches, after, c.touched, c.asOf);
  const overlap = rules.overlapping(c.punches, c.span[0], c.span[1], c.asOf);
  const hours = found.map((e) => {
    const h = rules.entryHours(e, c.punches, opts);
    return h === null ? null : [h.worked, h.paidBreak, h.unpaid];
  });
  const week = rules.weekHours(c.punches, { ...opts, week: c.week });
  return {
    entries: found.map(entryJson),
    flags: found.map((e) => rules.flags(e)),
    problems: found.map((e) => rules.problem(e)),
    hours,
    week: [week.worked, week.paidBreak, week.unpaid],
    conflicts: rules.conflicts(base, c.draft),
    records,
    after: rules.entries(after, c.asOf).map(entryJson),
    refusal: refused,
    overlap: overlap === null ? null : overlap.key,
    durations: hours.map((h) => (h === null ? null : rules.hm(h[0]))),
    clocks: c.punches.map((p) => [rules.clockTime(p.at, c.tz, false), rules.clockTime(p.at, c.tz, true), rules.localDate(p.at, c.tz)]),
    walls: c.walls.map(([day, time]) => rules.zonedTime(day, time, c.tz)),
  };
});
process.stdout.write(JSON.stringify(answers));
