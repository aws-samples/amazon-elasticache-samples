# Postmortem: PeakForm Counterfeit Detection Delay

**Incident ID:** INC-MKT-PEAKFORM-SUSPEND-2026-02
**Postmortem Date:** 2026-02-24
**Author:** Marketplace Trust & Safety Team

## Timeline
- **2025-08-01:** PeakForm onboarded as marketplace seller. Standard vetting completed.
- **2025-08-15 – 2025-12-31:** PeakForm sells ~800 orders. Low complaint rate. No red flags.
- **2026-01-05:** First authenticity complaint. Customer says AeroFlex Runner 2 "feels different from the pair I bought at the store." Treated as isolated complaint.
- **2026-01-12 – 2026-01-28:** 12 more authenticity complaints. Pattern not yet identified — complaints spread across different agents.
- **2026-02-01:** Marketplace Quality team runs monthly seller review. PeakForm flagged for elevated complaint rate (4.2% vs 2% threshold).
- **2026-02-05:** Brand Protection team purchases 5 sample products from PeakForm for inspection.
- **2026-02-10:** Safety incident — counterfeit SwiftPace overheats during charging (KB4: vs_029).
- **2026-02-12:** Brand Protection confirms all 5 samples are counterfeit. CPSC report filed for electronics.
- **2026-02-15:** PeakForm suspended. Listings removed. Refund program initiated.
- **2026-02-18:** Proactive email sent to ~1,200 PeakForm customers.

## Root Cause
PeakForm initially sold legitimate products (likely purchased at retail and resold). They gradually shifted to counterfeit products starting ~Nov 2025. The transition was gradual enough to avoid triggering automated quality alerts.

## What Went Wrong
1. **Onboarding vetting was insufficient.** Standard vetting checks business registration and tax ID but does not verify product supply chain.
2. **Complaint pattern detection was too slow.** 13 authenticity complaints over 3 weeks were handled by different agents and not correlated. No automated pattern detection for "authenticity" keyword in complaints.
3. **Monthly review cycle was too infrequent.** PeakForm's complaint rate crossed the threshold in January but wasn't flagged until the Feb 1 monthly review.
4. **Price anomaly not flagged.** PeakForm's SwiftPace at $89.99 (vs $249.99 retail) should have been an immediate red flag. No automated price anomaly detection exists.

## Action Items
1. ✅ **Implement real-time complaint keyword monitoring** for "fake," "counterfeit," "not genuine," "authenticity" — auto-flag seller when 3+ complaints contain these keywords in any 30-day window. (Completed Feb 20)
2. ✅ **Add price anomaly detection** — flag any listing priced >40% below MAP. (Completed Feb 22)
3. 🔄 **Move to weekly seller reviews** for Medium and Low trust tier sellers. (In progress, target Mar 1)
4. 🔄 **Enhanced onboarding** — require supply chain documentation for electronics sellers. (In progress, target Mar 15)
5. ⬜ **Retroactive review** of all current sellers for price anomalies. (Planned, target Mar 31)

## Customer Impact Summary
- ~1,200 customers affected
- 1 safety incident (no injury)
- Estimated refund cost: $89,000
- Brand reputation impact: moderate (contained by fast response after detection)

## Lessons for Support Agents
- If you see multiple complaints about a seller that mention product quality or authenticity, **flag it immediately** to Marketplace Quality team. Don't assume someone else has noticed.
- The new keyword monitoring will help, but human pattern recognition is still valuable.
