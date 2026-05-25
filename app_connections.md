# Android App Connections

This file documents only the backend routes used by the Android/TPE app and its handler bridge.

## Device Auth Contract

- Device-originated HTTP endpoints are protected with:
	- `Authorization: Bearer <tpe_webhook_secret>`
- Device identity is resolved from:
	- request body `device_id`
	- or `X-Device-ID` header fallback (where supported)

Secret source:

- Env: `TPE_WEBHOOK_SECRET`
- DB setting fallback: `tpe_webhook_secret`

## Android Device Endpoints (routers/tpe.py)

- `POST /api/pair`
	- Register device pairing/FCM identity.
- `POST /api/audit/upload`
	- Upload audit video + model scores.
- `POST /api/tpe/webhook`
	- Inbound consequence events from app automation.
- `POST /api/tpe/task/status`
	- Task completion/failure callback from app.
- `POST /api/tpe/upload`
	- General media upload from app.
- `POST /api/tpe/checkin`
	- Daily mood/compliance check-in from app.

## Android WebSockets (routers/tpe.py)

- `WS /ws`
	- Device hot-mic socket used by app websocket service.
- `WS /api/tpe/signal/{session_id}`
	- Device signaling channel for live review/session relay.

## Device Status + Handler Bridge (routers/handler.py)

- `POST /api/handler/device-status`
	- Device posts battery/GPS/AI alert status.
	- Auth: bearer webhook secret.
	- Supports `X-Device-ID` header fallback.

- `WS /ws/device-audio/{device_id}`
	- Device audio stream relay source.

- `WS /ws/handler`
	- Handler-side websocket target that receives device updates/audio relays.

## Device Vitals Sync (routers/vitals.py)

- `POST /api/vitals/sync`
	- App uploads batched biometrics (heart rate / steps records).
	- Auth: bearer webhook secret.
	- Supports `X-Device-ID` header fallback.

- `GET /api/vitals/history`
	- Handler/admin reads processed vitals history and baseline.
	- Auth: JWT bearer (`handler` or `admin` role).

## Handler-to-App Push Paths (server to Android)

Primary command dispatch paths that send commands/events back to the app:

- `POST /api/handler/tpe/push` (JWT role: `handler` or `admin`)
- `POST /api/handler/tpe/checkins/request` (JWT role: `handler` or `admin`)
- `POST /api/admin/tpe/push` (HTTP Basic admin)

These use stored device FCM token mappings and backend push transport.

## Configuration Keys Used By Android Integration

- `TPE_PAIRING_TOKEN`
- `TPE_WEBHOOK_SECRET`
- `TPE_AUDIT_PATH`
- `TPE_UPLOAD_PATH`
- `GOOGLE_APPLICATION_CREDENTIALS` (or DB `tpe_fcm_service_account_json`)

## Notes

- This document intentionally excludes public site routes and non-Android features.
- For public web route contracts, keep a separate document.
