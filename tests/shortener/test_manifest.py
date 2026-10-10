"""The manifest must describe the code that really exists."""
import importlib

import pytest

from shortener.app import create_app
from shortener.manifest import MANIFEST

MODULES = sorted(MANIFEST["modules"])


@pytest.mark.parametrize("module_name", MODULES)
def test_listed_modules_functions_and_constants_exist(module_name):
    module = importlib.import_module(f"shortener.{module_name}")
    info = MANIFEST["modules"][module_name]
    for name in info["public"] + info.get("constants", []):
        assert hasattr(module, name), f"{module_name}.{name} is in the manifest but missing"


def test_every_endpoint_exists_in_openapi_and_vice_versa():
    paths = create_app().openapi()["paths"]
    documented = {(m.upper(), p) for p, methods in paths.items() for m in methods}
    listed = {(e["method"], e["path"]) for e in MANIFEST["endpoints"]}
    assert listed == documented


def test_endpoint_handlers_exist():
    app_module = importlib.import_module("shortener.app")
    for endpoint in MANIFEST["endpoints"]:
        module_name, function_name = endpoint["handler"].split(".")
        assert module_name == "app"
        assert callable(getattr(app_module, function_name))


def test_endpoint_success_status_codes_match_openapi():
    paths = create_app().openapi()["paths"]
    for endpoint in MANIFEST["endpoints"]:
        responses = paths[endpoint["path"]][endpoint["method"].lower()]["responses"]
        assert str(endpoint["success"]) in responses


def test_data_fields_match_pydantic_models_and_records():
    from shortener import records, schemas

    for name, fields in MANIFEST["data_fields"].items():
        if name == "ErrorResponse":
            continue
        owner = getattr(schemas, name, None) or getattr(records, name)
        actual = list(owner.model_fields) if hasattr(owner, "model_fields") else list(
            owner.__dataclass_fields__
        )
        assert sorted(actual) == sorted(fields), name
