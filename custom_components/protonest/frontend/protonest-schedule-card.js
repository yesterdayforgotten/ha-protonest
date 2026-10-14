// ProtoNest's weekly schedule card: `type: custom:protonest-schedule-card`,
// `entity: climate.<thermostat>`, and optionally `schedule: heat | cool | range`
// (the temperature schedule shown; default: the one the current mode uses).
//
// Each day has two bars: the thermostat's temperature schedule, and below it
// the target sensor schedule Home Assistant runs (on thermostats with paired
// sensors). Tapping a day edits both.
//
// Temperatures are read from the `schedule` attribute and saved through
// protonest.schedule_add / _update / _remove, one entry at a time, like the
// Nest app. Sensors are read from the `sensor_schedule` attribute and saved
// whole through protonest.set_sensor_schedule.

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const KINDS = { heat: "Heat", cool: "Cool", range: "Heat · Cool" };
const MODE_KIND = { heat: "heat", cool: "cool", heat_cool: "range" };
const SENSORS = "sensors"; // the kind of a sensor schedule row
const STEP_MINUTES = 15; // the thermostat only takes quarter hours
const THERMOSTAT = "thermostat"; // the thermostat's own sensor, in the sensor schedule
const SENSOR_COLORS = ["#8e24aa", "#00897b", "#e53935", "#3949ab", "#f4511e", "#7cb342", "#6d4c41", "#00acc1"];
const THERMOSTAT_COLOR = "#78909c";
const DAY_SETS = { all: DAYS, weekdays: DAYS.slice(0, 5), weekend: DAYS.slice(5) }; // "Copy to…" shortcuts
const DAY_SET_NAMES = { all: "All", weekdays: "Weekdays", weekend: "Weekend" };
const DAY_FULL = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };
// mdi:clock-outline
const CLOCK_ICON = "M12,20A8,8 0 0,0 20,12A8,8 0 0,0 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22C6.47,22 2,17.5 2,12A10,10 0 0,1 12,2M12.5,7V12.25L17,14.92L16.25,16.15L11,13V7H12.5Z";
const DRAG_PX = 6; // how far a press moves before it's a drag rather than a tap

const minutes = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};
const hhmm = (mins) => `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
const quarter = (value) => hhmm(Math.min(24 * 60 - STEP_MINUTES, Math.round(minutes(value) / STEP_MINUTES) * STEP_MINUTES));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const byTime = (a, b) => DAYS.indexOf(a.day) - DAYS.indexOf(b.day) || minutes(a.time) - minutes(b.time);

class ProtoNestScheduleCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("Set `entity` to a ProtoNest thermostat (climate.…)");
    if (config.schedule && !(config.schedule in KINDS)) throw new Error("`schedule` must be heat, cool or range");
    this._config = config;
    this._kind = config.schedule || null; // null: follow the thermostat's mode
    this._open = null; // the day being edited
    this._pinned = null; // the temperature schedule being edited, fixed while a day is open
    this._rows = null; // that day's editable rows, temperature and sensor
    this._copyTo = null; // days ticked for "copy to"
    this._expanded = null; // the sensor row whose sensor picker is open
    this._focus = null; // {kind, time}: the entry tapped on a bar, to highlight in the editor
    this._busy = false;
    this._error = null;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("keydown", (ev) => this._key(ev));
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    // Redraw only when something shown changes, not on every temperature reading.
    const s = hass.states[this._config.entity];
    const a = s ? s.attributes : {};
    const key = s ? JSON.stringify([s.state, a.friendly_name, a.schedule, a.sensor_schedule]) : "";
    if (key === this._shown) {
      this._updateReadings();
      return;
    }
    this._shown = key;
    this._moved = null; // the schedule changed: show what's stored
    // Don't throw away edits the user hasn't saved yet.
    if (this._rows && this._rows.some((r) => r.dirty)) return;
    this._rows = null;
    this._render();
  }

  connectedCallback() {
    this._timer = setInterval(() => this._tick(), 60 * 1000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  _tick() {
    const now = new Date();
    const today = DAYS[(now.getDay() + 6) % 7];
    const marker = this.shadowRoot && this.shadowRoot.querySelector(".now");
    if (marker && marker.closest(".row").dataset.day === today) {
      marker.style.left = `${((now.getHours() * 60 + now.getMinutes()) / (24 * 60)) * 100}%`;
    } else if (!this._open) {
      this._render(); // a new day: move the marker (not while editing, to keep the inputs)
    }
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  // A typed time: "5:30p", "5:30 PM", "530pm", "17:30", "1730", "7" (7:00). Minutes since midnight, or null.
  static parseTime(text) {
    const m = String(text).trim().toLowerCase().replace(/[\s.]/g, "").match(/^(\d{1,2})(?::?(\d{2}))?(?:([ap])m?)?$/);
    if (!m) return null;
    let h = Number(m[1]);
    const min = m[2] ? Number(m[2]) : 0;
    if (min > 59) return null;
    if (m[3]) {
      if (h < 1 || h > 12) return null;
      h = (h % 12) + (m[3] === "p" ? 12 : 0);
    } else if (h > 23) return null;
    return h * 60 + min;
  }

  static getConfigElement() {
    return document.createElement("protonest-schedule-card-editor");
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find(
      (id) => id.startsWith("climate.") && hass.states[id].attributes.schedule
    );
    return { entity: entity || "" };
  }

  // --- data -------------------------------------------------------------------------

  get _state() {
    return this._hass && this._hass.states[this._config.entity];
  }

  get _schedule() {
    const s = this._state;
    return s && s.attributes.schedule;
  }

  get _sensorSchedule() {
    const s = this._state;
    return s && s.attributes.sensor_schedule;
  }

  // A sensor schedule only makes sense with paired sensors to choose from.
  get _hasSensors() {
    const ss = this._sensorSchedule;
    return !!(ss && ss.options && ss.options.some((o) => o.id !== THERMOSTAT));
  }

  // The temperature schedule shown: heat, cool or range.
  get _tempKind() {
    if (this._pinned) return this._pinned;
    if (this._kind) return this._kind;
    const s = this._state;
    const fromMode = s && MODE_KIND[s.state];
    if (fromMode) return fromMode;
    const sched = this._schedule;
    return (sched && Object.keys(KINDS).find((k) => sched[k] && sched[k].length)) || "heat";
  }

  get _unit() {
    return (this._schedule && this._schedule.unit) || "°F";
  }

  get _fahrenheit() {
    return this._unit.includes("F");
  }

  _entries(kind, day) {
    let list = kind === SENSORS
      ? (this._sensorSchedule && this._sensorSchedule.entries) || []
      : (this._schedule && this._schedule[kind]) || [];
    // A dropped entry shows at its new time while the save goes through.
    const mv = this._moved;
    if (mv && mv.kind === kind) list = list.map((e) => (e.day === mv.day && e.time === mv.from ? { ...e, time: mv.to } : e));
    return list.filter((e) => !day || e.day === day).sort(byTime);
  }

  _values(entry, kind) {
    if (kind === SENSORS) return { sensors: [...entry.sensors] };
    return kind === "range"
      ? { target_temp_low: entry.target_temp_low, target_temp_high: entry.target_temp_high }
      : { temperature: entry.temperature };
  }

  _fmt(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "–";
    return this._fahrenheit ? String(Math.round(value)) : String(Math.round(value * 2) / 2);
  }

  // Sensor schedule options: [{id, name, entity_id, color}], "thermostat" first.
  get _options() {
    const opts = (this._sensorSchedule && this._sensorSchedule.options) || [];
    let n = 0;
    return opts.map((o) => ({ ...o, color: o.id === THERMOSTAT ? THERMOSTAT_COLOR : SENSOR_COLORS[n++ % SENSOR_COLORS.length] }));
  }

  _option(id) {
    return this._options.find((o) => o.id === id) || { id, name: id, color: THERMOSTAT_COLOR };
  }

  _sensorsLabel(ids) {
    return ids.length > 1 ? `Avg: ${ids.map((i) => this._option(i).name).join(" + ")}` : this._option(ids[0]).name;
  }

  // A time of day the way Home Assistant is set to show times (12 or 24 hour).
  _fmtTime(mins) {
    const loc = (this._hass && this._hass.locale) || {};
    const opts = { hour: "numeric", minute: "2-digit" };
    // hourCycle, not hour12: with hour12 false, some locales show midnight as 24:00.
    if (loc.time_format === "12") opts.hourCycle = "h12";
    else if (loc.time_format === "24") opts.hourCycle = "h23";
    try {
      const lang = loc.time_format === "system" ? undefined : loc.language;
      return new Date(2000, 0, 1, Math.floor(mins / 60), mins % 60).toLocaleTimeString(lang, opts);
    } catch (err) {
      return hhmm(mins);
    }
  }

  _reading(entityId) {
    const st = entityId && this._hass && this._hass.states[entityId];
    if (!st || Number.isNaN(Number(st.state))) return "";
    return `${this._fmt(Number(st.state))}${st.attributes.unit_of_measurement || ""}`;
  }

  // --- actions ----------------------------------------------------------------------

  async _call(kind, service, data) {
    await this._hass.callService("protonest", service, { schedule: kind, ...data }, {
      entity_id: this._config.entity,
    });
  }

  async _setSensorEntries(list) {
    const entries = list.map((e) => ({ day: e.day, time: e.time, sensors: e.sensors }));
    await this._hass.callService("protonest", "set_sensor_schedule", { entries }, { entity_id: this._config.entity });
  }

  async _run(work) {
    this._busy = true;
    this._error = null;
    this._render();
    try {
      await work();
    } catch (err) {
      this._error = (err && err.message) || String(err);
    }
    this._busy = false;
    this._render();
  }

  _openDay(day) {
    if (this._busy) return;
    if (this._open === day) {
      this._open = null;
      this._pinned = null;
    } else {
      this._pinned = this._tempKind; // keep the schedule shown now, whatever the mode does
      this._open = day;
    }
    this._rows = null;
    this._copyTo = null;
    this._error = null;
    this._render();
  }

  // Esc closes "Copy to", then the day; Enter saves the row being edited.
  _key(ev) {
    if (this._busy) return;
    if (ev.key === "Escape" && ev.target && ev.target.dataset && ev.target.dataset.field === "time" && this._closeTimes(ev.target)) {
      ev.preventDefault();
    } else if (ev.key === "Escape" && (this._copyTo || this._open)) {
      ev.preventDefault();
      if (this._copyTo) {
        this._copyTo = null;
        this._render();
      } else this._openDay(this._open);
    } else if ((ev.key === "ArrowUp" || ev.key === "ArrowDown") && ev.target && ev.target.dataset && ev.target.dataset.field === "time") {
      ev.preventDefault();
      this._stepTime(ev.target, ev.key === "ArrowUp" ? 1 : -1);
    } else if (ev.key === "Enter" && ev.target && ev.target.dataset && ev.target.dataset.field) {
      const i = Number(ev.target.dataset.i);
      if (this._rows && this._rows[i] && this._rows[i].dirty) {
        ev.preventDefault();
        this._saveRow(i);
      }
    }
  }

  _tidyTime(input) {
    const row = this._rows && this._rows[Number(input.dataset.i)];
    if (!row || !row.time) return;
    row.time = quarter(row.time);
    row.timeText = null;
    input.value = this._fmtTime(minutes(row.time));
  }

  // ↑ / ↓ in a time field: 15 minutes later / earlier, wrapping around midnight.
  _stepTime(input, dir) {
    const row = this._rows && this._rows[Number(input.dataset.i)];
    if (!row) return;
    const at = row.time ? minutes(quarter(row.time)) : 8 * 60 - dir * STEP_MINUTES;
    this._setTime(input, hhmm((at + dir * STEP_MINUTES + 24 * 60) % (24 * 60)));
  }

  // Keep the clock's list on the typed time, so picking any time registers as a change.
  _syncPick(input, time) {
    const box = input.closest && input.closest(".when");
    const pick = box && box.querySelector("select");
    if (pick) pick.value = time ? quarter(time) : "";
    const list = box && box.querySelector(".times");
    if (list) this._markTime(list, time);
  }

  // While a time field has focus, a list of quarter hours drops down under it;
  // typing still works, and moves the list to what's typed.
  _openTimes(input) {
    const box = input.closest(".when");
    if (!box || box.querySelector(".times")) return;
    const list = document.createElement("div");
    list.className = "times";
    list.setAttribute("role", "listbox");
    let html = "";
    for (let m = 0; m < 24 * 60; m += STEP_MINUTES) html += `<div class="opt" role="option" data-time="${hhmm(m)}">${esc(this._fmtTime(m))}</div>`;
    list.innerHTML = html;
    // mousedown, not click: keep the focus in the field.
    list.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      const opt = ev.target.closest(".opt");
      if (!opt) return;
      this._setTime(input, opt.dataset.time);
      this._closeTimes(input);
    });
    box.appendChild(list);
    const row = this._rows && this._rows[Number(input.dataset.i)];
    this._markTime(list, row && row.time ? row.time : "08:00", !(row && row.time));
  }

  _closeTimes(input) {
    const box = input.closest && input.closest(".when");
    const list = box && box.querySelector(".times");
    if (list) list.remove();
    return !!list;
  }

  // Highlight a time in the list (unless only scrolling to it) and bring it into view.
  _markTime(list, time, scrollOnly = false) {
    const q = time ? quarter(time) : null;
    let hit = null;
    list.querySelectorAll(".opt").forEach((o) => {
      const on = !scrollOnly && o.dataset.time === q;
      o.classList.toggle("on", on);
      if (o.dataset.time === q) hit = o;
    });
    if (hit) list.scrollTop = hit.offsetTop - list.clientHeight / 2 + hit.offsetHeight / 2;
  }

  _setTime(input, time) {
    const row = this._rows && this._rows[Number(input.dataset.i)];
    if (!row) return;
    row.time = time;
    row.timeText = null;
    this._syncPick(input, time);
    row.dirty = true;
    input.value = this._fmtTime(minutes(row.time));
    input.classList.remove("bad");
    const entry = input.closest && input.closest(".entry");
    if (entry) entry.classList.add("dirty");
  }

  // Tapping an entry on a bar opens its day with that entry highlighted.
  _openEntry(day, kind, time) {
    if (this._busy) return;
    if (this._open !== day) this._openDay(day);
    this._focus = { kind, time };
    this._scroll = true;
    this._render();
  }

  _toggleSchedule() {
    const ss = this._sensorSchedule;
    if (!ss || !ss.switch) return;
    this._run(() => this._hass.callService("switch", ss.enabled ? "turn_off" : "turn_on", {}, { entity_id: ss.switch }));
  }

  _ensureRows() {
    if (this._rows || !this._open) return;
    const kinds = [this._tempKind, ...(this._hasSensors ? [SENSORS] : [])];
    this._rows = kinds.flatMap((kind) =>
      this._entries(kind, this._open).map((e) => ({ kind, orig: e.time, time: e.time, ...this._values(e, kind), dirty: false }))
    );
  }

  _addRow(kind) {
    const same = this._rows.filter((r) => r.kind === kind);
    const last = same[same.length - 1];
    const time = last ? hhmm(Math.min(minutes(last.time) + 60, 23 * 60 + 45)) : "08:00";
    let values;
    if (last) {
      values = this._values(last, kind);
    } else if (kind === SENSORS) {
      const first = this._options.find((o) => o.id !== THERMOSTAT);
      values = { sensors: [first ? first.id : THERMOSTAT] };
    } else {
      const s = this._state;
      const sameUnit = this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature === this._unit;
      const guess = (a) => (sameUnit && s && s.attributes[a] != null ? s.attributes[a] : null);
      values = kind === "range"
        ? { target_temp_low: guess("target_temp_low") ?? (this._fahrenheit ? 68 : 20), target_temp_high: guess("target_temp_high") ?? (this._fahrenheit ? 76 : 24.5) }
        : { temperature: guess("temperature") ?? (this._fahrenheit ? 70 : 21) };
    }
    const row = { kind, orig: null, time, ...values, dirty: true };
    this._rows.push(row);
    if (kind === SENSORS) this._expanded = row;
    this._render();
  }

  // Sensor rows: the thermostat alone, one sensor, or several to average.
  _pick(i, id) {
    const row = this._rows[i];
    let picked = row.sensors;
    if (id === THERMOSTAT || picked.includes(THERMOSTAT)) picked = [id];
    else if (picked.includes(id)) picked = picked.length > 1 ? picked.filter((x) => x !== id) : picked;
    else picked = [...picked, id];
    row.sensors = picked;
    row.dirty = true;
    this._render();
  }

  _expand(i) {
    const row = this._rows[i];
    this._expanded = this._expanded === row ? null : row;
    this._render();
  }

  _invalid(row) {
    if (!/^\d{1,2}:\d{2}/.test(row.time || "")) return "Enter a time, like 5:30 PM or 17:30.";
    if (row.kind === SENSORS) return row.sensors && row.sensors.length ? null : "Choose a sensor.";
    if (Object.values(this._values(row, row.kind)).some((v) => v === null || v === undefined || Number.isNaN(v)))
      return "Enter a temperature.";
    return null;
  }

  _forget(row) {
    const i = this._rows ? this._rows.indexOf(row) : -1;
    if (i >= 0) this._rows.splice(i, 1);
  }

  _settled() {
    // Show what was stored, unless other rows still have unsaved edits.
    if (this._rows && !this._rows.some((r) => r.dirty)) this._rows = null;
  }

  _saveRow(i) {
    const row = this._rows[i];
    const problem = this._invalid(row);
    if (problem) {
      this._error = problem;
      this._render();
      return;
    }
    const [kind, day] = [row.kind, this._open];
    const time = quarter(row.time);
    const values = this._values(row, kind);
    this._run(async () => {
      if (kind === SENSORS) {
        const others = this._entries(SENSORS).filter((e) => !(e.day === day && e.time === row.orig));
        await this._setSensorEntries([...others, { day, time, ...values }]);
      } else if (row.orig === null) {
        await this._call(kind, "schedule_add", { day, time, ...values });
      } else {
        await this._call(kind, "schedule_update", {
          day, time: row.orig, ...(time !== row.orig ? { new_time: time } : {}), ...values,
        });
      }
      Object.assign(row, { orig: time, time, timeText: null, dirty: false });
      this._settled();
    });
  }

  _deleteRow(i) {
    const row = this._rows[i];
    if (row.orig === null) {
      this._forget(row);
      this._render();
      return;
    }
    const [kind, day] = [row.kind, this._open];
    this._run(async () => {
      if (kind === SENSORS) {
        await this._setSensorEntries(this._entries(SENSORS).filter((e) => !(e.day === day && e.time === row.orig)));
      } else {
        await this._call(kind, "schedule_remove", { day, time: row.orig });
      }
      this._forget(row);
      this._settled();
    });
  }

  // "Copy to…" shortcuts: select those days, other than the one being copied.
  // They only fill in the selection, which can still be adjusted before copying.
  _copySet(set) {
    this._copyTo = new Set(DAY_SETS[set].filter((d) => d !== this._open));
    this._render();
  }

  // Copies the whole day: the temperature schedule shown and the sensor schedule.
  _copy() {
    const [kind, from] = [this._tempKind, this._open];
    const withSensors = this._hasSensors;
    const targets = DAYS.filter((d) => this._copyTo && this._copyTo.has(d));
    if (!targets.length) return;
    this._run(async () => {
      // Temperatures: change only what differs, as each call is a separate write to the thermostat.
      const source = this._entries(kind, from);
      const same = (a, b) => JSON.stringify(this._values(a, kind)) === JSON.stringify(this._values(b, kind));
      for (const day of targets) {
        const existing = new Map(this._entries(kind, day).map((e) => [e.time, e]));
        for (const e of source) {
          const old = existing.get(e.time);
          existing.delete(e.time);
          if (!old) await this._call(kind, "schedule_add", { day, time: e.time, ...this._values(e, kind) });
          else if (!same(old, e)) await this._call(kind, "schedule_update", { day, time: e.time, ...this._values(e, kind) });
        }
        for (const time of existing.keys()) await this._call(kind, "schedule_remove", { day, time });
      }
      // Sensors: one save, the whole schedule with those days replaced.
      if (withSensors) {
        const sensorSource = this._entries(SENSORS, from);
        const kept = this._entries(SENSORS).filter((e) => !targets.includes(e.day));
        await this._setSensorEntries([...kept, ...targets.flatMap((day) => sensorSource.map((e) => ({ ...e, day })))]);
      }
      this._copyTo = null;
    });
  }

  // --- dragging ---------------------------------------------------------------------
  // A temperature chip, or the handle at the start of a sensor band, drags along its
  // day in quarter hours, between its neighbours; dropping it saves the new time.
  // A press that doesn't move is a tap, which opens the entry.

  _dragStart(ev, el) {
    if (this._busy || ev.button > 0 || this._drag) return;
    if (this._rows && this._rows.some((r) => r.dirty)) {
      this._error = `Save or discard the changes to ${DAY_FULL[this._open]} first.`;
      this._render();
      return;
    }
    const { day, kind, time } = el.dataset;
    this._error = null;
    const at = minutes(time);
    const others = this._entries(kind, day).map((e) => minutes(e.time)).filter((m) => m !== at);
    this._drag = {
      el, day, kind, time, at, to: at, id: ev.pointerId, x: ev.clientX, moved: false,
      track: el.closest(".bars"),
      lo: Math.max(0, ...others.filter((m) => m < at).map((m) => m + STEP_MINUTES)),
      hi: Math.min(24 * 60 - STEP_MINUTES, ...others.filter((m) => m > at).map((m) => m - STEP_MINUTES)),
    };
    if (el.setPointerCapture) el.setPointerCapture(ev.pointerId);
  }

  _dragMove(ev) {
    const d = this._drag;
    if (!d || ev.pointerId !== d.id) return;
    if (!d.moved && Math.abs(ev.clientX - d.x) < DRAG_PX) return;
    if (!d.moved) {
      d.moved = true;
      d.el.classList.add("dragging");
      d.tip = document.createElement("span");
      d.tip.className = "tip";
      d.track.appendChild(d.tip);
    }
    ev.preventDefault();
    const rect = d.track.getBoundingClientRect();
    const raw = ((ev.clientX - rect.left) / rect.width) * 24 * 60;
    d.to = Math.min(d.hi, Math.max(d.lo, Math.round(raw / STEP_MINUTES) * STEP_MINUTES));
    this._dragShow(d);
  }

  _dragShow(d) {
    const pct = (m) => `${(m / (24 * 60)) * 100}%`;
    d.el.style.left = pct(d.to);
    d.tip.style.left = pct(d.to);
    d.tip.textContent = this._fmtTime(d.to);
    if (d.kind !== SENSORS) return;
    // Resize the band that starts here and the one before it.
    const band = d.track.querySelector(`.band[data-day="${d.day}"][data-time="${d.time}"]`);
    if (!band) return;
    band.style.left = pct(d.to);
    band.style.width = pct(Number(band.dataset.end) - d.to);
    const prev = band.previousElementSibling;
    if (prev && prev.classList.contains("band")) prev.style.width = pct(d.to - Number(prev.dataset.start));
  }

  _dragEnd(ev, cancelled = false) {
    const d = this._drag;
    if (!d || ev.pointerId !== d.id) return;
    this._drag = null;
    if (!d.moved) return; // a tap: the click opens the entry
    this._dragged = true; // swallow the click that follows the drop
    setTimeout(() => (this._dragged = false), 0);
    if (cancelled || d.to === d.at) {
      this._render();
      return;
    }
    const time = hhmm(d.to);
    const save = d.kind === SENSORS
      ? () => this._setSensorEntries(this._entries(SENSORS).map((e) => (e.day === d.day && e.time === d.time ? { ...e, time } : e)))
      : ((entry) => () => this._call(d.kind, "schedule_update", { day: d.day, time: d.time, new_time: time, ...this._values(entry, d.kind) }))(
          this._entries(d.kind, d.day).find((e) => e.time === d.time));
    const moved = { kind: d.kind, day: d.day, from: d.time, to: time };
    this._moved = moved;
    clearTimeout(this._movedTimer);
    this._movedTimer = setTimeout(() => {
      // Never confirmed: go back to what's stored.
      if (this._moved !== moved) return;
      this._moved = null;
      this._render();
    }, 20 * 1000);
    this._run(async () => {
      try {
        await save();
      } catch (err) {
        this._moved = null;
        throw err;
      }
      this._settled();
    });
  }

  // --- rendering --------------------------------------------------------------------

  _chip(entry, kind) {
    const pct = (minutes(entry.time) / (24 * 60)) * 100;
    const edge = pct < 4 ? "start" : pct > 96 ? "end" : "";
    const [label, inner] =
      kind === "range"
        ? [`${this._fmt(entry.target_temp_low)}–${this._fmt(entry.target_temp_high)}`,
           `<b class="lo">${esc(this._fmt(entry.target_temp_low))}</b><b class="hi">${esc(this._fmt(entry.target_temp_high))}</b>`]
        : [this._fmt(entry.temperature), esc(this._fmt(entry.temperature))];
    return `<span class="chip ${kind} ${edge}" style="left:${pct}%" data-act="entry" data-drag data-day="${entry.day}" data-kind="${kind}" data-time="${esc(entry.time)}" title="${esc(this._fmtTime(minutes(entry.time)))} · ${esc(label)}${esc(this._unit)}">${inner}</span>`;
  }

  _fill(ids) {
    const colors = ids.map((i) => this._option(i).color);
    if (colors.length === 1) return colors[0];
    // Averaging: stripes of each sensor's colour.
    const stops = colors.map((c, i) => `${c} ${i * 6}px ${(i + 1) * 6}px`).join(", ");
    return `repeating-linear-gradient(135deg, ${stops})`;
  }

  // The sensor schedule as coloured bands: each entry runs until the next one,
  // so a day starts with whatever was in effect at the end of the day before.
  _bands(day) {
    const all = this._entries(SENSORS);
    if (!all.length) return "";
    const dayIdx = DAYS.indexOf(day);
    const before = all.filter((e) => DAYS.indexOf(e.day) < dayIdx);
    const carried = before.length ? before[before.length - 1] : all[all.length - 1];
    const starts = [{ at: 0, e: carried }, ...this._entries(SENSORS, day).map((e) => ({ at: minutes(e.time), e }))]
      .filter((s, i, list) => !(i === 0 && list[1] && list[1].at === 0));
    return starts
      .map((s, i) => {
        const end = i + 1 < starts.length ? starts[i + 1].at : 24 * 60;
        const label = this._sensorsLabel(s.e.sensors);
        const [left, width] = [(s.at / (24 * 60)) * 100, ((end - s.at) / (24 * 60)) * 100];
        return `<span class="band" style="left:${left}%;width:${width}%;background:${this._fill(s.e.sensors)}" data-act="entry" data-day="${s.e.day}" data-kind="${SENSORS}" data-time="${esc(s.e.time)}" data-start="${s.at}" data-end="${end}" title="${esc(this._fmtTime(minutes(s.e.time)))} · ${esc(label)}"><span>${esc(label)}</span></span>`;
      })
      .join("") +
      // A handle at the start of each of this day's entries, to drag its time.
      starts
        .filter((s) => s.e.day === day)
        .map((s) => `<span class="handle" style="left:${(s.at / (24 * 60)) * 100}%" data-act="entry" data-drag data-day="${day}" data-kind="${SENSORS}" data-time="${esc(s.e.time)}" title="Drag to change the time"></span>`)
        .join("");
  }

  _sensorHeader() {
    const ss = this._sensorSchedule;
    const legend = this._options
      .map((o) => `<span class="key"><i style="background:${o.color}"></i>${esc(o.name)} <span class="reading" data-entity="${esc(o.entity_id || "")}">${esc(this._reading(o.entity_id))}</span></span>`)
      .join("");
    const avg = this._options.filter((o) => o.id !== THERMOSTAT).slice(0, 2).map((o) => o.id);
    return `
      <div class="run">
        <button class="switch ${ss.enabled ? "on" : ""}" data-act="toggle-schedule" role="switch" aria-checked="${ss.enabled}" ${this._busy ? "disabled" : ""}><i></i></button>
        <span>Sensor schedule ${ss.enabled ? "on: Home Assistant switches the target sensor at each entry." : "off: the target sensor stays as it is."}</span>
      </div>
      <div class="legend">${legend}${avg.length > 1 ? `<span class="key avg"><i style="background:${this._fill(avg)}"></i>striped = average</span>` : ""}</div>`;
  }

  _updateReadings() {
    if (!this.shadowRoot) return;
    this.shadowRoot.querySelectorAll(".reading[data-entity]").forEach((el) => {
      el.textContent = this._reading(el.dataset.entity);
    });
  }

  _editor() {
    this._ensureRows();
    if (this._focus) {
      const { kind, time } = this._focus;
      this._rows.forEach((r) => {
        r.focus = r.kind === kind && r.orig === time;
        if (r.focus && kind === SENSORS) this._expanded = r;
      });
      this._focus = null;
    }
    const tempKind = this._tempKind;
    const step = this._fahrenheit ? 1 : 0.5;
    const [lo, hi] = this._fahrenheit ? [50, 90] : [10, 32];
    const dis = this._busy ? "disabled" : "";
    const num = (i, field, value) =>
      `<input class="temp" type="number" inputmode="decimal" min="${lo}" max="${hi}" step="${step}" value="${esc(value ?? "")}" data-i="${i}" data-field="${field}" ${dis}>`;
    const picks = (i, r) =>
      `<span class="picks">${this._options
        .map((o) => `<button class="pick ${r.sensors.includes(o.id) ? "on" : ""}" style="--c:${o.color}" data-act="pick" data-i="${i}" data-id="${esc(o.id)}" ${dis}>${esc(o.name)}</button>`)
        .join("")}</span>`;
    // Times are typed (see parseTime), shown in HA's time format once entered.
    const timeInput = (i, r) => {
      const text = r.timeText != null ? r.timeText : /^\d{1,2}:\d{2}/.test(r.time || "") ? this._fmtTime(minutes(r.time)) : "";
      // Beside it, a clock opens the quarter hours as a list (the system time wheel on a phone).
      const at = /^\d{1,2}:\d{2}/.test(r.time || "") ? minutes(quarter(r.time)) : null;
      const opts = [];
      for (let m = 0; m < 24 * 60; m += STEP_MINUTES) opts.push(`<option value="${hhmm(m)}" ${m === at ? "selected" : ""}>${esc(this._fmtTime(m))}</option>`);
      return `<span class="when">
          <input class="time" type="text" autocomplete="off" spellcheck="false" placeholder="5:30 PM" value="${esc(text)}" data-i="${i}" data-field="time" title="Type a time, like 5:30p or 17:30. ↑ ↓ change it by 15 minutes." ${dis}>
          <span class="clock" title="Pick a time"><svg viewBox="0 0 24 24"><path d="${CLOCK_ICON}"/></svg>
            <select data-pick-time="${i}" aria-label="Pick a time" ${dis}>${at === null ? '<option value="" selected></option>' : ""}${opts.join("")}</select></span>
        </span>`;
    };
    // A sensor row shows what it picks; tapping that opens the picker below it.
    const summary = (i, r) => `
      <button class="summary" data-act="expand" data-i="${i}" aria-expanded="${this._expanded === r}" ${dis}>
        <i style="background:${this._fill(r.sensors)}"></i><span>${esc(this._sensorsLabel(r.sensors))}</span><b>${this._expanded === r ? "▴" : "▾"}</b>
      </button>`;
    const row = (r, i) => `
      <div class="entry ${r.dirty ? "dirty" : ""} ${r.focus ? "focus" : ""}">
        ${timeInput(i, r)}
        ${r.kind === SENSORS
          ? summary(i, r)
          : `<span class="temps">${r.kind === "range" ? `${num(i, "target_temp_low", r.target_temp_low)}<span class="dash">–</span>${num(i, "target_temp_high", r.target_temp_high)}` : num(i, "temperature", r.temperature)}<span class="unit">${esc(this._unit)}</span></span>`}
        <span class="row-actions">
          <button class="icon save" data-act="save" data-i="${i}" title="Save" ${dis}>✓</button>
          <button class="icon delete" data-act="delete" data-i="${i}" title="Delete" ${dis}>✕</button>
        </span>
      </div>
      ${r.kind === SENSORS && this._expanded === r
        ? `<div class="picker">${picks(i, r)}<div class="hint">One sensor, or two or more to average. The thermostat's own sensor can't be averaged.</div></div>`
        : ""}`;
    const section = (kind, title, empty) => {
      const rows = this._rows.map((r, i) => (r.kind === kind ? row(r, i) : "")).join("");
      return `
        <div class="section">
          <div class="section-head"><span>${esc(title)}</span><button class="text" data-act="add" data-kind="${kind}" ${dis}>+ Add</button></div>
          ${rows || `<div class="empty">${esc(empty)}</div>`}
        </div>`;
    };
    const what = this._hasSensors ? `${KINDS[tempKind].toLowerCase()} and sensor schedules` : `${KINDS[tempKind].toLowerCase()} schedule`;
    const n = this._copyTo ? this._copyTo.size : 0;
    const copy = this._copyTo
      ? `<div class="copy">
          <span>Copy ${DAY_NAMES[this._open]} to</span>
          ${DAYS.filter((d) => d !== this._open)
            .map((d) => `<button class="pill ${this._copyTo.has(d) ? "on" : ""}" data-act="copy-day" data-day="${d}" ${dis}>${DAY_NAMES[d]}</button>`)
            .join("")}
          <select class="sets" aria-label="Select days" ${dis}>
            <option value="" selected>Select…</option>
            ${Object.keys(DAY_SETS).map((k) => `<option value="${k}">${DAY_SET_NAMES[k]}</option>`).join("")}
          </select>
          <button class="text go" data-act="copy-go" title="Replaces those days' ${esc(what)}." ${n ? "" : "disabled"} ${dis}>${n ? `Replace ${n} day${n === 1 ? "" : "s"}` : "Copy"}</button>
        </div>`
      : "";
    return `
      <div class="editor">
        <div class="editor-head"><span>${DAY_FULL[this._open]}</span><button class="icon" data-act="close" title="Close" ${dis}>✕</button></div>
        ${section(tempKind, `${KINDS[tempKind]} temperatures`, `No ${KINDS[tempKind].toLowerCase()} entries on ${DAY_NAMES[this._open]}.`)}
        ${this._hasSensors ? section(SENSORS, "Sensors", `No sensor entries on ${DAY_NAMES[this._open]}.`) : ""}
        <div class="actions">
          <button class="text" data-act="copy" ${dis || this._rows.length ? "" : "disabled"} ${dis}>${this._copyTo ? "Cancel copy" : "Copy to other days…"}</button>
        </div>
        ${copy}
      </div>`;
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    if (this._drag && this._drag.moved) return; // redrawn on the drop
    const s = this._state;
    const title = this._config.title || (s && s.attributes.friendly_name) || this._config.entity;
    let body;
    if (!s) {
      body = `<div class="warn">Entity not found: ${esc(this._config.entity)}</div>`;
    } else if (!this._schedule) {
      body = `<div class="warn">${esc(this._config.entity)} has no schedule (is it a ProtoNest thermostat, version 0.1.3 or later?)</div>`;
    } else {
      const kind = this._tempKind;
      const sensors = this._hasSensors;
      const now = new Date();
      const today = DAYS[(now.getDay() + 6) % 7];
      const nowPct = ((now.getHours() * 60 + now.getMinutes()) / (24 * 60)) * 100;
      const rows = DAYS.map((day) => `
          <div class="row ${this._open === day ? "open" : ""}" data-act="day" data-day="${day}" role="button" tabindex="0">
            <span class="day ${day === today ? "today" : ""}">${DAY_NAMES[day]}</span>
            <div class="bars">
              <div class="track">${this._entries(kind, day).map((e) => this._chip(e, kind)).join("")}</div>
              ${sensors ? `<div class="track bands">${this._bands(day)}</div>` : ""}
              ${day === today ? `<span class="now" style="left:${nowPct}%"></span>` : ""}
            </div>
          </div>
          ${this._open === day ? this._editor() : ""}`).join("");
      const tabs = Object.entries(KINDS)
        .map(([k, label]) => `<button class="tab ${k === kind ? "on" : ""}" data-act="kind" data-kind="${k}" ${this._busy ? "disabled" : ""}>${label}</button>`)
        .join("");
      body = `
        <div class="tabs">${tabs}</div>
        ${sensors ? this._sensorHeader() : ""}
        <div class="axis"><span></span><div class="hours">${[0, 6, 12, 18, 24].map((h) => `<span style="left:${(h / 24) * 100}%">${h === 24 ? "" : h}</span>`).join("")}</div></div>
        ${rows}
        ${this._error ? `<div class="error">${esc(this._error)}</div>` : ""}`;
    }
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card><div class="head">${esc(title)} schedule</div><div class="body">${body}</div></ha-card>`;
    this._wire();
  }

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-act]").forEach((el) => {
      const act = el.dataset.act;
      const handler = (ev) => {
        if (act === "day") {
          if (ev.type === "keydown" && ev.key !== "Enter" && ev.key !== " ") return;
          ev.preventDefault();
          this._openDay(el.dataset.day);
        } else if (act === "entry") {
          if (!this._dragged) this._openEntry(el.dataset.day, el.dataset.kind, el.dataset.time);
        } else if (act === "kind") {
          if (this._busy) return;
          this._kind = el.dataset.kind;
          if (this._open) this._pinned = this._kind;
          this._rows = null;
          this._copyTo = null;
          this._render();
        } else if (act === "add") this._addRow(el.dataset.kind);
        else if (act === "save") this._saveRow(Number(el.dataset.i));
        else if (act === "delete") this._deleteRow(Number(el.dataset.i));
        else if (act === "expand") this._expand(Number(el.dataset.i));
        else if (act === "pick") this._pick(Number(el.dataset.i), el.dataset.id);
        else if (act === "toggle-schedule") this._toggleSchedule();
        else if (act === "close") this._openDay(this._open);
        else if (act === "copy") {
          this._copyTo = this._copyTo ? null : new Set();
          this._render();
        } else if (act === "copy-day") {
          const d = el.dataset.day;
          this._copyTo.has(d) ? this._copyTo.delete(d) : this._copyTo.add(d);
          this._render();
        } else if (act === "copy-go") this._copy();
        ev.stopPropagation();
      };
      el.addEventListener("click", handler);
      if (act === "day") el.addEventListener("keydown", handler);
    });
    root.querySelectorAll("[data-drag]").forEach((el) => {
      el.addEventListener("pointerdown", (ev) => this._dragStart(ev, el));
      el.addEventListener("pointermove", (ev) => this._dragMove(ev));
      el.addEventListener("pointerup", (ev) => this._dragEnd(ev));
      el.addEventListener("pointercancel", (ev) => this._dragEnd(ev, true));
    });
    root.querySelectorAll("select[data-pick-time]").forEach((pick) => {
      pick.addEventListener("change", () => {
        const input = pick.closest(".when").querySelector("input.time");
        if (pick.value) this._setTime(input, pick.value);
      });
    });
    const sets = root.querySelector("select.sets");
    if (sets) sets.addEventListener("change", () => sets.value && this._copySet(sets.value));
    root.querySelectorAll("[data-field]").forEach((input) => {
      const onEdit = () => {
        const row = this._rows[Number(input.dataset.i)];
        const field = input.dataset.field;
        if (field === "time") {
          const at = ProtoNestScheduleCard.parseTime(input.value);
          row.timeText = input.value;
          row.time = at === null ? "" : hhmm(at);
          input.classList.toggle("bad", at === null && input.value.trim() !== "");
          this._syncPick(input, row.time);
        } else row[field] = input.value === "" ? null : Number(input.value);
        if (!row.dirty) {
          row.dirty = true;
          const entry = input.closest(".entry");
          entry.classList.add("dirty");
        }
      };
      input.addEventListener("input", onEdit);
      input.addEventListener("change", onEdit);
      // Leaving a time field rounds it to the quarter hour and shows it in HA's format.
      if (input.dataset.field === "time") {
        input.addEventListener("focus", () => this._openTimes(input));
        input.addEventListener("click", () => this._openTimes(input)); // reopens it after a pick
        input.addEventListener("blur", () => {
          this._closeTimes(input);
          this._tidyTime(input);
        });
      }
    });
    if (this._scroll) {
      this._scroll = false;
      const focused = root.querySelector(".entry.focus");
      if (focused && focused.scrollIntoView) focused.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }
}

const STYLE = `
  :host { --heat: var(--state-climate-heat-color, #ff8100); --cool: var(--state-climate-cool-color, #2b9af9); }
  ha-card { padding: 12px 16px 16px; }
  .head { font-size: 1.15em; font-weight: 500; color: var(--primary-text-color); margin-bottom: 8px; }
  .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .tab { flex: 1; border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
         border-radius: 16px; padding: 5px 4px; font: inherit; font-size: 0.85em; cursor: pointer; white-space: nowrap; }
  .tab.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .axis, .row { display: grid; grid-template-columns: 38px 1fr; align-items: center; }
  .hours { position: relative; height: 14px; margin: 0 14px; font-size: 0.7em; color: var(--secondary-text-color); }
  .hours span { position: absolute; transform: translateX(-50%); }
  .row { min-height: 34px; padding: 2px 0; cursor: pointer; border-radius: 8px; }
  .row:hover, .row.open { background: var(--secondary-background-color); }
  .row:focus-visible { outline: 2px solid var(--primary-color); }
  .day { font-size: 0.85em; color: var(--secondary-text-color); padding-left: 4px; }
  .day.today { color: var(--primary-color); font-weight: 600; }
  .bars { position: relative; margin: 0 14px; }
  .track { position: relative; height: 26px;
           background: linear-gradient(var(--divider-color), var(--divider-color)) center / 100% 2px no-repeat; }
  .track.bands { height: 16px; margin-top: 2px; background: none; }
  .now { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--primary-text-color); opacity: 0.55; z-index: 2; }
  .chip { position: absolute; top: 3px; transform: translateX(-50%); min-width: 22px; padding: 2px 5px;
          border-radius: 10px; font-size: 0.75em; line-height: 16px; text-align: center; color: #fff; white-space: nowrap;
          box-shadow: 0 0 0 2px var(--card-background-color, #fff); }
  .chip.start { transform: none; } .chip.end { transform: translateX(-100%); }
  .chip:hover { z-index: 1; }
  .chip.heat { background: var(--heat); } .chip.cool { background: var(--cool); }
  .chip.range { display: flex; padding: 0; overflow: hidden; }
  .chip.range b { font-weight: inherit; padding: 2px 5px; }
  .chip.range .lo { background: var(--heat); } .chip.range .hi { background: var(--cool); }
  .band { position: absolute; top: 0; bottom: 0; box-sizing: border-box; border-right: 2px solid var(--card-background-color, #fff);
          border-radius: 3px; overflow: hidden; color: #fff; font-size: 0.65em; line-height: 16px; }
  .band span { display: block; padding: 0 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
               text-shadow: 0 0 3px rgba(0, 0, 0, 0.6); }
  .run { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; font-size: 0.85em; color: var(--secondary-text-color); }
  .switch { flex: none; position: relative; width: 36px; height: 20px; border-radius: 10px; border: none; padding: 0;
            background: var(--switch-unchecked-track-color, var(--disabled-color, #bdbdbd)); }
  .switch i { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: left 0.15s; }
  .switch.on { background: var(--primary-color); } .switch.on i { left: 18px; }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-bottom: 8px; font-size: 0.8em; color: var(--primary-text-color); }
  .key { display: inline-flex; align-items: center; gap: 4px; }
  .key i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .key .reading, .key.avg { color: var(--secondary-text-color); }
  .editor { margin: 4px 0 10px; padding: 8px 10px; border-radius: 8px; background: var(--secondary-background-color); }
  .section + .section { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--divider-color); }
  .section-head { display: flex; align-items: center; justify-content: space-between; font-size: 0.85em; font-weight: 500;
                  color: var(--secondary-text-color); }
  .entry { display: flex; align-items: center; gap: 8px; padding: 4px 6px; margin: 0 -6px; border-radius: 6px; }
  .entry.focus { background: var(--card-background-color, #fff); box-shadow: inset 3px 0 0 var(--primary-color); }
  .row-actions { margin-left: auto; display: flex; flex: none; }
  .entry:not(.dirty) .save { visibility: hidden; }
  .chip, .band { cursor: pointer; }
  .chip { touch-action: none; cursor: grab; }
  .chip.dragging { z-index: 4; cursor: grabbing; transform: translateX(-50%); box-shadow: 0 0 0 2px var(--primary-color); }
  .handle { position: absolute; top: -2px; bottom: -2px; width: 14px; transform: translateX(-50%); z-index: 3;
            cursor: ew-resize; touch-action: none; }
  .handle::after { content: ""; position: absolute; left: 5px; top: 0; bottom: 0; width: 4px; border-radius: 2px;
                   background: var(--card-background-color, #fff); box-shadow: 0 0 0 1px var(--divider-color); opacity: 0; transition: opacity 0.1s; }
  .bars:hover .handle::after, .handle.dragging::after { opacity: 1; }
  .tip { position: absolute; bottom: 100%; transform: translateX(-50%); margin-bottom: 2px; z-index: 5; white-space: nowrap;
         padding: 1px 6px; border-radius: 4px; font-size: 0.75em; background: var(--primary-text-color); color: var(--card-background-color, #fff); }
  .editor-head { display: flex; align-items: center; justify-content: space-between; font-weight: 500;
                 color: var(--primary-text-color); margin: -2px -4px 4px 0; }
  .summary { flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px; text-align: left;
             border: 1px solid var(--divider-color); border-radius: 6px; padding: 4px 8px;
             background: var(--card-background-color, #fff); color: var(--primary-text-color); }
  .summary i { flex: none; width: 12px; height: 12px; border-radius: 3px; }
  .summary span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .summary b { font-weight: normal; color: var(--secondary-text-color); }
  .entry.dirty .summary { border-color: var(--primary-color); }
  .picker { padding: 2px 0 8px; }
  .picker .hint { margin-top: 4px; }
  .entry.dirty .time, .entry.dirty .temp { border-color: var(--primary-color); }
  input { font: inherit; color: var(--primary-text-color); background: var(--card-background-color, #fff);
          border: 1px solid var(--divider-color); border-radius: 6px; padding: 4px 6px; }
  input:disabled { opacity: 0.6; }
  select { font: inherit; color: var(--primary-text-color); background: var(--card-background-color, #fff);
           border: 1px solid var(--divider-color); border-radius: 6px; padding: 4px 2px; }
  .time { width: 6.2em; flex: none; } .temp { width: 4em; }
  input.bad { border-color: var(--error-color, #db4437); }
  .when { position: relative; display: flex; align-items: center; gap: 2px; flex: none; }
  .times { position: absolute; top: 100%; left: 0; z-index: 10; margin-top: 2px; width: 7.5em; max-height: 220px; overflow-y: auto;
           background: var(--card-background-color, #fff); border: 1px solid var(--divider-color); border-radius: 6px;
           box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2); padding: 4px 0; }
  .times .opt { padding: 3px 10px; cursor: pointer; font-size: 0.9em; color: var(--primary-text-color); white-space: nowrap; }
  .times .opt:hover { background: var(--secondary-background-color); }
  .times .opt.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .clock { position: relative; display: flex; padding: 4px; border-radius: 50%; color: var(--secondary-text-color); }
  .clock:hover { color: var(--primary-color); background: var(--card-background-color, #fff); }
  .clock svg { width: 20px; height: 20px; fill: currentColor; }
  .clock select { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; padding: 0; border: none; }
  .temps { display: flex; align-items: center; gap: 4px; }
  .picks { display: flex; flex-wrap: wrap; gap: 6px; }
  .pick { border: 1px solid var(--c); background: none; color: var(--primary-text-color); border-radius: 12px;
          padding: 4px 10px; font-size: 0.85em; }
  .pick.on { background: var(--c); color: #fff; }
  .unit, .dash { color: var(--secondary-text-color); font-size: 0.85em; }
  button { font: inherit; cursor: pointer; }
  button:disabled { cursor: default; opacity: 0.5; }
  .icon { border: none; background: none; color: var(--secondary-text-color); font-size: 1em; padding: 4px 6px; border-radius: 50%; }
  .icon.save { color: var(--primary-color); font-weight: 700; }
  .icon.delete:hover { color: var(--error-color, #db4437); }
  .actions { display: flex; gap: 8px; margin-top: 8px; }
  .text { border: none; background: none; color: var(--primary-color); padding: 4px 6px; font-weight: 500; }
  .copy { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; font-size: 0.9em;
          color: var(--secondary-text-color); }
  .pill { border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); border-radius: 12px; padding: 2px 8px; }
  .pill.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .copy .go { margin-left: auto; }
  .hint { flex-basis: 100%; font-size: 0.85em; color: var(--secondary-text-color); }
  .empty { color: var(--secondary-text-color); font-size: 0.9em; padding: 4px 0; }
  .error { margin-top: 8px; color: var(--error-color, #db4437); font-size: 0.9em; }
  .warn { color: var(--warning-color, #ffa600); }
`;

// The card's visual editor, in the dashboard's "Edit card" dialog.
const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "climate", integration: "protonest" } } },
  { name: "schedule", selector: { select: { mode: "dropdown", options: [
    { value: "auto", label: "Follow the thermostat's mode" },
    ...Object.entries(KINDS).map(([value, label]) => ({ value, label })),
  ] } } },
  { name: "title", selector: { text: {} } },
];
const EDITOR_LABELS = { entity: "Thermostat", schedule: "Temperature schedule shown", title: "Title (optional)" };

class ProtoNestScheduleCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._config || !this._hass) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => EDITOR_LABELS[schema.name];
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const config = { ...ev.detail.value };
        if (config.schedule === "auto") delete config.schedule;
        if (!config.title) delete config.title;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = { schedule: "auto", ...this._config };
  }
}

customElements.define("protonest-schedule-card", ProtoNestScheduleCard);
customElements.define("protonest-schedule-card-editor", ProtoNestScheduleCardEditor);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "protonest-schedule-card",
  name: "ProtoNest schedule",
  description: "View and edit a Nest thermostat's weekly temperature schedule and its Home Assistant target sensor schedule.",
});
