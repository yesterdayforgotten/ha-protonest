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
    this._busy = false;
    this._error = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
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
    const list = kind === SENSORS
      ? (this._sensorSchedule && this._sensorSchedule.entries) || []
      : (this._schedule && this._schedule[kind]) || [];
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
    this._rows.push({ kind, orig: null, time, ...values, dirty: true });
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

  _invalid(row) {
    if (!/^\d{1,2}:\d{2}/.test(row.time || "")) return "Enter a time.";
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
      Object.assign(row, { orig: time, time, dirty: false });
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

  // --- rendering --------------------------------------------------------------------

  _chip(entry, kind) {
    const pct = (minutes(entry.time) / (24 * 60)) * 100;
    const edge = pct < 4 ? "start" : pct > 96 ? "end" : "";
    const [label, inner] =
      kind === "range"
        ? [`${this._fmt(entry.target_temp_low)}–${this._fmt(entry.target_temp_high)}`,
           `<b class="lo">${esc(this._fmt(entry.target_temp_low))}</b><b class="hi">${esc(this._fmt(entry.target_temp_high))}</b>`]
        : [this._fmt(entry.temperature), esc(this._fmt(entry.temperature))];
    return `<span class="chip ${kind} ${edge}" style="left:${pct}%" title="${esc(entry.time)} · ${esc(label)}${esc(this._unit)}">${inner}</span>`;
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
        return `<span class="band" style="left:${left}%;width:${width}%;background:${this._fill(s.e.sensors)}" title="${esc(s.e.time)} · ${esc(label)}"><span>${esc(label)}</span></span>`;
      })
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
    const row = (r, i) => `
      <div class="entry ${r.dirty ? "dirty" : ""} ${r.kind === SENSORS ? "sensors" : ""}">
        <input class="time" type="time" step="${STEP_MINUTES * 60}" value="${esc(r.time)}" data-i="${i}" data-field="time" ${dis}>
        ${r.kind === SENSORS
          ? picks(i, r)
          : `<span class="temps">${r.kind === "range" ? `${num(i, "target_temp_low", r.target_temp_low)}<span class="dash">–</span>${num(i, "target_temp_high", r.target_temp_high)}` : num(i, "temperature", r.temperature)}<span class="unit">${esc(this._unit)}</span></span>`}
        <button class="icon save" data-act="save" data-i="${i}" title="Save" ${r.dirty ? "" : "hidden"} ${dis}>✓</button>
        <button class="icon delete" data-act="delete" data-i="${i}" title="Delete" ${dis}>✕</button>
      </div>`;
    const section = (kind, title, empty, hint = "") => {
      const rows = this._rows.map((r, i) => (r.kind === kind ? row(r, i) : "")).join("");
      return `
        <div class="section">
          <div class="section-head"><span>${esc(title)}</span><button class="text" data-act="add" data-kind="${kind}" ${dis}>+ Add</button></div>
          ${rows || `<div class="empty">${esc(empty)}</div>`}
          ${rows && hint ? `<div class="hint">${esc(hint)}</div>` : ""}
        </div>`;
    };
    const what = this._hasSensors ? `${KINDS[tempKind].toLowerCase()} and sensor schedules` : `${KINDS[tempKind].toLowerCase()} schedule`;
    const copy = this._copyTo
      ? `<div class="copy">
          <span>Copy ${DAY_NAMES[this._open]} to</span>
          ${Object.keys(DAY_SETS)
            .map((k) => `<button class="pill set" data-act="copy-set" data-set="${k}" ${dis}>${DAY_SET_NAMES[k]}</button>`)
            .join("")}
          <span class="break"></span>
          ${DAYS.filter((d) => d !== this._open)
            .map((d) => `<button class="pill ${this._copyTo.has(d) ? "on" : ""}" data-act="copy-day" data-day="${d}" ${dis}>${DAY_NAMES[d]}</button>`)
            .join("")}
          <button class="text" data-act="copy-go" ${dis || this._copyTo.size ? "" : "disabled"} ${dis}>Copy</button>
          <div class="hint">Replaces those days' ${esc(what)}.</div>
        </div>`
      : "";
    return `
      <div class="editor">
        ${section(tempKind, `${KINDS[tempKind]} temperatures`, `No ${KINDS[tempKind].toLowerCase()} entries on ${DAY_NAMES[this._open]}.`)}
        ${this._hasSensors ? section(SENSORS, "Sensors", `No sensor entries on ${DAY_NAMES[this._open]}.`, "Pick two or more sensors to average them.") : ""}
        <div class="actions">
          <button class="text" data-act="copy" ${dis || this._rows.length ? "" : "disabled"} ${dis}>Copy to…</button>
          <span class="spacer"></span>
          <button class="text" data-act="close" ${dis}>Done</button>
        </div>
        ${copy}
      </div>`;
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
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
        } else if (act === "copy-set") this._copySet(el.dataset.set);
        else if (act === "copy-go") this._copy();
        ev.stopPropagation();
      };
      el.addEventListener("click", handler);
      if (act === "day") el.addEventListener("keydown", handler);
    });
    root.querySelectorAll("input[data-field]").forEach((input) => {
      input.addEventListener("input", () => {
        const row = this._rows[Number(input.dataset.i)];
        const field = input.dataset.field;
        row[field] = field === "time" ? input.value : input.value === "" ? null : Number(input.value);
        if (!row.dirty) {
          row.dirty = true;
          const entry = input.closest(".entry");
          entry.classList.add("dirty");
          entry.querySelector(".save").hidden = false;
        }
      });
    });
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
  .editor { margin: 4px 0 10px 38px; padding: 8px 10px; border-radius: 8px; background: var(--secondary-background-color); }
  .section + .section { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--divider-color); }
  .section-head { display: flex; align-items: center; justify-content: space-between; font-size: 0.85em; font-weight: 500;
                  color: var(--secondary-text-color); }
  .entry { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
  .entry.sensors { align-items: flex-start; }
  .entry.dirty .time, .entry.dirty .temp { border-color: var(--primary-color); }
  input { font: inherit; color: var(--primary-text-color); background: var(--card-background-color, #fff);
          border: 1px solid var(--divider-color); border-radius: 6px; padding: 4px 6px; }
  input:disabled { opacity: 0.6; }
  .time { width: 7.2em; flex: none; } .temp { width: 4em; }
  .temps { display: flex; align-items: center; gap: 4px; }
  .picks { display: flex; flex-wrap: wrap; gap: 4px; flex: 1; }
  .pick { border: 1px solid var(--c); background: none; color: var(--primary-text-color); border-radius: 12px;
          padding: 2px 8px; font-size: 0.85em; }
  .pick.on { background: var(--c); color: #fff; }
  .unit, .dash { color: var(--secondary-text-color); font-size: 0.85em; }
  button { font: inherit; cursor: pointer; }
  button:disabled { cursor: default; opacity: 0.5; }
  .icon { border: none; background: none; color: var(--secondary-text-color); font-size: 1em; padding: 4px 6px; border-radius: 50%; }
  .icon.save { color: var(--primary-color); font-weight: 700; }
  .icon.delete:hover { color: var(--error-color, #db4437); }
  .actions { display: flex; gap: 8px; margin-top: 8px; }
  .spacer { flex: 1; }
  .text { border: none; background: none; color: var(--primary-color); padding: 4px 6px; font-weight: 500; }
  .copy { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; font-size: 0.9em;
          color: var(--secondary-text-color); }
  .pill { border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); border-radius: 12px; padding: 2px 8px; }
  .pill.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .pill.set { border-style: dashed; }
  .break { flex-basis: 100%; height: 0; }
  .hint { flex-basis: 100%; font-size: 0.85em; color: var(--secondary-text-color); }
  .empty { color: var(--secondary-text-color); font-size: 0.9em; padding: 4px 0; }
  .error { margin-top: 8px; color: var(--error-color, #db4437); font-size: 0.9em; }
  .warn { color: var(--warning-color, #ffa600); }
`;

customElements.define("protonest-schedule-card", ProtoNestScheduleCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "protonest-schedule-card",
  name: "ProtoNest schedule",
  description: "View and edit a Nest thermostat's weekly temperature schedule and its Home Assistant target sensor schedule.",
});
