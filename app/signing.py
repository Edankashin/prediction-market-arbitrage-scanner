"""V2 order construction — builds signed orders, never submits.

This is the ONLY module that may touch the private key.
The key is read from the environment variable POLYGON_WALLET_PRIVATE_KEY.
It is never logged, never stored, and never passed to any other module.

M1 contract: build_v2_order() returns a signed order dict and stops there.
Submission is M4 scope.

V2 differences from V1 (per CLOB V2 launch 2026-04-28):
  - No feeRateBps field in the order struct
  - No nonce field in OrderArgs
  - No taker field in OrderArgs
  - verifyingContract resolved by the SDK, not hardcoded
  - fee_rate_bps handled internally by the SDK's __resolve_fee_rate_bps
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

_log = logging.getLogger(__name__)


def build_v2_order(
    token_id: str,
    price: Decimal,
    size: Decimal,
    side: str,
    chain_id: int,
    private_key: str,
    tick_size: str = "0.01",
    neg_risk: bool = False,
) -> dict[str, Any]:
    """Construct and sign a V2 CLOB order without submitting it.

    Converts Decimal price/size to float at the SDK boundary (only place
    where this conversion is allowed).

    Args:
        token_id: CLOB token ID (YES or NO token).
        price: Limit price as Decimal (e.g., Decimal("0.55")).
        size: Order size in shares as Decimal (e.g., Decimal("100")).
        side: "BUY" or "SELL".
        chain_id: 137 (Polygon mainnet) or 80002 (Amoy testnet).
        private_key: Polygon wallet private key (from env var, never logged).
        tick_size: Market minimum tick size (default "0.01").
        neg_risk: True if this token is in a NegRisk multi-outcome market.

    Returns:
        Signed order dict as produced by py-clob-client-v2. Fields vary
        by SDK version; callers should not depend on specific field names
        beyond the presence of a non-empty dict.

    Raises:
        ImportError: if py-clob-client-v2 is not installed.
        RuntimeError: if the SDK raises during order construction.
    """
    # Lazy import — keeps this module importable without the SDK installed
    # in test environments that mock this function.
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import OrderArgs
    except ImportError as exc:
        raise ImportError("py-clob-client-v2 is required for signing") from exc

    host = "https://clob.polymarket.com"
    client = ClobClient(host=host, chain_id=chain_id, key=private_key)

    # Pre-populate caches so create_order doesn't make HTTP calls
    # for tick_size, neg_risk, or version when those are already known.
    # This is especially important in tests (use the cache-injection pattern).
    client._ClobClient__tick_sizes[token_id] = tick_size  # type: ignore[attr-defined]
    client._ClobClient__neg_risk[token_id] = neg_risk       # type: ignore[attr-defined]
    client._ClobClient__cached_version = 2                   # type: ignore[attr-defined]

    order_args = OrderArgs(
        token_id=token_id,
        price=float(price),   # Decimal → float at SDK boundary
        size=float(size),     # Decimal → float at SDK boundary
        side=side,
    )

    try:
        signed_order = client.create_order(order_args)
    except Exception as exc:
        raise RuntimeError(f"V2 order construction failed: {exc}") from exc

    # Convert SDK object to plain dict (SDK returns SignedOrderV2 dataclass/namedtuple)
    if isinstance(signed_order, dict):
        order_dict: dict[str, Any] = signed_order
    elif hasattr(signed_order, "_asdict"):
        order_dict = signed_order._asdict()  # type: ignore[union-attr]
    else:
        order_dict = vars(signed_order)

    _log.info(
        "v2_order_built",
        extra={
            "token_id": token_id,
            "side": side,
            "price": str(price),
            "size": str(size),
            "neg_risk": neg_risk,
            # Do NOT log any field that might contain the private key
        },
    )
    return order_dict


def make_test_client(
    token_id: str = "test_token",
    tick_size: str = "0.01",
    neg_risk: bool = False,
    chain_id: int = 137,
    private_key: str | None = None,
) -> Any:
    """Create a ClobClient with caches pre-populated for offline testing.

    Uses a dummy private key if none provided (valid Ethereum key format
    required by eth-account; the key below is a well-known test key with
    no real funds — never use in production).
    """
    from py_clob_client_v2.client import ClobClient

    # Hardhat default test account #0 — public test key from Hardhat documentation.
    # See https://hardhat.org/hardhat-network/docs/reference#accounts
    _TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    key = private_key or _TEST_KEY

    client = ClobClient(host="https://clob.polymarket.com", chain_id=chain_id, key=key)
    client._ClobClient__tick_sizes[token_id] = tick_size   # type: ignore[attr-defined]
    client._ClobClient__neg_risk[token_id] = neg_risk       # type: ignore[attr-defined]
    client._ClobClient__cached_version = 2                   # type: ignore[attr-defined]
    return client
