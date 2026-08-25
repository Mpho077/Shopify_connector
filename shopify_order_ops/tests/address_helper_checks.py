"""Standalone checks that do not need a running Odoo.

Run: python tests/address_helper_checks.py
Odoo will not auto-discover this file (no test_ prefix).
"""
from __future__ import annotations

import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stub_odoo():
    if "odoo" in sys.modules:
        return

    def _pkg(name):
        mod = types.ModuleType(name)
        mod.__path__ = []
        sys.modules[name] = mod
        return mod

    odoo = _pkg("odoo")
    api = _pkg("odoo.api")
    models = _pkg("odoo.models")
    fields = _pkg("odoo.fields")
    exceptions = _pkg("odoo.exceptions")
    http = _pkg("odoo.http")

    class Model:
        def __init_subclass__(cls, **kwargs):
            return None

    class AbstractModel(Model):
        pass

    models.Model = Model
    models.AbstractModel = AbstractModel
    api.model = lambda fn: fn
    api.model_create_multi = lambda fn: fn
    def _field(*_a, **_k):
        return None

    fields.Char = fields.Boolean = fields.Selection = fields.Integer = _field
    fields.Many2one = fields.Datetime = fields.Date = fields.Text = _field
    exceptions.UserError = Exception
    http.Controller = type("Controller", (), {})
    http.request = None
    odoo.api = api
    odoo.models = models
    odoo.fields = fields
    odoo.exceptions = exceptions
    odoo.http = http


class FakeICP:
    def __init__(self, params):
        self.params = params

    def sudo(self):
        return self

    def get_param(self, key, default=None):
        return self.params.get(key, default)


class FakeEnv:
    def __init__(self, params):
        self._icp = FakeICP(params)

    def __getitem__(self, name):
        if name == "ir.config_parameter":
            return self._icp
        raise KeyError(name)


class FakeRel:
    def __init__(self, code=None):
        self.code = code

    def __bool__(self):
        return bool(self.code)


class FakePartner:
    def __init__(self, **vals):
        self.name = vals.get("name")
        self.street = vals.get("street")
        self.street2 = vals.get("street2")
        self.city = vals.get("city")
        self.zip = vals.get("zip")
        self.phone = vals.get("phone")
        self.country_id = FakeRel(vals.get("country_code"))
        self.state_id = FakeRel(vals.get("state_code"))
        self.display_name = self.name


def check_xml_settings_block():
    xml = (ROOT / "views" / "res_config_settings_views.xml").read_text(
        encoding="utf-8"
    )
    tree = ET.fromstring(xml)
    titles = [
        el.get("title")
        for el in tree.iter()
        if el.tag == "block" and el.get("title")
    ]
    assert "Order Address Sync" in titles, titles
    fields = [el.get("name") for el in tree.iter() if el.tag == "field"]
    assert "shopify_address_propagation_enabled" in fields
    assert "shopify_address_sync_direction" in fields
    print("OK  settings XML has Order Address Sync block")


def _load(module_name, relative):
    import importlib.util

    pkg = "shopify_order_ops"
    models_pkg = pkg + ".models"
    if pkg not in sys.modules:
        root_mod = types.ModuleType(pkg)
        root_mod.__path__ = [str(ROOT)]
        sys.modules[pkg] = root_mod
        models_mod = types.ModuleType(models_pkg)
        models_mod.__path__ = [str(ROOT / "models")]
        sys.modules[models_pkg] = models_mod

    full_name = models_pkg + "." + Path(relative).stem
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_direction_gates():
    _stub_odoo()
    mod = _load("order_update_sync", "models/order_update_sync.py")
    _address_sync_allows_odoo_to_shopify = mod._address_sync_allows_odoo_to_shopify
    _address_sync_allows_shopify_to_odoo = mod._address_sync_allows_shopify_to_odoo
    _address_sync_direction = mod._address_sync_direction

    cases = [
        ({}, "two_way", True, True),
        (
            {
                "shopify_order_ops.address_propagation_enabled": "False",
                "shopify_order_ops.address_sync_direction": "two_way",
            },
            "two_way",
            False,
            False,
        ),
        (
            {
                "shopify_order_ops.address_propagation_enabled": "True",
                "shopify_order_ops.address_sync_direction": "shopify_to_odoo",
            },
            "shopify_to_odoo",
            True,
            False,
        ),
        (
            {
                "shopify_order_ops.address_propagation_enabled": "True",
                "shopify_order_ops.address_sync_direction": "odoo_to_shopify",
            },
            "odoo_to_shopify",
            False,
            True,
        ),
        (
            {
                "shopify_order_ops.address_propagation_enabled": "True",
                "shopify_order_ops.address_sync_direction": "two_way",
            },
            "two_way",
            True,
            True,
        ),
    ]
    for params, direction, pull, push in cases:
        env = FakeEnv(params)
        assert _address_sync_direction(env) == direction, params
        assert _address_sync_allows_shopify_to_odoo(env) is pull, params
        assert _address_sync_allows_odoo_to_shopify(env) is push, params
    print("OK  direction gates (off / Shopify->Odoo / Odoo->Shopify / two-way)")


def check_partner_mapping():
    _stub_odoo()
    ShopifyOrderUpdateEngine = _load(
        "order_update_sync", "models/order_update_sync.py"
    ).ShopifyOrderUpdateEngine

    partner = FakePartner(
        name="Jane Old",
        street="1 Old Street",
        street2="Unit 4",
        city="Cape Town",
        zip="8000",
        country_code="ZA",
        state_code="WC",
        phone="+27821234567",
    )
    mapped = ShopifyOrderUpdateEngine._partner_to_shopify_shipping_address(
        ShopifyOrderUpdateEngine, partner
    )
    assert mapped == {
        "firstName": "Jane",
        "lastName": "Old",
        "address1": "1 Old Street",
        "address2": "Unit 4",
        "city": "Cape Town",
        "zip": "8000",
        "countryCode": "ZA",
        "provinceCode": "WC",
        "phone": "+27821234567",
    }, mapped
    rest = ShopifyOrderUpdateEngine._partner_to_shopify_rest_address(
        ShopifyOrderUpdateEngine, partner
    )
    assert rest == {
        "first_name": "Jane",
        "last_name": "Old",
        "address1": "1 Old Street",
        "address2": "Unit 4",
        "city": "Cape Town",
        "zip": "8000",
        "country_code": "ZA",
        "province_code": "WC",
        "phone": "+27821234567",
    }, rest
    print("OK  Odoo partner -> Shopify shippingAddress / REST billing mapping")


def check_address_key_match():
    _stub_odoo()
    ShopifyOrderPullEngine = _load(
        "order_pull_engine", "models/order_pull_engine.py"
    ).ShopifyOrderPullEngine

    engine = ShopifyOrderPullEngine()
    partner = FakePartner(
        street="99 Shopify Street",
        city="Cape Town",
        zip="8001",
        country_code="ZA",
    )
    shopify = {
        "address1": "99 Shopify Street",
        "city": "Cape Town",
        "zip": "8001",
        "country_code": "ZA",
    }
    assert engine._address_dict_matches(partner, shopify)
    shopify["address1"] = "Somewhere Else"
    assert not engine._address_dict_matches(partner, shopify)
    print("OK  Shopify vs Odoo address matching")


def main():
    check_xml_settings_block()
    check_direction_gates()
    check_partner_mapping()
    check_address_key_match()
    print("\nAll standalone address-sync checks passed.")


if __name__ == "__main__":
    main()
