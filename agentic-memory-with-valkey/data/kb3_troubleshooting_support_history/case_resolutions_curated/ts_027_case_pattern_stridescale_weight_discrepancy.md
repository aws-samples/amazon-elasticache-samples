# Case Pattern: StrideScale Shows Different Weight Than Doctor's Scale

## Pattern Summary
Moderate-frequency pattern (avg 6 cases/week). Customer reports StrideScale weight differs from their doctor's office scale, gym scale, or another home scale — typically by 2–5 lbs.

## Root Cause (Usually Not a Defect)
Multiple factors cause scale-to-scale variation:
1. **Time of day:** Body weight fluctuates 2–4 lbs throughout the day (food, water, activity). Doctor visit is usually midday; home weigh-in is usually morning.
2. **Clothing:** Doctor's scale with clothes on vs. StrideScale at home in underwear/naked.
3. **Surface:** StrideScale on carpet reads inaccurately (up to 5 lbs off). Must be on hard floor.
4. **Calibration:** All consumer scales have ±0.5 lb tolerance. Medical scales are calibrated quarterly.
5. **Altitude:** Gravitational variation by location can cause ~0.1–0.2 lb difference (negligible but technically real).

## Resolution
1. Ask: "What surface is your StrideScale on?" If carpet → explain hard floor requirement. This resolves ~40% of cases.
2. Ask: "Are you comparing readings at the same time of day, in similar clothing?" If not → explain daily fluctuation.
3. Calibration check: place a known weight on scale (e.g., 10 lb dumbbell). If scale reads within ±0.5 lb, it's within spec.
4. If scale is on hard floor and reads >1 lb off from known weight: factory reset (remove batteries 30 sec, reinsert). Re-test.
5. If still >1 lb off after reset with known weight: defective load cell. Initiate warranty replacement.

## What NOT to Say
- Do NOT say "our scale is more accurate than your doctor's" (we don't know that)
- Do NOT say "all scales are different, that's just how it is" (dismissive)
- DO say: "Let's figure out what's causing the difference" and walk through the checklist

## Stats
- First-contact resolution rate: 82%
- Avg handle time: 8 min
- Escalation rate: 5% (actual hardware defect)
- Customer satisfaction: 4.1/5 (lower than average — weight discrepancy is emotionally charged)
