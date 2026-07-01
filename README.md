# D2R Save Parser

A pure-Python parser for **Diablo II: Resurrected** save files. It reads your
shared stash (`*.d2i`) and character (`*.d2s`) files and produces a single,
human-readable Markdown report — `items.md` — listing every item, its mods,
sockets, runewords, quantities, and level requirements, grouped the way the
game's panels are (Equipped / Inventory / Belt / Cube / Personal Stash).
The report opens with a **Gems** matrix (gem type across columns, quality down
the rows, quantity in each cell) and a **Runes** count table, tallied from the
shared stash's *dedicated* gem/rune pages (a stash tab is treated as a stone page
when gems/runes make up most of its loose contents; socketed stones and stray
gems in gear tabs are excluded).

For each character it also reports **Attributes** (Strength, Dexterity,
Vitality, Energy, unspent points), the **Skills** with allocated hard points,
and a **Gear Bonuses** table of aggregate "page 2" stats (Magic Find, Faster
Cast Rate, resistances, life/mana steal, etc.). The Attributes and Skills tables
include `Base`, `From Items`, and `Total` columns — `From Items` sums the bonuses
the game counts toward the character sheet (the primary equipped set plus charms
carried in the inventory; the alternate weapon-swap set is excluded).
The main purpose is to provide a full list of items accross save files to
AI agents for analysis and decision-making.

> This targets **vanilla / official D2R**. Vanilla D2R keeps adding official
> content over patches (new classes, skills, runewords). If something shows up
> as `Skill(id=N)`, `Class(N)`, or an unnamed item, it> means the bundled 
> game-data tables are older than your install. Refresh them with the CASC
> extractor (see below). This project is not a modding tool.

## Requirements

- **Python 3.8+** (standard library only — no pip dependencies).
- A local install of Diablo II: Resurrected (only needed to refresh game data).

## Quick start

```bash
# From the repo root
python Scripts/d2i_parser.py
```

This scans your D2R save folder, parses all `*.d2s` and `*.d2i` files, and
writes a single combined report to `items.md` in the repo root.

### Save-file location

The save directory is currently **hardcoded** in `Scripts/d2i_parser.py`
(`main()`):

```python
stash_dir = Path(r"C:\Users\<you>\Saved Games\Diablo II Resurrected")
```

If you cloned this from GitHub, **edit that path** to point at your own D2R
save folder. Typical locations:

| OS        | Path                                                      |
|-----------|-----------------------------------------------------------|
| Windows   | `%USERPROFILE%\Saved Games\Diablo II Resurrected`         |
| macOS     | `~/Library/Application Support/Diablo II Resurrected`      |

### Options

| Flag                   | Effect                                                       |
|------------------------|-------------------------------------------------------------|
| `--base-type-columns`  | Add Base Type / Code columns to every item table.           |
| `--belts`              | Include each character's Belt panel.                         |
| `--cubes`              | Include each character's Horadric Cube panel.               |

```bash
python Scripts/d2i_parser.py --belts --cubes
```

## Refreshing game data (after a D2R patch)

The parser resolves item names, stats, skills, runewords, etc. from game-data
tables bundled under `Scripts/gamedata_d2r/excel/` (91 `.txt` tables) and
string tables under `Scripts/_strings/`. These come **straight from your local
D2R install** via a self-contained CASC extractor (stdlib only — no online
sources, which go stale):

```bash
python Scripts/casc_extract.py
# optional: pass a custom install path
python Scripts/casc_extract.py "D:\Games\Diablo II Resurrected"
```

Default install path: `C:\Program Files (x86)\Diablo II Resurrected`.

Re-run this after every D2R patch so newly added content resolves correctly.

## Output

A single `items.md` at the repo root (one unified report for **all** stashes +
characters — never per-stash files). Bad or partial parses are **kept and
flagged** in the output (`⚠NonStdFlags`, `[PARTIAL]`) rather than skipped, so
you can see exactly what didn't decode.

## Project layout

```
diablo/
├── Scripts/
│   ├── d2i_parser.py        # main parser  →  items.md
│   ├── casc_extract.py      # refresh game data from your D2R install
│   ├── gamedata_d2r/excel/  # 91 game-data .txt tables (regenerated)
│   └── _strings/            # localization .tbl string tables (regenerated)
├── items.md                 # generated report
├── README.md
└── AGENTS.md                # instructions for AI coding agents
```

## Notes / known gaps

- Socketed rune/gem "when socketed in X" modifiers are computed by the game, not
  stored in the save, so they aren't listed in an item's mod string. They **are**
  folded into the character `From Items` / `Total` columns (for the four core
  attributes) via gems.txt.
- Magic/rare affix-based level requirements are approximate and can read high.
- Attribute/skill `Base` values come from the save; `From Items` is computed
  from the items the game counts (active equipped set + inventory charms,
  excluding the weapon-swap set). It gates **set partial bonuses** by how many
  pieces of that set are equipped (a lone set piece's "green" bonuses don't
  count) and adds rune/gem socket mods. Charges and granted "oskills" are not
  counted toward skill levels. Full-set (complete-set) bonuses and set bonuses
  with item-specific (non-count) activation are not separately modeled.
- The Attributes table shows the four primary attributes (Str/Dex/Vit/Energy)
  plus unspent points. Max Life/Mana/Stamina and Level are intentionally omitted.
- The **Gear Bonuses** table is summed from gear only (active set + inventory
  charms, gated set bonuses, with socket mods). **Passive skills and auras**
  (e.g. Barbarian Natural Resistance / Increased Speed, Paladin resist auras)
  also feed these stats in-game but are NOT included. Resistances are shown as
  gear totals — the game subtracts the difficulty penalty (Nightmare −40, Hell
  −100) and caps at the max resist (75% default); neither is applied here.
  Defense / Attack Rating / weapon Damage need full character formulas and are
  not computed.
- Warlock (`war`) `+to skill tab` bonuses are not yet attributed (its screen-tab
  layout isn't mapped); +all/+class/+single-skill bonuses still apply normally.

See `AGENTS.md` for deeper format details and contributor guidance.
