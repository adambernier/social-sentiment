"""Export closed analytical intervals to verified, manifested Parquet objects."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import boto3
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql
from psycopg.types.json import Jsonb

DATASETS = {
    "hourly_sentiment_facts": "bucket_hour",
    "hourly_sentiment_fact_snapshots": "bucket_hour",
    "hourly_pipeline_quality_facts": "bucket_hour",
    "analytical_signal_snapshots": "as_of",
    "market_hourly_facts": "bucket_hour",
    "market_daily_facts": "bucket_day",
    "model_artifacts": "first_seen_at",
    "model_artifact_aliases": "first_seen_at",
    "raw_post_archive_staging": "event_at",
}
EXPORT_SCHEMA_VERSION = 1
BATCH_SIZE = 10_000


@dataclass(frozen=True)
class ExportResult:
    dataset: str
    object_key: str
    row_count: int
    byte_size: int
    sha256: str
    manifest_id: uuid.UUID


def _month_bounds(value: str | None) -> tuple[datetime, datetime]:
    if value:
        start = datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if current_month.month == 1:
            start = current_month.replace(year=current_month.year - 1, month=12)
        else:
            start = current_month.replace(month=current_month.month - 1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _s3_location(prefix: str) -> tuple[str, str]:
    parsed = urlparse(prefix)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("ANALYTICS_S3_PREFIX must be an s3://bucket/prefix URL")
    return parsed.netloc, parsed.path.strip("/")


def _decode_sse_c_key(encoded_key: str) -> bytes:
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("ANALYTICS_SSE_C_KEY_B64 must be valid base64") from error
    if len(key) != 32:
        raise ValueError("ANALYTICS_SSE_C_KEY_B64 must decode to exactly 32 bytes")
    return key


def _arrow_type(postgres_oid: int) -> pa.DataType:
    return {
        16: pa.bool_(),
        20: pa.int64(),
        21: pa.int16(),
        23: pa.int32(),
        700: pa.float32(),
        701: pa.float64(),
        1082: pa.date32(),
        1114: pa.timestamp("us"),
        1184: pa.timestamp("us", tz="UTC"),
    }.get(postgres_oid, pa.string())


def _arrow_schema(description) -> pa.Schema:
    return pa.schema(
        [
            pa.field(column.name, _arrow_type(column.type_code), nullable=True)
            for column in description
        ]
    )


def _normalize_value(value, arrow_type: pa.DataType):
    if value is None:
        return None
    if pa.types.is_string(arrow_type):
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        if isinstance(value, (uuid.UUID, Decimal)):
            return str(value)
    return value


def _write_parquet(
    cursor,
    output_path: Path,
    time_column: str,
) -> tuple[int, datetime | date | None, datetime | date | None, list[dict]]:
    arrow_schema = _arrow_schema(cursor.description)
    schema_metadata = [
        {
            "name": column.name,
            "postgres_oid": column.type_code,
            "arrow_type": str(field.type),
        }
        for column, field in zip(cursor.description, arrow_schema)
    ]
    row_count = 0
    min_event = None
    max_event = None
    event_index = next(
        index
        for index, column in enumerate(cursor.description)
        if column.name == time_column
    )

    with pq.ParquetWriter(
        output_path,
        arrow_schema,
        compression="zstd",
        write_statistics=True,
    ) as writer:
        while rows := cursor.fetchmany(BATCH_SIZE):
            arrays = []
            for index, field in enumerate(arrow_schema):
                values = [
                    _normalize_value(row[index], field.type)
                    for row in rows
                ]
                arrays.append(pa.array(values, type=field.type))
            writer.write_table(pa.Table.from_arrays(arrays, schema=arrow_schema))
            event_values = [row[event_index] for row in rows if row[event_index] is not None]
            if event_values:
                batch_min = min(event_values)
                batch_max = max(event_values)
                min_event = batch_min if min_event is None else min(min_event, batch_min)
                max_event = batch_max if max_event is None else max(max_event, batch_max)
            row_count += len(rows)

    return row_count, min_event, max_event, schema_metadata


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as archive_file:
        for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _export_query(dataset: str, time_column: str) -> sql.Composed:
    pending_filter = (
        sql.SQL(" AND archived_at IS NULL AND staged_at <= %s")
        if dataset == "raw_post_archive_staging"
        else sql.SQL("")
    )
    return sql.SQL("SELECT * FROM {} WHERE {} >= %s AND {} < %s").format(
        sql.Identifier(dataset),
        sql.Identifier(time_column),
        sql.Identifier(time_column),
    ) + pending_filter + sql.SQL(" ORDER BY {} ").format(sql.Identifier(time_column))


def export_dataset(
    conn: psycopg.Connection,
    s3_client,
    *,
    bucket: str,
    key_prefix: str,
    dataset: str,
    start: datetime,
    end: datetime,
    workdir: Path,
    sse_customer_key: bytes,
) -> ExportResult:
    time_column = DATASETS[dataset]
    stage_cutoff = datetime.now(timezone.utc)
    output_path = workdir / f"{dataset}.parquet"

    with conn.cursor() as cursor:
        params: list = [start, end]
        if dataset == "raw_post_archive_staging":
            params.append(stage_cutoff)
        cursor.execute(_export_query(dataset, time_column), params)
        row_count, min_event, max_event, schema_metadata = _write_parquet(
            cursor,
            output_path,
            time_column,
        )

    checksum = _sha256(output_path)
    byte_size = output_path.stat().st_size
    relative_key = (
        f"{dataset}/year={start.year:04d}/month={start.month:02d}/"
        f"part-{checksum}.parquet"
    )
    object_key = "/".join(part for part in (key_prefix, relative_key) if part)
    object_metadata = {
        "sha256": checksum,
        "dataset": dataset,
        "schema-version": str(EXPORT_SCHEMA_VERSION),
    }

    s3_client.upload_file(
        str(output_path),
        bucket,
        object_key,
        ExtraArgs={
            "ContentType": "application/vnd.apache.parquet",
            "Metadata": object_metadata,
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": sse_customer_key,
        },
    )
    head = s3_client.head_object(
        Bucket=bucket,
        Key=object_key,
        SSECustomerAlgorithm="AES256",
        SSECustomerKey=sse_customer_key,
    )
    if head["ContentLength"] != byte_size:
        raise RuntimeError(f"uploaded size mismatch for {object_key}")
    if head.get("Metadata", {}).get("sha256") != checksum:
        raise RuntimeError(f"uploaded checksum metadata mismatch for {object_key}")

    manifest_metadata = {
        "columns": schema_metadata,
        "compression": "zstd",
        "selection_cutoff": stage_cutoff.isoformat()
        if dataset == "raw_post_archive_staging"
        else None,
    }
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO archive_manifests (
                dataset, object_key, schema_version, partition_start,
                partition_end, row_count, min_event_at, max_event_at,
                sha256, byte_size, object_etag, object_version_id,
                verified_at, metadata
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, NOW(), %s
            )
            ON CONFLICT (object_key) DO UPDATE SET
                row_count = EXCLUDED.row_count,
                byte_size = EXCLUDED.byte_size,
                object_etag = EXCLUDED.object_etag,
                object_version_id = EXCLUDED.object_version_id,
                verified_at = NOW(),
                metadata = EXCLUDED.metadata
            RETURNING manifest_id
            """,
            [
                dataset,
                object_key,
                EXPORT_SCHEMA_VERSION,
                start,
                end,
                row_count,
                min_event,
                max_event,
                checksum,
                byte_size,
                head.get("ETag", "").strip('"') or None,
                head.get("VersionId"),
                Jsonb(manifest_metadata),
            ],
        )
        manifest_id = cursor.fetchone()[0]
        if dataset == "raw_post_archive_staging":
            cursor.execute(
                """
                UPDATE raw_post_archive_staging
                SET archived_at = NOW(), manifest_id = %s
                WHERE event_at >= %s AND event_at < %s
                  AND archived_at IS NULL AND staged_at <= %s
                """,
                [manifest_id, start, end, stage_cutoff],
            )

    return ExportResult(
        dataset=dataset,
        object_key=object_key,
        row_count=row_count,
        byte_size=byte_size,
        sha256=checksum,
        manifest_id=manifest_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="Closed UTC month in YYYY-MM format")
    parser.add_argument(
        "--dataset",
        action="append",
        choices=sorted(DATASETS),
        dest="datasets",
        help="Dataset to export; repeat as needed (default: all)",
    )
    parser.add_argument(
        "--purge-archived-staging-days",
        type=int,
        help="Delete already-archived staging rows older than this many days",
    )
    args = parser.parse_args()

    database_dsn = os.environ["DATABASE_DSN"]
    s3_prefix = os.environ["ANALYTICS_S3_PREFIX"]
    sse_customer_key = _decode_sse_c_key(
        os.environ["ANALYTICS_SSE_C_KEY_B64"]
    )
    bucket, key_prefix = _s3_location(s3_prefix)
    start, end = _month_bounds(args.month)
    if end > datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0):
        raise SystemExit("Only closed months may be exported")

    s3_client = boto3.client(
        "s3",
        endpoint_url=os.environ.get("SPACES_ENDPOINT"),
        region_name=os.environ.get("SPACES_REGION", "us-east-1"),
    )
    datasets = args.datasets or list(DATASETS)

    with psycopg.connect(database_dsn, autocommit=True) as conn:
        with tempfile.TemporaryDirectory(prefix="sentiment-archive-") as temp_dir:
            for dataset in datasets:
                result = export_dataset(
                    conn,
                    s3_client,
                    bucket=bucket,
                    key_prefix=key_prefix,
                    dataset=dataset,
                    start=start,
                    end=end,
                    workdir=Path(temp_dir),
                    sse_customer_key=sse_customer_key,
                )
                print(
                    f"{result.dataset}: {result.row_count} rows, "
                    f"{result.byte_size} bytes -> s3://{bucket}/{result.object_key} "
                    f"(sha256={result.sha256})"
                )

        if args.purge_archived_staging_days is not None:
            if args.purge_archived_staging_days < 1:
                raise SystemExit("purge retention must be at least one day")
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM raw_post_archive_staging
                    WHERE archived_at IS NOT NULL
                      AND archived_at < NOW() - make_interval(days => %s)
                    """,
                    [args.purge_archived_staging_days],
                )
                print(f"purged {cursor.rowcount} archived staging rows")


if __name__ == "__main__":
    main()
