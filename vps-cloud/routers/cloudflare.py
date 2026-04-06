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


# Zone ID cached after first auto-discovery so we only call the API once.
_cached_zone_id: Optional[str] = None

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


def _cf_request(method: str, path: str, **kwargs) -> dict:
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
    """Ensure all required platform subdomains exist as proxied CNAME DNS records.

    Checks each well-known subdomain prefix (anon, links, shop, drool, creator,
    member, www) and creates a proxied CNAME record pointing at
    ``CLAPI_TUNNEL_HOSTNAME`` for any that are missing.  Existing records are
    left untouched.

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


def deprovision_creator_subdomain(handle: str, root_domain: str) -> bool:
    """Remove Cloudflare resources tied to a deleted creator.

    Deletes:
    - Any CNAME records whose name is ``{handle}.{root_domain}``.
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
