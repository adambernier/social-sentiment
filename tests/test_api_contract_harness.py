import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_api_contract.py"
SPEC = importlib.util.spec_from_file_location("verify_api_contract", SCRIPT)
assert SPEC and SPEC.loader
contract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contract
SPEC.loader.exec_module(contract)


def test_contract_normalizes_timestamps_and_tolerates_small_float_differences():
    expected = {"at": "2026-08-18T12:00:00Z", "score": 0.1234560}
    actual = {"at": "2026-08-18T12:00:01+00:00", "score": 0.1234569}

    assert contract.compare(contract.normalize(expected), contract.normalize(actual)) == []


def test_contract_reports_order_and_shape_mismatches():
    assert contract.compare([{"id": 1}], [{"id": 2}]) == ["$[0].id: 1 != 2"]
    assert contract.compare({"id": 1}, {"id": 1, "extra": True})


def test_route_inventory_ignores_path_level_parameters():
    document = {
        "paths": {
            "/api/posts": {
                "parameters": [],
                "get": {"responses": {}},
            }
        }
    }
    assert contract.route_inventory(document) == {"/api/posts": ["get"]}
