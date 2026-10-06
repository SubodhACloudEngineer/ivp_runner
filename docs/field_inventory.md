# Field inventory (sanitised)

The list of Mist API fields this project may read. Every path below was seen
in a real captured response. The full inventory, with example values, lives
in `samples/FIELD_INVENTORY.md`, which is untracked.

This file deliberately contains no device names, addresses or example values.

## Notation

- Paths are relative to **one item** of the response. For list endpoints that means
  one element of the top-level array.
- `<port>` is an AP ethernet port key. Observed: `eth0`, `eth1`.
- `<band>` is a radio band key. Observed: `band_24`, `band_5`, `band_6`.
- **Presence** values:
  - `always` means the field is on every item.
  - `runtime` means it is present only while the AP is connected and reporting.
    It is absent on disconnected APs.
  - `conditional` means it is sometimes absent; the note says when.

## Endpoint: AP stats

`GET /api/v1/sites/{site_id}/stats/devices?type=ap`, which returns a list with one item per AP.

The per-device endpoint `GET /api/v1/sites/{site_id}/stats/devices/{device_id}` returns
the same key set as one item of this list, so it is not needed.

**Pagination is unverified.** The captures only prove that `limit` is honoured (limit=1000
returned all 91 APs). The collector requests `page=1,2,…` and stops at a short page. If the
server ignores `page` and repeats items, the collector stops with an error rather than
duplicating them. `scripts/capture_samples.py` now runs a two-page probe with limit=10 and
records response headers, so the next capture confirms or refutes this.

| Path | Type | Presence | Notes |
|---|---|---|---|
| `id` | str | always | Device UUID |
| `name` | str | always | |
| `mac` | str | always | |
| `model` | str | always | |
| `type` | str | always | |
| `status` | str | always | Observed values: `connected`, `disconnected` |
| `deviceprofile_name` | str | conditional | Absent on disconnected APs |
| `last_seen` | int | runtime | Epoch seconds |
| `uptime` | int | runtime | Seconds |
| `num_wlans` | int | runtime | Total across all bands |
| `radio_stat` | object | runtime | Keys are `<band>` |
| `radio_stat.<band>.num_wlans` | int | runtime | Number of WLANs on that band. No SSID names. |
| `radio_stat.<band>.disabled` | bool | conditional | Present only when the band is disabled |
| `radio_stat.<band>.power` | int | runtime | Radio transmit power (RRM-managed). **Not** PoE power mode. |
| `power_constrained` | bool | runtime | |
| `power_opmode` | str | runtime | Never seen populated. **Meaning unknown; do not use.** |
| `power_src` | str | runtime | |
| `power_srcs` | list[str] | runtime | |
| `power_needed` | int | runtime | mW |
| `power_avail` | int | runtime | mW |
| `power_budget` | int | runtime | Equals `power_avail - power_needed`. Can be negative. |
| `lldp_stat.power_allocated` | int | runtime | mW |
| `lldp_stat.power_requested` | int | runtime | mW |
| `lldp_stat.power_draw` | int | runtime | mW |
| `lldp_stat.ap_port_name` | str | runtime | AP port facing the LLDP neighbour |
| `lldp_stat.system_name` | str | runtime | Upstream switch name |
| `lldp_stat.port_id` | str | runtime | Upstream switch port |
| `ip` | str | runtime | |
| `ip_stat.ip` | str | runtime | |
| `ip_stat.netmask` | str | runtime | Dotted-quad |
| `ip_stat.gateway` | str | runtime | |
| `ip_stat.dhcp_server` | str | runtime | |
| `ip_stat.dns` | list[str] | runtime | **Configured** resolvers only. No resolution status. |
| `ip_stat.ips` | object | runtime | Key is an AP-internal interface name, not a VLAN ID |
| `ip_config.type` | str | conditional | Config echo. Has no VLAN key. |
| `port_stat` | object | runtime | Keys are `<port>`. Some models have no `eth1`. |
| `port_stat.<port>.up` | bool | runtime | |
| `port_stat.<port>.speed` | int | runtime | Mbps |
| `port_stat.<port>.full_duplex` | bool | runtime | |
| `port_stat.<port>.rx_errors` | int | runtime | Cumulative counter; resets on reboot |
| `port_stat.<port>.rx_pkts` | int | runtime | Cumulative |
| `port_stat.<port>.tx_pkts` | int | runtime | Cumulative |

### Fields that do not exist in this response

Checks must not assume any of these.

- Any TX error counter. `rx_errors` is the only key containing `err`.
- Any SSID name, or any mapping from an SSID to an AP or radio.
- Any VLAN ID for the AP's management interface.
- Any DNS resolution or health status.

## Endpoint: site derived WLANs

`GET /api/v1/sites/{site_id}/wlans/derived`, which returns a list with one item per WLAN.

**This is current state, not design intent.** Checks take their expected values
from the catalogue, not from here. It is listed for reference only.

| Path | Type | Presence | Notes |
|---|---|---|---|
| `ssid` | str | always | |
| `enabled` | bool | always | |
| `hide_ssid` | bool | always | |
| `bands` | list[str] | always | Values look like `"24"`, `"5"`, `"6"` |
| `apply_to` | str | always | |

## Endpoint: site

`GET /api/v1/sites/{site_id}`, which returns one object.

| Path | Type | Presence | Notes |
|---|---|---|---|
| `id` | str | always | |
| `name` | str | always | |
| `timezone` | str | always | IANA name. Used for site-local timestamp rendering. |

## Answerability

| Test ID | Coverage | Fields | Gap |
|---|---|---|---|
| AP-00 | full | `status`, plus the runtime fields used by AP-01…05 | — |
| AP-01 | **partial** | `radio_stat.<band>.num_wlans`, `radio_stat.<band>.disabled` | Counts only. Can't match SSID names. |
| AP-02 | full | `power_constrained` | `power_opmode` has unknown meaning and is not used |
| AP-03 | **partial** | `ip_stat.ip`, `ip_stat.netmask`, `ip_stat.dns` | Checks the subnet, not the VLAN. Checks DNS configuration, not DNS resolution. |
| AP-04 | full | `port_stat.<port>.up`, `.speed`, `.full_duplex` | The expected speed comes from the catalogue |
| AP-05 | **partial** | `port_stat.<port>.rx_errors`, `uptime` | RX only, as the delta over the run window. No TX counter exists. |
