"""Append-only DuckDB fact versions and auditable point-in-time selection."""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Iterable

from .models import Fact, Query, Retrieval

_COLUMNS = tuple(f.name for f in fields(Fact))
_SELECT = ", ".join(_COLUMNS)


def select_latest(query: Query, facts: Iterable[Fact], backend: str = "bitemporal") -> Retrieval:
    """Dependency-free reference implementation of point-in-time selection."""
    eligible = [fact for fact in facts if fact.cik == query.cik and fact.concept == query.concept
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
                self.connection.execute("SELECT count(*) FROM facts").fetchone()
            else:
                self._create_schema()
        except duckdb.Error as exc:
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
        before = self.connection.execute("SELECT count(*) FROM facts").fetchone()[0]
        self.connection.execute("BEGIN TRANSACTION")
        try:
            placeholders = ", ".join("?" for _ in _COLUMNS)
            self.connection.executemany(
                f"INSERT INTO facts ({_SELECT}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                [tuple(getattr(fact, key) for key in _COLUMNS) for fact in batch],
            )
            self.connection.executemany(
                "INSERT INTO fact_sources VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [(fact.fact_id, fact.source_sha256, fact.source_url) for fact in batch],
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
