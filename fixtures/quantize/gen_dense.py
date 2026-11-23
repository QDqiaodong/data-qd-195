"""Regenerate fixtures/quantize/dense.json (synthetic, deterministic).

Three rows of tanks: X00..X19, Y00..Y39, Z00..Z19, capacity 1 L each.  Every
X tank opens with 1 L carrying 100000001 g of salt; Y and Z open empty.  The
ledger quantum is 1 g.  Six rounds pump the full row into the Y row and then
into the other row (0.025 L per transfer, 800 transfers per step); the full
row alternates X, Z, X, Z, X, Z.  Each round's second step pins the two
transfers Y00->dest00 and Y01->dest01 to 2500001 units.

Run: python3 fixtures/quantize/gen_dense.py
"""
import json
from pathlib import Path

ROUNDS = 6
OUT = Path(__file__).resolve().with_name("dense.json")


def tank(tid, volume, mass):
    return {"id": tid, "capacity_l": "1", "volume_l": volume,
            "mass_g": {"salt": mass}}


def build():
    tanks = ([tank(f"X{i:02d}", "1", "100000001") for i in range(20)]
             + [tank(f"Y{j:02d}", "0", "0") for j in range(40)]
             + [tank(f"Z{i:02d}", "0", "0") for i in range(20)])
    steps = []
    pins = []
    for rnd in range(1, ROUNDS + 1):
        full, dest = ("X", "Z") if rnd % 2 == 1 else ("Z", "X")
        first = {"id": f"r{rnd}s1", "transfers": [
            {"id": f"{full}{i:02d}_Y{j:02d}", "source": f"{full}{i:02d}",
             "target": f"Y{j:02d}", "volume_l": "0.025"}
            for i in range(20) for j in range(40)]}
        second = {"id": f"r{rnd}s2", "transfers": [
            {"id": f"Y{j:02d}_{dest}{i:02d}", "source": f"Y{j:02d}",
             "target": f"{dest}{i:02d}", "volume_l": "0.025"}
            for j in range(40) for i in range(20)]}
        steps.extend([first, second])
        for j in (0, 1):
            pins.append({"kind": "transfer",
                         "item": f"Y{j:02d}_{dest}{j:02d}",
                         "step": second["id"], "units": 2500001})
    return {"plan": {"species": ["salt"], "tanks": tanks, "steps": steps},
            "species": "salt", "quantum_g": "1", "pins": pins}


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
