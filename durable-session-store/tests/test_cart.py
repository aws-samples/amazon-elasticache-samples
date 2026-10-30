# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Valkey-backed CartRepository (via fakeredis)."""
from __future__ import annotations

import fakeredis
import pytest

from app.cart import MAX_QUANTITY_PER_ITEM, CartRepository
from app.catalog import all_products

_P1 = all_products()[0].id
_P2 = all_products()[1].id
_P3 = all_products()[2].id


@pytest.fixture
def repo() -> CartRepository:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    return CartRepository(client, ttl_seconds=1209600)


@pytest.fixture
def redis_client(repo: CartRepository) -> fakeredis.FakeStrictRedis:
    return repo._client  # type: ignore[attr-defined]


# --- line items -------------------------------------------------------------


def test_add_item_new_line(repo: CartRepository) -> None:
    assert repo.add_item("c1", _P1, 2) is True
    assert repo.items("c1") == {_P1: 2}


def test_add_item_accumulates(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    assert repo.add_item("c1", _P1, 3) is True
    assert repo.items("c1")[_P1] == 5


def test_add_item_default_quantity_is_one(repo: CartRepository) -> None:
    repo.add_item("c1", _P1)
    assert repo.items("c1")[_P1] == 1


def test_add_unknown_product_ignored(repo: CartRepository) -> None:
    assert repo.add_item("c1", "nope", 1) is False
    assert repo.items("c1") == {}


def test_add_non_positive_ignored(repo: CartRepository) -> None:
    assert repo.add_item("c1", _P1, 0) is False
    assert repo.add_item("c1", _P1, -5) is False
    assert repo.items("c1") == {}


def test_add_clamps_to_max(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, MAX_QUANTITY_PER_ITEM + 50)
    assert repo.items("c1")[_P1] == MAX_QUANTITY_PER_ITEM


def test_set_quantity_absolute(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    assert repo.set_quantity("c1", _P1, 7) is True
    assert repo.items("c1")[_P1] == 7


def test_set_quantity_zero_removes(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    assert repo.set_quantity("c1", _P1, 0) is True
    assert _P1 not in repo.items("c1")


def test_set_quantity_unknown_ignored(repo: CartRepository) -> None:
    assert repo.set_quantity("c1", "nope", 3) is False


def test_remove_item(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 1)
    repo.add_item("c1", _P2, 2)
    assert repo.remove_item("c1", _P1) is True
    assert _P1 not in repo.items("c1")
    assert repo.remove_item("c1", _P1) is False


def test_clear(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 1)
    assert repo.clear("c1") is True
    assert repo.items("c1") == {}
    assert repo.clear("c1") is False


def test_count(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    repo.add_item("c1", _P2, 3)
    assert repo.count("c1") == 5


# --- TTL --------------------------------------------------------------------


def test_add_sets_sliding_ttl(
    repo: CartRepository, redis_client: fakeredis.FakeStrictRedis
) -> None:
    repo.add_item("c1", _P1, 1)
    ttl = redis_client.ttl("cart:c1")
    assert 0 < ttl <= 1209600


# --- coupons ----------------------------------------------------------------


def test_apply_and_list_coupons(repo: CartRepository) -> None:
    assert repo.apply_coupon("c1", "SAVE10") is True
    assert repo.apply_coupon("c1", "SAVE10") is False  # idempotent
    repo.apply_coupon("c1", "FREESHIP")
    assert repo.coupons("c1") == ["FREESHIP", "SAVE10"]


def test_coupons_not_counted_as_items(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    repo.apply_coupon("c1", "SAVE10")
    assert repo.items("c1") == {_P1: 2}  # coupon field excluded
    assert repo.count("c1") == 2


def test_remove_coupon(repo: CartRepository) -> None:
    repo.apply_coupon("c1", "SAVE10")
    assert repo.remove_coupon("c1", "SAVE10") is True
    assert repo.coupons("c1") == []


# --- saved for later --------------------------------------------------------


def test_save_for_later_moves_line(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    assert repo.save_for_later("c1", _P1) is True
    assert _P1 not in repo.items("c1")
    assert repo.saved_items("c1") == {_P1: 2}


def test_save_for_later_missing_is_noop(repo: CartRepository) -> None:
    assert repo.save_for_later("c1", _P1) is False


def test_move_to_cart(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    repo.save_for_later("c1", _P1)
    assert repo.move_to_cart("c1", _P1) is True
    assert repo.items("c1")[_P1] == 2
    assert repo.saved_items("c1") == {}


# --- guest -> account merge (account-wins) ----------------------------------


def test_merge_account_wins_on_conflict(repo: CartRepository) -> None:
    # Guest has P1=5, P2=1; account has P1=2, P3=4.
    repo.add_item("guest", _P1, 5)
    repo.add_item("guest", _P2, 1)
    repo.add_item("user:ada", _P1, 2)
    repo.add_item("user:ada", _P3, 4)

    repo.merge("guest", "user:ada")

    items = repo.items("user:ada")
    assert items[_P1] == 2  # account wins the conflict (not 5, not 7)
    assert items[_P2] == 1  # guest-only sku is brought over
    assert items[_P3] == 4  # account-only sku untouched


def test_merge_deletes_guest_cart(repo: CartRepository) -> None:
    repo.add_item("guest", _P1, 1)
    repo.add_item("user:ada", _P2, 1)
    repo.merge("guest", "user:ada")
    assert repo.items("guest") == {}


def test_merge_unions_coupons(repo: CartRepository) -> None:
    repo.apply_coupon("guest", "GUESTONLY")
    repo.apply_coupon("user:ada", "ACCTONLY")
    repo.merge("guest", "user:ada")
    assert repo.coupons("user:ada") == ["ACCTONLY", "GUESTONLY"]


def test_merge_unions_saved_for_later(repo: CartRepository) -> None:
    repo.add_item("guest", _P1, 1)
    repo.save_for_later("guest", _P1)
    repo.add_item("user:ada", _P2, 1)
    repo.save_for_later("user:ada", _P2)
    repo.merge("guest", "user:ada")
    assert set(repo.saved_items("user:ada")) == {_P1, _P2}


def test_merge_into_empty_account(repo: CartRepository) -> None:
    repo.add_item("guest", _P1, 3)
    repo.merge("guest", "user:ada")
    assert repo.items("user:ada") == {_P1: 3}


def test_merge_same_id_is_noop(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 2)
    repo.merge("c1", "c1")
    assert repo.items("c1") == {_P1: 2}


# --- resolved view ----------------------------------------------------------


def test_view_resolves_lines_and_totals(repo: CartRepository) -> None:
    from app.catalog import get_product

    repo.add_item("c1", _P1, 2)
    repo.add_item("c1", _P2, 1)
    view = repo.view("c1")
    assert view.item_count == 3
    expected = get_product(_P1).price_cents * 2 + get_product(_P2).price_cents
    assert view.total_cents == expected
    assert not view.is_empty


def test_view_includes_coupons_and_saved(repo: CartRepository) -> None:
    repo.add_item("c1", _P1, 1)
    repo.add_item("c1", _P2, 1)
    repo.save_for_later("c1", _P2)
    repo.apply_coupon("c1", "SAVE10")
    view = repo.view("c1")
    assert view.coupons == ("SAVE10",)
    assert {line.product.id for line in view.saved} == {_P2}


def test_view_skips_unknown_products(
    repo: CartRepository, redis_client: fakeredis.FakeStrictRedis
) -> None:
    repo.add_item("c1", _P1, 1)
    redis_client.hset("cart:c1", "ghost-sku", "5")  # stale sku
    view = repo.view("c1")
    assert len(view.lines) == 1
    assert view.lines[0].product.id == _P1


def test_view_empty(repo: CartRepository) -> None:
    view = repo.view("c1")
    assert view.is_empty
    assert view.total_cents == 0
    assert view.item_count == 0


def test_rejects_non_positive_ttl() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with pytest.raises(ValueError):
        CartRepository(client, ttl_seconds=0)
