"""Produce a scrubbed copy of a real Apple Wallet .order file, safe to commit.

Rewrites the fields that carry live credentials or personal data
(authenticationToken, orderManagementURL, customer name/email) to fake
values, then re-zips it. Everything else — the actual purchase data the
importer is tested against — is left untouched.

Usage: python scripts/scrub_order.py <input.order> <output.order>
"""

from __future__ import annotations

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

    return json.dumps(data, indent=None).encode("utf-8")


def scrub(input_path: Path, output_path: Path) -> None:
    with zipfile.ZipFile(input_path, "r") as src:
        names = src.namelist()
        contents = {name: src.read(name) for name in names}

    contents["order.json"] = scrub_order_json(contents["order.json"])

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
