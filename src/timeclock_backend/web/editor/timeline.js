// The shift editor's timeline (spec 2026-11-05 §8): one entry as a bar zoomed to fit it and at least its business day,
// with a marker per punch that drags to the minute. A changed punch leaves a hollow marker where it was; a punch the
// hours ignore is red.
import { clockTime, localDate, localParts } from "shift-editor/rules.js";

const MINUTE = 60000;
const HOUR = 60 * MINUTE;
const PAD = 30 * MINUTE;
const CLOSE = 7; // percent: labels nearer than this go either side of their markers

export default {
  props: {
    entry: Object,
    punches: Array, // the entry's punches and any stray moved into it
    was: Array, // [{id, at}]: where changed punches were
    breaks: Array, // rules.js totalHours's breaks, for where each one's paid part ends
    breaksShown: { type: Boolean, default: true }, // false: the legend leaves out the break keys (spec 2026-11-13)
    red: Array, // ids the hours ignore
    changed: Array, // ids the draft changed or added
    now: Number,
    days: Object, // base_json's timelineDays: a local day's least [start, end], either null
    tz: String,
    h24: Boolean,
    readOnly: Boolean,
  },
  emits: ["move"],
  data: () => ({ frozen: null, dragging: null, last: null }),
  computed: {
    range() {
      if (this.frozen !== null) return this.frozen; // a drag keeps the zoom it started with
      const times = [...this.punches.map((p) => p.at), ...this.was.map((w) => w.at)];
      if (this.entry.kind === "shift" && this.entry.end === null) times.push(this.now);
      // A day outside the editor's week isn't listed, and its entry zooms to fit alone.
      const [start, end] = this.days[localDate(this.entry.start, this.tz)] ?? [null, null];
      const lo = Math.min(Math.min(...times) - PAD, start ?? Infinity);
      return [lo, Math.max(Math.max(...times) + PAD, lo + 2 * HOUR, end ?? -Infinity)];
    },
    ticks() {
      const [lo, hi] = this.range;
      const span = (hi - lo) / HOUR;
      const step = span <= 10 ? 1 : span <= 20 ? 2 : 4;
      const parts = localParts(lo, this.tz);
      const found = [];
      for (let t = lo - parts.minute * MINUTE - parts.second * 1000 + HOUR; t <= hi; t += HOUR) {
        if (localParts(t, this.tz).hour % step !== 0) continue;
        const label = clockTime(t, this.tz, this.h24);
        found.push({ at: t, left: this.pct(t), label: this.h24 ? label : label.replace(":00", "") });
      }
      return found;
    },
    segments() {
      const shift = this.entry.shift;
      if (shift === null) return [];
      const piece = (cls, a, b) => ({ cls, left: this.pct(a), width: Math.max(this.pct(b) - this.pct(a), 0.4) });
      return [
        piece("sg w", shift.clockIn.at, shift.clockOut === null ? this.now : shift.clockOut.at),
        // A break's unpaid part is striped ("u"); a split break squares the corners where its two parts meet.
        ...shift.breaks.flatMap((b) => {
          const end = b.end === null ? this.now : b.end;
          const counted = this.breaks.find((x) => x.start === b.start);
          const cut = counted === undefined ? end : b.start + counted.paid;
          if (cut >= end) return [piece("sg b", b.start, end)];
          if (cut <= b.start) return [piece("sg b u", b.start, end)];
          return [piece("sg b head", b.start, cut), piece("sg b u tail", cut, end)];
        }),
        ...shift.lunches.map((l) => piece("sg l", l.start, l.end === null ? this.now : l.end)),
      ];
    },
    markers() {
      const red = new Set(this.red);
      const changed = new Set(this.changed);
      const found = this.punches.map((p) => {
        const big = p.kind === "clock_in" || p.kind === "clock_out";
        const flagged = red.has(p.id);
        const moved = changed.has(p.id);
        return {
          id: p.id,
          left: this.pct(p.at),
          cls: ["mk", big ? "" : "s", moved ? "c" : "", flagged ? "x" : ""],
          label: big || moved || flagged ? clockTime(p.at, this.tz, this.h24) : null,
          side: "",
        };
      });
      const labelled = found.filter((m) => m.label !== null).sort((a, b) => a.left - b.left);
      for (let i = 1; i < labelled.length; i++) {
        if (labelled[i].left - labelled[i - 1].left < CLOSE) {
          labelled[i - 1].side = "lft";
          labelled[i].side = "rgt";
        }
      }
      return found;
    },
  },
  methods: {
    pct(ms) {
      const [lo, hi] = this.range;
      return ((ms - lo) / (hi - lo)) * 100;
    },
    time(ms) {
      return clockTime(ms, this.tz, this.h24);
    },
    down(event, marker) {
      if (this.readOnly || event.button !== 0) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      this.frozen = this.range;
      this.dragging = marker.id;
      this.last = null;
    },
    move(event) {
      if (this.dragging === null) return;
      const box = this.$refs.bar.getBoundingClientRect();
      const x = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
      const [lo, hi] = this.frozen;
      const at = Math.round((lo + x * (hi - lo)) / MINUTE) * MINUTE;
      if (at === this.last) return;
      this.last = at;
      this.$emit("move", this.dragging, at);
    },
    up() {
      this.frozen = null;
      this.dragging = null;
    },
  },
  template: `
    <div class="timeline">
      <div class="tl" ref="bar" :class="{ readonly: readOnly }">
        <div class="ax"></div>
        <div v-for="t in ticks" :key="t.at" class="tk" :style="{ left: t.left + '%' }"><span>{{ t.label }}</span></div>
        <div v-for="(s, i) in segments" :key="i" :class="s.cls" :style="{ left: s.left + '%', width: s.width + '%' }"></div>
        <div v-for="w in was" :key="'was-' + w.id" class="mk gh" :style="{ left: pct(w.at) + '%' }"><i>was {{ time(w.at) }}</i></div>
        <div
          v-for="m in markers"
          :key="m.id"
          :class="m.cls"
          :style="{ left: m.left + '%' }"
          @pointerdown="down($event, m)"
          @pointermove="move($event)"
          @pointerup="up"
          @pointercancel="up"
        ><i v-if="m.label" :class="m.side">{{ m.label }}</i></div>
      </div>
      <div class="legend">
        <span class="k k-work">Working</span><template v-if="breaksShown"><span class="k k-break">Break</span><span class="k k-unpaid">Unpaid break</span></template><span class="k k-lunch">Lunch</span>
        <span class="k k-red">Needs fixing</span>
        <span v-if="!readOnly">Drag a marker to change its time, or type it below.</span>
      </div>
    </div>
  `,
};
