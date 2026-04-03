"""
Vista 20P panel command builders and data tables.

All sequences follow the safe pattern:
  *99          — harmlessly exit programming mode (no-op if not in prog mode)
  {code}800    — enter programming mode fresh
  ...fields...
  *99          — exit programming mode

This means sequences are idempotent: safe to call whether or not the panel
is already in programming mode.

Time-set uses the user command syntax (code + # + 63) which works from
normal operating mode and does NOT require entering programming mode first.
"""

from __future__ import annotations

import datetime

# ---------------------------------------------------------------------------
# Data tables
# ---------------------------------------------------------------------------

ZONE_TYPES: dict[int, str] = {
    0:  "Not Used",
    1:  "Entry/Exit #1",
    2:  "Entry/Exit #2",
    3:  "Perimeter",
    4:  "Interior Follower",
    5:  "Trouble Day / Alarm Night",
    6:  "24-Hr Silent",
    7:  "24-Hr Audible",
    8:  "24-Hr Aux",
    9:  "Fire",
    10: "Interior w/Delay",
    12: "Monitor Zone",
    14: "Carbon Monoxide",
    16: "Fire w/Verify",
    23: "No Alarm Response",
    24: "Silent Burglary",
    77: "Keyswitch",
    81: "AAV Monitor Zone",
    90: "Configurable Type 90",
    91: "Configurable Type 91",
}

# One-sentence descriptions shown as UI hints next to the zone-type selector.
ZONE_TYPE_DESCS: dict[int, str] = {
    0:  "Zone is disabled and ignored by the system.",
    1:  "Primary entry/exit point; provides entry delay #1 (field *35) when armed.",
    2:  "Secondary entry/exit with longer entry delay #2 (field *36).",
    3:  "Instant alarm on exterior doors/windows when the system is armed.",
    4:  "Delayed alarm if an entry/exit zone trips first; otherwise instant. Active in Away mode only.",
    5:  "Trouble notification while disarmed (day); instant alarm when armed (night).",
    6:  "Always-active silent alarm — reports to central station only, no audible sounding.",
    7:  "Always-active alarm with keypad and external audible sounding.",
    8:  "Always-active alarm with keypad sound only (no bell/siren output).",
    9:  "Supervised fire zone: alarm on short circuit, trouble on open. Always active, cannot be bypassed.",
    10: "Entry delay from Entry Delay 1 when tripped in Away mode; bypassed in Stay/Instant.",
    12: "Trouble-only monitor zone; reports fault/restore to central station without triggering an alarm.",
    14: "Carbon monoxide detector zone. Always active, cannot be bypassed.",
    16: "Fire alarm only after a second trigger within 90 seconds, to reduce false alarms.",
    23: "Relay output action only — no alarm triggered (e.g. lobby door access).",
    24: "Instant alarm with no audible sounding at keypad or siren; reports to central station.",
    77: "Zone is armed or disarmed via a keyswitch input.",
    81: "AAV (Audio Alarm Verification) monitor zone.",
    90: "Configurable zone type 90 — behaviour set by installer.",
    91: "Configurable zone type 91 — behaviour set by installer.",
}

HARDWIRE_TYPES: dict[int, str] = {
    0: "EOL",
    1: "NC",
    2: "NO",
    3: "ZD",
    4: "DB",
}

RESPONSE_TIMES: dict[int, str] = {
    0: "10 ms",
    1: "350 ms",
    2: "700 ms",
    3: "1.2 sec",
}

# Vocabulary list for alpha descriptors (*82)
# Keys are the 3-digit index used with # in programming mode.
VOCAB: dict[int, str] = {
    1:   "AIR",
    2:   "ALARM",
    4:   "ALLEY",
    5:   "AMBUSH",
    6:   "AREA",
    7:   "APARTMENT",
    9:   "ATTIC",
    10:  "AUDIO",
    12:  "BABY",
    13:  "BACK",
    14:  "BAR",
    16:  "BASEMENT",
    17:  "BATHROOM",
    18:  "BED",
    19:  "BEDROOM",
    20:  "BELL",
    21:  "BLOWER",
    22:  "BOILER",
    23:  "BOTTOM",
    25:  "BREAK",
    26:  "BUILDING",
    28:  "CABINET",
    29:  "CALL",
    30:  "CAMERA",
    31:  "CAR",
    33:  "CASH",
    34:  "CCTV",
    35:  "CEILING",
    36:  "CELLAR",
    37:  "CENTRAL",
    38:  "CIRCUIT",
    40:  "CLOSED",
    46:  "COMPUTER",
    47:  "CONTACT",
    48:  "DAUGHTERS",
    49:  "DELAYED",
    50:  "DEN",
    51:  "DESK",
    52:  "DETECTOR",
    53:  "DINING",
    54:  "DISCRIMINATOR",
    55:  "DISPLAY",
    57:  "DOOR",
    59:  "DOWN",
    60:  "DOWNSTAIRS",
    61:  "DRAWER",
    62:  "DRIVEWAY",
    64:  "DUCT",
    65:  "EAST",
    66:  "ELECTRIC",
    67:  "EMERGENCY",
    68:  "ENTRY",
    69:  "EQUIPMENT",
    71:  "EXIT",
    72:  "EXTERIOR",
    73:  "FACTORY",
    75:  "FAMILY",
    76:  "FATHERS",
    77:  "FENCE",
    79:  "FIRE",
    80:  "FLOOR",
    81:  "FLOW",
    82:  "FOIL",
    83:  "FOYER",
    84:  "FREEZER",
    85:  "FRONT",
    89:  "GARAGE",
    90:  "GAS",
    91:  "GATE",
    92:  "GLASS",
    93:  "GUEST",
    94:  "GUN",
    95:  "HALL",
    96:  "HEAT",
    98:  "HOLDUP",
    99:  "HOUSE",
    100: "INFRARED",
    101: "INSIDE",
    102: "INTERIOR",
    103: "INTRUSION",
    104: "JEWELRY",
    105: "KITCHEN",
    106: "LAUNDRY",
    107: "LEFT",
    108: "LEVEL",
    109: "LIBRARY",
    110: "LIGHT",
    111: "LINE",
    113: "LIVING",
    114: "LOADING",
    115: "LOCK",
    116: "LOOP",
    117: "LOW",
    118: "LOWER",
    119: "MACHINE",
    121: "MAIDS",
    122: "MAIN",
    123: "MASTER",
    125: "MEDICAL",
    126: "MEDICINE",
    128: "MONEY",
    129: "MONITOR",
    130: "MOTHERS",
    131: "MOTION",
    132: "MOTOR",
    134: "NORTH",
    135: "NURSERY",
    136: "OFFICE",
    138: "OPEN",
    139: "OPENING",
    140: "OUTSIDE",
    142: "OVERHEAD",
    143: "PAINTING",
    144: "PANIC",
    145: "PASSIVE",
    146: "PATIO",
    147: "PERIMETER",
    148: "PHONE",
    150: "POINT",
    151: "POLICE",
    152: "POOL",
    153: "POWER",
    155: "RADIO",
    156: "REAR",
    157: "RECREATION",
    159: "REFRIGERATION",
    160: "RF",
    161: "RIGHT",
    162: "ROOM",
    163: "ROOF",
    164: "SAFE",
    165: "SCREEN",
    166: "SENSOR",
    167: "SERVICE",
    168: "SHED",
    169: "SHOCK",
    170: "SHOP",
    171: "SHORT",
    173: "SIDE",
    174: "SKYLIGHT",
    175: "SLIDING",
    176: "SMOKE",
    178: "SONS",
    179: "SOUTH",
    180: "SPRINKLER",
    182: "STATION",
    184: "STORE",
    185: "STORAGE",
    186: "STORY",
    190: "SUPERVISED",
    191: "SUPERVISION",
    192: "SWIMMING",
    193: "SWITCH",
    194: "TAMPER",
    196: "TELCO",
    197: "TELEPHONE",
    199: "TEMPERATURE",
    200: "THERMOSTAT",
    201: "TOOL",
    202: "TRANSMITTER",
    205: "UP",
    206: "UPPER",
    207: "UPSTAIRS",
    208: "UTILITY",
    209: "VALVE",
    210: "VAULT",
    212: "VOLTAGE",
    213: "WALL",
    214: "WAREHOUSE",
    216: "WEST",
    217: "WINDOW",
    219: "WING",
    220: "WIRELESS",
    222: "XMITTER",
    223: "YARD",
    224: "ZONE (No.)",
    225: "ZONE",
    226: "0",
    227: "1",
    228: "1ST",
    229: "2",
    230: "2ND",
    231: "3",
    232: "3RD",
    233: "4",
    234: "4TH",
    235: "5",
    236: "5TH",
    237: "6",
    238: "6TH",
    239: "7",
    240: "7TH",
    241: "8",
    242: "8TH",
    243: "9",
    244: "9TH",
}

REVERSE_VOCAB: dict[str, int] = {v: k for k, v in VOCAB.items()}


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def _prog_wrap(code: str, inner: str) -> str:
    """Wrap an inner command sequence in safe prog-mode entry/exit."""
    return f"*99{code}800{inner}*99"


def build_set_time(code: str, dt: datetime.datetime | None = None) -> str:
    """
    Set the panel clock using user command: code + #63 + *HHMM(1|2)YYMMDD*
    1 = AM, 2 = PM.  Uses current local time if dt is None.
    NOTE: Does NOT need programming mode — works from normal operating mode.
    Confirmed format from working shell script: *HHMM{AP}yymmdd*
    """
    if dt is None:
        dt = datetime.datetime.now()
    hh = dt.strftime("%I")   # 12-hour zero-padded
    mm = dt.strftime("%M")
    ampm = "1" if dt.hour >= 12 else "0"  # 0 = AM, 1 = PM
    yy = dt.strftime("%y")
    mo = dt.strftime("%m")
    dd = dt.strftime("%d")
    return f"{code}#63*{hh}{mm}{ampm}{yy}{mo}{dd}*"


def build_review_field(code: str, field: int) -> str:
    """
    Enter programming mode and review (read-only) a data field.
    Sends #<field> which shows the value without allowing changes.
    Returns command string; caller must use send_and_capture().
    """
    return _prog_wrap(code, f"#{field:02d}")


def build_set_field(code: str, field: int, value: str) -> str:
    """
    Enter programming mode, navigate to a data field, set its value, and exit.
    value must be the exact digit string to enter (e.g. "60" for 60 seconds).
    """
    return _prog_wrap(code, f"*{field:02d}{value}")


def build_keypad_config(
    code: str, field_num: int, partition_enable: int, sound: int
) -> str:
    """
    Set a keypad configuration field (*190–*196).

    Each field accepts 2 digits total: partition/enable (1) + sound option (1).
      partition_enable: 0=disabled, 1-3=partition number (3=common on 20P)
      sound:           0=all sounds, 1=suppress arm/E-E, 2=suppress chime,
                       3=suppress all

    Field numbers: *190=keypad 2 (addr 17) through *196=keypad 8 (addr 23).

    After accepting the 2 digits the panel auto-advances to the next field.
    *99 exits programming mode from any data-field prompt.
    """
    value = f"{partition_enable}{sound}"
    return f"*99{code}800*{field_num}{value}*99"


# ---------------------------------------------------------------------------
# User code command builders
# ---------------------------------------------------------------------------

def build_installer_code_change(installer_code: str, new_code: str) -> str:
    """
    Change the installer code (user 01) via programming field *20.

    Sequence: *99{installer_code}800*20{new_code}*99
    The new code is 4 digits. After this, the OLD installer code is invalid.
    """
    return _prog_wrap(installer_code, f"*20{new_code}")


def build_master_code_change(master_code: str, new_code: str) -> str:
    """
    Change the master code (user 02).

    Sequence: {master_code}802{new_code}{new_code}
    The new code must be entered TWICE. This is a user-mode command,
    NOT a programming-mode command.
    """
    return f"{master_code}802{new_code}{new_code}"


def build_user_code_set(master_code: str, user_num: int, new_code: str) -> str:
    """
    Add or change a user code (users 03–49).

    Sequence: {master_code}8{user_num:02d}{new_code}
    User-mode command. The keypad beeps once to confirm.
    """
    return f"{master_code}8{user_num:02d}{new_code}"


def build_user_delete(master_code: str, user_num: int) -> str:
    """
    Delete a user code (users 03–49).

    Sequence: {master_code}8{user_num:02d}#0
    Erases the code and all attributes except assigned partition.
    """
    return f"{master_code}8{user_num:02d}#0"


def build_user_authority(master_code: str, user_num: int, level: int) -> str:
    """
    Set authority level for a user (users 03–49).

    Sequence: {master_code}8{user_num:02d}#1{level}
    Levels: 0=standard, 1=arm only, 2=guest, 3=duress,
            4=partition master (Vista-20P only)
    """
    return f"{master_code}8{user_num:02d}#1{level}"


def build_user_partition(
    master_code: str, user_num: int, partitions: list[int]
) -> str:
    """
    Set partition assignment for a user (users 03–49, Vista-20P only).

    Sequence: {master_code}8{user_num:02d}#30{partitions}#
    Partition entries: 1=partition 1 and common, 2=partition 2 and common,
                       3=common partition only.
    Multiple partitions are entered sequentially, then # to end.
    """
    parts = "".join(str(p) for p in partitions)
    return f"{master_code}8{user_num:02d}#30{parts}#"


def build_zone_type_edit(
    code: str,
    zone: int,
    zone_type: int,
    partition: int,
    report_code: int,
    input_type: int | None = None,
) -> str:
    """
    Edit zone type/attributes using *58 expert mode.

    Hardwired zones (1-8) have an additional Input Type field (HW type + RT).
    Expansion/wireless zones (9-48) do NOT have Input Type — the panel skips
    that field and accepts only ZT + P + RC before saving.

    Panel flow:
      *58              → "Set to Confirm? 0=No,1=Yes"
      0                → No → shows zone 01 summary
      {ZZ}             → navigate to target zone (2 digits)
      *                → enter edit mode
      {ZT:02d}         → Zone Type       (2 digits)
      {P}              → Partition        (1 digit: 0=none, 1-3)
      {RC:02d}         → Report Code     (2 digits, e.g. 01=enabled, 00=none)
      {IN:02d}         → Input Type      (2 digits, hardwired only)
      *                → save (advances to next zone)
      00               → zone 00 (exit sentinel)
      *                → confirm → "Enter * or #"
      *                → "Field?"
      99               → exit programming
    """
    fields = (
        f"{zone_type:02d}"         # Zone Type (2 digits)
        f"{partition}"             # Partition (1 digit)
        f"{report_code:02d}"       # Report Code (2 digits)
    )
    if input_type is not None:
        fields += f"{input_type:02d}"  # Input Type (2 digits, hardwired only)

    inner = (
        f"*580"                     # enter *58, answer 0 (No) to Set to Confirm
        f"{zone:02d}*"             # navigate to zone, * to enter edit mode
        f"{fields}"
        f"*"                       # save
        f"00**"                    # zone 00 + * → "Enter * or #" → * → "Field?"
    )
    return _prog_wrap(code, inner)


def build_zone_name(code: str, zone: int, word_ids: list[int]) -> str:
    """
    Set the alpha descriptor for a zone using *82 menu mode.
    word_ids: list of 1–3 vocabulary index numbers (from VOCAB dict).
              Empty slots are skipped. Pass [] to clear (leaves existing words).
    Sequence: enter prog → *82 → 1 (yes) → 0 (no custom) → *ZZ → *ZZ (enter edit) →
              #WWW6 per word → 8 (save) → #00 (quit) → 0 (exit alpha) → *99
    """
    word_seq = ""
    for wid in word_ids[:3]:
        if wid:
            word_seq += f"#{wid:03d}6"
    inner = (
        f"*821"                  # enter *82, answer yes to PROGRAM ALPHA
        f"0"                     # no custom words
        f"*{zone:02d}"           # navigate to zone
        f"*{zone:02d}"           # enter edit mode (cursor appears)
        f"{word_seq}"            # enter words
        f"8"                     # save
        f"#00"                   # quit zone loop
        f"0"                     # exit alpha mode
    )
    return _prog_wrap(code, inner)


def build_custom_word(code: str, word_num: int, text: str) -> str:
    """
    Set a custom word (1-12) in *82 custom word mode.

    Words 1-10 are general purpose for zone naming.
    Word 11 = Partition 1 name, Word 12 = Partition 2 name.

    Entry: *82 → 1 (yes PROGRAM ALPHA) → 1 (yes CUSTOM WORDS) → CUSTOM? 00
    Navigate to word NN — cursor appears at position 1.
    Type each character with #XX (2-digit ASCII code); cursor auto-advances.
    Pad remaining positions (up to 10) with #32 (space) to overwrite old content.
    Save with [8], exit with 00 → 0 → *99.

    Per the programming guide: "To change a custom word, just overwrite it."
    Character codes: #65-#90 = A-Z, #48-#57 = 0-9, #32 = space.

    text: up to 10 characters (A-Z, 0-9, space). Truncated at 10.
    """
    if not 1 <= word_num <= 12:
        raise ValueError(f"Custom word number must be 1-12, got {word_num}")
    text = text.upper().strip()[:10]

    # Build character entry sequence:
    # Cursor starts at position 1 after navigating to the word.
    # Type each character with #XX (cursor auto-advances after each),
    # then pad remaining positions with #32 (space) to clear old content.
    char_seq = ""
    for ch in text:
        char_seq += f"#{ord(ch):02d}"
    # Pad remaining positions with spaces to overwrite any old characters
    char_seq += "#32" * (10 - len(text))

    inner = (
        f"*8211"                   # enter *82, yes to PROGRAM ALPHA, yes to CUSTOM WORDS
        f"{word_num:02d}"          # navigate to word NN (cursor at position 1)
        f"{char_seq}"              # type text + pad with spaces
        f"8"                       # save word → back to CUSTOM? 00
        f"00"                      # word 00 (exit sentinel) → PROGRAM ALPHA?
        f"0"                       # no → data-field mode
    )
    return _prog_wrap(code, inner)


def build_enter_prog_mode(code: str) -> str:
    """Enter programming mode (safe: exits first)."""
    return f"*99{code}800"


def build_exit_prog_mode() -> str:
    """Exit programming mode cleanly."""
    return "*99"


# ---------------------------------------------------------------------------
# Scan helpers — sequences for the scanner to send one step at a time
# ---------------------------------------------------------------------------

def scan_review_field_seq(code: str, field: int) -> list[str]:
    """
    Returns a list of keypress strings to sequentially review a data field.
    Scanner sends each element and captures the display after each.
    Steps: [enter_prog_mode, #field]
    """
    return [
        f"*99{code}800",
        f"#{field:02d}",
    ]


def scan_zone_type_seq(code: str, zone: int) -> list[str]:
    """
    Returns keypress sequence to read zone type summary for one zone via *58.
    Steps: [enter_prog+*58, zone_num+*]  — display shows "Zn ZT P RC HW RT"
    """
    return [
        f"*99{code}800*58",
        f"{zone:02d}*",
    ]


def scan_zone_name_seq(code: str, zone: int) -> list[str]:
    """
    Returns keypress sequence to enter *82 and navigate to a zone's descriptor.
    Steps: [enter_prog+*82+1+0, *ZZ] — display shows the zone's current words
    """
    return [
        f"*99{code}800*821" + "0",  # enter *82, yes to program, no custom words
        f"*{zone:02d}",             # navigate to zone — display shows descriptor
    ]


# ---------------------------------------------------------------------------
# Reporting field definitions
# ---------------------------------------------------------------------------

# Metadata for reporting / dialer / pager fields (*40–*49, *160–*172).
# Each tuple: (field_num, key, label, scrolling, max_digits)
#   scrolling: True = panel scrolls through digits one at a time (phone numbers)
#   max_digits: maximum number of encoded digit-pairs for scrolling fields
REPORTING_FIELDS: list[tuple[int, str, str, bool, int]] = [
    # Dialer (*40–*49)
    (40,  "pabx",              "PABX / Call Wait Disable",      True,  6),
    (41,  "primary_phone",     "Primary Phone No.",             True, 20),
    (42,  "secondary_phone",   "Second Phone No.",              True, 20),
    (43,  "part1_acct_pri",    "Part. 1 Primary Acct. No.",     True, 10),
    (44,  "part1_acct_sec",    "Part. 1 Secondary Acct. No.",   True, 10),
    (45,  "part2_acct_pri",    "Part. 2 Primary Acct. No.",     True, 10),
    (46,  "part2_acct_sec",    "Part. 2 Secondary Acct. No.",   True, 10),
    (47,  "phone_system",      "Phone System Select",           False, 2),
    (48,  "report_format",     "Report Format (Pri/Sec)",       False, 2),
    (49,  "split_dual",        "Split/Dual Reporting",          False, 2),
    # Pager 1 (*160–*162)
    (160, "pager1_phone",      "Pager 1 Phone No.",             True, 20),
    (161, "pager1_chars",      "Pager 1 Characters",            True, 16),
    (162, "pager1_report",     "Pager 1 Report Options",        False, 3),
    # Pager 2 (*163–*165)
    (163, "pager2_phone",      "Pager 2 Phone No.",             True, 20),
    (164, "pager2_chars",      "Pager 2 Characters",            True, 16),
    (165, "pager2_report",     "Pager 2 Report Options",        False, 3),
    # Pager 3 (*166–*168)
    (166, "pager3_phone",      "Pager 3 Phone No.",             True, 20),
    (167, "pager3_chars",      "Pager 3 Characters",            True, 16),
    (168, "pager3_report",     "Pager 3 Report Options",        False, 3),
    # Pager 4 (*169–*171)
    (169, "pager4_phone",      "Pager 4 Phone No.",             True, 20),
    (170, "pager4_chars",      "Pager 4 Characters",            True, 16),
    (171, "pager4_report",     "Pager 4 Report Options",        False, 3),
    # Pager delay (*172)
    (172, "pager_delay",       "Pager Delay For Alarms",        False, 1),
]

# Lookup by key and by field number for convenience
REPORTING_BY_KEY: dict[str, tuple] = {t[1]: t for t in REPORTING_FIELDS}
REPORTING_BY_FIELD: dict[int, tuple] = {t[0]: t for t in REPORTING_FIELDS}

# Fields that support deletion via *NN* (per programming guide page 2)
REPORTING_DELETABLE_FIELDS: set[int] = {40, 41, 42, 43, 44, 45, 46, 94,
                                         160, 161, 163, 164, 166, 167, 169, 170}


def build_reporting_field_set(code: str, field: int, value: str) -> str:
    """
    Set a reporting/dialer/pager field.

    For phone number fields (up to 20 digits): value is the digit string.
    If shorter than the maximum, a trailing '*' is needed to end entry.
    The panel auto-advances once max digits are reached.

    For fixed-length fields (*47=1 digit, *48=2 digits, *49=1 digit,
    *162/*165/*168/*171=3 digits, *172=1 digit): value is the digit string
    and the panel auto-advances on the last digit.

    Encoding: digits 0-9 entered directly. Special characters:
      #+11 for '✱', #+12 for '#', #+13 for 2-sec pause,
      #+10 for 0 (in some contexts), #+14 for E, #+15 for F.
    The caller must pre-encode special characters if needed.
    """
    meta = REPORTING_BY_FIELD.get(field)
    if not meta:
        raise ValueError(f"Unknown reporting field: *{field}")

    scrolling = meta[3]
    max_digits = meta[4]

    # For variable-length fields, if we have fewer digits than max,
    # append '*' to end the entry and advance to next field.
    suffix = ""
    if scrolling and len(value) < max_digits:
        suffix = "*"

    return _prog_wrap(code, f"*{field:02d}{value}{suffix}")


def build_reporting_field_delete(code: str, field: int) -> str:
    """
    Delete (clear) a reporting/dialer/pager field using *NN* syntax.
    Only valid for fields that support deletion (phone numbers, account numbers,
    pager phone/chars).
    """
    if field not in REPORTING_DELETABLE_FIELDS:
        raise ValueError(f"Field *{field} does not support deletion")
    return _prog_wrap(code, f"*{field:02d}*")



