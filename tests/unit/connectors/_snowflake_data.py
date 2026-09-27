"""Shared data of the Snowflake connector tests (T01-23): settings, a key-pair credential
generated at run time (no key material in the tree), Arrow tables and the connector built on
`tests.support.fake_snowflake` (the `snowflake_env` fixture lives in this folder's conftest).
"""

from __future__ import annotations

import datetime
import functools
import json
from decimal import Decimal
from typing import Any

import keyring
import pyarrow as pa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tests.support.fake_snowflake import FakeSnowflake
from tests.unit.connectors._settings_data import snowflake, snowflake_entity

from herness.connectors.settings import SnowflakeSettings
from herness.connectors.snowflake import SnowflakeConnector
from herness.core import secrets

UTC = datetime.UTC
FIXED_NOW = datetime.datetime(2026, 3, 1, tzinfo=UTC)
USER = "HERNESS_SVC"
UNLOCK = "synthetic-unlock-words"  # the PEM encryption words of the tests
GB = 2**30
SELECT_SQL = (
    'SELECT "CC_ID", "NAME", "AMOUNT", "UPDATED_AT" FROM "FINANCE"."PUBLIC"."COST_CENTER"'
    ' WHERE "UPDATED_AT" >= %(since)s AND "UPDATED_AT" < %(until)s'
    ' ORDER BY "UPDATED_AT", "CC_ID"'
)


def naive(*parts: int) -> datetime.datetime:
    """A naive UTC datetime (Snowflake TIMESTAMP_NTZ in a UTC session)."""
    return datetime.datetime(*parts, tzinfo=UTC).replace(tzinfo=None)  # type: ignore[misc]


T0 = naive(2026, 1, 1)


def fixed_now() -> datetime.datetime:
    return FIXED_NOW


@functools.cache
def private_key() -> rsa.RSAPrivateKey:
    """One RSA key per session, generated at run time."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(passphrase: str | None = None) -> str:
    """The key as PKCS#8 PEM text, encrypted when ``passphrase`` is given."""
    enc: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    raw = private_key().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc
    )
    return raw.decode()


def der() -> bytes:
    """The unencrypted DER PKCS#8 bytes the connector must hand to `connect`."""
    return private_key().private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def store_credential(**fields: str) -> None:
    """Store the `secret:snowflake_svc` JSON credential (in-memory keyring expected)."""
    value = {"user": USER, "private_key": pem()} | fields
    keyring.set_password(secrets._SERVICE, "snowflake_svc", json.dumps(value))


def settings(entity: dict[str, Any] | None = None, **extra: Any) -> SnowflakeSettings:
    """A valid section: entity `cost_center` over FINANCE.PUBLIC.COST_CENTER."""
    base = snowflake_entity(columns=["CC_ID", "NAME", "AMOUNT", "UPDATED_AT"])
    data = snowflake(entities={"cost_center": base | (entity or {})}) | extra
    return SnowflakeSettings.model_validate(data)


def connector(
    server: FakeSnowflake, entity: dict[str, Any] | None = None, **extra: Any
) -> SnowflakeConnector:
    return SnowflakeConnector(settings(entity, **extra), clock=fixed_now, connect=server.connect)


SCHEMA = pa.schema(
    [
        pa.field("CC_ID", pa.int64()),
        pa.field("NAME", pa.string()),
        pa.field("AMOUNT", pa.decimal128(12, 2)),
        pa.field("UPDATED_AT", pa.timestamp("ns")),
    ]
)


def table(start: int, rows: int) -> pa.Table:
    """Rows ``start ..< start + rows``: one second apart from `T0`, amount ``i.25``."""
    ids = list(range(start, start + rows))
    return pa.table(
        {
            "CC_ID": ids,
            "NAME": [f"cc{i}" for i in ids],
            "AMOUNT": [Decimal(f"{i}.25") for i in ids],
            "UPDATED_AT": [T0 + datetime.timedelta(seconds=i) for i in ids],
        },
        schema=SCHEMA,
    )


def key_table(keys: list[Any], dtype: pa.DataType | None = None) -> pa.Table:
    return pa.table({"CC_ID": pa.array(keys, dtype)})
