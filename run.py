#!/usr/bin/env python3
"""hapkit: python3 run.py [--host 127.0.0.1] [--port 8765] [--project projects/x.hapkit.json]"""
import argparse

from hapkit.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="hapkit haptic waveform workbench")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--project")
    a = ap.parse_args()
    serve(a.host, a.port, a.project)
