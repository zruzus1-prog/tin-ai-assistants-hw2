"""Prepare traceable numerical records for analysis of TiN synthesis conditions."""

import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "conditions.csv"
UNITS = {
    "mL": ("mL", Decimal(1)),
    "mm": ("mm", Decimal(1)),
    "cm": ("mm", Decimal(10)),
    "nm": ("nm", Decimal(1)),
    "kHz": ("kHz", Decimal(1)),
    "mm/s": ("mm/s", Decimal(1)),
    "μJ": ("uJ", Decimal(1)),
    "mJ": ("uJ", Decimal(1)),
    "mV": ("mV", Decimal(1)),
    "min": ("min", Decimal(1)),
    "%": ("%", Decimal(1)),
}
GROUPS = {
    "A-07": "pulse_energy",
    "A-08": "pulse_energy",
    "F-02": "fragment_diameter",
    "F-06": "fragment_diameter",
}


def read_rows(path=INPUT):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def quantity(row):
    if row["status"] == "not_reported":
        return "missing", None, []
    if row["unit"] == "qualitative":
        return "text", None, []

    value = row["value"].replace("−", "-")
    if value.startswith("up to "):
        kind, parts = "upper_bound", [value.removeprefix("up to ")]
    elif value.startswith("~"):
        kind, parts = "approximate", [value[1:]]
    elif value.startswith("<"):
        kind, parts = "below", [value[1:]]
    elif "→" in value:
        kind, parts = "shift", value.split("→")
    elif "–" in value:
        kind, parts = "range", value.split("–")
    else:
        kind, parts = "exact", [value]
    unit, scale = UNITS[row["unit"]]
    return kind, unit, [format(Decimal(part) * scale, "f") for part in parts]


def prepare(rows):
    result = []
    for row in sorted(rows, key=lambda item: item["id"]):
        kind, unit, values = quantity(row)
        result.append({
            "id": row["id"],
            "process": row["process"],
            "medium": row["medium"],
            "parameter": row["parameter"],
            "source_value": row["value"],
            "source_unit": row["unit"],
            "page": int(row["page"]),
            "quote": row["quote"],
            "status": row["status"],
            "kind": kind,
            "unit": unit,
            "values": values,
            "conflict_group": GROUPS.get(row["id"]),
            "analysis_ready": (
                row["status"] == "reported"
                and bool(values)
                and "(hardware range)" not in row["parameter"]
            ),
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    args = parser.parse_args()
    print(json.dumps(prepare(read_rows(args.input)), indent=2))


if __name__ == "__main__":
    main()
