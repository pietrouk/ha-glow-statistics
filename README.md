# Glow Statistics

A Home Assistant custom integration that imports the half-hourly readings of UK smart meters from
[Hildebrand Glow](https://glowmarkt.com/) (the Bright app, DCC data) as **long-term statistics**,
ready to select in the Energy dashboard.

UK smart-meter data often reaches Glow hours or days late. Integrations that only read "usage today"
never count readings that arrive after midnight. This one writes every reading into the hour it belongs
to, and each refresh rewrites the last 10 days, so late readings are added when they arrive.

What it creates:

| Statistic ID | Unit | Energy dashboard field |
| --- | --- | --- |
| `glow_statistics:electricity_consumption` | kWh | Electricity grid → Grid consumption |
| `glow_statistics:electricity_cost` | GBP | Grid consumption → "Use an entity tracking the total costs" |
| `glow_statistics:gas_consumption` | kWh | Gas consumption |
| `glow_statistics:gas_cost` | GBP | Gas → "Use an entity tracking the total costs" |
| `glow_statistics:electricity_export` | kWh | Return to grid (only if your meter reports export) |

These are *external statistics*: they have no entity or state class, so Home Assistant stores them
exactly as written. They need no `customize:`, template sensors or other YAML. The integration
also adds a few sensors for cards and automations:

| Entity (per fuel) | Meaning | Device / state class |
| --- | --- | --- |
| `sensor.glow_statistics_<fuel>_consumption_today` | kWh Glow has for today (UK day) so far | energy / `total`, `last_reset` = UK midnight |
| `sensor.glow_statistics_<fuel>_cost_today` | today's cost incl. standing charge | monetary / `total`, `last_reset` = UK midnight |
| `sensor.glow_statistics_<fuel>_unit_rate` | unit rate in use today (GBP/kWh) | `measurement` |
| `sensor.glow_statistics_<fuel>_standing_charge` | standing charge in use today (GBP/day) | `measurement` |
| `sensor.glow_statistics_<fuel>_latest_reading` | end of the newest half-hour Glow has | timestamp (diagnostic) |

For the Energy dashboard, use the `glow_statistics:` statistics, not the "today" sensors. The
sensors only see readings that arrive before midnight, just like a "usage today" sensor.

It runs alongside other Glow integrations such as `hildebrandglow_dcc`. Its IDs, entities and
devices are all different, so you can compare the two.

## Installation

### HACS (custom repository)

1. In HACS, open the ⋮ menu → **Custom repositories**.
2. Add `https://github.com/pietrouk/ha-glow-statistics` with type **Integration**.
3. Search for **Glow Statistics**, download it, and restart Home Assistant.

### Manual

Copy `custom_components/glow_statistics/` into your Home Assistant `config/custom_components/`
folder, then restart Home Assistant.

## Setup

1. **Settings → Devices & services → Add integration → Glow Statistics**.
2. Enter the email address and password you use for the Bright app. They are stored in Home
   Assistant only and sent only to `api.glowmarkt.com`.
3. The first import runs in the background. It walks back in 10-day steps until Glow has no more
   data (at most the *backfill limit*, about 5 years by default). Each step is one API request, about
   one per second, so a few years of history takes a few minutes. Watch the
   `..._latest_reading` sensor or **Developer tools → Statistics** for progress.

## Options

**Settings → Devices & services → Glow Statistics → Configure** has three pages:

* **Refresh interval and look-back**
  * *Refresh interval* (default 60 min). Glow receives DCC data a few times a day, so polling more
    often gains nothing.
  * *Look-back window* (default 10 days). Each refresh fetches and rewrites this many days.
  * *Backfill limit* (default 1830 days). The furthest back the first import looks.
* **Add a tariff override** (dated prices for one fuel, see below).
* **Remove tariff overrides**.

### Tariffs and cost

Costs are calculated per hour: kWh × unit rate. Each UK day's standing charge goes in the first hour
of that day, which is 00:00 GMT or 23:00 UTC in summer. Prices include VAT.

* **By default** the integration uses the unit rate and standing charge from Glow's tariff API. Glow
  only reports the *current* tariff. The integration records each change it sees, so later days keep
  the price that applied on that day. Days before the first recorded tariff use the oldest one.
* **Overrides** replace Glow's tariff for the days they cover. An override has a fuel, a first day,
  an optional last day, a standing charge (p/day) and a unit rate (p/kWh). An override without a
  last day lasts until the next override of that fuel starts. Days outside every override use Glow's
  tariff.
* Adding or removing an override recalculates the **whole** cost history. The kWh statistics do
  not change.

#### Example

Gas was 31.28475p/day + 6.04695p/kWh until 26 Feb 2026, and 25.35p/day + 5.6868p/kWh from
27 Feb 2026. Electricity was 40.446p/day + 23.52p/kWh until 3 Sep 2026, and Glow's tariff after that.
Add three overrides:

| Fuel | First day | Last day | Standing charge (p/day) | Unit rate (p/kWh) |
| --- | --- | --- | --- | --- |
| Gas | 2020-01-01 | *(empty)* | 31.28475 | 6.04695 |
| Gas | 2026-02-27 | *(empty)* | 25.35 | 5.6868 |
| Electricity | 2020-01-01 | 2026-09-03 | 40.446 | 23.52 |

Use any first day that is earlier than your oldest data. 2020-01-01 is fine.

## Adding the statistics to the Energy dashboard

**Settings → Dashboards → Energy** (or the Energy dashboard's ⋮ → **Energy configuration**):

1. **Electricity grid → Add consumption** (or **Add grid connection**):
   * *Consumed energy*: **Glow electricity consumption** (`glow_statistics:electricity_consumption`).
   * *Cost*: choose **Use an entity tracking the total costs**, then **Glow electricity cost**
     (`glow_statistics:electricity_cost`).
   * If your meter reports export, set *Returned energy* to **Glow electricity export**.
2. **Gas consumption → Add gas source**:
   * *Gas usage*: **Glow gas consumption** (`glow_statistics:gas_consumption`).
   * *Cost*: **Use an entity tracking the total costs** → **Glow gas cost**
     (`glow_statistics:gas_cost`).
3. Save. **Settings → Repairs** and the Energy configuration page should show no issues. The
   integration's tests run Home Assistant's own `energy/validate` on exactly this setup.

The pickers list a statistic once it has data, so wait for the first import to finish. If you also
keep another Glow integration in the dashboard, both are counted. Add the new statistics as an
extra grid or gas source only if you want to compare them, and then remove one of the two.

### Gas in m³?

The Glowmarkt API serves gas only in kWh. Its `gas.consumption` resource is converted from the
meter's m³ by Glow. There is no m³ statistic. kWh is what UK suppliers bill.

## How backfill and late data work

* The API returns readings in half-hour slots (UTC), at most 10 days per request. The integration
  sums them into UTC hours, which is how Home Assistant stores statistics.
* **First run:** it fetches everything as far back as Glow has data and writes one row per hour,
  with a running total starting at 0.
* **Every refresh:** it asks Glow to fetch outstanding DCC data (`catchup`), then fetches the
  look-back window. It merges the readings with what is already stored and rewrites every hour from
  the start of the window. The running total continues from the last stored hour before the window.
  A reading that arrives three days late is added to its own hour, and every later hour's total
  moves up by the same amount. Hours before the window never change.
* If Home Assistant was off for longer than the window, the next refresh starts from the last
  stored hour.
* Hours Glow has no data for yet are not written. The statistics end at the newest reading.
* **Re-import:** the `glow_statistics.reimport` action (Developer tools → Actions) fetches
  everything from a date onwards and rewrites from there. Use it to fill a gap older than the window
  or to extend the backfill:

  ```yaml
  action: glow_statistics.reimport
  data:
    start_date: "2025-01-01"
  ```

* To start completely fresh, delete the integration, remove the `glow_statistics:` statistics in
  **Developer tools → Statistics**, and add the integration again.

## API notes

The integration uses `https://api.glowmarkt.com/api/v0-1/` with the Bright app's public
`applicationId`, following Glowmarkt's *API Description Individual User* (v1.7). The login token
lasts 7 days and is reused. Requests are spaced out during a backfill. A normal refresh makes about
three requests per fuel per hour.

## Troubleshooting

* Download diagnostics from the integration's ⋮ menu. Credentials and resource IDs are redacted.
* Debug logging:

  ```yaml
  logger:
    logs:
      custom_components.glow_statistics: debug
  ```

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

The tests use `pytest-homeassistant-custom-component` with a fake Glowmarkt API. They cover the
config and options flows, backfill, late-data correction, Glow tariff and override costs, and
Energy dashboard validation.

## Licence

MIT
