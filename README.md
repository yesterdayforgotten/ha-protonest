<p align="center">
  <img alt="ProtoNest" src="https://raw.githubusercontent.com/yesterdayforgotten/ha-protonest/main/custom_components/protonest/brand/logo@2x.png" width="260">
</p>

# ProtoNest

A Home Assistant integration for Nest Learning Thermostats and Nest Temperature
Sensors, using the protobuf API that the home.nest.com web app uses. Changes are
pushed to Home Assistant as they happen (no polling), and settings the Nest app
doesn't expose are available too.

Unofficial: Google can change or close this API at any time.

## Install

### With HACS

1. In Home Assistant, open **HACS**, then the **⋮** menu (top right) →
   **Custom repositories**.
2. Enter `https://github.com/yesterdayforgotten/ha-protonest`, choose the type
   **Integration**, and select **Add**.
3. Search HACS for **ProtoNest**, open it, and select **Download**.
4. Restart Home Assistant.
5. Get credentials (below), then go to **Settings → Devices & services → Add
   integration → ProtoNest** and paste them.

HACS then offers ProtoNest updates like any other integration.

### By hand

1. Copy `custom_components/protonest` into your Home Assistant configuration's
   `custom_components/` folder and restart Home Assistant.
2. Get credentials (below), then add the integration as in step 5 above.

### Credentials

ProtoNest signs in with your Google session, captured from a browser once.

1. Open a **Firefox private window**. (Chrome and Edge bind the session to the
   device; credentials copied from them stop working within hours.)
2. Open the developer tools (F12) → **Network**, and enable **Persist Logs**.
3. Go to `home.nest.com`. Click the shield in the address bar and turn off
   **Enhanced Tracking Protection** for the site. Sign in with Google.
4. Filter the requests for `issueToken`. Right-click the **iframerpc** request →
   **Copy Value → Copy as cURL**, and paste that into Home Assistant.
5. Close the private window **without signing out**. Signing out ends the
   session.

Google replaces some session cookies every time a token is issued; ProtoNest
saves the new ones automatically. Don't use the same capture in two places (two
installs, or HA and a script): each would replace cookies the other still uses.
If Google ever rejects the session, Home Assistant asks for a new capture.

## What you get

For each thermostat:

| Platform | Entities |
|---|---|
| Climate | Mode (off / heat / cool / heat-cool), setpoints, Eco preset, fan auto/on, target humidity (with a humidifier or dehumidifier) |
| Fan | Run the fan now, for the thermostat's remembered duration |
| Select | Target sensor (the thermostat, a paired sensor, average of all sensors, or the sensor schedule); sensor for each schedule period; fan schedule run time, start and end; fan run duration; Early-On; Farsight display; display units; humidifier operation |
| Switch | Fan schedule, Auto-Schedule, Home/Away Assist, Cool to Dry, Sunblock, Airwave, wake on approach, filter reminder, humidity control, run fan while humidifying, eco and safety limits on/off |
| Number | Eco heating/cooling limits, safety low/high limits, filter reminder threshold (runtime hours) |
| Button | Filter replaced |
| Sensor | Temperature, humidity, thermostat's own sensor, filter runtime / reminder level / last changed, time to target, compressor lockout, running stages, battery voltage |
| Binary sensor | Filter replacement needed, Leaf, fan running, online |

For each temperature sensor: temperature and battery voltage.

### Actions

On a thermostat's climate entity:

- `protonest.run_fan`: run the fan for a set time (up to a week).
- `protonest.average_sensors`: control to the average of the paired sensors you choose.
- `protonest.schedule_add` / `schedule_update` / `schedule_remove`: edit the
  weekly heat, cool or range schedule. Times are on quarter hours; temperatures
  are in the thermostat's display units.
- `protonest.pair_sensor` / `unpair_sensor`: pair or unpair a temperature sensor.

## Things to know

- **The filter change date is one-way.** Setting it (the Filter replaced button,
  or a date) makes the thermostat recalculate the runtime from its history; the
  previous runtime count can't be restored.
- **The filter reminder counts runtime, not calendar time.** The threshold is in
  HVAC runtime hours (500 by default) and can be set per thermostat.
- **Sensor averaging** starts with about 19 seconds at 0 °C while the thermostat
  computes its first average.
- **The sensor schedule has four fixed periods** (7:00, 11:00, 16:00, 21:00).
  Only the sensor for each period can be changed; the thermostats ignore other
  period times.
- Setting a temperature while in Eco turns Eco off first, as the Nest app does.
- **A thermostat that stops responding goes unavailable.** If a change isn't
  confirmed within 2 minutes (and the thermostat has confirmed nothing since),
  or Nest reports it offline, its entities become unavailable until it confirms
  something again; its **Online** sensor stays available and turns off, for
  automations. Nest does apply changes made while a thermostat is away once it
  reconnects, but a change made on the device in the meantime (even by just
  using it) can replace yours; Home Assistant then shows a notification.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                     # client library tests
docker build -f dev/Dockerfile.test -t protonest-test dev
docker run --rm -v "$PWD":/work -w /work protonest-test python -m pytest tests_ha   # HA tests
docker compose -f dev/docker-compose.yml up -d # dev Home Assistant on :8123
```

- `custom_components/protonest/pynestproto/` is the Home Assistant–independent
  client: auth, transport, Observe stream cache, read-modify-write writes, and
  the device model.
- `tools/` has command-line tools: `save_credentials.py`, `observe_cli.py`
  (live view and recording), `write_cli.py` (dry-run-first changes),
  `decode_fixture.py`.
- `scripts/gen_protos.py` regenerates the protobuf modules from `proto/`.
- Tests that use real captures skip when the (gitignored) captures are absent.

## Credits

The `.proto` schemas in `proto/` come from
[tronikos/nest_legacy](https://github.com/tronikos/nest_legacy) (Apache-2.0);
see `proto/README.md`.
