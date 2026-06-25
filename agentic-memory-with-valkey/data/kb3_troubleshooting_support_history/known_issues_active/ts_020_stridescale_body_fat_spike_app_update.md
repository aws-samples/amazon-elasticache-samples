# Known Issue: StrideScale Body Fat Readings Spike After App Update v5.7.0
**Status:** Active
**Severity:** Medium
**Affected Products:** StrideScale (all hardware revisions) with Accent Athletics Health app v5.7.0+
**Reported:** 2026-02-10
**Expected Fix:** App v5.9.0 (ETA: March 2026)

## Description
After updating the Accent Athletics Health app to v5.7.0, some StrideScale users see body fat percentage readings jump 3–8 percentage points higher than previous readings. Weight readings are unaffected.

## Root Cause
App v5.7.0 introduced an updated body composition algorithm intended to improve accuracy for athletic body types. However, the new algorithm was not properly calibrated for the StrideScale's bioimpedance sensor frequency. The algorithm works correctly with PulseFit Band 4's body composition feature but overcorrects on StrideScale data.

## Affected Population
All StrideScale users on app v5.7.0+. The spike is most noticeable for users with body fat in the 15–25% range. Users above 30% or below 12% see minimal difference.

## Temporary Workaround
- No user-facing workaround available. The algorithm runs server-side in the app.
- **Do NOT tell customers to downgrade the app** — v5.7.0 contains critical security patches.
- Advise customers: "We're aware that recent body composition readings may appear higher than expected. This is a software calibration issue, not a change in your actual body composition. A fix is coming in the next app update. Your weight readings remain accurate."

## Customer Communication
- Validate the customer's concern. Many users are distressed by sudden body fat increase.
- Emphasize: weight is accurate, body fat algorithm is being recalibrated.
- If customer asks "which reading is correct — old or new?": "The previous readings were closer to accurate for StrideScale. The new algorithm will be corrected in the upcoming update."

## Escalation
No escalation needed. If customer demands refund due to inaccurate readings, follow standard return policy (30-day window). Do not offer exceptions beyond policy.
