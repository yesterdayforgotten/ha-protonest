// Schedule card logic, run in Node with a minimal DOM stand-in:
//   node tests_card/schedule-card.test.mjs
// Each numbered check prints "check N passed"; a failure throws and exits non-zero.
import { readFileSync } from "fs";
import assert from "assert/strict";
let Card;
globalThis.HTMLElement = class { attachShadow() { const frame = { innerHTML: "", textContent: "" }; this.shadowRoot = { innerHTML: "", frame, querySelectorAll: () => [], querySelector: (sel) => (sel === "ha-card > .body" ? frame : sel === "ha-card > .head" ? {} : null), addEventListener() {} }; } };
let Editor;
globalThis.customElements = { define: (n, c) => { if (n.endsWith("-editor")) Editor = c; else Card = c; } };
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init.detail; } };
globalThis.window = globalThis;
let confirmAnswer = true, confirmed = 0;
globalThis.confirm = () => { confirmed++; return confirmAnswer; };
eval(readFileSync(new URL("../custom_components/protonest/frontend/protonest-schedule-card.js", import.meta.url), "utf8"));
const tick = () => new Promise((r) => setTimeout(r, 0));
const cards = [];
// Wait for every card's saves to finish (timers can be slow on a busy machine).
const settle = async () => {
  await new Promise((r) => setTimeout(r, 30));
  for (let n = 0; n < 500 && cards.some((c) => c._busy || c._inflight); n++) await new Promise((r) => setTimeout(r, 10));
};

const base = () => ({ unit: "°F", range: [], cool: [{ day: "mon", time: "08:00", temperature: 75 }], heat: [
  { day: "mon", time: "07:00", temperature: 68 }, { day: "mon", time: "22:00", temperature: 62 },
  { day: "tue", time: "07:00", temperature: 68 }, { day: "tue", time: "22:00", temperature: 60 }, { day: "tue", time: "12:00", temperature: 70 }] });
const sensorSched = (entries) => ({ enabled: true, switch: "switch.x_target_sensor_schedule",
  options: [{ id: "thermostat", name: "Thermostat", entity_id: "sensor.x_t" },
            { id: "sensor.a_temperature", name: "A", entity_id: "sensor.a_temperature" },
            { id: "sensor.b_temperature", name: "B", entity_id: "sensor.b_temperature" }],
  entries });

function make(mode, sched, sensorEntries = null) {
  const card = new Card();
  cards.push(card);
  card.setConfig({ entity: "climate.x" });
  const calls = [];
  card._call = async (kind, s, d) => { calls.push([kind, s, d]); await tick(); };
  card._setSensorEntries = async (list) => { calls.push(["sensors", "set_sensor_schedule", list]); await tick(); };
  const attributes = { schedule: sched, friendly_name: "X", temperature: 71 };
  if (sensorEntries) attributes.sensor_schedule = sensorSched(sensorEntries);
  card.hass = { locale: { language: "en", time_format: "24" }, config: { unit_system: { temperature: "°F" } }, states: { "climate.x": { state: mode, attributes } } };
  return [card, calls];
}
const S = (day, time, ...sensors) => ({ day, time, sensors });

// 1. copying temperatures changes only what differs
{
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._copyTo = new Set(["tue", "wed"]); card._copy();
  await settle();
  assert.deepEqual(calls.map((c) => [c[1], c[2].day, c[2].time]), [
    ["schedule_update", "tue", "22:00"], ["schedule_remove", "tue", "12:00"],
    ["schedule_add", "wed", "07:00"], ["schedule_add", "wed", "22:00"]]);
  assert(calls.every((c) => c[0] === "heat"));
}
// 2. the thermostat switching to cool mid-copy doesn't redirect the remaining writes
{
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._copyTo = new Set(["tue", "wed", "thu"]); card._copy();
  await tick();
  card.hass = { ...card._hass, states: { "climate.x": { state: "cool", attributes: { schedule: base(), friendly_name: "X" } } } };
  assert.equal(card._tempKind, "heat", "stays on heat while editing");
  await settle();
  assert(calls.length > 3 && calls.every((c) => c[0] === "heat"), JSON.stringify(calls));
  card._openDay("mon"); // close
  assert.equal(card._tempKind, "cool", "after closing, it follows the mode again");
}
// 3. empty inputs are caught before sending
{
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._ensureRows();
  card._rows[0].temperature = null; card._rows[0].dirty = true; card._commit(card._rows[0]);
  assert.equal(card._error, "Enter a temperature."); assert.equal(calls.length, 0);
  card._rows[0].temperature = 70; card._rows[0].time = ""; card._commit(card._rows[0]);
  assert.equal(card._error, "Enter a time, like 5:30 PM or 17:30."); assert.equal(calls.length, 0);
}
// 4. a save sends rounded quarter-hour times and the original time for updates
{
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._ensureRows();
  Object.assign(card._rows[0], { time: "07:52", temperature: 69, dirty: true }); card._commit(card._rows[0]);
  await settle();
  assert.deepEqual(calls[0], ["heat", "schedule_update", { day: "mon", time: "07:00", new_time: "07:45", temperature: 69 }]);
}
// 5. deleting while the list refreshes doesn't drop another row
{
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._ensureRows();
  card._deleteRow(0);
  assert.deepEqual(card._rows.map((r) => r.time), ["22:00"], "gone from the editor at once");
  await settle();
  assert.deepEqual(calls[0], ["heat", "schedule_remove", { day: "mon", time: "07:00" }]);
  assert.deepEqual(card._rows.map((r) => r.time), ["22:00"], "only the deleted row goes");
}
// 6. a new entry starts from the thermostat's setpoint only when units match
{
  const [card] = make("heat", { ...base(), heat: [] });
  card._openDay("fri"); card._ensureRows(); card._addRow("heat");
  assert.equal(card._rows[0].temperature, 71);
  const [c2] = make("heat", { ...base(), heat: [] });
  c2._hass.config.unit_system.temperature = "°C";
  c2._openDay("fri"); c2._ensureRows(); c2._addRow("heat");
  assert.equal(c2._rows[0].temperature, 70, "falls back to a default, not 71 °C read as °F");
}
// 7. picking sensors: one, several to average, the thermostat alone
{
  const [card] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature")]);
  card._openDay("mon"); card._ensureRows();
  const i = card._rows.findIndex((r) => r.kind === "sensors");
  card._pick(i, "sensor.b_temperature");
  assert.deepEqual(card._rows[i].sensors, ["sensor.a_temperature", "sensor.b_temperature"]);
  card._pick(i, "sensor.a_temperature");
  assert.deepEqual(card._rows[i].sensors, ["sensor.b_temperature"]);
  card._pick(i, "sensor.b_temperature"); // the last one can't be unpicked
  assert.deepEqual(card._rows[i].sensors, ["sensor.b_temperature"]);
  card._pick(i, "thermostat");
  assert.deepEqual(card._rows[i].sensors, ["thermostat"]);
  card._pick(i, "sensor.a_temperature");
  assert.deepEqual(card._rows[i].sensors, ["sensor.a_temperature"], "the thermostat isn't averaged");
}
// 8. the day editor holds both schedules; saving a sensor row sends the whole sensor schedule
{
  const [card, calls] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature"), S("tue", "07:00", "thermostat")]);
  card._openDay("mon"); card._ensureRows();
  assert.deepEqual(card._rows.map((r) => `${r.kind} ${r.time}`), ["heat 07:00", "heat 22:00", "sensors 07:00"]);
  const html = card._editor();
  assert(html.includes("Heat temperatures") && html.includes(">Sensors<"));
  const i = card._rows.findIndex((r) => r.kind === "sensors");
  Object.assign(card._rows[i], { time: "06:10", sensors: ["sensor.a_temperature", "sensor.b_temperature"], dirty: true });
  card._commit(card._rows[i]);
  await settle();
  assert.deepEqual(calls, [["sensors", "set_sensor_schedule", [S("tue", "07:00", "thermostat"),
    S("mon", "06:15", "sensor.a_temperature", "sensor.b_temperature")]]]);
}
// 9. copying a day copies both: temperatures (only what differs) and sensors (one save)
{
  const [card, calls] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature"), S("mon", "21:00", "thermostat"),
    S("tue", "09:00", "sensor.b_temperature"), S("sun", "10:00", "sensor.b_temperature")]);
  card._openDay("mon"); card._copyTo = new Set(["tue", "wed"]); card._copy();
  await settle(); await settle();
  const sensorCalls = calls.filter((c) => c[0] === "sensors");
  assert.equal(calls.filter((c) => c[0] === "heat").length, 4);
  assert.equal(sensorCalls.length, 1);
  assert.deepEqual(sensorCalls[0][2].map((e) => `${e.day} ${e.time}`).sort(),
    ["mon 07:00", "mon 21:00", "sun 10:00", "tue 07:00", "tue 21:00", "wed 07:00", "wed 21:00"]);
}
// 10. bands: a day starts with what was in effect at the end of the previous day (wrapping the week)
{
  const [card] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature"), S("wed", "00:00", "sensor.b_temperature"),
    S("sat", "22:00", "thermostat")]);
  const titles = (day) => [...card._bands(day).matchAll(/class="band"[^>]*title="([^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual(titles("mon"), ["22:00 · Thermostat", "07:00 · A"]);
  assert.deepEqual(titles("tue"), ["07:00 · A"]);
  assert.deepEqual(titles("wed"), ["00:00 · B"], "an entry at midnight replaces the carried one");
  assert.deepEqual(titles("sun"), ["22:00 · Thermostat"]);
}
// 11. copy shortcuts select the set's days except the one being copied, and only fill in the selection
{
  const [card, calls] = make("heat", base());
  card._openDay("sat");
  assert(!card._editor().includes('class="sets"'), "shortcuts appear only once Copy to… is open");
  card._copyTo = new Set();
  for (const set of ["all", "weekdays", "weekend"]) assert(card._editor().includes(`<option value="${set}">`));
  const days = () => [...card._copyTo].sort();
  card._copySet("all"); assert.deepEqual(days(), ["fri", "mon", "sun", "thu", "tue", "wed"]);
  card._copySet("weekdays"); assert.deepEqual(days(), ["fri", "mon", "thu", "tue", "wed"]);
  card._copySet("weekend"); assert.deepEqual(days(), ["sun"]);
  assert.equal(calls.length, 0, "a shortcut doesn't copy by itself");
}
// 12. no sensor bar or section without paired sensors
{
  const [card] = make("heat", base());
  card._hass.states["climate.x"].attributes.sensor_schedule = { enabled: false, switch: "s", entries: [],
    options: [{ id: "thermostat", name: "Thermostat" }] };
  assert.equal(card._hasSensors, false);
  card._openDay("mon");
  assert(!card._editor().includes(">Sensors<"));
}
// 13. tapping an entry on a bar opens its day with that entry highlighted; a sensor entry opens its picker
{
  const [card] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature"), S("sat", "22:00", "thermostat")]);
  card._openEntry("tue", "heat", "22:00");
  card._editor();
  assert.equal(card._open, "tue");
  assert.deepEqual(card._rows.filter((r) => r.focus).map((r) => `${r.kind} ${r.time}`), ["heat 22:00"]);
  assert.equal(card._expanded, null);
  // the band at the start of Monday is Saturday's 22:00 entry: it opens Saturday
  const band = card._bands("mon").match(/data-day="(\w+)" data-kind="(\w+)" data-time="([\d:]+)"/);
  assert.deepEqual(band.slice(1), ["sat", "sensors", "22:00"]);
  card._openEntry("sat", "sensors", "22:00");
  card._editor();
  assert.equal(card._open, "sat");
  assert.equal(card._expanded && card._expanded.time, "22:00");
  card._openEntry("sat", "sensors", "22:00"); // tapping again keeps the day open
  assert.equal(card._open, "sat");
}
// 14. sensor rows are one line; the picker shows only for the expanded row
{
  const [card] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature", "sensor.b_temperature"), S("mon", "21:00", "thermostat")]);
  card._openDay("mon");
  let html = card._editor();
  assert(html.includes("Avg: A + B") && !html.includes('data-act="pick"'));
  const i = card._rows.findIndex((r) => r.kind === "sensors" && r.time === "21:00");
  card._expand(i);
  html = card._editor();
  assert.equal((html.match(/data-act="pick"/g) || []).length, 3, "one row's picker");
  card._addRow("sensors");
  assert.equal(card._expanded, card._rows[card._rows.length - 1], "a new sensor row opens its picker");
}
// 15. the visual editor: "follow the mode" and an empty title are left out of the config
{
  const ed = new Editor();
  const sent = [];
  ed.appendChild = (form) => { ed.form = form; };
  ed.dispatchEvent = (ev) => sent.push(ev.detail.config);
  const listeners = {};
  globalThis.document = { createElement: () => ({ addEventListener: (t, f) => { listeners[t] = f; } }) };
  ed.setConfig({ type: "custom:protonest-schedule-card", entity: "climate.x" });
  ed.hass = {};
  assert.equal(ed.form.data.schedule, "auto");
  listeners["value-changed"]({ stopPropagation() {}, detail: { value: { type: "custom:protonest-schedule-card", entity: "climate.y", schedule: "auto", title: "" } } });
  listeners["value-changed"]({ stopPropagation() {}, detail: { value: { type: "custom:protonest-schedule-card", entity: "climate.y", schedule: "cool" } } });
  assert.deepEqual(sent, [{ type: "custom:protonest-schedule-card", entity: "climate.y" },
    { type: "custom:protonest-schedule-card", entity: "climate.y", schedule: "cool" }]);
}
// 16. dragging: a chip moves in quarter hours between its neighbours and saves on the drop
const fakeEl = (data) => ({ dataset: data, style: {}, isConnected: true, classList: { add() {}, contains: () => false },
  closest: () => ({ getBoundingClientRect: () => ({ left: 0, width: 960 }), appendChild() {}, querySelector: () => null }) });
globalThis.document = { createElement: () => ({ style: {} }) };
const ptr = (x) => ({ pointerId: 1, button: 0, clientX: x, preventDefault() {} });
const px = (hhmm) => (minutes(hhmm) / (24 * 60)) * 960; // 960px = 24 h: 10 px per quarter hour
function minutes(t) { const [h, m] = t.split(":").map(Number); return h * 60 + m; }
{
  const [card, calls] = make("heat", base()); // Tue: 07:00, 12:00, 22:00
  const el = fakeEl({ day: "tue", kind: "heat", time: "12:00" });
  card._dragStart(ptr(px("12:00")), el);
  card._dragMove(ptr(px("12:00") + 3)); // a jiggle is still a tap
  assert.equal(card._drag.moved, false);
  card._dragMove(ptr(px("13:20")));
  assert.equal(card._drag.to, minutes("13:15"), "snaps to the quarter hour");
  card._dragMove(ptr(px("23:50")));
  assert.equal(card._drag.to, minutes("21:45"), "stops a quarter hour before the next entry");
  card._dragMove(ptr(px("01:00")));
  assert.equal(card._drag.to, minutes("07:15"), "and after the previous one");
  card._dragMove(ptr(px("15:30")));
  card._dragEnd(ptr(px("15:30")));
  assert.equal(card._dragged, true, "the click after a drop doesn't open the editor");
  await settle();
  assert.deepEqual(calls, [["heat", "schedule_update", { day: "tue", time: "12:00", new_time: "15:30", temperature: 70 }]]);
}
// 17. dragging a sensor band's start saves the whole sensor schedule with that time changed; a tap saves nothing
{
  const [card, calls] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature"), S("mon", "21:00", "thermostat"), S("tue", "07:00", "thermostat")]);
  assert.equal((card._bands("mon").match(/class="handle"/g) || []).length, 2, "a handle for each of the day's entries, not the carried one");
  const tap = fakeEl({ day: "mon", kind: "sensors", time: "21:00" });
  card._dragStart(ptr(500), tap); card._dragEnd(ptr(500));
  assert.equal(card._dragged, undefined); assert.equal(calls.length, 0);
  const el = fakeEl({ day: "mon", kind: "sensors", time: "21:00" });
  card._dragStart(ptr(px("21:00")), el);
  card._dragMove(ptr(px("19:00")));
  card._dragEnd(ptr(px("19:00")));
  await settle();
  assert.deepEqual(calls[0][2].map((e) => `${e.day} ${e.time}`), ["mon 07:00", "mon 19:00", "tue 07:00"]);
}
// 18. no dragging while the editor has unsaved edits
{
  const [card] = make("heat", base());
  card._openDay("tue"); card._ensureRows(); card._rows[0].dirty = true;
  card._dragStart(ptr(px("12:00")), fakeEl({ day: "tue", kind: "heat", time: "12:00" }));
  assert.equal(card._error, null, "a press alone (a click) says nothing");
  card._dragMove(ptr(px("14:00")));
  assert.equal(card._drag, null);
  assert.match(card._error, /Finish the entry being edited on Tuesday/);
}
console.log("all card logic checks passed");

// 19. times follow HA's 12/24-hour setting
{
  const [card] = make("heat", base());
  assert.equal(card._fmtTime(17 * 60 + 15), "17:15");
  card._hass.locale.time_format = "12";
  assert.match(card._fmtTime(17 * 60 + 15), /^5:15\sPM$/);
  card._openDay("mon");
  const html = card._editor();
  assert.match(html, /class="time" type="text"[^>]*value="7:00\sAM"/, "typed field shows HA's format");
  assert.match(html, /<option value="07:00" selected>7:00\sAM<\/option>/, "the clock's list is on the same time");
  console.log("check 19 passed");
}
// 20. a dropped chip stays at its new time while saving, and goes back if the save fails
{
  const [card] = make("heat", base());
  delete card._call; // the real one: it remembers what it sent
  card._hass.callService = async () => { await tick(); };
  const el = fakeEl({ day: "tue", kind: "heat", time: "12:00" });
  card._dragStart(ptr(px("12:00")), el); card._dragMove(ptr(px("15:30"))); card._dragEnd(ptr(px("15:30")));
  assert.deepEqual(card._entries("heat", "tue").map((e) => e.time), ["07:00", "15:30", "22:00"]);
  await settle();
  assert.deepEqual(card._entries("heat", "tue").map((e) => e.time), ["07:00", "15:30", "22:00"], "until the thermostat's update arrives");
  const sched = base(); sched.heat[4].time = "15:30";
  card.hass = { ...card._hass, states: { "climate.x": { state: "heat", attributes: { schedule: sched, friendly_name: "X" } } } };
  assert.equal(card._moves.length, 0, "confirmed by the stored schedule");
  card._hass.callService = async () => { throw new Error("offline"); };
  card._dragStart(ptr(px("15:30")), fakeEl({ day: "tue", kind: "heat", time: "15:30" }));
  card._dragMove(ptr(px("18:00"))); card._dragEnd(ptr(px("18:00")));
  await settle();
  assert.equal(card._error, "offline");
  assert.deepEqual(card._entries("heat", "tue").map((e) => e.time), ["07:00", "15:30", "22:00"]);
  assert.equal(card._moves.length, 0);
}
// 21. dragging with unsaved edits says why it can't; Esc closes Copy to, then the day; Enter saves
{
  const [card, calls] = make("heat", base());
  card._openDay("tue"); card._ensureRows(); card._rows[0].dirty = true;
  card._dragStart(ptr(px("07:00")), fakeEl({ day: "mon", kind: "heat", time: "07:00" }));
  card._dragMove(ptr(px("09:00")));
  assert.match(card._error, /Tuesday/);
  const key = (k, target = {}) => card._key({ key: k, target, preventDefault() {} });
  key("Enter", { dataset: { field: "temperature", i: "0" }, value: "67" });
  await settle();
  assert.equal(calls[0][1], "schedule_update");
  card._copyTo = new Set(["wed"]);
  key("Escape"); assert.equal(card._copyTo, null); assert.equal(card._open, "tue");
  key("Escape"); assert.equal(card._open, null);
}
console.log("checks 20-21 passed");

// 22. typed times: what parses, the arrow keys, and the clock's list
{
  const P = (t) => { const m = Card.parseTime(t); return m === null ? null : `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
  const ok = { "5:30p": "17:30", "5:30 PM": "17:30", "530pm": "17:30", "17:30": "17:30", "1730": "17:30", "7": "07:00",
    "12am": "00:00", "12:15 a.m.": "00:15", "12p": "12:00", "0:00": "00:00", "23:45": "23:45", "9:05": "09:05" };
  for (const [t, want] of Object.entries(ok)) assert.equal(P(t), want, t);
  for (const t of ["", "24:00", "13pm", "0am", "5:60", "abc", "5:3", "12345"]) assert.equal(P(t), null, t);
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._ensureRows();
  const input = { dataset: { i: "0", field: "time" }, value: "", classList: { remove() {}, toggle() {} } };
  card._key({ key: "ArrowUp", target: input, preventDefault() {} });
  assert.equal(card._rows[0].time, "07:15"); assert.equal(input.value, "07:15"); assert.equal(card._rows[0].dirty, true);
  card._key({ key: "ArrowDown", target: input, preventDefault() {} });
  card._key({ key: "ArrowDown", target: input, preventDefault() {} });
  assert.equal(card._rows[0].time, "06:45");
  card._rows[1].time = "23:50"; card._stepTime({ ...input, dataset: { i: "1" } }, 1);
  assert.equal(card._rows[1].time, "00:00", "rounds, then wraps past midnight");
  card._setTime(input, "18:15");
  card._key({ key: "Enter", target: input, preventDefault() {} });
  await settle();
  assert.deepEqual(calls[0], ["heat", "schedule_update", { day: "mon", time: "07:00", new_time: "18:15", temperature: 68 }]);
  card._rows = null; card._ensureRows(); card._rows[0].time = ""; card._rows[0].dirty = true; card._commit(card._rows[0]);
  assert.match(card._error, /like 5:30 PM or 17:30/);
  console.log("check 22 passed");
}

// 23. review fixes
{
  // an update while a chip is pressed waits for the release: the drag goes on
  const [card, calls] = make("heat", base());
  const el = fakeEl({ day: "tue", kind: "heat", time: "12:00" });
  card._dragStart(ptr(px("12:00")), el);
  const sched = base(); sched.heat[0].temperature = 66;
  card.hass = { ...card._hass, states: { "climate.x": { state: "heat", attributes: { schedule: sched, friendly_name: "X" } } } };
  assert(card._drag && card._redraw, "redraw held");
  card._dragMove(ptr(px("13:00"))); card._dragEnd(ptr(px("13:00")));
  await settle();
  assert.deepEqual(calls.map((c) => [c[2].time, c[2].new_time]), [["12:00", "13:00"]]);
  assert.equal(card._redraw, false);
  calls.length = 0; card._moves.forEach((m) => clearTimeout(m.timer)); card._moves = [];
  // a tap during an update: redrawn after the click
  card._dragStart(ptr(px("07:00")), fakeEl({ day: "tue", kind: "heat", time: "07:00" }));
  card.hass = { ...card._hass, states: { "climate.x": { state: "heat", attributes: { schedule: base(), friendly_name: "X" } } } };
  card._dragEnd(ptr(px("07:00")));
  assert.equal(card._drag, null); assert.equal(calls.length, 0);
  // a chip that's gone (redrawn some other way) doesn't drag
  el.isConnected = false;
  card._dragStart(ptr(px("12:00")), el); card._dragMove(ptr(px("12:30")));
  assert.equal(card._drag, null, "a detached chip doesn't drag"); card._dragEnd(ptr(px("12:30")));
  assert.equal(calls.length, 0);
}
{
  // a saved row keeps its new values in the editor until the thermostat's update arrives
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._ensureRows();
  Object.assign(card._rows[0], { temperature: 72, dirty: true }); card._commit(card._rows[0]);
  await settle();
  card._ensureRows();
  assert.equal(card._rows[0].temperature, 72);
  card._deleteRow(1); await settle(); card._ensureRows();
  assert.deepEqual(card._rows.map((r) => r.time), ["07:00"], "a deleted row doesn't come back");
  const sched = base(); sched.heat[0].temperature = 72; sched.heat.splice(1, 1);
  card.hass = { ...card._hass, states: { "climate.x": { state: "heat", attributes: { schedule: sched, friendly_name: "X" } } } };
  card._ensureRows();
  assert.deepEqual(card._rows.map((r) => `${r.time} ${r.temperature}`), ["07:00 72"], "then shows what's stored");
}
{
  // two quick sensor drags both reach the save
  const [card] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature"), S("mon", "21:00", "thermostat")]);
  const calls = [];
  delete card._setSensorEntries; // the real one: it remembers what it sent
  card._hass.callService = async (domain, service, data) => { calls.push(["sensors", service, data.entries]); await tick(); };
  card._dragStart(ptr(px("07:00")), fakeEl({ day: "mon", kind: "sensors", time: "07:00" }));
  card._dragMove(ptr(px("08:00"))); card._dragEnd(ptr(px("08:00")));
  await settle();
  card._dragStart(ptr(px("21:00")), fakeEl({ day: "mon", kind: "sensors", time: "21:00" }));
  card._dragMove(ptr(px("22:00"))); card._dragEnd(ptr(px("22:00")));
  await settle();
  assert.deepEqual(calls[1][2].map((e) => e.time), ["08:00", "22:00"]);
  card._moves.forEach((m) => clearTimeout(m.timer));
}
{
  // an entry that can't be saved: asked before it's thrown away; "no" keeps it
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._ensureRows(); Object.assign(card._rows[0], { time: "", dirty: true });
  confirmAnswer = false; confirmed = 0;
  card._openDay("mon"); assert.equal(card._open, "mon");
  card._openDay("tue"); assert.equal(card._open, "mon");
  card._openEntry("wed", "heat", "07:00"); assert.equal(card._open, "mon");
  card._key({ key: "Escape", target: {}, preventDefault() {} }); assert.equal(card._open, "mon");
  assert.equal(confirmed, 4);
  assert(card._rows[0].dirty, "edits kept"); assert.equal(calls.length, 0);
  confirmAnswer = true;
  card._openDay("tue"); assert.equal(card._open, "tue");
}
{
  // a complete edit is simply saved on leaving the day: no question
  const [card, calls] = make("heat", base());
  card._openDay("mon"); card._ensureRows(); Object.assign(card._rows[0], { temperature: 66, dirty: true });
  confirmed = 0;
  card._openDay("mon"); assert.equal(card._open, null); assert.equal(confirmed, 0);
  await settle();
  assert.deepEqual(calls, [["heat", "schedule_update", { day: "mon", time: "07:00", temperature: 66 }]]);
}
{
  // a new row never starts on a taken time, nor NaN
  const [card] = make("heat", { ...base(), heat: [{ day: "mon", time: "23:45", temperature: 60 }, { day: "mon", time: "00:00", temperature: 60 }] });
  card._openDay("mon"); card._ensureRows();
  card._addRow("heat");
  assert.equal(card._rows[2].time, "00:15");
  card._rows[2].time = ""; card._addRow("heat");
  assert.equal(card._rows[3].time, "08:00");
}
console.log("check 23 passed");

// 24. auto-save: each row saves on its own, one at a time; sensors build on what was just sent
{
  const [card] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature"), S("mon", "21:00", "thermostat")]);
  const sent = [];
  // the real _call/_setSensorEntries, with a slow service that hasn't echoed anything back yet
  delete card._call; delete card._setSensorEntries;
  card._hass.callService = async (domain, service, data) => { sent.push([service, JSON.parse(JSON.stringify(data))]); await new Promise((r) => setTimeout(r, 20)); };
  card._openDay("mon"); card._ensureRows();
  const s = card._rows.filter((r) => r.kind === "sensors");
  card._pick(card._rows.indexOf(s[0]), "sensor.b_temperature"); // A -> A+B
  Object.assign(s[1], { time: "21:30", dirty: true }); card._commit(s[1]);
  assert.equal(card._status, "Saving…");
  await settle(); await settle(); await settle();
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[1][1].entries, [S("mon", "07:00", "sensor.a_temperature", "sensor.b_temperature"), S("mon", "21:30", "thermostat")],
    "the second save keeps the first, though the thermostat hasn't confirmed it");
  assert.equal(card._status, "Saved");
  // new temperature row: saved at once; a quick second edit goes as an update of what was added
  card._addRow("heat");
  const r = card._rows.at(-1);
  Object.assign(r, { time: "23:30", dirty: true }); card._commit(r);
  await settle(); await settle(); await settle();
  assert.deepEqual(sent.slice(2).map(([sv, d]) => [sv, d.time, d.new_time]), [["schedule_add", "23:00", undefined], ["schedule_update", "23:00", "23:30"]]);
  // the bars show what was saved before the thermostat confirms it
  assert(card._entries("heat", "mon").some((e) => e.time === "23:30"));
  // two entries at one time: not sent, stays marked
  Object.assign(card._rows[0], { time: "22:05", dirty: true }); card._commit(card._rows[0]);
  assert.match(card._error, /already an entry at 22:00/); assert.equal(sent.length, 4); assert(card._rows[0].dirty);
}
console.log("check 24 passed");
// 25. review fixes: _sent kept until the stored values match; no rows while saves are queued;
//     a drag sends the values as they are when it runs
{
  const [card] = make("heat", base(), [S("mon", "08:00", "sensor.a_temperature"), S("mon", "18:00", "thermostat")]);
  const sent = [];
  delete card._call; delete card._setSensorEntries;
  card._hass.callService = async (domain, service, data) => { sent.push([service, JSON.parse(JSON.stringify(data))]); await tick(); };
  card._openDay("mon"); card._ensureRows();
  const s = card._rows.filter((r) => r.kind === "sensors");
  card._pick(card._rows.indexOf(s[0]), "sensor.b_temperature"); // 08:00 A -> A+B
  await settle();
  // an older echo: same times, old values -> what was saved stays shown
  const st = card._hass.states["climate.x"];
  card.hass = { ...card._hass, states: { "climate.x": { ...st, attributes: { ...st.attributes, sensor_schedule: sensorSched([S("mon", "08:00", "sensor.a_temperature"), S("mon", "18:00", "sensor.b_temperature")]) } } } };
  assert.deepEqual(card._entries("sensors", "mon")[0].sensors, ["sensor.a_temperature", "sensor.b_temperature"]);
  // its own echo clears it
  card.hass = { ...card._hass, states: { "climate.x": { ...st, attributes: { ...st.attributes, sensor_schedule: sensorSched([S("mon", "08:00", "sensor.b_temperature", "sensor.a_temperature"), S("mon", "18:00", "thermostat")]) } } } };
  assert.equal(card._sent.sensors, undefined);

  // opening a day while a save is queued: no rows until it's done
  const [c2] = make("heat", base());
  c2._openDay("mon"); c2._ensureRows();
  Object.assign(c2._rows[0], { time: "09:00", dirty: true }); c2._commit(c2._rows[0]);
  c2._openDay("mon"); c2._openDay("mon");
  assert.equal(c2._rows, null); assert(c2._editor().includes("Saving…"));
  await settle();
  c2._ensureRows(); assert(c2._rows.length > 0);

  // a drag dropped while an edit of the same entry is queued sends the edited value
  const [c3, calls3] = make("heat", base());
  c3._openDay("mon"); c3._ensureRows();
  Object.assign(c3._rows[0], { temperature: 72, dirty: true });
  c3._call = async (kind, sv, d) => { calls3.push([kind, sv, d]); await tick(); c3._sent[kind] = { entries: c3._stored(kind).map((e) => (e.day === "mon" && e.time === "07:00" ? { ...e, temperature: 72 } : e)), at: Date.now() }; };
  c3._commit(c3._rows[0]);
  c3._dragStart(ptr(px("07:00")), fakeEl({ day: "mon", kind: "heat", time: "07:00" }));
  c3._dragMove(ptr(px("09:00"))); c3._dragEnd(ptr(px("09:00")));
  await settle();
  assert.deepEqual(calls3.map((c) => c[2].temperature), [72, 72], JSON.stringify(calls3));
  c3._moves.forEach((m) => clearTimeout(m.timer));
}
console.log("check 25 passed");
// 26. the same chip dragged twice before the first save returns: both moves apply
{
  const [card] = make("heat", base());
  const sent = [];
  delete card._call;
  card._hass.callService = async (domain, service, data) => { sent.push([service, data.time, data.new_time, data.temperature]); await new Promise((r) => setTimeout(r, 20)); };
  card._dragStart(ptr(px("07:00")), fakeEl({ day: "mon", kind: "heat", time: "07:00" }));
  card._dragMove(ptr(px("08:00"))); card._dragEnd(ptr(px("08:00")));
  card._dragStart(ptr(px("08:00")), fakeEl({ day: "mon", kind: "heat", time: "08:00" }));
  card._dragMove(ptr(px("09:00"))); card._dragEnd(ptr(px("09:00")));
  await settle(); await settle();
  assert.equal(card._error, null, card._error);
  assert.deepEqual(sent, [["schedule_update", "07:00", "08:00", 68], ["schedule_update", "08:00", "09:00", 68]]);
  assert.deepEqual(card._entries("heat", "mon").map((e) => e.time), ["09:00", "22:00"]);
  assert.equal(card._moves.length, 0, "moves let go once saved");
}
console.log("check 26 passed");
// 27. an open sensor picker survives the rows being rebuilt
{
  const [card] = make("heat", base(), [S("mon", "07:00", "sensor.a_temperature")]);
  card._openDay("mon"); card._ensureRows();
  card._expand(card._rows.findIndex((r) => r.kind === "sensors"));
  card._rows = null; card._ensureRows();
  assert(card._expanded && card._rows.includes(card._expanded) && card._expanded.orig === "07:00");
}
console.log("check 27 passed");
