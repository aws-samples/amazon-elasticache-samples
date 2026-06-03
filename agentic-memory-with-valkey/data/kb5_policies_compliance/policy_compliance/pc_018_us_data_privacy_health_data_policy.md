# US Data Privacy and Health Data Policy (Connected Devices)
**Policy Version:** PRIV-HEALTH-US-v1.4
**Region:** US
**Status:** Approved
**Effective:** 2026-01-01 to 2026-12-31

## Scope
Covers personal and health data collected by Accent Athletics connected devices:
- **PulseFit Band 4/Pro:** Heart rate, steps, sleep, blood oxygen, activity
- **SwiftPace Watch:** Heart rate, GPS location/routes, activity, sleep
- **AeroTrack Pro:** Listening habits, noise exposure levels
- **StrideScale:** Weight, body composition (body fat %, muscle mass, bone mass, water %)
- **SleepSync:** Breathing rate, sleep stages, movement, apnea detection events

## Data Classification
| Data Type | Classification | Retention | Deletion on Request |
|---|---|---|---|
| Activity data (steps, distance) | Personal | Indefinite (until account deletion) | Yes, within 30 days |
| Heart rate / blood oxygen | Health (sensitive) | Indefinite | Yes, within 30 days |
| GPS routes | Location (sensitive) | 2 years, then auto-archived | Yes, within 30 days |
| Body composition | Health (sensitive) | Indefinite | Yes, within 30 days |
| Sleep / breathing data | Health (sensitive) | Indefinite | Yes, within 30 days |
| Apnea detection events | Health (sensitive) | Indefinite | Yes, within 30 days |
| Device diagnostics | Technical | 1 year | Auto-deleted |

## State-Specific Requirements
- **California (CCPA/CPRA):** Customer has right to know, delete, and opt out of sale. Health data has additional protections under CMIA.
- **Washington (My Health My Data Act):** Explicit consent required before collecting health data. Consent obtained during app onboarding.
- **Illinois (BIPA):** Does not apply — Accent Athletics devices do not collect biometric identifiers (fingerprint, face, iris). Heart rate is biometric data but not a biometric identifier under BIPA.
- **All other states:** Follow baseline US privacy policy.

## Agent Responsibilities

### Privacy Requests (Use pc_004 Templates)
- Customer asks "what data do you have on me?" → Data Subject Access Request (DSAR). Use pc_004 template. Fulfill within 30 days.
- Customer asks "delete my data" → Deletion request. Use pc_004 template. Confirm scope (all data? specific device? specific date range?). Process within 30 days.
- Customer asks "stop collecting my health data" → Opt-out. Guide customer to app settings: Privacy → Data Collection → toggle off specific categories. Device continues to function but data is not uploaded to cloud.

### GearVault Factory Reset Issue (KB4: vs_027)
- GearVault sold renewed PulseFit Band 4 units without factory reset. Previous owner's health data was visible.
- This is a **data privacy incident**. If customer reports seeing someone else's data:
  1. Apologize and walk through factory reset (KB3: ts_001)
  2. Reassure: no account info (name, email) was on the device — only anonymized activity data
  3. File internal privacy incident report (category: PRIV-INCIDENT-THIRDPARTY-DATA)
  4. Do NOT tell customer to "just ignore it" — take it seriously

### SleepSync Apnea Data Sensitivity
- SleepSync apnea detection (KB3: ts_021 — currently miscalibrated) generates health data that customers may find alarming.
- If customer asks "can you delete my apnea alerts?": Yes. App → Health → Sleep → Apnea History → Delete All. Or submit deletion request per pc_004.
- If customer asks "will my insurance company see this?": "Accent Athletics does not share your health data with insurance companies, employers, or any third party without your explicit consent."
