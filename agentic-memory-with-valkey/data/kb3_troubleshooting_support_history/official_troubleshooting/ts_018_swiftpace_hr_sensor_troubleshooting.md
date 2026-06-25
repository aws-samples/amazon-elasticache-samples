# SwiftPace Running Watch Heart Rate Sensor Troubleshooting

## Symptom
SwiftPace watch shows erratic heart rate readings, flatlines, or "No HR Signal" during activities.

## Triage Questions
1. Wrist placement: which wrist, how tight, how far from wrist bone?
2. Activity type: running, cycling, weight lifting, swimming?
3. Skin tone and arm hair: optical HR sensors perform differently across skin types.
4. Tattoo on wrist? Ink can interfere with optical sensor.

## Resolution Tree

### "No HR Signal" or Flatline
- **Wrist fit:** Watch must be snug (not tight) ~1 finger width above wrist bone. Too loose = light leaks under sensor. Too far down on wrist = bone interferes.
- **Clean sensor:** Sweat, sunscreen, and dirt on the optical sensor window cause failures. Clean with damp cloth before activity.
- **Tattoo interference:** Dark tattoos (especially solid black) block the optical sensor. This is a known limitation of all optical HR watches. Recommend wearing on non-tattooed wrist or using external chest strap (ANT+/Bluetooth HR straps are compatible).
- **Cold weather:** Blood vessels constrict in cold, reducing signal. Warm up indoors for 5 min before starting outdoor activity, or wear watch over thin base layer sleeve.

### Erratic / Spiking HR (e.g., jumps from 80 to 180 randomly)
- **Cadence lock:** During running, the watch may lock onto arm swing cadence instead of heart rate. This is the #1 cause of erratic HR during running. Fix: tighten watch band slightly, move watch higher on forearm (~2 finger widths above wrist bone).
- **Weight lifting:** Gripping barbells/dumbbells compresses blood vessels and disrupts optical reading. Expected behavior. For accurate HR during lifting, use chest strap.
- **Firmware v3.0.x bug:** HR algorithm had a cadence-lock issue that was significantly improved in v3.1.0. Update firmware.

### HR Reads Consistently Low or High
- Compare with manual pulse check (count beats for 15 sec × 4).
- If watch reads consistently 10+ BPM off from manual check at rest: try other wrist.
- If still off: possible sensor degradation. Escalate to Tier 2 for warranty evaluation.
- Note: optical HR is ±3–5 BPM accurate at rest, ±5–10 BPM during intense activity. This is industry standard, not a defect.

### HR Works at Rest But Fails During Activity
- Almost always a fit issue. Band loosens during activity due to sweat. Tighten one notch before starting.
- Running: arm swing causes watch to shift. Consider a watch strap with a keeper loop (aftermarket accessory).
- Swimming: water between sensor and skin disrupts reading. SwiftPace HR during swimming is best-effort, not guaranteed accurate. Recommend chest strap for swim HR.

## Escalation
Escalate to Tier 2 if: HR consistently fails at rest on clean sensor, snug fit, non-tattooed wrist, after firmware update. Possible optical sensor hardware failure.
