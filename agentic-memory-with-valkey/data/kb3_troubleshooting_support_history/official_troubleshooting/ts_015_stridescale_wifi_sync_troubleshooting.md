# StrideScale Wi-Fi Sync Failure Troubleshooting

## Symptom
StrideScale shows weight on display but data does not appear in the Accent Athletics Health app. Scale LED may blink red after measurement (indicates sync failure).

## Triage Questions
1. Is the scale connected to Wi-Fi? (App → Devices → StrideScale → should show "Connected" with signal strength)
2. Has anything changed with the home network? (new router, password change, ISP change)
3. Is the app up to date?
4. How far is the scale from the Wi-Fi router?

## Resolution Tree

### Scale Shows "Offline" in App
- Open app → Devices → StrideScale → Reconnect Wi-Fi
- If network changed: re-run Wi-Fi setup (app will prompt for new network/password)
- Power cycle scale: remove one battery, wait 10 sec, reinsert
- Check router: is 2.4 GHz band still active? Some routers disable it after firmware updates.

### Scale Shows "Connected" But Data Doesn't Sync
- Force-close app, reopen, pull down to refresh on dashboard
- Check app permissions: iOS → Settings → Accent Athletics Health → ensure "Local Network" is enabled. Android → App Info → Permissions → ensure "Nearby Devices" is enabled.
- Measure again — sometimes first post-reconnect measurement is lost, second syncs fine.
- If multiple user profiles exist on scale: ensure correct profile is selected in app. Scale auto-detects user by weight range — if two users are similar weight, it may assign to wrong profile.

### Intermittent Sync (Works Sometimes)
- Likely Wi-Fi signal strength issue. Move scale closer to router or add a Wi-Fi extender.
- Check for interference: microwave ovens, baby monitors, and cordless phones on 2.4 GHz can interfere.
- Router channel congestion: if in apartment building, 2.4 GHz channels may be crowded. Customer can try changing router to channel 1, 6, or 11 (least overlap).

### Data Synced But Shows Wrong User
- StrideScale uses weight-range matching. If two household members are within 5 lbs, scale may misassign.
- Fix: App → Devices → StrideScale → User Detection → switch from "Auto" to "Step-On Order" (first person to step on after wake = User 1, etc.)
- Misassigned readings can be reassigned: App → History → tap reading → "Reassign to [other user]"

## Escalation
Escalate to Tier 2 if: Wi-Fi reconnection fails after power cycle + app reinstall + router verification. Include router brand/model if known.
