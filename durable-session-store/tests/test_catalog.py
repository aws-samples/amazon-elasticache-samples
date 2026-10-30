# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the static product catalog."""
from __future__ import annotations

from app.catalog import all_products, get_product


def test_catalog_is_non_empty() -> None:
    assert len(all_products()) >= 1


def test_product_ids_are_unique() -> None:
    ids = [p.id for p in all_products()]
    assert len(ids) == len(set(ids))


def test_prices_are_positive_integers() -> None:
    for product in all_products():
        assert isinstance(product.price_cents, int)
        assert product.price_cents > 0


def test_price_display_formats_dollars() -> None:
    product = all_products()[0]
    assert product.price_display.startswith("$")
    assert "." in product.price_display


def test_get_product_found() -> None:
    first = all_products()[0]
    assert get_product(first.id) is first


def test_get_product_missing_returns_none() -> None:
    assert get_product("no-such-sku") is None
