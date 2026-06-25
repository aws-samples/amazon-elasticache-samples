# Transcript: SwiftPace GPS Issue — Agent Recommends Deprecated Figure-8 Calibration

**Channel:** Chat
**Date:** 2026-02-20
**Agent:** [REDACTED]
**Customer:** [REDACTED]
**Product:** SwiftPace Running Watch
**Resolution:** Incorrect advice given, issue unresolved
**Quality Score:** 1/5

---

**Customer:** My SwiftPace GPS is all over the place. My run this morning showed 4.2 miles but my usual route is exactly 3.1 miles (it's a measured 5K course).

**Agent:** That does sound off! Have you tried calibrating the GPS? Go to Settings → Sensors → GPS → Calibrate, then walk in a figure-8 pattern for about 30 seconds.

**Customer:** I don't see a Calibrate option under GPS. I see Reset GPS Cache, GPS Mode, and About.

**Agent:** Hmm, it might be under a different menu. What firmware version are you on?

**Customer:** v3.1.2

**Agent:** Let me check... ok try Settings → System → Sensors instead of just Settings → Sensors.

**Customer:** There's no System menu either. I think my menus are different from what you're looking at.

**Agent:** That's strange. Let me put you on hold for a moment while I check with my team.

[5 minute hold]

**Agent:** Ok I'm back. It seems like the calibration option might have been removed in a recent update. Let me try something else — can you try resetting the GPS cache? That's under Settings → Sensors → GPS → Reset GPS Cache.

**Customer:** Ok doing that now. It says "GPS cache cleared." Should I go test it?

**Agent:** Yes, try a short outdoor walk and see if the tracking improves.

**Customer:** Ok I'll try tomorrow. Thanks.

---

**QA Notes:** ERRORS:
1. ❌ Agent recommended the **deprecated figure-8 GPS calibration** (ts_025) which was removed in firmware v3.0.0. Wasted 5+ minutes on a non-existent feature.
2. ⚠️ Agent eventually found the correct action (Reset GPS Cache) but only after confusing the customer.
3. ❌ Did not ask about environment (urban canyon, tree cover) which is the most common cause of GPS inaccuracy per ts_017.
4. ❌ Did not mention Multi-Band GPS mode which would likely fix the urban accuracy issue.
5. ❌ A 1.1-mile error on a 3.1-mile route (35% off) is severe — should have investigated more thoroughly.

**Correct approach:** Follow ts_017 troubleshooting tree. Ask about environment, check GPS mode setting, recommend Multi-Band GPS, reset cache as secondary step.
