# Android App Connections

This document is the app-build integration contract for Android and TPE flows.

## 1) Build-Time Requirements

- Backend base URL (HTTPS in production), for example: https://mochii.live
- WebSocket base URL (WSS in production), for example: wss://mochii.live
- A unique device identifier persisted by the app (device_id)
- FCM registration token support for pairing and push targeting
- Authorization header support for protected device endpoints

## 2) Required Server Configuration

These must be configured server-side before mobile integration testing:

- TPE_PAIRING_TOKEN
- TPE_WEBHOOK_SECRET
- TPE_AUDIT_PATH
- TPE_UPLOAD_PATH
- GOOGLE_APPLICATION_CREDENTIALS (or settings key tpe_fcm_service_account_json)

If TPE_PAIRING_TOKEN is missing, pairing returns 503.
If TPE_WEBHOOK_SECRET is set, protected endpoints require Authorization: Bearer <secret>.

## 3) Auth and Device Identity Rules

Protected device endpoints use:

- Authorization: Bearer <tpe_webhook_secret>

Device identity resolution is endpoint-specific, but generally supports:

- body device_id
- or header X-Device-ID fallback

## 4) Endpoint Contract (Android Device -> Backend)

### 4.1 Pairing

POST /api/pair

Body:

{
	"fcm_token": "<fcm_token>",
	"pairing_token": "<pairing_token>",
	"device_id": "<optional_device_id>",
	"mqtt_client_id": "<optional_mqtt_client_id>"
}

Notes:

- pairing_token must match effective pairing token
- device_id resolves in priority:
	1. body.device_id
	2. body.mqtt_client_id
	3. X-Device-ID header
	4. fallback fcm_token

Success:

{
	"status": "paired"
}

### 4.2 Device Status Heartbeat

POST /api/handler/device-status

Headers:

- Authorization: Bearer <tpe_webhook_secret> (when configured)
- X-Device-ID: <device_id> (optional fallback)

Body supports alias fields used by app payload variants:

{
	"device_id": "<optional_if_header_present>",
	"fcm_token": "<optional>",
	"battery_pct": 84,
	"lat": 37.123,
	"lon": -122.456,
	"ai_alert": false,
	"ai_label": "safe",
	"ai_score": 0.04
}

Also accepted aliases include:

- deviceId, battery, battery_level, batteryPercent, battery_percentage
- latitude, lng, longitude
- aiAlert, ai_filter_hit, alert
- aiLabel, label
- aiScore, score

Success:

{
	"status": "received",
	"device_id": "<resolved_device_id>"
}

### 4.3 Vitals Sync

POST /api/vitals/sync

Headers:

- Authorization: Bearer <tpe_webhook_secret> (when configured)
- X-Device-ID: <device_id> (optional fallback)

Accepted body format A (internal):

{
	"device_id": "<optional_if_header_present>",
	"readings": [
		{ "heart_rate": 72, "steps": 0, "timestamp": "2026-05-25T00:00:00Z" }
	]
}

Accepted body format B (tpeapp):

{
	"vitals": [
		{
			"type": "heart_rate",
			"value": 72.0,
			"unit": "bpm",
			"start_ms": 1714555200000,
			"end_ms": 1714555260000
		}
	]
}

Success:

{
	"stored": 1,
	"baseline": 68.5,
	"alert_status": null
}

### 4.4 Audit Upload

POST /api/audit/upload

Multipart form-data:

- video: mp4 (video/mp4 or application/octet-stream), max 200 MB
- scores: JSON string, example:
	{"detection_ratio":0.8,"last_label":"...","last_score":0.9,"session_ts":1234567890}

Success:

{
	"status": "received",
	"file": "audit_<timestamp>.mp4",
	"scores": { ... }
}

### 4.5 Consequence Webhook

POST /api/tpe/webhook

Headers:

- Authorization: Bearer <tpe_webhook_secret> (when configured)

Body:

{
	"event": "punishment",
	"reason": "<text>",
	"timestamp": 1714555200000
}

Success:

{
	"status": "received"
}

### 4.6 Task Status Callback

POST /api/tpe/task/status

Headers:

- Authorization: Bearer <tpe_webhook_secret> (when configured)

JSON mode body:

{
	"task_id": "<task_id>",
	"status": "completed",
	"proof_note": "optional"
}

Multipart mode fields:

- task_id (text)
- status (COMPLETED or FAILED)
- proof_note (optional text)
- photo (optional file)

Success:

{
	"status": "received"
}

### 4.7 Generic Upload

POST /api/tpe/upload

Headers:

- Authorization: Bearer <tpe_webhook_secret> (when configured)

Modes:

- multipart with file or image field
- raw binary body (image/* or video/*)

Max size: 50 MB

Success:

{
	"status": "received",
	"file": "upload_<timestamp>_<uuid>.<ext>",
	"size_bytes": 12345
}

### 4.8 Daily Check-in

POST /api/tpe/checkin

Headers:

- Authorization: Bearer <tpe_webhook_secret> (when configured)

Body:

{
	"mood_score": 7,
	"note": "optional"
}

Rules:

- mood_score must be 1..10 when supplied

Success:

{
	"status": "received"
}

## 5) WebSocket Contract (Android <-> Backend)

### 5.1 Hot Mic Socket

WS /ws?secret=<tpe_webhook_secret>&device_id=<device_id>

Behavior:

- Device sends binary PCM chunks
- Device may receive command frames from handler relay
- If secret mismatch, server closes with code 4001

### 5.2 Signaling Socket

WS /api/tpe/signal/{session_id}

Behavior:

- Used for signaling relay in live review/session flows
- Session id is issued by handler/admin review start flow

## 6) Backend -> App Push Paths

These backend endpoints trigger outbound app commands via MQTT/FCM bridge:

- POST /api/handler/tpe/push (JWT handler/admin)
- POST /api/handler/tpe/checkins/request (JWT handler/admin)
- POST /api/admin/tpe/push (HTTP Basic admin)

## 7) Optional App-Integrated Queue Endpoints

If Android app includes queue/inbox modules, these endpoints are available:

- Public intake:
	- POST /api/booking
	- POST /api/puppy-mail
- Handler queue operations (JWT handler/admin):
	- GET /api/handler/booking
	- POST /api/handler/booking/{booking_id}/status
	- GET /api/handler/puppy-mail/threads
	- GET /api/handler/puppy-mail/threads/{thread_id}
	- POST /api/handler/puppy-mail/threads/{thread_id}/reply
	- POST /api/handler/puppy-mail/threads/{thread_id}/status

## 8) Build Integration Checklist

- Persist and reuse device_id across launches
- Send Authorization bearer token on all protected device endpoints
- Include X-Device-ID header on vitals/status calls as fallback identity
- Implement retry with backoff for 5xx and transient network failures
- Handle 401 as secret mismatch and surface reconfiguration action
- Handle 403 on /api/pair as invalid pairing token
- Handle 413 on upload endpoints (file too large)
- Handle WebSocket close code 4001 as auth failure
- Verify pairing first, then status heartbeat, then vitals sync

## 9) Out of Scope For This File

- Public website route contract
- Non-Android admin UI details
- Marketing/public content surfaces
