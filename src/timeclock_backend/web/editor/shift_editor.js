// The shift editor (spec 2026-11-05 §6, §8; mockups C to E, H): everything below the title Python draws. The draft
// lives here and in localStorage, so editing carries on through a dropped connection; `save` sends it to Python
// (shift_editor.py's save_shift) and `saveResult` is the answer. The logic is draft.js's; this file draws it.
import { markRaw } from "vue";
import * as D from "shift-editor/draft.js";
import { clockTime, entries, entryHours, flags, hm, localDate, order, weekHours } from "shift-editor/rules.js";
import Timeline from "shift-editor/timeline.js";

const KINDS = [
  ["clock_in", "Clock in"],
  ["clock_out", "Clock out"],
  ["break_start", "Break start"],
  ["break_end", "Break end"],
  ["lunch_start", "Lunch start"],
  ["lunch_end", "Lunch end"],
];
const LABEL = Object.fromEntries(KINDS);
const WORD = { clock_in: "clock-in", clock_out: "clock-out", break_start: "break start", break_end: "break end", lunch_start: "lunch start", lunch_end: "lunch end" };
const NUMBERS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"];
const MINUTE = 60000;
const PAIR_LENGTH = { break: 15 * MINUTE, lunch: 30 * MINUTE }; // an added break or lunch, until it's moved

// A punch's ⋮ menu (spec §6.6): what it was meant as, or remove it.
const PunchMenu = {
  props: { punch: Object, tz: String, h24: Boolean },
  emits: ["apply"],
  data: () => ({ kinds: KINDS }),
  methods: {
    time(ms) {
      return clockTime(ms, this.tz, this.h24);
    },
  },
  computed: {
    D: () => D,
  },
  template: `
    <q-btn flat round dense size="sm" class="more" :aria-label="'Change the punch at ' + time(punch.at)">
      <span class="page-icon icon-more"></span>
      <q-menu class="punch-menu" auto-close>
        <div class="menu-h">{{ time(punch.at) }} was meant as</div>
        <q-item v-for="[kind, label] in kinds" :key="kind" clickable dense @click="$emit('apply', (d) => D.setKind(d, punch.id, kind))">
          <span :class="['page-icon', kind === punch.kind ? 'icon-check' : 'icon-blank']"></span>{{ label }}
        </q-item>
        <q-separator />
        <q-item clickable dense class="menu-remove" @click="$emit('apply', (d) => D.remove(d, punch.id))">
          <span class="page-icon icon-delete"></span>Remove this punch
        </q-item>
      </q-menu>
    </q-btn>
  `,
};

// One slot (spec §6.5): a punch's time field, a missing punch to type in, or a choice between several.
const SlotField = {
  components: { "punch-menu": PunchMenu },
  props: {
    place: Object,
    kind: String,
    label: String,
    entry: Object,
    base: Object, // Map of the base punches by id
    edits: Object, // the draft's edits
    red: Object, // Set of ids the hours ignore
    strays: Object, // Set of ids moved in from a stray
    tz: String,
    h24: Boolean,
    readOnly: Boolean,
    working: Boolean,
    adding: Boolean, // a shift being added: every punch is new, so none is marked so
  },
  emits: ["apply"],
  data: () => ({ bad: false }),
  computed: {
    D: () => D,
    word() {
      return WORD[this.kind];
    },
    many() {
      const n = this.place.punches.length;
      return `${n <= 10 ? NUMBERS[n] : n} ${WORD[this.kind]}s`;
    },
  },
  methods: {
    time(ms) {
      return clockTime(ms, this.tz, this.h24);
    },
    placed(event) {
      const typed = D.parseTime(event.target.value);
      this.bad = typed === null;
      return typed === null ? null : D.placeTime(this.entry, this.kind, typed, this.tz);
    },
    typed(punch, event) {
      const at = this.placed(event);
      if (at === null) return;
      this.$emit("apply", (d) => D.setTime(d, punch.id, at));
      event.target.value = this.time(at);
    },
    added(event) {
      const at = this.placed(event);
      if (at === null) return;
      event.target.value = "";
      this.$emit("apply", (d) => D.add(d, this.kind, at));
    },
    keptAt(event) {
      const at = this.placed(event);
      if (at === null) return;
      this.$emit("apply", (d) => D.keep(d, this.place, { at }));
    },
    was(punch) {
      const before = this.base.get(punch.id);
      const edit = this.edits.get(punch.id);
      if (before === undefined || edit === undefined || edit.new) return null;
      if (before.kind === punch.kind && before.at === punch.at) return null;
      const what = before.kind === punch.kind ? this.time(before.at) : `${LABEL[before.kind]} at ${this.time(before.at)}`;
      return `Was ${what}${before.automatic ? " (automatic clock-out)" : ""}`;
    },
    tag(punch) {
      return this.strays.has(punch.id) ? "stray" : this.red.has(punch.id) ? "extra" : null;
    },
  },
  template: `
    <div class="slot">
      <span class="lab">{{ label }}</span>
      <div v-if="place.punches.length > 1" class="choice">
        <div class="hd">{{ many }}. Keep:</div>
        <div class="opts">
          <template v-for="p in place.punches" :key="p.id">
            <button class="radio" :disabled="readOnly" @click="$emit('apply', (d) => D.keep(d, place, { id: p.id }))">
              <span class="rd"></span>{{ time(p.at) }}<span v-if="tag(p)" class="tagx">{{ tag(p) }}</span>
            </button>
            <punch-menu v-if="!readOnly" :punch="p" :tz="tz" :h24="h24" @apply="(f) => $emit('apply', f)" />
          </template>
          <span class="radio-other">Another time</span>
          <div :class="['field', 'field-small', { bad }]"><input :disabled="readOnly" placeholder="--:--" @change="keptAt" /></div>
        </div>
        <div class="small muted">The ones you don't keep are removed. Another time keeps a single {{ word }} at the time you type.</div>
      </div>
      <div v-else-if="place.punches.length === 1" class="slot-one">
        <div class="row-tight">
          <div :class="['field', { changed: was(place.punches[0]) || (!adding && edits.get(place.punches[0].id)?.new), bad: bad || red.has(place.punches[0].id) }]">
            <span class="page-icon icon-schedule"></span>
            <input :value="time(place.punches[0].at)" :disabled="readOnly" :aria-label="label" @change="typed(place.punches[0], $event)" />
          </div>
          <span v-if="tag(place.punches[0])" class="tagx">{{ tag(place.punches[0]) }}</span>
          <punch-menu v-if="!readOnly" :punch="place.punches[0]" :tz="tz" :h24="h24" @apply="(f) => $emit('apply', f)" />
        </div>
        <div v-if="was(place.punches[0])" class="was">
          {{ was(place.punches[0]) }}
          <button v-if="!readOnly" class="btn text" @click="$emit('apply', (d) => D.undo(d, place.punches[0].id))"><span class="page-icon icon-undo"></span>Undo</button>
        </div>
        <div v-else-if="!adding && edits.get(place.punches[0].id)?.new" class="was">
          Added
          <button v-if="!readOnly" class="btn text" @click="$emit('apply', (d) => D.undo(d, place.punches[0].id))"><span class="page-icon icon-undo"></span>Undo</button>
        </div>
      </div>
      <div v-else :class="['field', 'missing', { bad }]">
        <input :disabled="readOnly" :placeholder="working ? 'Working' : 'Missing'" :aria-label="label" @change="added" />
      </div>
    </div>
  `,
};

export default {
  components: { "editor-timeline": Timeline, "editor-slot": SlotField, "punch-menu": PunchMenu },
  props: { base: Object, history: Array, options: Object },
  emits: ["save"],
  data() {
    return {
      draft: markRaw(D.newDraft(this.base)),
      note: "",
      now: Date.now(),
      connected: window.socket ? window.socket.connected : true,
      saving: false,
      outside: [], // changes someone else made, pointed out after a rebase (spec §6.1)
      confirmOutside: false, // then Save asks first
      message: null, // {color, text, open}
      dialog: null, // {kind: "join" | "split" | "outside" | "fault", ...}
      shownHistory: this.history, // the prop, or a save's answer since
    };
  },
  computed: {
    D: () => D,
    tz() {
      return this.draft.base.tz;
    },
    h24() {
      return this.options.h24;
    },
    readOnly() {
      return this.draft.base.own;
    },
    first() {
      return this.draft.base.employee.first;
    },
    current() {
      return D.punches(this.draft);
    },
    shownAll() {
      return D.shown(this.draft);
    },
    baseById() {
      return new Map(this.draft.base.punches.map((p) => [p.id, p]));
    },
    hoursOpts() {
      return { paidBreakMinutes: this.draft.base.paidBreakMinutes, tz: this.tz, now: this.now };
    },
    refusedIds() {
      const found = D.refused(this.draft);
      return found === null ? [] : found.punchIds;
    },
    views() {
      const d = this.draft;
      const all = this.shownAll;
      const shiftKeys = new Set(all.filter((e) => e.kind === "shift").map((e) => e.key));
      const before = new Map(entries(d.base.punches, Infinity).map((e) => [e.key, e]));
      const everything = entries(this.current, Infinity);
      return all
        .filter((e) => !(e.kind === "stray" && shiftKeys.has(d.moved.get(e.key))))
        .map((e) => {
          const moved = all.filter((s) => s.kind === "stray" && d.moved.get(s.key) === e.key).flatMap((s) => s.punches);
          const red = new Set([...e.ignored, ...moved.map((p) => p.id), ...this.refusedIds]);
          const out = e.clockOut;
          const old = before.get(e.key);
          const day = localDate(e.start, this.tz);
          return {
            key: e.key,
            entry: e,
            sections: D.sections(d, e),
            punches: [...e.punches, ...moved].sort(order),
            red,
            strays: new Set(moved.map((p) => p.id)),
            banner: e.kind === "new" ? null : D.banner(e, { tz: this.tz, h24: this.h24 }), // a shift being added says what it lacks under Save
            flags: flags(e),
            automatic: out !== null && out.automatic && out.version === null ? out : null,
            hours: e.kind === "shift" ? entryHours(e, this.current, this.hoursOpts) : null,
            before: old !== undefined && old.kind === "shift" ? entryHours(old, d.base.punches, this.hoursOpts) : null,
            shiftsThatDay: e.kind === "stray" ? everything.filter((s) => s.kind === "shift" && localDate(s.start, this.tz) === day) : [],
          };
        });
    },
    shiftViews() {
      return this.views.filter((v) => v.entry.kind === "shift");
    },
    week() {
      const opts = { ...this.hoursOpts, week: this.draft.base.week };
      const before = weekHours(this.draft.base.punches, opts).worked;
      return { before, after: this.overlapNote ? before : weekHours(this.current, opts).worked }; // an overlap merges into the other shift
    },
    changed() {
      return [...this.draft.edits].filter(([, e]) => !e.removed).map(([id]) => id);
    },
    changeList() {
      return D.changeList(this.draft, { tz: this.tz, h24: this.h24 });
    },
    writeLines() {
      return D.writes(this.draft);
    },
    conflicts() {
      return D.clashes(this.draft);
    },
    overlapNote() {
      return D.overlap(this.draft, { h24: this.h24 });
    },
    blockers() {
      return D.blockers(this.draft, { connected: this.connected, readOnly: this.readOnly });
    },
    canSave() {
      return !this.saving && this.blockers.length === 0;
    },
  },
  watch: {
    history(next) {
      this.shownHistory = next;
    },
    base(next) {
      this.rebaseOnto(next, false);
    },
    draft() {
      this.store();
    },
    note() {
      this.store();
    },
  },
  created() {
    let stored = null;
    try {
      stored = localStorage.getItem(this.options.storageKey);
    } catch {
      stored = null;
    }
    const restored = stored === null ? null : D.restore(stored);
    if (restored !== null && !this.base.own) {
      this.draft = markRaw(restored.draft);
      this.note = restored.note;
      this.rebaseOnto(this.base, false);
    }
  },
  mounted() {
    this.ticker = setInterval(() => (this.now = Date.now()), 30000);
    this.onConnect = () => (this.connected = true);
    this.onDisconnect = () => (this.connected = false);
    const attach = () => {
      if (!window.socket) {
        this.attaching = setTimeout(attach, 200);
        return;
      }
      window.socket.on("connect", this.onConnect);
      window.socket.on("disconnect", this.onDisconnect);
      this.connected = window.socket.connected;
    };
    attach();
  },
  unmounted() {
    clearInterval(this.ticker);
    clearTimeout(this.attaching);
    if (window.socket) {
      window.socket.off("connect", this.onConnect);
      window.socket.off("disconnect", this.onDisconnect);
    }
  },
  methods: {
    time(ms) {
      return clockTime(ms, this.tz, this.h24);
    },
    hm(ms) {
      return hm(ms);
    },
    span(entry) {
      return D.span(entry, { tz: this.tz, h24: this.h24 });
    },
    kindTime(p) {
      return `${LABEL[p.kind]} at ${this.time(p.at)}`;
    },
    listed(entry) {
      return entry.punches.map((p) => `${LABEL[p.kind]} ${this.time(p.at)}`).join(", ");
    },
    // Every change goes through here, so a join or a split is confirmed before it applies (spec §6.7).
    apply(change) {
      const next = typeof change === "function" ? change(this.draft) : change;
      if (next === this.draft) return;
      const found = D.joinSplit(this.draft, next);
      if (found !== null) {
        this.dialog = { kind: found.kind, next: markRaw(next), entries: found.entries };
        return;
      }
      this.draft = markRaw(next);
      this.message = null;
    },
    confirmChange() {
      this.draft = this.dialog.next;
      this.dialog = null;
      this.message = null;
    },
    was(view) {
      return view.punches
        .map((p) => ({ p, before: this.baseById.get(p.id), edit: this.draft.edits.get(p.id) }))
        .filter(({ p, before, edit }) => before !== undefined && edit !== undefined && !edit.new && before.at !== p.at)
        .map(({ before }) => ({ id: before.id, at: before.at }));
    },
    addPair(view, what) {
      const shift = view.entry.shift;
      const start = shift.clockIn.at;
      const end = shift.clockOut === null ? Math.max(this.now, start + 2 * PAIR_LENGTH[what]) : shift.clockOut.at;
      const at = Math.round((start + (end - start) / 2) / MINUTE) * MINUTE;
      this.apply((d) => D.add(D.add(d, `${what}_start`, at), `${what}_end`, at + PAIR_LENGTH[what]));
    },
    removePair(pair) {
      this.apply((d) => [...pair.start.punches, ...pair.end.punches].reduce((next, p) => D.remove(next, p.id), d));
    },
    duration(pair) {
      if (pair.start.punches.length !== 1 || pair.end.punches.length !== 1) return "";
      return hm(pair.end.punches[0].at - pair.start.punches[0].at);
    },
    moveIn(stray, shift) {
      this.apply((d) => D.moveIn(d, stray.key, shift.key));
    },
    resolve(id, choice) {
      this.apply((d) => D.resolve(d, id, choice));
    },
    rebaseOnto(newBase, fromSave) {
      const hadEdits = this.draft.edits.size > 0;
      const found = D.rebase(this.draft, newBase, { h24: this.h24 });
      this.draft = markRaw(found.draft);
      if (found.outside.length > 0) {
        this.outside = [...this.outside, ...found.outside];
        if (hadEdits) this.confirmOutside = true;
      }
      if (fromSave && found.conflicts.length === 0) {
        if (found.outside.length > 0) this.dialog = { kind: "outside" };
        else this.save(false); // nothing this editor shows changed; the token had moved elsewhere in the week
      }
    },
    save(acceptServer) {
      if (this.confirmOutside && !acceptServer && this.dialog?.kind !== "outside") {
        this.dialog = { kind: "outside" };
        return;
      }
      this.dialog = null;
      this.confirmOutside = false;
      const asOf = Date.now();
      const opts = { ...this.hoursOpts, now: asOf };
      const preview = { entries: {}, week: weekHours(this.current, { ...opts, week: this.draft.base.week }).worked };
      for (const e of this.shownAll) {
        if (e.kind !== "shift") continue;
        const h = entryHours(e, this.current, opts);
        preview.entries[e.key] = [h.worked, h.paidBreak, h.unpaid];
      }
      this.saving = true;
      this.message = null;
      this.$emit("save", {
        draft: D.draftPunches(this.draft),
        note: this.note,
        token: this.draft.base.token,
        focus: [...this.draft.focus],
        asOf,
        preview,
        acceptServer,
      });
    },
    saveResult(result) {
      this.saving = false;
      if (result.ok) {
        const focus = [...this.draft.focus, ...this.changed];
        this.draft = markRaw(D.newDraft(result.base, { focus }));
        this.note = "";
        this.outside = [];
        this.confirmOutside = false;
        this.message = { color: "green", text: "Saved." };
        this.shownHistory = result.history;
        this.forget();
      } else if (result.changed) this.rebaseOnto(result.changed, true);
      else if (result.refused) this.message = { color: "red", text: result.refused, open: result.open ?? null };
      else if (result.fault) this.dialog = { kind: "fault", ...result.fault };
      else if (result.signedOut) window.location.assign("/signin");
      else if (result.reload) window.location.reload();
      else this.message = { color: "red", text: result.error ?? "The save didn't go through." };
    },
    discard() {
      this.draft = markRaw(D.newDraft(this.draft.base));
      this.note = "";
      this.outside = [];
      this.confirmOutside = false;
      this.message = null;
      this.forget();
      if (this.options.add) window.location.assign(this.options.back);
    },
    store() {
      if (this.readOnly || !this.options.storageKey) return;
      try {
        const d = this.draft;
        if (d.edits.size > 0 || d.moved.size > 0 || this.note) {
          localStorage.setItem(this.options.storageKey, D.serialize(d, { note: this.note, token: d.base.token }));
        } else localStorage.removeItem(this.options.storageKey);
      } catch {
        // storage full or blocked: the draft still lives in this page
      }
    },
    forget() {
      try {
        localStorage.removeItem(this.options.storageKey);
      } catch {
        // as store()
      }
    },
  },
  template: `
    <div class="editor">
      <div v-for="c in conflicts" :key="'c-' + c.id" class="callout red">
        <span class="page-icon icon-error"></span>
        <div class="grow">
          <b>Someone else changed this punch while you were editing.</b>
          Theirs: {{ c.theirs ? kindTime(c.theirs) : "removed" }}<template v-if="c.at">, saved {{ time(c.at) }}</template><template v-if="c.by"> by {{ c.by }}</template>
          · Yours: {{ c.yours.removed ? "removed" : kindTime(c.yours) }}
        </div>
        <button class="btn outline" @click="resolve(c.id, 'theirs')">Keep theirs</button>
        <button class="btn outline" @click="resolve(c.id, 'mine')">Keep mine</button>
      </div>
      <div v-if="outside.length" class="callout blue">
        <span class="page-icon icon-info"></span>
        <div class="grow">
          <b>The punches changed while you were editing.</b> Your changes are kept on top of them.
          <ul class="plain"><li v-for="(line, i) in outside" :key="i">{{ line }}</li></ul>
        </div>
        <button class="btn text" @click="outside = []">OK</button>
      </div>
      <div v-if="message" :class="['callout', message.color]">
        <span :class="['page-icon', message.color === 'green' ? 'icon-check' : 'icon-error']"></span>
        <div class="grow">{{ message.text }}</div>
        <a v-if="message.open" class="btn outline" :href="message.open">Open that shift</a>
      </div>
      <div v-if="overlapNote" class="callout red">
        <span class="page-icon icon-error"></span>
        <div class="grow">{{ overlapNote.text }}</div>
        <a class="btn outline" :href="overlapNote.open">Open that shift</a>
      </div>

      <div class="ed">
        <div class="ed-main">
          <template v-for="v in views" :key="v.key">
            <div v-if="views.length > 1" class="entry-head">{{ span(v.entry) }}</div>
            <div v-if="v.banner" class="callout red">
              <span class="page-icon icon-error"></span>
              <div><b>Needs fixing:</b> {{ v.banner }}</div>
            </div>
            <div v-if="v.automatic && !readOnly" class="callout orange">
              <span class="page-icon icon-schedule"></span>
              <div class="grow">
                {{ first }} was clocked out automatically at {{ time(v.automatic.at) }}. Change the clock-out below to the
                real time, or mark it as correct if {{ time(v.automatic.at) }} is right.
              </div>
              <button class="btn outline" @click="apply((d) => D.confirm(d, v.automatic.id))">Mark as correct</button>
            </div>
            <div v-if="v.punches.length" class="card">
              <h4>Timeline</h4>
              <editor-timeline
                :entry="v.entry"
                :punches="v.punches"
                :was="was(v)"
                :red="[...v.red]"
                :changed="options.add ? [] : changed"
                :now="now"
                :tz="tz"
                :h24="h24"
                :read-only="readOnly"
                @move="(id, at) => apply((d) => D.setTime(d, id, at))"
              />
            </div>
            <div class="card">
              <h4>{{ v.entry.kind === "stray" ? "Stray punch" + (v.entry.punches.length > 1 ? "es" : "") : "Shift" }}</h4>
              <div class="row-slots">
                <editor-slot label="Clock in" kind="clock_in" :place="v.sections.clockIn" :entry="v.entry" :base="baseById" :edits="draft.edits"
                  :red="v.red" :strays="v.strays" :tz="tz" :h24="h24" :read-only="readOnly" :adding="options.add" @apply="apply" />
                <editor-slot label="Clock out" kind="clock_out" :place="v.sections.clockOut" :entry="v.entry" :base="baseById" :edits="draft.edits"
                  :red="v.red" :strays="v.strays" :tz="tz" :h24="h24" :read-only="readOnly" :adding="options.add" :working="v.entry.kind === 'shift' && v.entry.end === null" @apply="apply" />
              </div>
              <div v-if="v.sections.others.length" class="others">
                <div class="lab">Punches the hours ignore</div>
                <div v-for="p in v.sections.others" :key="p.id" class="row-tight">
                  <span class="tagx">extra</span>{{ kindTime(p) }}
                  <punch-menu v-if="!readOnly" :punch="p" :tz="tz" :h24="h24" @apply="apply" />
                </div>
              </div>
              <div v-if="v.shiftsThatDay.length && !readOnly" class="move-into">
                <button v-for="s in v.shiftsThatDay" :key="s.key" class="btn outline" @click="moveIn(v.entry, s)">
                  Move into the shift {{ time(s.start) }} to {{ s.end === null ? "now" : time(s.end) }}
                </button>
              </div>
            </div>
            <div v-for="what in ['break', 'lunch']" :key="what" class="card" v-show="v.entry.kind !== 'stray' || v.sections[what === 'break' ? 'breaks' : 'lunches'].length">
              <h4>{{ what === "break" ? "Breaks" : "Lunches" }}</h4>
              <div v-for="(pair, i) in v.sections[what === 'break' ? 'breaks' : 'lunches']" :key="i" class="pair">
                <span class="lbl">{{ what === "break" ? "Break" : "Lunch" }}</span>
                <editor-slot label="Start" :kind="what + '_start'" :place="pair.start" :entry="v.entry" :base="baseById" :edits="draft.edits"
                  :red="v.red" :strays="v.strays" :tz="tz" :h24="h24" :read-only="readOnly" :adding="options.add" @apply="apply" />
                <span class="to">to</span>
                <editor-slot label="End" :kind="what + '_end'" :place="pair.end" :entry="v.entry" :base="baseById" :edits="draft.edits"
                  :red="v.red" :strays="v.strays" :tz="tz" :h24="h24" :read-only="readOnly" :adding="options.add" @apply="apply" />
                <span class="dur">{{ duration(pair) }}</span>
                <q-btn v-if="!readOnly" flat round dense size="sm" class="del" aria-label="Remove" @click="removePair(pair)">
                  <span class="page-icon icon-delete"></span>
                </q-btn>
              </div>
              <button v-if="!readOnly && v.entry.kind === 'shift'" class="btn text" @click="addPair(v, what)">
                <span class="page-icon icon-add"></span>Add {{ what }}
              </button>
              <div v-else-if="v.entry.kind === 'new'" class="small muted">Add the clock-in first.</div>
              <div v-else-if="!v.sections[what === 'break' ? 'breaks' : 'lunches'].length" class="small muted">None.</div>
            </div>
            <div v-if="v.sections.strays.length && !readOnly" class="card">
              <h4>Stray punches this day</h4>
              <div v-for="s in v.sections.strays" :key="s.key" class="stray-row">
                <span class="tagx">stray</span><span class="grow">{{ listed(s) }}</span>
                <button class="btn outline" @click="moveIn(s, v.entry)">Move into this shift</button>
              </div>
            </div>
          </template>
          <div v-if="!views.length" class="callout grey">
            <span class="page-icon icon-info"></span>
            <div>Every punch here is removed. Save to remove them, or Discard to start again.</div>
          </div>
          <div v-if="!options.add" class="card">
            <h4>History</h4>
            <p v-if="!shownHistory.length" class="small muted">No earlier changes to this shift.</p>
            <ul v-else class="hist">
              <li v-for="(h, i) in shownHistory" :key="i">
                <div><b>{{ h.when }}</b> · {{ h.who }}</div>
                <div v-for="(line, j) in h.lines" :key="j">{{ line }}</div>
                <div v-if="h.note" class="muted">"{{ h.note }}"</div>
                <div v-if="options.technical" class="det">{{ h.detail }}</div>
              </li>
            </ul>
          </div>
        </div>

        <div class="ed-side">
          <div class="card hours">
            <h4>Hours</h4>
            <table>
              <template v-for="v in shiftViews" :key="v.key">
                <tr v-if="shiftViews.length > 1"><td colspan="2" class="sub">{{ span(v.entry) }}</td></tr>
                <tr v-for="[label, field] in [['Worked', 'worked'], ['Paid breaks', 'paidBreak'], ['Lunch and unpaid breaks', 'unpaid']]" :key="field">
                  <td>{{ label }}</td>
                  <td class="v">
                    <span v-if="v.before && v.before[field] !== v.hours[field]" class="old">{{ hm(v.before[field]) }}</span>
                    <span :class="{ new: v.before && v.before[field] !== v.hours[field] }">{{ hm(v.hours[field]) }}</span>
                  </td>
                </tr>
              </template>
              <tr class="wk2">
                <td>{{ first }}'s week</td>
                <td class="v">
                  <span v-if="week.before !== week.after" class="old">{{ hm(week.before) }}</span>
                  <span :class="{ new: week.before !== week.after }">{{ hm(week.after) }}</span>
                </td>
              </tr>
            </table>
            <div class="small muted">Preview, checked again when you save.</div>
          </div>
          <div v-if="!readOnly" class="card">
            <h4>Your changes</h4>
            <ul v-if="changeList.length" class="changes">
              <li v-for="c in changeList" :key="c.id">
                <span class="page-icon icon-edit"></span><span class="grow">{{ c.text }}</span>
                <button v-if="c.id !== null" class="btn text" @click="apply((d) => D.undo(d, c.id))">Undo</button>
              </li>
            </ul>
            <p v-else class="small muted">None yet.</p>
            <div v-if="options.technical && writeLines.length" class="det">
              <div v-for="(w, i) in writeLines" :key="i">{{ w }}</div>
            </div>
            <textarea v-model="note" class="note" placeholder="Note (optional), e.g. left at 5 and forgot to clock out"></textarea>
            <div class="saverow">
              <button class="btn outline" @click="discard">{{ options.add ? "Cancel" : "Discard" }}</button>
              <button class="btn primary" :disabled="!canSave" @click="save(false)">
                {{ saving ? "Saving..." : options.add ? "Add shift" : "Save changes" }}
              </button>
            </div>
            <div v-for="(b, i) in blockers" :key="i" class="blocker">{{ b }}</div>
          </div>
        </div>
      </div>

      <q-dialog :model-value="dialog !== null" @update:model-value="(open) => { if (!open) dialog = null; }">
        <div v-if="dialog" class="dlg">
          <template v-if="dialog.kind === 'join'">
            <h3>Join two shifts?</h3>
            <p>This change runs this shift into the next one, so they become one shift: {{ span(dialog.entries[0]) }}. It shows here as one shift, and both save together.</p>
            <div class="saverow"><button class="btn outline" @click="dialog = null">Cancel</button><button class="btn primary" @click="confirmChange">Join them</button></div>
          </template>
          <template v-else-if="dialog.kind === 'split'">
            <h3>Split this shift?</h3>
            <p>This change splits the shift in two:</p>
            <ul class="plain"><li v-for="e in dialog.entries" :key="e.key">{{ span(e) }}</li></ul>
            <p>Both show here, one above the other, and save together.</p>
            <div class="saverow"><button class="btn outline" @click="dialog = null">Cancel</button><button class="btn primary" @click="confirmChange">Split it</button></div>
          </template>
          <template v-else-if="dialog.kind === 'outside'">
            <h3>Save on top of the new punches?</h3>
            <p>The punches changed while you were editing (listed at the top). Your changes are kept on top of them.</p>
            <div class="saverow"><button class="btn outline" @click="dialog = null">Cancel</button><button class="btn primary" @click="save(false)">Save</button></div>
          </template>
          <template v-else-if="dialog.kind === 'fault'">
            <h3>The page found a problem in itself</h3>
            <p>
              Please tell {{ options.contact }}: {{ dialog.what }} shows {{ dialog.preview }} here, but the server worked out
              {{ dialog.server }}. The server's numbers are the right ones.
            </p>
            <div class="saverow"><button class="btn outline" @click="dialog = null">Cancel</button><button class="btn primary" @click="save(true)">Save with {{ dialog.server }}</button></div>
          </template>
        </div>
      </q-dialog>
    </div>
  `,
};
