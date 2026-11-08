"""Run the registered TiN verifier and paired challenge experiment."""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
import math
import random
from statistics import mean, stdev

from checks.contract import basic, CONTEXT, GROUPS, load_reference, strong
from src.prepare import INPUT, ROOT, prepare, read_rows


SEEDS = (11, 23, 47, 89, 131)
FAMILIES = ("scale", "qualifier", "citation", "context", "missing", "conflict", "inventory", "hardware")
CONTROLS = ("reorder", "decimal_equivalence")
PHASES = ("dry", "smoke", "sweep", "full")
T_CRITICAL = 2.776445105
MIN_DELTA = 0.30


def alternative(rows, family, rng):
    """Change exactly one aspect of a prepared table."""
    pack = deepcopy(rows)
    by_id = {r["id"]: r for r in pack}

    if family == "scale":
        ident, factor = rng.choice((
            ("A-08", "0.001"), ("A-08", "1000"),
            ("F-05", "0.1"), ("F-05", "10"),
        ))
        row = by_id[ident]
        row["values"] = [format(Decimal(v) * Decimal(factor), "f") for v in row["values"]]
    elif family == "qualifier":
        row = rng.choice([r for r in pack if r["kind"] in {
            "approximate", "upper_bound", "below", "range", "shift"
        }])
        row["kind"], row["values"] = "exact", row["values"][:1]
    elif family == "citation":
        row = rng.choice(pack)
        if rng.randrange(4) == 0:
            row["quote"] = "[missing from the article]"
        else:
            other = rng.choice([r for r in pack if (r["page"], r["quote"]) != (row["page"], row["quote"])])
            row["page"], row["quote"] = other["page"], other["quote"]
    elif family == "context":
        row = rng.choice(pack)
        other = rng.choice([r for r in pack if any(r[k] != row[k] for k in CONTEXT)])
        row.update({key: other[key] for key in CONTEXT})
    elif family == "missing":
        by_id["F-00"].update(
            status="reported", kind="exact", unit="uJ",
            values=[str(rng.randint(1, 100))], analysis_ready=True,
        )
    elif family == "conflict":
        row = rng.choice([r for r in pack if r["conflict_group"] is not None])
        row.update(status="reported", conflict_group=None, analysis_ready=True)
    elif family == "inventory":
        index = rng.randrange(len(pack))
        if rng.choice((False, True)):
            pack.append(dict(pack[index]))
        else:
            del pack[index]
    elif family == "hardware":
        by_id["A-04"]["analysis_ready"] = True
    elif family == "reorder":
        rng.shuffle(pack)
    elif family == "decimal_equivalence":
        row = rng.choice([r for r in pack if r["values"]])
        index = rng.randrange(len(row["values"]))
        value = row["values"][index]
        row["values"][index] = value + ("0" if "." in value else ".0")
    else:
        raise ValueError(f"Unknown challenge family: {family}")
    return pack


def trial(seed, rows, reference, pages, count):
    rng = random.Random(seed)
    found = {}
    for name in FAMILIES + CONTROLS:
        b0 = b1 = 0
        for _ in range(count):
            variant = alternative(rows, name, rng)
            b0 += bool(basic(variant, pages))
            b1 += bool(strong(variant, reference, pages))
        found[name] = {"cases": count, "b0_reject": b0, "b1_reject": b1}

    return {
        "seed": seed,
        "faults": len(FAMILIES) * count,
        "valid_controls": len(CONTROLS) * count,
        "b0_detected": sum(found[n]["b0_reject"] for n in FAMILIES),
        "b1_detected": sum(found[n]["b1_reject"] for n in FAMILIES),
        "b0_false_reject": sum(found[n]["b0_reject"] for n in CONTROLS),
        "b1_false_reject": sum(found[n]["b1_reject"] for n in CONTROLS),
        "families": found,
    }


def trial_passes(result):
    return (
        result["b1_detected"] == result["faults"]
        and result["b0_false_reject"] == result["b1_false_reject"] == 0
    )


def summarize(values):
    avg, spread = mean(values), stdev(values)
    half = T_CRITICAL * spread / math.sqrt(len(values))
    return {"mean": avg, "sample_sd": spread, "ci95": [avg - half, avg + half]}


def verify(phase):
    outcome = {"status": "PASS", "phase": phase, "stages": {}}
    stages = outcome["stages"]
    level = PHASES.index(phase)

    def fail(message, findings=()):
        outcome.update(status="FAIL", error=message, findings=list(findings))
        return outcome

    try:
        reference, pages = load_reference(ROOT)
        stages["dry"] = {"source_rows": len(reference), "source_pages": sorted(pages)}
        if level == 0:
            return outcome

        raw = read_rows(INPUT)
        rows = prepare(raw)
        errors = strong(rows, reference, pages)
        if errors:
            return fail("Smoke: export violates the data contract", errors)
        if rows != prepare(reversed(raw)):
            return fail("P8: changing source row order changed the export")

        smoke = trial(SEEDS[0], rows, reference, pages, 1)
        stages["smoke"] = smoke
        if not trial_passes(smoke):
            return fail("Smoke: at least one challenge verdict is wrong")
        if level == 1:
            return outcome

        repeats = [trial(seed, rows, reference, pages, 8) for seed in SEEDS]
        b0 = [r["b0_detected"] / r["faults"] for r in repeats]
        b1 = [r["b1_detected"] / r["faults"] for r in repeats]
        comparison = {
            "b0": summarize(b0),
            "b1": summarize(b1),
            "paired_b1_minus_b0": summarize([y - x for x, y in zip(b0, b1, strict=True)]),
            "minimum_practical_delta": MIN_DELTA,
        }
        stages["sweep"] = {"repeats": repeats, "comparison": comparison}
        if not all(trial_passes(r) for r in repeats):
            return fail("Sweep: fault detection or valid-control gate failed")
        if comparison["paired_b1_minus_b0"]["ci95"][0] <= MIN_DELTA:
            return fail("Sweep: lower paired interval bound does not exceed the threshold")
        if level == 2:
            return outcome

        restored = json.loads(json.dumps(rows))
        errors = strong(restored, reference, pages)
        if errors or restored != rows:
            return fail("Full: exported JSON fails its round-trip contract", errors)
        stages["full"] = {
            "analysis_ready_rows": sum(r["analysis_ready"] for r in rows),
            "unreported_rows": sorted(r["id"] for r in rows if r["kind"] == "missing"),
            "disagreements": {
                group: [
                    {k: r[k] for k in ("id", "kind", "unit", "values", "page", "analysis_ready")}
                    for r in rows if r["id"] in ids
                ]
                for group, ids in GROUPS.items()
            },
        }
        return outcome
    except (OSError, ValueError, KeyError, ArithmeticError) as exc:
        return fail(f"Cannot complete {phase}: {exc}")


def display(result):
    stages = result["stages"]
    if "dry" in stages:
        print(f"dry: PASS ({stages['dry']['source_rows']} source records)")
    if "smoke" in stages:
        smoke = stages["smoke"]
        print(f"smoke: B1 detected {smoke['b1_detected']}/{smoke['faults']}; "
              f"false rejections {smoke['b1_false_reject']}/{smoke['valid_controls']}")
    if "sweep" in stages:
        for row in stages["sweep"]["repeats"]:
            print(f"seed {row['seed']}: B0 {row['b0_detected']}/{row['faults']}, "
                  f"B1 {row['b1_detected']}/{row['faults']}, "
                  f"B1 false rejections {row['b1_false_reject']}/{row['valid_controls']}")
        delta = stages["sweep"]["comparison"]["paired_b1_minus_b0"]
        lo, hi = delta["ci95"]
        print(f"paired B1-B0: {delta['mean']:.6f}; 95% CI [{lo:.6f}, {hi:.6f}]")
    if "full" in stages:
        print(f"full: {stages['full']['analysis_ready_rows']} rows ready for numerical analysis")
    if "error" in result:
        print(result["error"])
        for detail in result["findings"]:
            print(detail)
    print(f"Verdict: {result['status']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=PHASES, default="full")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify(args.phase)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        display(result)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
