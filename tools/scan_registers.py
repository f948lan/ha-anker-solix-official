#!/usr/bin/env python3
"""Find Modbus registers on an Anker SOLIX device by watching them change.

Three sub-commands:

  snapshot  Read a block of registers and save them to a JSON file.
  diff      Compare two snapshots and list registers that changed,
            decoded as UINT16, INT16 and big-endian INT32.
  watch     Print a few registers live (INT32) so you can correlate them
            with a load you switch on and off.

Typical workflow for the EPS / backup port:

  1. Disable the Anker SOLIX Official integration in Home Assistant
     (Settings > Devices & services > ... > Disable), so only one Modbus
     client talks to the device.
  2. Unplug everything from the EPS port, wait ~30 s:
         python3 scan_registers.py snapshot --host 192.168.1.50 --out eps_off.json
  3. Plug a steady, known load into the EPS port (kettle, heater, lamp),
     wait ~30 s, and note the wattage the Anker app shows for it:
         python3 scan_registers.py snapshot --host 192.168.1.50 --out eps_on.json
  4. Compare, telling the tool roughly what the app showed:
         python3 scan_registers.py diff eps_off.json eps_on.json --expect 1200
  5. Confirm the best candidate live while toggling the load:
         python3 scan_registers.py watch --host 192.168.1.50 --addr 10233 --addr 10208

Requires: pip install "pymodbus>=3.10,<4"
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pymodbus.client import ModbusTcpClient

DEFAULT_INPUT_RANGES = "10000-10499,32768-32780"
DEFAULT_HOLDING_RANGES = "10060-10081,60000-60003"
DEFAULT_CONFIG = str(
    Path(__file__).resolve().parent.parent / "custom_components" / "anker_solix_official"
    / "config" / "58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml"
)
CHUNK = 40  # registers per request; the integration itself uses blocks up to ~67


# --------------------------------------------------------------------------- helpers
def parse_ranges(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            a, b = part.split("-", 1)
            out.append((int(a, 0), int(b, 0)))
        else:
            out.append((int(part, 0), int(part, 0)))
    return out


def unit_kwargs(func, unit: int) -> dict:
    """pymodbus renamed slave= to device_id= in 3.10; support both."""
    params = inspect.signature(func).parameters
    if "device_id" in params:
        return {"device_id": unit}
    if "slave" in params:
        return {"slave": unit}
    return {}


def read_block(client, kind: str, start: int, count: int, unit: int):
    func = client.read_input_registers if kind == "input" else client.read_holding_registers
    try:
        rr = func(address=start, count=count, **unit_kwargs(func, unit))
    except Exception:
        return None
    if rr.isError() or len(getattr(rr, "registers", [])) != count:
        return None
    return list(rr.registers)


def read_range(client, kind: str, start: int, end: int, unit: int) -> dict[int, int | None]:
    """Read [start, end] in chunks; fall back to single reads where a chunk fails."""
    values: dict[int, int | None] = {}
    addr = start
    while addr <= end:
        n = min(CHUNK, end - addr + 1)
        regs = read_block(client, kind, addr, n, unit)
        if regs is not None:
            for i, v in enumerate(regs):
                values[addr + i] = v
        else:
            for a in range(addr, addr + n):
                one = read_block(client, kind, a, 1, unit)
                values[a] = one[0] if one else None
        addr += n
    return values


def known_labels(path: str | None) -> dict[int, str]:
    """Map address -> entity key from an integration device YAML (optional)."""
    if not path:
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        cfg = yaml.safe_load(open(path, encoding="utf-8"))
    except OSError:
        return {}
    labels: dict[int, str] = {}

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if isinstance(val, dict) and "address" in val:
                    addr = int(str(val["address"]), 0)
                    labels.setdefault(addr, key)
                else:
                    walk(val)

    walk(cfg)
    return labels


def i16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def i32(hi: int, lo: int) -> int:
    u = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
    return u - 0x1_0000_0000 if u & 0x8000_0000 else u


def connect(host: str, port: int) -> ModbusTcpClient:
    client = ModbusTcpClient(host, port=port, timeout=3, retries=1)
    if not client.connect():
        sys.exit(f"Could not connect to {host}:{port}. Is Modbus TCP enabled in the Anker app?")
    return client


# --------------------------------------------------------------------------- commands
def cmd_snapshot(args) -> None:
    client = connect(args.host, args.port)
    snap = {
        "host": args.host,
        "taken_at": datetime.now(UTC).isoformat(),
        "note": args.note,
        "input": {},
        "holding": {},
    }
    try:
        for kind, spec in (("input", args.input), ("holding", args.holding)):
            for start, end in parse_ranges(spec):
                print(f"Reading {kind} {start}-{end} ...", file=sys.stderr)
                for a, v in read_range(client, kind, start, end, args.unit).items():
                    snap[kind][str(a)] = v
    finally:
        client.close()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=1)
    readable = sum(v is not None for k in ("input", "holding") for v in snap[k].values())
    print(f"Saved {readable} readable registers to {args.out}")


def cmd_diff(args) -> None:
    a = json.load(open(args.before, encoding="utf-8"))
    b = json.load(open(args.after, encoding="utf-8"))

    rows = []
    for kind in ("input", "holding"):
        ra = {int(k): v for k, v in a.get(kind, {}).items()}
        rb = {int(k): v for k, v in b.get(kind, {}).items()}
        for addr in sorted(set(ra) & set(rb)):
            va, vb = ra[addr], rb[addr]
            if va is None or vb is None or va == vb:
                continue
            row = {
                "kind": kind,
                "addr": addr,
                "u16": (va, vb),
                "i16": (i16(va), i16(vb)),
                "i32": None,
            }
            # INT32 starting here (this register = high word)
            if ra.get(addr + 1) is not None and rb.get(addr + 1) is not None:
                row["i32"] = (i32(va, ra[addr + 1]), i32(vb, rb[addr + 1]))
            rows.append(row)

    # Also catch INT32 pairs where only the low word changed (small values)
    seen = {(r["kind"], r["addr"]) for r in rows}
    for kind in ("input", "holding"):
        ra = {int(k): v for k, v in a.get(kind, {}).items()}
        rb = {int(k): v for k, v in b.get(kind, {}).items()}
        for addr in sorted(ra):
            lo = addr + 1
            if (kind, addr) in seen or (kind, lo) not in seen:
                continue
            if None in (ra.get(addr), rb.get(addr), ra.get(lo), rb.get(lo)):
                continue
            rows.append({
                "kind": kind, "addr": addr,
                "u16": (ra[addr], rb[addr]), "i16": (i16(ra[addr]), i16(rb[addr])),
                "i32": (i32(ra[addr], ra[lo]), i32(rb[addr], rb[lo])),
            })

    def score(r):
        if r["i32"] is None:
            d = abs(r["i16"][1] - r["i16"][0])
        else:
            d = abs(r["i32"][1] - r["i32"][0])
        if args.expect:
            return abs(d - args.expect)  # closest to the expected watt change first
        return -d

    rows = [r for r in rows if r["i32"] is None or abs(r["i32"][1] - r["i32"][0]) >= args.min_delta
            or abs(r["i16"][1] - r["i16"][0]) >= args.min_delta]
    rows.sort(key=score)

    if not rows:
        print("No registers changed by at least --min-delta. Was the load actually drawing power?")
        return

    labels = known_labels(args.config)
    print(f"{'type':8}{'addr':>7}  {'INT32 before → after (Δ)':34}{'INT16 before → after':24}already mapped as")
    print("-" * 100)
    for r in rows[: args.top]:
        if r["i32"]:
            x, y = r["i32"]
            s32 = f"{x} → {y} ({y - x:+d})"
        else:
            s32 = "-"
        s16 = f"{r['i16'][0]} → {r['i16'][1]}"
        print(f"{r['kind']:8}{r['addr']:>7}  {s32:34}{s16:24}{labels.get(r['addr'], '')}")
    if args.expect:
        print(f"\nSorted by how close the change is to --expect {args.expect} W. "
              "Power registers in the known map are INT32, high word first.")
    print("Tip: confirm a candidate with `watch` while switching the load on and off.")


def cmd_watch(args) -> None:
    client = connect(args.host, args.port)
    addrs = args.addr
    print("time      " + "".join(f"{a:>10}" for a in addrs))
    try:
        while True:
            vals = []
            for a in addrs:
                regs = read_block(client, args.type, a, 2, args.unit)
                vals.append("err" if regs is None else str(i32(*regs)))
            print(time.strftime("%H:%M:%S  ") + "".join(f"{v:>10}" for v in vals), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


# --------------------------------------------------------------------------- CLI
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def conn(sp):
        sp.add_argument("--host", required=True, help="Device IP shown in the Anker app")
        sp.add_argument("--port", type=int, default=502)
        sp.add_argument("--unit", type=int, default=1, help="Modbus unit/device id (default 1)")

    s = sub.add_parser("snapshot", help="save a register snapshot")
    conn(s)
    s.add_argument("--out", required=True)
    s.add_argument("--input", default=DEFAULT_INPUT_RANGES, help=f"default {DEFAULT_INPUT_RANGES}")
    s.add_argument("--holding", default=DEFAULT_HOLDING_RANGES, help=f"default {DEFAULT_HOLDING_RANGES}")
    s.add_argument("--note", default="", help="free text stored in the file, e.g. 'kettle on EPS'")
    s.set_defaults(func=cmd_snapshot)

    d = sub.add_parser("diff", help="compare two snapshots")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--expect", type=int, default=0, help="watts the app showed on the EPS port")
    d.add_argument("--min-delta", type=int, default=10)
    d.add_argument("--top", type=int, default=30)
    d.add_argument("--config", default=DEFAULT_CONFIG,
                   help="integration device YAML used to label known registers (needs PyYAML)")
    d.set_defaults(func=cmd_diff)

    w = sub.add_parser("watch", help="print INT32 registers live")
    conn(w)
    w.add_argument("--addr", type=int, action="append", required=True)
    w.add_argument("--type", choices=("input", "holding"), default="input")
    w.add_argument("--interval", type=float, default=2.0)
    w.set_defaults(func=cmd_watch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
