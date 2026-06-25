# SwiftPace Running Watch GPS Troubleshooting

## Symptom
SwiftPace Running Watch shows inaccurate GPS tracks, slow GPS lock, or "GPS Searching" that never resolves during outdoor activities.

## Triage Questions
1. Firmware version? (Watch → Settings → About → Software)
2. Where is the issue occurring? (urban canyon, dense forest, open field)
3. How long does GPS lock take? (Normal: <30 sec outdoors with clear sky)
4. Is the watch connected to phone during activity? (Assisted GPS uses phone connection)

## Resolution Tree

### GPS Won't Lock At All
- Ensure Location Services are enabled on watch: Settings → Privacy → Location → On
- Go outdoors with clear sky view. GPS cannot lock indoors or under heavy cover.
- Force GPS refresh: Settings → System → Sensors → GPS → Reset GPS Cache. This clears stale satellite almanac data and forces fresh download. Takes 2–5 min on first lock after reset.
- If watch hasn't been used in >2 weeks: satellite almanac is stale. Connect to Wi-Fi, leave watch outdoors for 10 min to download fresh almanac.

### GPS Locks But Track Is Inaccurate
- **Urban canyons (downtown, tall buildings):** GPS multipath reflection causes zigzag tracks. This is a physics limitation, not a defect. Recommend enabling "Multi-Band GPS" in activity settings (uses L1+L5 bands for better urban accuracy). Available on firmware v3.1.0+.
- **Dense tree cover:** Similar multipath issue. Multi-Band GPS helps. Also try wrist position: wear watch on top of wrist (not underside) for better sky view.
- **Consistent offset (track is 50–100m off):** Likely stale almanac. Reset GPS cache and re-lock.

### GPS Lock Takes >60 Seconds
- Connect watch to phone via Bluetooth before starting activity. Assisted GPS uses phone's approximate location to speed up satellite search.
- Update firmware — each release improves GPS acquisition algorithms.
- Check if "Battery Saver GPS" is enabled (Settings → Activity → GPS Mode). Battery Saver reduces GPS polling to every 60 sec — great for battery, terrible for accuracy. Switch to "Standard" or "Multi-Band" for accurate tracking.

### GPS Drains Battery Too Fast
- Multi-Band GPS uses ~40% more battery than Standard GPS. For long runs (>4 hours), consider Standard GPS mode.
- Battery Saver GPS extends battery to ~30 hours but with reduced accuracy.
- Expected battery life by mode: Multi-Band ~12 hrs, Standard ~20 hrs, Battery Saver ~30 hrs.

## Escalation
Escalate to Tier 2 if: GPS fails to lock outdoors with clear sky after cache reset + firmware update. Possible hardware defect in GPS antenna. Include firmware version and approximate location (city) in escalation.
