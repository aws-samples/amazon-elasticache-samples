# Known Issue: AeroTrack Pro Left Earbud Battery Drain (Firmware v2.4.0)
**Status:** Active
**Severity:** High
**Affected Products:** AeroTrack Pro (firmware v2.4.0 only)
**Reported:** 2026-02-20
**Expected Fix:** Firmware v2.4.1 (ETA: early March 2026)

## Description
After updating to firmware v2.4.0, some AeroTrack Pro users report the left earbud drains battery 2–3x faster than the right. Left earbud may die within 1.5 hours while right still has 60%+ battery.

## Root Cause
Engineering has identified a bug in v2.4.0 where the left earbud's ANC microphone stays active even when ANC is disabled or earbuds are in Transparency mode. The always-on mic draws continuous power.

## Affected Population
Estimated 30% of v2.4.0 users. Not all units are affected — appears related to specific hardware revision (Rev C boards manufactured Oct–Dec 2025).

## Temporary Workaround
1. Disable ANC completely: App → Sound Modes → select "Off" (not Transparency — Transparency still triggers the bug).
2. This reduces left earbud drain to near-normal levels.
3. If customer needs ANC: recommend downgrading to v2.3.2. App → Devices → AeroTrack Pro → Firmware → Advanced → Install Previous Version. **Note:** downgrade option was added in app v5.8.0 — customer must update app first.

## Customer Communication
- Do NOT proactively notify all customers. Only address when customer reports the issue.
- Approved response: "We're aware of a battery drain issue affecting some AeroTrack Pro earbuds on the latest firmware. Our engineering team has identified the cause and a fix is expected in early March. In the meantime, [provide workaround above]."
- Do NOT promise a specific date for v2.4.1.

## Escalation
No escalation needed — this is a known issue with a confirmed fix timeline. If customer demands immediate resolution beyond workaround, offer one-time $15 account credit (agent discretion, no manager approval needed).
