"""
payments/segpay.py – Segpay payment provider adapter.

Implements the Segpay S2S (Server-to-Server) integration:

  - Checkout:  builds a Segpay dynamic-pricing URL and returns it so the
               caller can redirect the user.
  - Webhook:   verifies a Segpay HTTP postback, validates an optional
               HMAC-SHA256 signature, and returns normalised order data.

Required environment variables
-------------------------------
SEGPAY_PACKAGE_ID      – Merchant package ID assigned by Segpay.
SEGPAY_PRICE_POINT_ID  – Default price-point ID for the package.
BASE_URL               – Public root URL (e.g. https://mochii.live) used
                         to build the postback / return URLs.

Optional environment variables
-------------------------------
SEGPAY_WEBHOOK_SECRET  – Shared secret for postback HMAC-SHA256 verification.
                         Leave empty to skip signature checking.
"""

import hashlib
import hmac
import logging
import os
from urllib.parse import parse_qs, urlencode

from .base import BasePaymentProvider

logger = logging.getLogger(__name__)

_SEGPAY_PURCHASE_BASE = "https://purchase.segpay.com/hosted/index.asp"

# Segpay API limit for the x-description field.
_SEGPAY_MAX_DESCRIPTION_LENGTH = 50

# Segpay response codes that indicate a successful payment.
_PAID_CODES = frozenset({"1", "approved", "success"})


class SegpayProvider(BasePaymentProvider):
    """Implements :class:`BasePaymentProvider` for Segpay."""

    async def create_checkout_session(
        self,
        order_id: str,
        cart: list[dict],
        customer_email: str,
        total_amount: float,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Build and return a Segpay dynamic-pricing checkout URL."""
        package_id = os.environ.get("SEGPAY_PACKAGE_ID", "")
        price_point_id = os.environ.get("SEGPAY_PRICE_POINT_ID", "")
        base_url = os.environ.get("BASE_URL", "").rstrip("/")

        if not package_id or not price_point_id:
            raise ValueError(
                "SEGPAY_PACKAGE_ID and SEGPAY_PRICE_POINT_ID must be configured."
            )

        postback_url = f"{base_url}/api/webhooks/payments/segpay"

        # Build a human-readable description from cart items (≤50 chars).
        item_names = ", ".join(item.get("name", "item") for item in cart)
        description = item_names[:_SEGPAY_MAX_DESCRIPTION_LENGTH] if item_names else "mochii.live order"

        params = {
            "x-eticketid": f"{package_id}:{price_point_id}",
            "x-amount": f"{total_amount:.2f}",
            "x-amountusd": f"{total_amount:.2f}",
            "x-currencycode": "USD",
            "x-description": description,
            "x-referenceId": order_id,
            "x-billemail": customer_email,
            "x-postbackurl": postback_url,
            "x-successurl": success_url,
            "x-cancelurl": cancel_url,
        }

        checkout_url = f"{_SEGPAY_PURCHASE_BASE}?{urlencode(params)}"
        logger.info("Created Segpay checkout URL for order %s", order_id)
        return checkout_url

    async def verify_webhook(self, request_body: bytes, headers: dict) -> dict:
        """Parse and verify a Segpay HTTP postback.

        Segpay sends a form-encoded POST to the registered postback URL.
        When ``SEGPAY_WEBHOOK_SECRET`` is set this method validates the
        HMAC-SHA256 signature carried in the ``x-sig`` field / header
        before processing the payload.
        """
        webhook_secret = os.environ.get("SEGPAY_WEBHOOK_SECRET", "")

        # Parse form-encoded body.
        try:
            parsed = parse_qs(
                request_body.decode("utf-8", errors="replace"),
                keep_blank_values=True,
            )
        except Exception as exc:
            raise ValueError(f"Failed to parse Segpay postback body: {exc}") from exc

        def _first(key: str) -> str:
            return parsed.get(key, [""])[0]

        # Verify HMAC when a shared secret is configured.
        if webhook_secret:
            provided_sig = _first("x-sig") or headers.get("x-sig", "")
            if not provided_sig:
                raise ValueError("Missing Segpay webhook signature (x-sig).")
            expected = hmac.new(
                webhook_secret.encode(),
                request_body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, provided_sig.lower()):
                raise ValueError("Segpay webhook signature mismatch.")

        transaction_id = _first("x-transactionid") or _first("trans_id")
        order_id = _first("x-referenceId") or _first("x-referenceid")
        email = _first("x-billemail") or _first("email")
        response_code = (
            _first("x-responsecode") or _first("response_code") or ""
        ).lower()

        # Normalise payment status.
        payment_status = "paid" if response_code in _PAID_CODES else "failed"

        return {
            "external_transaction_id": transaction_id,
            "order_id": order_id,
            "status": payment_status,
            "customer_email": email,
        }
