"""Postgres: the source of truth for every pass."""
from pathlib import Path

from sqlalchemy import (ARRAY, Boolean, Column, DateTime, MetaData, String, Table, Text, create_engine, func,
                        select, text)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from .bigquery_source import SourceRow
from .claude_tagger import TagResult

metadata = MetaData()

llm_tags = Table(
    "llm_tags", metadata,
    Column("source_row_id", String, primary_key=True),
    Column("base_value", Text, nullable=False),
    Column("options", ARRAY(String), nullable=False),
    Column("tag", Text),
    Column("selected_option", Text),
    Column("is_alcoholic", Boolean),
    Column("suggested_name", Text),
    Column("confidence", String(10)),
    Column("reason", Text),
    Column("judgements", JSONB, nullable=False, server_default="[]"),
    Column("sources", JSONB, nullable=False, server_default="[]"),
    Column("error", Text),
    Column("skip_reason", Text),  # set when the tagger found the record too unclear; tag is then NULL
    Column("model", String(64), nullable=False),
    Column("source_insert_dttm", DateTime(timezone=True)),
    Column("tagged_at", DateTime(timezone=True), server_default=func.now()),
)

claude_retags = Table(
    "claude_retags", metadata,
    Column("source_row_id", String, primary_key=True),
    Column("base_value", Text, nullable=False),
    Column("first_tag", Text, nullable=False),
    Column("first_model", String(64)),
    Column("tag", Text),
    Column("is_alcoholic", Boolean),
    Column("suggested_name", Text),
    Column("confidence", String(10)),
    Column("reason", Text),
    Column("sources", JSONB, nullable=False, server_default="[]"),
    Column("error", Text),
    Column("skip_reason", Text),  # set when the tagger found the record too unclear; tag is then NULL
    Column("model", String(64), nullable=False),
    Column("tagged_at", DateTime(timezone=True), server_default=func.now()),
)

claude_final_tags = Table(
    "claude_final_tags", metadata,
    Column("source_row_id", String, primary_key=True),
    Column("base_value", Text, nullable=False),
    Column("options", ARRAY(String), nullable=False),
    Column("tag_1", Text),
    Column("tag_2", Text),
    Column("tag_1_model", String(64)),
    Column("tag_2_model", String(64)),
    Column("tag", Text),
    Column("selected_option", Text),
    Column("is_alcoholic", Boolean),
    Column("suggested_name", Text),
    Column("confidence", String(10)),
    Column("reason", Text),
    Column("judgements", JSONB, nullable=False, server_default="[]"),
    Column("sources", JSONB, nullable=False, server_default="[]"),
    Column("error", Text),
    Column("skip_reason", Text),  # set when the tagger found the record too unclear; tag is then NULL
    Column("model", String(64), nullable=False),
    Column("tagged_at", DateTime(timezone=True), server_default=func.now()),
)

# Every row of each CSV given to `run --csv` or `import`, kept as uploaded.
csv_records = Table(
    "csv_records", metadata,
    Column("source_row_id", String, primary_key=True),
    Column("base_value", Text, nullable=False),
    Column("options", Text),          # as in the file: "a | b | c"
    Column("source_file", Text, nullable=False),
    Column("data", JSONB, nullable=False),  # the whole row, including human tags such as tag_1 and final_tags
    Column("tagged_at", DateTime(timezone=True), server_default=func.now()),  # when it was uploaded
)

# create_all doesn't change a table made by an earlier version.
MIGRATIONS = [
    "ALTER TABLE claude_retags ADD COLUMN IF NOT EXISTS skip_reason TEXT",
    "ALTER TABLE claude_final_tags ADD COLUMN IF NOT EXISTS skip_reason TEXT",
]

COMPARISON_VIEW = r"""
CREATE OR REPLACE VIEW claude_tag_comparison AS
WITH tags AS (
    SELECT
        t.source_row_id, t.base_value, t.options,
        regexp_replace(regexp_replace(t.tag, '^\s*others:\s*', '', 'i'),
                       '^\s*<\s*(misc - not a liquor)\s*>\s*$', '\1', 'i') AS tag_1,
        regexp_replace(regexp_replace(r.tag, '^\s*others:\s*', '', 'i'),
                       '^\s*<\s*(misc - not a liquor)\s*>\s*$', '\1', 'i') AS tag_2,
        t.model AS tag_1_model, r.model AS tag_2_model,
        t.is_alcoholic AS is_alcoholic_1, r.is_alcoholic AS is_alcoholic_2,
        r.source_row_id IS NOT NULL AS retagged,
        f.tag AS tag_3, f.model AS tag_3_model, f.is_alcoholic AS is_alcoholic_3,
        t.skip_reason AS skip_reason_1, r.skip_reason AS skip_reason_2, f.skip_reason AS skip_reason_3
    FROM llm_tags t
    LEFT JOIN claude_retags r USING (source_row_id)
    LEFT JOIN claude_final_tags f USING (source_row_id)
)
SELECT
    source_row_id, base_value, options, tag_1, tag_2,
    CASE WHEN retagged THEN COALESCE(lower(trim(tag_1)) = lower(trim(tag_2)), false) END AS tags_match,
    tag_1_model, tag_2_model,
    -- new columns go last: CREATE OR REPLACE VIEW can only add columns at the end
    is_alcoholic_1, is_alcoholic_2,
    is_alcoholic_1 OR is_alcoholic_2 AS is_alcoholic_any,
    tag_3, tag_3_model, is_alcoholic_3,
    skip_reason_1, skip_reason_2, skip_reason_3
FROM tags
"""

VOTES_VIEW = """
CREATE OR REPLACE VIEW claude_tag_votes AS
WITH v AS (
    SELECT c.*,
           lower(trim(tag_3)) = lower(trim(tag_1)) AS tag_3_agrees_1,
           lower(trim(tag_3)) = lower(trim(tag_2)) AS tag_3_agrees_2
    FROM claude_tag_comparison c
)
SELECT
    source_row_id, base_value, options, tag_1, tag_2, tag_3,
    CASE
        WHEN tags_match THEN tag_1
        WHEN skip_reason_1 IS NOT NULL THEN NULL
        WHEN tag_2 IS NULL AND skip_reason_2 IS NULL THEN NULL
        WHEN NOT is_alcoholic_any THEN 'Misc - Not a Liquor'
        WHEN tag_3_agrees_1 OR tag_3_agrees_2 THEN tag_3
    END AS final_tag,
    CASE
        WHEN tags_match THEN 'agreed'
        WHEN skip_reason_1 IS NOT NULL THEN 'skipped'
        WHEN tag_2 IS NULL AND skip_reason_2 IS NULL THEN 'needs retag'
        WHEN NOT is_alcoholic_any THEN 'both not alcoholic'
        WHEN tag_3 IS NULL AND skip_reason_3 IS NULL THEN 'needs final'
        WHEN tag_3_agrees_1 THEN 'final agrees with 1'
        WHEN tag_3_agrees_2 THEN 'final agrees with 2'
        WHEN skip_reason_3 IS NOT NULL THEN 'skipped'
        ELSE 'no majority'
    END AS decision,
    tag_1_model, tag_2_model, tag_3_model,
    CASE WHEN is_alcoholic_1 = is_alcoholic_2 THEN is_alcoholic_1 ELSE is_alcoholic_3 END AS is_alcoholic,
    is_alcoholic_1, is_alcoholic_2, is_alcoholic_3,
    -- why the row was skipped: by pass 1 (never retagged), or by pass 3 when it couldn't settle a dispute
    CASE
        WHEN tags_match THEN NULL
        WHEN skip_reason_1 IS NOT NULL THEN skip_reason_1
        WHEN tag_3_agrees_1 OR tag_3_agrees_2 THEN NULL
        ELSE skip_reason_3
    END AS skip_reason
FROM v
"""

# Pass 3 input: pass 2 disagreed with pass 1 (a pass 2 skip counts as disagreeing) and one of them says alcoholic.
DISPUTED = ("tag_1 IS NOT NULL AND (tag_2 IS NOT NULL OR skip_reason_2 IS NOT NULL) "
            "AND tags_match IS NOT TRUE AND is_alcoholic_any IS TRUE")

# The human tag from an uploaded CSV next to the LLM's tag (the vote's final tag, else pass 1's).
HUMAN_COMPARISON_VIEW = r"""
CREATE OR REPLACE VIEW human_tag_comparison AS
WITH h AS (
    SELECT r.source_row_id, r.base_value, r.options, r.source_file,
           NULLIF(trim(COALESCE(NULLIF(r.data->>'final_tags', ''), r.data->>'tag_1')), '') AS human_tag,
           COALESCE(v.final_tag, v.tag_1) AS llm_tag,
           v.tag_1_model AS llm_model, v.decision, v.skip_reason
    FROM csv_records r
    LEFT JOIN claude_tag_votes v USING (source_row_id)
), n AS (
    SELECT h.*,
           -- compare ignoring "Others: ", case, punctuation and spacing
           regexp_replace(lower(regexp_replace(human_tag, '^\s*others:\s*', '', 'i')), '[^0-9a-z]+', '', 'g') AS human_key,
           regexp_replace(lower(llm_tag), '[^0-9a-z]+', '', 'g') AS llm_key,
           NOT EXISTS (SELECT 1 FROM unnest(string_to_array(options, '|')) o
                       WHERE lower(trim(o)) = lower(llm_tag)) AS llm_new_name
    FROM h
)
SELECT source_row_id, base_value, options, source_file, human_tag, llm_tag, llm_model, decision, skip_reason,
    CASE
        WHEN human_tag IS NULL THEN 'no human tag'
        WHEN decision = 'skipped' THEN 'llm skipped'
        WHEN llm_tag IS NULL THEN 'not tagged by llm'
        WHEN human_key = llm_key THEN 'same'
        WHEN human_tag ~* '^\s*none\s*$' THEN 'human skipped'
        -- both made up a name: the human's often has a description after it, so check these by eye
        WHEN human_tag ~* '^\s*others:' AND llm_new_name AND lower(llm_tag) <> lower('Misc - Not a Liquor')
            THEN 'both new name'
        ELSE 'different'
    END AS comparison
FROM n
"""

# The old claude_tags, the new llm_tags and the human tag from an uploaded CSV, side by side, for every record in any
# of them. Only created when claude_tags exists, since it's no longer in `metadata`.
THREE_WAY_COMPARISON_VIEW = r"""
CREATE OR REPLACE VIEW three_way_tag_comparison AS
WITH ids AS (
    SELECT source_row_id FROM csv_records
    UNION SELECT source_row_id FROM claude_tags
    UNION SELECT source_row_id FROM llm_tags
), j AS (
    SELECT i.source_row_id,
           COALESCE(h.base_value, c.base_value, l.base_value) AS base_value,
           COALESCE(h.options, array_to_string(COALESCE(l.options, c.options), ' | ')) AS options,
           h.source_file,
           h.source_row_id IS NOT NULL AS in_csv,
           NULLIF(trim(COALESCE(NULLIF(h.data->>'final_tags', ''), h.data->>'tag_1')), '') AS human_tag,
           c.source_row_id IS NOT NULL AS in_claude, c.tag AS claude_tag, c.model AS claude_model,
           c.skip_reason AS claude_skip_reason, c.error AS claude_error,
           l.source_row_id IS NOT NULL AS in_llm, l.tag AS llm_tag, l.model AS llm_model,
           l.skip_reason AS llm_skip_reason, l.error AS llm_error
    FROM ids i
    LEFT JOIN csv_records h USING (source_row_id)
    LEFT JOIN claude_tags c USING (source_row_id)
    LEFT JOIN llm_tags l USING (source_row_id)
), n AS (
    -- compare ignoring "Others: ", case, punctuation and spacing (also turns "<Misc - not a Liquor>" into the plain form)
    SELECT j.*,
           regexp_replace(lower(regexp_replace(human_tag, '^\s*others:\s*', '', 'i')), '[^0-9a-z]+', '', 'g') AS human_key,
           regexp_replace(lower(regexp_replace(claude_tag, '^\s*others:\s*', '', 'i')), '[^0-9a-z]+', '', 'g') AS claude_key,
           regexp_replace(lower(regexp_replace(llm_tag, '^\s*others:\s*', '', 'i')), '[^0-9a-z]+', '', 'g') AS llm_key
    FROM j
)
SELECT source_row_id, base_value, options, source_file, human_tag, claude_tag, llm_tag, claude_model, llm_model,
    claude_skip_reason, llm_skip_reason, claude_error, llm_error,
    CASE
        WHEN NOT in_claude THEN 'not in claude_tags'
        WHEN NOT in_llm THEN 'not in llm_tags'
        WHEN claude_error IS NOT NULL OR llm_error IS NOT NULL THEN 'error'
        WHEN claude_tag IS NULL AND llm_tag IS NULL THEN 'both skipped'
        WHEN claude_tag IS NULL THEN 'claude skipped'
        WHEN llm_tag IS NULL THEN 'llm skipped'
        WHEN claude_key = llm_key THEN 'same'
        ELSE 'different'
    END AS claude_vs_llm,
    CASE
        WHEN human_tag IS NULL THEN 'no human tag'
        WHEN NOT in_claude THEN 'not in claude_tags'
        WHEN claude_error IS NOT NULL THEN 'error'
        WHEN claude_tag IS NULL THEN 'claude skipped'
        WHEN human_key = claude_key THEN 'same'
        WHEN human_tag ~* '^\s*none\s*$' THEN 'human skipped'
        ELSE 'different'
    END AS human_vs_claude,
    CASE
        WHEN human_tag IS NULL THEN 'no human tag'
        WHEN NOT in_llm THEN 'not in llm_tags'
        WHEN llm_error IS NOT NULL THEN 'error'
        WHEN llm_tag IS NULL THEN 'llm skipped'
        WHEN human_key = llm_key THEN 'same'
        WHEN human_tag ~* '^\s*none\s*$' THEN 'human skipped'
        ELSE 'different'
    END AS human_vs_llm,
    CASE
        WHEN human_tag IS NULL OR claude_tag IS NULL OR llm_tag IS NULL THEN 'not all tagged'
        WHEN human_tag ~* '^\s*none\s*$' THEN 'human skipped'
        WHEN claude_key = llm_key AND llm_key = human_key THEN 'all same'
        WHEN claude_key = llm_key THEN 'human differs'
        WHEN claude_key = human_key THEN 'llm differs'
        WHEN llm_key = human_key THEN 'claude differs'
        ELSE 'all different'
    END AS claude_vs_llm_vs_human
FROM n
"""


def _upsert(conn, table: Table, values: dict | list[dict]) -> None:
    statement = insert(table).values(values)
    updates = {c.name: statement.excluded[c.name] for c in table.columns
               if c.name not in ("source_row_id", "tagged_at")}
    updates["tagged_at"] = func.now()
    conn.execute(statement.on_conflict_do_update(index_elements=["source_row_id"], set_=updates))


def _decision_values(result: TagResult) -> dict:
    return {
        "tag": result.tag, "is_alcoholic": result.is_alcoholic, "suggested_name": result.suggested_name,
        "confidence": result.confidence, "reason": result.reason, "sources": result.sources or [],
        "error": result.error, "skip_reason": result.skip_reason,
    }


CONNECT_ARGS = {"connect_timeout": 10}


def _create_database(database_url: str) -> None:
    """Create the database in DATABASE_URL, connecting to the server's built-in `postgres` database to do it."""
    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args=CONNECT_ARGS)
    try:
        with admin.connect() as conn:
            # template0 allows no connections, so creating can't fail with "template1 is being accessed by other users"
            name = conn.dialect.identifier_preparer.quote(url.database)
            conn.exec_driver_sql(f"CREATE DATABASE {name} TEMPLATE template0")
    finally:
        admin.dispose()
    print(f"Created database {url.database}", flush=True)


class Store:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, pool_pre_ping=True, connect_args=CONNECT_ARGS)
        try:
            metadata.create_all(self.engine)
        except OperationalError as e:
            if f'database "{make_url(database_url).database}" does not exist' not in str(e.orig):
                raise
            _create_database(database_url)
            metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            for statement in MIGRATIONS + [COMPARISON_VIEW, VOTES_VIEW, HUMAN_COMPARISON_VIEW]:
                conn.exec_driver_sql(statement)
            if conn.exec_driver_sql("SELECT to_regclass('claude_tags')").scalar():
                conn.exec_driver_sql(THREE_WAY_COMPARISON_VIEW)

    def _rows(self, statement) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(statement)]

    def _ids(self, table: Table, include_errors: bool) -> set[str]:
        statement = select(table.c.source_row_id)
        if not include_errors:
            statement = statement.where(table.c.error.is_(None))
        with self.engine.connect() as conn:
            return set(conn.execute(statement).scalars())

    # Pass 1

    def watermark(self):
        with self.engine.connect() as conn:
            return conn.execute(select(func.max(llm_tags.c.source_insert_dttm))).scalar()

    def tagged_ids(self, include_errors: bool = True) -> set[str]:
        return self._ids(llm_tags, include_errors)

    def save(self, row: SourceRow, result: TagResult, model: str) -> None:
        options = list(row.options)
        # A suggested name becomes one more option, so the tag always reads like a candidate.
        if result.tag and result.tag == result.suggested_name and result.tag not in options:
            options.append(result.tag)
        values = {"source_row_id": row.source_row_id, "base_value": row.base_value, "options": options,
                  "selected_option": result.selected_option, "judgements": result.judgements or [],
                  "model": model, "source_insert_dttm": row.insert_dttm, **_decision_values(result)}
        with self.engine.begin() as conn:
            _upsert(conn, llm_tags, values)

    def error_rows(self) -> list[dict]:
        """Failed pass 1 rows, oldest first, as stored (including human edits)."""
        statement = (select(llm_tags).where(llm_tags.c.error.is_not(None))
                     .order_by(llm_tags.c.source_insert_dttm.asc().nulls_first(), llm_tags.c.source_row_id))
        return self._rows(statement)

    def first_pass_rows(self) -> list[dict]:
        statement = (select(llm_tags).where(llm_tags.c.tag.is_not(None))
                     .order_by(llm_tags.c.source_insert_dttm.asc().nulls_first(), llm_tags.c.source_row_id))
        return self._rows(statement)

    def first_pass_models(self) -> list[str]:
        with self.engine.connect() as conn:
            return list(conn.execute(select(llm_tags.c.model).distinct()).scalars())

    # Pass 2

    def retagged_ids(self, include_errors: bool = True) -> set[str]:
        return self._ids(claude_retags, include_errors)

    def save_retag(self, first: dict, result: TagResult, model: str) -> None:
        values = {"source_row_id": first["source_row_id"], "base_value": first["base_value"],
                  "first_tag": first["tag"], "first_model": first["model"], "model": model,
                  **_decision_values(result)}
        if result.tag and not result.selected_option and result.tag == result.suggested_name:
            values["tag"] = f"Others: {result.tag}"
        with self.engine.begin() as conn:
            _upsert(conn, claude_retags, values)

    # Pass 3

    def disputed_rows(self) -> list[dict]:
        return self._rows(text(f"SELECT * FROM claude_tag_comparison WHERE {DISPUTED} ORDER BY source_row_id"))

    def final_tagged_ids(self, include_errors: bool = True) -> set[str]:
        return self._ids(claude_final_tags, include_errors)

    def save_final(self, disputed: dict, row: SourceRow, result: TagResult, model: str) -> None:
        values = {"source_row_id": row.source_row_id, "base_value": row.base_value, "options": row.options,
                  "tag_1": disputed["tag_1"], "tag_2": disputed["tag_2"],
                  "tag_1_model": disputed["tag_1_model"], "tag_2_model": disputed["tag_2_model"],
                  "selected_option": result.selected_option, "judgements": result.judgements or [],
                  "model": model, **_decision_values(result)}
        with self.engine.begin() as conn:
            _upsert(conn, claude_final_tags, values)

    # Uploaded CSVs

    def save_csv(self, path: str, rows: list[dict]) -> int:
        """Upsert the rows read by read_csv into csv_records. Returns how many were saved."""
        by_id = {}  # one statement can't upsert the same id twice, so a later duplicate wins
        for r in rows:
            row_id, base_value = (r.get("source_row_id") or "").strip(), (r.get("base_value") or "").strip()
            if row_id and base_value:
                by_id[row_id] = {"source_row_id": row_id, "base_value": base_value, "options": r.get("options"),
                                 "source_file": Path(path).name, "data": r}
        if by_id:
            with self.engine.begin() as conn:
                _upsert(conn, csv_records, list(by_id.values()))
        return len(by_id)

    # Export

    def all_rows(self) -> list[dict]:
        return self._rows(select(llm_tags).order_by(llm_tags.c.source_row_id))

    def all_retags(self) -> list[dict]:
        return self._rows(select(claude_retags).order_by(claude_retags.c.source_row_id))
