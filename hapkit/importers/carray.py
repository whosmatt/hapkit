"""int8 PCM arrays from C/C++ sources (QTI libqtivibratoreffect / LineageOS device effect.cpp style)."""
from __future__ import annotations

import re

from ..core import ids

SIGNED_TYPES = ("int8_t", "signed char", "s8", "__s8")
_ARRAY_RE = re.compile(
    r"(?:static\s+)?(?:const\s+)?(?:static\s+)?(?P<type>int8_t|uint8_t|signed\s+char|unsigned\s+char|char|s8|u8|__s8)"
    r"\s+(?:const\s+)?(?P<name>[A-Za-z_]\w*)\s*\[\s*[^\]]*\]\s*=\s*\{(?P<body>[^{}]*)\}\s*;", re.S)
_TABLE_RE = re.compile(r"struct\s+effect_stream\s+(?P<name>[A-Za-z_]\w*)\s*\[\s*[^\]]*\]\s*=\s*\{(?P<body>.*?)\}\s*;",
                       re.S)
_ENTRY_RE = re.compile(r"\{(?P<body>[^{}]*)\}", re.S)


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _defines(src: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"^\s*#\s*define\s+(\w+)\s+([^\n]+)$", src, re.M)}


def _value(tok: str, defines: dict[str, str]) -> int:
    tok = tok.strip()
    for _ in range(8):
        if tok in defines:
            tok = defines[tok].strip()
    tok = re.sub(r"^\(\s*(?:int8_t|uint8_t|signed\s+char|char|int)\s*\)", "", tok).strip()
    tok = tok.strip("()").strip()
    neg = tok.startswith("-")
    tok = tok.lstrip("+-").strip()
    v = int(tok.rstrip("uUlL"), 0)
    return -v if neg else v


def parse_arrays(src: str) -> list[dict]:
    clean = strip_comments(src)
    defines = _defines(clean)
    out = []
    for m in _ARRAY_RE.finditer(clean):
        toks = [t for t in m.group("body").replace("\n", " ").split(",") if t.strip()]
        try:
            vals = [_value(t, defines) for t in toks]
        except ValueError:
            continue
        typ = " ".join(m.group("type").split())
        signed = typ in SIGNED_TYPES or (typ == "char" and min(vals, default=0) < 0)
        if signed:
            vals = [v - 256 if v > 127 else v for v in vals]
        out.append({"name": m.group("name"), "type": typ, "signed": signed, "values": vals,
                    "line": clean.count("\n", 0, m.start()) + 1})
    return out


def parse_tables(src: str) -> dict[str, dict]:
    """Map array name -> {effect_id, rate, table, kind} from struct effect_stream tables."""
    clean = strip_comments(src)
    defines = _defines(clean)
    found: dict[str, dict] = {}
    for t in _TABLE_RE.finditer(clean):
        table = t.group("name")
        for e in _ENTRY_RE.finditer(t.group("body")):
            body = e.group("body")
            fields = {}
            if "." in body and "=" in body:
                for k, v in re.findall(r"\.(\w+)\s*=\s*([^,]+)", body):
                    fields[k] = v.strip()
            else:
                parts = [p.strip() for p in body.split(",") if p.strip()]
                if len(parts) >= 4:
                    fields = dict(zip(("effect_id", "length", "play_rate_hz", "data"), parts))
            data = fields.get("data", "").strip().lstrip("&").split("[")[0].strip()
            if not data:
                continue
            rec = {"table": table}
            for key, name in (("effect_id", "effect_id"), ("play_rate_hz", "rate")):
                if key in fields:
                    try:
                        rec[name] = _effect_id(fields[key], defines) if key == "effect_id" else _value(fields[key], defines)
                    except ValueError:
                        rec[name] = None
            eid = rec.get("effect_id")
            prim = "primitive" in table.lower() or (isinstance(eid, int) and eid & 0x8000)
            rec["kind"] = "primitive" if prim else "effect"
            if isinstance(eid, int):
                rec["aidl"] = eid & 0x7FFF
                rec["label"] = ids.name_of(rec["kind"], rec["aidl"])
            found[data] = rec
    return found


def _effect_id(expr: str, defines: dict[str, str]) -> int:
    total = 0
    for part in expr.split("|"):
        part = part.strip()
        m = re.search(r"(?:Effect|CompositePrimitive)::(\w+)|^(?:EFFECT|PRIMITIVE)_(\w+)$", part)
        if m:
            name = m.group(1) or m.group(2)
            if name in ids.EFFECTS and "Primitive" not in part and not part.startswith("PRIMITIVE_"):
                total |= ids.EFFECTS[name]
            elif name in ids.PRIMITIVES:
                total |= ids.PRIMITIVES[name]
            else:
                raise ValueError(part)
        else:
            total |= _value(part, defines)
    return total


def extract(src: str, default_rate: int = 24000) -> list[dict]:
    """Signed int8 arrays with their effect_stream metadata (unsigned arrays are skipped: not PCM)."""
    tables = parse_tables(src)
    out = []
    for a in parse_arrays(src):
        if not a["signed"]:
            continue
        meta = tables.get(a["name"], {})
        out.append({"name": a["name"], "values": a["values"], "fs": meta.get("rate") or default_rate,
                    "line": a["line"], **{k: meta[k] for k in ("kind", "aidl", "label", "table", "effect_id")
                                          if k in meta}})
    return out
