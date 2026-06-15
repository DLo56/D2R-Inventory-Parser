"""
casc_extract.py — Extract game-data files directly from a D2R install's CASC storage.

WHY THIS EXISTS
---------------
Vanilla D2R keeps adding official content (new classes like the Warlock, new
skills, new runewords like Authority).  The parser's name/stat resolution is only
as current as its game-data tables, so those tables MUST be refreshed from the
player's actual install — never from anything online (online copies go stale).

This tool reads ONLY the local install directory (no network) and pulls the
data\global\excel\*.txt files out of CASC so the parser can use current data.

USAGE
-----
    python casc_extract.py [INSTALL_DIR] [OUT_DIR]

    INSTALL_DIR  default: C:\\Program Files (x86)\\Diablo II Resurrected
    OUT_DIR      default: <this script dir>/gamedata_d2r/excel

It uses only the Python standard library (struct, zlib, hashlib).

CASC PIPELINE (all local)
-------------------------
  .build.info                 -> active Build Key
  Data/config/<buildkey>      -> build config: encoding CKey/EKey, vfs-root EKey
  Data/data/*.idx             -> EKey(9) -> (archive#, offset, size)   [_load_indices]
  Data/data/data.NNN          -> 30-byte blob header + BLTE payload     [_read_ekey]
  BLTE                        -> zlib/raw chunk decode                  [_blte_decode]
  vfs-root (TVFS manifest)    -> file path -> content EKey              [_parse_tvfs]
"""

import os
import struct
import sys
import zlib
from pathlib import Path


# ---------------------------------------------------------------------------
# BLTE — Blizzard's chunked container around every stored file
# ---------------------------------------------------------------------------
def _blte_decode(data: bytes) -> bytes:
    if data[:4] != b"BLTE":
        raise ValueError("not BLTE data")
    header_size = struct.unpack_from(">I", data, 4)[0]
    out = bytearray()
    if header_size == 0:
        # single chunk spanning the rest of the buffer
        chunks = [(len(data) - 8, None)]
        pos = 8
    else:
        flags, count_hi, count_lo = struct.unpack_from(">BBH", data, 8)
        chunk_count = (count_hi << 16) | count_lo
        chunks = []
        p = 12
        for _ in range(chunk_count):
            comp_size, decomp_size = struct.unpack_from(">II", data, p)
            p += 8 + 16            # skip 16-byte chunk checksum
            chunks.append((comp_size, decomp_size))
        pos = header_size
    for comp_size, _ in chunks:
        mode = data[pos:pos + 1]
        chunk = data[pos + 1:pos + comp_size]
        if mode == b"N":           # not compressed
            out += chunk
        elif mode == b"Z":         # zlib
            out += zlib.decompress(chunk)
        elif mode == b"E":         # encrypted — game-data txt is never encrypted
            raise ValueError("encrypted BLTE chunk (unexpected for excel data)")
        else:
            raise ValueError(f"unknown BLTE chunk mode {mode!r}")
        pos += comp_size
    return bytes(out)


# ---------------------------------------------------------------------------
# Local indices (.idx): EKey(first 9 bytes) -> (archive index, offset, size)
# ---------------------------------------------------------------------------
def _load_indices(data_dir: Path) -> dict:
    index: dict = {}
    # For each bucket keep only the highest-numbered .idx (latest version).
    latest: dict = {}
    for name in os.listdir(data_dir):
        if not name.endswith(".idx"):
            continue
        bucket = name[:2]
        if bucket not in latest or name > latest[bucket]:
            latest[bucket] = name
    for name in latest.values():
        raw = (data_dir / name).read_bytes()
        hdr_len = struct.unpack_from("<I", raw, 0)[0]
        _ver, _bucket, _x, size_b, ofs_b, key_b, seg_bits = struct.unpack_from(
            "<HBBBBBB", raw, 8)
        ent_ofs = (8 + hdr_len + 0x0F) & ~0x0F
        entries_len = struct.unpack_from("<I", raw, ent_ofs)[0]
        ent_ofs += 8
        entry_size = key_b + ofs_b + size_b
        seg_mask = (1 << seg_bits) - 1
        for p in range(ent_ofs, ent_ofs + entries_len, entry_size):
            ekey = raw[p:p + key_b]
            packed = int.from_bytes(raw[p + key_b:p + key_b + ofs_b], "big")
            archive = packed >> seg_bits
            offset = packed & seg_mask
            size = struct.unpack_from("<I", raw, p + key_b + ofs_b)[0]
            if ekey not in index:
                index[ekey] = (archive, offset, size)
    return index


def _read_ekey(data_dir: Path, index: dict, ekey: bytes) -> bytes:
    """Read + BLTE-decode the content stored under the given EKey."""
    key9 = ekey[:9]
    if key9 not in index:
        raise KeyError(f"EKey {ekey.hex()} not in local indices")
    archive, offset, size = index[key9]
    with open(data_dir / f"data.{archive:03d}", "rb") as fh:
        fh.seek(offset)
        blob = fh.read(size)
    # 30-byte blob header (reversed EKey[16] + size[4] + flags[2] + checksums[8])
    return _blte_decode(blob[30:])


# ---------------------------------------------------------------------------
# Build config
# ---------------------------------------------------------------------------
def _read_build_config(install: Path) -> dict:
    info = (install / ".build.info").read_text(encoding="utf-8").splitlines()
    headers = info[0].split("|")
    bk_col = next(i for i, h in enumerate(headers) if h.startswith("Build Key"))
    build_key = info[1].split("|")[bk_col]
    cfg_path = install / "Data" / "config" / build_key[:2] / build_key[2:4] / build_key
    cfg = {}
    for line in cfg_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


# ---------------------------------------------------------------------------
# TVFS — the manifest mapping file paths to content EKeys.
# Path table is a radix trie: name fragments concatenate down the tree; a
# 0xFF marker carries an int32 BE whose top bit means "folder (subtree of
# size N)" and otherwise means "file (offset into the VFS span table)".
# ---------------------------------------------------------------------------
def _tvfs_header(t: bytes) -> dict:
    flags, pto, pts, vto, vts, cto, cts = struct.unpack_from(">iIIIIII", t, 8)
    ekey_size = t[6]
    cft_off_size = 1
    while (1 << (8 * cft_off_size)) <= cts:
        cft_off_size += 1
    return dict(ekey_size=ekey_size, cft_off_size=cft_off_size,
                pt=(pto, pts), vfs=(vto, vts), cft=(cto, cts))


def _tvfs_cft_ekey(t: bytes, h: dict, vfs_offset: int) -> bytes:
    """Resolve a VFS-table offset to the content EKey via the CFT table."""
    vto, _ = h["vfs"]
    cto, _ = h["cft"]
    p = vto + vfs_offset
    _num_spans = t[p]; p += 1                      # first span is enough for whole files
    # span = contentOffset(4 BE) + contentSize(4 BE) + cftOffset(cft_off_size BE)
    p += 8
    cft_offset = int.from_bytes(t[p:p + h["cft_off_size"]], "big")
    return t[cto + cft_offset: cto + cft_offset + h["ekey_size"]]


def _parse_tvfs(t: bytes) -> dict:
    """Return {path(str): content EKey(bytes)} for every file in this manifest."""
    h = _tvfs_header(t)
    pt_off, pt_size = h["pt"]
    paths: dict = {}                                # path -> vfs span-table offset

    def walk(start: int, end: int, prefix: bytes) -> None:
        pos = start
        label = b""
        while pos < end:
            b = t[pos]
            if b == 0xFF:
                val = struct.unpack_from(">I", t, pos + 1)[0]
                pos += 5
                if val & 0x80000000:               # folder: recurse into subtree
                    # The folder size counts the 4-byte value field too, so the
                    # actual child bytes are (size - 4).
                    size = (val & 0x7FFFFFFF) - 4
                    walk(pos, pos + size, prefix + label)
                    pos += size
                else:                               # file: prefix+label is the path
                    paths[(prefix + label).decode("latin-1")] = val
                label = b""
            elif b == 0:                            # path separator within a label
                label += b"\\"
                pos += 1
            else:                                   # name fragment
                label += t[pos + 1:pos + 1 + b]
                pos += 1 + b

    walk(pt_off, pt_off + pt_size, b"")
    return {p: _tvfs_cft_ekey(t, h, v) for p, v in paths.items()}


def extract_excel(install: Path, out_dir: Path) -> int:
    data_dir = install / "Data" / "data"
    cfg = _read_build_config(install)
    index = _load_indices(data_dir)
    # The big file manifest with all game data is vfs-2.
    vfs2 = _read_ekey(data_dir, index, bytes.fromhex(cfg["vfs-2"].split()[1]))
    files = _parse_tvfs(vfs2)
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for path, ekey in files.items():
        low = path.lower().replace("/", "\\")
        # Active excel tables live at data\global\excel\<name>.txt — skip the
        # \base\ copies (the base game's pre-override versions).
        if not (low.startswith("data\\global\\excel\\") and low.endswith(".txt")):
            continue
        rest = low[len("data\\global\\excel\\"):]
        if "\\" in rest:                            # in a subfolder (e.g. base\)
            continue
        content = _read_ekey(data_dir, index, ekey)
        (out_dir / Path(path).name).write_bytes(content)
        count += 1
    return count


def extract_strings(install: Path, out_dir: Path) -> int:
    """Extract the English localization string tables (string/expansion/patch
    .tbl) used to resolve internal keys to display names (e.g. Doomspittle ->
    Doomslinger, Wraithra -> Wraith)."""
    data_dir = install / "Data" / "data"
    cfg = _read_build_config(install)
    index = _load_indices(data_dir)
    vfs2 = _read_ekey(data_dir, index, bytes.fromhex(cfg["vfs-2"].split()[1]))
    files = _parse_tvfs(vfs2)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = ("string.tbl", "expansionstring.tbl", "patchstring.tbl")
    count = 0
    for name in wanted:
        path = f"data\\local\\lng\\eng\\{name}"
        if path in files:
            (out_dir / name).write_bytes(_read_ekey(data_dir, index, files[path]))
            count += 1
    return count


_SCRIPT_DIR = Path(__file__).parent
_DEFAULT_INSTALL = Path(r"C:\Program Files (x86)\Diablo II Resurrected")
_EXCEL_OUT = _SCRIPT_DIR / "gamedata_d2r" / "excel"
_STRINGS_OUT = _SCRIPT_DIR / "_strings"


def main() -> None:
    install = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_INSTALL
    cfg = _read_build_config(install)
    print(f"install: {install}")
    print(f"build:   {cfg.get('build-name')}")
    n_excel = extract_excel(install, _EXCEL_OUT)
    print(f"extracted {n_excel} excel/*.txt -> {_EXCEL_OUT}")
    n_str = extract_strings(install, _STRINGS_OUT)
    print(f"extracted {n_str} string .tbl -> {_STRINGS_OUT}")
    print("done — both the parser's game tables and string names are now current.")


if __name__ == "__main__":
    main()
