# Known Issue: SwiftPace Watch Crash During Firmware v3.2.0 Update
**Status:** Active
**Severity:** Critical
**Affected Products:** SwiftPace Running Watch (hardware Rev A and Rev B only)
**Reported:** 2026-02-22
**Expected Fix:** Firmware v3.2.1 hotfix (ETA: Feb 28, 2026)

## Description
Some SwiftPace watches are bricking (unresponsive black screen) during the v3.2.0 firmware update. The update process reaches ~70% and then the watch becomes unresponsive. Standard restart (long-press side button) does not recover.

## Root Cause
v3.2.0 update package exceeds available flash memory on Rev A/B hardware during the update process (not after — the final image fits, but the delta update process needs temporary space that Rev A/B boards don't have). Rev C hardware (manufactured after Aug 2025) is unaffected.

## Affected Population
~15% of update attempts on Rev A/B hardware. Rev C hardware: 0% failure rate. Total affected units estimated at 2,000–3,000 globally.

## CRITICAL: Update Has Been Pulled
- v3.2.0 has been **removed from the update server** as of Feb 23.
- Watches that have not yet updated will no longer be prompted.
- Watches already on v3.2.0 (successful update) are fine — the issue only occurs during the update process.

## Recovery for Bricked Watches
1. **Hard reset:** Press and hold BOTH side buttons simultaneously for 30 seconds. If watch vibrates and shows logo, it's recovering. Wait 5 min.
2. **USB recovery mode:** Connect watch to computer via charging cable. Hold top button while connecting. Watch should appear as USB drive. Download recovery image from support.accentathletics.com/swiftpace-recovery and drag to USB drive. Watch will reboot.
3. **If neither works:** Watch must be sent in for repair/replacement. Initiate warranty replacement — **expedited shipping, no cost to customer, regardless of warranty status.** This is a manufacturer defect.

## Customer Communication
- Acknowledge the severity: "I'm sorry your watch was affected by this update issue. This is a known problem that we've already taken steps to prevent for other customers."
- Do NOT blame the customer or suggest they did something wrong during the update.
- Offer recovery steps. If recovery fails, immediately initiate replacement — do not make customer jump through hoops.

## Escalation
- Bricked watch that can't be recovered via hard reset or USB: Tier 1 can initiate warranty replacement directly. No Tier 2 escalation needed.
- Customer demanding compensation beyond replacement: escalate to Tier 2. Authorized to offer replacement + up to $25 credit.
