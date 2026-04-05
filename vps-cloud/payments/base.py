"""
payments/base.py – Abstract base class for payment provider adapters.

Defines the interface that every concrete payment integration must
implement so the rest of the application can be provider-agnostic.
"""

from abc import ABC, abstractmethod


class BasePaymentProvider(ABC):
    """Payment provider adapter interface.

    Each concrete subclass wraps a specific payment gateway (Segpay,
    Stripe, etc.) and exposes a uniform surface to the rest of the
    application.
    """

    @abstractmethod
    async def create_checkout_session(
        self,
        order_id: str,
        cart: list[dict],
        customer_email: str,
        total_amount: float,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Create a payment session and return a redirect URL.

        Parameters
        ----------
        order_id:
            Our internal order UUID.
        cart:
            List of cart item dicts (product_id, quantity, price, name).
        customer_email:
            Buyer's email address.
        total_amount:
            Total order amount in USD.
        success_url:
            URL to redirect the buyer after successful payment.
        cancel_url:
            URL to redirect the buyer if they cancel.

        Returns
        -------
        str
            Absolute URL to redirect the user to for payment.

        Raises
        ------
        ValueError
            If the provider is misconfigured and cannot build a URL.
        """

    @abstractmethod
    async def create_one_time_charge(
        self,
        amount_cents: int,
        metadata: dict,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """Create a one-time payment charge (tip, PPV, download, etc.).

        Parameters
        ----------
        amount_cents:
            Amount to charge in cents.
        metadata:
            Arbitrary key-value data to pass through to the payment provider
            (e.g. creator_handle, content_type, user_id).
        success_url:
            Redirect URL on successful payment.
        cancel_url:
            Redirect URL on cancellation.

        Returns
        -------
        str
            Absolute URL to redirect the user to for payment.
        """

    @abstractmethod
    async def verify_webhook(self, request_body: bytes, headers: dict) -> dict:
        """Verify an incoming webhook/postback and extract order data.

        Parameters
        ----------
        request_body:
            Raw bytes of the HTTP request body.
        headers:
            HTTP request headers as a plain dict.

        Returns
        -------
        dict
            Parsed payload containing at minimum:

            - ``external_transaction_id`` – provider transaction ID.
            - ``order_id``               – our internal order UUID.
            - ``status``                 – normalised status string
              (``'paid'``, ``'failed'``, etc.).
            - ``customer_email``         – buyer e-mail if available.

        Raises
        ------
        ValueError
            If the signature is invalid or the payload is malformed.
        """
