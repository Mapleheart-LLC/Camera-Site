"""
payments/__init__.py – Payment provider factory.

Usage::

    from payments import get_payment_provider

    provider = get_payment_provider()          # uses PAYMENT_PROVIDER env var
    provider = get_payment_provider("segpay")  # explicit provider name
"""

import os

from .base import BasePaymentProvider


def get_payment_provider(provider_name: str | None = None) -> BasePaymentProvider:
    """Return an instance of the requested payment provider.

    When *provider_name* is ``None`` the value of the ``PAYMENT_PROVIDER``
    environment variable is used, falling back to ``'segpay'``.

    Parameters
    ----------
    provider_name:
        Case-insensitive name of the provider (e.g. ``'segpay'``).

    Raises
    ------
    ValueError
        If the requested provider name is not recognised.
    """
    name = (provider_name or os.environ.get("PAYMENT_PROVIDER", "segpay")).lower()

    if name == "segpay":
        from .segpay import SegpayProvider

        return SegpayProvider()

    raise ValueError(f"Unknown payment provider: '{name}'")
