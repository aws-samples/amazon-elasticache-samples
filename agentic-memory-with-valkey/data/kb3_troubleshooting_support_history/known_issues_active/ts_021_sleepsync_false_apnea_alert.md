# Known Issue: SleepSync False "Apnea Alert" Notifications
**Status:** Active
**Severity:** High
**Affected Products:** SleepSync (firmware v1.3.0 and v1.3.1)
**Reported:** 2026-02-15
**Expected Fix:** Firmware v1.4.0 (ETA: late February 2026)

## Description
SleepSync is sending false "Potential Sleep Apnea Detected" push notifications to users who do not have sleep apnea. The alert triggers when the device detects >5 breathing pauses per hour during sleep.

## Root Cause
Firmware v1.3.0 introduced apnea detection as a new feature. The breathing pause threshold is too sensitive — it's counting normal sighs, position changes, and brief arousals as apnea events. Engineering is recalibrating the detection algorithm.

## CRITICAL: Health Anxiety Risk
This is a **high-sensitivity issue**. Customers receiving false apnea alerts may:
- Experience significant health anxiety
- Schedule unnecessary doctor visits
- Lose trust in the product

**Every agent handling this issue must follow the approved messaging below exactly.**

## Approved Customer Response
"I understand this alert is concerning. We've identified that the current version of SleepSync is sending apnea alerts more frequently than intended due to a software calibration issue. The device is detecting normal sleep movements as breathing pauses. **This does not mean you have sleep apnea.** We have a firmware update coming very soon that corrects this. In the meantime, I can help you disable the apnea alert notifications while keeping all other sleep tracking features active."

## Temporary Workaround
1. App → Devices → SleepSync → Notifications → uncheck "Apnea Detection Alerts"
2. This disables only the apnea notifications. All other sleep data (stages, breathing rate, movement) continues to track normally.
3. **Do NOT recommend disabling the device entirely** — customers value the sleep tracking data.

## What NOT to Say
- Do NOT say "the device is inaccurate" (it tracks sleep stages fine, only apnea detection is miscalibrated)
- Do NOT say "you should see a doctor just in case" (this amplifies anxiety for a false alert)
- Do NOT say "it's probably nothing" (dismissive)
- DO say: "This is a known software issue that we're actively fixing"

## Escalation
Escalate to Tier 2 only if: customer reports they've already seen a doctor based on the alert and wants compensation. Tier 2 has authority to offer up to $50 credit + expedited firmware update (beta channel).
