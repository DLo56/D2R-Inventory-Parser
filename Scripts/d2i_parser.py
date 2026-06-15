"""
D2R Shared Stash (.d2i) and Character Save (.d2s) parser.

Reads vanilla D2R save files (SharedStash*.d2i, *.d2s).
Item bit-format is based on dschu012/D2SLib (src/Model/Save/Items.cs) for
save version >= 0x61 (this stash is version 105 == 0x69), with one
empirical adjustment that D2SLib does NOT document:
  * 1 extra bit consumed between the realm-data flag and the
    armor/durability/stat-list block (every extended item).

Compact (simple=True) items:
  * NO explicit padding field; after reading code+socketed the caller's
    align() advances to the next byte boundary.  The Huffman code length
    varies per item code, so a fixed-width padding would desync the stream.

Other D2R format points (these were wrong in the prior implementation):
  * Item code = exactly 4 Huffman-decoded chars, NOT space-terminated.
  * NumberOfSocketedItems is read in the compact section right after the
    code: 1 bit if the item is compact, 3 bits otherwise.
  * Every item (and every socketed child) is byte-aligned after it ends.
  * Realm data: 1 flag bit; if set, 96 bits follow.
  * Durability field widths come from itemstatcost (maxdurability).

D2I SECTION LAYOUT:
  0x00  magic        (uint32 LE) = 0xAA55AA55
  0x04  header_flag  (uint32 LE)
  0x08  version      (uint32 LE) = 105
  0x0C  gold         (uint32 LE)
  0x10  section_len  (uint32 LE) = total bytes of THIS section
  0x14-0x3F  padding
  0x40  'JM'         (2-byte list marker)
  0x42  item_count   (uint16 LE) = root items only
  0x44+ item data    (byte-aligned items, LSB-first bit fields)
"""

import argparse
import csv
import struct
from collections import defaultdict
from pathlib import Path
from typing import List, Optional


class UnknownStatError(Exception):
    """Raised when the stat list contains an ID with no known bit-width."""
    def __init__(self, msg: str, bit_pos: int, stat_id: int = -1) -> None:
        super().__init__(msg)
        self.bit_pos = bit_pos
        self.stat_id = stat_id

# ---------------------------------------------------------------------------
# Huffman codec (standard D2 item-code tree; codes stored LSB-first,
# decoded MSB-walk per bit). Verified against d07riv / Phrozen Keep.
# ---------------------------------------------------------------------------

HUFFMAN_TABLE = {
    " ": "10", "0": "11111011", "1": "1111100", "2": "001100",
    "3": "1101101", "4": "11111010", "5": "00010110", "6": "1101111",
    "7": "01111", "8": "000100", "9": "01110", "a": "11110", "b": "0101",
    "c": "01000", "d": "110001", "e": "110000", "f": "010011", "g": "11010",
    "h": "00011", "i": "1111110", "j": "000101110", "k": "010010", "l": "11101",
    "m": "01101", "n": "001101", "o": "1111111", "p": "11001", "q": "11011001",
    "r": "11100", "s": "0010", "t": "01100", "u": "00001", "v": "1101110",
    "w": "00000", "x": "00111", "y": "0001010", "z": "11011000",
}


def _build_huffman_tree() -> dict:
    root: dict = {}
    for char, pattern in HUFFMAN_TABLE.items():
        node = root
        for bit_char in pattern:
            node = node.setdefault(bit_char, {})
        node["_char"] = char
    return root


HUFFMAN_TREE = _build_huffman_tree()

# ---------------------------------------------------------------------------
# Game-data tables (real D2R data, JSON extracted from the game CASC).
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent
# Current D2R game-data tables, extracted from the local install's CASC by
# casc_extract.py.  ALL parsing data must come from here (i.e. from the install)
# so it tracks the live game; never from stale online/bundled copies.
_GAMEDATA = _SCRIPT_DIR / "gamedata_d2r" / "excel"

# Localization: classic D2 .tbl string tables map internal string KEYS (the
# uniqueitems/setitems "index" and rareprefix/raresuffix "name" columns) to the
# English display text.  Real .tbl bytes live in Scripts/_strings/ (the
# _pyd2s_runtime copies are Git-LFS pointer stubs).  See memory tbl-string-format.
_STRINGS_DIR = _SCRIPT_DIR / "_strings"
_STRING_TABLE: dict = {}   # localization key -> English display string

# Indexed by integer stat id; sized to whatever the loaded itemstatcost table
# contains. The 9-bit stat id field can address 0..510, so a value
# > _MAX_STAT_ID at parse time genuinely means the bit stream desynced.
_STAT_NAMES:      List[str] = []
_STAT_PARAM_BITS: List[int] = []
_STAT_VALUE_BITS: List[int] = []
_STAT_SAVE_ADD:   List[int] = []
_MAX_STAT_ID:     int       = 0
# Stats with no descfunc in itemstatcost are not displayed by the game itself
# (e.g. item_extrablood on Gorefoot) — suppress them from the attribute string.
_HIDDEN_STATS:    set       = set()

# Consecutive-stat blocks: a stat id whose following id(s) are written with
# NO 9-bit id prefix (D2SLib ItemStatList.Read).
#   one extra:  magicmindam(52), item_maxdamage_percent(17),
#               firemindam(48), lightmindam(50)
#   two extra:  coldmindam(54), poisonmindam(57)
_CONSECUTIVE_ONE = {52, 17, 48, 50}
_CONSECUTIVE_TWO = {54, 57}

# Item-code classification.
_ARMOR_CODES:     set = set()
_WEAPON_CODES:    set = set()
_STACKABLE_CODES: set = set()
_ITEM_NAMES:      dict = {}   # code -> base type name
_UNIQUE_NAMES:    dict = {}   # unique item *ID -> display name
_SET_ITEM_NAMES:  dict = {}   # set item *ID   -> display name
_RARE_PREFIX:     dict = {}   # 1-based row -> first word of rare name
_RARE_SUFFIX:     dict = {}   # 1-based row -> second word of rare name
_RARE_NAME:       dict = {}   # combined 8-bit name index -> word (suffixes 1..N, prefixes N+1..)
_ITEM_LEVELREQ:   dict = {}   # code -> base level requirement from game data
_UNIQUE_LEVELREQ: dict = {}   # unique *ID -> level requirement
_SET_LEVELREQ:    dict = {}   # set item *ID -> level requirement
_PREFIX_LEVELREQ: dict = {}   # magic affix id -> level requirement
_SUFFIX_LEVELREQ: dict = {}   # magic affix id -> level requirement
_PREFIX_NAMES:    dict = {}   # magic affix id -> prefix word (e.g. 'Bronze')
_SUFFIX_NAMES:    dict = {}   # magic affix id -> suffix word (e.g. 'of Flame')
_ITEM_MAXSTACK:   dict = {}   # code -> max stack size (stackable items)
_RUNEWORD_NAMES:  dict = {}   # tuple(rune codes in order) -> runeword display name
_SKILL_NAMES:     dict = {}   # skill *Id -> (display name, charclass code)


def _as_int(v) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(v)
    except ValueError:
        return 0


def _parse_tbl(path: Path) -> dict:
    """Parse one classic D2 .tbl string table into {key: value}.

    Format (little-endian): 21-byte header, then numElements u16 indices, then
    hashTableSize 17-byte hash nodes (used u8, index u16, hashValue u32,
    keyOffset u32, valueOffset u32, valueLen u16).  keyOffset/valueOffset are
    absolute file offsets to null-terminated strings.  See memory
    tbl-string-format for the full layout."""
    data = path.read_bytes()
    if len(data) < 21:
        return {}
    num_elements = struct.unpack_from("<H", data, 2)[0]
    hash_size    = struct.unpack_from("<I", data, 4)[0]
    node_off     = 21 + num_elements * 2
    out: dict = {}

    def cstr(off: int) -> str:
        end = data.index(b"\x00", off)
        return data[off:end].decode("latin-1")

    for i in range(hash_size):
        o = node_off + i * 17
        if o + 17 > len(data) or data[o] == 0:   # unused node
            continue
        _idx, _hash, key_off, val_off, _vlen = struct.unpack_from("<HIIIH", data, o + 1)
        try:
            key = cstr(key_off)
            val = cstr(val_off)
        except ValueError:
            continue
        if key:
            out[key] = val
    return out


def _load_string_tables() -> None:
    """Load + merge the three .tbl files.  Lookup precedence matches the game:
    string < expansion < patch (later files override earlier), so e.g. the
    patch's 'Wraithra' -> 'Wraith' wins."""
    for fname in ("string.tbl", "expansionstring.tbl", "patchstring.tbl"):
        try:
            _STRING_TABLE.update(_parse_tbl(_STRINGS_DIR / fname))
        except FileNotFoundError:
            pass


def _loc(key: str) -> str:
    """Resolve a localization key to its display string, falling back to the
    key itself when no string is present."""
    return _STRING_TABLE.get(key, key)


def _load_stat_table() -> None:
    """Load d2r_itemstatcost.txt (TSV). Sized to whatever the file contains;
    *ID can be sparse so we resize dynamically."""
    global _MAX_STAT_ID
    path = _GAMEDATA / "itemstatcost.txt"
    with open(path, encoding="cp1252") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            sid_raw = row.get("*ID") or row.get("ID")
            if sid_raw is None or sid_raw == "":
                continue
            try:
                sid = int(sid_raw)
            except ValueError:
                continue
            if sid < 0:
                continue
            while len(_STAT_NAMES) <= sid:
                _STAT_NAMES.append("")
                _STAT_PARAM_BITS.append(0)
                _STAT_VALUE_BITS.append(0)
                _STAT_SAVE_ADD.append(0)
            _STAT_NAMES[sid]      = (row.get("Stat") or "").strip()
            _STAT_PARAM_BITS[sid] = _as_int(row.get("Save Param Bits"))
            _STAT_VALUE_BITS[sid] = _as_int(row.get("Save Bits"))
            _STAT_SAVE_ADD[sid]   = _as_int(row.get("Save Add"))
            # No descfunc => the game cannot render this stat, so neither do we.
            if not (row.get("descfunc") or "").strip():
                _HIDDEN_STATS.add(sid)
            if sid > _MAX_STAT_ID:
                _MAX_STAT_ID = sid


def _load_item_kinds() -> None:
    def codes_from(filename: str):
        with open(_GAMEDATA / filename, encoding="cp1252") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                code = (row.get("code") or "").strip()
                if code:
                    yield code, row

    def set_item_name(code: str, name: str) -> None:
        code = code.strip()
        name = name.strip()
        if code and name:
            _ITEM_NAMES[code] = name

    # Item base names come straight from the current excel tables ("name" column,
    # e.g. armor.txt qui -> "Quilted Armor").  Each code's row is authoritative.
    for code, row in codes_from("armor.txt"):
        _ARMOR_CODES.add(code)
        set_item_name(code, (row.get("name") or "").strip())
        req = _as_int(row.get("levelreq") or "")
        if req:
            _ITEM_LEVELREQ[code] = req

    for code, row in codes_from("weapons.txt"):
        _WEAPON_CODES.add(code)
        set_item_name(code, (row.get("name") or "").strip())
        # Throwing weapons (javelins, throwing axes/spears) are stackable: they
        # carry a 9-bit quantity in the extended item header just like ammo.
        # Without this, the quantity field is skipped and the stat list desyncs.
        if (row.get("stackable") or "0").strip() == "1":
            _STACKABLE_CODES.add(code)
            ms = _as_int(row.get("maxstack") or "")
            if ms:
                _ITEM_MAXSTACK[code] = ms
        req = _as_int(row.get("levelreq") or "")
        if req:
            _ITEM_LEVELREQ[code] = req

    for code, row in codes_from("misc.txt"):
        set_item_name(code, (row.get("name") or "").strip())
        if (row.get("stackable") or "0").strip() == "1" \
                or (row.get("AdvancedStashStackable") or "0").strip() == "1":
            _STACKABLE_CODES.add(code)
            ms = _as_int(row.get("maxstack") or "")
            if ms:
                _ITEM_MAXSTACK[code] = ms
        req = _as_int(row.get("levelreq") or "")
        if req:
            _ITEM_LEVELREQ[code] = req

    # unique and set item name + level-requirement tables (keyed by *ID).
    for fname, dest, lvldest in (
        ("uniqueitems.txt", _UNIQUE_NAMES, _UNIQUE_LEVELREQ),
        ("setitems.txt", _SET_ITEM_NAMES, _SET_LEVELREQ),
    ):
        try:
            with open(_GAMEDATA / fname, encoding="cp1252") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    uid_raw = (row.get("*ID") or "").strip()
                    name    = (row.get("index") or "").strip()
                    if uid_raw.isdigit() and name:
                        # "index" is a localization KEY (e.g. 'Doomspittle');
                        # resolve to display text, falling back to the key.
                        dest[int(uid_raw)] = _loc(name)
                        lr = _as_int(row.get("lvl req") or "")
                        if lr:
                            lvldest[int(uid_raw)] = lr
        except FileNotFoundError:
            pass

    # Rare name tables (current D2R excel).
    # The save stores TWO 8-bit name indices (name1 = first word, name2 = second
    # word) into a SINGLE combined namespace: indices 1..(#suffix rows) map to
    # raresuffix.txt, and indices above that map to rareprefix.txt offset by the
    # suffix row count.  (Verified: "Shadow Shell" = name1 168 -> prefix row 13,
    # name2 151 -> suffix row 151; "Beast Sunder" = name1 156 -> prefix row 1.)
    _pyd2s = _GAMEDATA
    suffix_rows = 0
    for fname, dest in (("rareprefix.txt", _RARE_PREFIX), ("raresuffix.txt", _RARE_SUFFIX)):
        try:
            with open(_pyd2s / fname, encoding="cp1252") as fh:
                rows = 0
                for idx, row in enumerate(csv.DictReader(fh, delimiter="\t"), start=1):
                    rows = idx
                    name = (row.get("name") or "").strip()
                    if name:
                        # "name" is a localization KEY (e.g. 'Wraithra'->'Wraith',
                        # 'noose'->'Noose'); resolve, falling back to a tidied key.
                        dest[idx] = _STRING_TABLE.get(name, name.capitalize())
                if fname == "raresuffix.txt":
                    suffix_rows = rows
        except FileNotFoundError:
            pass

    # Build the combined index used by the save's name1/name2 fields.
    for idx, word in _RARE_SUFFIX.items():
        _RARE_NAME[idx] = word
    for idx, word in _RARE_PREFIX.items():
        _RARE_NAME[suffix_rows + idx] = word

    # Magic affix level-requirement tables, keyed by the 11-bit affix id the save
    # stores on magic/rare items.  D2R's affix-id space is offset by one row from
    # these files: the stored id V maps to the (V+1)-th data row (the first data
    # row is a blank spacer).  Hence enumerate start=0 so dest[V] == data-row V+1.
    # Verified against actual item stats: id 258->'Fine', 560->"Maiden's",
    # 309->"Dragon's", 373(suffix)->'of Might'.  The "Name" column is a display
    # word (e.g. 'Bronze', 'of Flame'); _loc resolves any that are string keys.
    for fname, lvl, names in (("magicprefix.txt", _PREFIX_LEVELREQ, _PREFIX_NAMES),
                              ("magicsuffix.txt", _SUFFIX_LEVELREQ, _SUFFIX_NAMES)):
        try:
            with open(_pyd2s / fname, encoding="cp1252") as fh:
                for idx, row in enumerate(csv.DictReader(fh, delimiter="\t"), start=0):
                    lvl[idx] = _as_int(row.get("levelreq") or "")
                    nm = (row.get("Name") or "").strip()
                    if nm:
                        names[idx] = _loc(nm)
        except FileNotFoundError:
            pass

    # Runeword definitions: map the ordered rune-code sequence (e.g. r07,r05)
    # to the runeword's display name (e.g. 'Stealth'), so a runeword item can be
    # labelled by name as well as by its constituent runes.
    try:
        with open(_pyd2s / "runes.txt", encoding="cp1252") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                name  = (row.get("*Rune Name") or row.get("Rune Name") or "").strip()
                runes = tuple(
                    (row.get(f"Rune{i}") or "").strip()
                    for i in range(1, 7)
                    if (row.get(f"Rune{i}") or "").strip()
                )
                if name and runes:
                    _RUNEWORD_NAMES[runes] = _STRING_TABLE.get(name, name)
    except FileNotFoundError:
        pass


def _load_skills() -> None:
    """Load skills.txt (TSV): skill *Id -> (display name, charclass code).
    Used to resolve skill-id params (item_singleskill, auras, CTC procs, etc.)."""
    path = _GAMEDATA / "skills.txt"
    try:
        with open(path, encoding="cp1252") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                sid_raw = (row.get("*Id") or row.get("Id") or "").strip()
                if not sid_raw.isdigit():
                    continue
                name = (row.get("skill") or "").strip()
                cls  = (row.get("charclass") or "").strip()
                if name:
                    _SKILL_NAMES[int(sid_raw)] = (name, cls)
    except FileNotFoundError:
        pass


_load_string_tables()
_load_stat_table()
_load_item_kinds()
_load_skills()

# maxdurability is stat id 73; both the max and current durability fields in
# the extended-item header are empirically 8 bits in these D2R saves (the
# itemstatcost "Save Bits" of 9 for current durability does NOT apply here).
_MAXDUR_BITS = _STAT_VALUE_BITS[73] if len(_STAT_VALUE_BITS) > 73 else 8
if _MAXDUR_BITS == 0:
    _MAXDUR_BITS = 8

# ---------------------------------------------------------------------------
# Bit stream (LSB-first within each byte, matching D2SLib BitReader)
# ---------------------------------------------------------------------------


class BitStream:
    def __init__(self, data: bytes, start_bit: int = 0) -> None:
        self.data = data
        self.bit_pos = start_bit

    def read_bits(self, num_bits: int) -> int:
        if num_bits == 0:
            return 0
        result = 0
        for i in range(num_bits):
            byte_idx = self.bit_pos >> 3
            bit_idx = self.bit_pos & 7
            if byte_idx >= len(self.data):
                raise ValueError(
                    f"read past end of data (bit_pos={self.bit_pos}, "
                    f"data_bits={len(self.data) * 8})"
                )
            result |= ((self.data[byte_idx] >> bit_idx) & 1) << i
            self.bit_pos += 1
        return result

    def align(self) -> None:
        """Advance to the next byte boundary (no-op if already aligned)."""
        self.bit_pos = (self.bit_pos + 7) & ~7

    def read_huffman_char(self) -> str:
        node = HUFFMAN_TREE
        for _ in range(80):
            node = node.get(str(self.read_bits(1)))
            if node is None:
                raise ValueError(f"invalid Huffman path at bit {self.bit_pos - 1}")
            if "_char" in node:
                return node["_char"]
        raise ValueError(f"Huffman code exceeded 80 bits at bit {self.bit_pos}")

# ---------------------------------------------------------------------------
# Item parser  (mirrors D2SLib Item.ReadCompact / ReadComplete, version >=0x61)
# ---------------------------------------------------------------------------

_QUALITY_STR = {
    1: "Inferior", 2: "Normal", 3: "Superior", 4: "Magic",
    5: "Set", 6: "Rare", 7: "Unique", 8: "Crafted", 9: "Tempered",
}
_LOCATION_STR = {
    0: "None", 1: "Head", 2: "Amulet", 3: "Body", 4: "Right Hand", 5: "Left Hand",
    6: "Ring Left", 7: "Ring Right", 8: "Belt", 9: "Feet", 10: "Gloves",
    11: "Right Hand (alt)", 12: "Left Hand (alt)",
}
_MODE_STR = {0: "Stored", 1: "Equipped", 2: "Belt", 4: "Cursor", 6: "Socketed"}

# Rune short names keyed by item code (r01-r33)
_RUNE_NAME = {
    "r01": "El",  "r02": "Eld", "r03": "Tir",  "r04": "Nef", "r05": "Eth",
    "r06": "Ith", "r07": "Tal", "r08": "Ral",  "r09": "Ort", "r10": "Thul",
    "r11": "Amn", "r12": "Sol", "r13": "Shael","r14": "Dol", "r15": "Hel",
    "r16": "Io",  "r17": "Lum", "r18": "Ko",   "r19": "Fal", "r20": "Lem",
    "r21": "Pul", "r22": "Um",  "r23": "Mal",  "r24": "Ist", "r25": "Gul",
    "r26": "Vex", "r27": "Ohm", "r28": "Lo",   "r29": "Sur", "r30": "Ber",
    "r31": "Jah", "r32": "Cham","r33": "Zod",
}


# ---------------------------------------------------------------------------
# Stat display labels  (key = Stat column from d2r_itemstatcost.txt)
#
# HOW TO ADD AN ENTRY
#   Find the internal name with: python -c "from d2i_parser import _STAT_NAMES; print(_STAT_NAMES[<id>])"
#   Or look it up in the 'Stat' column of d2r_itemstatcost.txt.
#   Each value is a Python %-format string:
#     %d          → the stat's numeric value
#     %%          → a literal "%" sign
#   If %d appears, the value is substituted in place.
#   If there is NO %d, the label is a boolean flag (value omitted entirely).
#   Labels starting with "_" are special handlers in _fmt_stat() below.
#
# HOW TO EDIT AN ENTRY
#   Just change the string on the right-hand side.
# ---------------------------------------------------------------------------

# Character class index used by param-bearing stats (item_addclassskills,
# item_addskill_tab, etc.).  Matches charstats.txt row order.
_CLASS_NAMES: dict = {
    0: "Amazon", 1: "Sorceress", 2: "Necromancer",
    3: "Paladin", 4: "Barbarian", 5: "Druid", 6: "Assassin",
    7: "Warlock",   # newer D2R class
}

# 3-letter charclass code (skills.txt) -> class display name.  Empty code
# means a non-class skill (e.g. Attack, Kick) shared by all classes.
_CLASS_CODE_NAMES: dict = {
    "ama": "Amazon", "sor": "Sorceress", "nec": "Necromancer",
    "pal": "Paladin", "bar": "Barbarian", "dru": "Druid", "ass": "Assassin",
    "war": "Warlock",
}


def _skill_label(skill_id: int) -> str:
    """Resolve a skill id to its display name, e.g. 132 -> 'Leap'.
    Unknown ids fall back to 'Skill(id=N)' so desync/garbage stays visible."""
    entry = _SKILL_NAMES.get(skill_id)
    if entry is None:
        return f"Skill(id={skill_id})"
    return entry[0]


def _skill_label_classed(skill_id: int) -> str:
    """Skill name plus class suffix, e.g. 132 -> 'Leap (Barbarian Only)'.
    Used by class-restricted bonuses (item_singleskill)."""
    entry = _SKILL_NAMES.get(skill_id)
    if entry is None:
        return f"Skill(id={skill_id})"
    name, cls = entry
    cls_name = _CLASS_CODE_NAMES.get(cls)
    return f"{name} ({cls_name} Only)" if cls_name else name

_STAT_LABEL: dict = {
    # ── Attributes ─────────────────────────────────────────────────────────
    "strength":                  "+%d to Strength",
    "dexterity":                 "+%d to Dexterity",
    "vitality":                  "+%d to Vitality",
    "energy":                    "+%d to Energy",
    "maxhp":                     "+%d to Life",
    "maxmana":                   "+%d to Mana",
    "maxstamina":                "+%d to Maximum Stamina",
    "hpregen":                   "Replenish Life +%d",
    # ── Offense ────────────────────────────────────────────────────────────
    "mindamage":                 "+%d to Minimum Damage",
    "maxdamage":                 "+%d to Maximum Damage",
    "secondary_mindamage":       "+%d to Minimum Damage",
    "secondary_maxdamage":       "+%d to Maximum Damage",
    "item_mindamage_percent":    "%d%% Enhanced Minimum Damage",
    # 17 is always stored with 18 (the consecutive pair) and both equal the ED%
    # roll, which the game shows as a single "+N% Enhanced Damage" line.
    "item_maxdamage_percent":    "+%d%% Enhanced Damage",
    "tohit":                     "+%d to Attack Rating",
    "item_tohit_percent":        "+%d%% Bonus to Attack Rating",
    "item_demondamage_percent":  "%d%% Damage to Demons",
    "item_undeaddamage_percent": "%d%% Damage to Undead",
    "item_ignoretargetac":       "Ignore Target's Defense",   # boolean
    "item_knockback":            "Knockback",                 # boolean
    "item_throw_mindamage":      "+%d to Minimum Throw Damage",
    "item_throw_maxdamage":      "+%d to Maximum Throw Damage",
    # ── Defense ────────────────────────────────────────────────────────────
    "armorclass":                "+%d Defense",
    "armorclass_vs_missile":     "+%d Defense vs. Missiles",
    "item_armor_percent":        "%d%% Enhanced Defense",
    "toblock":                   "+%d%% Increased Chance of Blocking",
    "normal_damage_reduction":   "Damage Reduced by %d",
    "magic_damage_reduction":    "Magic Damage Reduced by %d",
    # ── Resistances ────────────────────────────────────────────────────────
    "fireresist":                "Fire Resist +%d%%",
    "lightresist":               "Lightning Resist +%d%%",
    "coldresist":                "Cold Resist +%d%%",
    "poisonresist":              "Poison Resist +%d%%",
    "magicresist":               "Magic Resist +%d%%",
    "maxfireresist":             "+%d%% to Maximum Fire Resistance",
    "maxpoisonresist":           "+%d%% to Maximum Poison Resistance",
    "item_poisonlengthresist":   "Poison Length Reduced by %d%%",
    # ── Speed ──────────────────────────────────────────────────────────────
    "item_fasterattackrate":     "%d%% Increased Attack Speed",
    "item_fastergethitrate":     "%d%% Faster Hit Recovery",
    "item_fastercastrate":       "%d%% Faster Cast Rate",
    "item_fastermovevelocity":   "%d%% Faster Run/Walk",
    # ── Elemental damage (consecutive — shown as range by _fmt_stat) ───────
    "firemindam":                "_fire",
    "lightmindam":               "_light",
    "coldmindam":                "_cold",
    "poisonmindam":              "_poison",
    # ── Life/mana interaction ───────────────────────────────────────────────
    "lifedrainmindam":           "%d%% Life Stolen per Hit",
    "manadrainmindam":           "%d%% Mana Stolen per Hit",
    "item_attackertakesdamage":  "Attacker Takes Damage of %d",
    "item_manaafterkill":        "+%d to Mana after each Kill",
    "item_damagetomana":         "%d%% Damage Taken Goes to Mana",
    "manarecoverybonus":         "Regenerate Mana %d%%",
    "staminarecoverybonus":      "Regenerate Stamina %d%%",
    # ── Find / gold ────────────────────────────────────────────────────────
    "item_magicbonus":           "%d%% Better Chance of Magic Items",
    "item_goldbonus":            "%d%% Extra Gold from Monsters",
    # ── Misc modifiers ─────────────────────────────────────────────────────
    "item_allskills":            "+%d to All Skills",
    "item_addclassskills":       "_classskills",              # special handler (param = class)
    "item_absorbmagic":          "Absorbs %d Magic Damage",
    "item_deadlystrike":         "%d%% Deadly Strike",
    "item_nonclassskill":        "_oskill",                   # special handler (param = skill id)
    "item_addskill_tab":         "_skilltab",                 # special handler
    "item_lightradius":          "%+d to Light Radius",       # %+d gives +2 or -2
    "item_maxdurability_percent": "%d%% Increased Maximum Durability",
    "item_levelreq":             "Required Level +%d",
    "item_openwounds":           "%d%% Chance of Open Wounds",
    "item_crushingblow":         "%d%% Chance of Crushing Blow",
    "item_fasterblockrate":      "%d%% Faster Block Rate",
    "item_damagetargetac":       "%+d to Monster Defense Per Hit",
    "item_demon_tohit":          "+%d to Attack Rating against Demons",
    "item_undead_tohit":         "+%d to Attack Rating against Undead",
    "item_demondamage_percent":  "+%d%% Damage to Demons",
    "item_undeaddamage_percent": "+%d%% Damage to Undead",
    "item_pierce":               "Piercing Attack",           # boolean-style
    "item_replenish_quantity":   "Replenishes Quantity",
    "item_req_percent":          "Requirements %d%%",         # usually negative
    "item_reducedprices":        "%d%% Reduced Vendor Prices",
    "item_singleskill":          "_singleskill",              # special handler (param = skill id)
    "item_slow":                 "Slows Target by %d%%",
    "item_howl":                 "%d%% Chance of Monster Flee",
    "item_stupidity":            "Hit Blinds Target +%d",
    "item_preventheal":          "Prevent Monster Heal",      # boolean
    "item_halffreezeduration":   "Half Freeze Duration",      # boolean
    "item_cannotbefrozen":       "Cannot Be Frozen",          # boolean
    "item_replenish_durability": "Repairs 1 Durability in %d Seconds",
    # ── Skills ─────────────────────────────────────────────────────────────
    "item_aura":                 "_aura",                     # special handler
    "item_skillonattack":        "_onattack",                 # special handler
    "item_skillongethit":        "_ongethit",                 # special handler
    "item_skillonhit":           "_onhit",                    # special handler
    "item_charged_skill":        "_charged",                  # special handler
    # ── Per-level ──────────────────────────────────────────────────────────
    "item_armor_perlevel":       "+%d Defense (per level)",
    "item_maxdamage_perlevel":   "+%d Max Damage (per level)",
    "item_tohit_perlevel":       "+%d Attack Rating (per level)",
}


def _fmt_stat(sid: int, name: str, param: int, value: int,
              all_stats: list) -> str:
    """Format one stat entry as game-text.

    To add a new stat: insert a row in _STAT_LABEL above with a %-format string.
    To edit a label:   change the string on the right-hand side.
    Special handlers for damage ranges, auras, and skill procs are below.
    """
    # Stats the game itself cannot render (no descfunc, e.g. item_extrablood on
    # Gorefoot) are hidden so the report matches what's visible in-game.
    if sid in _HIDDEN_STATS:
        return ""
    label = _STAT_LABEL.get(name)
    if label is None:
        # Unknown stat — fall back to internal name so nothing is silently lost
        return f"+{value} {name}" if param == 0 else f"+{value} {name}(p={param})"

    # ── Special handlers ───────────────────────────────────────────────────
    if label == "_fire":
        nxt = {n: v for _, n, _, v in all_stats}
        return f"Adds {value}-{nxt.get('firemaxdam', '?')} Fire Damage"
    if label == "_light":
        nxt = {n: v for _, n, _, v in all_stats}
        return f"Adds {value}-{nxt.get('lightmaxdam', '?')} Lightning Damage"
    if label == "_cold":
        nxt = {n: v for _, n, _, v in all_stats}
        dur  = nxt.get("coldlength", 0)
        secs = dur // 25
        return f"Adds {value}-{nxt.get('coldmaxdam', '?')} Cold Damage ({secs}s)"
    if label == "_poison":
        nxt  = {n: v for _, n, _, v in all_stats}
        dur  = nxt.get("poisonlength", 0)
        secs = dur // 25
        # Poison damage stored as (damage-per-frame × 256); dur is in frames.
        lo   = round(value                          * dur / 256)
        hi   = round(nxt.get("poisonmaxdam", value) * dur / 256)
        if lo == hi:
            return f"+{lo} Poison Damage over {secs}s"
        return f"Adds {lo}-{hi} Poison Damage over {secs}s"
    if label == "_classskills":
        cls_name = _CLASS_NAMES.get(param, f"Class({param})")
        return f"+{value} to {cls_name} Skill Levels"
    if label == "_singleskill":          # param = skill id, class-restricted
        return f"+{value} to {_skill_label_classed(param)}"
    if label == "_oskill":               # param = skill id, usable by any class
        return f"+{value} to {_skill_label(param)}"
    if label == "_skilltab":
        tab = param & 0x7
        cls = (param >> 3) & 0x7
        _CLS = _CLASS_NAMES
        _TAB = {
            (0, 0): "Bow & Crossbow",    (0, 1): "Passive & Magic",      (0, 2): "Javelin & Spear",
            (1, 0): "Fire",              (1, 1): "Lightning",             (1, 2): "Cold",
            (2, 0): "Summoning",         (2, 1): "Poison & Bone",         (2, 2): "Curses",
            (3, 0): "Combat",            (3, 1): "Offensive Auras",       (3, 2): "Defensive Auras",
            (4, 0): "Combat",            (4, 1): "Masteries",             (4, 2): "Warcries",
            (5, 0): "Summoning",         (5, 1): "Shape Shifting",        (5, 2): "Elemental",
            (6, 0): "Traps",             (6, 1): "Shadow Disciplines",    (6, 2): "Martial Arts",
        }
        tab_name = _TAB.get((cls, tab), f"Tab({param})")
        cls_name = _CLS.get(cls, f"Class({cls})")
        return f"+{value} to {tab_name} Skills ({cls_name} Only)"
    if label == "_aura":                 # param = skill id of the granted aura
        return f"Level {value} {_skill_label(param)} Aura When Equipped"
    if label == "_onattack":
        skill_lvl = param & 0x3F
        skill_id  = param >> 6
        return f"{value}% Chance to Cast Level {skill_lvl} {_skill_label(skill_id)} on Attack"
    if label == "_onhit":
        skill_lvl = param & 0x3F
        skill_id  = param >> 6
        return f"{value}% Chance to Cast Level {skill_lvl} {_skill_label(skill_id)} on Striking"
    if label == "_ongethit":
        skill_lvl = param & 0x3F
        skill_id  = param >> 6
        return f"{value}% Chance to Cast Level {skill_lvl} {_skill_label(skill_id)} when Struck"
    if label == "_charged":
        skill_lvl   = param & 0x3F
        skill_id    = param >> 6
        max_charges = value >> 8
        cur_charges = value & 0xFF
        return (f"Level {skill_lvl} {_skill_label(skill_id)} "
                f"({cur_charges}/{max_charges} Charges)")

    # ── Boolean flag (no format specifier after stripping literal %%) ──────
    if "%" not in label.replace("%%", ""):
        return label

    # ── Normal format string ────────────────────────────────────────────────
    return label % value


class D2Item:
    """A single D2R item. Parses one item starting at start_bit; does NOT
    consume the trailing byte-alignment or socketed children (the caller
    handles those, matching D2SLib's ItemList/Item.Read structure)."""

    def __init__(self, data: bytes, start_bit: int, strict_flags: bool = True) -> None:
        self.stream = BitStream(data, start_bit)
        self.start_bit = start_bit
        self._strict_flags = strict_flags
        self.flags_warning = False  # True when bit 23 absent but item parsed anyway

        self.code = ""
        self.identified = False
        self.socketed = False
        self.is_ear = False
        self.simple = False        # "compact" in D2SLib
        self.ethereal = False
        self.personalized = False
        self.runeword = False
        self.runeword_id = 0
        self.children: list = []   # socketed items filling this item's sockets
        self.mode = 0
        self.location = 0
        self.x = 0
        self.y = 0
        self.page = 0
        self.player_name = ""
        self.quantity = 1
        self.num_socketed = 0      # children that follow this item
        self.total_sockets = 0
        self.ilvl = 0
        self.quality = 2
        self.unique_id   = -1
        self.set_id      = -1
        self.rare_name1  = 0   # 1-based index into rareprefix.txt (quality 6/8)
        self.rare_name2  = 0   # 1-based index into raresuffix.txt (quality 6/8)
        self.prefix_ids: list = []  # magicprefix rows (magic/rare) for level-req calc
        self.suffix_ids: list = []  # magicsuffix rows (magic/rare) for level-req calc
        self.stats: list = []      # [(sid, name, param, value), ...]
        self.section_idx = 0
        self.partial = False
        self.partial_bit_pos = 0
        self.partial_stat_id = -1
        self.parse()

    # ------------------------------------------------------------------
    def parse(self) -> None:
        s = self.stream

        flags = s.read_bits(32)
        # Bit 23 is set on every valid D2R item.  Positions lacking it are
        # padding/junk bytes that appear after items whose stat list left the
        # bit-stream 8 bits short (one byte).  Raising here causes
        # _scan_next_item to skip the junk byte and land on the real item.
        if not ((flags >> 23) & 1):
            if self._strict_flags:
                raise UnknownStatError(
                    f"invalid flags 0x{flags:08x} at bit {self.start_bit} (bit 23 not set)",
                    self.start_bit,
                    stat_id=-1,
                )
            self.flags_warning = True
        self.identified   = bool((flags >> 4)  & 1)
        self.socketed     = bool((flags >> 11) & 1)
        self.is_ear       = bool((flags >> 16) & 1)
        self.simple       = bool((flags >> 21) & 1)
        self.ethereal     = bool((flags >> 22) & 1)
        self.personalized = bool((flags >> 24) & 1)
        self.runeword     = bool((flags >> 26) & 1)

        s.read_bits(3)                       # version (3 bits for >=0x61)
        self.mode     = s.read_bits(3)
        self.location = s.read_bits(4)
        self.x        = s.read_bits(4)
        self.y        = s.read_bits(4)
        self.page     = s.read_bits(3)

        if self.is_ear:
            s.read_bits(3)                   # ear: file index
            s.read_bits(7)                   # ear: level
            self.player_name = self._read_player_name()
            return

        self.code = "".join(s.read_huffman_char() for _ in range(4))
        self.num_socketed = s.read_bits(1 if self.simple else 3)

        if self.simple:
            # Stackable compact items (gems, runes, scrolls, arrows, charms) store
            # a 1-bit is-stacked flag followed by 8 bits of quantity (9 bits total);
            # the caller's align() then advances to the next byte boundary.
            #
            # Non-stackable compact items occupy exactly 80 bits (10 bytes) on disk.
            # The Huffman code length varies per item code, so the padding is NOT a
            # fixed 8 bits.  E.g. 'mp2 ' = 18 Huffman bits → header(53)+code(18)+
            # socketed(1) = 72 bits, already byte-aligned → align() is a no-op but
            # the item is still 9 bytes, 1 byte short of the 10-byte boundary.
            # 'mp3 ' = 19 bits → 73 bits → padding = 7 to reach 80.
            # Correct formula: padding = 80 − bits_consumed_so_far.
            code = self.code.strip()
            if code in _STACKABLE_CODES:
                s.read_bits(1)           # flag bit (always 1 for stacked items)
                self.quantity = s.read_bits(8)
            else:
                bits_used = s.bit_pos - self.start_bit
                padding = 80 - bits_used
                if padding > 0:
                    s.read_bits(padding)
            return

        self._parse_complete()

    # ------------------------------------------------------------------
    def _parse_complete(self) -> None:
        s = self.stream

        s.read_bits(32)                      # unique id
        self.ilvl = s.read_bits(7)
        self.quality = s.read_bits(4)

        if s.read_bits(1):                   # has multiple graphics
            s.read_bits(3)                   #   graphic id
        if s.read_bits(1):                   # is auto-affix
            s.read_bits(11)                  #   auto-affix id

        q = self.quality
        if q in (1, 3):                      # inferior / superior
            s.read_bits(3)
        elif q == 4:                         # magic
            pid = s.read_bits(11)            #   prefix (1-based magicprefix row)
            sid = s.read_bits(11)            #   suffix (1-based magicsuffix row)
            if pid:
                self.prefix_ids.append(pid)
            if sid:
                self.suffix_ids.append(sid)
        elif q in (6, 8):                    # rare / crafted
            self.rare_name1 = s.read_bits(8) #   rare name 1 (index into rareprefix)
            self.rare_name2 = s.read_bits(8) #   rare name 2 (index into raresuffix)
            for _ in range(3):
                if s.read_bits(1):
                    self.prefix_ids.append(s.read_bits(11))
                if s.read_bits(1):
                    self.suffix_ids.append(s.read_bits(11))
        elif q == 5:                         # set
            self.set_id = s.read_bits(12)
        elif q == 7:                         # unique
            self.unique_id = s.read_bits(12)
        # q == 2 (normal) and 9 (tempered): no quality-specific fixed data

        extra_lists = 0
        if self.runeword:
            self.runeword_id = s.read_bits(12)
            extra_lists |= 1 << (s.read_bits(4) + 1)

        if self.personalized:
            self.player_name = self._read_player_name()

        # Item codes in the save are 4 chars, space-padded; the game-data
        # tables key on the unpadded code.
        code = self.code.strip()

        if code in ("tbk", "ibk"):
            s.read_bits(5)                   # tome: spell id

        if s.read_bits(1):                   # has realm data
            s.read_bits(96)

        s.read_bits(1)                       # unknown bit after realm block

        is_armor  = code in _ARMOR_CODES
        is_weapon = code in _WEAPON_CODES

        if is_armor:
            s.read_bits(11)                  # defense (armorclass)
        if is_armor or is_weapon:
            max_dur = s.read_bits(_MAXDUR_BITS)
            if max_dur > 0:
                s.read_bits(_MAXDUR_BITS)    # current durability (empirically 8 bits)
                s.read_bits(1)               # indestructible / unknown flag
        if code in _STACKABLE_CODES:
            self.quantity = s.read_bits(9)
        if self.socketed:
            self.total_sockets = s.read_bits(4)

        if q == 5:                           # set: which bonus lists follow
            extra_lists |= s.read_bits(5)

        try:
            self._read_stat_list()
            for bit in (1, 2, 4, 8, 16, 32, 64):
                if extra_lists & bit:
                    self._read_stat_list()
        except UnknownStatError as _e:
            self.partial = True
            self.partial_bit_pos = _e.bit_pos
            self.partial_stat_id = _e.stat_id

    # ------------------------------------------------------------------
    def _read_player_name(self) -> str:
        chars = []
        for _ in range(16):           # 15 chars + null terminator
            c = self.stream.read_bits(7)
            if c == 0:
                break
            chars.append(chr(c))
        return "".join(chars)

    # ------------------------------------------------------------------
    def _read_one_stat(self, sid: int) -> int:
        if not (0 <= sid <= _MAX_STAT_ID):
            raise UnknownStatError(
                f"unknown stat id {sid} at bit {self.stream.bit_pos - 9} "
                f"(item='{self.code}', quality={self.quality})",
                self.stream.bit_pos - 9,
                stat_id=sid,
            )
        param = self.stream.read_bits(_STAT_PARAM_BITS[sid])
        raw   = self.stream.read_bits(_STAT_VALUE_BITS[sid])
        value = raw - _STAT_SAVE_ADD[sid]
        self.stats.append((sid, _STAT_NAMES[sid], param, value))
        return value

    def _read_stat_list(self) -> None:
        s = self.stream
        while True:
            sid = s.read_bits(9)
            if sid == 0x1FF:
                return
            self._read_one_stat(sid)
            if sid in _CONSECUTIVE_ONE:
                self._read_one_stat(sid + 1)
            elif sid in _CONSECUTIVE_TWO:
                self._read_one_stat(sid + 1)
                self._read_one_stat(sid + 2)

    # ------------------------------------------------------------------
    @property
    def location_str(self) -> str:
        return _MODE_STR.get(self.mode, f"Mode({self.mode})")

    @property
    def display_name(self) -> str:
        """Item display name: unique/set/rare name when available, else base type."""
        code = self.code.strip()
        base = _ITEM_NAMES.get(code, code)
        if self.quality == 7 and self.unique_id >= 0:
            return _UNIQUE_NAMES.get(self.unique_id, base)
        if self.quality == 5 and self.set_id >= 0:
            return _SET_ITEM_NAMES.get(self.set_id, base)
        if self.quality in (6, 8) and self.rare_name1 and self.rare_name2:
            w1 = _RARE_NAME.get(self.rare_name1, "")
            w2 = _RARE_NAME.get(self.rare_name2, "")
            if w1 and w2:
                return f"{w1} {w2}"
        if self.quality == 4:                       # magic: prefix + base + suffix
            pre = _PREFIX_NAMES.get(self.prefix_ids[0], "") if self.prefix_ids else ""
            suf = _SUFFIX_NAMES.get(self.suffix_ids[0], "") if self.suffix_ids else ""
            if pre or suf:
                return " ".join(p for p in (pre, base, suf) if p)
        return base

    @property
    def rune_sequence(self) -> str:
        """For runeword items: 'Tir+Ral+Amn' from socketed children."""
        if not self.runeword or not self.children:
            return ""
        parts = []
        for child in self.children:
            code = child.code.strip()
            parts.append(_RUNE_NAME.get(code, code))
        return "+".join(parts)

    @property
    def runeword_name(self) -> str:
        """The runeword's display name (e.g. 'Stealth'), matched from the ordered
        rune codes in its sockets.  Empty if not a runeword or no match."""
        if not self.runeword or not self.children:
            return ""
        rune_codes = tuple(c.code.strip() for c in self.children)
        return _RUNEWORD_NAMES.get(rune_codes, "")

    @property
    def level_req(self) -> int:
        # Required level is the MAX of: base item req, the unique/set item's own
        # req, and every magic affix's req (magic/rare items).  D2 takes the max,
        # not the sum.  The item_levelreq stat ("Required Level +N") adds on top.
        reqs = [_ITEM_LEVELREQ.get(self.code.strip(), 0)]
        if self.quality == 7:
            reqs.append(_UNIQUE_LEVELREQ.get(self.unique_id, 0))
        elif self.quality == 5:
            reqs.append(_SET_LEVELREQ.get(self.set_id, 0))
        for pid in self.prefix_ids:
            reqs.append(_PREFIX_LEVELREQ.get(pid, 0))
        for sid in self.suffix_ids:
            reqs.append(_SUFFIX_LEVELREQ.get(sid, 0))
        base = max(reqs)
        for _, name, _, value in self.stats:
            if name == "item_levelreq":
                base += value
        return base

    def stats_str(self) -> str:
        """Compact, human-readable attributes string."""
        parts = []
        skip = 0
        for sid, name, param, value in self.stats:
            if skip > 0:
                skip -= 1
                continue
            parts.append(_fmt_stat(sid, name, param, value, self.stats))
            if sid in _CONSECUTIVE_ONE:
                skip = 1
            elif sid in _CONSECUTIVE_TWO:
                skip = 2
        result = ", ".join(p for p in parts if p)
        if self.partial:
            result = (result + " " if result else "") + "[PARTIAL]"
        return result

    def __repr__(self) -> str:
        if self.is_ear:
            return f"Ear({self.player_name!r}) @ {self.location_str}"
        flags = []
        if self.identified:
            flags.append("ID")
        if self.ethereal:
            flags.append("Ethereal")
        if self.socketed:
            flags.append(f"Sck{self.total_sockets}")
        if self.runeword:
            seq = self.rune_sequence
            flags.append(f"Runeword:{seq}" if seq else f"RW(id={self.runeword_id})")
        if self.personalized:
            flags.append(f"Pers:{self.player_name}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        qual = _QUALITY_STR.get(self.quality, f"Q{self.quality}")
        code = self.code.strip()
        name = self.display_name
        qty = f" x{self.quantity}" if self.quantity != 1 else ""
        if name != code:
            return f"{name} [{code}]{qty} {qual}{flag_str} @ {self.location_str}"
        return f"{code}{qty} {qual}{flag_str} @ {self.location_str}"


# ---------------------------------------------------------------------------
# Shared markdown helpers
# ---------------------------------------------------------------------------


def _qty_note(item: "D2Item") -> str:
    """Quantity as 'current of max' for a genuinely stackable item (e.g. '8 of 20').
    Empty string for items that don't stack (single potions, rejuvs) or whose
    quantity is just 1 — those carry no meaningful count."""
    code = item.code.strip()
    if code not in _STACKABLE_CODES:
        return ""
    mx = _ITEM_MAXSTACK.get(code, 0)
    if mx > 1:
        return f"{item.quantity} of {mx}"
    return str(item.quantity) if item.quantity > 1 else ""


def _runeword_note(item: "D2Item") -> str:
    """Runeword label combining name and runes, e.g. 'Stealth (Tal+Eth)'."""
    seq = item.rune_sequence
    name = item.runeword_name
    if name and seq:
        return f"{name} ({seq})"
    if seq:
        return f"Runeword ({seq})"
    return f"Runeword(id={item.runeword_id})"


def _attrs_cell(item: "D2Item") -> str:
    """Build the combined Attributes cell, folding in the former Notes column.

    Layout (one table cell):
        <stat list>, Ethereal, Personalized: <name>, ⚠NonStdFlags
        <br>Quantity: <n of max>
        <br><n> sockets — <runeword (runes)>

    Ethereal, personalization, and the non-standard-flags parse warning sit
    inline at the end of the stat list; quantity and sockets each get their own
    <br> line.  ('identified' is intentionally omitted — not useful to the reader.)"""
    inline = []
    attrs = item.stats_str()
    if attrs:
        inline.append(attrs)
    if item.ethereal:
        inline.append("Ethereal")
    if item.personalized:
        inline.append(f"Personalized: {item.player_name}")
    if item.flags_warning:
        inline.append("⚠NonStdFlags")
    cell = ", ".join(inline)

    extras = []
    qn = _qty_note(item)
    if qn:
        extras.append(f"Quantity: {qn}")
    if item.socketed or item.runeword:
        sock = []
        if item.socketed:
            sock.append(f"{item.total_sockets} sockets")
        if item.runeword:
            sock.append(_runeword_note(item))
        extras.append(" — ".join(sock))
    for extra in extras:
        cell = (cell + "<br>" if cell else "") + extra
    return cell


class _ReportConfig:
    """Optional, off-by-default columns/panels that make the report less compact.
    Toggled from main()'s CLI flags; all default False so the baseline report is
    as compact as possible (base type still shown inline via *(base)* when the
    display name differs, so no item identity is lost when these are off)."""
    base_type_columns = False   # the "Base Type"/"Code" column in every item table
    belts             = False   # each character's Belt inventory panel
    cubes             = False   # each character's Horadric Cube panel


_CONFIG = _ReportConfig()


def _gear_table_header(first_col: str) -> tuple:
    """(header, separator) for a gear/slot table. `first_col` is '#' or 'Slot'.
    The Base Type column is included only when enabled in the config."""
    cols = [first_col, "Item Name"]
    if _CONFIG.base_type_columns:
        cols.append("Base Type")
    cols += ["Quality", "Lvl Req", "Attributes"]
    return ("| " + " | ".join(cols) + " |",
            "|" + "|".join("---" for _ in cols) + "|")


def _append_gear_row(lines: list, i_or_slot, item: "D2Item") -> None:
    """Append one row to a gear table; columns mirror _gear_table_header()."""
    code  = item.code.strip()
    base  = _ITEM_NAMES.get(code, code)
    dname = item.display_name
    name_cell = f"{dname} *({base})*" if dname != base else dname
    qual  = _QUALITY_STR.get(item.quality, f"Q{item.quality}")
    lvl   = item.level_req or ""
    cells = [str(i_or_slot), name_cell]
    if _CONFIG.base_type_columns:
        cells.append(f"`{code}`")
    cells += [qual, str(lvl), _attrs_cell(item)]
    lines.append("| " + " | ".join(cells) + " |")


def _qty_cell(item: "D2Item") -> str:
    """Quantity cell for consumable/belt tables: 'current of max' for stackables,
    plain '1' for non-stackables (single potions etc.)."""
    return _qty_note(item) or "1"


def _write_consumable_table(lines: list, items: list) -> None:
    """Write a consumable/stackable table (| # | Item | [Code] | Qty |).
    The Code column is included only when enabled in the config."""
    cols = ["#", "Item"]
    if _CONFIG.base_type_columns:
        cols.append("Code")
    cols.append("Qty")
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join("---" for _ in cols) + "|")
    for i, item in enumerate(items, 1):
        cells = [str(i), item.display_name]
        if _CONFIG.base_type_columns:
            cells.append(f"`{item.code.strip()}`")
        cells.append(_qty_cell(item))
        lines.append("| " + " | ".join(cells) + " |")


# ---------------------------------------------------------------------------
# Stash file parser
# ---------------------------------------------------------------------------


class D2IFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        self.items: List[D2Item] = []
        self._current_section_idx = 0
        self.parse()

    def _scan_next_item(self, from_bit: int, section_end_bit: int) -> int:
        """Scan forward byte-by-byte from from_bit looking for a byte-aligned
        position whose item header decodes to a known item code.  Returns the
        bit offset of the next candidate, or section_end_bit if none found."""
        pos = (from_bit + 7) & ~7          # align to next byte
        while pos + 300 < section_end_bit:
            try:
                s = BitStream(self.data, pos)
                flags  = s.read_bits(32)
                if not ((flags >> 23) & 1):
                    raise ValueError(f"bit 23 not set in flags 0x{flags:08x}")
                ear    = bool((flags >> 16) & 1)
                simple = bool((flags >> 21) & 1)
                s.read_bits(3)                      # version
                mode = s.read_bits(3)
                if mode != 0:                       # stash items are Stored (0)
                    raise ValueError("bad mode")
                s.read_bits(4 + 4 + 4 + 3)         # location/x/y/page
                if not ear:
                    code = "".join(s.read_huffman_char() for _ in range(4))
                    if code.strip() in _ITEM_NAMES:
                        if not simple:
                            # Validate ilvl and quality so we don't land on cascade garbage
                            s.read_bits(3)          # num_socketed
                            s.read_bits(32)         # uid
                            s.read_bits(7)          # ilvl
                            q = s.read_bits(4)
                            if q > 9:
                                raise ValueError("bad quality")
                        return pos
            except Exception:
                pass
            pos += 8
        return section_end_bit

    def _zero_byte_at(self, bit: int) -> bool:
        """True if the 8 bits starting at `bit` are all zero (byte-aligned pad)."""
        if bit % 8 != 0 or bit // 8 >= len(self.data):
            return False
        return self.data[bit // 8] == 0

    def _valid_item_code_at(self, bit: int) -> bool:
        """True if a byte-aligned item header at `bit` decodes to a known item code."""
        if bit % 8 != 0 or bit + 80 > len(self.data) * 8:
            return False
        try:
            s = BitStream(self.data, bit)
            flags = s.read_bits(32)
            if (flags >> 16) & 1:                    # ear item: no code field
                return False
            s.read_bits(3 + 3 + 4 + 4 + 4 + 3)       # ver/mode/loc/x/y/page
            code = "".join(s.read_huffman_char() for _ in range(4)).strip()
            return code in _ITEM_NAMES
        except Exception:
            return False

    def _skip_pad(self, bit_off: int, end_bit: int) -> int:
        """Skip a single zero pad byte left between an item that ends exactly on a
        byte boundary and the next item (see D2CharFile for the full rationale)."""
        if (not self._valid_item_code_at(bit_off)
                and bit_off + 8 <= end_bit
                and self._zero_byte_at(bit_off)
                and self._valid_item_code_at(bit_off + 8)):
            return bit_off + 8
        return bit_off

    def _read_item_tree(self, bit_offset: int, end_bit: int) -> int:
        """Read one item plus its socketed children, mirroring D2SLib
        Item.Read: read item, byte-align, then recurse for each child."""
        item = D2Item(self.data, bit_offset)
        item.section_idx = self._current_section_idx
        self.items.append(item)
        if item.partial:
            raise UnknownStatError(
                f"partial item '{item.code.strip()}' quality={item.quality} "
                f"stat_id={item.partial_stat_id}",
                item.partial_bit_pos,
                stat_id=item.partial_stat_id,
            )
        s = item.stream
        s.align()
        bit_offset = s.bit_pos
        for _ in range(item.num_socketed):
            bit_offset = self._skip_pad(bit_offset, end_bit)
            child_start_idx = len(self.items)
            bit_offset = self._read_item_tree(bit_offset, end_bit)
            # attach newly-added child (and its own children) to the parent
            for child in self.items[child_start_idx:]:
                item.children.append(child)
        return bit_offset

    def parse(self) -> None:
        section_offset = 0
        while section_offset + 68 <= len(self.data):
            if struct.unpack_from("<I", self.data, section_offset)[0] != 0xAA55AA55:
                break
            jm_offset = section_offset + 0x40
            if self.data[jm_offset:jm_offset + 2] != b"JM":
                break

            item_count = struct.unpack_from("<H", self.data, jm_offset + 2)[0]
            bit_offset = (jm_offset + 4) * 8

            section_length = struct.unpack_from("<I", self.data, section_offset + 0x10)[0]
            if section_length == 0:
                break
            section_end_bit = (section_offset + section_length) * 8

            parsed = 0
            while parsed < item_count:
                try:
                    bit_offset = self._read_item_tree(bit_offset, section_end_bit)
                    parsed += 1
                    if parsed < item_count:
                        bit_offset = self._skip_pad(bit_offset, section_end_bit)
                except Exception as exc:
                    print(
                        f"  [SKIP] section @ 0x{section_offset:x}: item {parsed+1}/{item_count} "
                        f"could not be parsed — {exc} — scanning forward"
                    )
                    # Scan from 1 byte past the item START (not exc.bit_pos, which may
                    # sit deep inside a garbage stat list past the real next item).
                    next_offset = self._scan_next_item(bit_offset + 8, section_end_bit)
                    # Guard against infinite loop: scanner must advance forward.
                    if next_offset >= section_end_bit or next_offset <= bit_offset:
                        break
                    bit_offset = next_offset

            self._current_section_idx += 1
            section_offset += section_length

    def display(self) -> None:
        print("=" * 70)
        print(f"D2R SHARED STASH: {self.path.name}")
        print("=" * 70)
        if not self.items:
            print("  (No items)")
            return
        print(f"\n  {len(self.items)} items total")
        print("-" * 70)
        for idx, item in enumerate(self.items, start=1):
            print(f"    {idx:3d}. {item}")

    def _write_stash_sections(self, lines: list) -> None:
        """Append all stash section tables for this file into lines."""
        sections: dict = defaultdict(list)
        for item in self.items:
            sections[item.section_idx].append(item)

        lines.append(f"## Stash: {self.path.name}")
        lines.append(f"*{len(self.items)} items total*")
        lines.append("")

        for sec_idx in sorted(sections):
            sec_items = sections[sec_idx]
            partial_count = sum(1 for it in sec_items if it.partial)

            consumable_count = sum(
                1 for it in sec_items
                if it.code.strip() in _STACKABLE_CODES or it.simple
            )
            is_consumable = consumable_count == len(sec_items)

            if is_consumable:
                sec_label = "Gems, Runes & Consumables"
            else:
                sec_label = f"Page {sec_idx + 1}"

            partial_note = f", {partial_count} partially parsed" if partial_count else ""
            lines.append(f"### {sec_label}")
            lines.append(f"*{len(sec_items)} items{partial_note}*")
            lines.append("")

            if is_consumable:
                _write_consumable_table(lines, sec_items)
            else:
                header, sep = _gear_table_header("#")
                lines.append(header)
                lines.append(sep)
                for i, item in enumerate(sec_items, 1):
                    _append_gear_row(lines, i, item)
            lines.append("")

    def write_markdown(self, output_path: Path,
                       chars: "Optional[List[D2CharFile]]" = None) -> None:
        lines = [
            "# D2R Save Report",
            "",
            f"**Source:** `{self.path.name}`  ",
            f"**Total items parsed:** {len(self.items)}",
            "",
            "[toc]",
            "",
        ]
        self._write_stash_sections(lines)
        if chars:
            lines.append("## Character Inventories")
            lines.append("")
            for char in chars:
                char.write_markdown_sections(lines)
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Character save (.d2s) parser
# ---------------------------------------------------------------------------


class D2CharFile:
    """Parses a D2R character save (.d2s) to extract inventory items.

    Header offsets (vanilla D2R v105 saves):
      offset 24 : class byte (0=Amazon..6=Assassin, 7=Warlock)
      offset 27 : level byte
      offset 299: character name (16 bytes, null-terminated ASCII)

    Item sections are located by scanning for b'JM' markers:
      1st JM: character's own item list
      2nd JM: corpse items (usually 0)
      3rd JM: mercenary items
    """

    _CLASS_NAMES = {
        0: "Amazon", 1: "Sorceress", 2: "Necromancer",
        3: "Paladin", 4: "Barbarian", 5: "Druid", 6: "Assassin",
        7: "Warlock",
    }

    def __init__(self, path: Path) -> None:
        self.path   = path
        self.data   = path.read_bytes()
        self.char_name  = ""
        self.char_class = 0
        self.char_level = 0
        self.items:      List[D2Item] = []   # character's own items
        self.merc_items: List[D2Item] = []   # mercenary items
        self._parse_header()
        self._parse_all_items()

    # ------------------------------------------------------------------
    def _parse_header(self) -> None:
        self.char_class = self.data[24]
        self.char_level = self.data[27]
        self.char_name  = self.data[299:315].split(b"\x00")[0].decode("ascii", errors="replace")

    # ------------------------------------------------------------------
    def _find_jm(self, start: int) -> tuple:
        """Return (byte_offset, item_count) of next JM section from start."""
        pos = start
        while pos < len(self.data) - 3:
            if self.data[pos:pos + 2] == b"JM":
                count = struct.unpack_from("<H", self.data, pos + 2)[0]
                if count <= 300:
                    return pos, count
            pos += 1
        return -1, 0

    # ------------------------------------------------------------------
    # Valid characters in a D2 item code (Huffman alphabet)
    _VALID_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 ")

    def _zero_byte_at(self, bit: int) -> bool:
        """True if the 8 bits starting at `bit` are all zero (byte-aligned pad)."""
        if bit % 8 != 0 or bit // 8 >= len(self.data):
            return False
        return self.data[bit // 8] == 0

    def _valid_item_code_at(self, bit: int) -> bool:
        """True if a byte-aligned item header at `bit` decodes to a known item
        code (used to validate item boundaries without a full parse)."""
        if bit % 8 != 0 or bit + 80 > len(self.data) * 8:
            return False
        try:
            s = BitStream(self.data, bit)
            flags = s.read_bits(32)
            if (flags >> 16) & 1:                    # ear item: no code field
                return False
            s.read_bits(3 + 3 + 4 + 4 + 4 + 3)       # ver/mode/loc/x/y/page
            code = "".join(s.read_huffman_char() for _ in range(4)).strip()
            return code in _ITEM_NAMES
        except Exception:
            return False

    def _scan_next_item(self, from_bit: int, end_bit: int) -> int:
        """Scan forward (byte-aligned) for the next plausible item start.

        Validity requires a known item code (present in _ITEM_NAMES), valid
        Huffman characters, and a sane quality value for non-compact items.
        """
        pos = (from_bit + 7) & ~7
        while pos + 300 < end_bit:
            try:
                s = BitStream(self.data, pos)
                flags = s.read_bits(32)
                ear    = bool((flags >> 16) & 1)
                simple = bool((flags >> 21) & 1)
                s.read_bits(3)           # version
                s.read_bits(3)           # mode (any value allowed for characters)
                s.read_bits(4 + 4 + 4 + 3)  # location/x/y/page
                if ear:
                    raise ValueError("skip ear positions in scan")
                code = "".join(s.read_huffman_char() for _ in range(4))
                if not all(c in self._VALID_CODE_CHARS for c in code):
                    raise ValueError("invalid code chars")
                if not code.strip():
                    raise ValueError("empty code")
                if code.strip() not in _ITEM_NAMES:
                    raise ValueError("unknown item code")
                if not simple:
                    s.read_bits(3)   # num_socketed
                    s.read_bits(32)  # uid
                    s.read_bits(7)   # ilvl
                    q = s.read_bits(4)
                    if q > 9:
                        raise ValueError("bad quality")
                return pos
            except Exception:
                pass
            pos += 8
        return end_bit

    # ------------------------------------------------------------------
    def _parse_section(self, jm_off: int, count: int) -> List["D2Item"]:
        """Parse one JM item list, returning all top-level items."""
        if jm_off < 0 or count == 0:
            return []
        items: List[D2Item] = []
        bit_offset = (jm_off + 4) * 8
        # Use a generous end boundary (next 64 KB or EOF)
        end_bit = min(len(self.data), jm_off + 65536) * 8
        parsed = 0
        # Need a temporary list + tree helper; replicate D2IFile logic inline.
        all_items: List[D2Item] = []

        # Some items end exactly on a byte boundary and are followed by a single
        # zero pad byte that align() cannot absorb (observed on socketed items and
        # set armor).  If the next position is not a valid item start but skipping
        # one zero byte makes it one, consume the pad byte.  Fully validated against
        # _ITEM_NAMES, so it never invents garbage.  Applies both between top-level
        # items AND between an item and its socketed children.
        def skip_pad(bit_off: int) -> int:
            if (not self._valid_item_code_at(bit_off)
                    and bit_off + 8 <= end_bit
                    and self._zero_byte_at(bit_off)
                    and self._valid_item_code_at(bit_off + 8)):
                return bit_off + 8
            return bit_off

        def read_tree(bit_off: int) -> int:
            item = D2Item(self.data, bit_off, strict_flags=False)
            all_items.append(item)
            if item.partial:
                raise UnknownStatError(
                    f"partial item '{item.code.strip()}'",
                    item.partial_bit_pos,
                    stat_id=item.partial_stat_id,
                )
            item.stream.align()
            bit_off = item.stream.bit_pos
            for _ in range(item.num_socketed):
                bit_off = skip_pad(bit_off)
                child_start = len(all_items)
                bit_off = read_tree(bit_off)
                for child in all_items[child_start:]:
                    item.children.append(child)
            return bit_off

        while parsed < count:
            try:
                bit_offset = read_tree(bit_offset)
                parsed += 1
                if parsed < count:
                    bit_offset = skip_pad(bit_offset)
            except Exception as exc:
                _sid = getattr(exc, "stat_id", -1)
                _sname = _STAT_NAMES[_sid] if 0 <= _sid < len(_STAT_NAMES) else "?"
                print(
                    f"  [WARN] {self.path.name} item {parsed+1}/{count} "
                    f"could not be parsed — {exc} [stat_id={_sid} '{_sname}'] — scanning forward"
                )
                # Scan from 1 byte past the item START to find the next known-code item.
                next_off = self._scan_next_item(bit_offset + 8, end_bit)
                if next_off >= end_bit or next_off <= bit_offset:
                    print(f"  [WARN] {self.path.name}: no valid next item found, stopping parse")
                    break
                bit_offset = next_off

        # Return only top-level items (socketed children are nested in .children)
        # all_items contains both parents and children; we need just the roots.
        # The roots are items whose bit start_bit matches what we passed to read_tree.
        # Simpler: collect items as they are appended at the root level.
        # Re-collect: items appended at the root level are those not nested as children.
        child_ids = set()
        for it in all_items:
            for ch in it.children:
                child_ids.add(id(ch))
        return [it for it in all_items if id(it) not in child_ids]

    # ------------------------------------------------------------------
    def _parse_all_items(self) -> None:
        jm1_off, cnt1 = self._find_jm(400)
        self.items = self._parse_section(jm1_off, cnt1)

        # Second JM = corpse (skip); third JM = merc items
        jm2_off, _ = self._find_jm(jm1_off + 1) if jm1_off >= 0 else (-1, 0)
        jm3_off, cnt3 = self._find_jm(jm2_off + 1) if jm2_off >= 0 else (-1, 0)
        self.merc_items = self._parse_section(jm3_off, cnt3)

    # ------------------------------------------------------------------
    def display(self) -> None:
        cls_name = self._CLASS_NAMES.get(self.char_class, f"Class({self.char_class})")
        print("=" * 70)
        print(f"D2R CHARACTER: {self.char_name} ({cls_name}, Level {self.char_level})")
        print("=" * 70)
        print(f"  {len(self.items)} inventory items, {len(self.merc_items)} merc items")

    # ------------------------------------------------------------------
    def write_markdown_sections(self, lines: list) -> None:
        cls_name = self._CLASS_NAMES.get(self.char_class, f"Class({self.char_class})")
        lines.append(f"### {self.char_name}  ({cls_name} · Level {self.char_level})")
        lines.append("")

        # mode=6 (Socketed) items are children nested inside other items; skip them.
        # page=4 is the Horadric Cube in all known D2R formats.
        visible = [it for it in self.items if it.mode != 6]

        # Build equipped table: one item per valid body slot (1-12).
        # Scan-forward false positives can land at real equip slots before the genuine
        # item in all_items order.  Pick the best candidate per slot using a 3-level
        # confidence: known code > unknown code; within same tier, no-flags_warning >
        # flags_warning; within same tier-pair, keep first (player precedes merc in JM1).
        _VALID_EQUIP_LOCS = set(range(1, 13))   # Head=1 … SwapL=12

        def _equip_score(item: "D2Item") -> int:
            known = item.code.strip() in _ITEM_NAMES
            clean = not item.flags_warning
            return (2 if known else 0) + (1 if clean else 0)

        slot_best: dict = {}   # location -> D2Item
        for it in visible:
            if it.mode == 1 and it.location in _VALID_EQUIP_LOCS:
                loc = it.location
                if loc not in slot_best or _equip_score(it) > _equip_score(slot_best[loc]):
                    slot_best[loc] = it
        equipped = list(slot_best.values())
        # Stored items (mode=0) live on one of several panels, distinguished by the
        # 3-bit `page` field: 1 = backpack inventory, 4 = Horadric Cube, 5 = personal
        # stash.  Any other page value is bucketed as "other stored" so nothing is lost.
        backpack  = [it for it in visible if it.mode == 0 and it.page == 1]
        cube      = [it for it in visible if it.mode == 0 and it.page == 4]
        stash     = [it for it in visible if it.mode == 0 and it.page == 5]
        other     = [it for it in visible if it.mode == 0 and it.page not in (1, 4, 5)]
        belt      = [it for it in visible if it.mode == 2]

        # ── Equipped ──────────────────────────────────────────────────
        if equipped:
            lines.append(f"#### Equipped - {self.char_name}")
            lines.append(f"*{len(equipped)} items*")
            lines.append("")
            header, sep = _gear_table_header("Slot")
            lines.append(header)
            lines.append(sep)
            for item in equipped:
                slot = _LOCATION_STR.get(item.location, f"Slot({item.location})")
                _append_gear_row(lines, slot, item)
            lines.append("")

        # ── Inventory (backpack) ──────────────────────────────────────
        def _write_stored_panel(title: str, items: list) -> None:
            if not items:
                return
            consumable_count = sum(
                1 for it in items if it.code.strip() in _STACKABLE_CODES or it.simple
            )
            is_all_consumable = consumable_count == len(items)
            lines.append(f"#### {title} - {self.char_name}")
            lines.append(f"*{len(items)} items*")
            lines.append("")
            if is_all_consumable:
                _write_consumable_table(lines, items)
            else:
                header, sep = _gear_table_header("#")
                lines.append(header)
                lines.append(sep)
                for i, item in enumerate(items, 1):
                    _append_gear_row(lines, i, item)
            lines.append("")

        _write_stored_panel("Inventory", backpack)

        # ── Belt ──────────────────────────────────────────────────────
        # Off by default (compact); enable with --belts.
        if belt and _CONFIG.belts:
            lines.append(f"#### Belt - {self.char_name}")
            lines.append(f"*{len(belt)} items*")
            lines.append("")
            _write_consumable_table(lines, belt)
            lines.append("")

        # ── Horadric Cube ─────────────────────────────────────────────
        # Off by default (compact); enable with --cubes.
        if _CONFIG.cubes:
            _write_stored_panel("Horadric Cube", cube)

        # ── Personal Stash ────────────────────────────────────────────
        _write_stored_panel("Personal Stash", stash)

        # ── Unparsed / misaligned (mode=0 with an unmapped page value) ─
        # In practice these are scan-forward phantoms (page=0, PARTIAL, NonStdFlags):
        # surfaced rather than dropped so failed parses stay visible for inspection.
        _write_stored_panel("Unparsed / Misaligned", other)

        # ── Mercenary ─────────────────────────────────────────────────
        if self.merc_items:
            lines.append(f"#### Mercenary - {self.char_name}")
            lines.append(f"*{len(self.merc_items)} items*")
            lines.append("")
            header, sep = _gear_table_header("Slot")
            lines.append(header)
            lines.append(sep)
            for item in self.merc_items:
                slot = _LOCATION_STR.get(item.location, f"Slot({item.location})")
                _append_gear_row(lines, slot, item)
            lines.append("")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse D2R save files into a single items.md report."
    )
    parser.add_argument(
        "--base-type-columns", action="store_true",
        help="Include the Base Type/Code columns in every item table "
             "(off by default; base type still shows inline as *(base)*).",
    )
    parser.add_argument(
        "--belts", action="store_true",
        help="Include each character's Belt inventory panel (off by default).",
    )
    parser.add_argument(
        "--cubes", action="store_true",
        help="Include each character's Horadric Cube panel (off by default).",
    )
    args = parser.parse_args()
    _CONFIG.base_type_columns = args.base_type_columns
    _CONFIG.belts            = args.belts
    _CONFIG.cubes            = args.cubes

    stash_dir = Path(r"C:\Users\dmlop\Saved Games\Diablo II Resurrected")
    output_dir = _SCRIPT_DIR.parent
    report_path = output_dir / "items.md"

    chars = []
    for d2s_file in sorted(stash_dir.glob("*.d2s")):
        try:
            char = D2CharFile(d2s_file)
            char.display()
            chars.append(char)
        except Exception as exc:
            print(f"Error reading {d2s_file.name}: {exc}")

    stashes = []
    for d2i_file in sorted(stash_dir.glob("*.d2i")):
        try:
            stash = D2IFile(d2i_file)
            stash.display()
            stashes.append(stash)
        except Exception as exc:
            print(f"Error reading {d2i_file.name}: {exc}\n")

    if stashes:
        # Write a single combined report: all stash pages then all characters.
        # Use the first stash as the anchor for write_markdown; append remaining
        # stash sections by re-calling write_markdown with successive stashes and
        # merging, or build all lines here.
        lines = [
            "# D2R Save Report",
            "",
            f"**Characters parsed:** {len(chars)}  ",
            f"**Stash files parsed:** {len(stashes)}",
            "",
            "[toc]",
            "",
        ]
        # Write all stash sections
        for stash in stashes:
            stash._write_stash_sections(lines)
        # Write all character sections
        if chars:
            lines.append("## Character Inventories")
            lines.append("")
            for char in chars:
                char.write_markdown_sections(lines)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote {report_path}")
    elif chars:
        # Characters only, no stash files found
        lines = [
            "# D2R Save Report",
            "",
            f"**Characters parsed:** {len(chars)}",
            "",
            "## Character Inventories",
            "",
        ]
        for char in chars:
            char.write_markdown_sections(lines)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
