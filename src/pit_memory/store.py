"""Append-only DuckDB fact versions and auditable point-in-time selection."""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Iterable

from .models import Fact, Query, Retrieval, normalize_cik

_COLUMNS = tuple(f.name for f in fields(Fact))
_SELECT = ", ".join(_COLUMNS)
_UNNEST_FACTS = ", ".join(f"UNNEST(?::{'DOUBLE' if name == 'value' else 'VARCHAR'}[]) AS {name}"
                          for name in _COLUMNS)
# One filing's reported value: everything that identifies a fact except the
# derived availability date and the provenance fields.
_FILING_KEY = ("cik", "concept", "unit", "value", "period_start", "period_end", "filed", "accession", "form")


def select_latest(query: Query, facts: Iterable[Fact], backend: str = "bitemporal") -> Retrieval:
    """Dependency-free reference implementation of point-in-time selection."""
    unique: dict[str, Fact] = {}
    for fact in facts:  # one row per fact_id, as the DuckDB primary key keeps
        unique.setdefault(fact.fact_id, fact)
    eligible = [fact for fact in unique.values() if fact.cik == query.cik and fact.concept == query.concept
                and fact.unit == query.unit and fact.available_on <= query.as_of
                and fact.period_end <= query.as_of
                and (query.period_end is None or fact.period_end == query.period_end)]
    if not eligible:
        return Retrieval(query.query_id, backend, None, (), "not_known", "No eligible annual fact was available by the cutoff")
    best_key = max((fact.period_end, fact.filed) for fact in eligible)
    context = tuple(sorted((fact for fact in eligible if (fact.period_end, fact.filed) == best_key), key=lambda fact: fact.fact_id))
    if len({(fact.period_start, fact.value) for fact in context}) > 1:
        return Retrieval(query.query_id, backend, None, context, "ambiguous", "Conflicting facts share period end and filing date; acceptance-time order is unavailable")
    return Retrieval(query.query_id, backend, context[0], context, "known")


_DUCKDB_SIGNATURE = b"DUCK"  # bytes 8-11 of every DuckDB database file


class DuckDBStore:
    """Append-only fact versions in a DuckDB file.

    ``read_only=True`` opens an existing database without taking the write
    lock, so several readers (for example parallel ``query`` calls) can share it.
    """

    def __init__(self, path: str | Path = ":memory:", read_only: bool = False) -> None:
        import duckdb

        target = str(path)
        if target != ":memory:":
            file = Path(path)
            if file.is_dir():
                raise ValueError(f"{file} is a directory, not a DuckDB database file")
            if file.is_file() and file.stat().st_size > 0:
                with file.open("rb") as handle:
                    if handle.read(12)[8:12] != _DUCKDB_SIGNATURE:
                        # DuckDB would silently open some formats (JSON, CSV) as an
                        # empty in-memory database and lose every write.
                        raise ValueError(f"{file} is not a DuckDB database file")
            elif read_only:
                raise ValueError(f"Database does not exist: {file}")
            else:
                file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.connection = duckdb.connect(target, read_only=read_only)
            if read_only:
                self._validate_schema()
            else:
                # A different/incomplete DuckDB schema must not be partly
                # modified if initialization fails halfway through.
                self.connection.execute("BEGIN TRANSACTION")
                try:
                    self._create_schema()
                    self._validate_schema()
                    self.connection.execute("COMMIT")
                except Exception:
                    self.connection.execute("ROLLBACK")
                    raise
        except (duckdb.Error, ValueError) as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise RuntimeError(f"Could not open DuckDB database {target}: {exc}") from exc

    def _create_schema(self) -> None:
        self.connection.execute("""
          CREATE TABLE IF NOT EXISTS facts (
            cik VARCHAR NOT NULL, concept VARCHAR NOT NULL, unit VARCHAR NOT NULL,
            value DOUBLE NOT NULL, period_start VARCHAR NOT NULL, period_end VARCHAR NOT NULL,
            filed VARCHAR NOT NULL, accession VARCHAR NOT NULL, form VARCHAR NOT NULL,
            available_on VARCHAR NOT NULL, fact_id VARCHAR PRIMARY KEY,
            source_url VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
            CHECK (period_start <= period_end AND period_end <= filed AND filed < available_on)
          )
        """)
        self.connection.execute("""
          CREATE TABLE IF NOT EXISTS fact_sources (
            fact_id VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL,
            source_url VARCHAR NOT NULL,
            PRIMARY KEY (fact_id, source_sha256, source_url)
          )
        """)
        self.connection.execute("CREATE INDEX IF NOT EXISTS facts_lookup ON facts(cik, concept, unit, available_on)")

    def _validate_schema(self) -> None:
        # Merely selecting the column names accepts a table copied with CREATE
        # TABLE AS (which loses the primary key), or one whose value is TEXT.
        # Those databases cannot preserve idempotent appends and typed facts.
        for table, columns, primary_key in (
            ("facts", _COLUMNS, {"fact_id"}),
            ("fact_sources", ("fact_id", "source_sha256", "source_url"),
             {"fact_id", "source_sha256", "source_url"}),
        ):
            actual = {row[1]: (row[2], row[3], row[5]) for row in
                      self.connection.execute(f"PRAGMA table_info('{table}')").fetchall()}
            expected = {name: ("DOUBLE" if name == "value" else "VARCHAR", True,
                               name in primary_key) for name in columns}
            if actual != expected:
                raise ValueError(f"Incompatible {table} schema: expected typed, non-null columns and primary key")

    def __enter__(self) -> "DuckDBStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def append(self, facts: Iterable[Fact]) -> int:
        batch = list(facts)
        if not batch:
            return 0
        # Fully validate before the first write. The transaction includes source
        # links so failures do not leave partially imported snapshots.
        batch = [Fact(**fact.to_dict()) for fact in batch]
        # The first row of a repeated fact keeps its provenance in `facts`, as a
        # row-by-row insert would; every distinct source link is recorded.
        first: dict[str, Fact] = {}
        for fact in batch:
            first.setdefault(fact.fact_id, fact)
        links = list(dict.fromkeys((fact.fact_id, fact.source_sha256, fact.source_url) for fact in batch))
        # available_on is derived from `filed` by the exchange calendar and is
        # part of the content hash. A calendar update (for example a newly
        # announced closure) would otherwise add a second row for the same
        # filing value and leave the stale date eligible forever.
        dates: dict[tuple, str] = {}
        for fact in first.values():
            key = tuple(getattr(fact, name) for name in _FILING_KEY)
            if dates.setdefault(key, fact.available_on) != fact.available_on:
                raise ValueError(f"Two availability dates for one filing value ({fact.accession}, {fact.concept}); "
                                 "facts must come from one exchange calendar")
        columns = [[getattr(fact, key) for fact in first.values()] for key in _COLUMNS]
        before = self.connection.execute("SELECT count(*) FROM facts").fetchone()[0]
        self.connection.execute("BEGIN TRANSACTION")
        try:
            clash = self.connection.execute(
                f"SELECT n.accession, n.concept, f.available_on, n.available_on "
                f"FROM (SELECT {_UNNEST_FACTS}) n JOIN facts f USING ({', '.join(_FILING_KEY)}) "
                "WHERE f.available_on <> n.available_on LIMIT 1", columns).fetchone()
            if clash:
                raise ValueError(f"Filing {clash[0]} ({clash[1]}) is stored as available on {clash[2]} but now "
                                 f"computes to {clash[3]}; the exchange calendar changed, so rebuild the database "
                                 "instead of appending")
            # One set-based statement per table: DuckDB unnests the column
            # arrays in step. Row-by-row executemany took about 3 ms per row,
            # minutes for one large issuer and most of an hour for 40.
            self.connection.execute(
                f"INSERT INTO facts ({_SELECT}) SELECT {_SELECT} FROM (SELECT {_UNNEST_FACTS}) "
                "ON CONFLICT DO NOTHING", columns)
            self.connection.execute(
                "INSERT INTO fact_sources (fact_id, source_sha256, source_url) "
                "SELECT * FROM (SELECT UNNEST(?::VARCHAR[]) AS fact_id, "
                "UNNEST(?::VARCHAR[]) AS source_sha256, UNNEST(?::VARCHAR[]) AS source_url) "
                "ON CONFLICT DO NOTHING",
                [list(column) for column in zip(*links)],
            )
            after = self.connection.execute("SELECT count(*) FROM facts").fetchone()[0]
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return after - before

    def all_facts(self) -> list[Fact]:
        rows = self.connection.execute(f"SELECT {_SELECT} FROM facts ORDER BY cik, concept, unit, period_end, filed, fact_id").fetchall()
        return [Fact(**dict(zip(_COLUMNS, row))) for row in rows]

    def select_facts(self, *, ciks: Iterable[str | int] | None = None, concepts: Iterable[str] | None = None,
                     unit: str | None = None) -> list[Fact]:
        """Stored versions matching every given filter, in ``all_facts`` order.

        Commands that need one issuer, concept or unit load only those rows;
        building every Fact of a full database takes seconds and a gigabyte.
        """
        filters: list[str] = []
        params: list[str] = []
        for column, values in (("cik", None if ciks is None else {normalize_cik(c) for c in ciks}),
                               ("concept", None if concepts is None else set(concepts))):
            if values is None:
                continue
            if not values:
                return []
            filters.append(f"{column} IN ({', '.join('?' for _ in values)})")
            params.extend(sorted(values))
        if unit is not None:
            filters.append("unit = ?")
            params.append(unit)
        where = f"WHERE {' AND '.join(filters)} " if filters else ""
        rows = self.connection.execute(
            f"SELECT {_SELECT} FROM facts {where}ORDER BY cik, concept, unit, period_end, filed, fact_id",
            params).fetchall()
        return [Fact(**dict(zip(_COLUMNS, row))) for row in rows]

    def scoped_facts(self, cik: str | int, concept: str, unit: str) -> list[Fact]:
        """Every stored version for one issuer, concept and unit, in ``all_facts`` order.

        All backends scope by exactly these three fields first, so a comparison
        backend given this subset answers as it would with the whole database.
        """
        return self.select_facts(ciks=[cik], concepts=[concept], unit=unit)

    def count(self) -> int:
        return self.connection.execute("SELECT count(*) FROM facts").fetchone()[0]

    def sources(self, fact_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT source_sha256, source_url FROM fact_sources WHERE fact_id=? ORDER BY source_sha256, source_url", [fact_id]
        ).fetchall()
        return [{"source_sha256": sha, "source_url": url} for sha, url in rows]

    def retrieve(self, query: Query, backend: str = "bitemporal") -> Retrieval:
        if backend not in {"pit", "bitemporal"}:
            raise ValueError("DuckDBStore implements only the point-in-time backend")
        filters = "cik=? AND concept=? AND unit=? AND available_on<=? AND period_end<=?"
        params = [query.cik, query.concept, query.unit, query.as_of, query.as_of]
        if query.period_end is not None:
            filters += " AND period_end=?"
            params.append(query.period_end)
        # Rank end first, then known date. ASOF alone is not enough because a
        # later filing may restate an older fiscal period.
        rows = self.connection.execute(f"""
          SELECT {_SELECT} FROM facts WHERE {filters}
          QUALIFY dense_rank() OVER (ORDER BY period_end DESC, filed DESC) = 1
          ORDER BY fact_id
        """, params).fetchall()
        context = tuple(Fact(**dict(zip(_COLUMNS, row))) for row in rows)
        if not context:
            return Retrieval(query.query_id, backend, None, (), "not_known", "No eligible annual fact was available by the cutoff")
        if len({(f.period_start, f.value) for f in context}) > 1:
            return Retrieval(query.query_id, backend, None, context, "ambiguous", "Conflicting facts share period end and filing date; acceptance-time order is unavailable")
        return Retrieval(query.query_id, backend, context[0], context, "known")
