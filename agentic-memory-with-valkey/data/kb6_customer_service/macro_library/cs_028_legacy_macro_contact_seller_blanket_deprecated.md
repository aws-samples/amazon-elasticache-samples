# Legacy Macro: "Contact the Seller" Blanket Response (Deprecated)
**Status:** Deprecated

> **Status: DEPRECATED** | Last updated: 2026-01-15
> **Superseded by:** cs_013 (Marketplace Seller Order Issue Routing) and cs_017 (Marketplace Macros)

## Original Macro (Do Not Use)

> "Hi [Customer Name], I see your order was fulfilled by a marketplace seller. For any issues with marketplace orders, please contact the seller directly. We're unable to process returns, exchanges, or troubleshooting for marketplace seller orders. Here is the seller's contact information: [Seller Info]. Thank you for your understanding!"

## Why Deprecated
This blanket redirect was the #1 source of customer complaints about marketplace support in 2025. It incorrectly told customers that Accent Athletics couldn't help with ANY marketplace issue, when in fact:
- Firmware/software issues → Accent Athletics handles (KB3)
- Manufacturing defects under warranty → Accent Athletics handles (KB5: pc_011, pc_012)
- Counterfeit products → Accent Athletics handles (KB4: vs_020, KB5: pc_021)
- Safety incidents → Accent Athletics handles (KB5: pc_017)

The only issues that should go to the seller are: wrong item, shipping damage, sizing exchanges, and non-defective returns.

## Impact of Using This Deprecated Macro
- Customer gets bounced to seller for a firmware bug → seller can't help → customer calls back angry → double handle time
- Customer with defective product told to "contact seller" → seller says "contact manufacturer" → customer is stuck in a loop
- Customer with counterfeit product told to "contact seller" → seller is suspended and unreachable

## Current Approach
Use cs_013 (routing workflow) to determine who handles the issue, then use the appropriate macro from cs_017 (marketplace macros) or cs_016 (electronics known issues).
