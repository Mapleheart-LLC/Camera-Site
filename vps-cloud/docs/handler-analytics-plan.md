# Handler Analytics Plan

## Goal
Build reliable, privacy-safe analytics for Handler Panel and public flows so we can answer:
- Which workflows are used most
- Where commands fail
- How quickly handlers respond
- Which UX changes improve outcomes

## Scope
- In scope:
  - Handler Panel V2 interactions and command lifecycle
  - Device hygiene operations
  - Public landing actions (link clicks, booking intent, anonymous mail usage)
- Out of scope (phase 1):
  - Full user-level behavioral profiling
  - Third-party ad attribution
  - Cross-device identity stitching

## Principles
- Collect only operationally useful events.
- Keep PII out of analytics tables.
- Use stable event names and versioned payloads.
- Track both attempts and outcomes.
- Make metrics queryable from SQLite first, then optional export.

## Event Taxonomy
### Core event envelope
- event_id (uuid)
- occurred_at (UTC ISO timestamp)
- source (handler_panel, backend_job, public_site)
- actor_role (admin, handler, public, system)
- actor_key (hashed, optional)
- session_id (optional)
- device_id (optional)
- event_name
- event_version (int)
- status (ok, failed, warning)
- duration_ms (optional)
- metadata_json (stringified JSON)

### Handler command events
- command.dispatch
- command.ack.executed
- command.ack.failed
- command.timeout
- command.cancelled

Required metadata:
- command_name
- command_id
- dispatch_transport (api, mqtt, ws_fallback)
- target_type (device, group)

### Device fleet operations
- fleet.cleanup.dry_run
- fleet.cleanup.executed
- fleet.cleanup.error
- fleet.delete_all.preview
- fleet.delete_all.executed
- fleet.delete_all.error
- fleet.maintenance.weekly_run

Required metadata:
- older_than_hours
- candidates
- deleted
- remaining

### Public site events
- public.link.click
- public.live_control.open
- public.booking.open
- public.question.submit
- public.mail.thread.load
- public.mail.message.send

Required metadata:
- route
- card_id or action_id
- success flag

## Data Model
## Phase 1 tables (SQLite)
1. analytics_events
- id INTEGER PRIMARY KEY AUTOINCREMENT
- event_id TEXT UNIQUE NOT NULL
- occurred_at TEXT NOT NULL
- source TEXT NOT NULL
- actor_role TEXT
- actor_key TEXT
- session_id TEXT
- device_id TEXT
- event_name TEXT NOT NULL
- event_version INTEGER NOT NULL DEFAULT 1
- status TEXT NOT NULL DEFAULT 'ok'
- duration_ms INTEGER
- metadata_json TEXT

2. analytics_daily_rollups
- day_utc TEXT NOT NULL
- metric_name TEXT NOT NULL
- metric_value REAL NOT NULL
- dimensions_json TEXT
- PRIMARY KEY (day_utc, metric_name, dimensions_json)

3. analytics_export_cursor (optional)
- sink_name TEXT PRIMARY KEY
- last_event_id INTEGER NOT NULL
- exported_at TEXT NOT NULL

## Instrumentation Plan
## Backend
1. Add lightweight helper: emit_analytics_event(db, payload).
2. Emit events in:
- command dispatch APIs
- command ACK processing
- cleanup and delete-all endpoints
- weekly maintenance scheduler run
3. Add one background rollup job (daily) to compute KPIs into analytics_daily_rollups.

## Frontend (Handler Panel)
1. Emit non-sensitive UI events to backend endpoint /api/handler/analytics/event.
2. Track command panel section usage and key CTA clicks.
3. Track modal confirmations and cancellations for destructive ops.

## Public Site
1. Add click tracking for key cards and contact flows.
2. Track anonymous mail load/send success and failures.
3. Avoid storing message body or passcodes.

## KPI Set
- Command dispatch success rate = executed ACK / dispatched
- Median command ACK latency (p50, p95)
- Device hygiene effectiveness = deleted / candidates
- Weekly stale device reduction trend
- Public conversion:
  - Ask a Question click-through
  - Booking open rate
  - Live control open rate

## Rollout Phases
1. Phase 0 (1-2 days): schema + emitter + smoke metrics
- Add analytics_events table and helper
- Emit from one command path and one cleanup endpoint
- Validate inserts and basic dashboard query

2. Phase 1 (3-4 days): core command and fleet coverage
- Instrument all handler commands + ACK events
- Instrument delete-all preview/execute and weekly maintenance
- Add 6-8 baseline queries for operations review

3. Phase 2 (2-3 days): public funnel and UX diagnostics
- Add public card click and funnel events
- Add panel section usage events
- Add cancellation reason buckets for destructive actions

4. Phase 3 (2-3 days): quality, alerts, and optional export
- Daily rollups job
- Alert thresholds (for example command failure rate > 15%)
- Optional webhook or file export pipeline

## Validation and Testing
- Unit tests:
  - event insert success
  - invalid payload rejection
  - redaction helper for sensitive fields
- Integration tests:
  - command dispatch emits expected event sequence
  - weekly maintenance emits run event
- Data quality checks:
  - no duplicate event_id
  - event_name from allowlist
  - metadata_json parseable

## Privacy and Security
- Never store raw passcodes, JWTs, message bodies, or webhook secrets.
- Hash actor identifiers using stable one-way hashing when needed.
- Keep retention policy explicit:
  - raw events: 90 days
  - daily rollups: 365 days

## Implementation Backlog
1. Add migration for analytics_events and indexes.
2. Add analytics helper module in services.
3. Instrument command dispatch and ACK paths.
4. Instrument cleanup/delete-all/maintenance endpoints.
5. Add public and panel analytics event endpoints.
6. Add SQL query pack in tools/analytics_queries.sql.
7. Add admin-only analytics summary API.
8. Add panel analytics snapshot card.

## Success Criteria
- 95% of command dispatches have a matching outcome event.
- Weekly maintenance runs are recorded automatically.
- Top 5 panel workflows and top 3 drop-off points are visible in one query set.
- No sensitive data appears in analytics payloads.
