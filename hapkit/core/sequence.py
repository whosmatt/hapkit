"""AW8693x sequencer model: 8 slots of RAM wave index (with loop count) or wait (0x80 | n)."""
from __future__ import annotations

from .render import qround, render_wave

SLOTS = 8
WAIT_FLAG = 0x80
LOOP_INFINITE = 0x0F


def wait_slot(ms: float, device: dict) -> tuple[int, float]:
    fs, unit = int(device["fs"]), int(device.get("wait_unit", 64))
    n = max(1, min(127, qround(float(ms) * fs / 1000.0 / unit)))
    return WAIT_FLAG | n, n * unit * 1000.0 / fs


def plays(loops: int, device: dict) -> int:
    return max(1, int(loops) + int(device.get("loop_extra_plays", 1)))


def ram_index(project: dict, wave_id: str) -> int:
    order = project.get("ram", {}).get("waves", [])
    if wave_id not in order:
        raise ValueError(f"wave {wave_id!r} is not in RAM")
    return order.index(wave_id) + 1


def seq_registers(seq: dict, project: dict) -> dict:
    device = project["device"]
    slots = seq.get("slots") or []
    if len(slots) > SLOTS:
        raise ValueError(f"{len(slots)} slots (max {SLOTS})")
    wavcfg, loops = [0] * SLOTS, [0] * SLOTS
    for i, slot in enumerate(slots):
        if slot.get("kind") == "wait":
            wavcfg[i] = wait_slot(slot.get("ms", 0), device)[0]
        else:
            wavcfg[i] = ram_index(project, slot["wave"])
            loops[i] = int(slot.get("loops", 0))
            if not 0 <= loops[i] < LOOP_INFINITE:
                raise ValueError(f"slot {i}: loops {loops[i]} out of range 0..14")
    mainloop = int(seq.get("mainloop", 0))
    if not 0 <= mainloop < LOOP_INFINITE:
        raise ValueError(f"mainloop {mainloop} out of range 0..14")
    return {"wavcfg": wavcfg, "loops": loops, "mainloop": mainloop,
            "wait_unit": int(device.get("wait_unit", 64)), "count": len(slots)}


def render_sequence(seq: dict, project: dict, cache: dict | None = None) -> dict:
    device = project["device"]
    fs = int(device["fs"])
    waves = {w["id"]: w for w in project.get("waves", [])}
    cache = {} if cache is None else cache
    regs = seq_registers(seq, project)
    out: list[int] = []
    slot_bounds, warnings = [], []
    total_us = 0
    for m in range(plays(regs["mainloop"], device)):
        for i, slot in enumerate(seq.get("slots") or []):
            start = len(out)
            if slot.get("kind") == "wait":
                reg, eff_ms = wait_slot(slot.get("ms", 0), device)
                n = (reg & ~WAIT_FLAG) * regs["wait_unit"]
                out.extend([0] * n)
                total_us += n * 1000000 // fs
                label = f"wait {eff_ms:.2f} ms"
            else:
                wid = slot["wave"]
                if wid not in waves:
                    raise ValueError(f"slot {i}: unknown wave {wid!r}")
                if wid not in cache:
                    cache[wid] = render_wave(waves[wid], device)
                r = cache[wid]
                p = plays(slot.get("loops", 0), device)
                for _ in range(p):
                    out.extend(r["samples"])
                total_us += p * (len(r["samples"]) * 1000000 // fs)
                warnings += [f"{wid}: {w}" for w in r["warnings"]]
                label = f"{wid}" + (f" x{p}" if p > 1 else "")
            slot_bounds.append({"start": start, "end": len(out), "label": label, "index": i, "pass": m,
                                "kind": slot.get("kind", "wave")})
    return {"fs": fs, "samples": out, "float": [s / 127.0 for s in out], "bounds": slot_bounds,
            "slot_bounds": slot_bounds, "clips": 0, "warnings": sorted(set(warnings)),
            "duration_ms": len(out) * 1000.0 / fs, "kernel_ms": -(-total_us // 1000), "registers": regs}


def effect_sequence(effect: dict, project: dict) -> dict | None:
    """The RAM sequence an effect plays: its own sequence, else a one-slot sequence of its single wave
    (`stream_wave`) when that wave is in RAM, else None."""
    sid = effect.get("sequence")
    if sid:
        return next((s for s in project.get("sequences", []) if s["id"] == sid), None)
    wid = effect.get("stream_wave")
    if wid and wid in (project.get("ram") or {}).get("waves", []):
        return {"id": f"_wave_{wid}", "name": "", "notes": "", "mainloop": 0,
                "slots": [{"kind": "wave", "wave": wid, "loops": 0}]}
    return None


def render_effect(effect: dict, project: dict, route: str = "ram", cache: dict | None = None) -> dict:
    device = project["device"]
    if route == "stream" and effect.get("stream_wave"):
        waves = {w["id"]: w for w in project.get("waves", [])}
        wid = effect["stream_wave"]
        if wid not in waves:
            raise ValueError(f"unknown stream wave {wid!r}")
        r = render_wave(waves[wid], device)
        r["duration_ms"] = len(r["samples"]) * 1000.0 / r["fs"]
        r["kernel_ms"] = None
    elif effect.get("sequence") or (route == "ram" and effect_sequence(effect, project)):
        seq = effect_sequence(effect, project)
        if seq is None:
            raise ValueError(f"unknown sequence {effect.get('sequence')!r}")
        r = render_sequence(seq, project, cache)
    elif effect.get("stream_wave"):
        raise ValueError(f"effect {effect.get('id')!r}: wave {effect['stream_wave']!r} is not in RAM")
    else:
        raise ValueError(f"effect {effect.get('id')!r} has no sequence/stream wave")
    scale = float(effect.get("scale", 1.0))
    if scale != 1.0:
        from .render import quantize
        r["float"] = [v * scale for v in r["float"]]
        r["samples"], r["clips"] = quantize(r["float"])
    if route == "stream":
        r["stream_ms"] = len(r["samples"]) * 1000 // int(device["fs"]) + 1
    return r
