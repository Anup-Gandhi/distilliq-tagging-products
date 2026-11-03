"""Reads candidate rows from BigQuery (or a CSV export of the same table) and cleans them."""
import csv
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

MISC_OPTION = "Misc - Not a Liquor"  # the "not alcoholic" tag


@dataclass(frozen=True)
class SourceRow:
    source_row_id: str
    base_value: str               # the store's item name
    options: list[str]            # candidate catalogue names
    insert_dttm: datetime | None  # BigQuery insert time (used as the watermark)


@dataclass
class FetchStats:
    fetched: int = 0
    invalid: int = 0
    invalid_ids: list[str] = field(default_factory=list)


def parse_options(raw) -> list[str]:
    """Split `a | b | c` (or take a list), trim, drop empties and duplicates, keeping the first."""
    parts = raw if isinstance(raw, (list, tuple)) else str(raw or "").split("|")
    seen, options = set(), []
    for part in parts:
        option = str(part or "").strip()
        if option and option not in seen:
            seen.add(option)
            options.append(option)
    return options


def clean_row(source_row_id, base_value, raw_options, insert_dttm=None) -> SourceRow | None:
    """Return the cleaned row, or None when it is invalid and should be skipped."""
    source_row_id = str(source_row_id or "").strip()
    base_value = str(base_value or "").strip()
    options = parse_options(raw_options)
    # No real candidates (only None / Misc): still tag it, so the LLM can name the product itself.
    is_misc = not options or all(o.lower() in ("none", MISC_OPTION.lower()) for o in options)
    if is_misc:
        options = [MISC_OPTION]  # no candidates: the LLM names the product itself
    if not source_row_id or not base_value or (not is_misc and len(options) < 2):
        return None
    return SourceRow(source_row_id, base_value, options, insert_dttm)


QUERY = """
SELECT CAST(sourceRowId AS STRING) AS source_row_id,
       baseValue AS base_value,
       options AS options,
       TIMESTAMP(insert_dttm) AS insert_dttm
FROM `{table}`
WHERE @wm IS NULL OR TIMESTAMP(insert_dttm) >= @wm
ORDER BY insert_dttm
"""


def fetch_rows(table: str, watermark: datetime | None, stats: FetchStats) -> Iterator[SourceRow]:
    from google.cloud import bigquery

    client = bigquery.Client()
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("wm", "TIMESTAMP", watermark)])
    job = client.query(QUERY.format(table=table), job_config=config)
    for r in job.result(page_size=5000):
        stats.fetched += 1
        row = clean_row(r["source_row_id"], r["base_value"], r["options"], r["insert_dttm"])
        if row is None:
            stats.invalid += 1
            stats.invalid_ids.append(str(r["source_row_id"]))
            continue
        yield row


def _column(fieldnames: list[str], *names: str) -> str | None:
    lookup = {f.strip().lower(): f for f in fieldnames}
    return next((lookup[n.lower()] for n in names if n.lower() in lookup), None)


def read_csv(path: str) -> list[dict]:
    """Every non-blank row of a CSV, with its id, item, options and insert time columns renamed to
    source_row_id, base_value, options and insert_dttm. All other columns are kept as they are."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        columns = {"source_row_id": _column(fields, "sourceRowId", "source_row_id"),
                   "base_value": _column(fields, "baseValue", "base_value"),
                   "options": _column(fields, "options")}
        if not all(columns.values()):
            raise SystemExit(f"{path} needs the columns sourceRowId, baseValue and options (found: {fields})")
        columns["insert_dttm"] = _column(fields, "insert_dttm", "source_insert_dttm")
        rows = []
        for r in reader:
            if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
                continue  # spreadsheet exports end with rows of empty cells
            for name, column in columns.items():
                if column:
                    r[name] = r.pop(column)
            rows.append(r)
        return rows


def fetch_csv(path: str, stats: FetchStats) -> Iterator[SourceRow]:
    """Rows from a CSV export of topk_options (sourceRowId, baseValue, options[, insert_dttm])."""
    for r in read_csv(path):
        stats.fetched += 1
        insert_dttm = None
        if r.get("insert_dttm"):
            try:
                insert_dttm = datetime.fromisoformat(r["insert_dttm"].strip().replace(" UTC", "+00:00"))
            except ValueError:
                pass
        row = clean_row(r["source_row_id"], r["base_value"], r["options"], insert_dttm)
        if row is None:
            stats.invalid += 1
            stats.invalid_ids.append(str(r["source_row_id"]))
            continue
        yield row
