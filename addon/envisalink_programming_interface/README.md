# Envisalink Programming Interface — Home Assistant Add-on

A web-based programming interface for Honeywell/ADEMCO Vista alarm panels,
accessed through Home Assistant's sidebar. Read and modify panel configuration
— zone types, zone names, system delays, reporting settings, custom words — all
from your browser, no physical keypad or laptop serial cable required.

> **Status:** Early development / community project. Use at your own risk.

> **⚠️ Important:** This tool is intended for users who are **reasonably
> familiar with the Vista panel's programming interface** (installer menus,
> field numbers, `*99` exit, etc.). Scanning and saving require the panel to
> enter programming mode, during which zone monitoring is suspended. If a
> **power outage, network interruption, or software crash** occurs while the
> panel is in programming mode, the addon cannot automatically exit it — the
> panel will remain in programming mode until someone manually presses `*99`
> on a physical keypad. For this reason, **you should have a physical keypad
> (or a second alpha keypad) readily accessible** so you can recover from an
> interrupted session. Do not use this addon if you are unsure how to
> recognise when your panel is stuck in programming mode or how to exit it
> manually.

---

## Table of Contents

- [Envisalink Programming Interface — Home Assistant Add-on](#envisalink-programming-interface--home-assistant-add-on)
  - [Table of Contents](#table-of-contents)
  - [Supported Hardware](#supported-hardware)
    - [Alarm Panels](#alarm-panels)
    - [Interface Modules](#interface-modules)
    - [Zone Limitations](#zone-limitations)
  - [How It Works](#how-it-works)
  - [Connection Modes](#connection-modes)
    - [HA Mode (Default — Recommended)](#ha-mode-default--recommended)
    - [Direct EVL Mode (Fallback Only)](#direct-evl-mode-fallback-only)
  - [Features](#features)
    - [Zones Tab](#zones-tab)
    - [System Tab](#system-tab)
    - [Custom Words Tab](#custom-words-tab)
    - [Reporting Tab](#reporting-tab)
    - [Users Tab](#users-tab)
    - [Keypads Tab](#keypads-tab)
    - [Event Log Tab](#event-log-tab)
    - [Virtual Keypad Tab](#virtual-keypad-tab)
    - [Bus Sniffer Tab](#bus-sniffer-tab)
    - [LCD Display (Always Visible)](#lcd-display-always-visible)
  - [Installation](#installation)
    - [Prerequisites](#prerequisites)
    - [HACS (Recommended)](#hacs-recommended)
    - [Manual Installation](#manual-installation)
    - [Updating](#updating)
  - [Screenshots](#screenshots)
    - [Installer Code Prompt](#installer-code-prompt)
    - [Welcome / Sync Screen](#welcome--sync-screen)
    - [Zone Configuration](#zone-configuration)
    - [System Settings](#system-settings)
    - [Virtual Keypad](#virtual-keypad)
  - [First-Time Setup](#first-time-setup)
    - [Setting the User Code](#setting-the-user-code)
  - [Using the Interface](#using-the-interface)
    - [LCD Display](#lcd-display)
    - [Zones Tab](#zones-tab-1)
    - [System Tab](#system-tab-1)
    - [Custom Words Tab](#custom-words-tab-1)
    - [Reporting Tab](#reporting-tab-1)
    - [Users Tab](#users-tab-1)
    - [Keypads Tab](#keypads-tab-1)
    - [Event Log Tab](#event-log-tab-1)
    - [Virtual Keypad Tab](#virtual-keypad-tab-1)
    - [Bus Sniffer Tab](#bus-sniffer-tab-1)
  - [Re-scanning](#re-scanning)
  - [Important Warnings \& Disclaimers](#important-warnings--disclaimers)
    - [⚠️ General](#️-general)
    - [⚠️ Alarm Monitoring](#️-alarm-monitoring)
    - [⚠️ Programming Mode](#️-programming-mode)
    - [⚠️ Envisalink Limitations](#️-envisalink-limitations)
    - [⚠️ Data Accuracy](#️-data-accuracy)
    - [⚠️ Security](#️-security)
  - [Troubleshooting](#troubleshooting)
    - [The addon won't connect / shows "Disconnected"](#the-addon-wont-connect--shows-disconnected)
    - [Scan fails or gets stuck](#scan-fails-or-gets-stuck)
    - ["Scan interrupted" error](#scan-interrupted-error)
    - [Settings don't match what I see on the keypad](#settings-dont-match-what-i-see-on-the-keypad)
    - [Panel stuck in programming mode](#panel-stuck-in-programming-mode)
    - [Direct EVL mode: "Connection refused" or frequent disconnects](#direct-evl-mode-connection-refused-or-frequent-disconnects)
  - [Technical Details](#technical-details)
  - [Credits \& License](#credits--license)

---

## Supported Hardware

### Alarm Panels

| Panel | Status | Notes |
|-------|--------|-------|
| **Honeywell/ADEMCO Vista 20P** | ✅ Fully supported | Primary development & testing target |
| **Honeywell/ADEMCO Vista 15P** | ✅ Should work | Same programming interface as the 20P |
| Other Honeywell Vista panels | ⚠️ Unknown | May work if they use the same `*58`/`*82` programming mode — untested |
| **DSC panels** | ❌ Not supported | This addon is designed for Honeywell/ADEMCO programming sequences only |

### Interface Modules

| Device | Status | Notes |
|--------|--------|-------|
| **EyezOn Envisalink 4** | ✅ Supported | Recommended; tested in both HA mode and Direct EVL mode |
| **EyezOn Envisalink 3** | ✅ Supported | Same protocol; should work identically |
| **Uno IP Hybrid** (Envisalink-compatible) | ⚠️ Untested | May work in HA mode if the `envisalink_new` integration supports it |
| Other IP alarm interfaces | ❌ Not supported | Requires EVL-compatible TPI protocol |

### Zone Limitations

The addon reads and programs **zones 1–48**:

| Group | Zones | Notes |
|-------|-------|-------|
| **Hardwired** | 1–8 | Main board wired zones; full support including HW type & response time |
| **Expansion** | 9–16 | Zone expander slots; type, name, partition, report code supported |
| **Wireless** | 17–48 | Wireless zones; type, name, partition, report code supported |

---

## How It Works

The addon sends keypresses to your alarm panel through the Envisalink module —
exactly the same keypresses a technician would type on a physical keypad to
enter programming mode, navigate menus, and read or write settings. It then
reads back the 32-character LCD display to extract the panel's responses.

**This means:**

- The panel is temporarily placed into **installer programming mode** during
  scans and saves. Normal alarm operation (monitoring, arming, disarming)
  is paused while programming mode is active.
- Each operation (scan, save) takes several seconds because the addon must
  wait for the panel to process each keypress and update the display.
- A full scan of all settings takes approximately **3–6 minutes**.
- The addon always exits programming mode when finished (and has a recovery
  mechanism in case of interruption).

---

## Connection Modes

The addon supports two ways of communicating with the panel. **HA Mode is
strongly recommended** — it works in parallel with the normal
`envisalink_new` integration, so your alarm system continues to operate
normally between programming operations. Direct EVL Mode should only be
used as a fallback when troubleshooting system-level issues (e.g. the HA
integration is not loading, the entity states are stale, or you need to
rule out the middleware layer during debugging).

### HA Mode (Default — Recommended)

Communicates through the **envisalink_new** Home Assistant integration. The
addon connects to HA's WebSocket API, subscribes to keypad sensor updates,
and sends keypresses via the `alarm_keypress` service. It uses two HA
entities:

- **Keypad sensor** (`sensor.envisalink_new_keypad_partition_1`) — the addon
  subscribes to state-change events on this sensor via the HA WebSocket API
  to receive real-time display updates from the panel.
- **Alarm control panel** (`alarm_control_panel.envisalink_new_partition_1`) —
  the addon sends keypresses to this entity via the
  `envisalink_new/alarm_keypress` service through the HA REST API.

Because the addon piggybacks on the integration's existing TCP connection to
the Envisalink, there is **no conflict** — the integration keeps monitoring
zones, handling arm/disarm, and reporting state to HA while the addon reads
or writes panel programming data. Entity IDs are auto-discovered on first
boot and saved; no manual configuration is required.

**Advantages:**
- Works alongside the integration — alarm monitoring continues normally
  between programming operations
- No need to disable the integration
- Uses HA's existing connection to the Envisalink
- Authenticates automatically via the HA Supervisor token

**Requirements:**
- The [envisalink_new](https://github.com/ufodone/envisalink_new) custom
  integration must be installed and configured in Home Assistant
- The addon auto-detects the keypad sensor and partition entity, or you can
  configure them manually via the Connection Settings

### Direct EVL Mode (Fallback Only)

Connects directly to the Envisalink module over TCP (port 4025), bypassing
Home Assistant entirely. **Use this only when HA Mode is not working** — for
example, if the `envisalink_new` integration won't load, entity states are
not updating, or you need to isolate a problem to the middleware layer.

**Advantages:**
- Works without the HA integration installed
- Useful for diagnosing integration or connectivity issues

**Disadvantages:**
- **The Envisalink only allows ONE TCP client at a time.** You must first
  **disable** the `envisalink_new` integration in HA (Settings → Devices &
  Services → Envisalink → Disable) before connecting in Direct EVL mode.
  Otherwise, the EVL will force-close one of the two connections.
- Alarm monitoring is offline while using Direct EVL mode
- You must re-enable the integration after you are done

**Requirements:**
- Envisalink IP address and TPI password
- The HA integration must be disabled first

You can switch between modes at any time via the **Connection Settings**
(click the connection indicator in the header).

---

## Features

### Zones Tab
- View and edit **zone type** (Entry/Exit, Perimeter, Interior Follower, etc.)
- View and edit **zone names** using the panel's built-in vocabulary (3-word
  descriptors, e.g. "FRONT DOOR", "FAMILY ROOM MOTION")
- View and edit **hardwire type** (EOL, Normally Closed, Normally Open, Zone
  Doubling, Double Balanced) — zones 1–8 only
- View and edit **response time** (10 ms, 350 ms, 700 ms, 1.2 s) — zones 1–8 only
- View and edit **report code** (00 = disabled, 01–15 = enabled)
- View and edit **partition assignment** (1, 2, or 3)
- **Interactive zone bypass** — bypass or clear bypass on individual zones
  directly from the table (requires user/master code)
- **Bypass scan** — read which zones are currently bypassed directly from
  the panel's LCD display (amber "⊘ Read Bypass" button)
- Zones organized in three collapsible groups: Hardwired (1–8), Expansion
  (9–16), Wireless (17–48)
- Changes are written to the panel immediately on Save

### System Tab
- **Partition 1 Account Number** — central station account (4 or 10 digits)
- **Fire Alarm Timeout** — whether the sounder stops at timeout or runs
  continuously (UL fire installations require "No timeout")
- **Bell (Alarm) Timeout** — how long the alarm sounder runs (4/8/12/16 min)
- **Exit Delay** (Part 1 / Part 2) — seconds before alarm arms after arming
- **Entry Delay 1** (Part 1 / Part 2) — time to disarm after entry
- **Entry Delay 2** (Part 1 / Part 2) — alternate entry delay
- **Panel Clock** — read the panel's current date/time and compare against
  your browser clock; one-click sync to set the panel clock

### Custom Words Tab
- View and edit the panel's 12 programmable word slots:
  - **Words 1–10:** user-defined custom vocabulary (words 245–254), used in
    zone descriptors for location-specific names not in the standard vocabulary
    (e.g. "DECK", "BONUS", "STUDIO")
  - **Words 11–12:** partition names
- Each word supports up to 10 characters (A–Z, 0–9)

### Reporting Tab
- View and edit **phone numbers** (dialer and pager)
- View and edit **account numbers**
- View and edit **report format** settings
- View and edit **pager configuration**
- Delete/clear reporting fields

### Users Tab
- Manage user codes 01–49 (write-only — the panel cannot report which
  slots are active)
- **User 01 (Installer):** change code only (via `*20` programming field)
- **User 02 (Master):** change code only (code entered twice for confirmation)
- **Users 03–49:** add/delete code, set authority level (0–4), set partition
- All user codes are exactly **4 digits**

### Keypads Tab
- View and configure keypads 2–8 (bus addresses 17–23, fields `*190`–`*196`)
- Keypad 1 (address 16) is factory-set and shown as read-only
- **Partition/Enable:** Disabled, Part 1, Part 2, or Common
- **Sound Option:** All Sounds, or suppression levels 1–3
- Settings are read during panel scan and written stepwise with panel feedback

### Event Log Tab
- Fetch entries from the panel's internal event log
- Configurable number of entries (5, 10, 25, 50, 75, 100)
- Decoded event descriptions using Contact ID (CID) codes mapped from
  the Vista 20P manual
- Raw hex data alongside human-readable event descriptions

### Virtual Keypad Tab
- Full virtual keypad — press any key as if on a physical keypad
- Direct passthrough to the panel
- **Recovery button** — sends a force-exit sequence to return the panel to
  normal mode if programming is interrupted

### Bus Sniffer Tab
- Records every keypad display change pushed by the panel in real time
- Useful for debugging, understanding panel behaviour, and reverse-engineering
  programming sequences
- Copy log to clipboard, view previous scan logs

### LCD Display (Always Visible)
- Live 2-line LCD display showing the panel's current keypad text — mirrors
  what a physical 6160/6150 keypad would show
- LED indicator row: READY, ARMED, BYPASS, TROUBLE, ALARM, FIRE, PROG
- Always visible at the top of the page regardless of which tab is active

---

## Installation

### Prerequisites

- **Home Assistant** (2024.1 or later recommended)
- **Home Assistant Supervisor** — the addon requires the Supervisor and runs as
  a Docker container. It does **not** work on HA Core-only installations.
- **EyezOn Envisalink 3 or 4** connected to your Vista 20P panel
- For HA Mode: the **envisalink_new** custom integration installed and working

### HACS (Recommended)

1. Open **HACS** in your Home Assistant sidebar
2. Go to the **three-dot menu** (⋮) → **Custom repositories**
3. Add the repository URL:
   ```
   https://github.com/MichaelB2018/envisalink_new
   ```
   Category: **Integration** (the addon is bundled with the integration repo)
4. After adding, navigate to **Settings → Add-ons → Add-on Store**
5. Search for **"Envisalink Programming Interface"**
6. Click **Install**
7. After installation, enable **"Show in sidebar"** on the add-on's Info page
8. Click **Start**
9. The "Vista Programmer" entry will appear in your HA sidebar

### Manual Installation

1. Download or clone the repository:
   ```
   git clone https://github.com/MichaelB2018/envisalink_new.git
   ```
2. Copy the `addon/envisalink_programming_interface/` folder into your
   Home Assistant's `/addons/` directory (local add-ons):
   ```
   /addons/envisalink_programming_interface/
     ├── config.yaml
     ├── Dockerfile
     ├── requirements.txt
     └── app/
         ├── server.py
         ├── ha_client.py
         ├── evl_client.py
         ├── scanner.py
         ├── panel_commands.py
         ├── run.sh
         └── static/
             ├── index.html
             ├── css/style.css
             ├── js/ (app.js, zones.js, settings.js, etc.)
             └── images/
   ```
3. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
4. Click the **three-dot menu** (⋮) → **Check for updates** (or reload)
5. Your local addon "Envisalink Programming Interface" should appear under
   **Local add-ons**
6. Click **Install**, then enable **"Show in sidebar"** and **Start**

### Updating

To update the addon after pulling new code:

1. **HACS:** HACS will notify you of updates; click Update
2. **Manual:** Replace the addon folder with the new version, then go to the
   add-on's page in HA and click **Rebuild**

> **Important:** After updating, you must **Rebuild** and **Restart** the addon
> for changes to take effect. The addon runs inside a Docker container — simply
> editing files on disk does not update the running container.

---

## Screenshots

### Installer Code Prompt

On first launch, the addon prompts for your Vista 20P installer code and
optional user/disarm code. The live LCD display at the top mirrors your
panel's current state.

![Start Screen — Installer Code Prompt](https://raw.githubusercontent.com/MichaelB2018/envisalink_new/main/addon/envisalink_programming_interface/images/1%20-%20Start%20Screen.png)

### Welcome / Sync Screen

After entering the installer code, the welcome screen explains what the
initial scan does and lists important safety warnings. Click **Sync Panel
Config** to begin reading all panel settings.

![Welcome Screen — Panel Configuration Sync](https://raw.githubusercontent.com/MichaelB2018/envisalink_new/main/addon/envisalink_programming_interface/images/2%20-%20Welcome%20Screen.png)

### Zone Configuration

The Zones tab displays all 48 zones with editable fields for zone type,
name (3-word descriptor), hardwire type, response time, report code,
partition, and bypass status. Changes are written to the panel immediately
on Save.

![Zones Tab — Zone Configuration](https://raw.githubusercontent.com/MichaelB2018/envisalink_new/main/addon/envisalink_programming_interface/images/3%20-%20Zones%20Settings.png)

### System Settings

The System tab shows fire alarm timeout, bell timeout, exit/entry delays
(per-partition), and the panel clock with one-click sync.

![System Tab — System Settings](https://raw.githubusercontent.com/MichaelB2018/envisalink_new/main/addon/envisalink_programming_interface/images/4%20-%20System%20Settings.png)

### Virtual Keypad

A full virtual keypad for direct key passthrough. The EXIT PROG and
RECOVERY buttons provide a safety net for exiting stuck programming
sessions.

![Virtual Keypad Tab](https://raw.githubusercontent.com/MichaelB2018/envisalink_new/main/addon/envisalink_programming_interface/images/5%20-%20Virtual%20Keypad.png)

---

## First-Time Setup

1. **Open the addon** from the HA sidebar ("Vista Programmer")
2. The addon will attempt to auto-detect your Envisalink integration entities.
   If auto-detection fails, click the **connection indicator** in the header
   to open Connection Settings and configure manually:
   - **Keypad sensor entity:** e.g. `sensor.envisalink_new_keypad_1`
   - **Partition entity:** e.g. `alarm_control_panel.envisalink_new_partition_1`
3. Once connected (green dot in header), the LCD display will show the panel's
   current state
4. **Enter your installer code** when prompted (typically `4112` for Vista 20P
   — check your panel's documentation or installer)
5. Click **"Read Panel Config"** to perform the initial scan
6. Wait 3–6 minutes for the scan to complete — progress is shown on screen
7. Once the scan finishes, all tabs will populate with your panel's current
   configuration

### Setting the User Code

Some features (zone bypass, clock read/sync) require your **user/master code**
(not the installer code). Set this on the **System tab** under the user code
field, or when prompted by a bypass action.

---

## Using the Interface

### LCD Display

The 2-line LCD at the top mirrors your panel's physical keypad display in real
time. The LED indicators (READY, ARMED, BYPASS, etc.) reflect the actual panel
state. The **PROG** LED lights up when the addon detects the panel is in
programming mode.

### Zones Tab

Each row represents one hardwired zone (1–8). To edit a zone:

1. Change any field (type, name words, HW type, response time, report code,
   partition)
2. Click **Save** for that row
3. The addon enters programming mode, writes the zone settings, writes the
   zone name, then exits programming mode
4. The UI locks during save to prevent conflicting panel commands
5. A green ✓ confirms success; red ✗ indicates an error

**Zone Bypass:** Click the bypass pill button to toggle a zone's bypass state.
The amber **Clear** button clears ALL zone bypasses (equivalent to pressing
OFF on the keypad). Use the amber **⊘ Read Bypass** button in the tab header
to read which zones are currently bypassed directly from the panel's LCD
display (the BYPASS LED must be active).

### System Tab

Each setting has a description of what it does and its panel field number
(e.g. `*34` for exit delay). Edit the value and click **Save**.

**Panel Clock:** Click **Read Clock** to fetch the panel's current time. The
UI will lock and wait until the panel returns to normal mode (~30 seconds)
before releasing control. Click **Sync Now** to set the panel clock to your
browser's current time.

### Custom Words Tab

The panel supports 12 programmable word slots:
- **Words 1–10:** user-defined vocabulary (slots 245–254), used in zone
  descriptors for custom location names
- **Words 11–12:** partition names

To edit a custom word:
1. Type the new word in the text field (A–Z, 0–9, up to 10 characters)
2. Click **Save**
3. Re-scan the Zones tab to see the updated word in zone name dropdowns

### Reporting Tab

Configure central station reporting — phone numbers, account numbers, report
format, and pager settings. Each field shows its panel field number for
cross-reference with the Vista 20P Programming Guide.

### Users Tab

Manage user codes 01–49. This is a **write-only** interface — the panel does
not provide a way to read back which user codes are active.

- **User 01 (Installer):** change installer code via `*20` programming field
- **User 02 (Master):** change master code (entered twice for confirmation)
- **Users 03–49:** add or delete codes, set authority level and partition
  assignment
- All codes are exactly **4 digits**. The panel beeps once to confirm success.

### Keypads Tab

View and configure additional keypads (2–8, bus addresses 17–23).

- Keypad 1 (address 16) is factory-set and shown as read-only
- For each keypad, set the **partition/enable** (Disabled, Part 1, Part 2,
  Common) and **sound option** (All Sounds, or suppression levels 1–3)
- Changes are written to the panel using stepwise programming with display
  feedback

### Event Log Tab

Fetches entries from the panel's internal event log. Select how many entries
to fetch and click **Fetch Log**. The raw hex data is decoded into human-
readable event descriptions using Contact ID (CID) codes from the Vista 20P
manual.

### Virtual Keypad Tab

A direct passthrough to the panel — every button press sends the corresponding
key to the panel via the Envisalink. Use this for any panel operation not
covered by the other tabs.

The **Recovery** button sends a force-exit sequence (`*00`, `0`, `00*`, `*99`)
that safely exits any programming mode the panel might be stuck in.

### Bus Sniffer Tab

Captures every keypad display update from the panel in real time. Useful for:
- Debugging why a scan or save failed
- Understanding the exact sequence of display changes for a panel operation
- Verifying that the panel returned to normal mode after programming

Click **Start Capture**, then operate your physical keypad (or trigger a scan)
to see the display changes in real time.

---

## Re-scanning

You can re-scan all or part of the panel configuration at any time:

- **Re-scan Panel** (top right) — full scan of everything (~3–6 minutes)
- **Re-scan Zones** — re-reads zone types and names only
- **Re-scan System** — re-reads system delay fields only
- **Re-scan Words** — re-reads custom words only
- **Re-scan Reporting** — re-reads reporting fields only
- **Re-scan Keypads** — re-reads keypad configuration only

Section re-scans are faster than a full scan since they only read the relevant
panel menus.

---

## Important Warnings & Disclaimers

### ⚠️ General

- **This is community software, not affiliated with or endorsed by Honeywell,
  Resideo, EyezOn, or any alarm monitoring company.**
- **Use entirely at your own risk.** Incorrect programming can leave your
  alarm system in a non-functional state, fail to report alarms, or cause
  false alarms.
- **This software is provided "as-is" without warranty of any kind.** The
  authors are not responsible for any damage, loss, or security compromise
  resulting from its use.

### ⚠️ Alarm Monitoring

- **If your system is professionally monitored**, consult your monitoring
  company before making any changes. Modifying reporting settings, account
  numbers, or phone numbers can disrupt alarm signal delivery.
- **Do not change settings you do not fully understand.** Consult your panel’s
  programming guide before modifying any configuration.
- **Changes take effect immediately** on the panel when you click Save.
  There is no undo — to revert a change, you must manually set it back to
  the previous value.

### ⚠️ Programming Mode

- While the addon is scanning or saving, the panel is in **programming mode**.
  During this time:
  - The alarm system **does not monitor zones** (no alarm response)
  - Physical keypads will show programming-mode prompts
  - The system will **not arm or disarm** until programming mode is exited
- **Do not scan or save while the system is armed.** Always disarm first.
- If the addon is interrupted mid-scan (browser closed, network issue, HA
  restart), the panel may remain in programming mode. Use the **Recovery**
  button on the Virtual Keypad tab, or press `*99` on a physical keypad to
  exit programming mode manually.

### ⚠️ Envisalink Limitations

- The Envisalink allows **only one TCP client** at a time. In Direct EVL mode,
  you must disable the HA integration first.
- The Envisalink's command buffer is limited. The addon inserts small delays
  between keypresses to avoid overwhelming it. Do not use other tools (EyezOn
  web portal, other TPI clients) simultaneously.

### ⚠️ Data Accuracy

- The addon reads the panel display to extract configuration values. Display
  parsing is heuristic — unusual panel firmware versions or non-standard
  configurations may cause incorrect readings.
- **Always verify critical settings on a physical keypad** after making changes
  through the addon.
- The cached configuration shown in the UI is a snapshot from the last scan.
  If someone changes settings via a physical keypad, the addon won't know
  until you re-scan.

### ⚠️ Security

- The installer code and user code are stored locally within the addon's
  persistent data directory (`/data/code.json`). They are **not** transmitted
  outside your local network.
- The addon runs within Home Assistant's ingress proxy — it is only accessible
  to authenticated HA users. It does not expose any external ports.
- **Never share your installer code or user code.** If you share screenshots
  or logs, redact any codes.

---

## Troubleshooting

### The addon won't connect / shows "Disconnected"

- Verify the `envisalink_new` integration is installed and running in HA
- Check that your keypad sensor entity exists: **Settings → Devices → Envisalink**
- Click the connection indicator → Connection Settings and verify entity IDs
- Check the addon logs: **Settings → Add-ons → Envisalink Programming Interface → Log**

### Scan fails or gets stuck

- Ensure the panel is **disarmed** before scanning
- Ensure no one is using a physical keypad during the scan
- Check that the installer code is correct (default: `4112`)
- Try the **Recovery** button on the Virtual Keypad tab, wait 30 seconds,
  then retry
- Check the Bus Sniffer → **Scan Log** for details on where it failed

### "Scan interrupted" error

The panel stopped responding mid-scan. This can happen if:
- The panel is busy processing another input
- The Envisalink connection was interrupted
- The installer code is incorrect

Click **Try Again** to restart the scan. The addon will automatically exit
programming mode first.

### Settings don't match what I see on the keypad

Re-scan the relevant section. The addon shows cached data from the last scan.
If changes were made via a physical keypad, the addon's cache is stale.

### Panel stuck in programming mode

On any physical keypad, press `*99` to exit programming mode. Or use the
addon's Virtual Keypad tab → **Recovery** button.

### Direct EVL mode: "Connection refused" or frequent disconnects

- Verify the Envisalink IP address and TPI password
- Ensure the `envisalink_new` integration is **disabled** in HA first
- Only one TCP client can connect to the EVL at a time

---

## Technical Details

- **Backend:** FastAPI (Python 3.12) with Uvicorn ASGI server
- **Frontend:** Vanilla JavaScript + jQuery, custom dark CSS theme
- **Communication:** WebSocket for real-time display updates; REST API for
  configuration operations
- **Container:** Docker-based HA addon (aarch64, amd64, armhf, armv7)
- **Ingress:** Served through HA's ingress proxy (port 8099 internal)
- **Data:** Cached configuration stored in `/data/config_cache.json`

---

## Credits & License

This addon is part of the
[envisalink_new](https://github.com/ufodone/envisalink_new) community project
— a fork of the Home Assistant core Envisalink integration with extended
support for Honeywell and DSC panels.

Licensed under the same terms as the parent project. See
[LICENSE](../../LICENSE) for details.

---

*Last updated: April 2026*
