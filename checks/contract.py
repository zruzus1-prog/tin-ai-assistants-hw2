"""Verify a prepared table against the frozen dataset and source pages."""

import csv
from fractions import Fraction
import hashlib
import json
import re
import unicodedata


PAGES = (2, 3, 5, 8)
RAW_COLUMNS = ("id", "process", "medium", "parameter", "value", "unit", "page", "quote", "status")
CONTEXT = ("process", "medium", "parameter")
STR_FIELDS = ("id", *CONTEXT, "source_value", "source_unit", "quote", "status", "kind")
COLUMNS = set(STR_FIELDS) | {"page", "unit", "values", "conflict_group", "analysis_ready"}
STATUSES = {"reported", "conflict", "not_reported"}
ARITY = {"exact": 1, "approximate": 1, "upper_bound": 1, "below": 1,
         "range": 2, "shift": 2, "missing": 0, "text": 0}
GROUPS = {"pulse_energy": {"A-07", "A-08"}, "fragment_diameter": {"F-02", "F-06"}}
NUM = r"([+-]?[0-9]+(?:\.[0-9]+)?)"
GRAMMAR = (
    ("exact", NUM), ("approximate", "~" + NUM), ("upper_bound", "up to " + NUM),
    ("below", "<" + NUM), ("range", NUM + "–" + NUM), ("shift", NUM + "→" + NUM),
)


def norm(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def expected_quantity(row):
    """Compute the oracle independently of the export's parser and Decimal arithmetic."""
    if row["status"] == "not_reported":
        return "missing", None, ()
    if row["unit"] == "qualitative":
        return "text", None, ()
    for kind, pattern in GRAMMAR:
        match = re.fullmatch(pattern, row["value"].replace("−", "-"))
        if match:
            values = tuple(Fraction(number) for number in match.groups())
            unit = row["unit"]
            if unit == "mJ":
                return kind, "uJ", tuple(n * 10**3 for n in values)
            if unit == "cm":
                return kind, "mm", tuple(n * 10 for n in values)
            return kind, "uJ" if unit == "μJ" else unit, values
    raise ValueError(f"Unrecognised reference value for {row['id']}")


def load_reference(root):
    manifest = json.loads((root / "data/manifest.json").read_text(encoding="utf-8"))
    page_files = {n: f"data/pages/page_{n:02}.txt" for n in PAGES}
    frozen = {"data/conditions.csv", *page_files.values()}
    if set(manifest["files"]) != frozen:
        raise ValueError("Unexpected source manifest files")
    for name in sorted(frozen):
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != manifest["files"][name]["sha256"]:
            raise ValueError(f"Input checksum mismatch: {name}")

    pages = {n: norm((root / path).read_text(encoding="utf-8")) for n, path in page_files.items()}
    with (root / "data/conditions.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != RAW_COLUMNS:
            raise ValueError("Unexpected input columns")
        rows = list(reader)

    by_id = {}
    for row in rows:
        if None in row or any(not row[k] for k in RAW_COLUMNS):
            raise ValueError("Empty or extra input fields")
        ident = row["id"]
        if not re.fullmatch(r"[AF]-[0-9]{2}", ident) or ident in by_id:
            raise ValueError(f"Invalid input ID: {ident}")
        if row["status"] not in STATUSES or row["process"] not in {"ablation", "fragmentation"}:
            raise ValueError(f"Invalid input status or process: {ident}")
        if (row["process"] == "ablation") != ident.startswith("A-"):
            raise ValueError(f"ID and process disagree: {ident}")
        if norm(row["quote"]) not in pages.get(int(row["page"]), ""):
            raise ValueError(f"Source quote is not on the stated page: {ident}")
        if row["status"] == "not_reported" and (row["value"], row["unit"]) != ("not reported", "—"):
            raise ValueError(f"Invalid missing-value declaration: {ident}")
        kind, _, values = expected_quantity(row)
        if kind == "range" and values[0] > values[1]:
            raise ValueError(f"Source range is reversed: {ident}")
        by_id[ident] = row

    expected_ids = {f"A-{i:02}" for i in range(1, 21)} | {f"F-{i:02}" for i in range(7)}
    if by_id.keys() != expected_ids:
        raise ValueError("Reference inventory changed")
    if {r["id"] for r in rows if r["status"] == "conflict"} != set().union(*GROUPS.values()):
        raise ValueError("Reference disagreement inventory changed")
    if {r["id"] for r in rows if r["status"] == "not_reported"} != {"F-00"}:
        raise ValueError("Reference missing-value inventory changed")
    return by_id, pages


def basic(pack, pages):
    """B0 checks shape, unique IDs, and whether the cited text occurs on its page."""
    if not isinstance(pack, list) or not pack:
        return ["B0: empty or malformed table"]
    errors = []
    seen = set()
    for row in pack:
        if not isinstance(row, dict) or row.keys() != COLUMNS:
            errors.append("B0: unexpected export columns")
            continue
        if any(not isinstance(row[key], str) or not row[key].strip() for key in STR_FIELDS):
            errors.append("B0: missing or invalid string field")
            continue
        ident = row["id"]
        if not re.fullmatch(r"[AF]-[0-9]{2}", ident) or ident in seen:
            errors.append(f"B0 {ident}: invalid or duplicate ID")
        seen.add(ident)
        kind = row["kind"]
        values = row["values"]
        if (
            kind not in ARITY
            or not isinstance(values, list)
            or len(values) != ARITY[kind]
            or any(not isinstance(v, str) or not re.fullmatch(NUM, v) for v in values)
            or (row["unit"] is not None and not isinstance(row["unit"], str))
            or (row["conflict_group"] is not None and not isinstance(row["conflict_group"], str))
            or row["status"] not in STATUSES
            or type(row["analysis_ready"]) is not bool
        ):
            errors.append(f"B0 {ident}: invalid quantity or status fields")
        page = row["page"]
        if type(page) is not int or page not in pages or norm(row["quote"]) not in pages[page]:
            errors.append(f"B0 {ident}: quote not found on page")
    return errors


def strong(pack, reference, pages):
    """B1 uses B0 and checks conservation of source meaning per record."""
    errors = basic(pack, pages)
    if errors:
        return errors

    observed = {row["id"]: row for row in pack}
    if observed.keys() != reference.keys():
        missing = sorted(reference.keys() - observed.keys())
        extra = sorted(observed.keys() - reference.keys())
        errors.append(f"P1: missing IDs={missing}; unexpected IDs={extra}")
    expected_groups = {ident: group for group, ids in GROUPS.items() for ident in ids}

    for ident in sorted(observed.keys() & reference.keys()):
        row, raw = observed[ident], reference[ident]
        if row["page"] != int(raw["page"]) or norm(row["quote"]) != norm(raw["quote"]):
            errors.append(f"P2 {ident}: citation belongs to a different record")
        if any(row[key] != raw[key] for key in CONTEXT) or (
            row["source_value"], row["source_unit"]
        ) != (raw["value"], raw["unit"]):
            errors.append(f"P3 {ident}: original context or value changed")
        kind, unit, expected = expected_quantity(raw)
        actual = tuple(Fraction(v) for v in row["values"])
        if (row["kind"], row["unit"], actual) != (kind, unit, expected):
            errors.append(
                f"P4 {ident}: got {row['kind']} {row['values']} {row['unit']}; "
                f"expected {kind} {[str(v) for v in expected]} {unit}"
            )
        if raw["status"] == "not_reported" and (
            row["status"] != "not_reported" or row["values"] or row["analysis_ready"]
        ):
            errors.append(f"P5 {ident}: an unreported condition was imputed")
        if row["status"] != raw["status"] or row["conflict_group"] != expected_groups.get(ident):
            errors.append(f"P6 {ident}: source status or disagreement group changed")
        ready = raw["status"] == "reported" and bool(expected) and ident != "A-04"
        if row["analysis_ready"] != ready:
            errors.append(f"P7 {ident}: invalid admission to numerical summary")
    return errors
