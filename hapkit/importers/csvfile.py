"""Accelerometer logs as CSV: optional time column, one or more axis columns."""
from __future__ import annotations

import csv
import io

import numpy as np

TIME_NAMES = ("t", "time", "timestamp", "time_s", "time_ms", "time_us", "seconds", "ms", "us", "ns")


def read_csv(data: bytes, options: dict) -> tuple[np.ndarray, float, dict]:
    """Return (signal, fs, meta). options: time (column name|index|'none'), time_unit (s|ms|us|ns|auto),
    fs (when there is no time column), axis (column name|index|'mag'|'auto'), detrend (bool, default true)."""
    text = data.decode("utf-8", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t ")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if r and any(c.strip() for c in r)]
    if not rows:
        raise ValueError("empty CSV")

    def numeric(r):
        try:
            [float(c) for c in r if c.strip()]
            return True
        except ValueError:
            return False

    header = None if numeric(rows[0]) else [c.strip() for c in rows[0]]
    body = rows[1:] if header else rows
    width = min(len(r) for r in body)
    arr = np.array([[float(c) for c in r[:width]] for r in body if numeric(r[:width])], dtype=np.float64)
    names = (header or [f"c{i}" for i in range(width)])[:width]

    def col(spec):
        if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
            return int(spec)
        low = [n.lower() for n in names]
        if spec.lower() in low:
            return low.index(spec.lower())
        raise ValueError(f"no column {spec!r} in {names}")

    tspec = options.get("time", "auto")
    tcol = None
    if tspec == "auto":
        tcol = next((i for i, n in enumerate(names) if n.lower() in TIME_NAMES), None)
    elif tspec not in ("none", None):
        tcol = col(tspec)
    meta = {"columns": names, "rows": len(arr)}
    if tcol is not None:
        t = arr[:, tcol]
        unit = options.get("time_unit", "auto")
        if unit == "auto":
            name = names[tcol].lower()
            unit = next((u for u in ("ns", "us", "ms") if name.endswith(u)), None) or (
                "ns" if np.median(np.diff(t)) > 1e3 else "us" if np.median(np.diff(t)) > 1 else "s")
        t = t * {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}[unit]
        dt = np.diff(t)
        if np.any(dt <= 0):
            order = np.argsort(t, kind="stable")
            arr, t = arr[order], t[order]
            keep = np.concatenate([[True], np.diff(t) > 0])
            arr, t = arr[keep], t[keep]
        fs = 1.0 / float(np.median(np.diff(t)))
        meta.update(time_column=names[tcol], time_unit=unit, jitter=round(float(np.std(np.diff(t)) * fs), 4))
    else:
        if not options.get("fs"):
            raise ValueError("CSV has no time column: set fs")
        fs = float(options["fs"])
        t = np.arange(len(arr)) / fs
    axes = [i for i in range(width) if i != tcol]
    aspec = options.get("axis", "auto")
    if aspec in ("mag", "magnitude"):
        x = np.linalg.norm(arr[:, axes] - arr[:, axes].mean(axis=0), axis=1)
        meta["axis"] = "magnitude"
    else:
        if aspec == "auto":
            var = [(np.var(arr[:, i]), i) for i in axes]
            ai = max(var)[1]
        else:
            ai = col(aspec)
        x = arr[:, ai].copy()
        meta["axis"] = names[ai]
    if tcol is not None and meta.get("jitter", 0) > 0.01:
        tu = np.arange(t[0], t[-1], 1.0 / fs)
        x = np.interp(tu, t, x)
        meta["resampled_uniform"] = True
    if options.get("detrend", True) and meta["axis"] != "magnitude":
        x = x - np.mean(x)
    meta["fs"] = round(fs, 3)
    return x, fs, meta
