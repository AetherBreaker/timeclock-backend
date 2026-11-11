// The editor's hours (editor/rules.js's totalHours), under node:test; tests/test_web_js.py runs this file.
import assert from "node:assert/strict";
import { test } from "node:test";

import { totalHours, zonedTime } from "../../src/timeclock_backend/web/editor/rules.js";

const TZ = "America/New_York";
const MIN = 60000;
const at = (time) => zonedTime("2026-11-03", time, TZ);
let n = 0;
const punch = (kind, time) => ({ id: `${String(++n).padStart(8, "0")}-0000-4000-8000-000000000000`, kind, at: at(time) });
const shift = (...pairs) => pairs.map(([kind, time]) => punch(kind, time));
const opts = (paidBreakMinutes, breakLimitMinutes, start = at("00:00"), end = at("23:59")) => ({
  paidBreakMinutes,
  breakLimitMinutes,
  tz: TZ,
  start,
  end,
  now: at("23:59"),
});

test("paid hours, lunch, unpaid breaks and each break's paid part, with why the rest isn't", () => {
  const punches = shift(
    ["clock_in", "07:00"],
    ["break_start", "09:00"],
    ["break_end", "09:20"], // 20 minutes: the 15-minute limit pays 15
    ["lunch_start", "12:00"],
    ["lunch_end", "12:30"],
    ["break_start", "13:00"],
    ["break_end", "13:12"], // 12 minutes, all paid: 15 of the day's 30 left
    ["break_start", "14:00"],
    ["break_end", "14:10"], // 10 minutes, 3 left of the day's 30
    ["clock_out", "15:30"],
  );
  const h = totalHours(punches, opts(30, 15));
  assert.deepEqual(
    { worked: h.worked, paidBreak: h.paidBreak, paid: h.paid, unpaidBreak: h.unpaidBreak, lunch: h.lunch },
    { worked: 438 * MIN, paidBreak: 30 * MIN, paid: 468 * MIN, unpaidBreak: 12 * MIN, lunch: 30 * MIN },
  );
  assert.deepEqual(h.breaks, [
    { start: at("09:00"), end: at("09:20"), paid: 15 * MIN, unpaid: 5 * MIN, cause: "limit" },
    { start: at("13:00"), end: at("13:12"), paid: 12 * MIN, unpaid: 0, cause: null },
    { start: at("14:00"), end: at("14:10"), paid: 3 * MIN, unpaid: 7 * MIN, cause: "allowance" },
  ]);
});

test("a break the limit and the day's allowance cut alike is over the limit", () => {
  const punches = shift(
    ["clock_in", "07:00"],
    ["break_start", "09:00"],
    ["break_end", "09:15"],
    ["break_start", "13:00"],
    ["break_end", "13:20"],
    ["clock_out", "15:00"],
  );
  assert.deepEqual(totalHours(punches, opts(30, 15)).breaks[1], { start: at("13:00"), end: at("13:20"), paid: 15 * MIN, unpaid: 5 * MIN, cause: "limit" });
});

test("with no limit, only the day's allowance leaves a break unpaid", () => {
  const punches = shift(["clock_in", "07:00"], ["break_start", "09:00"], ["break_end", "09:40"], ["clock_out", "15:00"]);
  const h = totalHours(punches, opts(30, 0));
  assert.deepEqual([h.paidBreak, h.unpaidBreak, h.breaks[0].cause], [30 * MIN, 10 * MIN, "allowance"]);
});

test("a range edge cuts the totals but not a break's own split", () => {
  const punches = shift(["clock_in", "07:00"], ["break_start", "09:00"], ["break_end", "09:20"], ["clock_out", "15:00"]);
  const h = totalHours(punches, opts(30, 15, at("07:00"), at("09:10")));
  assert.deepEqual([h.worked, h.paidBreak, h.unpaidBreak, h.lunch], [120 * MIN, 10 * MIN, 0, 0]);
  assert.deepEqual([h.breaks[0].paid, h.breaks[0].unpaid], [15 * MIN, 5 * MIN]);
});

test("an open break runs to now", () => {
  const punches = shift(["clock_in", "07:00"], ["break_start", "23:30"]);
  assert.deepEqual(totalHours(punches, opts(30, 15)).breaks, [{ start: at("23:30"), end: null, paid: 15 * MIN, unpaid: 14 * MIN, cause: "limit" }]);
});
