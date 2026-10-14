// ProtoNest's weekly schedule card: `type: custom:protonest-schedule-card`,
// `entity: climate.<thermostat>`, and optionally `schedule: heat | cool | range`
// (default: the one the thermostat's current mode uses).
//
// It reads the thermostat's `schedule` attribute and saves changes through the
// protonest.schedule_add / schedule_update / schedule_remove actions, one
// entry at a time, like the Nest app.

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const KINDS = { heat: "Heat", cool: "Cool", range: "Heat · Cool" };
const MODE_KIND = { heat: "heat", cool: "cool", heat_cool: "range" };
const STEP_MINUTES = 15; // the thermostat only takes quarter hours

const minutes = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};
const hhmm = (mins) => `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
const quarter = (value) => hhmm(Math.min(24 * 60 - STEP_MINUTES, Math.round(minutes(value) / STEP_MINUTES) * STEP_MINUTES));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

class ProtoNestScheduleCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("Set `entity` to a ProtoNest thermostat (climate.…)");
    if (config.schedule && !(config.schedule in KINDS)) throw new Error("`schedule` must be heat, cool or range");
    this._config = config;
    this._kind = config.schedule || null; // null: follow the thermostat's mode
    this._open = null; // the day being edited
    this._pinned = null; // the schedule kind being edited, fixed while a day is open
    this._rows = null; // that day's editable rows
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
    const key = s ? JSON.stringify([s.state, s.attributes.friendly_name, s.attributes.schedule]) : "";
    if (key === this._shown) return;
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
    return 7;
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

  get _activeKind() {
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

  _entries(day) {
    const sched = this._schedule;
    const list = (sched && sched[this._activeKind]) || [];
    return list.filter((e) => !day || e.day === day).sort((a, b) => minutes(a.time) - minutes(b.time));
  }

  _temps(entry) {
    return this._activeKind === "range"
      ? { target_temp_low: entry.target_temp_low, target_temp_high: entry.target_temp_high }
      : { temperature: entry.temperature };
  }

  _fmt(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "–";
    return this._fahrenheit ? String(Math.round(value)) : String(Math.round(value * 2) / 2);
  }

  // --- actions ----------------------------------------------------------------------

  async _call(kind, service, data) {
    await this._hass.callService("protonest", service, { schedule: kind, ...data }, {
      entity_id: this._config.entity,
    });
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
      this._pinned = this._activeKind; // keep the tab shown now, whatever the mode does
      this._open = day;
    }
    this._rows = null;
    this._copyTo = null;
    this._error = null;
    this._render();
  }

  _ensureRows() {
    if (this._rows || !this._open) return;
    this._rows = this._entries(this._open).map((e) => ({ orig: e.time, time: e.time, ...this._temps(e), dirty: false }));
  }

  _addRow() {
    const rows = this._rows;
    const last = rows[rows.length - 1];
    const time = last ? hhmm(Math.min(minutes(last.time) + 60, 23 * 60 + 45)) : "08:00";
    const s = this._state;
    const sameUnit = this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature === this._unit;
    const guess = (a) => (sameUnit && s && s.attributes[a] != null ? s.attributes[a] : null);
    const temps = last
      ? this._temps(last)
      : this._activeKind === "range"
        ? { target_temp_low: guess("target_temp_low") ?? (this._fahrenheit ? 68 : 20), target_temp_high: guess("target_temp_high") ?? (this._fahrenheit ? 76 : 24.5) }
        : { temperature: guess("temperature") ?? (this._fahrenheit ? 70 : 21) };
    rows.push({ orig: null, time, ...temps, dirty: true });
    this._render();
  }

  _invalid(row) {
    if (!/^\d{1,2}:\d{2}/.test(row.time || "")) return "Enter a time.";
    if (Object.values(this._temps(row)).some((v) => v === null || v === undefined || Number.isNaN(v)))
      return "Enter a temperature.";
    return null;
  }

  _forget(row) {
    const i = this._rows ? this._rows.indexOf(row) : -1;
    if (i >= 0) this._rows.splice(i, 1);
  }

  _settled() {
    // Show what the thermostat stored, unless other rows still have unsaved edits.
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
    const [kind, day] = [this._activeKind, this._open];
    const time = quarter(row.time);
    const temps = this._temps(row);
    this._run(async () => {
      if (row.orig === null) {
        await this._call(kind, "schedule_add", { day, time, ...temps });
      } else {
        await this._call(kind, "schedule_update", {
          day, time: row.orig, ...(time !== row.orig ? { new_time: time } : {}), ...temps,
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
    const [kind, day] = [this._activeKind, this._open];
    this._run(async () => {
      await this._call(kind, "schedule_remove", { day, time: row.orig });
      this._forget(row);
      this._settled();
    });
  }

  _copy() {
    const kind = this._activeKind;
    const source = this._entries(this._open);
    const targets = DAYS.filter((d) => this._copyTo && this._copyTo.has(d));
    if (!targets.length) return;
    this._run(async () => {
      // Change only what differs: each call is a separate write to the thermostat.
      const same = (a, b) => JSON.stringify(this._temps(a)) === JSON.stringify(this._temps(b));
      for (const day of targets) {
        const existing = new Map(this._entries(day).map((e) => [e.time, e]));
        for (const e of source) {
          const old = existing.get(e.time);
          existing.delete(e.time);
          if (!old) await this._call(kind, "schedule_add", { day, time: e.time, ...this._temps(e) });
          else if (!same(old, e)) await this._call(kind, "schedule_update", { day, time: e.time, ...this._temps(e) });
        }
        for (const time of existing.keys()) await this._call(kind, "schedule_remove", { day, time });
      }
      this._copyTo = null;
    });
  }

  // --- rendering --------------------------------------------------------------------

  _chip(entry) {
    const pct = (minutes(entry.time) / (24 * 60)) * 100;
    const kind = this._activeKind;
    const edge = pct < 4 ? "start" : pct > 96 ? "end" : "";
    const [label, inner] =
      kind === "range"
        ? [`${this._fmt(entry.target_temp_low)}–${this._fmt(entry.target_temp_high)}`,
           `<b class="lo">${esc(this._fmt(entry.target_temp_low))}</b><b class="hi">${esc(this._fmt(entry.target_temp_high))}</b>`]
        : [this._fmt(entry.temperature), esc(this._fmt(entry.temperature))];
    return `<span class="chip ${kind} ${edge}" style="left:${pct}%" title="${esc(entry.time)} · ${esc(label)}${esc(this._unit)}">${inner}</span>`;
  }

  _editor() {
    this._ensureRows();
    const range = this._activeKind === "range";
    const step = this._fahrenheit ? 1 : 0.5;
    const [lo, hi] = this._fahrenheit ? [50, 90] : [10, 32];
    const dis = this._busy ? "disabled" : "";
    const num = (i, field, value) =>
      `<input class="temp" type="number" inputmode="decimal" min="${lo}" max="${hi}" step="${step}" value="${esc(value ?? "")}" data-i="${i}" data-field="${field}" ${dis}>`;
    const rows = this._rows
      .map(
        (r, i) => `
      <div class="entry ${r.dirty ? "dirty" : ""}">
        <input class="time" type="time" step="${STEP_MINUTES * 60}" value="${esc(r.time)}" data-i="${i}" data-field="time" ${dis}>
        <span class="temps">${range ? `${num(i, "target_temp_low", r.target_temp_low)}<span class="dash">–</span>${num(i, "target_temp_high", r.target_temp_high)}` : num(i, "temperature", r.temperature)}<span class="unit">${esc(this._unit)}</span></span>
        <button class="icon save" data-act="save" data-i="${i}" title="Save" ${r.dirty ? "" : "hidden"} ${dis}>✓</button>
        <button class="icon delete" data-act="delete" data-i="${i}" title="Delete" ${dis}>✕</button>
      </div>`
      )
      .join("");
    const copy = this._copyTo
      ? `<div class="copy">
          <span>Copy ${DAY_NAMES[this._open]} to</span>
          ${DAYS.filter((d) => d !== this._open)
            .map((d) => `<button class="pill ${this._copyTo.has(d) ? "on" : ""}" data-act="copy-day" data-day="${d}" ${dis}>${DAY_NAMES[d]}</button>`)
            .join("")}
          <button class="text" data-act="copy-go" ${dis || this._copyTo.size ? "" : "disabled"} ${dis}>Copy</button>
          <div class="hint">Replaces those days' ${esc(KINDS[this._activeKind].toLowerCase())} schedule.</div>
        </div>`
      : "";
    return `
      <div class="editor">
        ${rows || `<div class="empty">No ${esc(KINDS[this._activeKind].toLowerCase())} entries on ${DAY_NAMES[this._open]}.</div>`}
        <div class="actions">
          <button class="text" data-act="add" ${dis}>+ Add</button>
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
      const now = new Date();
      const today = DAYS[(now.getDay() + 6) % 7];
      const nowPct = ((now.getHours() * 60 + now.getMinutes()) / (24 * 60)) * 100;
      const rows = DAYS.map((day) => {
        const chips = this._entries(day).map((e) => this._chip(e)).join("");
        return `
          <div class="row ${this._open === day ? "open" : ""}" data-act="day" data-day="${day}" role="button" tabindex="0">
            <span class="day ${day === today ? "today" : ""}">${DAY_NAMES[day]}</span>
            <div class="track">${day === today ? `<span class="now" style="left:${nowPct}%"></span>` : ""}${chips}</div>
          </div>
          ${this._open === day ? this._editor() : ""}`;
      }).join("");
      const tabs = Object.entries(KINDS)
        .map(([k, label]) => `<button class="tab ${k === this._activeKind ? "on" : ""}" data-act="kind" data-kind="${k}" ${this._busy ? "disabled" : ""}>${label}</button>`)
        .join("");
      body = `
        <div class="tabs">${tabs}</div>
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
        } else if (act === "add") this._addRow();
        else if (act === "save") this._saveRow(Number(el.dataset.i));
        else if (act === "delete") this._deleteRow(Number(el.dataset.i));
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
         border-radius: 16px; padding: 5px 8px; font: inherit; font-size: 0.85em; cursor: pointer; }
  .tab.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .axis, .row { display: grid; grid-template-columns: 38px 1fr; align-items: center; }
  .hours { position: relative; height: 14px; margin: 0 14px; font-size: 0.7em; color: var(--secondary-text-color); }
  .hours span { position: absolute; transform: translateX(-50%); }
  .row { min-height: 34px; cursor: pointer; border-radius: 8px; }
  .row:hover, .row.open { background: var(--secondary-background-color); }
  .row:focus-visible { outline: 2px solid var(--primary-color); }
  .day { font-size: 0.85em; color: var(--secondary-text-color); padding-left: 4px; }
  .day.today { color: var(--primary-color); font-weight: 600; }
  .track { position: relative; height: 26px; margin: 0 14px;
           background: linear-gradient(var(--divider-color), var(--divider-color)) center / 100% 2px no-repeat; }
  .now { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--primary-color); opacity: 0.6; }
  .chip { position: absolute; top: 3px; transform: translateX(-50%); min-width: 22px; padding: 2px 5px;
          border-radius: 10px; font-size: 0.75em; line-height: 16px; text-align: center; color: #fff; white-space: nowrap;
          box-shadow: 0 0 0 2px var(--card-background-color, #fff); }
  .chip.start { transform: none; } .chip.end { transform: translateX(-100%); }
  .chip:hover { z-index: 1; }
  .chip.heat { background: var(--heat); } .chip.cool { background: var(--cool); }
  .chip.range { display: flex; padding: 0; overflow: hidden; }
  .chip.range b { font-weight: inherit; padding: 2px 5px; }
  .chip.range .lo { background: var(--heat); } .chip.range .hi { background: var(--cool); }
  .editor { margin: 4px 0 10px 38px; padding: 8px 10px; border-radius: 8px; background: var(--secondary-background-color); }
  .entry { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
  .entry.dirty .time, .entry.dirty .temp { border-color: var(--primary-color); }
  input { font: inherit; color: var(--primary-text-color); background: var(--card-background-color, #fff);
          border: 1px solid var(--divider-color); border-radius: 6px; padding: 4px 6px; }
  input:disabled { opacity: 0.6; }
  .time { width: 7.2em; } .temp { width: 4em; }
  .temps { display: flex; align-items: center; gap: 4px; }
  .unit, .dash { color: var(--secondary-text-color); font-size: 0.85em; }
  button { font: inherit; cursor: pointer; }
  button:disabled { cursor: default; opacity: 0.5; }
  .icon { border: none; background: none; color: var(--secondary-text-color); font-size: 1em; padding: 4px 6px; border-radius: 50%; }
  .icon.save { color: var(--primary-color); font-weight: 700; }
  .icon.delete:hover { color: var(--error-color, #db4437); }
  .actions { display: flex; gap: 8px; margin-top: 6px; }
  .spacer { flex: 1; }
  .text { border: none; background: none; color: var(--primary-color); padding: 4px 6px; font-weight: 500; }
  .copy { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; font-size: 0.9em;
          color: var(--secondary-text-color); }
  .pill { border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); border-radius: 12px; padding: 2px 8px; }
  .pill.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .hint { flex-basis: 100%; font-size: 0.85em; }
  .empty { color: var(--secondary-text-color); font-size: 0.9em; padding: 4px 0; }
  .error { margin-top: 8px; color: var(--error-color, #db4437); font-size: 0.9em; }
  .warn { color: var(--warning-color, #ffa600); }
`;

customElements.define("protonest-schedule-card", ProtoNestScheduleCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "protonest-schedule-card",
  name: "ProtoNest schedule",
  description: "View and edit a Nest thermostat's weekly heat, cool and heat · cool schedules.",
});
