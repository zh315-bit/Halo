# AGENTS.md — HALO Agent

Instructions for any coding agent working in this repo. Read this file fully before writing code.
Design source: *HALO AI Agent 架构设计* (Nov 14, 2026). Where this file and that doc differ, this file wins
for implementation details. Any item marked ⚑ is still open with the team (§13).

## 1. What we are building

HALO is our team's entry in the **2026 NASA Space Apps Challenge** (Nov 14–15), challenge *"Create Health
Monitoring Software for Astronauts on Space Missions"*.

The **HALO Agent** is an onboard, autonomous health assistant with two jobs:
1. **Crew SOS:** when a crew member is in trouble, it walks a crewmate through a preset response checklist, one step at a time.
2. **Daily report:** every morning it produces a health report for each crew member.

All judgements are made against **that crew member's own baseline**, and the agent keeps working with Earth
out of contact (about 20 min communication delay).

**This repo's owner (Chris) builds modules M2–M6.** M1 (data ingest, Dhruv / Kavya) and M7 (notifications)
are integration points only: code against their interfaces, don't build them.

Judging criteria that drive design:
- **Validity:** every threshold and every checklist step must be traceable to a source.
- **Impact / Creativity:** personal baseline, trend detection and offline operation.

## 2. Core architecture: deterministic core + LLM shell

| Layer | Contents | Network | LLM |
|---|---|---|---|
| **Core** (must run offline) | personal baseline, scoring engine, red flags, SOS checklist state machine, report templates | not needed | **never** |
| **Shell** (enhancement when online) | (a) map a crewmate's spoken/typed description to symptom candidates, (b) answer follow-up questions using only the current checklist + vitals, (c) rewrite the daily report as plain prose | needed | yes |

- When the shell is unavailable (offline flag set, no key, timeout, or error), every feature degrades:
  symptom classification → symptom picker; ask → "Not covered offline — follow the checklist";
  report prose → template text.
- Degradation must be **automatic and silent to the core**: the core never imports the shell.
- Offline mode switch: env `HALO_OFFLINE=1` **and** a runtime toggle `POST /api/v1/admin/mode {"mode":"offline"}`.
  The demo will "pull the network" live.

## 3. Non-negotiable rules

1. **Never invent medical content.** No thresholds, checklist steps, drug names or doses that the team did not
   supply with a source. If something has no source: keep it `enabled: false` / `verified: false`, use the
   placeholder text `"[PLACEHOLDER — not medical guidance]"`, and flag it in your summary.
2. **No diagnosis.** Output says how far a reading deviates from baseline and what the checklist says to do.
   It never names a disease. Every SOS and report payload carries `"disclaimer": "Decision support only. Not a diagnosis."`
3. **The LLM only uses provided text.** Answers must cite a checklist step id or a knowledge-base chunk id.
   If there is nothing to cite, reply "The checklist doesn't cover this — contact ground." Medication text
   is shown verbatim from the checklist, never generated.
4. **Humans decide.** LLM symptom classification returns the top 3 candidates; the crewmate confirms one.
   Never auto-select. Escalation recipients are fixed in config.
5. **Missing is missing.** Never treat a missing value as normal or 0. Every result carries `confidence` and `data_origin`.
6. **Label data honestly.** Demo readings always keep `"Demo data - not measured from your camera"`.
   Generated data is marked `SIMULATED` in the file name, header and `data_origin`. NASA environmental data
   and Inspiration4 lab data are stored **separately** from QR session readings and are never merged into one dataset.
7. **No look-ahead.** A reading is scored only with data at or before its own timestamp.
8. **Contracts first.** Don't change the schemas in §6 without updating this file and telling the user.
9. Core uses the standard library only (`csv`, `statistics`, `math`, `datetime`). No pandas in `halo_agent/core/`.

## 4. Modules

| Module | Owner | Input | Output |
|---|---|---|---|
| M1 Data ingest | Dhruv (backend), Kavya (schema) | face-scan vitals or QR demo CSV playback, morning activity time | `VitalsReading` with source label |
| **M2 Personal baseline** | Chris | crew's history of `VitalsReading`, grouped by activity | per-metric median, spread, deviation, multi-day trend |
| **M3 Scoring engine** | Chris | latest reading window + baseline | `ScoreResult` |
| **M4 Crew SOS** | Chris | symptom (picker or speech), latest vitals, score | current checklist step, related vitals, escalation decision |
| **M5 Daily report** | Chris | previous night's in-bed period, morning activity time, trends, environment reference | `DailyReport` |
| **M6 Protocol & knowledge library** | Chris (content source ⚑) | preset checklists, NASA public references | checklist JSON, citable chunks |
| M7 Notify & escalate | TBD (Twilio ⚑) | escalation events from M4 | SMS/call attempt, ack state, offline queue |

## 5. Data facts (verified from real headers)

### `qr_scan_demo_clean.csv` (Kavya)
- 276 rows × 23 columns, one crew member `CREW_01`, every row `scenario=baseline`, 5-minute interval,
  mission days 1–2 (`clock_time` 10:10 → next day 09:05). No missing values.
- ID column is `demo_record_id` → map it to `reading_id`.
- **Timestamp ⚑:** there is no calendar date. For now:
  `timestamp = mission_start (config, default 2026-11-15T00:00:00Z) + (mission_day − 1) days + clock_time`.
- `activity`: resting 166, in_bed 76, active 34.
- **Carry-forward:** BP, temp, resp and SpO2 mostly repeat the previous value. SBP changes only 26 times,
  temp 5 times, roughly in sync with `bp_new_reading` / `temp_new_reading`.
- `measurement_origin = master_csv_playback_not_camera` → `data_origin = "demo_playback"`.
- ⚠️ BP is high for a "baseline": the first row is 162/58, the last SBP is 189, and the resting SBP median is 162 ⚑.
  The `bp_crisis` red flag (§7.4) will fire on the last rows. That is correct behaviour; don't suppress it.
- No `blink_rate_per_min` column ⚑. Keep the field nullable.

### `radiation_5min.csv`
- Historical ISS Columbus readings, 2024-05-01 → 2024-05-30, 8,627 rows. Absorbed dose in µGy.
  This is **environment**, not personal dose.
- It is used only as an `environment_context` reference ("Historical ISS radiation - May 2024").
  It never changes a crew member's `status` or `severity`.
- 2024-05-11 has 4 SAA periods above 25 µGy/h for ≥15 min, which makes a good reference example.
- No radiation prediction model exists → show `"Model not trained"`, never a made-up number.

### `simulated_bone_density.csv`
- 42 rows, crew `SIM-A …` / `SIM-B …`, with a `halo_status` column. Not in the agent design doc → **lowest
  priority** (Phase 4). If used: config maps `CREW_01 → SIM-A`, and our status must agree with `halo_status` on all 42 rows.

### Not available
Emergency scenarios, sleep stages, mood and cabin CO₂ are not in the data. Sleep is **estimated from
`activity = in_bed` periods**. The morning activity is undefined ⚑.

## 6. Contracts

Status vocabulary: `normal | caution | alert`, plus `insufficient` for an indicator without baseline.
Severity is `0–3`. External level names `watch` / `act` (threshold JSONs) map to `caution` / `alert`.

### 6.1 Endpoints

| Endpoint | Purpose | Owner |
|---|---|---|
| POST /api/v1/vitals | ingest one reading | Dhruv |
| GET /api/v1/crew/{crew_id}/vitals/latest | latest reading + baseline comparison + score | Dhruv, calls Chris |
| WS /ws/crew/{crew_id} | push readings, score changes, alerts | Dhruv |
| **POST /api/v1/score** | reading window → `ScoreResult` | **Chris** |
| **POST /api/v1/sos** | start an SOS session, return step 1 | **Chris** |
| **POST /api/v1/sos/{session_id}/step** | submit `done` / `cannot_do` / `condition_changed`, return the next step | **Chris** |
| **POST /api/v1/sos/{session_id}/classify** | free text → top-3 symptom candidates (shell) | **Chris** |
| **POST /api/v1/sos/{session_id}/confirm** | crewmate confirms one problem type `{"symptom": ...}` after `/classify` (or the picker) → `SosSession`; a different type switches the checklist | **Chris** |
| **POST /api/v1/agent/ask** | free question; with `session_id` it is limited to that checklist | **Chris** |
| **POST /api/v1/activity/morning** | morning activity result for the daily report. Body `{crew_id, date?, completed_at?, completion_seconds?}` → **202** `{"accepted": true, "report_date": "YYYY-MM-DD"}`; stored in memory and used by the report (the activity itself is still ⚑ Q6) | **Chris** |
| **GET /api/v1/crew/{crew_id}/daily-report?date=** | daily report | **Chris** |
| **POST /api/v1/admin/mode** | toggle online/offline (demo) | **Chris** |
| **POST / GET / DELETE /api/v1/admin/replay** | demo feed until M1: POST `{file, speed=60, reset=true}` → **202** `ReplayStatus` (feeds a `data/*.csv` into the live Engine at `speed`× real time via `Engine.update`; 409 if one is running; only files inside `data/`); GET = progress; DELETE = stop. Startup warm-up: env `HALO_WARMUP_CSV=<file in data/>` | **Chris** |

`/classify` is an addition to the design doc: it keeps classification separate from session start.
All of Chris's endpoints must first exist as **stubs returning fixed JSON** (Phase 0).

### 6.2 `VitalsReading`
```json
{
  "reading_id": "QR_DEMO_0001",
  "crew_id": "CREW_01",
  "timestamp": "2026-11-15T10:10:00Z",
  "mission_day": 1,
  "activity": "resting",
  "heart_rate_bpm": 93.0, "resp_rate_bpm": 18.0,
  "systolic_bp_mmhg": 162.0, "diastolic_bp_mmhg": 58.0,
  "body_temp_c": 37.2, "spo2_pct": 97.0,
  "blink_rate_per_min": null,
  "new_reading": {"bp": true, "temp": false},
  "measurement_origin": "master_csv_playback_not_camera",
  "display_label": "Demo data - not measured from your camera"
}
```
`new_reading` is derived from `bp_new_reading` / `temp_new_reading` and is used by M2.

### 6.3 `ScoreResult`
The team draft's fields are kept unchanged. `reading_id`, `indicators` and `reasons[].status/source/rule_id/text` are additions.
```json
{
  "crew_id": "CREW_01",
  "reading_id": "QR_DEMO_0001",
  "as_of": "2026-11-15T10:10:00Z",
  "status": "caution",
  "severity": 2,
  "confidence": 0.7,
  "baseline_quality": "ok",
  "engine": "rules-v0",
  "data_origin": "demo_playback",
  "indicators": [
    {"metric": "heart_rate_bpm", "value": 93.0, "status": "caution", "deviation_mad": 2.1,
     "baseline": {"median": 84.0, "sigma": 4.3, "n": 120, "group": "resting"},
     "trend": {"direction": "rising", "slope_per_day": 3.1, "days_to_alert": 4.2},
     "fresh_minutes": 0}
  ],
  "reasons": [
    {"metric": "heart_rate_bpm", "value": 93.0, "baseline_median": 84.0, "deviation_mad": 2.1,
     "direction": "high", "status": "caution", "source": "baseline", "rule_id": "z_band",
     "text": "Resting heart rate 93 bpm is 2.1 robust SD above your baseline (84 bpm)."}
  ]
}
```
- `deviation_mad` = robust z = `(value − median) / sigma`, with `sigma = 1.4826 × max(MAD, floor)`.
- `baseline_quality` ∈ `ok | partial | insufficient`.
- `reasons[].source` ∈ `nasa | guideline | team_derived | baseline`.
- `trend` may be `null`. `engine` names the implementation; swapping in an ML model only changes this field.
- **Request** (`POST /api/v1/score`): `{"readings": [VitalsReading, …], "baseline_until": "ISO8601" | null}`, oldest → newest;
  the last reading is the one scored. Readings at or before `baseline_until` are the baseline phase (§7.1);
  `null` means the whole window is baseline phase (the QR demo). Request models ignore unknown fields
  (M1 may post a whole CSV row); response models reject them (`extra="forbid"`).
- `indicators` has one entry per vital (`heart_rate_bpm, resp_rate_bpm, systolic_bp_mmhg, diastolic_bp_mmhg,
  body_temp_c, spo2_pct`) plus `blink_rate_per_min`, which stays `insufficient` / `null` until a source exists
  and is not counted in `baseline_quality` or `confidence`.
- `indicators[].status` and `reasons[].status` ∈ `normal | caution | alert | insufficient`.
  `baseline.group` is the activity name, or `pooled` for the cross-activity fallback (`baseline_quality: partial`).
- A carried-forward value is not re-scored: the indicator keeps the result of that metric's last new
  measurement and only `fresh_minutes` grows.
- `reasons` lists only non-normal findings and missing values. `rule_id` ∈ `z_band | trend | missing` or a
  red-flag id from `red_flags.yaml` (`spo2_low | fever | bp_crisis`, `source: guideline`).
- Top-level `baseline_quality` is the worst quality among vitals that have a value. If no vital can be
  assessed, `status` is `normal` but `confidence` is `0` and `baseline_quality` is `insufficient`:
  consumers must check `confidence` before showing a green state.
- Python entry point for other modules: `from halo_agent.core import score_latest` →
  `score_latest(crew_id) -> ScoreResult | None` (None until that crew has been scored).

### 6.4 `SosSession`
The Phase 0 fields are unchanged; everything below `disclaimer` is a sos-v0 addition. `score` is now nullable.
```json
{
  "session_id": "sos_1a2b3c4d",
  "crew_id": "CREW_01",
  "symptom": "chest_pain",
  "symptom_source": "picker",
  "protocol_id": "chest_pain_v2",
  "step": {"index": 1, "total": 11, "id": "c0", "type": "escalate",
           "text": "Alert the CMO and the flight surgeon now. Don't wait to see if the pain goes away.",
           "source_ref": "JSC-48031 p.1-36 (contact Surgeon)", "ref": "JSC-48031 p.1-36 (contact Surgeon)",
           "sources": [{"key": "nasa_medcl", "title": "NASA Medical Checklist, JSC-48031 Rev H (Space Shuttle, 2001)",
                        "url": "https://…", "kind": "spaceflight_procedure", "medical": true}],
           "note": null, "env": null, "adaptation": null, "gravity_dependent": null,
           "on_condition_changed_means": null,
           "options": ["done", "cannot_do", "condition_changed", "skip"]},
  "vitals_snapshot": {"reading_id": "QR_DEMO_0276", "age_seconds": 0, "timestamp": "2026-11-16T09:05:00Z",
                      "display_label": "Demo data - not measured from your camera", "data_origin": "demo_playback",
                      "stale": false,
                      "metrics": {"body_temp_c": {"value": 37.3, "measured_at": "2026-11-16T08:00:00Z",
                                                  "age_seconds": 3900, "status": "stale"}}},
  "score": null,
  "escalation": {"triggered": true, "reasons": ["sos_reported", "escalate_step:c0"], "channel": "sms",
                 "state": "queued_offline", "acknowledged": false},
  "mode": "offline",
  "disclaimer": "Decision support only. Not a diagnosis.",
  "state": "GUIDING",
  "review_status": "sourced_unreviewed",
  "review_label": "Sourced, not medically reviewed",
  "mission_env": "partial_gravity",
  "show_vitals": ["heart_rate_bpm", "systolic_bp_mmhg", "diastolic_bp_mmhg", "spo2_pct", "resp_rate_bpm"],
  "scope_note": null, "scope_sources": [],
  "checklist_title": "Chest pain or pressure",
  "checklist_complete": false,
  "started_at": "2026-11-14T17:30:56Z", "ended_at": null, "note": null,
  "steps_done": [],
  "score_reasons": [],
  "alert": {"mode": "offline_queue", "sms_status": "queued_offline", "call_status": "not_implemented",
            "acknowledged_at": null, "ack_by": null,
            "ui_message": "Ground will receive this in ~20 min. Continue the checklist.",
            "messages": [{"message_id": "sos_1a2b3c4d-m1", "reason": "sos_reported", "channel": "sms",
                          "state": "queued_offline", "sender": "queue", "sid": null, "attempts": 0,
                          "error": null, "created_at": "…", "body": "HALO SOS …"}]},
  "events": [{"seq": 1, "at": "…", "action": "report", "actor": "crew", "state_from": "REPORTED", "state_to": "REPORTED"}]
}
```
- `symptom_source` ∈ `picker | llm`. If it is `llm`, the UI must get confirmation before the session advances.
- `symptom` ∈ `chest_pain | breathing_difficulty | fainting | bleeding_injury | other` (sos-v2).
- `step.type` ∈ `action | check | escalate`. For `check`, `on_condition_changed_means = "warning_sign"`: the UI
  labels that button as "warning sign present". `step.index` / `total` count only the steps that apply in
  `mission_env`. `step.sources[]` (title, url, kind, `medical`), `step.ref` and the checklist's
  `review_label` must be shown; `team_policy` sources have `medical: false`. `step.note` and `scope_note` go in a
  details panel and are **never shown as instructions**. Text such as `[N — TBD]` is shown verbatim.
- `review_status` ∈ `demo_unreviewed | sourced_unreviewed | reviewed`; `review_label` is the banner text.
- `mission_env` ∈ `microgravity | partial_gravity | post_landing`, fixed when the session starts.
- `step.options` ∈ `done | cannot_do | condition_changed | skip`. When the checklist is finished,
  `step.id = "END"`, `options = []`, `checklist_complete = true`; the crew then calls `/resolve`.
- `escalation.state` ∈ `not_needed | sending | sent | acknowledged | queued_offline | failed | simulated`.
  `simulated` (sos-v0 addition) = dry-run mode: nothing was sent, and it is never shown as `sent`.
  `alert.sms_status` ∈ `not_sent | sending | dry_run | sent | queued_offline | failed` (`dry_run` ↔ `simulated`).
- `mode` ∈ `online | offline`. `alert.mode` ∈ `dry_run | live | offline_queue`.
- `state` ∈ `REPORTED | ALERTING | GUIDING | RESOLVED | HANDED_OFF`. Acknowledgement is an overlay
  (`alert.acknowledged_at`), not a state.
- `vitals_snapshot` comes from `halo_agent.core.latest_reading` and `score` from `score_latest` — the same live
  Engine. `score.reading_id` always equals `vitals_snapshot.reading_id`; a score for any other reading is not
  shown. `score` is `null` when unavailable, `None`, or not for the displayed reading; the UI shows "Score
  unavailable" and the flow continues. No reading → every vital `missing`.
- `alert.messages[]` carry `reasons[]` (the opening message combines `sos_reported`, any context reasons and an
  escalate first step). `level=report` escalations are never messages: they appear in `alert.reports[]` and
  `alert.report_ui_message = "Reported to flight surgeon"`; `escalation.state` / `sms_status` follow urgent
  messages only. `GET /api/v1/sos/ground-reports` lists the ground report queue.
- `step.adaptation` / `step.gravity_dependent` are not used by v2 (superseded by `env`); the UI hides them when null.
- `vitals_snapshot.metrics.*.status` ∈ `ok | stale | missing`. `measured_at` is the metric's last **new**
  measurement (carry-forward excluded). Stale = older than 10 min (`team_derived`).
- `events[]` are append-only: `seq, at, action, actor, state_from, state_to` plus action-specific keys.
- SOS endpoints (sos-v0): `GET /api/v1/sos/problem-types`, `POST /api/v1/sos` (`crew_id`, `symptom` | `free_text`,
  optional `note`), `GET /api/v1/sos/{id}` (polling), `POST /api/v1/sos/{id}/step` (`action`, optional `step_id`:
  mismatch → 409), `POST /api/v1/sos/{id}/ack` (`by`), `POST /api/v1/sos/{id}/resolve` (`outcome`:
  `resolved | handed_off`), `POST /api/v1/sos/{id}/classify` (`free_text` → §10), `POST /api/v1/sos/{id}/confirm`
  (`symptom`: the crewmate's choice; a different type switches the checklist).
  Illegal transition → 409, unknown session → 404, unknown symptom → 422.
- `POST /api/v1/admin/mode` (sos-v2 additions): body `{mode?, mission_env?}` (at least one); response adds
  `mission_env` and `mission_env_source` (`config | runtime_toggle`). The env switch applies to sessions started
  after it. Default `mission_env` from `config/app.yaml` (`partial_gravity`), env override `HALO_MISSION_ENV`.

### 6.5 `DailyReport`
```json
{
  "crew_id": "CREW_01",
  "date": "2026-11-15",
  "duty_status": "go",
  "sleep": {"in_bed_minutes": null, "vs_baseline": null, "in_bed_hr_median": null},
  "morning_activity": {"completion_seconds": null, "vs_baseline_pct": null},
  "vitals_trend": [{"metric": "heart_rate_bpm", "days": 3, "direction": "stable"}],
  "environment_context": {"label": "Historical ISS radiation - May 2024", "is_personal_dose": false},
  "watch_items": [],
  "recommendations": [],
  "recommendations_source": "team_policy",
  "summary_text": "...",
  "summary_generated_by": "template",
  "data_sources": ["demo_playback"],
  "disclaimer": "Decision support only. Not a diagnosis."
}
```
`duty_status` ∈ `go | caution | rest`. `summary_generated_by` ∈ `template | llm`.
- **M5 template version (2026-11-14).** Built from the live Engine's (reading, score) history, so the report,
  SOS and `/score` share one source. `GET …/daily-report` returns **404** until the crew has readings (use
  `HALO_WARMUP_CSV` or `/admin/replay`); `date` defaults to the day of the crew's latest reading.
- Night window for date D: `[D−1 18:00, D 12:00)`, cut at the morning activity's `completed_at` if one was
  posted (`config/report.yaml`). `sleep.in_bed_minutes` = in-bed readings × sampling step, **null** unless
  readings cover ≥ 80 % of the window. `sleep.vs_baseline` = minutes vs the median of ≥ 3 earlier nights.
  `morning_activity.vs_baseline_pct` = % vs the median of ≥ 3 earlier mornings (⚑ Q6), else null.
- `sleep.in_bed_hr_median` = median HR over the night's in-bed readings (null with the minutes).
- `duty_status` (decided 2026-11-14): the **resting** readings of the morning window `[cutoff − 4 h, cutoff]`
  (`morning_window_hours`, `team_derived`), worst status **after debounce**: a metric's z-band level counts
  only if its last 2 consecutive new measurements reach it (`duty_consecutive`); unfavourable trends count at
  once; **red flags count at once from any activity** (an absolute line just crossed is never hidden because
  the reading was taken while active). Then normal → go, caution → caution, alert → rest. No resting reading
  in the window → `caution`, never `go`. `watch_items` lists only the findings that counted.
  `vitals_trend` comes from the latest resting reading (`insufficient` when the trend is null).
- `recommendations` uses only three fixed `team_policy` sentences (`recommendations_source`, no medical
  content, `config/report.yaml`): an unfavourable trend → "Discuss the [metric] trend with the flight surgeon at
  the next contact."; no resting reading or a missing vital → "Take a fresh vitals reading."; `rest` →
  "Follow the flight surgeon’s guidance before resuming duties."
- `environment_context` adds optional `reference_date, dose_day_ugy, level, threshold_source, source,
  prediction` (mission day N → 2024-05-01 + N−1; `level` from `halo_alert_rules`, watch/act → caution/alert;
  `prediction: "Model not trained"`). It never affects `duty_status` or `watch_items`.

## 7. M2 + M3 — baseline and scoring (core)

### 7.1 Baseline (personal, frozen)
- Group by `crew × metric × activity`. Compute the median and MAD from `scenario=baseline` rows only, then
  **freeze** the result (it acts as a pre-flight baseline). Later readings never update it, so slow drift
  cannot be absorbed into the baseline.
- Use only new measurements:
  - SBP, DBP, resp, SpO2: rows where `new_reading.bp OR new_reading.temp` is true.
  - Temperature: rows where `new_reading.temp` is true.
  - HR: every row.
  - Carried-forward values are not re-scored; they only lower freshness.
- If an activity group has fewer than `min_points` points, fall back to the metric's pooled baseline across
  activities (`baseline_quality: partial`). If that still has fewer than `min_points`, the indicator is
  `insufficient` and no z band is applied (red flags still run). `min_points = 12` (`team_derived`, decided
  2026-11-14): 3 points is not a baseline, and a very narrow σ must not produce cautions.
- **Baseline phase vs. no look-ahead (decided 2026-11-14).** The QR demo is baseline phase from start to end,
  so freezing "all baseline rows" would score each row against its own future. Instead, during the baseline
  phase the baseline accumulates in reading order from readings strictly *before* the one being scored; at
  the crew's first non-baseline reading it is frozen and never changes again. `ScoreRequest.baseline_until`
  (or the CSV `scenario` column) marks the phase.
- **A changed value is always a new measurement (decided 2026-11-14).** The new-reading flags are not
  reliable on their own: SBP and resp change a few times in the demo with both flags false. A value is new
  if its flag says so, **or** it differs from the previous row's value, **or** it is the first value seen.
  This keeps `reasons[].value` equal to the value the indicator shows.
- MAD floors (`team_derived`, measurement resolution): temp 0.1 °C (switch to 0.2 if too sensitive),
  SpO2 1 %, resp 1 /min, HR 1 bpm, BP 2 mmHg.

### 7.2 Robust-z bands
- `|deviation_mad| ≥ 2` → caution, `≥ 3` → alert (both in `scoring.yaml`).
- Only the unfavourable direction counts: HR, resp and temp high; SpO2 low; BP both directions.
- **Debounce:** an indicator reaches `alert` only after 2 consecutive new readings beyond the alert band.
  One reading caps at `caution`.

### 7.3 Trend
- Window size and unit are configurable (`days | hours | readings`). Defaults: 3 days for reports, readings for tests.
- Aggregate per step (median) within the current activity group, then fit a least-squares slope.
- `|slope| ≥ 0.5 σ / day` → `rising` / `falling`, otherwise `stable`. An unfavourable direction adds a
  `caution` reason (`source: baseline`).
- `days_to_alert = (alert boundary − current value) / slope`. Include it only when it is > 0 and ≤ 14.
- A days/hours step with fewer than `min_points_per_step` (6, `team_derived`) new readings is left out of
  the fit, so one reading just after midnight cannot drive the slope. Trends are computed from new
  measurements only, at or before the scored reading. The alert boundary is `median ± alert_z × σ` on the
  side the trend is heading.

### 7.4 Absolute red flags (`red_flags.yaml`)
Red flags override the baseline and set the indicator to `alert`. Values are **provisional**: each rule has
`verified: false` until the user confirms its source. Don't add more on your own.

| rule_id | condition | source (to verify) |
|---|---|---|
| spo2_low | `spo2_pct < 90` | guideline |
| fever | `body_temp_c >= 38.0` | guideline |
| bp_crisis | `systolic_bp_mmhg >= 180` | guideline (AHA hypertensive crisis) |

### 7.5 Aggregation and confidence
- `status` is the worst indicator status. Severity: normal 0, caution 1 (one indicator) or 2 (two or more,
  or any unfavourable trend), alert 3.
- `confidence = baseline_term × freshness_term × completeness_term`:
  - baseline_term: mean of `min(1, n/12)`
  - freshness_term: 1 if the last new reading is ≤ 30 min old, falling linearly to 0 at 6 h
  - completeness_term: `1 − missing_fraction`
- Provide both `score(readings) -> list[ScoreResult]` (batch) and a stateful `Engine.update(reading) -> ScoreResult`
  (for the WS feed). **They must give identical results** on the same data.

## 8. M4 — Crew SOS (core state machine + optional shell)

Kavya's four steps: **report → display → guide → alert**, plus a session log. Code: `halo_agent/sos/`
(`protocols.py`, `state_machine.py`, `escalation.py`, `vitals.py`, `session_log.py`, `service.py`). No LLM.

### 8.1 Checklists (M6 content, `config/checklists/*.yaml`)
Converted verbatim from the team's `sos_checklists_v2.yaml` (DRAFT, sourced, **not medically reviewed**).
One YAML per problem type (`chest_pain`, `breathing_difficulty`, `fainting`, `bleeding_injury`, `other`) plus the
shared `config/checklists/_sources.yaml` table. Parsed outside core (`sos/protocols.py`) and validated **at load
time**; an invalid file is refused.
```yaml
protocol_id: chest_pain_v2
symptom: chest_pain
title: "Chest pain or pressure"
review_status: sourced_unreviewed     # declared; the effective value can only be stricter
verified: false
show_vitals: [heart_rate_bpm, systolic_bp_mmhg, ...]
red_flags: [bp_crisis, spo2_low]
start: c0
# optional: scope_note, scope_source, escalate_if_unresolved_after
steps:
  - id: c1
    type: action                      # action | check | escalate
    env: [microgravity]               # optional; absent = every environment
    text: "Anchor yourself and your crewmate ..."
    source: nasa_medcl                # key or list of keys from _sources.yaml
    ref: "JSC-48031 p.1-6 (Patient Restraint)"
    # optional: note (reviewer note), adaptation, gravity_dependent
    next: c1g                         # step id | END (default: following step)
    on_cannot_do: escalate            # step id | escalate | END (default escalate, AGENTS §8.3)
    # on_condition_changed: step id | escalate | END (required for check; absent on action = stay)
```
- `type: escalate`: escalation fires **every time the step is entered**; the checklist continues at `next`.
- `type: check`: `condition_changed` = YES, a warning sign is present → `on_condition_changed`.
- A target of `escalate` (v2 `ESCALATE`) on `on_cannot_do` / `on_condition_changed` escalates and continues at
  the step's `next`.
- **Environment:** a step whose `env` does not include the session's `mission_env` is skipped by following its
  `next` until an applicable step (or END); the same rule applies to every target (`next`, `on_cannot_do`,
  `on_condition_changed`) and to the checklist `start`.
- `review_status` (effective) = stricter of the declared value and the content: `demo_unreviewed` if any step has
  no source; `sourced_unreviewed` if all cite `_sources.yaml`; `reviewed` only if declared reviewed and
  `verified: true`. The UI always shows `review_label` (e.g. "Sourced, not medically reviewed").
- `team_policy` sources are citations but not medical sources (`medical: false`). Medication policy (team_policy):
  HALO never names a drug or dose.
- Validation: unique ids, known keys only, valid `type` / `env` / `show_vitals`, every source key exists, no
  dangling target, `next` cannot be `escalate`, every check has `on_condition_changed`, no loop through
  non-applicable steps in any environment, every non-placeholder step cites a source, all five problem types present.
- ⚑ JSC-48031 page numbers come from a public OCR copy; verify against the PDF before citing on stage.
- `adaptation` / `gravity_dependent` are not filled: v2 expresses this with `env`. The fields stay optional.
- Optional `level: urgent | report` on escalate steps (default urgent). `fainting_v2.f7` is `report`.

### 8.2 Session flow and state machine
`REPORTED → ALERTING → GUIDING → RESOLVED | HANDED_OFF`. The legal moves are one table
(`state_machine.TRANSITIONS`); anything else → 409.
1. **Report:** `POST /sos` with `symptom` (picker), or with `free_text`: the generic `other` checklist starts
   immediately (nobody waits for the LLM) and the text is stored as `note`. Then `/classify` → top-3 candidates →
   crewmate picks one → `/confirm`. A different type switches the checklist to its first step and logs
   `checklist_switched` (`from_protocol`, `to_protocol`, `from_step`); `steps_done` keeps the history. The same type
   logs `symptom_confirmed`. `symptom_source` = `llm` if the choice was one of the LLM candidates, else `picker`.
2. **Alert + guide together:** the same request resolves step 1 and sends **one** opening message whose
   `reasons` contain `sos_reported`, any context reasons and `escalate_step:<first step>`. Network sends run as
   background tasks, so guidance never waits for delivery.
3. **Display:** `from halo_agent.core import latest_reading` (the Engine's last processed reading, with per-vital
   `measured_at` / `metric_age_seconds`); the SOS has no clock or CSV replay of its own. Per vital: value, last
   new measurement, age, `ok | stale | missing` (unknown age is never `ok`), plus `display_label`. Score via
   `score_latest`, kept only if its `reading_id` matches (one retry on a race, else `score: null`).
4. **Guide:** one step at a time, in the session's `mission_env`. `done` / `skip` → `next`; `cannot_do` →
   `on_cannot_do`; `condition_changed` → re-read vitals + score, go to `on_condition_changed` or stay. Every
   target is env-resolved (§8.1); the step event records `skipped_to` when steps were skipped.
5. **Alert:** escalation conditions are evaluated at start and after every step.
6. **Ack:** `POST /sos/{id}/ack` (manual, demo) sets the overlay; idempotent; allowed after the session ended.
7. **Log:** every event is appended to `data/sos_sessions/<id>.jsonl` (env `HALO_SOS_LOG_DIR`); on
   RESOLVED / HANDED_OFF the full session is exported to `<id>.json`.

### 8.3 Escalation conditions (`config/escalation.yaml`, validated at load; each can be toggled)
- `sos_reported`: the SOS itself (once)
- `severity_max`: score severity 3 (once)
- `red_flag:<rule_id>`: a red flag named in the checklist fires with status `alert` (once per rule)
- `cannot_do`: crewmate picks `cannot_do` on a step with `on_cannot_do: escalate` (every time)
- `escalate_step:<step_id>`: a `type: escalate` step is entered (every time, including the start step)
- `warning_sign:<step_id>`: `condition_changed` on a step whose `on_condition_changed` is `escalate` (every time)
- `unresolved_after_step`: the `escalate_if_unresolved_after` step is left and the session is not resolved (once)
- `stale_vitals`: freshness_term < 0.2 or no reading (once)
- `low_classification_confidence`: best `/classify` candidate < `min_classification_confidence` (0.5, `team_derived`) (once)

Policy (also in `escalation.yaml`):
- **Same-reason cooldown:** a reason already sent is not re-sent within `same_reason_cooldown_s`; the repeat is
  logged as `escalate_suppressed`. Value **60 s** (`team_derived`, 2026-11-14: no repeated demo SMS); `null` = off.
- **Level** `urgent | report`: urgent = SMS (or queue/dry run). report = session log + ground report queue
  (`data/sos_sessions/ground_reports.jsonl`, `GET /api/v1/sos/ground-reports`), no SMS, UI "Reported to flight
  surgeon". Resolution: `step_levels["<protocol_id>.<step_id>"]` > checklist step `level` > `rule_levels[rule]` >
  urgent.

### 8.4 M7 interface
`AlertSender.send(AlertMessage) -> SendResult` (`halo_agent/notify/`). Recipients come only from env
(`MISSION_SUPPORT_NUMBER`), never from a request. Message bodies are a fixed template (crew, problem type, reason,
time, session id) — no medical text.
- Offline (`mode=offline`) → `QueueSender`: `queued_offline`, UI text *"Ground will receive this in ~20 min.
  Continue the checklist."* The queue is flushed on the next SOS request made online.
- Online, `ALERT_MODE=dry_run` (default) → `DryRunSender`: logs what would be sent, calls nothing;
  message and `escalation.state` = `simulated`, `alert.sms_status` = `dry_run`.
- Online, `ALERT_MODE=live` → `TwilioSender` (SMS only; optional extra `alert`). Env: `TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `MISSION_SUPPORT_NUMBER`. One automatic retry; still failing →
  `failed`, UI "Alert not delivered. Continue the checklist.", checklist continues. Trial accounts can only text
  verified numbers.
- **Phase 3:** voice call (TwiML), `ACK <session>` SMS replies with signature check, status callbacks.

### 8.5 Proactive nudge (`sos/nudge.py`, `config/nudges.yaml`)
Suggests checking on a crewmate when their score turns `alert`. Deterministic, offline, no LLM, **no new medical
content**: suggestions are derived from the checklists' `red_flags`.
- **Trigger (push):** a hook runs right after every `Engine.update` (`routes_sos.attach_nudges(engine)`, called once in
  `app.py` after the warm-up, so only live updates nudge). When that `ScoreResult` transitions into `alert` (first
  alert seen, or non-alert → alert) the nudge is created in the same call and sent to the event exit
  **`on_nudge(nudge)`**: subscribers register with `halo_agent.sos.nudge.register_on_nudge(fn)` (Dhruv's WebSocket).
  A snoozed nudge that becomes active again is pushed too (noticed on the next score update or list). A failing
  subscriber or hook never affects `Engine.update`. The hook uses `engine.add_listener(fn)` if core provides it,
  otherwise it wraps `update` on that Engine instance (core code unchanged).
- **Red-flag alert** (a reason with a red-flag `rule_id` and status `alert`): checklists whose `red_flags` contain it,
  ordered by `priority` (`spo2_low: [breathing_difficulty, chest_pain]`, `bp_crisis: [chest_pain]`; other matches
  follow in file order), at most `max_suggestions` (2), then `other`. Text: "Check on your crewmate. Red flag: …".
  `priority` can only order existing links: listing a checklist that lacks the red flag fails at load.
- **z-band alert, no red flag:** `other` only, text "Check on your crewmate".
- **Suppression** (each logged to `data/sos_sessions/nudges.jsonl` as `nudge_suppressed` with `why`): an SOS
  session is open for that crew, the crew is snoozed, less than `min_interval_s` (300, `team_derived`) since the
  last nudge, or a nudge is still active.
- **Nudge:** `{nudge_id, crew_id, created_at, state: active|snoozed|dismissed|accepted, text, trigger: {reading_id,
  as_of, status, severity, kind: red_flag|z_band, red_flags, reasons}, suggestions: [{symptom, protocol_id, title,
  review_label, because}], snoozed_until, session_id, disclaimer}`.
- **Endpoints:** `GET /api/v1/crew/{crew_id}/nudges` (lists active nudges; never creates one), `POST /api/v1/nudges/{id}/snooze`
  (`minutes` ∈ `snooze_minutes` [5, 15, 30]; also holds back new nudges for that crew until it ends; the snoozed
  nudge comes back active afterwards), `POST /api/v1/nudges/{id}/dismiss`, `POST /api/v1/nudges/{id}/accept`
  (`symptom` must be one of the suggestions; starts an SOS session for that crew — mission support is alerted —
  and returns `{nudge, session}`). Unknown nudge → 404, already dismissed/accepted → 409, bad input → 422.

## 9. M5 — Daily report

1. **Trigger:** `POST /activity/morning`, or a fixed time.
2. **Collect:** the previous night's `in_bed` period (minutes, HR during it), morning activity time ⚑,
   resting vitals, 3-day trends, and the radiation environment reference.
3. **Score:** call M3.
4. **Template first:** build all structured fields and a template `summary_text`. Default `duty_status`
   mapping (`team_derived`): normal → `go`, caution → `caution`, alert → `rest`.
5. **Shell rewrite (online):** the LLM receives **only the structured fields** and writes one paragraph.
   Validate the result: every number in the text must appear in the fields. If validation fails, fall back
   to the template and set `summary_generated_by: "template"`.
6. Every number shown carries its source (`demo_playback`, face scan, NASA historical reference).

## 10. M6 + shell — knowledge and LLM

- `LLMClient` (`shell/llm.py`): `available() -> bool`, `complete(system, user, timeout_s) -> str`. Provider and
  key come from env (`HALO_LLM_PROVIDER`, `HALO_LLM_API_KEY`, `HALO_LLM_MODEL`) ⚑; until an adapter exists every
  value maps to `NullLLM`. Tests use `FakeLLM`. **All calls go through `call_llm()`**, which returns the fallback
  before touching the client when offline or unavailable; timeout `HALO_LLM_TIMEOUT_S` (≤ 8 s); errors fall back.
- Orchestration: **LangGraph** router → `sos` / `ask` / `report` subgraphs. In offline mode the same graph
  runs, with LLM nodes swapped for rule/template nodes.
- Retrieval: local `sentence-transformers` + FAISS (or Chroma) over protocol steps and NASA public reference
  chunks. Each chunk has an `id` and a `source`.
- `/agent/ask` with `session_id` (sos-v0): the context is the **current step** (id, text, `ref`, source titles) +
  latest vitals values only; the reviewer `note` is not sent. No LLM call if the step is the placeholder or has no
  source, or the checklist is finished → "The checklist doesn't cover this — contact ground." The answer is
  accepted only if it cites exactly the current step id, every number in it appears in the context, it repeats
  no word the question introduced that the step does not use (e.g. a drug name), and it contains no dose word
  (mg, tablet, dose, …) absent from the step; otherwise the same fixed reply. Offline / no LLM →
  "Not covered offline — follow the checklist". Without `session_id`: knowledge-base chunks only, with citations
  (retrieval not built yet → fixed "not covered" reply, no LLM call). Unknown `session_id` → 404. Each ask is logged
  as an `ask` event (question, `answered_by`, citations).
  Response: `{answer, citations: [{kind: "checklist_step" | "kb_chunk", id, ref, sources: [{key, title, url,
  medical}]}], session_id, answered_by: "llm" | "fixed_reply", mode, disclaimer}`.
- `/sos/{id}/classify` response: `{session_id, candidates, source: "llm" | "picker_fallback",
  requires_confirmation: true, mode, low_confidence}`. `llm`: exactly 3 candidates
  `[{"symptom", "confidence": 0.0–1.0}]`, best first, restricted to problem types that have a checklist (invented
  types dropped, padded with the remaining types at 0.0). `picker_fallback` (offline, no LLM, timeout, unparsable
  output): every problem type at confidence 0.0. Never auto-selected; logged as a `classify` event.
- Speech-to-text happens in the browser (Web Speech API). The backend receives text only.

## 11. Layout

```
halo_agent/
  core/        config.py  models.py  baseline.py  trend.py  rules.py  score.py  engine.py
  sos/         protocols.py  state_machine.py  escalation.py  session_log.py
  report/      daily.py  templates.py
  shell/       llm.py  classify.py  ask.py  rewrite.py  graph.py  retrieval.py
  notify/      base.py  queue.py  console.py
  api/         app.py  routes_score.py  routes_sos.py  routes_report.py  routes_admin.py  stubs.py
  cli.py
config/        scoring.yaml  red_flags.yaml  escalation.yaml  app.yaml  thresholds/*.json
protocols/     *.json
data/          qr_scan_demo_clean.csv  radiation_5min.csv  reference/…  SIMULATED_*.csv
scripts/       make_drift_scenario.py  make_sos_scenario.py
tests/  tests/fixtures/
README.md
```
`core/` must not import from `shell/`. Add a test that enforces this.

Environment: Python 3.11, `uv`, `pyproject.toml`. Deps: fastapi, pydantic>=2, pyyaml, uvicorn, pytest,
httpx. Optional extras group `shell`: langgraph, sentence-transformers, faiss-cpu, plus the LLM SDK ⚑.
The core must install and run without the `shell` extras.

## 12. Phases (work in this order; feature freeze Sun 10:00)

**Phase 0 — Sat early afternoon. Unblocks the team.**
- Every endpoint of Chris's in §6.1 returns fixed JSON matching §6. Run it, then tell the user so the design group can wire screens.

**Phase 1 — Sat afternoon. Core.**
- CSV replay → frozen baseline → robust-z, debounce, red flags → `ScoreResult` (all six vitals), batch + `Engine.update`.
- 2–3 placeholder protocols + SOS state machine + escalation + `QueueNotifier` + session log. **No LLM yet.**
- Stop and show the user: one `ScoreResult` and one full SOS session trace.

**Phase 2 — Sat evening. Shell.**
- `LLMClient`, LangGraph router, `/classify`, `/agent/ask` (checklist-scoped), daily report template + LLM rewrite with validation.
- Offline toggle wired through every feature.

**Phase 3 — Sat late night. Demo data and extras.** Drop anything that isn't done.
- `make_drift_scenario.py` → `SIMULATED_drift_7d.csv`: 7 days of slowly rising resting HR and falling
  SpO2. No red flag fires; trend `caution` with a finite `days_to_alert` appears by day 4–5.
- `make_sos_scenario.py` → `SIMULATED_sos_event.csv`: an acute SpO2 drop + HR rise that reaches `alert`.
  **Pending Kavya's OK** (⚑ Q4).
- Radiation `environment_context`, and Twilio if confirmed.

**Phase 4 — only if everything else is done.** Bone density, confidence polish, local-LLM experiments.

**Not before Sunday:** training an ML scoring model (keep the interface ready), building face-scan vital-sign
algorithms, tuning a local LLM.

## 13. Tests (definition of done)
- Demo playback runs with no errors. Report the % of `normal` rows (expected high, with the BP red flag near the end).
- Fixtures (SIMULATED): HR rising over several days, SpO2 falling, a single spike (stays `caution` because of debounce),
  baseline with < 3 points, NaN values, MAD = 0.
- `score()` and an `Engine.update()` replay produce identical output.
- Every protocol validates. An SOS walkthrough reaches `END`. `cannot_do` escalates and the checklist continues.
  Offline escalation ends in `queued_offline`.
- Offline mode: `/classify` returns a picker fallback, `/agent/ask` returns the fixed reply, the report is
  `template` — and **no network call is attempted** (assert with `FakeLLM`).
- Report rewrite validation rejects text containing a number that is not in the fields.
- Import test: `core/` never imports `shell/`.

## 14. Open with the team (⚑) — don't block on them; use the defaults above
1. **Ownership:** is the Agent officially Chris's, and does scoring stay with Chris?
2. **Interfaces (Kavya, Dhruv):** CSV column names as the schema? Timestamp generation? Blink-rate field name? REST vs WS for the live feed?
3. **Protocol source (Kavya):** which public, medically reviewed checklists? Which 2–3 symptoms for the demo?
4. **Emergency data (Kavya):** a real public segment, or a scripted SIMULATED scenario clearly labelled?
5. **BP values (Kavya):** baseline at 162/58 rising to SBP 189 — intentional?
6. **Morning activity:** what is it, how is it timed, and which endpoint receives it?
7. **Twilio:** use it? SMS only or calls too? Whose account and phone?
8. **LLM:** which provider, who has the key, is there internet at the venue?
9. **Submission time and demo length:** the design doc says a 1–2 min video, but the official rule on the
   team map is a **video of 30 seconds or less** (or ≤ 7 slides), or no global judging. Confirm before recording.

## 15. README must state
- Every threshold, its source class and whether it is verified; every protocol's source and whether it is verified.
- Which data is real, replayed, historical-environment or simulated.
- What runs offline and what needs the LLM.
- Skipped items and why.
- "HALO is a prototype, not a medical device."
- AI tools used during development (required by Space Apps).
