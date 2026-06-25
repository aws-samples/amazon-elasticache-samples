# Temporary Policy Exception: SleepSync Apnea Alert — Goodwill Credit
**Exception ID:** TEMP-GW-SLEEPSYNC-APNEA-2026-02
**Region:** US
**Status:** Active
**Effective:** 2026-02-15 to 2026-03-31
**Overrides:** Standard goodwill credit limits
**Exception Priority:** 2

## Exception
Due to the SleepSync false apnea alert issue (KB3: ts_021), agents are authorized to offer enhanced goodwill credits:

| Scenario | Standard Limit | Exception Limit |
|---|---|---|
| Customer received false apnea alert, wants acknowledgment | $0 (no credit for software bugs) | $15 account credit |
| Customer saw a doctor based on false alert | $0 | $50 account credit |
| Customer saw a doctor AND wants to return SleepSync | Standard return policy | $50 credit + full refund even if outside 60-day window |
| Customer experienced significant distress/anxiety | Agent discretion up to $15 | Agent discretion up to $30 |

## Rationale
False health alerts cause real emotional distress. The standard "no credit for software bugs" policy is insufficient for health-related false alarms. This exception acknowledges the impact while the firmware fix (v1.4.0) is being finalized.

## Process
1. Follow KB3: ts_021 approved messaging FIRST (explain the issue, disable alerts)
2. Then offer goodwill credit based on scenario above
3. Use credit code SLEEPSYNC-GW-2026 in support portal
4. If customer saw a doctor: request documentation (not required, but helps with internal tracking). Do NOT make the credit conditional on documentation.

## Escalation
- Credits up to $50: Tier 1 agent can approve directly under this exception
- Credits above $50: escalate to Tier 2 (e.g., customer had a sleep study ordered based on false alert — costs could be significant)
- Tier 2 authorized up to $200 credit. Above $200: escalate to Customer Experience Manager.

## Expiration
Exception expires Mar 31, 2026 or when firmware v1.4.0 is deployed to all units, whichever comes first. After expiration, revert to standard goodwill credit limits.
