# Transcript: AeroTrack Pro Promised Free Replacement Using Deprecated Program

**Channel:** Phone (transcribed)
**Date:** 2026-02-19
**Agent:** [REDACTED]
**Customer:** [REDACTED]
**Product:** AeroTrack Pro (serial AT24Q1-08842)
**Resolution:** INCORRECT — agent promised free replacement under closed program
**Quality Score:** 1/5

---

**Customer:** My AeroTrack Pro charging case stopped working. It won't charge at all. I read online that there was a free replacement program?

**Agent:** Yes! Let me check your serial number. What is it?

**Customer:** AT24Q1-08842.

**Agent:** Yep, that's an affected serial number. I'll set up a free replacement case for you. Should arrive in 5–7 business days.

**Customer:** Great, thank you!

**Agent:** You're welcome! Is there anything else?

**Customer:** Nope, that's all. Thanks!

---

**QA Notes:** CRITICAL ERROR — POLICY VIOLATION:
1. ❌ Agent referenced the **deprecated** free replacement program (ts_024) which ended January 2026.
2. ❌ The free replacement program is CLOSED. Agent should have checked ts_023 for current options.
3. ❌ Customer's unit is out of standard warranty (Q1 2024 purchase, now Feb 2026 = ~2 years old, warranty is 1 year).
4. ❌ Correct action: offer replacement case at 50% discount ($24.99) per ts_023.
5. ❌ Agent likely found the deprecated FAQ (ts_024) in the knowledge base and followed it without checking the deprecation notice.

**Impact:** Customer was promised a free replacement that will need to be honored (can't retract a promise), but this sets a bad precedent and costs the company $49.99 per incorrect promise. This case has been flagged for coaching.

**Lesson for retrieval systems:** This is exactly why deprecated documents must be clearly deprioritized or filtered. The agent's knowledge base search returned the deprecated FAQ before the current resolved-issue note.
