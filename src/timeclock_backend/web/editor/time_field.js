// A time field (spec 2026-11-05 §6.5; owner, 2026-11-11): only digits are typed, shown as a time as they go in ("239"
// reads 2:39); AM or PM sits beside them, a click to switch (or a typed a or p), and the clock opens a picker. `at` is
// the punch's time, or null for a field a new time is typed into; `set` carries the instant placeTime puts a committed
// time at. A time commits on Enter, on leaving the field, from the picker, or, a punch's own time untouched, on a
// switch of AM or PM. An empty field's AM or PM is draft.js's guessHalf until it's set.
import * as D from "shift-editor/draft.js";
import { localParts } from "shift-editor/rules.js";

const pad = (n) => String(n).padStart(2, "0");

export default {
  props: {
    at: { type: Number, default: null },
    entry: Object,
    kind: String, // the slot's punch kind
    tz: String,
    h24: Boolean,
    readOnly: Boolean,
    label: String,
    placeholder: String,
  },
  emits: ["set"],
  data: () => ({ digits: "", half: null, dirty: false, bad: false, picking: false, picked: null, chosen: false }),
  computed: {
    text() {
      return this.digits.length > 2 ? `${this.digits.slice(0, -2)}:${this.digits.slice(-2)}` : this.digits;
    },
    // The half shown: none in 24-hour time or before an hour is typed, and a 24-hour hour's own.
    shown() {
      if (this.h24 || this.digits === "") return null;
      const h = Number(this.digits.length > 2 ? this.digits.slice(0, -2) : this.digits);
      if (h > 23) return null;
      if (h === 0) return "AM";
      if (h > 12) return "PM";
      return this.half ?? D.guessHalf(this.entry, this.kind, { h, m: this.digits.length > 2 ? Number(this.digits.slice(-2)) : 0 }, this.tz);
    },
    saved() {
      return [this.at, this.tz, this.h24];
    },
  },
  watch: {
    saved: { handler: "reset", immediate: true },
  },
  methods: {
    reset() {
      this.dirty = false;
      this.bad = false;
      if (this.at === null) {
        this.digits = "";
        this.half = null;
        return;
      }
      const p = localParts(this.at, this.tz);
      this.digits = `${this.h24 ? pad(p.hour) : p.hour % 12 || 12}${pad(p.minute)}`;
      this.half = this.h24 ? null : p.hour < 12 ? "AM" : "PM";
    },
    typed(event) {
      const field = event.target;
      const raw = field.value;
      const before = raw.slice(0, field.selectionStart).replace(/\D/g, "").length; // digits before the caret
      const letter = /[ap]/i.exec(raw);
      if (letter !== null && !this.h24) this.half = `${letter[0].toUpperCase()}M`;
      this.digits = raw.replace(/\D/g, "").slice(0, 4);
      this.dirty = true;
      this.bad = false;
      // Set here as well as through :value, which leaves a dropped letter in place when the digits don't change.
      field.value = this.text;
      let caret = 0;
      for (let seen = 0; seen < before && caret < this.text.length; caret += 1) if (this.text[caret] !== ":") seen += 1;
      field.setSelectionRange(caret, caret);
    },
    // A time from the picker, or the typed one; nothing typed in a field without a punch is nothing to commit.
    commit(picked = null) {
      if (picked === null && !this.dirty) return;
      if (picked === null && this.digits === "" && this.at === null) return this.reset();
      const time = picked ?? D.readTime(this.digits, this.shown);
      if (time === null) {
        this.bad = true;
        return;
      }
      this.$emit("set", D.placeTime(this.entry, this.kind, time, this.tz));
      this.reset(); // back to `at`, which the change moves (a join or split asks first, and may not)
    },
    left() {
      if (!this.picking) this.commit();
    },
    flip() {
      const shown = this.shown;
      const h = Number(this.digits.length > 2 ? this.digits.slice(0, -2) : this.digits);
      if (h === 0 || h > 12) this.digits = `${h % 12 || 12}${this.digits.length > 2 ? this.digits.slice(-2) : ""}`; // 12-hour first
      this.half = shown === "AM" ? "PM" : "AM";
      if (this.at !== null && !this.dirty) {
        this.dirty = true;
        this.commit();
      }
    },
    opened() {
      this.picking = true;
      this.chosen = false;
      const time = D.readTime(this.digits, this.shown);
      this.picked = time === null ? null : `${pad(time.h)}:${pad(time.m)}`;
    },
    closed() {
      this.picking = false;
      if (!this.chosen || this.picked === null) return this.commit(); // what was typed, as leaving the field would
      const [h, m] = this.picked.split(":").map(Number);
      this.commit({ h, m });
    },
  },
  template: `
    <div :class="['field', 'time-field', { bad }]">
      <span v-if="readOnly" class="page-icon icon-schedule"></span>
      <button v-else type="button" class="tf-pick" aria-label="Pick a time" @mousedown.prevent>
        <span class="page-icon icon-schedule"></span>
        <q-popup-proxy @before-show="opened" @hide="closed">
          <q-time v-model="picked" mask="HH:mm" :format24h="h24" class="tf-picker">
            <div class="tf-actions">
              <button type="button" class="btn text" v-close-popup>Cancel</button>
              <button type="button" class="btn text" v-close-popup @click="chosen = true">Set</button>
            </div>
          </q-time>
        </q-popup-proxy>
      </button>
      <input :value="text" :disabled="readOnly" :placeholder="placeholder" :aria-label="label" inputmode="numeric" autocomplete="off"
        @input="typed" @keydown.enter.prevent="commit()" @blur="left" />
      <span v-if="shown && readOnly" class="tf-half">{{ shown }}</span>
      <button v-else-if="shown" type="button" class="tf-half" tabindex="-1" :aria-label="'Switch to ' + (shown === 'AM' ? 'PM' : 'AM')"
        @mousedown.prevent @click="flip">{{ shown }}</button>
    </div>
  `,
};
