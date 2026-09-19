"""Schema validation: structural checks run on the extracted DataFrame
BEFORE it is written to the Sandbox Lakehouse.

The failure mode this guards against isn't "the scraper crashed" - it's the
quiet one: GFPVAN renames a column, a widget fails to render and a cell
comes back blank, or pagination silently drops a page. Those all produce a
DataFrame that looks superficially fine and would otherwise land in the
lakehouse unnoticed. This step turns that into a loud, catchable failure.

Requires: pip install pandera  (or: uv add pandera)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema

from .logger import get_logger

log = get_logger(__name__)


# Columns every extract must have, non-blank. strict=False means extra
# columns are allowed (the real grid may have more than we enumerate here) -
# this validates completeness of the fields we depend on, not an exact
# column count.
#
# IMPORTANT: "Country Name", "L5 Product", and "Review Status - Inventory"
# are placeholders matching the column names already assumed elsewhere in
# this pipeline (select_approved_rows, config.yaml). Update this list once
# you've confirmed the real grid's header text - a mismatch here just means
# every run reports these as "missing", not a crash.
GFPVAN_EXTRACT_SCHEMA = DataFrameSchema(
    {
        "Country Name": Column(str, Check.str_length(min_value=1), nullable=False),
        "L5 Product": Column(str, Check.str_length(min_value=1), nullable=False),
        "Review Status - Inventory": Column(
            str, Check.str_length(min_value=1), nullable=False
        ),
        "_source_page": Column(int, nullable=False),
        "_extracted_at": Column(str, nullable=False),
    },
    strict=False,
    coerce=False,
)

# Schema for the WIDE Multi-Collab metric extract (multi_collab_extract.py's
# reshape_to_wide output) - a different shape from the grid extract above.
# These three columns are exactly config.yaml's dedup_key: if any of them
# come back blank, the upsert in extract.upsert_to_excel() can't reliably
# match rows, so this treats that as a hard validation failure rather than
# letting a bad key silently corrupt the master workbook.
MULTI_COLLAB_SCHEMA = DataFrameSchema(
    {
        "Product": Column(str, Check.str_length(min_value=1), nullable=False),
        "Country": Column(str, Check.str_length(min_value=1), nullable=False),
        "Period": Column(str, Check.str_length(min_value=1), nullable=False),
    },
    strict=False,
    coerce=False,
)


@dataclass
class ValidationResult:
    ok: bool
    errors: pd.DataFrame | None = None  # pandera's failure_cases table, if any
    blank_field_counts: dict = field(default_factory=dict)
    missing_columns: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return "Validation passed."
        parts = []
        if self.missing_columns:
            parts.append(f"missing columns: {self.missing_columns}")
        if self.blank_field_counts:
            parts.append(f"blank field counts: {self.blank_field_counts}")
        if self.errors is not None and not self.errors.empty:
            parts.append(f"{len(self.errors)} schema check failure(s)")
        return "Validation failed - " + "; ".join(parts)


def validate_extract(
    df: pd.DataFrame, schema: DataFrameSchema = GFPVAN_EXTRACT_SCHEMA
) -> ValidationResult:
    """Run structural checks against df. Returns a ValidationResult rather
    than raising, so the caller decides whether to halt the pipeline or
    quarantine-and-continue. Only raises for a genuinely malformed input."""
    if df is None:
        raise ValueError("validate_extract() received None, not a DataFrame")

    missing_columns = [c for c in schema.columns if c not in df.columns]

    if df.empty:
        log.warning("Extract is empty - nothing to validate")
        return ValidationResult(
            ok=False, missing_columns=missing_columns or ["<all - dataframe is empty>"]
        )

    # Blank-but-present values (e.g. "" or whitespace) are the classic sign
    # of UI drift: the column still renders, but the field inside it came
    # back empty. Reported for every object column, not just schema ones,
    # since a newly-blank column you haven't added to the schema yet is
    # exactly the kind of thing this should surface.
    blank_counts: dict[str, int] = {}
    for col in df.columns:
        if df[col].dtype == object:
            blank = df[col].astype(str).str.strip().eq("").sum()
            if blank > 0:
                blank_counts[col] = int(blank)

    validatable_cols = [c for c in schema.columns if c in df.columns]
    if not validatable_cols:
        return ValidationResult(
            ok=False, missing_columns=missing_columns, blank_field_counts=blank_counts
        )

    try:
        partial_schema = schema.remove_columns(
            [c for c in schema.columns if c not in validatable_cols]
        )
        partial_schema.validate(df, lazy=True)
        ok = not missing_columns and not blank_counts
        return ValidationResult(
            ok=ok, missing_columns=missing_columns, blank_field_counts=blank_counts
        )
    except pa.errors.SchemaErrors as e:
        log.error(
            "Schema validation failed: %d failure case(s)", len(e.failure_cases)
        )
        return ValidationResult(
            ok=False,
            errors=e.failure_cases,
            missing_columns=missing_columns,
            blank_field_counts=blank_counts,
        )
