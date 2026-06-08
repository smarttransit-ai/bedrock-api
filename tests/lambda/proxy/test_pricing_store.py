"""Tests for the S3-backed live pricing catalog (pricing_store)."""

import os

import boto3
import pytest
from pricing_store import (
    PRICING_OBJECT_KEY,
    load_live_catalog,
    load_live_meta,
    save_live_catalog,
)


def _s3():
    return boto3.client("s3", region_name="us-east-1")


def test_save_then_load_round_trip(pricing_bucket):
    s3 = _s3()
    catalog = {"m": {"on_demand": {"input_usd_micros_per_1k": 1}}}
    save_live_catalog(s3, catalog, {"entry_count": 1})
    assert load_live_catalog(s3) == catalog


def test_load_absent_object_returns_none(pricing_bucket):
    assert load_live_catalog(_s3()) is None


def test_load_meta_round_trip(pricing_bucket):
    s3 = _s3()
    save_live_catalog(s3, {"m": {"on_demand": {}}}, {"entry_count": 1, "fetched_at": "t"})
    assert load_live_meta(s3) == {"entry_count": 1, "fetched_at": "t"}


def test_load_meta_absent_returns_none(pricing_bucket):
    assert load_live_meta(_s3()) is None


def test_load_absent_bucket_returns_none():
    # No pricing_bucket fixture → bucket doesn't exist → NoSuchBucket → None.
    assert load_live_catalog(_s3()) is None


def test_load_malformed_object_raises(pricing_bucket):
    s3 = _s3()
    s3.put_object(Bucket=os.environ["PRICING_BUCKET"], Key=PRICING_OBJECT_KEY, Body=b"not json")
    with pytest.raises(Exception):
        load_live_catalog(s3)
