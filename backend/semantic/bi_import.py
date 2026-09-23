"""Fold a BI platform's own semantic model into our catalog.

Power BI and Tableau already hold governed definitions — measures with formulas, declared
relationships, curated field descriptions. Re-deriving that would be wasteful and less accurate than
what the business already agreed on, so it is imported directly:

    model table/column descriptions  ->  Table.description / Column.business_definition
    measures                         ->  Metric  (SQL when translatable, otherwise definition-only)
    declared relationships           ->  Relationship, pre-approved (the BI model is authoritative)

Measures whose native formula cannot be translated exactly keep their DAX/Tableau formula and are
marked `is_computable = False`; they remain searchable and citable, but the engine will not invent a
number for them. Importing is idempotent — re-syncing updates in place.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.connectors.bi_base import SemanticModel
from backend.metadata.models import Column, DataSource, Dataset, Metric, Relationship, Table
from backend.semantic.expressions import translate
from backend.semantic.versioning import record_version, snapshot

log = logging.getLogger(__name__)

SYSTEM_LABEL = {"powerbi": "Power BI", "tableau": "Tableau"}


def metric_slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(name)).strip("_").lower() or "measure"
    return ("m_" + s) if s[0].isdigit() else s


def _format_of(format_string: str | None, name: str) -> tuple[str, str | None]:
    """Map a BI format string to our (format, unit)."""
    fs = (format_string or "").strip()
    low = f"{fs} {name}".lower()
    if "%" in fs or "percent" in low or low.strip().endswith(" pct"):
        return "percent", "%"
    for sym, unit in (("₹", "INR"), ("$", "USD"), ("€", "EUR"), ("£", "GBP")):
        if sym in fs:
            return "currency", unit
    if "currency" in low or "revenue" in low or "amount" in low or "sales" in low or "cost" in low:
        return "currency", None
    return "number", None


def import_semantic_model(db: Session, source: DataSource, model: SemanticModel, actor: str = "system") -> dict[str, Any]:
    """Apply `model` to the catalog rows already created for `source`. Returns a summary."""
    label = SYSTEM_LABEL.get(model.system, model.system)
    provenance = f"{label} · {model.name}"
    summary: dict[str, Any] = {
        "system": model.system, "model": model.name, "tables_described": 0, "columns_described": 0,
        "metrics_created": 0, "metrics_updated": 0, "metrics_definition_only": 0,
        "relationships_imported": 0, "skipped": [],
    }

    dataset = db.scalar(select(Dataset).where(Dataset.source_id == source.id))
    if dataset is None:
        summary["skipped"].append("no dataset for this source — run a sync first")
        return summary

    tables = db.scalars(
        select(Table).where(Table.dataset_id == dataset.id).options(selectinload(Table.columns))
    ).all()
    by_name = {t.table_name.lower(): t for t in tables}

    # ---------------------------------------------------------------- descriptions
    for mt in model.tables:
        t = by_name.get(mt.name.lower())
        if t is None:
            continue
        if mt.description and not t.description:
            prev = snapshot(t, "table")
            t.description = mt.description
            record_version(db, "table", t, prev, actor, t.qualified_name)
            summary["tables_described"] += 1
        cols = {c.column_name.lower(): c for c in t.columns}
        for mc in mt.columns:
            c = cols.get(mc.name.lower())
            if c is None:
                continue
            changed = False
            if mc.description and not c.business_definition:
                c.business_definition = mc.description
                changed = True
            fmt, unit = _format_of(mc.format_string, mc.name)
            if unit and not c.unit:
                c.unit = unit
                changed = True
            if mc.is_hidden and c.semantic_type != "identifier":
                c.semantic_type = c.semantic_type or "dimension"
            if changed:
                summary["columns_described"] += 1
    db.flush()

    # ---------------------------------------------------------------- measures -> metrics
    natives = {m.name.strip().lower(): m.expression for m in model.measures}
    for measure in model.measures:
        if measure.is_hidden:
            continue
        try:
            _import_measure(db, source, model, measure, natives, by_name, provenance, actor, summary)
        except Exception as e:  # noqa: BLE001 - one bad measure must not abort the import
            log.warning("measure %r could not be imported: %s", measure.name, e)
            summary["skipped"].append(f"measure {measure.name}: {e}")
    db.flush()

    # ---------------------------------------------------------------- relationships
    existing = {(r.from_column_id, r.to_column_id) for r in db.scalars(select(Relationship)).all()}
    for rel in model.relationships:
        if not rel.is_active:
            continue
        a = _find_column(by_name, rel.from_table, rel.from_column)
        b = _find_column(by_name, rel.to_table, rel.to_column)
        if a is None or b is None:
            summary["skipped"].append(f"relationship {rel.from_table}.{rel.from_column} -> {rel.to_table}.{rel.to_column}: column not found")
            continue
        if (a.id, b.id) in existing or (b.id, a.id) in existing:
            continue
        db.add(Relationship(
            from_column_id=a.id, to_column_id=b.id, type=rel.cardinality, confidence=1.0,
            reason=f"Declared in the {provenance} semantic model", status="approved",
            created_by=f"import:{source.name}", is_cross_source=False,
            evidence={"signal": "bi_semantic_model", "system": model.system},
        ))
        existing.add((a.id, b.id))
        summary["relationships_imported"] += 1
    db.flush()
    return summary


def _find_column(by_name: dict[str, Table], table: str, column: str) -> Column | None:
    t = by_name.get(str(table).lower())
    if t is None:
        return None
    return next((c for c in t.columns if c.column_name.lower() == str(column).lower()), None)


def _import_measure(db: Session, source: DataSource, model: SemanticModel, measure, natives: dict[str, str],
                    by_name: dict[str, Table], provenance: str, actor: str, summary: dict) -> None:
    tr = translate(measure.expression, model.language, natives)

    # home table: whichever table the formula actually reads, else the one the tool records
    home: Table | None = None
    if tr.ok and tr.tables:
        home = by_name.get(next(iter(tr.tables)).lower())
    if home is None and measure.table:
        home = by_name.get(str(measure.table).lower())
    if home is None and len(by_name) == 1:
        home = next(iter(by_name.values()))

    computable = bool(tr.ok and home is not None)
    if tr.ok and home is None:
        tr.reason = "the table it reads is not in this source"

    name = metric_slug(measure.name)
    existing = db.scalar(select(Metric).where(Metric.name == name))
    if existing is not None and existing.source_system != provenance:
        name = f"{metric_slug(source.name)}_{name}"
        existing = db.scalar(select(Metric).where(Metric.name == name))

    fmt, unit = _format_of(measure.format_string, measure.name)
    note = f"Imported from {provenance}."
    if not computable:
        note += f" Definition only — the {model.language.upper()} formula has no exact SQL equivalent here ({tr.reason})."
    description = " ".join(x for x in [measure.description, note] if x)

    values = dict(
        display_name=measure.name,
        description=description,
        table_id=home.id if home is not None else None,
        expression=tr.sql if computable else measure.expression,
        filters=tr.filters if computable else None,
        unit=unit,
        format=fmt,
        domain=(source.department or "").lower() or None,
        owner=source.owner,
        native_expression=measure.expression,
        native_language=model.language,
        is_computable=computable,
        source_system=provenance,
    )

    if existing is None:
        metric = Metric(name=name, **values)
        db.add(metric)
        db.flush()
        record_version(db, "metric", metric, None, actor)
        summary["metrics_created"] += 1
    else:
        prev = snapshot(existing, "metric")
        for k, v in values.items():
            setattr(existing, k, v)
        record_version(db, "metric", existing, prev, actor)
        summary["metrics_updated"] += 1
    if not computable:
        summary["metrics_definition_only"] += 1
