"""Translate BI measure formulas (Power BI DAX, Tableau) into the SQL aggregate expressions our
query engine runs.

The single most important property here is that translation **fails loudly**. A measure we cannot
translate exactly is marked non-computable and keeps its native formula, so it stays searchable and
quotable while the engine refuses to produce a number for it. Silently dropping a filter or a
time-intelligence wrapper would yield a confident wrong answer, which is the one outcome the whole
platform is built to avoid.

A real recursive-descent parser is used rather than regex substitution, precisely so that anything
unrecognised raises instead of slipping through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class Untranslatable(ValueError):
    """Raised when a formula cannot be rendered as an exact SQL equivalent."""


@dataclass
class Translation:
    sql: str | None = None
    filters: str | None = None
    tables: set[str] = field(default_factory=set)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.sql is not None


# ---------------------------------------------------------------- dialect definitions
# name -> (sql_template, arg_kind)  arg_kind: "column" | "table" | "expr" | "expr2"
DAX_FUNCS: dict[str, tuple[str, str]] = {
    "SUM": ("SUM({0})", "column"),
    "AVERAGE": ("AVG({0})", "column"),
    "MIN": ("MIN({0})", "column"),
    "MAX": ("MAX({0})", "column"),
    "COUNT": ("COUNT({0})", "column"),
    "COUNTA": ("COUNT({0})", "column"),
    "DISTINCTCOUNT": ("COUNT(DISTINCT {0})", "column"),
    "COUNTROWS": ("COUNT(*)", "table"),
    "DIVIDE": ("({0}) / NULLIF(({1}), 0)", "expr2"),
    "ABS": ("ABS({0})", "expr"),
    "ROUND": ("ROUND({0}, {1})", "expr2"),
}

TABLEAU_FUNCS: dict[str, tuple[str, str]] = {
    "SUM": ("SUM({0})", "column"),
    "AVG": ("AVG({0})", "column"),
    "MIN": ("MIN({0})", "column"),
    "MAX": ("MAX({0})", "column"),
    "COUNT": ("COUNT({0})", "column"),
    "COUNTD": ("COUNT(DISTINCT {0})", "column"),
    "ZN": ("COALESCE({0}, 0)", "expr"),
    "IFNULL": ("COALESCE({0}, {1})", "expr2"),
    "ABS": ("ABS({0})", "expr"),
    "ROUND": ("ROUND({0}, {1})", "expr2"),
}

# Anything that rewrites filter context or walks a table row by row cannot be expressed as a plain
# single-table SQL aggregate. Listed explicitly so the failure message is useful.
DAX_UNSUPPORTED = {
    "CALCULATETABLE", "ALL", "ALLEXCEPT", "ALLSELECTED", "FILTER", "EARLIER", "RELATED", "RELATEDTABLE",
    "USERELATIONSHIP", "CROSSFILTER", "SUMX", "AVERAGEX", "COUNTX", "MINX", "MAXX", "RANKX", "TOPN",
    "DATESYTD", "DATESQTD", "DATESMTD", "TOTALYTD", "TOTALQTD", "TOTALMTD", "SAMEPERIODLASTYEAR",
    "PARALLELPERIOD", "DATEADD", "DATESBETWEEN", "DATESINPERIOD", "PREVIOUSMONTH", "PREVIOUSYEAR",
    "NEXTMONTH", "NEXTYEAR", "FIRSTDATE", "LASTDATE", "OPENINGBALANCEMONTH", "CLOSINGBALANCEYEAR",
    "VALUES", "DISTINCT", "SELECTEDVALUE", "HASONEVALUE", "ISFILTERED", "ISCROSSFILTERED",
    "SUMMARIZE", "ADDCOLUMNS", "GENERATE", "UNION", "EXCEPT", "INTERSECT", "LOOKUPVALUE",
}
TABLEAU_UNSUPPORTED = {
    "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX", "WINDOW_COUNT", "RUNNING_SUM",
    "RUNNING_AVG", "INDEX", "RANK", "RANK_DENSE", "FIRST", "LAST", "LOOKUP", "TOTAL", "SIZE",
    "PREVIOUS_VALUE", "SCRIPT_REAL", "SCRIPT_STR", "SCRIPT_INT", "SCRIPT_BOOL",
}

AGG_MARKERS = re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT)\s*\(", re.I)


# ---------------------------------------------------------------- tokenizer
TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<qtable>'(?:[^']|'')*'\s*\[[^\]]*\])     # 'Table Name'[Column]
  | (?P<btable>[A-Za-z_][\w]*\s*\[[^\]]*\])     # Table[Column]
  | (?P<bracket>\[[^\]]*\])                     # [Column] or [Measure]
  | (?P<qstring>"(?:[^"]|"")*")                 # "text"
  | (?P<sstring>'(?:[^']|'')*')                 # 'Table' (bare, e.g. COUNTROWS('Sales'))
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_][\w.]*)
  | (?P<op><>|<=|>=|=|<|>|\+|\-|\*|/|\(|\)|,|&&|\|\||&)
    """,
    re.X,
)


def _tokenize(src: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:
            raise Untranslatable(f"unrecognised syntax at {src[pos:pos + 24]!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        if kind == "ws":
            continue
        out.append((kind, m.group()))
    return out


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src.strip()


def _split_ref(tok: str) -> tuple[str | None, str]:
    """'Sales'[Revenue] -> ("Sales", "Revenue");  [Revenue] -> (None, "Revenue")."""
    i = tok.index("[")
    table = tok[:i].strip().strip("'").replace("''", "'") or None
    return table, tok[i + 1 : tok.rindex("]")].strip()


def _quote(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


# ---------------------------------------------------------------- parser
class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], funcs: dict, unsupported: set[str],
                 measures: dict[str, str] | None, depth: int, bare_bracket_is_column: bool = False):
        self.t = tokens
        self.i = 0
        self.funcs = funcs
        self.unsupported = unsupported
        self.measures = measures or {}
        self.depth = depth
        # In DAX a bare [Name] is a measure reference; in Tableau it is a field (column) reference
        # that may also name a calculated field.
        self.bare_bracket_is_column = bare_bracket_is_column
        self.tables: set[str] = set()
        # columns reached outside an aggregate call: SQL would reject them, so we refuse up front
        self.bare_columns: set[str] = set()

    # -- token helpers
    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def next(self) -> tuple[str, str]:
        if self.i >= len(self.t):
            raise Untranslatable("unexpected end of formula")
        self.i += 1
        return self.t[self.i - 1]

    def expect(self, value: str) -> None:
        k, v = self.next()
        if v != value:
            raise Untranslatable(f"expected {value!r}, found {v!r}")

    # -- grammar
    def expression(self) -> str:
        left = self.term()
        while (p := self.peek()) and p[1] in ("+", "-"):
            op = self.next()[1]
            left = f"{left} {op} {self.term()}"
        return left

    def term(self) -> str:
        left = self.factor()
        while (p := self.peek()) and p[1] in ("*", "/"):
            op = self.next()[1]
            right = self.factor()
            left = f"{left} / NULLIF({right}, 0)" if op == "/" else f"{left} * {right}"
        return left

    def factor(self) -> str:
        kind, val = self.next()
        if val == "-":
            return f"-{self.factor()}"
        if val == "(":
            inner = self.expression()
            self.expect(")")
            return f"({inner})"
        if kind == "number":
            return val
        if kind == "qstring":
            return "'" + val[1:-1].replace('""', '"').replace("'", "''") + "'"
        if kind in ("qtable", "btable"):
            table, col = _split_ref(val)
            if table:
                self.tables.add(table)
            self.bare_columns.add(col)
            return _quote(col)
        if kind == "bracket":
            _, name = _split_ref(val)
            return self.bracket_ref(name)
        if kind == "sstring":  # a bare 'Table' outside a function call
            raise Untranslatable(f"bare table reference {val}")
        if kind == "name":
            nxt = self.peek()
            if nxt and nxt[1] == "(":
                return self.call(val.upper())
            raise Untranslatable(f"unknown identifier {val!r}")
        raise Untranslatable(f"unexpected token {val!r}")

    def bracket_ref(self, name: str) -> str:
        if name.strip().lower() in self.measures:
            return self.inline_measure(name)
        if self.bare_bracket_is_column:
            self.bare_columns.add(name.strip())
            return _quote(name.strip())
        raise Untranslatable(f"references measure [{name}] which is not in the imported model")

    def inline_measure(self, name: str) -> str:
        """[Other Measure] -> inline its formula, so Profit = [Revenue] - [Cost] translates."""
        key = name.strip().lower()
        if key not in self.measures:
            raise Untranslatable(f"references measure [{name}] which is not in the imported model")
        if self.depth <= 0:
            raise Untranslatable("measure references nested too deeply")
        sub = _Parser(_tokenize(_strip_comments(self.measures[key])), self.funcs, self.unsupported,
                      self.measures, self.depth - 1, self.bare_bracket_is_column)
        sql = sub.expression()
        if sub.peek() is not None:
            raise Untranslatable(f"could not fully parse referenced measure [{name}]")
        self.tables |= sub.tables
        self.bare_columns |= sub.bare_columns
        return f"({sql})"

    def call(self, fname: str) -> str:
        self.expect("(")
        args = self.args()
        if fname in self.unsupported:
            raise Untranslatable(f"{fname}() changes filter/row context and has no exact single-table SQL equivalent")
        if fname not in self.funcs:
            raise Untranslatable(f"unsupported function {fname}()")
        template, kind = self.funcs[fname]
        if kind == "table":
            if len(args) != 1 or args[0][0] not in ("sstring", "name", "raw_table"):
                raise Untranslatable(f"{fname}() expects a single table reference")
            return template
        if kind == "column":
            if len(args) != 1 or args[0][0] != "column":
                raise Untranslatable(f"{fname}() expects a single column reference")
            return template.format(args[0][1])
        if kind == "expr":
            if len(args) != 1:
                raise Untranslatable(f"{fname}() expects one argument")
            return template.format(args[0][1])
        if kind == "expr2":
            if len(args) != 2:
                raise Untranslatable(f"{fname}() expects two arguments")
            return template.format(args[0][1], args[1][1])
        raise Untranslatable(f"unsupported function {fname}()")

    def args(self) -> list[tuple[str, str]]:
        """Returns [(kind, sql)] where kind is 'column', 'sstring', 'name' or 'expr'."""
        out: list[tuple[str, str]] = []
        if (p := self.peek()) and p[1] == ")":
            self.next()
            return out
        while True:
            out.append(self.argument())
            k, v = self.next()
            if v == ")":
                return out
            if v != ",":
                raise Untranslatable(f"expected ',' or ')', found {v!r}")

    def argument(self) -> tuple[str, str]:
        p = self.peek()
        # a lone table reference, e.g. COUNTROWS('Sales')
        if p and p[0] == "sstring":
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
            if nxt and nxt[1] in (",", ")"):
                self.next()
                self.tables.add(p[1].strip("'").replace("''", "'"))
                return ("sstring", "")
        if p and p[0] == "name":
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
            if nxt and nxt[1] in (",", ")"):
                self.next()
                self.tables.add(p[1])
                return ("name", "")
        # a lone column reference
        if p and p[0] in ("qtable", "btable", "bracket"):
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
            if nxt and nxt[1] in (",", ")"):
                kind, val = self.next()
                if kind == "bracket":
                    _, name = _split_ref(val)
                    if name.strip().lower() in self.measures:
                        return ("expr", self.bracket_ref(name))
                    if self.bare_bracket_is_column:
                        return ("column", _quote(name))
                    return ("expr", self.bracket_ref(name))
                table, col = _split_ref(val)
                if table:
                    self.tables.add(table)
                return ("column", _quote(col))
        return ("expr", self.expression())


# ---------------------------------------------------------------- filter extraction
COMPARATORS = {"=": "=", "<>": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


def _translate_filter(src: str) -> tuple[str, set[str]]:
    """Only simple column/value comparisons — `'T'[Status] = "POSTED"` — are accepted."""
    toks = _tokenize(_strip_comments(src))
    if len(toks) != 3:
        raise Untranslatable(f"filter {src.strip()!r} is not a simple column comparison")
    (lk, lv), (ok, ov), (rk, rv) = toks
    if lk not in ("qtable", "btable") or ov not in COMPARATORS:
        raise Untranslatable(f"filter {src.strip()!r} is not a simple column comparison")
    table, col = _split_ref(lv)
    if rk == "qstring":
        value = "'" + rv[1:-1].replace('""', '"').replace("'", "''") + "'"
    elif rk == "number":
        value = rv
    elif rk == "name" and rv.upper() in ("TRUE", "FALSE"):
        value = rv.upper()
    else:
        raise Untranslatable(f"filter {src.strip()!r} compares against an unsupported value")
    return f"{_quote(col)} {COMPARATORS[ov]} {value}", ({table} if table else set())


def _split_top_level(src: str) -> list[str]:
    parts, depth, buf, quote = [], 0, [], ""
    for ch in src:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------- public API
def translate(formula: str, language: str = "dax", measures: dict[str, str] | None = None) -> Translation:
    """Translate a measure formula to a SQL aggregate + optional filter predicate.

    `measures` maps lower-cased measure name -> native formula, so references between measures can be
    inlined. Returns a Translation whose `.ok` is False (with a `.reason`) when exact translation is
    impossible — callers must never fall back to a guess.
    """
    funcs = DAX_FUNCS if language == "dax" else TABLEAU_FUNCS
    unsupported = DAX_UNSUPPORTED if language == "dax" else TABLEAU_UNSUPPORTED
    try:
        src = _strip_comments(formula or "")
        if not src:
            raise Untranslatable("empty formula")
        # a measure often arrives as "Name = FORMULA"
        if language == "dax":
            m = re.match(r"^\s*[\w '\[\]]+?\s*=\s*(?=[A-Za-z(])", src)
            if m and "(" in src[m.end():]:
                src = src[m.end():].strip()

        filters: list[str] = []
        tables: set[str] = set()
        body = src
        if language == "dax":
            cm = re.match(r"^CALCULATE\s*\((.*)\)\s*$", src, re.S | re.I)
            if cm:
                parts = _split_top_level(cm.group(1))
                if not parts:
                    raise Untranslatable("CALCULATE() with no arguments")
                body = parts[0]
                for f in parts[1:]:
                    sql_f, ft = _translate_filter(f)
                    filters.append(sql_f)
                    tables |= ft

        parser = _Parser(_tokenize(body), funcs, unsupported, measures, depth=3,
                         bare_bracket_is_column=(language != "dax"))
        sql = parser.expression()
        if parser.peek() is not None:
            raise Untranslatable(f"trailing tokens after {sql!r}")
        tables |= parser.tables

        if not AGG_MARKERS.search(sql):
            raise Untranslatable("row-level calculation, not an aggregate measure")
        if parser.bare_columns:
            raise Untranslatable(
                f"mixes unaggregated column(s) {', '.join(sorted(parser.bare_columns))} with aggregates")
        if len(tables) > 1:
            raise Untranslatable(f"spans multiple tables ({', '.join(sorted(tables))}) — needs a join")
        return Translation(sql=sql, filters=" AND ".join(filters) or None, tables=tables)
    except Untranslatable as e:
        return Translation(reason=str(e))
    except Exception as e:  # noqa: BLE001 - never let a parser bug fabricate a metric
        return Translation(reason=f"could not parse formula ({type(e).__name__}: {e})")


def demo() -> None:  # pragma: no cover - runnable self-check
    ok = lambda f, lang="dax", m=None: translate(f, lang, m)  # noqa: E731

    t = ok("SUM('Sales'[Revenue])")
    assert t.ok and t.sql == 'SUM("Revenue")' and t.tables == {"Sales"}, t

    t = ok("Total Revenue = SUM('Sales'[Revenue])")
    assert t.ok and t.sql == 'SUM("Revenue")', t

    t = ok("CALCULATE(SUM('Invoices'[Amount]), 'Invoices'[Status] = \"POSTED\")")
    assert t.ok and t.sql == 'SUM("Amount")' and t.filters == "\"Status\" = 'POSTED'", t

    t = ok("COUNTROWS('Orders')")
    assert t.ok and t.sql == "COUNT(*)" and t.tables == {"Orders"}, t

    t = ok("DISTINCTCOUNT('Sales'[CustomerId])")
    assert t.ok and t.sql == 'COUNT(DISTINCT "CustomerId")', t

    t = ok("DIVIDE(SUM('S'[Profit]), SUM('S'[Revenue]))")
    assert t.ok and t.sql == '(SUM("Profit")) / NULLIF((SUM("Revenue")), 0)', t

    t = ok("SUM('S'[Revenue]) - SUM('S'[Cost])")
    assert t.ok and t.sql == 'SUM("Revenue") - SUM("Cost")', t

    # measure references are inlined
    t = ok("[Revenue] - [Cost]", "dax", {"revenue": "SUM('S'[Revenue])", "cost": "SUM('S'[Cost])"})
    assert t.ok and t.sql == '(SUM("Revenue")) - (SUM("Cost"))', t

    # --- must refuse rather than guess
    for bad, needle in [
        ("CALCULATE(SUM('S'[Revenue]), DATESYTD('Date'[Date]))", "DATESYTD"),
        ("TOTALYTD(SUM('S'[Revenue]), 'Date'[Date])", "TOTALYTD"),
        ("SUMX('S', 'S'[Qty] * 'S'[Price])", "SUMX"),
        ("CALCULATE(SUM('S'[Revenue]), ALL('S'))", "ALL"),
        ("SUM('A'[X]) + SUM('B'[Y])", "multiple tables"),
        ("'S'[Price] * 1.2", "row-level"),
        ("[Unknown Measure] * 2", "not in the imported model"),
        ("SUM('S'[Revenue], 'S'[Cost])", "single column"),
    ]:
        t = ok(bad)
        assert not t.ok and needle.lower() in (t.reason or "").lower(), (bad, t)

    # --- tableau
    t = translate("SUM([Sales Amount])", "tableau")
    assert t.ok and t.sql == 'SUM("Sales Amount")', t
    t = translate("COUNTD([Customer ID])", "tableau")
    assert t.ok and t.sql == 'COUNT(DISTINCT "Customer ID")', t
    t = translate("ZN(SUM([Profit]))", "tableau")
    assert t.ok and t.sql == 'COALESCE(SUM("Profit"), 0)', t
    t = translate("WINDOW_SUM(SUM([Sales]))", "tableau")
    assert not t.ok and "WINDOW_SUM" in (t.reason or ""), t
    t = translate("[Price] * [Qty]", "tableau")
    assert not t.ok, t

    print("expressions demo: all assertions passed")


if __name__ == "__main__":
    demo()
