from datetime import datetime, timezone
from types import SimpleNamespace

import pyarrow as pa
from export import (
    DATASETS,
    _arrow_schema,
    _decode_sse_c_key,
    _month_bounds,
    _s3_location,
    _write_parquet,
)


def test_s3_location_requires_bucket_url():
    assert _s3_location("s3://sentiment-archive/analytics/prod") == (
        "sentiment-archive",
        "analytics/prod",
    )


def test_archive_includes_reproducibility_and_quality_datasets():
    assert DATASETS["analytical_signal_snapshots"] == "as_of"
    assert DATASETS["hourly_pipeline_quality_facts"] == "bucket_hour"
    assert DATASETS["model_artifacts"] == "first_seen_at"
    assert DATASETS["model_artifact_aliases"] == "first_seen_at"


def test_sse_c_key_must_be_base64_encoded_32_bytes():
    encoded_key = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

    assert _decode_sse_c_key(encoded_key) == bytes(range(32))


def test_month_bounds_are_closed():
    start, end = _month_bounds("2026-07")

    assert start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_postgres_types_get_explicit_arrow_schema():
    schema = _arrow_schema(
        [
            SimpleNamespace(name="count", type_code=20),
            SimpleNamespace(name="bucket_hour", type_code=1184),
            SimpleNamespace(name="metadata", type_code=3802),
        ]
    )

    assert schema.field("count").type == pa.int64()
    assert schema.field("bucket_hour").type == pa.timestamp("us", tz="UTC")
    assert schema.field("metadata").type == pa.string()


def test_parquet_event_range_uses_named_time_column(tmp_path):
    first = datetime(2026, 7, 1, tzinfo=timezone.utc)
    second = datetime(2026, 7, 2, tzinfo=timezone.utc)

    class Cursor:
        def __init__(self):
            self.description = [
                SimpleNamespace(name="maintenance_run_id", type_code=2950),
                SimpleNamespace(name="bucket_hour", type_code=1184),
            ]
            self.returned = False

        def fetchmany(self, _size):
            if self.returned:
                return []
            self.returned = True
            return [("run-1", second), ("run-2", first)]

    output_path = tmp_path / "snapshot.parquet"
    count, min_event, max_event, _schema = _write_parquet(
        Cursor(),
        output_path,
        "bucket_hour",
    )

    assert count == 2
    assert min_event == first
    assert max_event == second
    assert output_path.stat().st_size > 0
