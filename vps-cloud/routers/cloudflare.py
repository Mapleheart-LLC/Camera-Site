"""
routers/cloudflare.py – Cloudflare API integration for mochii.live.

Exposes admin-protected endpoints for the four permission scopes granted to
the CLAPI token: DNS:Edit, Load Balancers:Edit, Analytics:Read,
Email Routing Rules:Edit.

Environment variables
---------------------
CLAPI
    Scoped Cloudflare API token (required).
CLAPI_ZONE_ID
    Zone ID for the site's domain.  When not set, the zone is
    auto-discovered once from the API using BASE_URL and cached for the
    lifetime of the process.
CLAPI_TUNNEL_HOSTNAME
    Cloudflare Tunnel edge hostname (e.g. ``<uuid>.cfargotunnel.com``).
    When set, new creator subdomains are automatically provisioned as
    proxied CNAME records pointing at the tunnel on creation and removed
    on deletion.  Leave empty to manage DNS records manually.
CLAPI_ACCOUNT_ID
    Cloudflare account ID.  When not set, auto-discovered once via
    ``GET /accounts`` and cached for the process lifetime.  Required for
    tunnel ingress route management (Cloudflare Tunnel:Edit).
CLAPI_TUNNEL_SERVICE
    Internal service URL that the tunnel should forward all public hostnames
    to (e.g. ``http://backend:8000``).  Required for tunnel ingress route
    management.  Leave empty to skip tunnel ingress auto-configuration.
CLAPI_EMAIL_ROUTING_DEST
    Global fallback e-mail address for per-creator routing rules (e.g.
    ``admin@mochii.live``).  Used only when a creator has no
    ``forwarding_email`` and no ``agent_email`` set.  When set, a catch-all
    rule for ``{handle}@{root_domain}`` is created on creator provisioning
    and deleted on de-provisioning.  Leave empty to skip auto-email-routing.

Admin-protected endpoints
-------------------------
GET  /api/admin/cloudflare/status
GET  /api/admin/cloudflare/analytics?since=<minutes|-iso>&until=<minutes|0>
GET  /api/admin/cloudflare/dns[?type=&name=]
POST /api/admin/cloudflare/dns
DELETE /api/admin/cloudflare/dns/{record_id}
GET  /api/admin/cloudflare/email-routing/rules
POST /api/admin/cloudflare/email-routing/rules
DELETE /api/admin/cloudflare/email-routing/rules/{rule_id}
GET  /api/admin/cloudflare/load-balancers
POST /api/admin/cloudflare/load-balancers
PATCH /api/admin/cloudflare/load-balancers/{lb_id}
DELETE /api/admin/cloudflare/load-balancers/{lb_id}
"""

import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dependencies import get_admin_user

router = APIRouter(prefix="/api/admin/cloudflare", tags=["cloudflare"])
logger = logging.getLogger(__name__)

_CF_BASE = "https://api.cloudflare.com/client/v4"

# Well-known platform subdomain prefixes that are auto-provisioned on startup.
_PLATFORM_SUBDOMAINS = ("anon", "links", "shop", "drool", "creator", "member", "www")

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _cf_token() -> str:
    return os.environ.get("CLAPI", "")


def _cf_zone_id_env() -> str:
    return os.environ.get("CLAPI_ZONE_ID", "")


def _tunnel_hostname() -> str:
    return os.environ.get("CLAPI_TUNNEL_HOSTNAME", "")


def _email_routing_dest() -> str:
    return os.environ.get("CLAPI_EMAIL_ROUTING_DEST", "")


def _cf_account_id_env() -> str:
    return os.environ.get("CLAPI_ACCOUNT_ID", "")


def _tunnel_service() -> str:
    return os.environ.get("CLAPI_TUNNEL_SERVICE", "")


# Zone ID cached after first auto-discovery so we only call the API once.
_cached_zone_id: Optional[str] = None

# Account ID cached after first auto-discovery.
_cached_account_id: Optional[str] = None

# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_cf_token()}",
        "Content-Type": "application/json",
    }


def _require_token() -> None:
    if not _cf_token():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLAPI token not configured.",
        )


def _discover_zone_id_sync(hostname: str) -> Optional[str]:
    """Look up the Cloudflare zone ID for *hostname*.

    Strips one subdomain level at a time until the apex is reached so that
    both ``sub.example.com`` and ``example.com`` resolve correctly.
    Returns the zone ID of the first matching zone, or ``None``.
    """
    parts = hostname.lower().rstrip(".").split(".")
    while len(parts) >= 2:
        candidate = ".".join(parts)
        try:
            r = httpx.get(
                f"{_CF_BASE}/zones",
                params={"name": candidate},
                headers=_auth_headers(),
                timeout=10.0,
            )
            data = r.json()
            results = data.get("result", [])
            if results:
                return results[0]["id"]
        except (httpx.RequestError, ValueError, KeyError) as exc:
            logger.warning("CF zone lookup for '%s' failed: %s", candidate, exc)
            return None
        parts = parts[1:]
    return None


def _get_zone_id() -> str:
    """Return the effective zone ID, auto-discovering it if necessary.

    Raises HTTP 503 if the zone cannot be determined.
    """
    global _cached_zone_id
    zone_id = _cf_zone_id_env() or _cached_zone_id
    if not zone_id:
        base_url = os.environ.get("BASE_URL", "").rstrip("/")
        hostname = urlparse(base_url).hostname or ""
        if hostname:
            zone_id = _discover_zone_id_sync(hostname)
            if zone_id:
                _cached_zone_id = zone_id
                logger.info("Auto-discovered Cloudflare zone ID: %s", zone_id)
    if not zone_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Cloudflare zone ID could not be determined. "
                "Set CLAPI_ZONE_ID or ensure BASE_URL matches a zone in your account."
            ),
        )
    return zone_id


def _get_account_id() -> str:
    """Return the effective Cloudflare account ID, auto-discovering if necessary.

    Raises HTTP 503 if the account cannot be determined.
    """
    global _cached_account_id
    account_id = _cf_account_id_env() or _cached_account_id
    if not account_id:
        try:
            r = httpx.get(
                f"{_CF_BASE}/accounts",
                headers=_auth_headers(),
                timeout=10.0,
            )
            data = r.json()
            results = data.get("result", [])
            if results:
                account_id = results[0]["id"]
                _cached_account_id = account_id
                logger.info("Auto-discovered Cloudflare account ID: %s", account_id)
        except (httpx.RequestError, ValueError, KeyError) as exc:
            logger.warning("CF account ID lookup failed: %s", exc)
    if not account_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Cloudflare account ID could not be determined. "
                "Set CLAPI_ACCOUNT_ID or ensure the token can list accounts."
            ),
        )
    return account_id


def _tunnel_id_from_hostname() -> str:
    """Extract the tunnel UUID from CLAPI_TUNNEL_HOSTNAME.

    E.g. ``23153a57-a144-40cb-9ed6-4e4e44d3ebcf.cfargotunnel.com``
    → ``23153a57-a144-40cb-9ed6-4e4e44d3ebcf``.
    Returns an empty string when CLAPI_TUNNEL_HOSTNAME is not set or has no UUID prefix.
    """
    host = _tunnel_hostname()
    if not host:
        return ""
    return host.split(".")[0]


def _get_tunnel_ingress(account_id: str, tunnel_id: str) -> list[dict]:
    """Fetch the current ingress rules for the given tunnel.

    Returns a list of ingress rule dicts.  The trailing catch-all rule
    (which has no ``hostname`` key) is included.
    """
    data = _cf_request("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")
    return data.get("result", {}).get("config", {}).get("ingress", [])


def _put_tunnel_ingress(account_id: str, tunnel_id: str, ingress: list[dict]) -> None:
    """Replace the tunnel ingress configuration with *ingress*.

    Always ensures there is a catch-all rule at the end (``{"service": "http_status:404"}``).
    """
    # Remove any existing catch-all entries then append one clean one at the end.
    rules = [r for r in ingress if r.get("hostname")]
    rules.append({"service": "http_status:404"})
    _cf_request(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        json={"config": {"ingress": rules}},
    )


def ensure_tunnel_ingress_routes(hostnames: list[str], service: str) -> None:
    """Idempotently add *hostnames* as ingress routes on the Cloudflare Tunnel.

    Reads the current tunnel config, merges any missing hostnames (pointing at
    *service*), then writes the config back.  Existing routes are preserved and
    never duplicated.  Best-effort — errors are logged but never raised.

    Requires ``CLAPI_TUNNEL_HOSTNAME`` (to derive the tunnel ID) and either
    ``CLAPI_ACCOUNT_ID`` or a token that can list accounts.
    ``CLAPI_TUNNEL_SERVICE`` (or the *service* argument) must be non-empty.
    """
    tunnel_id = _tunnel_id_from_hostname()
    if not tunnel_id:
        logger.debug("CLAPI_TUNNEL_HOSTNAME not set – skipping tunnel ingress provisioning.")
        return
    if not service:
        logger.debug("CLAPI_TUNNEL_SERVICE not set – skipping tunnel ingress provisioning.")
        return
    try:
        account_id = _get_account_id()
    except HTTPException as exc:
        logger.warning("CF tunnel ingress: account ID lookup failed: %s", exc.detail)
        return
    try:
        current = _get_tunnel_ingress(account_id, tunnel_id)
    except HTTPException as exc:
        logger.warning("CF tunnel ingress: failed to read current config: %s", exc.detail)
        return

    existing_hosts = {r["hostname"] for r in current if r.get("hostname")}
    new_routes = [r for r in current if r.get("hostname")]
    added: list[str] = []
    for hostname in hostnames:
        if hostname not in existing_hosts:
            new_routes.append({"hostname": hostname, "service": service})
            added.append(hostname)

    if not added:
        logger.debug("CF tunnel ingress: all hostnames already present – nothing to add.")
        return

    try:
        _put_tunnel_ingress(account_id, tunnel_id, new_routes)
        logger.info("CF tunnel ingress: added routes for %s → %s", added, service)
    except HTTPException as exc:
        logger.warning("CF tunnel ingress: failed to update config: %s", exc.detail)


def remove_tunnel_ingress_route(hostname: str) -> None:
    """Remove *hostname* from the Cloudflare Tunnel ingress config if present.

    Best-effort — errors are logged but never raised.
    """
    tunnel_id = _tunnel_id_from_hostname()
    if not tunnel_id:
        return
    try:
        account_id = _get_account_id()
    except HTTPException as exc:
        logger.warning("CF tunnel ingress remove: account ID lookup failed: %s", exc.detail)
        return
    try:
        current = _get_tunnel_ingress(account_id, tunnel_id)
    except HTTPException as exc:
        logger.warning("CF tunnel ingress remove: failed to read current config: %s", exc.detail)
        return

    updated = [r for r in current if r.get("hostname") and r["hostname"] != hostname]
    if len(updated) == len([r for r in current if r.get("hostname")]):
        logger.debug("CF tunnel ingress: hostname %s not found – nothing to remove.", hostname)
        return

    try:
        _put_tunnel_ingress(account_id, tunnel_id, updated)
        logger.info("CF tunnel ingress: removed route for %s", hostname)
    except HTTPException as exc:
        logger.warning("CF tunnel ingress remove: failed to update config: %s", exc.detail)



    """Make a *synchronous* Cloudflare API request and return the JSON body.

    Raises ``HTTPException`` on network errors or API-level failures.
    """
    _require_token()
    url = f"{_CF_BASE}{path}"
    try:
        r = httpx.request(method, url, headers=_auth_headers(), timeout=15.0, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cloudflare API unreachable: {exc}") from exc
    try:
        data = r.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Cloudflare returned a non-JSON response.") from exc
    if not data.get("success", False):
        errors = data.get("errors", [])
        msg = errors[0].get("message", "Unknown Cloudflare error") if errors else "Unknown Cloudflare error"
        raise HTTPException(status_code=r.status_code or 502, detail=f"Cloudflare: {msg}")
    return data


async def _cf_request_async(method: str, path: str, **kwargs) -> dict:
    """Make an *asynchronous* Cloudflare API request and return the JSON body.

    Raises ``HTTPException`` on network errors or API-level failures.
    """
    _require_token()
    url = f"{_CF_BASE}{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.request(method, url, headers=_auth_headers(), **kwargs)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cloudflare API unreachable: {exc}") from exc
    try:
        data = r.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Cloudflare returned a non-JSON response.") from exc
    if not data.get("success", False):
        errors = data.get("errors", [])
        msg = errors[0].get("message", "Unknown Cloudflare error") if errors else "Unknown Cloudflare error"
        raise HTTPException(status_code=r.status_code or 502, detail=f"Cloudflare: {msg}")
    return data


# ---------------------------------------------------------------------------
# Module-level helpers (synchronous, best-effort — called from admin.py)
# ---------------------------------------------------------------------------


def provision_creator_subdomain(
    handle: str,
    root_domain: str,
    forwarding_email: Optional[str] = None,
    agent_email: Optional[str] = None,
) -> bool:
    """Provision Cloudflare resources for a newly created creator.

    Creates:
    - A proxied CNAME record ``{handle}.{root_domain}`` → ``CLAPI_TUNNEL_HOSTNAME``
      (only when ``CLAPI_TUNNEL_HOSTNAME`` is configured).
    - A tunnel ingress route for ``{handle}.{root_domain}`` → ``CLAPI_TUNNEL_SERVICE``
      (only when both ``CLAPI_TUNNEL_HOSTNAME`` and ``CLAPI_TUNNEL_SERVICE`` are configured).
    - An email routing rule forwarding ``{handle}@{root_domain}`` to the
      appropriate destinations, resolved in this order:

      1. If *forwarding_email* or *agent_email* are given, the routing rule
         forwards to those addresses (both when both are present, otherwise
         whichever is set).
      2. Falls back to ``CLAPI_EMAIL_ROUTING_DEST`` only when neither
         per-creator address is provided.
      3. Skips email routing entirely when no destination is available.

    Both operations are best-effort: failures are logged as warnings but do
    not raise so that creator creation never blocks on Cloudflare.
    Returns ``True`` if the DNS record was created.
    """
    if not _cf_token():
        logger.debug("CLAPI not set – skipping auto-provision for '@%s'.", handle)
        return False

    try:
        zone_id = _get_zone_id()
    except HTTPException as exc:
        logger.warning("CF auto-provision: zone lookup failed: %s", exc.detail)
        return False

    dns_ok = False

    # ── DNS CNAME ─────────────────────────────────────────────────────────
    tunnel_host = _tunnel_hostname()
    if tunnel_host:
        try:
            _cf_request(
                "POST",
                f"/zones/{zone_id}/dns_records",
                json={
                    "type": "CNAME",
                    "name": f"{handle}.{root_domain}",
                    "content": tunnel_host,
                    "proxied": True,
                    "ttl": 1,
                    "comment": f"Auto-created for creator @{handle}",
                },
            )
            logger.info("CF: created CNAME %s.%s → %s", handle, root_domain, tunnel_host)
            dns_ok = True
        except HTTPException as exc:
            logger.warning("CF: CNAME creation failed for @%s: %s", handle, exc.detail)
    else:
        logger.debug(
            "CLAPI_TUNNEL_HOSTNAME not set – skipping CNAME auto-creation for @%s.", handle
        )

    # ── Tunnel ingress route ───────────────────────────────────────────────
    service = _tunnel_service()
    if tunnel_host and service:
        ensure_tunnel_ingress_routes([f"{handle}.{root_domain}"], service)

    # ── Email routing rule ─────────────────────────────────────────────────
    # Build destination list: per-creator addresses take precedence; fall back
    # to the global CLAPI_EMAIL_ROUTING_DEST only when neither is supplied.
    destinations: list[str] = [e for e in [forwarding_email, agent_email] if e]
    if not destinations:
        global_dest = _email_routing_dest()
        if global_dest:
            destinations = [global_dest]

    if destinations:
        try:
            _cf_request(
                "POST",
                f"/zones/{zone_id}/email/routing/rules",
                json={
                    "name": f"Creator @{handle}",
                    "enabled": True,
                    "priority": 10,
                    "matchers": [
                        {
                            "type": "literal",
                            "field": "to",
                            "value": f"{handle}@{root_domain}",
                        }
                    ],
                    "actions": [{"type": "forward", "value": destinations}],
                },
            )
            logger.info(
                "CF: created email routing rule %s@%s → %s",
                handle, root_domain, destinations,
            )
        except HTTPException as exc:
            logger.warning(
                "CF: email routing rule creation failed for @%s: %s", handle, exc.detail
            )
    else:
        logger.debug(
            "No email destination configured – skipping email routing for @%s.", handle
        )

    return dns_ok


def ensure_platform_subdomains(root_domain: str) -> None:
    """Ensure all required platform subdomains exist as DNS records and tunnel ingress routes.

    Checks each well-known subdomain prefix (anon, links, shop, drool, creator,
    member, www) and creates a proxied CNAME record pointing at
    ``CLAPI_TUNNEL_HOSTNAME`` for any that are missing.  Existing records are
    left untouched.

    Also adds each subdomain (plus the bare root domain) as a tunnel ingress
    route when ``CLAPI_TUNNEL_SERVICE`` is configured, so that Cloudflare
    forwards traffic through the tunnel to the backend service.

    Called automatically at application startup.  Requires both ``CLAPI`` and
    ``CLAPI_TUNNEL_HOSTNAME`` to be configured; silently skips otherwise.
    Best-effort — failures are logged as warnings and never raised.
    """
    if not _cf_token():
        logger.debug("CLAPI not set – skipping platform subdomain auto-provisioning.")
        return

    tunnel_host = _tunnel_hostname()
    if not tunnel_host:
        logger.debug(
            "CLAPI_TUNNEL_HOSTNAME not set – skipping platform subdomain auto-provisioning."
        )
        return

    try:
        zone_id = _get_zone_id()
    except HTTPException as exc:
        logger.warning("CF platform subdomain provisioning: zone lookup failed: %s", exc.detail)
        return

    for prefix in _PLATFORM_SUBDOMAINS:
        fqdn = f"{prefix}.{root_domain}"
        try:
            existing = _cf_request(
                "GET",
                f"/zones/{zone_id}/dns_records",
                params={"name": fqdn},
            )
            if existing.get("result"):
                logger.debug("CF: subdomain %s already exists – skipping.", fqdn)
                continue
            _cf_request(
                "POST",
                f"/zones/{zone_id}/dns_records",
                json={
                    "type": "CNAME",
                    "name": fqdn,
                    "content": tunnel_host,
                    "proxied": True,
                    "ttl": 1,
                    "comment": "Auto-provisioned platform subdomain",
                },
            )
            logger.info("CF: created platform CNAME %s → %s", fqdn, tunnel_host)
        except HTTPException as exc:
            logger.warning("CF: failed to provision platform subdomain %s: %s", fqdn, exc.detail)

    # ── Tunnel ingress routes ──────────────────────────────────────────────
    service = _tunnel_service()
    if service:
        all_hostnames = [f"{p}.{root_domain}" for p in _PLATFORM_SUBDOMAINS] + [root_domain]
        ensure_tunnel_ingress_routes(all_hostnames, service)


def deprovision_creator_subdomain(handle: str, root_domain: str) -> bool:
    """Remove Cloudflare resources tied to a deleted creator.

    Deletes:
    - Any CNAME records whose name is ``{handle}.{root_domain}``.
    - The tunnel ingress route for ``{handle}.{root_domain}`` (when configured).
    - Any email routing rules whose first ``to``-literal matcher targets
      ``{handle}@{root_domain}``.

    Best-effort — failures are logged as warnings and never raised.
    Returns ``True`` if at least one DNS record was deleted.
    """
    if not _cf_token():
        return False

    try:
        zone_id = _get_zone_id()
    except HTTPException as exc:
        logger.warning("CF deprovision: zone lookup failed: %s", exc.detail)
        return False

    dns_ok = False
    fqdn = f"{handle}.{root_domain}"

    # ── DNS CNAME ─────────────────────────────────────────────────────────
    try:
        records_data = _cf_request(
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={"name": fqdn, "type": "CNAME"},
        )
        for rec in records_data.get("result", []):
            _cf_request("DELETE", f"/zones/{zone_id}/dns_records/{rec['id']}")
            logger.info("CF: deleted CNAME record %s (id=%s)", fqdn, rec["id"])
            dns_ok = True
    except HTTPException as exc:
        logger.warning("CF: CNAME deletion failed for @%s: %s", handle, exc.detail)

    # ── Tunnel ingress route ───────────────────────────────────────────────
    remove_tunnel_ingress_route(fqdn)

    # ── Email routing rule ─────────────────────────────────────────────────
    target_email = f"{handle}@{root_domain}"
    try:
        rules_data = _cf_request("GET", f"/zones/{zone_id}/email/routing/rules")
        for rule in rules_data.get("result", []):
            for matcher in rule.get("matchers", []):
                if matcher.get("value", "") == target_email:
                    _cf_request(
                        "DELETE",
                        f"/zones/{zone_id}/email/routing/rules/{rule['id']}",
                    )
                    logger.info(
                        "CF: deleted email routing rule for %s (id=%s)",
                        target_email, rule["id"],
                    )
                    break
    except HTTPException as exc:
        logger.warning(
            "CF: email routing rule deletion failed for @%s: %s", handle, exc.detail
        )

    return dns_ok


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class DNSRecordCreate(BaseModel):
    type: str = Field(..., description="Record type: A, AAAA, CNAME, MX, TXT, etc.")
    name: str = Field(..., description="DNS record name (e.g. 'sub.example.com' or '@').")
    content: str = Field(..., description="Record value (IP, hostname, text content, …).")
    ttl: int = Field(1, description="Time-to-live in seconds. Use 1 for Cloudflare's 'auto'.")
    proxied: bool = Field(False, description="Whether to proxy the record through Cloudflare.")
    comment: Optional[str] = Field(None, description="Optional note attached to the record.")


class EmailRoutingRuleCreate(BaseModel):
    name: str = Field(..., description="Human-readable label for this rule.")
    enabled: bool = Field(True)
    priority: int = Field(
        10, description="Rule priority — lower values take precedence."
    )
    matchers: list[dict] = Field(
        ...,
        description=(
            'List of matcher objects, e.g. '
            '[{"type": "literal", "field": "to", "value": "user@example.com"}]'
        ),
    )
    actions: list[dict] = Field(
        ...,
        description=(
            'List of action objects, e.g. '
            '[{"type": "forward", "value": ["dest@example.com"]}]'
        ),
    )


class LoadBalancerCreate(BaseModel):
    name: str = Field(
        ..., description="Hostname for the load balancer (e.g. 'lb.example.com')."
    )
    fallback_pool: str = Field(..., description="ID of the fallback origin pool.")
    default_pools: list[str] = Field(
        ..., description="Ordered list of origin pool IDs."
    )
    proxied: bool = Field(True)
    ttl: int = Field(30)
    description: Optional[str] = None


class LoadBalancerPatch(BaseModel):
    name: Optional[str] = None
    fallback_pool: Optional[str] = None
    default_pools: Optional[list[str]] = None
    proxied: Optional[bool] = None
    ttl: Optional[int] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Route: Status / health check
# ---------------------------------------------------------------------------


@router.get("/status")
async def cloudflare_status(_: str = Depends(get_admin_user)):
    """Return Cloudflare integration health: token validity, zone resolution, and optional features."""
    token = _cf_token()
    if not token:
        return {
            "configured": False,
            "token_set": False,
            "zone_id": None,
            "zone_auto_discovered": False,
            "tunnel_hostname_set": False,
            "tunnel_service_set": False,
            "email_routing_dest_set": False,
        }

    # Verify the token is active via the token introspection endpoint.
    token_valid = False
    try:
        data = await _cf_request_async("GET", "/user/tokens/verify")
        token_valid = data.get("result", {}).get("status") == "active"
    except HTTPException:
        pass

    # Resolve zone ID (may trigger a one-time API call).
    zone_id: Optional[str] = None
    zone_auto = False
    try:
        zone_id = _get_zone_id()
        zone_auto = not bool(_cf_zone_id_env())
    except HTTPException:
        pass

    return {
        "configured": True,
        "token_set": True,
        "token_valid": token_valid,
        "zone_id": zone_id or "(not found — set CLAPI_ZONE_ID)",
        "zone_auto_discovered": zone_auto,
        "tunnel_hostname_set": bool(_tunnel_hostname()),
        "tunnel_id": _tunnel_id_from_hostname() or None,
        "tunnel_service_set": bool(_tunnel_service()),
        "tunnel_ingress_auto_provisioning": bool(_tunnel_hostname() and _tunnel_service()),
        "email_routing_dest_set": bool(_email_routing_dest()),
    }


# ---------------------------------------------------------------------------
# Routes: Analytics (Analytics:Read)
# ---------------------------------------------------------------------------


@router.get("/analytics")
async def cloudflare_analytics(
    since: str = Query(
        "-10080",
        description=(
            "Start of reporting period. "
            "Use a negative integer for minutes-ago (e.g. -1440 for 24 h) "
            "or an ISO 8601 timestamp."
        ),
    ),
    until: str = Query(
        "0",
        description=(
            "End of reporting period. "
            "Use 0 for now, or an ISO 8601 timestamp."
        ),
    ),
    _: str = Depends(get_admin_user),
):
    """Return zone analytics from Cloudflare (requests, bandwidth, threats, page views)."""
    zone_id = _get_zone_id()
    data = await _cf_request_async(
        "GET",
        f"/zones/{zone_id}/analytics/dashboard",
        params={"since": since, "until": until, "continuous": "false"},
    )
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Routes: DNS records (DNS:Edit)
# ---------------------------------------------------------------------------


@router.get("/dns")
async def cloudflare_list_dns(
    record_type: Optional[str] = Query(
        None, alias="type", description="Filter by record type (A, CNAME, MX, TXT, …)."
    ),
    name: Optional[str] = Query(None, description="Filter by record name."),
    _: str = Depends(get_admin_user),
):
    """List all DNS records in the zone, with optional type and name filters."""
    zone_id = _get_zone_id()
    params: dict[str, str] = {}
    if record_type:
        params["type"] = record_type.upper()
    if name:
        params["name"] = name
    data = await _cf_request_async(
        "GET", f"/zones/{zone_id}/dns_records", params=params
    )
    return data.get("result", [])


@router.post("/dns", status_code=status.HTTP_201_CREATED)
async def cloudflare_create_dns(
    payload: DNSRecordCreate,
    _: str = Depends(get_admin_user),
):
    """Create a new DNS record in the zone."""
    zone_id = _get_zone_id()
    body: dict[str, Any] = {
        "type": payload.type.upper(),
        "name": payload.name,
        "content": payload.content,
        "ttl": payload.ttl,
        "proxied": payload.proxied,
    }
    if payload.comment:
        body["comment"] = payload.comment
    data = await _cf_request_async(
        "POST", f"/zones/{zone_id}/dns_records", json=body
    )
    return data.get("result", {})


@router.delete("/dns/{record_id}", status_code=status.HTTP_200_OK)
async def cloudflare_delete_dns(
    record_id: str,
    _: str = Depends(get_admin_user),
):
    """Delete a DNS record by its Cloudflare record ID."""
    zone_id = _get_zone_id()
    data = await _cf_request_async(
        "DELETE", f"/zones/{zone_id}/dns_records/{record_id}"
    )
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Routes: Email Routing Rules (Email Routing Rules:Edit)
# ---------------------------------------------------------------------------


@router.get("/email-routing/rules")
async def cloudflare_list_email_routing_rules(_: str = Depends(get_admin_user)):
    """List all email routing rules for the zone."""
    zone_id = _get_zone_id()
    data = await _cf_request_async(
        "GET", f"/zones/{zone_id}/email/routing/rules"
    )
    return data.get("result", [])


@router.post("/email-routing/rules", status_code=status.HTTP_201_CREATED)
async def cloudflare_create_email_routing_rule(
    payload: EmailRoutingRuleCreate,
    _: str = Depends(get_admin_user),
):
    """Create a new email routing rule."""
    zone_id = _get_zone_id()
    data = await _cf_request_async(
        "POST",
        f"/zones/{zone_id}/email/routing/rules",
        json={
            "name": payload.name,
            "enabled": payload.enabled,
            "priority": payload.priority,
            "matchers": payload.matchers,
            "actions": payload.actions,
        },
    )
    return data.get("result", {})


@router.delete("/email-routing/rules/{rule_id}", status_code=status.HTTP_200_OK)
async def cloudflare_delete_email_routing_rule(
    rule_id: str,
    _: str = Depends(get_admin_user),
):
    """Delete an email routing rule by its Cloudflare rule ID."""
    zone_id = _get_zone_id()
    data = await _cf_request_async(
        "DELETE", f"/zones/{zone_id}/email/routing/rules/{rule_id}"
    )
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Routes: Load Balancers (Load Balancers:Edit)
# ---------------------------------------------------------------------------


@router.get("/load-balancers")
async def cloudflare_list_load_balancers(_: str = Depends(get_admin_user)):
    """List all load balancers configured on the zone."""
    zone_id = _get_zone_id()
    data = await _cf_request_async("GET", f"/zones/{zone_id}/load_balancers")
    return data.get("result", [])


@router.post("/load-balancers", status_code=status.HTTP_201_CREATED)
async def cloudflare_create_load_balancer(
    payload: LoadBalancerCreate,
    _: str = Depends(get_admin_user),
):
    """Create a new load balancer on the zone."""
    zone_id = _get_zone_id()
    body: dict[str, Any] = {
        "name": payload.name,
        "fallback_pool": payload.fallback_pool,
        "default_pools": payload.default_pools,
        "proxied": payload.proxied,
        "ttl": payload.ttl,
    }
    if payload.description:
        body["description"] = payload.description
    data = await _cf_request_async(
        "POST", f"/zones/{zone_id}/load_balancers", json=body
    )
    return data.get("result", {})


@router.patch("/load-balancers/{lb_id}")
async def cloudflare_patch_load_balancer(
    lb_id: str,
    payload: LoadBalancerPatch,
    _: str = Depends(get_admin_user),
):
    """Update one or more fields on an existing load balancer."""
    zone_id = _get_zone_id()
    body = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )
    data = await _cf_request_async(
        "PATCH", f"/zones/{zone_id}/load_balancers/{lb_id}", json=body
    )
    return data.get("result", {})


@router.delete("/load-balancers/{lb_id}", status_code=status.HTTP_200_OK)
async def cloudflare_delete_load_balancer(
    lb_id: str,
    _: str = Depends(get_admin_user),
):
    """Delete a load balancer by its ID."""
    zone_id = _get_zone_id()
    data = await _cf_request_async(
        "DELETE", f"/zones/{zone_id}/load_balancers/{lb_id}"
    )
    return data.get("result", {})
