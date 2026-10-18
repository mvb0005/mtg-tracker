"""Produce a scrubbed copy of a real Apple Wallet .order file, safe to commit.

Rewrites the fields that carry live credentials or personal data to fake
values: the authenticationToken, orderManagementURL, customer name/email,
the card's last four digits, the store's order number and fulfillment ids,
and the merchant (name, URLs, address, ids, logo), so a public repo can't tie
the fixture to a person, a card or a store. Apple's signature is dropped and
the manifest rebuilt to match. The purchase data the importer is tested
against (line items, amounts, dates) is left untouched.

Usage: python scripts/scrub_order.py <input.order> <output.order>
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

FAKE_AUTH_TOKEN = "FAKE.SCRUBBED.TOKEN"
FAKE_ORDER_MANAGEMENT_URL = "https://example.com/orders/fake?key=fake"
FAKE_EMAIL = "test.buyer@example.com"
FAKE_FAMILY_NAME = "Buyer"
FAKE_GIVEN_NAME = "Test"
FAKE_ORDER_IDENTIFIER = "0" * 32
FAKE_LAUNCH_URL = "https://example.com/launch/fake"
FAKE_ORDER_NUMBER = "#00000"
FAKE_MERCHANT = {
    "displayName": "Example Games",
    "emailAddress": "support@example.com",
    "url": "https://example.com",
    "contactUrl": "https://example.com",
    "merchantIdentifier": "gid://shopify/Shop/0",
    "address": {"addressLines": ["PO BOX 0"], "countryCode": "US", "postalCode": "00000"},
}
FAKE_RETURN_POLICY_URL = "https://example.com/policies/returns"
DROPPED_FILES = {"signature"}  # Apple's signature over the real order


def _last_four(display_name: str) -> str:
    """'Visa 1234' -> 'Visa 0000': keep the card brand, never the digits."""
    words = display_name.split()
    return " ".join("0000" if w.isdigit() else w for w in words) if words else display_name


def scrub_order_json(raw: bytes) -> bytes:
    data = json.loads(raw)

    if "authenticationToken" in data:
        data["authenticationToken"] = FAKE_AUTH_TOKEN
    if "orderManagementURL" in data:
        data["orderManagementURL"] = FAKE_ORDER_MANAGEMENT_URL
    if "orderIdentifier" in data:
        data["orderIdentifier"] = FAKE_ORDER_IDENTIFIER
    if "customer" in data:
        data["customer"]["emailAddress"] = FAKE_EMAIL
        data["customer"]["familyName"] = FAKE_FAMILY_NAME
        data["customer"]["givenName"] = FAKE_GIVEN_NAME
    for app in data.get("associatedApplications", []):
        if "launchURL" in app:
            app["launchURL"] = FAKE_LAUNCH_URL
        if "customProductPageIdentifier" in app:
            app["customProductPageIdentifier"] = "00000000-0000-0000-0000-000000000000"
    if "orderNumber" in data:
        data["orderNumber"] = FAKE_ORDER_NUMBER
    if "merchant" in data:
        logo = data["merchant"].get("logo")
        data["merchant"] = {**FAKE_MERCHANT, **({"logo": logo} if logo else {})}
    for txn in (data.get("payment") or {}).get("transactions", []):
        method = txn.get("paymentMethod") or {}
        if "displayName" in method:
            method["displayName"] = _last_four(method["displayName"])
    for fulfillment in data.get("fulfillments", []):
        if "fulfillmentIdentifier" in fulfillment:
            fulfillment["fulfillmentIdentifier"] = "0" * len(str(fulfillment["fulfillmentIdentifier"]))
    if "returnInfo" in data and "returnPolicyURL" in data["returnInfo"]:
        data["returnInfo"]["returnPolicyURL"] = FAKE_RETURN_POLICY_URL

    return json.dumps(data, indent=None).encode("utf-8")


# A 1x1 white JPEG, standing in for the merchant's logo.
_BLANK_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f"
    "141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100"
    "ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000"
    "017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a2526272829"
    "2a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a929394"
    "95969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9"
    "eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)


def scrub(input_path: Path, output_path: Path) -> None:
    with zipfile.ZipFile(input_path, "r") as src:
        names = src.namelist()
        contents = {name: src.read(name) for name in names}

    logo = (json.loads(contents["order.json"]).get("merchant") or {}).get("logo")
    contents["order.json"] = scrub_order_json(contents["order.json"])
    if logo in contents:
        contents[logo] = _BLANK_JPEG
    names = [n for n in names if n not in DROPPED_FILES]
    if "manifest.json" in contents:
        contents["manifest.json"] = json.dumps(
            {n: hashlib.sha256(contents[n]).hexdigest() for n in names if n != "manifest.json"}
        ).encode("utf-8")

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in names:
            dst.writestr(name, contents[name])


def main() -> None:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <input.order> <output.order>", file=sys.stderr)
        raise SystemExit(1)

    scrub(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
