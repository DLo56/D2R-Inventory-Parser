# Agent Instructions — D2R Save Parser

Guidance for AI coding agents (GitHub Copilot, Claude Code, Cursor, etc.)
working in this repo. Read this before editing.

## What this project is

A pure-Python (stdlib-only, Python 3.8+) parser for **Diablo II: Resurrected**
save files:

- `Scripts/d2i_parser.py` — parses `*.d2s` (characters) and `*.d2i` (shared
  stash) into a **single** `items.md` report at the repo root.
- `Scripts/casc_extract.py` — refreshes game-data tables straight from the
  user's local D2R install (CASC), into `Scripts/gamedata_d2r/excel/` and
  `Scripts/_strings/`. Stdlib only (`struct`, `zlib`, `hashlib`).

Run the parser: `python Scripts/d2i_parser.py` (flags: `--belts`, `--cubes`,
`--base-type-columns`). Refresh data: `python Scripts/casc_extract.py [install_dir]`.

## Hard rules (do not violate)

1. **This is VANILLA / official D2R — never call anything "modding."** Vanilla
   D2R adds official content over patches (e.g. Warlock class, new skills,
   new runewords). If content doesn't resolve (`Skill(id=N)`, `Class(N)`,
   unnamed items), the cause is **stale bundled game-data tables**, not a mod.
   The fix is re-running `casc_extract.py` to update the tables — never add
   code, comments, or docs implying the save is modded.

2. **Never skip or hide bad parses.** Failed/partial items must still be
   emitted to `items.md` with visible markers (`⚠NonStdFlags`, `[PARTIAL]`).
   Do not add `continue`/skip logic for failed items — the user inspects bad
   output to reverse-engineer the correct parse. Hiding failures destroys
   diagnostic value.

3. **One output file only.** `main()` collects all stashes + all characters
   and writes a single `items.md` in the repo root. Do not create per-stash or
   per-character output files.

4. **Stdlib only.** No third-party dependencies in either script.

5. **Keep game data current**, never bundle stale tables as the source of truth.
  The live tables under `Scripts/gamedata_d2r/excel/` (loaded as
   `_GAMEDATA`) and `Scripts/_strings/` are authoritative. The legacy
   `Scripts/d2r_*.txt` / `d2r_*.json` / `_pyd2s_runtime/` (classic-D2 MPQ data)
   are **unused/stale** — do not reintroduce them as data sources.

6. When modifying user-facing features, ensure that the README.md is updated
   to reflect the changes.

## Save-file format cheat-sheet

Bit fields are LSB-first. Items are byte-aligned (caller `align()` after each
item and each socketed child). Key points that were historically wrong and
must be preserved:

- **Compact (simple) items read NO explicit padding field** — the caller's
  `align()` advances to the next byte boundary. Adding fixed-width padding
  desyncs the stream (Huffman code length varies per item code).
- Item code = exactly **4 Huffman-decoded chars** (not space-terminated).
- `NumberOfSocketedItems`: 1 bit if compact, 3 bits otherwise.
- 1 extra bit between the realm-data flag and the armor/durability/stat block
  on every extended item (D2SLib does not document this).
- **Byte-aligned zero pad byte:** some items (e.g. content ending exactly on a
  byte boundary, and socketed items before their rune/gem children) are
  followed by a single `0x00` pad byte that `align()` cannot absorb. Handled
  by `_skip_pad` / `_zero_byte_at` / `_valid_item_code_at` — present in **both**
  the character path (`D2CharFile`) and the **separate** stash path (`D2IFile`).
- Durability: both max and current durability are empirically **8 bits** in
  these saves (using 9 explodes desyncs). Use `_MAXDUR_BITS`.
- Stackables (tomes, keys, ammo, **and throwing weapons** — javelins/throwing
  axes/spears) carry a 9-bit quantity. Throwing weapons are weapons, so they
  read durability **and** quantity.

"Zero desyncs" means BOTH no `scanning forward` (character path) **and** no
`[SKIP]` (stash path) — they are different code paths with different messages.

## Character header sections (attributes & skills)

Between the fixed header and the first `JM` item list, a `.d2s` carries two
sections the parser also reads (`_parse_stats` / `_parse_skills`):

- **Attributes** — marker `gf`, then a packed bit-stream of `[9-bit stat id]
  [value]` pairs terminated by id `0x1FF`. Value widths come from
  itemstatcost's **`CSvBits`** column (NOT the item "Save Bits"); `CSvSigned`
  marks two's-complement values. Only base stats (ids 0–15) are stored;
  life/mana/stamina (current+max) are **×256 fixed-point**. Derived stats
  (resists, breakpoints) are runtime-only and never in the save.
- **Skills** — marker `if`, then **30 bytes** of hard points, one per class
  skill. Index 0–29 maps to `skills.txt` rows filtered by the class's
  `charclass` token (row order == in-game tree order). Class id→token lives in
  `_CLASS_SKILL_TOKEN`. Gear `+skills` are runtime-only, not saved.

`From Items` totals are computed from the **primary equipped set (loc 1–10,
excluding weapon-swap 11/12) + inventory charms (`cm1/2/3`)** — the items the
game counts toward the sheet. Sum stat 127 (all-skills), 83 (class, param==class
id), 188 (skill tab), 107 (single skill, param==skill id); skip 204
(charges/oskills). Two effects the save doesn't hand you directly:
- **Set partial bonuses** are stored on the item (extra stat lists after the
  base list, recorded as index ranges in `set_bonus_lists`) but are only active
  once enough set pieces are worn. Gate them: list *k* activates at *k+2* pieces,
  i.e. apply `set_bonus_lists[:pieces-1]`, dropping the rest. Count pieces by
  `_SET_ITEM_GROUP[set_id]`. They still render in the item's mod string.
- **Rune/gem socket mods** aren't stored at all (the "when socketed in X" gap).
  Recompute from `gems.txt` (`weaponMod/helmMod/shieldMod`) keyed by the parent's
  base type — weapon vs `_SHIELD_TYPES` vs `_HELM_ARMOR_TYPES` (`_ARMOR_TYPE`).
  Mod codes resolve to stat ids via `properties.txt` (`_PROP_STATS`, e.g.
  `res-all` → all four resists), so resistances/MF/etc. are covered, not just
  attributes.

`_compute_item_bonuses` also accumulates `item_stat_totals[sid]` (gated set
bonuses + socket mods) for the **Gear Bonuses** table — aggregate "page 2" stats
(`_GEAR_BONUS_ROWS` + `_RESIST_ROWS`). These are **gear-only**: passive skills
and auras that feed the same stats in-game (Barbarian Natural Resistance /
Increased Speed, resist auras) are NOT added, and resistances are raw gear totals
(no difficulty penalty, no 75% cap). Defense/AR/Damage need full formulas and are
out of scope. Keep the section's caveat note in sync with what's actually summed. **Skill-tab gotcha:** the `item_addskill_tab` param tab index
(see `_skilltab`, screen order) does NOT equal `skilldesc.SkillPage` and the
permutation differs per class (Paladin Combat=page1=tab0, but Necromancer
Curses=page1=tab2). Resolve it via anchor skills (`_TAB_ANCHORS` →
`_CLASS_PAGE_TO_TAB`), never `page-1`. Warlock’s tabs aren’t anchored yet, so its
+tab bonuses are deliberately not attributed.

## When adding/decoding a stat

- Bit widths come from `itemstatcost.txt` ("Save Bits", "Save Param Bits").
- Param-bearing stats (`p=`) have handlers in `_fmt_stat`, keyed by a sentinel
  label in `_STAT_LABEL`. Add a sentinel + handler for new ones.
- Stats with empty `descfunc` are hidden in-game → collect in `_HIDDEN_STATS`
  and suppress (return `""`).
- Skill ids resolve via the loaded `skills` table; class/affix/unique/set names
  via their respective tables. Unknown ids should fall back to a visible
  placeholder (`Skill(id=N)`) — keep bad parses visible (rule 2).

## Verifying changes

There is no automated test suite. Validate by running the parser against real
saves and checking `items.md`:

```bash
python Scripts/d2i_parser.py
```

The bar is **0 desyncs** (no `scanning forward`, no `[SKIP]`) and parsed
item/quantity counts matching the in-game panels. When debugging a desync,
count bits for the offending item; compact-item fixes belong in the parse
`simple` branch relying on the caller's `align()`.
