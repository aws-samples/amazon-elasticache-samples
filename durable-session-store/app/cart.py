# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shopping cart backed by a dedicated Valkey/Redis hash per cart.

Use case #4 (shopping carts) from the durable-session-store design: each cart is
one hash, ``cart:{cartId}``, holding ``{sku: qty}`` line items plus a couple of
namespaced fields for applied coupons. Saved-for-later items live in a sibling
``saved:{cartId}`` hash. Every cart carries a sliding TTL (default 14 days) so a
returning customer finds a full cart, while abandoned carts expire on their own.

Modelling the cart as its own key (rather than inside the login session) is what
makes guest-to-account merge, cross-device account carts, and a cheap cart-badge
read possible. All operations are single-key hash commands, so this works
unchanged against both ``redis.Redis`` and ``redis.cluster.RedisCluster``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .catalog import Product, get_product

if TYPE_CHECKING:  # avoid importing redis at runtime just for typing
    import redis
    import redis.cluster

# Guardrail so a single line item can't be inflated without bound.
MAX_QUANTITY_PER_ITEM = 99

# Default sliding TTL for a cart: 14 days.
DEFAULT_CART_TTL_SECONDS = 1_209_600

# Key prefixes.
_CART_PREFIX = "cart:"
_SAVED_PREFIX = "saved:"
# Hash fields holding applied coupons are namespaced so they never collide with
# a product SKU.
_COUPON_FIELD_PREFIX = "coupon:"


# --- Value objects (pure, render + costing) ---------------------------------


@dataclass(frozen=True)
class CartLine:
    """A resolved cart line: the product, its quantity, and the line subtotal."""

    product: Product
    quantity: int

    @property
    def subtotal_cents(self) -> int:
        return self.product.price_cents * self.quantity

    @property
    def subtotal_display(self) -> str:
        return f"${self.subtotal_cents / 100:,.2f}"


@dataclass(frozen=True)
class CartView:
    """A fully resolved view of the cart for rendering."""

    lines: tuple[CartLine, ...]
    coupons: tuple[str, ...] = ()
    saved: tuple[CartLine, ...] = ()

    @property
    def total_cents(self) -> int:
        return sum(line.subtotal_cents for line in self.lines)

    @property
    def total_display(self) -> str:
        return f"${self.total_cents / 100:,.2f}"

    @property
    def item_count(self) -> int:
        """Total number of individual items (sum of quantities)."""
        return sum(line.quantity for line in self.lines)

    @property
    def is_empty(self) -> bool:
        return not self.lines


def _coerce_quantity(value: object) -> int:
    """Parse a quantity, clamping to the range [0, MAX_QUANTITY_PER_ITEM].

    Invalid or negative values become 0 (which removes the line).
    """
    try:
        qty = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if qty < 0:
        return 0
    return min(qty, MAX_QUANTITY_PER_ITEM)


def _is_coupon_field(field: str) -> bool:
    return field.startswith(_COUPON_FIELD_PREFIX)


# --- Repository (all Valkey I/O) ---------------------------------------------


class CartRepository:
    """Durable cart store over a Valkey/Redis hash per cart.

    The client must be created with ``decode_responses=True`` so hash fields and
    values come back as ``str``.
    """

    def __init__(
        self,
        client: redis.Redis | redis.cluster.RedisCluster,
        ttl_seconds: int = DEFAULT_CART_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._client = client
        self._ttl = ttl_seconds

    # -- key helpers --
    @staticmethod
    def _cart_key(cart_id: str) -> str:
        return f"{_CART_PREFIX}{cart_id}"

    @staticmethod
    def _saved_key(cart_id: str) -> str:
        return f"{_SAVED_PREFIX}{cart_id}"

    def _touch(self, *keys: str) -> None:
        """Refresh the sliding TTL on the given keys (only if they exist)."""
        for key in keys:
            self._client.expire(key, self._ttl)

    # -- line items --
    def items(self, cart_id: str) -> dict[str, int]:
        """Return ``{sku: qty}`` line items (coupon fields excluded)."""
        raw = self._client.hgetall(self._cart_key(cart_id)) or {}
        return {
            field: _coerce_quantity(value)
            for field, value in raw.items()
            if not _is_coupon_field(field)
        }

    def count(self, cart_id: str) -> int:
        """Total item count for the cart badge (sum of quantities)."""
        return sum(self.items(cart_id).values())

    def add_item(self, cart_id: str, sku: str, quantity: int = 1) -> bool:
        """Add ``quantity`` of ``sku``; returns True if the cart changed."""
        if get_product(sku) is None:
            return False
        qty = _coerce_quantity(quantity)
        if qty <= 0:
            return False
        key = self._cart_key(cart_id)
        current = _coerce_quantity(self._client.hget(key, sku) or 0)
        new_qty = min(current + qty, MAX_QUANTITY_PER_ITEM)
        if new_qty == current:
            return False
        self._client.hset(key, sku, new_qty)
        self._touch(key)
        return True

    def set_quantity(self, cart_id: str, sku: str, quantity: int) -> bool:
        """Set an absolute quantity; zero removes the line."""
        if get_product(sku) is None:
            return False
        qty = _coerce_quantity(quantity)
        if qty <= 0:
            return self.remove_item(cart_id, sku)
        key = self._cart_key(cart_id)
        if _coerce_quantity(self._client.hget(key, sku) or 0) == qty:
            return False
        self._client.hset(key, sku, qty)
        self._touch(key)
        return True

    def remove_item(self, cart_id: str, sku: str) -> bool:
        """Remove a line entirely. Returns True if something was removed."""
        removed = self._client.hdel(self._cart_key(cart_id), sku)
        if removed:
            self._touch(self._cart_key(cart_id))
        return bool(removed)

    def clear(self, cart_id: str) -> bool:
        """Delete the whole cart (items + coupons). True if it existed."""
        return bool(self._client.delete(self._cart_key(cart_id)))

    # -- coupons --
    def apply_coupon(self, cart_id: str, code: str) -> bool:
        """Record an applied coupon. Returns True if newly added."""
        code = code.strip()
        if not code:
            return False
        key = self._cart_key(cart_id)
        field = f"{_COUPON_FIELD_PREFIX}{code}"
        if self._client.hget(key, field) is not None:
            return False
        self._client.hset(key, field, "1")
        self._touch(key)
        return True

    def remove_coupon(self, cart_id: str, code: str) -> bool:
        field = f"{_COUPON_FIELD_PREFIX}{code.strip()}"
        return bool(self._client.hdel(self._cart_key(cart_id), field))

    def coupons(self, cart_id: str) -> list[str]:
        raw = self._client.hgetall(self._cart_key(cart_id)) or {}
        return sorted(
            field[len(_COUPON_FIELD_PREFIX):]
            for field in raw
            if _is_coupon_field(field)
        )

    # -- saved for later --
    def save_for_later(self, cart_id: str, sku: str) -> bool:
        """Move a line from the cart into the saved-for-later hash."""
        key = self._cart_key(cart_id)
        qty = _coerce_quantity(self._client.hget(key, sku) or 0)
        if qty <= 0:
            return False
        saved = self._saved_key(cart_id)
        self._client.hset(saved, sku, qty)
        self._client.hdel(key, sku)
        self._touch(key, saved)
        return True

    def move_to_cart(self, cart_id: str, sku: str) -> bool:
        """Move a saved item back into the cart (summing if already present)."""
        saved = self._saved_key(cart_id)
        qty = _coerce_quantity(self._client.hget(saved, sku) or 0)
        if qty <= 0:
            return False
        self._client.hdel(saved, sku)
        self.add_item(cart_id, sku, qty)
        self._touch(saved)
        return True

    def saved_items(self, cart_id: str) -> dict[str, int]:
        raw = self._client.hgetall(self._saved_key(cart_id)) or {}
        return {field: _coerce_quantity(value) for field, value in raw.items()}

    # -- guest -> account merge (account-wins) --
    def merge(self, guest_cart_id: str, account_cart_id: str) -> None:
        """Merge a guest cart into the account cart, then delete the guest cart.

        Conflict rule is **account-wins**: for any SKU present in both carts the
        account's quantity is kept; the guest cart only contributes SKUs the
        account does not already have. Coupons and saved-for-later are unioned.
        """
        if guest_cart_id == account_cart_id:
            return

        guest_key = self._cart_key(guest_cart_id)
        account_key = self._cart_key(account_cart_id)
        guest_hash = self._client.hgetall(guest_key) or {}
        account_hash = self._client.hgetall(account_key) or {}

        updates: dict[str, str] = {}
        for field, value in guest_hash.items():
            if field in account_hash:
                continue  # account-wins: keep the account's value
            updates[field] = value
        if updates:
            self._client.hset(account_key, mapping=updates)

        # Union saved-for-later (account value wins on conflict).
        guest_saved = self._client.hgetall(self._saved_key(guest_cart_id)) or {}
        account_saved_key = self._saved_key(account_cart_id)
        account_saved = self._client.hgetall(account_saved_key) or {}
        saved_updates = {
            field: value
            for field, value in guest_saved.items()
            if field not in account_saved
        }
        if saved_updates:
            self._client.hset(account_saved_key, mapping=saved_updates)

        # Delete the guest cart so it can't be merged again.
        self._client.delete(guest_key, self._saved_key(guest_cart_id))
        self._touch(account_key, account_saved_key)

    # -- resolved view --
    def view(self, cart_id: str) -> CartView:
        """Resolve the stored cart into a renderable :class:`CartView`.

        Products no longer in the catalog are skipped so an old cart still
        renders cleanly.
        """
        lines = self._resolve_lines(self.items(cart_id))
        saved = self._resolve_lines(self.saved_items(cart_id))
        return CartView(
            lines=lines,
            coupons=tuple(self.coupons(cart_id)),
            saved=saved,
        )

    @staticmethod
    def _resolve_lines(mapping: dict[str, int]) -> tuple[CartLine, ...]:
        lines: list[CartLine] = []
        for sku, qty in mapping.items():
            product = get_product(sku)
            if product is None or qty <= 0:
                continue
            lines.append(CartLine(product=product, quantity=qty))
        return tuple(lines)
