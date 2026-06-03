# SOP: Electronics Troubleshooting Triage and Handoff Workflow

## Purpose
Standardize how agents triage electronics support contacts and determine whether to troubleshoot, escalate, or hand off to a seller.

## Step 1: Identify the Product
Ask: "Which Accent Athletics device are you contacting about?"
- SwiftPace Running Watch → KB3: ts_017 (GPS), ts_018 (HR), ts_022 (bricking)
- AeroTrack Pro Earbuds → KB3: ts_012 (setup), ts_013 (ANC), ts_019 (battery drain)
- PulseFit Band 4 / 4 Pro → KB3: ts_001 (setup), ts_002 (sync), ts_003 (firmware delay)
- StrideScale → KB3: ts_014 (setup), ts_015 (Wi-Fi sync), ts_020 (body fat spike)
- SleepSync → KB3: ts_016 (setup), ts_021 (false apnea alert)
- PowerBase Charging Dock → KB3: ts_009 (charging interruption)

## Step 2: Identify Purchase Source
Ask: "Where did you purchase this device?"
| Source | Next Step |
|---|---|
| accentathletics.com or Accent Athletics store | Full support — proceed to troubleshooting |
| FitTech Solutions | Software/firmware: full support. Hardware/shipping: redirect to FitTech (KB4: vs_018) |
| GearVault Outlet | Software: full support. Hardware: 90-day GearVault warranty (KB4: vs_019) |
| RunReady or TrailBound | They don't sell electronics — verify product is genuine |
| PeakForm Athletics | SUSPENDED seller — counterfeit. Follow KB4: vs_020, do NOT troubleshoot |
| Other / unknown | Verify authenticity (KB4: vs_022) before troubleshooting |

## Step 3: Check for Active Known Issues
Before troubleshooting, check if the customer's symptom matches an active known issue:
| Symptom | Known Issue | Action |
|---|---|---|
| SwiftPace bricked during update | ts_022 (CRITICAL) | Recovery steps → warranty replacement if fails (pc_020 exception) |
| AeroTrack left earbud dies fast | ts_019 (HIGH) | Workaround: disable ANC or downgrade firmware |
| SleepSync apnea alert | ts_021 (HIGH) | Approved messaging → disable alerts → goodwill credit (pc_023) |
| StrideScale body fat jumped | ts_020 (MEDIUM) | Explain calibration issue, reassure weight is accurate |
| PulseFit Band sync delay | ts_003 (HIGH) | Workaround per ts_004 |

## Step 4: Troubleshoot or Escalate
- If known issue: follow the known issue bulletin directly. No general troubleshooting needed.
- If not a known issue: follow the product-specific troubleshooting article from KB3.
- If troubleshooting doesn't resolve: escalate to Tier 2 with notes on what was tried.

## Step 5: Warranty or Return?
After troubleshooting, if the issue is hardware:
- Within warranty → process warranty claim per pc_011
- Outside warranty → offer repair service or upgrade path
- GearVault purchase outside 90 days → no coverage from either party (pc_011 + KB4: vs_019)
- Counterfeit (PeakForm) → refund per pc_021, no warranty
