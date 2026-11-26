"""Tests for api/thread_store.py's float<->Decimal conversion.

Regression coverage for a bug confirmed live on 2026-10-14: a thread was
created (POST /threads) but its run never started — DynamoDBThreadStore
.create_run()'s put_item() call raised ``TypeError: Float types are not
supported. Use Decimal types instead.`` because run_body (a real loan
payload) contained plain Python floats, and that exception was never caught
anywhere before a run record (or even a run_id) existed. The fix adds a
recursive float->Decimal converter before every DynamoDB write, and the
symmetric Decimal->native converter on every read-back, so the stored data
round-trips through DynamoDB's Decimal-only Number type without leaking
Decimals back out to callers (which would just trade one TypeError for
another: Decimal can't be compared directly against float either).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.types import TypeSerializer

from api.thread_store import DynamoDBThreadStore, _decimals_to_native, _floats_to_decimal


class TestFloatsToDecimal:
    def test_converts_top_level_float(self):
        assert _floats_to_decimal(450000.5) == Decimal("450000.5")

    def test_leaves_int_unchanged(self):
        assert _floats_to_decimal(450000) == 450000
        assert isinstance(_floats_to_decimal(450000), int)

    def test_leaves_string_unchanged(self):
        assert _floats_to_decimal("450000.5") == "450000.5"

    def test_leaves_bool_unchanged_not_converted_to_decimal(self):
        # bool is a subclass of int in Python; must not be mistaken for a
        # float-like numeric value and wrapped in Decimal.
        assert _floats_to_decimal(True) is True
        assert _floats_to_decimal(False) is False

    def test_converts_float_nested_in_dict(self):
        payload = {"loan_amount": 450000.5, "ltv": 80, "borrower": "Michael Kelly"}
        out = _floats_to_decimal(payload)
        assert out["loan_amount"] == Decimal("450000.5")
        assert isinstance(out["loan_amount"], Decimal)
        assert out["ltv"] == 80
        assert out["borrower"] == "Michael Kelly"

    def test_converts_float_nested_in_list(self):
        out = _floats_to_decimal([1.5, 2, "three", {"rate": 4.25}])
        assert out[0] == Decimal("1.5")
        assert out[1] == 2
        assert out[2] == "three"
        assert out[3]["rate"] == Decimal("4.25")

    def test_converts_deeply_nested_mixed_structure(self):
        payload = {
            "input": {
                "document_requests": [
                    {"document_type": "Appraisal Report", "ltv": 79.9},
                    {"document_type": "Bank Statement", "ltv": 80},
                ],
                "scenario_summary": {"loan_amount": 325000.0, "borrowers": ["A", "B"]},
            }
        }
        out = _floats_to_decimal(payload)
        drs = out["input"]["document_requests"]
        assert drs[0]["ltv"] == Decimal("79.9")
        assert drs[1]["ltv"] == 80
        assert out["input"]["scenario_summary"]["loan_amount"] == Decimal("325000.0")

    def test_none_passes_through(self):
        assert _floats_to_decimal(None) is None


class TestDecimalsToNative:
    def test_integral_decimal_becomes_int(self):
        result = _decimals_to_native(Decimal("450000"))
        assert result == 450000
        assert isinstance(result, int)

    def test_non_integral_decimal_becomes_float(self):
        result = _decimals_to_native(Decimal("450000.5"))
        assert result == 450000.5
        assert isinstance(result, float)

    def test_leaves_bool_unchanged(self):
        assert _decimals_to_native(True) is True
        assert _decimals_to_native(False) is False

    def test_converts_nested_in_dict_and_list(self):
        payload = {
            "loan_amount": Decimal("450000.5"),
            "ltv": Decimal("80"),
            "items": [Decimal("1.25"), Decimal("2"), "text"],
        }
        out = _decimals_to_native(payload)
        assert out["loan_amount"] == 450000.5 and isinstance(out["loan_amount"], float)
        assert out["ltv"] == 80 and isinstance(out["ltv"], int)
        assert out["items"][0] == 1.25 and isinstance(out["items"][0], float)
        assert out["items"][1] == 2 and isinstance(out["items"][1], int)
        assert out["items"][2] == "text"

    def test_none_passes_through(self):
        assert _decimals_to_native(None) is None


class TestFloatDecimalRoundTrip:
    @pytest.mark.parametrize("value", [450000.5, 0.1, 79.9, 325000.0, -12.34])
    def test_float_survives_round_trip(self, value):
        assert _decimals_to_native(_floats_to_decimal(value)) == value

    def test_nested_payload_survives_round_trip(self):
        payload = {
            "loan_amount": 450000.5,
            "ltv": 79.9,
            "borrower_name": "Michael Kelly",
            "document_requests": [{"category_ids": ["323"], "weight": 1.0}],
        }
        round_tripped = _decimals_to_native(_floats_to_decimal(payload))
        assert round_tripped["loan_amount"] == 450000.5
        assert round_tripped["ltv"] == 79.9
        assert round_tripped["borrower_name"] == "Michael Kelly"
        assert round_tripped["document_requests"][0]["weight"] == 1.0


class TestRealBoto3SerializerRegression:
    """Confirms, against boto3's actual TypeSerializer (the exact component
    that raises the error reported live: "Float types are not supported.
    Use Decimal types instead."), that the raw payload fails and the
    converted payload succeeds — not just against a mock."""

    def test_raw_float_payload_fails_real_serializer(self):
        serializer = TypeSerializer()
        run_body = {"input": {"loan_amount": 450000.5, "ltv": 79.9, "borrower": "Michael Kelly"}}
        with pytest.raises(TypeError, match="Float types are not supported"):
            serializer.serialize(run_body)

    def test_converted_payload_passes_real_serializer(self):
        serializer = TypeSerializer()
        run_body = {"input": {"loan_amount": 450000.5, "ltv": 79.9, "borrower": "Michael Kelly"}}
        serializer.serialize(_floats_to_decimal(run_body))  # must not raise


class TestDynamoDBThreadStoreCreateRunConversion:
    """Verify create_run() never hands boto3's put_item a raw float, and
    never hands the caller back a raw Decimal."""

    def _make_store(self) -> tuple[DynamoDBThreadStore, MagicMock]:
        store = DynamoDBThreadStore.__new__(DynamoDBThreadStore)
        mock_table = MagicMock()
        store._table = mock_table
        return store, mock_table

    def test_put_item_receives_no_native_floats(self):
        store, mock_table = self._make_store()
        run_body = {"input": {"loan_amount": 450000.5, "borrower": "Michael Kelly"}}

        store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        put_item_kwargs = mock_table.put_item.call_args.kwargs
        written_payload = put_item_kwargs["Item"]["input_payload"]
        assert isinstance(written_payload["input"]["loan_amount"], Decimal)

    def test_returned_value_has_no_decimals(self):
        store, mock_table = self._make_store()
        run_body = {"input": {"loan_amount": 450000.5, "borrower": "Michael Kelly"}}

        returned = store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        assert returned["input_payload"]["input"]["loan_amount"] == 450000.5
        assert isinstance(returned["input_payload"]["input"]["loan_amount"], float)
        assert returned["run_id"]
        assert returned["status"] == "pending"

    def test_does_not_raise_on_float_bearing_run_body(self):
        # This is the exact regression: before the fix, put_item() against a
        # real (non-mocked) table would raise TypeError here. With a mock
        # table it can't reproduce boto3's validation directly, so this test
        # instead asserts no float instance anywhere survives into the item
        # handed to put_item — the actual guarantee that prevents the error.
        store, mock_table = self._make_store()
        run_body = {
            "input": {
                "loan_amount": 450000.5,
                "ltv": 79.9,
                "nested": {"rate": 4.125, "points": [1.0, 2.5]},
            }
        }

        store.create_run("thread-1", assistant_id="predicted-conditions", run_body=run_body)

        def _assert_no_float(value):
            assert not isinstance(value, float), f"found native float: {value!r}"
            if isinstance(value, dict):
                for v in value.values():
                    _assert_no_float(v)
            elif isinstance(value, list):
                for v in value:
                    _assert_no_float(v)

        _assert_no_float(mock_table.put_item.call_args.kwargs["Item"]["input_payload"])


class TestDynamoDBThreadStoreGetRunConversion:
    def test_get_run_converts_decimals_back_to_native(self):
        store = DynamoDBThreadStore.__new__(DynamoDBThreadStore)
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "RUN#run-1",
                "SK": "METADATA",
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "predicted-conditions",
                "status": "success",
                "input_payload": {"loan_amount": Decimal("450000.5")},
                "result": {"final_output": {"ltv": Decimal("80")}},
                "created_at": "2026-10-14T00:00:00+00:00",
                "updated_at": "2026-10-14T00:00:00+00:00",
                "error": None,
            }
        }
        store._table = mock_table

        run = store.get_run("run-1")

        assert isinstance(run["input_payload"]["loan_amount"], float)
        assert run["input_payload"]["loan_amount"] == 450000.5
        assert isinstance(run["result"]["final_output"]["ltv"], int)
        assert run["result"]["final_output"]["ltv"] == 80
        assert "PK" not in run and "SK" not in run

    def test_get_run_returns_none_when_missing(self):
        store = DynamoDBThreadStore.__new__(DynamoDBThreadStore)
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        store._table = mock_table

        assert store.get_run("missing-run") is None


class TestDynamoDBThreadStoreUpdateRunConversion:
    def test_update_run_converts_result_floats_before_write(self):
        store = DynamoDBThreadStore.__new__(DynamoDBThreadStore)
        mock_table = MagicMock()
        store._table = mock_table

        store.update_run(
            "run-1",
            status="success",
            result={"final_output": {"document_requests": [{"weight": 1.0}]}},
        )

        expr_values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        result_value = expr_values[":result"]
        assert isinstance(result_value["final_output"]["document_requests"][0]["weight"], Decimal)
