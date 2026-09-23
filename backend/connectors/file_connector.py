"""File sources: CSV, TSV, Excel (.xlsx/.xls, one table per sheet), JSON, JSONL, Parquet, XML.
Config: {"files": ["/abs/path/a.csv", ...]}  (uploads are stored under data/uploads/<source_id>/)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

import duckdb
import pandas as pd

from backend.connectors.base import ConfigField
from backend.connectors.materialized import MaterializedConnector
from backend.connectors.registry import register
from backend.core.config import settings

SUPPORTED = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".json", ".jsonl", ".ndjson", ".parquet", ".xml"}


def table_name_for(path: Path, suffix: str | None = None) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", path.stem).strip("_").lower() or "table"
    if base[0].isdigit():
        base = "t_" + base
    if suffix:
        base += "_" + re.sub(r"[^a-zA-Z0-9]+", "_", suffix).strip("_").lower()
    return base


@register
class FileConnector(MaterializedConnector):
    type_key = "file"
    display_name = "Files (CSV / Excel / JSON / Parquet)"
    category = "file"
    config_fields = [ConfigField("files", "Files", type="file", help="Upload one or more files")]

    def files(self) -> list[Path]:
        paths = [Path(p) for p in self.config.get("files", [])]
        d = settings.uploads_dir / self.source_id
        if d.exists():
            paths += [p for p in d.iterdir() if p.suffix.lower() in SUPPORTED and p not in paths]
        return [p for p in paths if p.exists()]

    def load_into(self, con: duckdb.DuckDBPyConnection) -> list[str]:
        names: list[str] = []
        for path in self.files():
            ext = path.suffix.lower()
            p = str(path).replace("'", "''")
            if ext in {".csv", ".tsv", ".txt"}:  # DuckDB streams the file: no pandas, bounded memory
                name = table_name_for(path)
                con.execute(f"CREATE OR REPLACE TABLE {self.quote_ident(name)} AS SELECT * FROM read_csv_auto('{p}', header=true, sample_size=-1)")
                names.append(name)
            elif ext == ".parquet":
                name = table_name_for(path)
                con.execute(f"CREATE OR REPLACE TABLE {self.quote_ident(name)} AS SELECT * FROM read_parquet('{p}')")
                names.append(name)
            elif ext in {".json", ".jsonl", ".ndjson"}:
                name = table_name_for(path)
                try:
                    con.execute(f"CREATE OR REPLACE TABLE {self.quote_ident(name)} AS SELECT * FROM read_json_auto('{p}')")
                    desc = con.execute(f"DESCRIBE {self.quote_ident(name)}").fetchall()
                    if len(desc) == 1 and desc[0][1].upper().startswith("STRUCT") and desc[0][1].endswith("[]"):
                        # {"data": [ {...}, ... ]} wrapper → flatten records (nested keys become parent_child columns)
                        raise duckdb.Error("wrapped json")
                except duckdb.Error:  # nested/odd JSON: fall back to pandas normalisation
                    df = _read_json_pandas(path)
                    con.register("_tmp_df", df)
                    con.execute(f"CREATE OR REPLACE TABLE {self.quote_ident(name)} AS SELECT * FROM _tmp_df")
                    con.unregister("_tmp_df")
                names.append(name)
            else:
                for name, df in self._frames_for(path):
                    con.register("_tmp_df", df)
                    con.execute(f"CREATE OR REPLACE TABLE {self.quote_ident(name)} AS SELECT * FROM _tmp_df")
                    con.unregister("_tmp_df")
                    names.append(name)
        return names

    def _frames_for(self, path: Path) -> Iterator[tuple[str, pd.DataFrame]]:
        ext = path.suffix.lower()
        if ext in {".xlsx", ".xlsm", ".xls"}:
            sheets = pd.read_excel(path, sheet_name=None)
            multi = len(sheets) > 1
            for sheet, df in sheets.items():
                if df.empty:
                    continue
                df.columns = [str(c).strip() for c in df.columns]
                yield table_name_for(path, sheet if multi else None), df
        elif ext == ".xml":
            root = ET.parse(path).getroot()
            records = [{**child.attrib, **{c.tag: c.text for c in child}} for child in root]
            yield table_name_for(path), pd.DataFrame(records)


def _read_json_pandas(path: Path) -> pd.DataFrame:
    import json

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records", "rows"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    return pd.json_normalize(data, sep="_")
