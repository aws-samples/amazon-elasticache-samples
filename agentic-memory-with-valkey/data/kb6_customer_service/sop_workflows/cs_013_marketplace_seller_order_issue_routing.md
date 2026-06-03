# SOP: Marketplace Seller Order Issue Routing Workflow

## Purpose
Help agents quickly determine who owns a marketplace order issue — Accent Athletics or the seller — and route accordingly.

## Decision Tree

### Step 1: Is the order from a marketplace seller?
Check order details for seller name. If seller is "Accent Athletics" or blank → direct order, handle normally. If seller name appears → marketplace order, continue below.

### Step 2: Is the seller active or suspended?
| Seller | Status | Action |
|---|---|---|
| RunReady (RRA-US-415) | Active | Continue to Step 3 |
| FitTech (FTS-US-502) | Active | Continue to Step 3 |
| TrailBound (TBO-US-833) | Active | Continue to Step 3 |
| GearVault (GVO-US-618) | Active | Continue to Step 3 |
| StrideZone (SZD-US-101) | Active | Continue to Step 3 |
| FlexWear (FWC-US-204) | Active | Continue to Step 3 |
| ActiveTech (ATM-US-330) | Active | Continue to Step 3 |
| PeakForm (PFA-US-720) | **SUSPENDED** | Skip to counterfeit handling (KB4: vs_020, pc_021) |

### Step 3: What is the issue?
| Issue Category | Who Handles | Reference |
|---|---|---|
| Wrong item shipped | Seller | Redirect to seller support |
| Item damaged in shipping | Seller | Redirect to seller support |
| Item never arrived | Seller (check tracking first) | Redirect to seller support |
| Sizing exchange (footwear/apparel) | Seller | KB4: vs_017 (RunReady), vs_021 (TrailBound) |
| Non-defective return | Seller | Follow seller's return window |
| Firmware / software bug | **Accent Athletics** | KB3 troubleshooting |
| Manufacturing defect (warranty) | **Accent Athletics** | KB5: pc_011 (electronics), pc_012 (footwear/apparel) |
| Product authenticity concern | **Accent Athletics** | KB4: vs_022 verification guide |
| Safety incident | **Accent Athletics** | KB5: pc_017 safety policy, pc_010 intake templates |
| Missing accessories (seller packing error) | Seller | KB4: vs_025 (FitTech cable issue) |
| Third-party accessory issue | Seller | KB4: vs_026 (FitTech screen protector) |
| Previous owner's data on renewed device | **Accent Athletics** (privacy) + GearVault | KB4: vs_027, KB5: pc_018 |

### Step 4: Handoff
- **To seller:** "I've identified that this issue is best handled by [Seller Name], who fulfilled your order. Let me connect you with their support team." Provide seller contact info from partner master (KB4).
- **To Accent Athletics Tier 2:** If issue is complex or spans both seller and product. Include notes on what you've determined.
- **NEVER bounce the customer.** If you're unsure, keep the customer and escalate internally. Do not tell the customer to "call the seller and then call us back."
