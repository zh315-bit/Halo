# HALO Agent — integration guide

For teammates wiring the scan page, the dashboard and M1 to the HALO Agent API. Field-level contracts are in
[AGENTS.md](../AGENTS.md) §6; this page is the short, practical version. **HALO is a prototype, not a medical
device.** Every SOS and report payload carries `"disclaimer": "Decision support only. Not a diagnosis."`

## Run the API

```bash
uv sync --extra llm
HALO_WARMUP_CSV=qr_scan_demo_clean.csv ALERT_MODE=dry_run uv run uvicorn halo_agent.api.app:app --port 8000
```

- `HALO_WARMUP_CSV` loads the synthetic demo crew `CREW_01` at startup (without it, `CREW_01` has no data until a replay).
- `ALERT_MODE=dry_run` (default) never sends an SMS. Offline demo: `HALO_OFFLINE=1` or `POST /api/v1/admin/mode {"mode": "offline"}`.
- Interactive docs: `http://127.0.0.1:8000/docs`. Health: `GET /healthz`.

## Endpoints at a glance

| What | Endpoint |
|---|---|
| Crew list for the switcher (synthetic vs live) | `GET /api/v1/crews` |
| Send a camera scan | `POST /api/v1/scan` (fallback: `POST /api/v1/scan/upload`) |
| Score a window of readings (stateless) | `POST /api/v1/score` |
| Daily report | `GET /api/v1/crew/{crew_id}/daily-report?date=YYYY-MM-DD` (`summary_generated_by`: `llm` when online and the rewritten paragraph passed every check, else `template`) |
| Morning activity (synthetic crews) | `POST /api/v1/activity/morning` |
| Crew SOS | `GET /api/v1/sos/problem-types`, `POST /api/v1/sos`, `POST /api/v1/sos/{id}/step`, `…/classify`, `…/confirm`, `…/ack`, `…/resolve`, `GET /api/v1/sos/{id}` |
| Ask about the current checklist step | `POST /api/v1/agent/ask` |
| Nudges ("check on your crewmate") | `GET /api/v1/crew/{crew_id}/nudges`, `POST /api/v1/nudges/{id}/snooze \| dismiss \| accept` |
| Demo controls | `POST /api/v1/admin/demo/{scene}` (list: `GET /api/v1/admin/demo`), `POST /api/v1/admin/mode`, `POST/GET/DELETE /api/v1/admin/replay`, `DELETE /api/v1/admin/live-scans` |

## Demo scenes

`POST /api/v1/admin/demo/{scene}` switches the demo in one call. Scenes live in `config/demo.yaml`:

| Scene | Effect |
|---|---|
| `baseline` | loads the replayed QR demo crew `CREW_01` at once, online |
| `drift` | loads the SIMULATED 7-day drift crew `SIM_CREW_DRIFT` at once (rising resting HR, falling SpO₂) |
| `drift_live` | replays the same drift at 3600× (about 3 min), so the dashboard can watch it move |
| `offline` | "pulls the network": offline mode, data unchanged |
| `reset` | forgets every synthetic crew, back online, mission environment from config |

How every scene behaves:
- It stops a running replay first.
- It resets only the crews it loads.
- It never touches live camera crews or `data/live_scans.jsonl`.

There is no SOS-event scene yet: its data is waiting for the data owner's OK.

## Camera scan ingest (scan page → HALO)

### Request: two accepted body shapes

`POST /api/v1/scan`, `Content-Type: application/json`. Both shapes are equivalent:

```jsonc
// 1. Wrapped (preferred)
{"crew_id": "LIVE_01", "record": { /* the scan record exactly as the page downloads it */ }}

// 2. Flat: the record itself, with an optional crew_id key added
{"crew_id": "LIVE_01", "session_id": "…", "created_at": "2026-11-14T22:35:12.136Z", "heart_bpm": null, …}
```

- A body that contains a `record` object is shape 1; anything else is treated as the record itself (shape 2).
- `crew_id` is optional in both. The default is `live_crew_id` in `config/scan.yaml` (`LIVE_01`).
- The record uses the field names of the `scans` table (`scan/supabase_schema.sql` in the team repo); see
  `tests/fixtures/scan_record_sample.json`. Required: `session_id` and `created_at` (ISO 8601 **with** a timezone).
  Extra fields are fine.

```js
await fetch("http://127.0.0.1:8000/api/v1/scan", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({crew_id: "LIVE_01", record: scanRecord}),
});
```

**Fallback (file upload):** `POST /api/v1/scan/upload` takes multipart form data: field `file` holds the
downloaded JSON, and an optional form field `crew_id`.
- The file may hold one record, `{crew_id, record}`, or a list of records. A list is ingested oldest first.
- The limit is 2 MB.
- The response is `{accepted: n, results: [...]}`, where each result has the same shape as a `/scan` response.

### Response

```jsonc
{"accepted": true, "duplicate": false, "crew_id": "LIVE_01", "reading_id": "<session_id>",
 "reading": { /* VitalsReading (AGENTS §6.2) incl. scan_context */ },
 "score":   { /* ScoreResult (AGENTS §6.3) incl. baseline_building */ }}
```

| Status | When |
|---|---|
| 200 `duplicate: false` | new scan, scored and persisted |
| 200 `duplicate: true` | same `session_id` already ingested for this crew; nothing is re-scored or re-written (safe to retry) |
| 409 | the scan is older than this crew's last reading, or `crew_id` is a synthetic crew (e.g. `CREW_01`) |
| 422 | not a usable record (no `session_id` / `created_at`, timestamp without timezone, bad numbers) |

### What HALO does with a scan

| Scan field | HALO |
|---|---|
| `heart_bpm` | `heart_rate_bpm` only if `heart_quality ≥ 0.35`. Below that, or with no quality → **missing** |
| `breathing_brpm` | `resp_rate_bpm` only if `breathing_quality ≥ 0.35` (the page's `QUALITY_MIN`). Otherwise → **missing** |
| `blink_rate_per_min` | kept, reported, never scored |
| SpO₂, blood pressure, temperature | **missing**: the camera does not measure them, and they are never shown as normal |
| reaction, go/no-go, memory and docking scores; `sleep_hours`, `self_alertness`, `self_mood`; `verdict`, `readiness_score`, `flags` | copied verbatim into `reading.scan_context`, **never scored** |
| `created_at` / `session_id` | `timestamp` / `reading_id` |

A vital that was dropped is listed with its reason in `scan_context.excluded`, for example
`{"heart_rate_bpm": "heart_quality 0.15 < 0.35"}`. Every scan carries `activity: resting`, `data_origin: camera_scan`
and `display_label: "Live camera measurement (experimental)"`. Show the label whenever you show the values.

### Live crew rules

- **Separate data, separate baseline.** A crew holds either live camera scans or synthetic data (demo playback or
  SIMULATED), never both. A scan is never compared with `CREW_01`'s baseline. To test, use another id
  (`LIVE_02`, `LIVE_SMOKE`, …).
- **Baseline building.** A live crew's own baseline needs **3 scans** (`team_derived`; about one scan a day).
  - Until then `score.baseline_quality` is `insufficient` and `score.baseline_building.label` reads
    "Baseline building (n/3)". Show that label instead of a green state.
  - `n` counts the least-built vital, so a scan without a usable heart rate keeps it low.
  - The absolute red flags still apply.
- **Confidence** counts only what the camera can measure (heart rate and breathing rate).
  - Example: both present and fresh, each with 3 earlier scans → `confidence` 1.0.
  - Always check `confidence` before showing a reassuring state.
- **Persistence.** Accepted scans are appended to `data/live_scans.jsonl` and fed back in at startup, so baselines
  survive a restart.
  - Replays never touch the file.
  - Clean up with `DELETE /api/v1/admin/live-scans?crew_id=LIVE_02`. Omitting `crew_id` clears every live crew;
    a synthetic id returns 409.
- **SOS.** `POST /api/v1/sos {"crew_id": "LIVE_01", …}` works like it does for the demo crew.
  - Unmeasured vitals show `missing` with the note "Not measured by camera scan".
  - The session carries the latest scan as `scan_context`, and live readings in `live_data` (AGENTS §8.6).
- **Daily report** (`GET /api/v1/crew/LIVE_01/daily-report`): `crew_source: "live"`.
  - Sleep is self-reported (`sleep.source: "self_report"`).
  - `morning_activity.tests` compares the docking and reaction scores with the crew's own earlier scans (≥ 3).
  - `scan_verdict` shows the page's own verdict, labelled "Scan verdict". It never changes HALO's `duty_status`
    (`ready | caution | rest`). The two can disagree.
- **Crew switcher.** `GET /api/v1/crews` lists every crew with `source: synthetic | live`. The configured live crew
  is always listed, with 0 readings until its first scan.

### CORS

Browsers may call the API from:
- `https://dhruvachaitanya13.github.io` (the scan page);
- any `http(s)://localhost:<port>` or `http(s)://127.0.0.1:<port>`.

Other origins get no CORS headers. The setting is in `config/scan.yaml` → `cors`; restart the server after changing it.

### Quick check

```bash
uv run python scripts/smoke_sos.py --url http://127.0.0.1:8000
```

The `scan` scenario posts the fixture as `LIVE_SMOKE`, so a real `LIVE_01` history is not touched, and then runs
an SOS on it. Afterwards clean up with:

```bash
curl -X DELETE "http://127.0.0.1:8000/api/v1/admin/live-scans?crew_id=LIVE_SMOKE"
```

## Twilio: how to connect (team bridge `twilio/halo_twilio.py` ↔ HALO)

Two processes: the team's Twilio bridge on **:8000** (`uvicorn halo_twilio:app --port 8000`) and the HALO Agent on
**:8001**. They talk over HTTP in both directions.

**1. Bridge → HALO (crew texts / calls).** In `twilio/halo_twilio.py`, replace the two stub functions
(`ask_agent` and `current_status`, the block under `# ---- stubs to replace`) with one import:

```python
from halo_agent.integrations.twilio_bridge import ask_agent, current_status
```

Same names and signatures as the stubs. The module is standard-library only, so the bridge needs no HALO
dependencies: either run the bridge with `PYTHONPATH=<path to the HALO repo>`, or copy
`halo_agent/integrations/twilio_bridge.py` next to `halo_twilio.py` and use
`from twilio_bridge import ask_agent, current_status`. Bridge env:

| Variable | Default | Meaning |
|---|---|---|
| `HALO_AGENT_URL` | `http://127.0.0.1:8001` | where the HALO Agent runs |
| `HALO_BRIDGE_CREW` | `CREW_01` | crew an SMS emergency / daily report is about |

What the crew gets (every reply ≤ 300 characters; 5 s timeout; on any error a safe fixed reply, never an exception):

| Text | HALO does |
|---|---|
| `EMERGENCY …` / `HELP …` / `SOS …` | starts an SOS from the text → `SOS 1/5: <step> Reply DONE / CANT / CHANGED` |
| `DONE` / `CANT` / `CHANGED` (also `SKIP`) | the step action for **this phone's** open SOS → next step; at the end "checklist complete" |
| any other text | `/agent/ask`, scoped to this phone's open SOS if there is one |
| daily report (`/reports/daily`) | the report's `summary_text` |
| incoming call | `current_status()`: worst crew status, `normal` / `watch` (caution) / `act` (alert); `unknown` if HALO has no data |

Open SOS sessions are remembered per phone number in the bridge process (a restart forgets them; the crew texts
EMERGENCY again). Ending a session (resolve / hand off) stays in the HALO console.

**2. HALO → bridge (alerts to mission control).** Start HALO with:

```bash
ALERT_MODE=bridge TWILIO_BRIDGE_URL=http://127.0.0.1:8000 uv run uvicorn halo_agent.api.app:app --port 8001
```

Every urgent SOS escalation is `POST $TWILIO_BRIDGE_URL/alerts/send` with `{"level": "act", "message": "<one
sentence>"}` (the bridge's `Alert` model; `act` = SMS + phone call to `MISSION_CONTROL_NUMBERS`). Report-level
escalations (e.g. fainting f7) are never sent. If the bridge is down or sends nothing, the alert is queued
(`queued_offline`, UI "Alert bridge unreachable: alert queued and retried automatically") and retried in the
background at most every 30 s; the checklist never waits. Recipients are configured on the bridge only.

Checked end to end (2026-11-14): a copy of `halo_twilio.py` with the import above, `TWILIO_VALIDATE=0`, form posts
to `/twilio/sms` and `/twilio/voice`, and HALO alerts posted to `/alerts/send` (503 without Twilio credentials →
queued as designed).

