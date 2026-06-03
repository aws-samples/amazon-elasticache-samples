# Incident Bulletin: SleepSync False Apnea Alerts — Customer Impact

**Incident ID:** INC-PROD-SLEEPSYNC-APNEA-2026-02
**Status:** Active (firmware fix in development)
**Severity:** High
**Opened:** 2026-02-15
**Last Updated:** 2026-02-25

## Summary
SleepSync firmware v1.3.0/v1.3.1 is sending false "Potential Sleep Apnea Detected" notifications. The apnea detection algorithm is too sensitive, counting normal sleep movements as breathing pauses.

## Customer Impact
- Customers are experiencing health anxiety from false alerts
- Some customers have scheduled unnecessary doctor visits
- Trust in SleepSync product is eroding
- This is an emotionally sensitive issue — handle with extra care

## Agent Actions
1. **Identify:** Customer reports apnea alert notification from SleepSync
2. **Reassure:** Use EXACT approved messaging from KB3: ts_021. Do NOT improvise.
3. **Disable alerts:** App → Devices → SleepSync → Notifications → uncheck "Apnea Detection Alerts"
4. **Goodwill credit:** Per KB5: pc_023 exception — $15 standard, $50 if customer saw a doctor, up to $30 at discretion for distress
5. **Health disclaimer:** Include wellness device disclaimer per KB5: pc_026 (Template 2)
6. **Macro:** Use ELEC-KI-003 (cs_016) for response

## What NOT to Do
- Do NOT say "it's probably nothing" or "the device is inaccurate" (see KB3: ts_033 bad transcript example)
- Do NOT say "you should see a doctor just in case" (amplifies anxiety for a false alert)
- Do NOT say "you don't need to see a doctor" (we can't give medical advice)
- DO say: "This is a known software calibration issue that we're actively fixing"

## Metrics (as of Feb 25)
- Total contacts: 234
- Alerts disabled successfully: 228 (97%)
- Goodwill credits issued: 189 (avg $18.50)
- Customers who saw a doctor: 12 ($50 credit each)
- Escalations to Tier 2: 6
- CSAT for this issue: 3.8/5 (lower than SwiftPace bricking — emotional impact is harder to resolve)

## Cross-Reference
- Technical details: KB3 ts_021
- Bad handling example: KB3 ts_033 (transcript — agent used unapproved messaging)
- Goodwill credit exception: KB5 pc_023
- Health disclaimers: KB5 pc_026
- Macro: cs_016 (ELEC-KI-003)
