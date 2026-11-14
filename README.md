# HALO Agent

NASA Space Apps Challenge 2026 — *Create Health Monitoring Software for Astronauts on Space Missions*.

**HALO is a prototype, not a medical device.** Every output is decision support only, not a diagnosis.
Implementation rules live in [AGENTS.md](AGENTS.md).

## Run

```bash
uv sync
uv run pytest -q
uv run uvicorn halo_agent.api.app:app --port 8000
```

- Score a CSV offline: `uv run python -m halo_agent score data/qr_scan_demo_clean.csv --out out/scores.jsonl`
  (`--live` uses `Engine.update` instead of batch `score()`; the outputs are identical).
- Demo feed until M1 exists: start the server with `HALO_WARMUP_CSV=qr_scan_demo_clean.csv`, or
  `POST /api/v1/admin/replay {"file": "SIMULATED_drift_7d.csv", "speed": 600}`.
- Offline mode: `HALO_OFFLINE=1`, or `POST /api/v1/admin/mode {"mode": "offline"}`.
- Real camera scans from the team's scan page: `POST /api/v1/scan {"crew_id": "LIVE_01", "record": {...}}`
  (crew_id optional, default `LIVE_01`), or upload the downloaded JSON to `POST /api/v1/scan/upload` (form field
  `file`). `GET /api/v1/crews` lists every crew with `source: synthetic | live` for the front-end switch. CORS allows
  `https://dhruvachaitanya13.github.io` and any localhost port (`config/scan.yaml`).

No `.env`, API key or optional extra is needed for the above: the LLM features fall back automatically and SOS
alerts are a dry run (nothing is sent).

## Crew SOS: try it

Start the server with the demo feed so the SOS has vitals and a score to show:

```bash
HALO_WARMUP_CSV=qr_scan_demo_clean.csv uv run uvicorn halo_agent.api.app:app --port 8000
```

If port 8000 is taken, use another `--port` and change it in the commands below.
Then, in another terminal (interactive docs: <http://localhost:8000/docs>):

```bash
# problem types: chest_pain, breathing_difficulty, fainting, bleeding_injury, other
curl -s localhost:8000/api/v1/sos/problem-types

# start a session -> state, step 1, vitals, score, alert status (copy session_id from the reply)
curl -s -X POST localhost:8000/api/v1/sos -H 'Content-Type: application/json' \
  -d '{"crew_id": "CREW_01", "symptom": "chest_pain"}'

# answer the current step: done | cannot_do | condition_changed | skip
curl -s -X POST localhost:8000/api/v1/sos/SESSION_ID/step -H 'Content-Type: application/json' \
  -d '{"action": "done"}'

# ground confirms receipt, then end the session (resolved | handed_off)
curl -s -X POST localhost:8000/api/v1/sos/SESSION_ID/ack -H 'Content-Type: application/json' -d '{"by": "capcom"}'
curl -s -X POST localhost:8000/api/v1/sos/SESSION_ID/resolve -H 'Content-Type: application/json' \
  -d '{"outcome": "resolved"}'
```

| Endpoint | What it does |
|---|---|
| `GET /api/v1/sos/{id}` | full session for polling (step, vitals, score, alerts, event log) |
| `POST /api/v1/sos` with `{"crew_id", "free_text"}` | starts the generic `other` checklist at once; then `/classify` → crewmate picks → `/confirm` |
| `POST /api/v1/sos/{id}/classify` `{"free_text"}` | top-3 problem types (LLM) or the full picker (no LLM / offline) |
| `POST /api/v1/sos/{id}/confirm` `{"symptom"}` | switches to that checklist (logged as `checklist_switched`) |
| `POST /api/v1/agent/ask` `{"question", "session_id"}` | explains the current step only; otherwise a fixed reply |
| `GET /api/v1/crew/{crew_id}/nudges` | proactive "check on your crewmate" suggestions when a score turns `alert` |
| `GET /api/v1/sos/ground-reports` | non-urgent reports for the flight surgeon (never texted) |

Demo switches:

```bash
# "pull the network": alerts queue for ground, LLM features fall back
curl -s -X POST localhost:8000/api/v1/admin/mode -H 'Content-Type: application/json' -d '{"mode": "offline"}'
# mission environment for NEW sessions: microgravity | partial_gravity (default) | post_landing
curl -s -X POST localhost:8000/api/v1/admin/mode -H 'Content-Type: application/json' -d '{"mission_env": "microgravity"}'
```

Smoke test (in-process, nothing sent, no key): `uv run python scripts/smoke_sos.py` (add `--url
http://localhost:8000` to test a running server). Its `scan` scenario posts a sample camera scan for `LIVE_01` and is
skipped until `POST /api/v1/scan` exists. Live camera crews show "Live camera measurement (experimental)"; SpO₂ and
blood pressure show as missing ("Not measured by camera scan").

Session logs are written to `data/sos_sessions/` (git-ignored): one `.jsonl` per session, plus a `.json` export when
it ends. Full contract: AGENTS.md §6.4 and §8.

### SOS checklists: sources and review status

Converted verbatim from the team's `sos_checklists_v2.yaml` into `config/checklists/` (shared source table:
`config/checklists/_sources.yaml`). **All five are "Sourced, not medically reviewed"**; the UI always shows that label.

| Checklist | Sources cited | Verified |
|---|---|---|
| `chest_pain_v2` | NASA Medical Checklist JSC-48031 Rev H; DGLRM/ESAM microgravity CPR guideline (2020); MedlinePlus; HALO team policy (medication step, not a medical source) | **no** |
| `breathing_difficulty_v2` | NASA Medical Checklist JSC-48031 Rev H; DGLRM/ESAM CPR guideline; MedlinePlus; HALO team policy (medication step) | **no** |
| `fainting_v2` | NASA Medical Checklist JSC-48031 Rev H; NASA HRP orthostatic intolerance evidence; DGLRM/ESAM; MedlinePlus | **no** |
| `bleeding_v2` | NASA Medical Checklist JSC-48031 Rev H; microgravity bloodstain study; MedlinePlus | **no** |
| `other_v2` | NASA Medical Checklist JSC-48031 Rev H; MedlinePlus; HALO team policy (not a medical source) | **no** |

JSC-48031 page numbers come from a public OCR copy; verify against the PDF before citing them on stage. Numbers the
sources don't give are shown as `[N — TBD]`. HALO never names a drug or a dose.

### SOS alerts (SMS)

`ALERT_MODE=dry_run` (default) only logs what would be sent. For real SMS: `uv sync --extra alert`, set
`ALERT_MODE=live` and the four `TWILIO_*` / `MISSION_SUPPORT_NUMBER` variables in `.env`. Twilio trial accounts can
only text numbers verified in the Twilio console. The recipient is only ever read from `.env`.

## Optional: LLM features (Claude)

Without this, symptom classification falls back to the picker and `/agent/ask` gives a fixed reply. To turn it on:

```bash
cp .env.example .env          # then put your own key in HALO_LLM_API_KEY (never commit .env)
uv sync --extra llm
set -a && . ./.env && set +a  # load .env (uv's --env-file breaks on paths with spaces)
uv run --extra llm python -m halo_agent.shell.llm     # smoke test: prints "reply: OK" (one paid call)
HALO_WARMUP_CSV=qr_scan_demo_clean.csv uv run --extra llm uvicorn halo_agent.api.app:app --port 8000
```

Always keep `--extra llm` on `uv run`, otherwise uv re-syncs the environment and removes the SDK. Model:
`claude-opus-5-5` (`HALO_LLM_MODEL`), effort `low` (`HALO_LLM_EFFORT`). Answers are cached in
`data/llm_cache.json` (git-ignored) so the same question shows the same answer; empty it with
`curl -X DELETE localhost:8000/api/v1/admin/llm-cache`. Live LLM tests (paid, skipped by default):
`HALO_LIVE_LLM=1 uv run --extra llm pytest tests/test_sos_llm_live.py`.

## Data: what is real, replayed, historical or simulated

| File | Kind | Notes |
|---|---|---|
| `data/qr_scan_demo_clean.csv` | **Replayed demo data** (Kavya) | Composite dataset played back; not measured from a camera. Label `Demo data - not measured from your camera` is kept. `data_origin = demo_playback`. No calendar date: timestamps = `mission_start` (2026-11-15) + mission day + clock time. |
| `data/SIMULATED_drift_7d.csv` | **SIMULATED** | Generated by `scripts/make_drift_scenario.py` (seeded). 7 days; days 1–2 baseline, then resting/in-bed HR +1.5 bpm/day and SpO₂ −1.0 %/day. Made-up values for the demo, not physiology claims. `data_origin = simulated`. |
| `data/radiation_5min.csv` | **Historical environment** (NASA RadLab, DOSTEL-1, ISS Columbus, May 2024) | Cabin environment, **not personal dose**. Only an `environment_context` reference in the daily report; it never changes a crew member's status. |
| Camera scans (`POST /api/v1/scan`) | **Real measurement, experimental** (team scan page) | Heart rate (only when `heart_quality ≥ 0.35`), breathing rate (only when `breathing_quality ≥ 0.35`) and blink rate from a face video. Label `Live camera measurement (experimental)`, `data_origin = camera_scan`. SpO₂, BP and temperature are **not measured** and stay missing. Test scores, self-reported sleep / mood / alertness and the scan page's own verdict are kept in `scan_context` and are never scored. Persisted to `data/live_scans.jsonl` (git-ignored) and restored at startup; clear with `DELETE /api/v1/admin/live-scans?crew_id=`. |
| `data/simulated_bone_density.csv` | **SIMULATED** | From published loss rates; not used by the scoring engine yet (Phase 4). |

Environment data and QR readings are never merged into one dataset. Real scans and synthetic data are never mixed
either: every crew has its own baseline, a crew is either live or synthetic, and a scan sent to a synthetic crew id
(e.g. `CREW_01`) is refused (409).

## Scoring thresholds (engine `rules-v0`)

All values are in `config/scoring.yaml` and `config/red_flags.yaml`; nothing is hard-coded.

| Threshold | Value | Source class | Verified |
|---|---|---|---|
| Personal baseline | median / MAD per crew × metric × activity | personal baseline | — |
| Robust SD | σ = 1.4826 × max(MAD, floor) | standard statistics constant | — |
| MAD floors | HR 1 bpm, resp 1 /min, BP 2 mmHg, temp 0.1 °C, SpO₂ 1 % (measurement resolution) | team_derived | — |
| Minimum baseline points | 12 per activity group (pooled fallback otherwise; HR never pooled) | team_derived | — |
| Minimum baseline points, live camera crews | 3 scans (about one scan a day); below that "Baseline building (n/3)" | team_derived | — |
| Camera quality gates | `heart_quality ≥ 0.35` and `breathing_quality ≥ 0.35` (scan page `QUALITY_MIN`), else missing | team_derived | — |
| Confidence for camera scans | completeness and baseline terms over HR + resp only (`measurable_metrics`); baseline complete at 3 scans | team_derived | — |
| z bands | \|z\| ≥ 2 caution, ≥ 3 alert, unfavourable direction only | team_derived | — |
| Alert debounce | 2 consecutive new readings beyond the alert band | team_derived | — |
| Trend | 3-day window, ≥ 6 readings per day, \|slope\| ≥ 0.5 σ/day **and** slope t ≥ 2.3 | team_derived | — |
| `days_to_alert` | shown when 0 < d ≤ 14 | team_derived | — |
| Confidence | baseline × freshness (1 up to 30 min → 0 at 6 h) × completeness | team_derived | — |
| Daily report duty (`ready` / `caution` / `rest`) | resting readings of the last 4 h before the report (live crews: the last 2 scans), z level counts after 2 new readings in a row; trends and red flags (any activity) count at once; nothing assessable → `caution` | team_derived | — |
| Live report comparisons | self-reported sleep and the docking / reaction scores vs the median of ≥ 3 earlier scans of the same crew | team_derived | — |
| Report recommendations | three fixed sentences, no medical content | team_policy | — |
| Red flag `spo2_low` | SpO₂ < 90 % | guideline | **no** |
| Red flag `fever` | temp ≥ 38.0 °C | guideline | **no** |
| Red flag `bp_crisis` | SBP ≥ 180 mmHg | guideline (AHA hypertensive crisis category) | **no** |

Red flags were supplied by the team and are provisional until their sources are confirmed. A rule without a
`source` never runs.

**Limits you should know about**

- **SpO₂ resolution.** SpO₂ readings are whole percent, so the MAD floor gives σ ≈ 1.48 %. With a 3-day
  window the smallest SpO₂ drift the trend rule can detect is about **1 % per day**; slower drifts show up
  only once the single-reading z band catches them.
- **Overfitting risk.** The trend gate (`min_slope_t = 2.3`, chosen over R² ≥ 0.6 and "all steps in the same
  direction"), `min_points_per_step = 6` and `no_pool_metrics` were tuned on `SIMULATED_drift_7d.csv` and
  the QR demo. The drift scenario's SpO₂ rate was then set to −1.0 %/day so it sits above the detection
  limit. These numbers fit our simulated data; they are not validated on real crew data.
- **Live camera crews.** One scan measures at most 2 of the 6 vitals (confidence counts only those 2), and trends
  never appear (a trend needs ≥ 6 readings per day). The scan page's own verdict is shown as "Scan verdict" next to HALO's
  duty status; the two are computed differently and can disagree.
- The QR demo has SBP ≥ 180 on 68 of 276 rows, so `bp_crisis` fires often in playback. This is the data, not
  a bug (open question to the data owner).

## What runs offline

The whole core (baseline, scoring, trends, red flags, SOS checklist state machine, report templates) runs
without network or LLM. LLM features (symptom classification, checklist-scoped Q&A, report prose) fall back to
the symptom picker, a fixed reply and the template text when offline.

## Skipped / not yet done

- Radiation prediction model: none exists, the UI shows "Model not trained".
- Bone density in scoring (Phase 4), ML scoring model, face-scan vital-sign algorithms.
- Daily report LLM rewrite (the shared `LLMClient` now exists; the rewrite itself is not built). The template report is live:
  `GET /api/v1/crew/{crew_id}/daily-report?date=` (sleep is estimated from `in_bed` periods; there are no
  sleep stages in the data).

## AI tools used during development

Claude Code (Anthropic) was used to write and test code in this repository.
