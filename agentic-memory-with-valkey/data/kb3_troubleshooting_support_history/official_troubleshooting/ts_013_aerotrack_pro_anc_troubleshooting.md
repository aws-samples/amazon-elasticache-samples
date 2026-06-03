# AeroTrack Pro ANC Troubleshooting

## Symptom
Customer reports Active Noise Cancellation (ANC) is not working, sounds weak, or produces a "hissing" or "wind tunnel" effect.

## Triage Questions
1. Which firmware version? (App → Settings → About → Firmware Version)
2. Which ear tips are installed? Did they run the Ear Tip Fit Test?
3. Is ANC enabled? (App → Sound Modes → should show "Active Noise Cancellation" selected)
4. Does the issue affect one or both earbuds?
5. Environment: indoors, outdoors, windy?

## Resolution Tree

### ANC Not Activating
- Verify ANC is toggled ON in app (Sound Modes screen)
- Check firmware is v2.3.0+. ANC was significantly improved in v2.3.0. If on older firmware, update first.
- Factory reset if ANC toggle is grayed out: case button hold 8 sec → re-pair → re-enable ANC

### ANC Sounds Weak
- **Most common cause:** Poor ear tip seal. Run Ear Tip Fit Test. Switch to larger tips if "Poor Seal."
- Check for earwax buildup on mesh grille. Clean with dry soft brush (included in box). Do NOT use water or alcohol.
- If seal is good and grilles are clean: possible hardware defect in ANC microphone. Escalate to Tier 2.

### Hissing / Wind Tunnel Sound
- **Firmware v2.2.x bug:** Known issue — ANC algorithm produced hiss in quiet environments. Fixed in v2.3.0. Update firmware.
- If on v2.3.0+ and still hissing: switch to "Transparency" mode, wait 5 sec, switch back to ANC. This recalibrates.
- Outdoors in wind: ANC mics pick up wind noise. Switch to "Wind Reduction" mode (App → Sound Modes → Wind Reduction). This is expected behavior, not a defect.

### ANC Works in One Ear Only
- Check that both earbuds are connected (App → Device → should show L and R with battery levels)
- Clean ANC mic port on affected earbud (small hole on outer shell)
- If persists after cleaning: hardware defect. Initiate warranty replacement for affected earbud.

## Escalation
Escalate to Tier 2 if: ANC issue persists after firmware update + ear tip refit + factory reset. Include firmware version, fit test results, and environment details in escalation notes.
