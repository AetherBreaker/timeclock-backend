// The shift editor's draft (spec 2026-11-05 §6, §8): what the user has changed, kept apart from the punches it was made
// against (`base`, the page's JSON), and everything the editor shows of it. Pure: no DOM, no Vue, so tests/js runs it.
//
// A draft is {base, edits, focus, moved, resolved}. `edits` maps a punch id to {kind, at, removed, confirmed, new,
// version}: what the user wants for that punch, and the version it was edited from (null for a new punch). `focus` is
// the opened entry's punch ids, `moved` maps a stray entry's key to the shift it was moved into, `resolved` the side
// picked for each conflict. Every change returns a new draft.
//
// Shifts are worked out over every punch, later than now or not (`ALL`): a time typed past now stays where it was put.
// The hours count up to now, as the server's do.
import { PAGE_INSTANCE_ID, PENDING, clockTime, entries, localDate, localParts, order, records, refusal, zonedTime } from "./rules.js";

const ALL = Infinity;
const LABEL = {
  clock_in: "Clock in",
  clock_out: "Clock out",
  break_start: "Break start",
  break_end: "Break end",
  lunch_start: "Lunch start",
  lunch_end: "Lunch end",
};
// A kind mid-sentence ("the clock-in at 7:04 AM").
const WORD = {
  clock_in: "clock-in",
  clock_out: "clock-out",
  break_start: "break start",
  break_end: "break end",
  lunch_start: "lunch start",
  lunch_end: "lunch end",
};
const KINDS = Object.keys(LABEL);
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const NUMBERS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"];
const OFFLINE = "The connection is interrupted. You can keep editing, and save your changes as soon as it's back.";
const STORED_VERSION = 1;

const byId = (draft) => new Map(draft.base.punches.map((p) => [p.id, p]));
const capital = (s) => s[0].toUpperCase() + s.slice(1);
const article = (word) => `${/^[aeiou]/.test(word) ? "an" : "a"} ${word}`;
const listed = (items) => (items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`);

export function newDraft(base, { focus = base.focus, moved = [] } = {}) {
  return { base, edits: new Map(), focus: new Set(focus), moved: new Map(moved), resolved: new Map() };
}

// One punch's edit, merged with `change`; an edit that's back to the punch as it stands goes, so it's no longer touched.
function edited(draft, id, change) {
  const was = byId(draft).get(id);
  const current = draft.edits.get(id) ?? (was && { kind: was.kind, at: was.at, removed: false, confirmed: false, new: false, version: was.version });
  if (current === undefined) return draft;
  const edit = { ...current, ...change };
  const edits = new Map(draft.edits);
  if (!edit.new && !edit.removed && !edit.confirmed && edit.kind === was?.kind && edit.at === was?.at) edits.delete(id);
  else edits.set(id, edit);
  return { ...draft, edits };
}

export const setTime = (draft, id, at) => edited(draft, id, { at });
export const setKind = (draft, id, kind) => edited(draft, id, { kind });
export const confirm = (draft, id) => edited(draft, id, { confirmed: true });

export function remove(draft, id) {
  if (!draft.edits.get(id)?.new) return edited(draft, id, { removed: true });
  const edits = new Map(draft.edits);
  edits.delete(id);
  return { ...draft, edits };
}

export function add(draft, kind, at, id = crypto.randomUUID()) {
  return { ...draft, edits: new Map(draft.edits).set(id, { kind, at, removed: false, confirmed: false, new: true, version: null }) };
}

export function undo(draft, id) {
  const edits = new Map(draft.edits);
  const resolved = new Map(draft.resolved);
  edits.delete(id);
  resolved.delete(id);
  return { ...draft, edits, resolved };
}

// A choice (spec §6.5): keep one punch of the slot, or its first at another time; the rest are removed.
export function keep(draft, slot, choice) {
  const kept = "id" in choice ? choice.id : slot.punches[0].id;
  let next = "id" in choice ? draft : setTime(draft, kept, choice.at);
  for (const p of slot.punches) if (p.id !== kept) next = remove(next, p.id);
  return next;
}

// A stray into a shift that day (spec §6.8), which the editor then shows too.
export function moveIn(draft, strayKey, shiftKey) {
  const shift = entries(punches(draft), ALL).find((e) => e.key === shiftKey);
  const focus = new Set([...draft.focus, ...(shift?.punches ?? []).map((p) => p.id)]);
  return { ...draft, focus, moved: new Map(draft.moved).set(strayKey, shiftKey) };
}

// The punches as the draft has them, by (at, id): edited ones carry PENDING, as rules.applyRecords marks them.
export function punches(draft) {
  const found = [];
  for (const p of draft.base.punches) {
    const edit = draft.edits.get(p.id);
    if (edit === undefined) found.push(p);
    else if (!edit.removed) found.push({ ...p, kind: edit.kind, at: edit.at, version: PENDING });
  }
  for (const [id, edit] of draft.edits) {
    if (!edit.new) continue;
    found.push({
      id,
      kind: edit.kind,
      at: edit.at,
      automatic: false,
      originalKind: edit.kind,
      originalAt: edit.at,
      version: null,
      addedOnPage: true,
      instanceId: PAGE_INSTANCE_ID,
      dbLogin: null,
      receivedAt: null,
      changedBy: null,
      changedAt: null,
    });
  }
  return found.sort(order);
}

const touched = (draft) => new Set([...draft.focus, ...draft.edits.keys()]);

// The entries the editor shows: those holding an opened or touched punch, and the strays moved into one of them.
export function shown(draft) {
  const all = entries(punches(draft), ALL);
  const ids = touched(draft);
  const picked = all.filter((e) => e.punches.some((p) => ids.has(p.id)));
  const shifts = new Set(picked.filter((e) => e.kind === "shift").map((e) => e.key));
  const moved = all.filter((e) => e.kind === "stray" && !picked.includes(e) && shifts.has(draft.moved.get(e.key)));
  return [...picked, ...moved].sort((a, b) => a.start - b.start);
}

const slot = () => ({ punches: [], missing: true });

// The editor's fields for one entry (spec §6.5, §6.8): each slot holds the punches competing for it, a choice when there
// are several. In a shift, every clock-in shares Clock in, an ignored start during its break or lunch shares that Start,
// and an ignored end straight after a valid one shares that End; anything else ignored is in `others`. A stray's
// punches, and a moved-in stray's, go to their kinds' slots, an end closing a start of its own run.
export function sections(draft, entry) {
  const found = { clockIn: slot(), clockOut: slot(), breaks: [], lunches: [], others: [], strays: [] };
  const pairs = { break: found.breaks, lunch: found.lunches };
  const placeRun = (run) => {
    const open = {};
    for (const p of run) {
      const [what, side] = p.kind.split("_");
      if (p.kind === "clock_in") found.clockIn.punches.push(p);
      else if (p.kind === "clock_out") found.clockOut.punches.push(p);
      else if (side === "start") pairs[what].push((open[what] = { start: { punches: [p] }, end: slot() }));
      else if (open[what] === undefined) pairs[what].push({ start: slot(), end: { punches: [p] } });
      else {
        open[what].end.punches.push(p);
        delete open[what];
      }
    }
  };
  const all = entries(punches(draft), ALL);
  if (entry.kind === "stray") placeRun(entry.punches);
  else {
    let open = null; // the break or lunch under way: {kind, pair}
    let closed = null; // the one the last punch ended
    for (const p of entry.punches) {
      const ignored = entry.ignored.has(p.id);
      if (ignored && closed !== null && p.kind === closed.kind) {
        closed.pair.end.punches.push(p);
        continue;
      }
      closed = null;
      if (p.kind === "clock_in") found.clockIn.punches.push(p);
      else if (ignored && open !== null && p.kind === open.kind) open.pair.start.punches.push(p);
      else if (ignored) found.others.push(p);
      else if (p.kind === "clock_out") {
        found.clockOut.punches.push(p);
        open = null;
      } else if (p.kind.endsWith("_start")) {
        const pair = { start: { punches: [p] }, end: slot() };
        pairs[p.kind.split("_")[0]].push(pair);
        open = { kind: p.kind, pair };
      } else {
        open.pair.end.punches.push(p); // valid, so it ends the open one
        closed = { kind: p.kind, pair: open.pair };
        open = null;
      }
    }
    const day = localDate(entry.start, draft.base.tz);
    for (const e of all) {
      if (e.kind !== "stray") continue;
      if (draft.moved.get(e.key) === entry.key) placeRun(e.punches);
      else if (!draft.moved.has(e.key) && localDate(e.start, draft.base.tz) === day) found.strays.push(e);
    }
  }
  const settle = (s) => {
    s.punches.sort(order);
    s.missing = s.punches.length === 0;
  };
  settle(found.clockIn);
  settle(found.clockOut);
  for (const list of [found.breaks, found.lunches]) {
    for (const pair of list) {
      settle(pair.start);
      settle(pair.end);
    }
    list.sort((a, b) => (a.start.punches[0] ?? a.end.punches[0]).at - (b.start.punches[0] ?? b.end.punches[0]).at);
  }
  return found;
}

const slotsOf = (found) => [found.clockIn, found.clockOut, ...[...found.breaks, ...found.lunches].flatMap((p) => [p.start, p.end])];

// A join (an after shift holding punches of several before shifts) or a split (a before shift's punches across several
// after shifts), which the editor confirms before applying (spec §6.7). Strays don't count: keeping one joins nothing.
export function joinSplit(before, after) {
  const shiftsOf = (draft) => entries(punches(draft), ALL).filter((e) => e.kind === "shift");
  const where = (list) => new Map(list.flatMap((e, i) => e.punches.map((p) => [p.id, i])));
  const was = shiftsOf(before);
  const now = shiftsOf(after);
  const wasAt = where(was);
  const nowAt = where(now);
  const keys = new Set(shown(after).map((e) => e.key));
  for (const e of now) {
    const from = new Set(e.punches.filter((p) => wasAt.has(p.id)).map((p) => wasAt.get(p.id)));
    if (from.size > 1 && keys.has(e.key)) return { kind: "join", entries: [e] };
  }
  for (const e of was) {
    const to = [...new Set(e.punches.filter((p) => nowAt.has(p.id)).map((p) => nowAt.get(p.id)))].sort((a, b) => a - b);
    if (to.length > 1) return { kind: "split", entries: to.map((i) => now[i]) };
  }
  return null;
}

// Touched punches someone else changed or removed since they were edited (spec §6.1), until a side is picked.
export function clashes(draft) {
  const current = byId(draft);
  const found = [];
  for (const [id, edit] of draft.edits) {
    if (edit.new) continue;
    const theirs = current.get(id) ?? null;
    if (theirs !== null && theirs.version === edit.version) continue;
    found.push({ id, theirs, yours: edit, by: theirs?.changedBy ?? null, at: theirs?.changedAt ?? null });
  }
  return found;
}

// "mine" keeps the edit over their version (re-added under `newId` when they removed the punch); "theirs" drops it.
export function resolve(draft, id, choice, newId = crypto.randomUUID()) {
  const resolved = new Map(draft.resolved).set(id, choice);
  if (choice === "theirs") return { ...undo(draft, id), resolved };
  const edit = draft.edits.get(id);
  const theirs = byId(draft).get(id);
  const edits = new Map(draft.edits);
  if (theirs !== undefined) return { ...draft, edits: edits.set(id, { ...edit, version: theirs.version }), resolved };
  edits.delete(id);
  if (!edit.removed) edits.set(newId, { ...edit, new: true, version: null, confirmed: false });
  return { ...draft, edits, focus: new Set([...draft.focus, newId]), resolved };
}

// The draft over a newer base: inserts already saved go, conflicts are listed, and other changes to what the editor
// shows are put in plain words (spec §6.1).
export function rebase(draft, newBase, { h24 = false } = {}) {
  const before = byId(draft);
  const after = new Map(newBase.punches.map((p) => [p.id, p]));
  const edits = new Map();
  const saved = new Set();
  for (const [id, edit] of draft.edits) {
    const theirs = after.get(id);
    if (edit.new && theirs !== undefined) saved.add(id);
    else if (!edit.new && theirs === undefined && edit.removed) continue; // removed on both sides
    else if (!edit.new && theirs !== undefined && !edit.removed && theirs.kind === edit.kind && theirs.at === edit.at) {
      edits.set(id, { ...edit, version: theirs.version }); // they made the same change
    } else edits.set(id, edit);
  }
  const next = { ...draft, base: newBase, edits };
  const tz = newBase.tz;
  const time = (ms) => clockTime(ms, tz, h24);
  const relevant = new Set([...shown(draft), ...shown(next)].flatMap((e) => e.punches.map((p) => p.id)));
  const outside = [];
  for (const id of relevant) {
    if (draft.edits.has(id) || saved.has(id)) continue;
    const was = before.get(id);
    const now = after.get(id);
    if (was === undefined && now !== undefined) outside.push([now.at, `${capital(article(WORD[now.kind]))} at ${time(now.at)} was added.`]);
    else if (was !== undefined && now === undefined) outside.push([was.at, `The ${WORD[was.kind]} at ${time(was.at)} was removed.`]);
    else if (was === undefined || now === undefined || was.version === now.version) continue;
    else if (was.kind !== now.kind && was.at !== now.at) {
      outside.push([was.at, `The ${WORD[was.kind]} at ${time(was.at)} was changed to ${article(WORD[now.kind])} at ${time(now.at)}.`]);
    } else if (was.at !== now.at) outside.push([was.at, `The ${WORD[was.kind]} at ${time(was.at)} was moved to ${time(now.at)}.`]);
    else if (was.kind !== now.kind) outside.push([was.at, `The ${WORD[was.kind]} at ${time(was.at)} was changed to ${article(WORD[now.kind])}.`]);
    else outside.push([was.at, `The ${WORD[was.kind]} at ${time(was.at)} was marked as correct.`]);
  }
  outside.sort((a, b) => a[0] - b[0]);
  return { draft: next, conflicts: clashes(next), outside: outside.map(([, line]) => line) };
}

// The draft's punches as the save payload carries them (rules.py's DraftPunch).
export function draftPunches(draft) {
  return [...draft.edits].map(([id, e]) => ({ id, kind: e.kind, at: e.at, removed: e.removed, confirmed: e.confirmed, version: e.version, new: e.new }));
}

// What a save would write; a conflict's edit waits for its side to be picked.
export function recordsOf(draft) {
  const base = Object.fromEntries(draft.base.punches.map((p) => [p.id, p]));
  const clashing = new Set(clashes(draft).map((c) => c.id));
  return records(
    base,
    draftPunches(draft).filter((d) => !clashing.has(d.id)),
  );
}

// Why the server would refuse (spec §6.4): a punch the hours would newly ignore, or one still ignored in a touched entry.
export const refused = (draft) => refusal(draft.base.punches, punches(draft), [...touched(draft)], ALL);

// Why Save is off, in plain words; none means it's on.
export function blockers(draft, { connected = true, readOnly = false } = {}) {
  if (readOnly) return ["This is your own shift. Another editor has to change it."];
  const found = [];
  if (clashes(draft).length > 0) found.push("Pick theirs or yours for each punch marked in red.");
  const choices = new Set();
  for (const e of shown(draft)) {
    if (e.kind === "stray" && draft.moved.has(e.key)) continue; // drawn inside its shift
    for (const s of slotsOf(sections(draft, e))) if (s.punches.length > 1) choices.add(s.punches[0].kind);
  }
  for (const kind of KINDS) if (choices.has(kind)) found.push(`Choose which ${WORD[kind]} to keep to save.`);
  if (choices.size === 0 && refused(draft) !== null) found.push("Fix the punches marked in red to save.");
  if (found.length === 0 && recordsOf(draft).length === 0) found.push("Nothing to save yet.");
  if (!connected) found.push(OFFLINE);
  return found;
}

// "Your changes" (spec §8), by where each punch was: [{id, text}], the id the one Undo puts back.
export function changeList(draft, { tz, h24 }) {
  const time = (ms) => clockTime(ms, tz, h24);
  const current = byId(draft);
  const lines = [];
  for (const [id, e] of draft.edits) {
    const was = current.get(id);
    if (e.new) {
      if (!e.removed) lines.push([e.at, `Added ${article(WORD[e.kind])} at ${time(e.at)}`, id]);
    } else if (was === undefined) continue; // a conflict, which says so itself
    else if (e.removed) lines.push([was.at, `Removed the ${WORD[was.kind]} at ${time(was.at)}`, id]);
    else if (e.kind !== was.kind && e.at !== was.at) lines.push([was.at, `${LABEL[was.kind]} at ${time(was.at)} to ${LABEL[e.kind]} at ${time(e.at)}`, id]);
    else if (e.at !== was.at) lines.push([was.at, `${LABEL[was.kind]}: ${time(was.at)} to ${time(e.at)}`, id]);
    else if (e.kind !== was.kind) lines.push([was.at, `${time(was.at)}: ${LABEL[was.kind]} to ${LABEL[e.kind]}`, id]);
    else if (e.confirmed) lines.push([was.at, `Marked the ${was.automatic ? "automatic " : ""}${WORD[was.kind]} at ${time(was.at)} as correct`, id]);
  }
  return lines.sort((a, b) => a[0] - b[0]).map(([, text, id]) => ({ id, text }));
}

export const changes = (draft, opts) => changeList(draft, opts).map((c) => c.text);

// The same, as the rows a save writes, for technical details (spec §11).
export function writes(draft) {
  const utc = (ms) => `${new Date(ms).toISOString().replace("T", " ").slice(0, 19)} UTC`;
  const short = (id) => `${id.slice(0, 8)}…`;
  return recordsOf(draft).map((r) => {
    if (r.action === "insert") return `punch (insert) ${short(r.punchId)} · kind ${r.kind} · at ${utc(r.at)} · instance_id ${PAGE_INSTANCE_ID}`;
    const parts = [`correction (${r.action}) on punch ${short(r.punchId)}`];
    if (r.kind !== null) parts.push(`new_kind ${r.kind}`);
    if (r.at !== null) parts.push(`new_at ${utc(r.at)}`);
    return parts.join(" · ");
  });
}

// The "Needs fixing" banner (spec §8), or null when the entry holds no problem.
// "Monday, November 2, 8:00 AM to 4:30 PM"; the end names its day when it's another one.
export function span(entry, { tz, h24 }) {
  const time = (ms) => clockTime(ms, tz, h24);
  const day = (ms) => {
    const [y, m, d] = localDate(ms, tz).split("-").map(Number);
    return `${DAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]}, ${MONTHS[m - 1]} ${d}`;
  };
  const start = `${day(entry.start)}, ${time(entry.start)}`;
  if (entry.end === null) return `${start} to working`;
  return `${start} to ${localDate(entry.end, tz) === localDate(entry.start, tz) ? "" : `${day(entry.end)}, `}${time(entry.end)}`;
}

export function banner(entry, { tz, h24 }) {
  const time = (ms) => clockTime(ms, tz, h24);
  if (entry.kind === "stray") {
    const what = listed(entry.punches.map((p) => `${article(WORD[p.kind])} at ${time(p.at)}`));
    return `${capital(what)} outside any shift. The hours ignore ${entry.punches.length === 1 ? "it" : "them"}.`;
  }
  const ignored = entry.punches.filter((p) => entry.ignored.has(p.id));
  if (ignored.length === 0) return null;
  const kinds = new Set(ignored.map((p) => p.kind));
  const [kind] = kinds;
  const same = entry.punches.filter((p) => p.kind === kind);
  if (kinds.size === 1 && same.length === ignored.length + 1 && !entry.ignored.has(same[0].id)) {
    const many = same.length <= 10 ? NUMBERS[same.length] : String(same.length);
    const counts = kind === "clock_in" ? "count from" : "count";
    const rest = same.length === 2 ? "the second" : "the rest";
    return `${many} ${WORD[kind]}s, at ${listed(same.map((p) => time(p.at)))}. The hours ${counts} the first and ignore ${rest}.`;
  }
  const which = ignored.map((p) => `${LABEL[p.kind]} at ${time(p.at)}`).join(", ");
  return `The hours ignore ${ignored.length} punch${ignored.length === 1 ? "" : "es"} in this shift: ${which}.`;
}

// A typed time of day: "5:05 PM", "5:05pm", "17:05", "1705", "505p", "5p", "12 am"; null when it isn't one.
export function parseTime(text) {
  const typed = text.toLowerCase().replace(/[\s.]/g, "");
  const match = /^(\d{1,2})(?::?(\d{2}))?(a|p|am|pm)?$/.exec(typed);
  if (match === null) return null;
  let h = Number(match[1]);
  const m = match[2] === undefined ? 0 : Number(match[2]);
  const half = match[3]?.[0];
  if (m > 59) return null;
  if (half === undefined) return h > 23 ? null : { h, m };
  if (h < 1 || h > 12) return null;
  h = (h % 12) + (half === "p" ? 12 : 0);
  return { h, m };
}

// The instant a typed time names for a slot (spec §8): a clock-in on the entry's day; anything else on the clock-in's
// day, or the next day when it's earlier in the day than the clock-in.
export function placeTime(entry, slotKind, { h, m }, tz) {
  const start = entry.kind === "shift" ? entry.shift.clockIn.at : entry.start;
  const day = localDate(start, tz);
  const wall = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  const at = zonedTime(day, wall, tz);
  if (slotKind === "clock_in" || entry.kind === "stray") return at;
  const begun = localParts(start, tz);
  if (h * 60 + m >= begun.hour * 60 + begun.minute) return at;
  const next = new Date(`${day}T12:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return zonedTime(next.toISOString().slice(0, 10), wall, tz);
}

export const storageKey = (email, database, employeeId, key) => `timeclock-draft:${email}:${database}:${employeeId}:${key}`;

// A draft for localStorage, with its note and the base token it was made against (spec §6.2).
export function serialize(draft, { note, token }) {
  return JSON.stringify({
    v: STORED_VERSION,
    base: draft.base,
    edits: [...draft.edits],
    focus: [...draft.focus],
    moved: [...draft.moved],
    resolved: [...draft.resolved],
    note,
    token,
  });
}

// `serialize`'s draft back, over the base it was made against (rebase it onto the current one); null when unreadable.
export function restore(text) {
  try {
    const stored = JSON.parse(text);
    if (stored?.v !== STORED_VERSION) return null;
    const draft = {
      base: stored.base,
      edits: new Map(stored.edits),
      focus: new Set(stored.focus),
      moved: new Map(stored.moved),
      resolved: new Map(stored.resolved),
    };
    return { draft, note: stored.note ?? "", token: stored.token };
  } catch {
    return null;
  }
}
