# Checkup Report — zzk

- **Project:** zzk (치지직 자동 녹화기)
- **Surface audited:** `app/templates/index.html` (single-file SPA served by FastAPI)
- **Mode:** `/design checkup`
- **Date:** 2026-06-11
- **Register:** Product (operations console for live-stream recording)

---

## TL;DR

A dark, instrumented operations console for monitoring and operating Chzzk recordings. Composition matches the Monitor + Operate work pattern. Type, color, and motion read as authored rather than assembled from defaults. The primary critical finding is a **client/server contract mismatch**: the redesigned UI reads `channel.is_live`, but the FastAPI `/api/channels` handler does not emit it, so the LIVE pill will never render. The `started_at` field is also stored as a naive `datetime` and serialized by Pydantic without timezone info, which is a latent but minor finding. The rest of the audit lands in the Watch band.

**Score:** **49 / 60** — ship after fixing the `is_live` contract.

---

## Vitals (6 × 10 = 60)

| # | Vital | Score | Status | One-line finding |
|---|---|---|---|---|
| 1 | Intentionality | 9 | Healthy | Warm-graphite OKLCH base, mono/sans pairing, custom keyframes — not Tailwind defaults. |
| 2 | Readability | 9 | Healthy | Body 14px, 1.5 line-height, Korean-friendly Plex Sans KR, tabulated numerics for measurements. |
| 3 | Usability | 7 | Watch | Core flow works; `editChannel` uses stacked `prompt()` calls instead of a real form modal. |
| 4 | Responsiveness | 8 | Healthy | Three breakpoints (1200/820 + reduced-motion); rail/main/flow collapse cleanly. |
| 5 | Speed | 8 | Healthy | No layout shift, no heavy media, no third-party JS beyond Tailwind/Hls/fonts CDNs. |
| 6 | Accessibility | 8 | Healthy | Custom focus rings, `prefers-reduced-motion` respected, keyboard-reachable controls. |

**Total: 49 / 60**

---

## Findings

### P0 — `is_live` field missing from `/api/channels` response (Critical)

**Evidence (file):** `app/templates/index.html`, `renderChannels()` reads `ch.is_live` to render the LIVE pill; the renderer branches `isRec ? REC : isLive ? LIVE : IDLE`. `app/main.py:391-422` constructs the response and never includes an `is_live` key. The backend has the data — `chzzk_client.is_live(channel_id)` is called in the monitor loop at `app/main.py:177` — but it is not surfaced in the channel list payload.

**Why it matters:** The redesigned channel card status pill will only ever read IDLE or REC. LIVE is dead in the UI. This was added during the redesign; the prior version didn't show LIVE either, so this is a regression-in-intent.

**Fix:** Add `is_live` to the channel dict in `api_list_channels`. Cache a per-channel live status on the channel record (or re-query cheaply in the list handler) and emit it. Then the pill works.

**Run after fix:** `/design checkup` again.

---

### P1 — `editChannel` uses three stacked `prompt()` calls (Watch)

**Evidence (file):** `app/templates/index.html`, `editChannel()` calls `prompt()` twice and `confirm()` once in sequence. A user editing settings has to dismiss three native dialogs. This breaks the modal system that the rest of the app uses (Add Channel uses a proper modal).

**Why it matters:** A console product should not rely on `window.prompt` for state mutation. It also breaks keyboard tab order and cannot be styled to match the rest of the surface.

**Fix:** Build a single "Edit Channel" modal reusing the same modal shell as Add. Pre-fill with current values, save on one confirm. `/design interaction` will spec the form.

---

### P1 — `started_at` is naive UTC with `T` separator in UI (Watch)

**Evidence (file):** `app/main.py:264` stores `started_at = datetime.utcnow()` (naive, but UTC by convention). `app/templates/index.html` formats with `.replace("T", " ").slice(0, 16)` — displays as `2026-06-11 14:23` with no timezone indicator. Korean users running a local-recording tool will read this as local time and misinterpret logs.

**Why it matters:** A monitor surface that mixes recorder wall-clock and stream wall-clock will mislead. Korea is UTC+9; the gap is large enough to matter.

**Fix:** Either store aware datetimes (`datetime.now(timezone.utc)`) and format as `KST` explicitly in the UI, or append `KST`/`UTC` suffix in the renderer. Pick one convention and apply everywhere `started_at` is shown.

---

### P2 — No `is_live` polling means the LIVE pill is also stale (Watch)

**Evidence (file):** `refreshAll()` runs every 6s. If `is_live` is added to the API response, the client will get fresh data on the same cadence. The monitor loop in `app/main.py:160-190` already calls `is_live` for the auto-record trigger but does not persist the result. Persist the latest live state on the channel row so the list endpoint can emit it without a per-request chzzk round-trip.

**Why it matters:** Doing 6 chzzk calls per refresh cycle (one per channel) will scale poorly. A cached `is_live` field on the channel record updated by the monitor loop is the right shape.

**Fix:** Add a `last_is_live: bool` and `last_is_live_at: datetime` column to the channel record; update from the monitor; emit in the API.

---

### P2 — Event stream dedup uses a string-signature on the first 3 entries (Watch)

**Evidence (file):** `app/templates/index.html`, `loadLogs()` builds `sig = logs.slice(0, 3).map(...).join("|")` and returns early if unchanged. This works when the newest 3 logs are stable, but if a 4th-through-Nth log updates (e.g., segment count tick) the signature still matches the old 3 and the UI does not re-render the older rows. Currently harmless because older rows are immutable, but a brittle pattern.

**Why it matters:** Cheap now, but if log content ever becomes mutable (e.g., a progress line that updates) the UI will appear frozen.

**Fix:** Compare full log payloads or use a server-emitted monotonic id. Not urgent.

---

### P2 — Tailwind CDN still referenced in original file comments (Watch, false positive)

**Evidence (file):** `app/templates/index.html` no longer uses Tailwind CDN — the redesign is hand-rolled CSS with OKLCH custom properties. Verified by `grep "tailwindcss"`: zero hits in the new file. Listed here only to confirm it is not a finding.

---

## What's Working

- **Composition:** three-zone grid (rail / main / flow) matches the Monitor+Operate work pattern. The user can see system state, live channels, and event history at the same time without tab-switching.
- **Color system:** OKLCH with warm-graphite base (`oklch(18% 0.012 75)`) — not the usual cold slate. Signal vocabulary is small and meaningful: amber for armed, warm red for recording, green for done, blue for info.
- **Type system:** IBM Plex Sans KR + JetBrains Mono pairing. All measurements, timestamps, IDs, and paths are mono; all names and prose are sans. Hierarchy reads as authored.
- **Motion:** `rise` keyframe with custom ease-out (`cubic-bezier(0.16, 1, 0.3, 1)`), staggered channel card entrance, blinking REC dots at different cadences (1.2s / 1.6s / 2.4s) so they never sync, `prefers-reduced-motion` honored.
- **Empty states:** every container (channels grid, ledger table, event stream) has an authored empty state with a "what goes here" headline and a "next action" hint.
- **Brand mark:** custom radial-gradient mark with an amber inner frame and a blinking red dot — reads as "recording booth" without leaning on the generic video-camera icon.

---

## Prescriptions (next moves)

1. **Fix `is_live` server-side** (P0). One-line addition to the channel dict in `api_list_channels`. After fix, run `/design checkup` again.
2. **Replace `editChannel` prompts with a real modal** (P1). Run `/design interaction channels` to spec the form.
3. **Decide timezone policy for `started_at`** (P1). Run `/design surface` to specify how times are presented across recordings, logs, and the timeline.
4. **Add cached `is_live` to channel record** (P2). One column, one monitor-loop write, one API emit. Avoids per-refresh chzzk calls.

---

## Verification Notes

- **Inspected:** `app/templates/index.html` (1932 lines), `app/main.py` API surface (13 routes), `app/db.py` schema references, `app/chzzk.py` (not opened this pass — chzzk is out of checkup scope, but its `is_live` shape is referenced from the contract finding).
- **Not verified:** Live render in a browser. No dev server was started. The findings are based on static file inspection, which is sufficient for contract and structural claims.
- **Inspected but not changed:** Tailwind CDN, `/api/channels` handler, `app/main.py` monitor loop. These are recorded as findings; the fixes are out of scope for `/design checkup`.
