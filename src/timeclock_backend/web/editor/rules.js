// The shift rules in JavaScript (spec 2026-11-05 §2): a copy of web/rules.py and of the schema's build_timeline and
// total_hours, so the editor previews hours while it drafts, connected or not. Python decides what's saved; the drift
// test (tests/test_web_drift.py) runs both over the same cases, so a change on either side goes to the other.
//
// Times are epoch milliseconds, ids lowercase UUID strings (their string order is the UUIDs' order), and a punch is
// web/rules.py's PagePunch as JSON: {id, kind, at, automatic, originalKind, originalAt, version, addedOnPage,
// instanceId, dbLogin, receivedAt, changedBy, changedAt}.

export const PENDING = "00000000-0000-0000-0000-000000000000";
export const PAGE_INSTANCE_ID = "web";

const VALID_NEXT = {
  off: ["clock_in"],
  working: ["clock_out", "break_start", "lunch_start"],
  on_break: ["break_end", "clock_out"],
  at_lunch: ["lunch_end", "clock_out"],
};
const NEXT_STATE = {
  clock_in: "working",
  clock_out: "off",
  break_start: "on_break",
  break_end: "working",
  lunch_start: "at_lunch",
  lunch_end: "working",
};

// The walk's order: time, then id.
export const order = (a, b) => a.at - b.at || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

// The schema's build_timeline over punches already corrected.
export function buildTimeline(punches, asOf) {
  const events = punches.filter((p) => p.at <= asOf).sort(order);
  let state = "off";
  const shifts = [];
  const anomalies = [];
  let lastEventAt = null;
  let clockIn = null;
  let breaks = [];
  let lunches = [];
  let intervalStart = null;
  for (const p of events) {
    if (!VALID_NEXT[state].includes(p.kind)) {
      anomalies.push({ punch: p, state });
      continue;
    }
    lastEventAt = p.at;
    // Every punch valid from a break or lunch ends it: its own end punch, or a clock-out ending it implicitly.
    if (state === "on_break" || state === "at_lunch") (state === "on_break" ? breaks : lunches).push({ start: intervalStart, end: p.at });
    if (p.kind === "clock_in") {
      clockIn = p;
      breaks = [];
      lunches = [];
    } else if (p.kind === "break_start" || p.kind === "lunch_start") {
      intervalStart = p.at;
    } else if (p.kind === "clock_out") {
      shifts.push({ clockIn, clockOut: p, breaks, lunches });
    }
    state = NEXT_STATE[p.kind];
  }
  if (state !== "off") {
    if (state !== "working") (state === "on_break" ? breaks : lunches).push({ start: intervalStart, end: null });
    shifts.push({ clockIn, clockOut: null, breaks, lunches });
  }
  return { state, shifts, anomalies, lastEventAt };
}

// rules.py's entries: shifts with every punch in their span, and runs of stray punches.
export function entries(punches, asOf) {
  const ordered = punches.filter((p) => p.at <= asOf).sort(order);
  const timeline = buildTimeline(ordered, asOf);
  const ignored = new Set(timeline.anomalies.map((a) => a.punch.id));
  const groups = [];
  let span = 0;
  for (const p of ordered) {
    const shifts = timeline.shifts;
    while (span < shifts.length && shifts[span].clockOut !== null && order(shifts[span].clockOut, p) < 0) span++;
    const shift = span < shifts.length && order(p, shifts[span].clockIn) >= 0 ? shifts[span] : null;
    const last = groups[groups.length - 1];
    if (last !== undefined && last.shift === shift) last.punches.push(p);
    else groups.push({ shift, punches: [p] });
  }
  return groups.map(({ shift, punches: group }) => ({
    kind: shift === null ? "stray" : "shift",
    punches: group,
    ignored: new Set(group.filter((p) => ignored.has(p.id)).map((p) => p.id)),
    shift,
    key: group[0].id,
    start: group[0].at,
    end: shift === null ? group[group.length - 1].at : shift.clockOut === null ? null : shift.clockOut.at,
    clockOut: shift === null ? null : shift.clockOut,
  }));
}

// Changed by a person (shift_summary's `corrected`); the automatic clock-out shares the owner's postgres login.
export const byHand = (p) => p.version !== null || p.addedOnPage || (p.dbLogin === "postgres" && !p.automatic);

export function flags(entry) {
  const found = [];
  if (entry.kind === "stray" || entry.ignored.size > 0) found.push("needs_fixing");
  const automatic = entry.clockOut !== null && entry.clockOut.automatic ? entry.clockOut : null;
  if (automatic !== null) found.push(automatic.version === null ? "automatic" : "automatic_fixed");
  if (entry.punches.some((p) => p !== automatic && byHand(p))) found.push("changed");
  if (entry.kind === "shift" && entry.end === null) found.push("working");
  return found;
}

export function problem(entry) {
  if (entry.kind === "stray") return "stray";
  const kinds = new Set(entry.punches.filter((p) => entry.ignored.has(p.id)).map((p) => p.kind));
  if (kinds.size === 0) return null;
  return kinds.size === 1 ? `extra:${[...kinds][0]}` : "mixed";
}

const formatters = new Map();

// The wall clock in `tz` at `ms`, through Intl so the browser's own zone never matters.
export function localParts(ms, tz) {
  let format = formatters.get(tz);
  if (format === undefined) {
    format = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      weekday: "short",
    });
    formatters.set(tz, format);
  }
  const parts = {};
  for (const { type, value } of format.formatToParts(ms)) parts[type] = value;
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
    second: Number(parts.second),
    weekday: parts.weekday,
  };
}

const pad = (n) => String(n).padStart(2, "0");

export function localDate(ms, tz) {
  const p = localParts(ms, tz);
  return `${p.year}-${pad(p.month)}-${pad(p.day)}`;
}

// text.py's hm: h:mm, seconds dropped.
export function hm(ms) {
  const minutes = Math.floor(ms / 60000);
  return `${Math.floor(minutes / 60)}:${pad(minutes % 60)}`;
}

// text.py's clock_time: "6:55 AM", or "06:55" in 24-hour time.
export function clockTime(ms, tz, h24) {
  const p = localParts(ms, tz);
  if (h24) return `${pad(p.hour)}:${pad(p.minute)}`;
  return `${p.hour % 12 || 12}:${pad(p.minute)} ${p.hour < 12 ? "AM" : "PM"}`;
}

const offsetAt = (ms, tz) => {
  const p = localParts(ms, tz);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(ms / 1000) * 1000;
};

// The instant a wall time ("YYYY-MM-DD", "HH:MM") names in `tz`, as Python's fold=0 gives it: in a DST gap or overlap,
// the offset from before the change.
export function zonedTime(day, time, tz) {
  const [y, m, d] = day.split("-").map(Number);
  const [h, mi] = time.split(":").map(Number);
  const wall = Date.UTC(y, m - 1, d, h, mi);
  const earlier = offsetAt(wall - 86400000, tz);
  const later = offsetAt(wall + 86400000, tz);
  if (earlier === later) return wall - earlier;
  const first = wall - earlier;
  const second = wall - later;
  if (first + offsetAt(first, tz) === wall) return first;
  return second + offsetAt(second, tz) === wall ? second : first;
}

// The schema's total_hours: worked (less every break and lunch), paid break (each break up to the per-break limit, and
// those paid parts up to the local day's allowance, earliest first) and unpaid, inside [start, end); an open shift,
// break or lunch runs to `now`. A limit of 0 or none is off.
export function totalHours(punches, { paidBreakMinutes, breakLimitMinutes, tz, start, end, now }) {
  const allowance = paidBreakMinutes * 60000;
  const limit = breakLimitMinutes ? breakLimitMinutes * 60000 : Infinity;
  const left = new Map();
  let worked = 0;
  let paidBreak = 0;
  let unpaid = 0;
  for (const shift of buildTimeline(punches, now).shifts) {
    const lo = Math.max(shift.clockIn.at, start);
    const hi = Math.min(shift.clockOut === null ? now : shift.clockOut.at, end);
    const counted = (a, b) => Math.max(Math.min(b, hi) - Math.max(a, lo), 0);
    worked += counted(lo, hi);
    for (const lunch of shift.lunches) {
      const time = counted(lunch.start, lunch.end === null ? now : lunch.end);
      worked -= time;
      unpaid += time;
    }
    // Charged even when the shift lies outside the range, so a later break that day sees the allowance already used.
    for (const brk of shift.breaks) {
      const brkEnd = brk.end === null ? now : brk.end;
      const day = localDate(brk.start, tz);
      const have = left.has(day) ? left.get(day) : allowance;
      const paidLength = Math.min(have, brkEnd - brk.start, limit);
      left.set(day, have - paidLength);
      const paidPart = counted(brk.start, brk.start + paidLength);
      const unpaidPart = counted(brk.start + paidLength, brkEnd);
      worked -= paidPart + unpaidPart;
      paidBreak += paidPart;
      unpaid += unpaidPart;
    }
  }
  return { worked, paidBreak, unpaid };
}

export function entryHours(entry, punches, opts) {
  if (entry.kind === "stray") return null;
  return totalHours(punches, { ...opts, start: entry.start, end: entry.end === null ? opts.now : entry.end });
}

export function weekHours(punches, opts) {
  return totalHours(punches, { ...opts, start: opts.week[0], end: opts.week[1] });
}

// `base` is an object of punches by id; a draft punch is {id, kind, at, removed, confirmed, version, new}.
export function conflicts(base, draft) {
  return draft.filter((d) => !d.new && (!Object.hasOwn(base, d.id) || base[d.id].version !== d.version)).map((d) => d.id);
}

// An amend names what differs from the punch's original: the database applies the newest correction alone, over it.
export function records(base, draft) {
  const found = [];
  for (const d of draft) {
    if (d.new) {
      if (!d.removed) found.push({ action: "insert", punchId: d.id, kind: d.kind, at: d.at });
      continue;
    }
    const was = base[d.id];
    if (d.removed) found.push({ action: "void", punchId: d.id, kind: null, at: null });
    else if (d.kind !== was.kind || d.at !== was.at || d.confirmed) {
      const kind = d.kind !== was.originalKind ? d.kind : null;
      const at = d.at !== was.originalAt ? d.at : null;
      found.push({ action: "amend", punchId: d.id, kind, at: kind === null && at === null ? was.originalAt : at });
    }
  }
  return found;
}

export function applyRecords(base, records) {
  const byId = new Map(base.map((p) => [p.id, p]));
  for (const r of records) {
    if (r.action === "void") byId.delete(r.punchId);
    else if (r.action === "amend") {
      const was = byId.get(r.punchId);
      const kind = r.kind === null ? was.originalKind : r.kind;
      byId.set(r.punchId, { ...was, kind, at: r.at === null ? was.originalAt : r.at, version: PENDING });
    } else {
      byId.set(r.punchId, {
        id: r.punchId,
        kind: r.kind,
        at: r.at,
        automatic: false,
        originalKind: r.kind,
        originalAt: r.at,
        version: null,
        addedOnPage: true,
        instanceId: PAGE_INSTANCE_ID,
        dbLogin: null,
        receivedAt: null,
        changedBy: null,
        changedAt: null,
      });
    }
  }
  return [...byId.values()].sort(order);
}

// rules.py's refusal: a punch the hours counted that they'd now ignore, then an entry holding a touched punch that
// still holds an ignored one.
export function refusal(before, after, touched, asOf) {
  const touchedIds = new Set(touched);
  const beforeEntries = entries(before, asOf);
  const wasIgnored = new Set(beforeEntries.flatMap((e) => [...e.ignored]));
  const counted = new Set(beforeEntries.flatMap((e) => e.punches.map((p) => p.id)).filter((id) => !wasIgnored.has(id)));
  const afterEntries = entries(after, asOf);
  for (const e of afterEntries) {
    const newly = e.punches.filter((p) => e.ignored.has(p.id) && counted.has(p.id)).map((p) => p.id);
    if (newly.length > 0) return { kind: "newly_ignored", punchIds: newly, entryKey: e.key };
  }
  for (const e of afterEntries) {
    if (e.ignored.size > 0 && e.punches.some((p) => touchedIds.has(p.id))) {
      return { kind: "still_ignored", punchIds: e.punches.filter((p) => e.ignored.has(p.id)).map((p) => p.id), entryKey: e.key };
    }
  }
  return null;
}

export function overlapping(punches, start, end, asOf) {
  for (const e of entries(punches, asOf)) {
    if (e.kind === "shift" && e.start <= end && (e.end === null ? asOf : e.end) >= start) return e;
  }
  return null;
}
