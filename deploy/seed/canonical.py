"""Seed the canonical dev scenario (S07, src §35.9) into the dev data containers.

    uv run python deploy/seed/canonical.py --rows 100000 --duplicates 417 [--seed 7]

Deterministic and idempotent: tables are dropped and recreated on every run. Values are synthetic.
Admin credentials come from ``.env`` (``WAREHOUSE_PG_PASSWORD``, ``MYSQL_ROOT_PASSWORD``) and are
never printed.

* ``sales.customer`` (MySQL, source): ``rows`` customers, ``customer_id`` primary key.
* ``dw.customer`` (PostgreSQL, target): the same customers plus ``duplicates`` re-loaded keys, so
  ``rows + duplicates`` rows with ``duplicates`` duplicate ``customer_id`` values.
* ``dw.sales_daily`` (PostgreSQL): range-partitioned by ``sale_date`` with one partition per month
  given in ``--months``.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import warnings
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import aiomysql  # type: ignore[import-untyped]
import asyncpg
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

FIRST_NAMES = ("Ada", "Ben", "Chloe", "Dev", "Esme", "Femi", "Gus", "Hana", "Ivo", "Juno")
LAST_NAMES = ("Archer", "Brook", "Cole", "Dale", "Ennis", "Frost", "Gale", "Hart", "Iles", "Jory")
COUNTRIES = ("GB", "US", "DE", "FR", "IN", "NL", "ES", "IE")
BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
BATCH = 5_000


class SeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    warehouse_pg_password: SecretStr
    warehouse_pg_port: int = Field(default=55433)
    mysql_root_password: SecretStr
    mysql_port: int = Field(default=13306)


Customer = tuple[int, str, str, str, str, datetime]


def customers(rows: int, rng: random.Random) -> list[Customer]:
    out: list[Customer] = []
    for cid in range(1, rows + 1):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        email = f"customer{cid}@example.test"
        phone = f"+1-555-{cid:07d}"
        updated = BASE_TIME + timedelta(minutes=rng.randrange(0, 60 * 24 * 200))
        out.append((cid, name, email, phone, rng.choice(COUNTRIES), updated))
    return out


def month_bounds(month: str) -> tuple[date, date]:
    start = datetime.strptime(month, "%Y-%m").date()
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, end


async def seed_mysql(settings: SeedSettings, rows: list[Customer]) -> None:
    conn = await aiomysql.connect(
        host="127.0.0.1",
        port=settings.mysql_port,
        user="root",
        password=settings.mysql_root_password.get_secret_value(),
        db="sales",
        connect_timeout=10,
        autocommit=False,
        charset="utf8mb4",
    )
    try:
        async with conn.cursor() as cur:
            with warnings.catch_warnings():  # MySQL warns when the table does not exist yet
                warnings.simplefilter("ignore")
                await cur.execute("DROP TABLE IF EXISTS sales.customer")
            await cur.execute(
                "CREATE TABLE sales.customer ("
                " customer_id BIGINT PRIMARY KEY, name VARCHAR(100) NOT NULL,"
                " email VARCHAR(200) NOT NULL, phone VARCHAR(40) NOT NULL,"
                " country CHAR(2) NOT NULL, updated_at DATETIME(6) NOT NULL)"
            )
            plain = [(*r[:5], r[5].replace(tzinfo=None)) for r in rows]
            for i in range(0, len(plain), BATCH):
                await cur.executemany(
                    "INSERT INTO sales.customer VALUES (%s, %s, %s, %s, %s, %s)",
                    plain[i : i + BATCH],
                )
        await conn.commit()
    finally:
        conn.close()


async def seed_postgres(
    settings: SeedSettings, rows: list[Customer], duplicates: int, months: list[str], seed: int
) -> None:
    rng = random.Random(seed + 1)  # noqa: S311 - reproducible synthetic data, not security
    reloaded = sorted(rng.sample(range(len(rows)), duplicates))
    target = rows + [rows[i] for i in reloaded]
    conn = await asyncpg.connect(
        host="127.0.0.1",
        port=settings.warehouse_pg_port,
        user="warehouse_admin",
        password=settings.warehouse_pg_password.get_secret_value(),
        database="warehouse",
        timeout=10,
    )
    try:
        async with conn.transaction():
            # Objects are owned by dw_owner so the role grants (default privileges) apply.
            await conn.execute("SET LOCAL ROLE dw_owner")
            await conn.execute("DROP TABLE IF EXISTS dw.customer, dw.sales_daily CASCADE")
            await conn.execute(
                "CREATE TABLE dw.customer ("
                " load_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
                " customer_id BIGINT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL,"
                " phone TEXT NOT NULL, country CHAR(2) NOT NULL,"
                " updated_at TIMESTAMPTZ NOT NULL, loaded_at TIMESTAMPTZ NOT NULL)"
            )
            loaded_at = BASE_TIME + timedelta(days=200)
            await conn.copy_records_to_table(
                "customer",
                schema_name="dw",
                columns=[
                    "customer_id",
                    "name",
                    "email",
                    "phone",
                    "country",
                    "updated_at",
                    "loaded_at",
                ],
                records=[(*r, loaded_at) for r in target],
            )
            await conn.execute(
                "CREATE TABLE dw.sales_daily ("
                " sale_date DATE NOT NULL, store_id INT NOT NULL, amount NUMERIC(12, 2) NOT NULL)"
                " PARTITION BY RANGE (sale_date)"
            )
            sales: list[tuple[date, int, Decimal]] = []
            for month in months:
                start, end = month_bounds(month)
                name = f"sales_daily_{start:%Y_%m}"
                await conn.execute(
                    f"CREATE TABLE dw.{name} PARTITION OF dw.sales_daily"
                    f" FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                )
                day = start
                while day < end:
                    for store in range(1, 6):
                        sales.append((day, store, Decimal(rng.randrange(10_000, 500_000)) / 100))
                    day += timedelta(days=1)
            await conn.copy_records_to_table("sales_daily", schema_name="dw", records=sales)
    finally:
        await conn.close()


async def main_async(args: argparse.Namespace) -> None:
    settings = SeedSettings()
    rows = customers(args.rows, random.Random(args.seed))  # noqa: S311 - reproducible data
    await seed_mysql(settings, rows)
    await seed_postgres(settings, rows, args.duplicates, args.months, args.seed)
    print(
        f"seeded sales.customer={len(rows)} dw.customer={len(rows) + args.duplicates}"
        f" (duplicate keys={args.duplicates}) dw.sales_daily partitions={len(args.months)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--duplicates", type=int, default=417)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--months", nargs="+", default=["2026-08", "2026-09"])
    args = parser.parse_args(argv)
    if not 1 <= args.duplicates <= args.rows:
        parser.error("--duplicates must be between 1 and --rows")
    for month in args.months:
        try:
            month_bounds(month)
        except ValueError:
            parser.error(f"--months entries must be YYYY-MM, got {month!r}")
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
