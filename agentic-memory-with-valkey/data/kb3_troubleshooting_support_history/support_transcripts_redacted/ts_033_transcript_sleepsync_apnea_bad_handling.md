# Transcript: SleepSync Apnea Alert — Agent Uses Unapproved Messaging

**Channel:** Phone (transcribed)
**Date:** 2026-02-21
**Agent:** [REDACTED]
**Customer:** [REDACTED]
**Product:** SleepSync
**Resolution:** Alert disabled, but customer left anxious
**Quality Score:** 2/5

---

**Customer:** I got a notification from my SleepSync last night saying I might have sleep apnea. I'm really worried. Should I see a doctor?

**Agent:** Oh yeah, we've been getting a lot of calls about that. The device is kind of glitchy right now with the apnea stuff. It's probably nothing, I wouldn't worry about it.

**Customer:** But it said "potential sleep apnea detected." That sounds serious.

**Agent:** Yeah the device isn't really that accurate with the apnea detection. It's a new feature and it doesn't work great. I'd just ignore it honestly.

**Customer:** So should I see a doctor or not? I have a family history of sleep apnea.

**Agent:** I mean, I can't really give medical advice. But like I said, the device is inaccurate so I wouldn't base any decisions on it. Let me just turn off those notifications for you.

**Customer:** Okay... I guess. I'm still kind of worried though.

**Agent:** Yeah I understand. But it's just a software thing. Anything else I can help with?

**Customer:** No, I guess not. Thanks.

---

**QA Notes:** MULTIPLE VIOLATIONS of approved messaging (ts_021):
1. ❌ Said "it's probably nothing" — explicitly listed as what NOT to say
2. ❌ Said "device isn't really that accurate" — should say apnea detection specifically is miscalibrated, not that the device is inaccurate
3. ❌ Said "I wouldn't worry about it" — dismissive of legitimate health concern
4. ❌ Did NOT use the approved response language
5. ❌ Did NOT explain that it's a known software calibration issue with a fix coming
6. ❌ Left customer more anxious than when they called

**Correct approach:** Use exact approved messaging from ts_021. Acknowledge concern, explain it's a known software calibration issue (not device inaccuracy), confirm a fix is coming, disable alerts while keeping other tracking active.
