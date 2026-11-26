"""Tests for api/thread_store.py's S3 offload of oversized run_body payloads.

Regression coverage for a real, confirmed-live failure: compiled_inputs/montes'
combined loan_file_xml + manifest_json + eligibility_json payload is ~455KB,
over DynamoDB's 400KB hard item-size cap, so DynamoDBThreadStore.create_run()'s
put_item() call failed with ValidationException ("Item size has exceeded the
maximum allowed size") before a run record -- or even a run_id -- ever existed,
surfacing to the client as a bare 500 before the agent was ever invoked. See
docs/pc-accuracy-uat-status.md and docs/tickets-4275-4278-acceptance-criteria-proof.md
for the live-confirmed root cause.

Fix: reuse the SAME S3 bucket api/checkpointer.py's DynamoDBSaver already
offloads oversized checkpoints into (CHECKPOINT_S3_BUCKET env var, already
IAM-granted to both the Lambda and the ECS worker task role in
infra/stacks/predicted_conditions_stack.py) -- when run_body serializes to
more than _INPUT_PAYLOAD_S3_THRESHOLD_BYTES, upload it there and store a
small S3 pointer (input_payload_ref) in DynamoDB instead of the payload
itself. get_run() transparently resolves that pointer back to input_payload
on read, so no caller (load_persisted_run, execute_background_run's Lambda
self-invoke dispatch, etc.) needs any changes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

from api.thread_store import (
    DynamoDBThreadStore,
    _INPUT_PAYLOAD_S3_THRESHOLD_BYTES,
)


def _make_store(s3_bucket: str = "test-checkpoint-bucket") -> tuple[DynamoDBThreadStore, MagicMock, MagicMock]:
    """Same __new__-bypass-__init__ pattern as
    test_thread_store_decimal_conversion.py's _make_store, extended with the
    new S3 attributes this fix adds."""
    store = DynamoDBThreadStore.__new__(DynamoDBThreadStore)
    mock_table = MagicMock()
    mock_s3 = MagicMock()
    store._table = mock_table
    store._region_name = "us-east-2"
    store._s3_bucket = s3_bucket
    store._s3 = mock_s3
    return store, mock_table, mock_s3


class TestSmallPayloadStaysInline:
    """No regression for the common case: a normal-sized run_body must
    keep going straight into DynamoDB inline, with no S3 call at all --
    exactly the pre-fix behavior for every fixture except montes."""

    def test_small_payload_not_offloaded(self):
        store, mock_table, mock_s3 = _make_store()
        run_body = {"input": {"loan_amount": 450000.5, "borrower": "Michael Kelly"}}

        store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "input_payload" in item
        assert "input_payload_ref" not in item
        mock_s3.put_object.assert_not_called()

    def test_small_payload_put_item_unaffected_by_missing_bucket(self):
        """No CHECKPOINT_S3_BUCKET configured at all (e.g. local dev with
        CHECKPOINT_TABLE_NAME set but not CHECKPOINT_S3_BUCKET) must behave
        identically to before this fix -- always inline."""
        store, mock_table, mock_s3 = _make_store(s3_bucket="")
        run_body = {"input": {"loan_amount": 450000.5}}

        store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "input_payload" in item
        assert "input_payload_ref" not in item
        mock_s3.put_object.assert_not_called()


class TestOversizedPayloadOffloadsToS3:
    """The actual fix: a payload shaped like montes' real ~455KB combined
    loan_file_xml/manifest_json/eligibility_json must go to S3, not inline,
    and DynamoDB must only ever see a small pointer."""

    def _oversized_run_body(self) -> dict:
        # Comfortably over _INPUT_PAYLOAD_S3_THRESHOLD_BYTES once serialized,
        # mirroring montes' real shape (one dominant large string field).
        return {
            "input": {
                "loan_file_xml": "<Loan>" + ("x" * (_INPUT_PAYLOAD_S3_THRESHOLD_BYTES)) + "</Loan>",
                "manifest_json": "{}",
                "eligibility_json": "{}",
            }
        }

    def test_oversized_payload_calls_s3_put_object_not_dynamodb_inline(self):
        store, mock_table, mock_s3 = _make_store()
        run_body = self._oversized_run_body()

        store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        mock_s3.put_object.assert_called_once()
        put_kwargs = mock_s3.put_object.call_args.kwargs
        assert put_kwargs["Bucket"] == "test-checkpoint-bucket"
        assert put_kwargs["Key"].startswith("runs/")
        assert put_kwargs["Key"].endswith("/input.json")
        assert json.loads(put_kwargs["Body"]) == run_body

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "input_payload" not in item
        assert item["input_payload_ref"] == {
            "bucket": "test-checkpoint-bucket",
            "key": put_kwargs["Key"],
        }

    def test_oversized_payload_without_bucket_falls_back_to_inline(self):
        """No CHECKPOINT_S3_BUCKET configured: an oversized payload must
        still attempt the inline path (and therefore still fail loudly with
        DynamoDB's own ValidationException against a REAL table, exactly as
        it did before this fix) rather than silently dropping data. Against
        this mock table specifically, that just means it goes inline."""
        store, mock_table, mock_s3 = _make_store(s3_bucket="")
        run_body = self._oversized_run_body()

        store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        mock_s3.put_object.assert_not_called()
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "input_payload" in item
        assert "input_payload_ref" not in item

    def test_create_run_return_value_has_the_real_payload_not_a_pointer(self):
        """Caller-facing contract must not change: create_run()'s return
        value always carries the real input_payload, offloaded or not --
        callers (create_background_run) shouldn't need to know or care."""
        store, mock_table, mock_s3 = _make_store()
        run_body = self._oversized_run_body()

        returned = store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        assert returned["input_payload"] == run_body
        assert "input_payload_ref" not in returned
        assert returned["run_id"]
        assert returned["status"] == "pending"

    def test_oversized_payload_does_not_raise_measuring_size(self):
        """Sanity: the size-measurement step itself must not choke on a
        large-but-ordinary payload (no recursion errors, no silent hangs)."""
        store, mock_table, mock_s3 = _make_store()
        run_body = self._oversized_run_body()

        # Must simply not raise.
        store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)


class TestGetRunResolvesS3Pointer:
    """get_run() must transparently turn an input_payload_ref pointer back
    into the real input_payload -- this is what every dispatch path
    (load_persisted_run, the Lambda self-invoke handler, etc.) actually
    calls, so this is the half of the fix that matters most for runs to
    ever actually execute once offloaded."""

    def test_get_run_resolves_pointer_to_real_payload(self):
        store, mock_table, mock_s3 = _make_store()
        real_payload = {"input": {"loan_amount": 999999.99, "borrower": "Big Loan LLC"}}
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=json.dumps(real_payload).encode("utf-8")))
        }
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "RUN#run-1",
                "SK": "METADATA",
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "predicted-conditions",
                "status": "pending",
                "input_payload_ref": {"bucket": "test-checkpoint-bucket", "key": "runs/run-1/input.json"},
                "created_at": "2026-11-24T00:00:00+00:00",
                "updated_at": "2026-11-24T00:00:00+00:00",
                "error": None,
                "result": None,
            }
        }

        run = store.get_run("run-1")

        mock_s3.get_object.assert_called_once_with(
            Bucket="test-checkpoint-bucket", Key="runs/run-1/input.json"
        )
        assert run["input_payload"] == real_payload
        assert "input_payload_ref" not in run
        assert run["input_payload"]["input"]["loan_amount"] == 999999.99
        assert isinstance(run["input_payload"]["input"]["loan_amount"], float)

    def test_get_run_inline_payload_unaffected_no_s3_call(self):
        """Non-offloaded runs (the common case) must not touch S3 at all on
        read, exactly as before this fix."""
        store, mock_table, mock_s3 = _make_store()
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "RUN#run-1",
                "SK": "METADATA",
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "predicted-conditions",
                "status": "success",
                "input_payload": {"loan_amount": Decimal("450000.5")},
                "result": None,
                "created_at": "2026-11-24T00:00:00+00:00",
                "updated_at": "2026-11-24T00:00:00+00:00",
                "error": None,
            }
        }

        run = store.get_run("run-1")

        mock_s3.get_object.assert_not_called()
        assert run["input_payload"]["loan_amount"] == 450000.5
        assert isinstance(run["input_payload"]["loan_amount"], float)


class TestBackwardCompatibilityWithoutS3Attributes:
    """Guards the exact compatibility fix this change needed: callers that
    construct DynamoDBThreadStore via __new__(...) and only stub out
    `_table` (bypassing __init__ entirely -- see
    test_thread_store_decimal_conversion.py's _make_store, written before
    this fix existed) must keep working unchanged, never raising
    AttributeError for the missing _s3_bucket/_s3/_region_name attributes."""

    def test_create_run_works_without_s3_attributes_set(self):
        store = DynamoDBThreadStore.__new__(DynamoDBThreadStore)
        mock_table = MagicMock()
        store._table = mock_table  # the ONLY attribute set, same as the
        # pre-existing decimal-conversion test file's _make_store helper

        run_body = {"input": {"loan_amount": 450000.5, "borrower": "Michael Kelly"}}
        returned = store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "input_payload" in item
        assert "input_payload_ref" not in item
        assert returned["run_id"]

    def test_get_run_works_without_s3_attributes_set(self):
        store = DynamoDBThreadStore.__new__(DynamoDBThreadStore)
        mock_table = MagicMock()
        store._table = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "RUN#run-1",
                "SK": "METADATA",
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "predicted-conditions",
                "status": "success",
                "input_payload": {"loan_amount": Decimal("450000.5")},
                "result": None,
                "created_at": "2026-11-24T00:00:00+00:00",
                "updated_at": "2026-11-24T00:00:00+00:00",
                "error": None,
            }
        }

        run = store.get_run("run-1")
        assert run["input_payload"]["loan_amount"] == 450000.5
