# Transcript: StrideScale Weight Discrepancy — Agent Gives Incorrect Carpet Advice

**Channel:** Chat
**Date:** 2026-02-18
**Agent:** [REDACTED]
**Customer:** [REDACTED]
**Product:** StrideScale
**Resolution:** Partial — customer still confused
**Quality Score:** 2/5

---

**Customer:** My StrideScale says I weigh 158 but my gym scale says 153. Which one is right?

**Agent:** Hi! Scales can vary a little bit, that's normal. A 5 lb difference is within the expected range for different scales.

**Customer:** 5 lbs seems like a lot though. Is my scale broken?

**Agent:** Probably not. Are you weighing at the same time of day? Your weight can change throughout the day.

**Customer:** I weigh at home in the morning and at the gym in the afternoon, so yeah different times. But still 5 lbs?

**Agent:** That could account for some of it. Also make sure you're standing still on the scale and your feet are centered.

**Customer:** I am. The scale is in my bedroom on the carpet. Could that matter?

**Agent:** Carpet shouldn't make a big difference. The scale adjusts for different surfaces.

**Customer:** Ok so it's just the time of day thing?

**Agent:** Most likely yes. Try weighing at the gym in the morning and comparing. If it's still off by a lot, let us know and we can look into a replacement.

---

**QA Notes:** CRITICAL ERROR:
1. ❌ **Agent said carpet "shouldn't make a big difference."** This is WRONG. StrideScale on carpet can read up to 5 lbs inaccurate. This was almost certainly the primary cause and the agent missed it entirely. See ts_027 and ts_014.
2. ❌ Did not ask the surface question proactively — customer volunteered it and agent dismissed it.
3. ❌ Said "5 lb difference is within expected range" — it is NOT within spec for the same person. ±0.5 lb is spec.
4. ⚠️ Time-of-day advice was correct but insufficient as the sole explanation.

**Correct approach:** Immediately identify carpet as the likely cause. Explain hard floor requirement. Have customer move scale to hard floor and re-test before considering replacement.
