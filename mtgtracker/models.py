"""Typed dataclasses mirroring the schema. Not an ORM — these are plain
value objects; all reads/writes go through explicit SQL in each module."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Transaction:
    id: int | None
    order_type_identifier: str | None
    order_identifier: str | None
    order_number: str | None
    vendor: str
    status: str | None
    created_at: str
    updated_at: str
    subtotal_cents: int
    tax_cents: int
    shipping_cents: int
    total_cents: int
    currency: str
    payment_method: str | None
    source: str


@dataclass
class Product:
    id: int | None
    transaction_id: int
    sku: str | None
    title: str
    display_name: str
    qty: int
    unit_price_cents: int
    line_subtotal_cents: int
    tax_allocated_cents: int
    cost_basis_cents: int
    product_type: str | None


@dataclass
class Lot:
    id: int | None
    product_id: int | None
    transaction_id: int
    scryfall_id: str | None
    card_name: str
    set_code: str | None
    collector_number: str | None
    finish: str
    allocated_cost_cents: int | None
    acquired_date: str
    notes: str | None


@dataclass
class Deck:
    id: int | None
    name: str
    format: str
    created_at: str
    notes: str | None


@dataclass
class DeckCard:
    deck_id: int
    lot_id: int
    equipped: bool


@dataclass
class Receipt:
    id: int | None
    transaction_id: int | None
    sha256: str
    original_filename: str
    mime_type: str
    file_ext: str
    imported_at: str
    kind: str
