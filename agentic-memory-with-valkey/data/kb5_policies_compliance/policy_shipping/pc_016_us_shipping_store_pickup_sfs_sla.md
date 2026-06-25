# US Shipping Policy: Store Pickup and Ship-From-Store SLA
**Policy Version:** SHIP-SFS-US-v1.5
**Region:** US
**Status:** Approved
**Effective:** 2026-01-01 to 2026-12-31
**Supplements:** SHIP-US-v3.2 (pc_001)

## Store Pickup SLA
| Metric | Target | Measurement |
|---|---|---|
| Order ready notification | Within 2 hours of order placement (during store hours) | Time from order to "Ready for Pickup" email/SMS |
| Hold duration | 48 hours from notification | Auto-cancel after 72 hours (1 reminder at 48 hrs) |
| Hold extension — high value (>$200) | 5 business days with manager approval | Per KB2: so_009 inventory playbook |
| Hold extension — store closure | Duration of closure + 24 hours | Per KB2: so_010 emergency closure playbook |

## Ship-From-Store SLA
| Metric | Target |
|---|---|
| Packing SLA | Same business day if order received before 2 PM local time |
| Carrier handoff | By end of business day |
| Delivery estimate | 2–5 business days depending on distance |

## Store-Specific Overrides
Standard SLAs may be overridden by store-specific notes (KB2) or temporary advisories:
- **Denver Cherry Creek (DEN-021):** Currently CLOSED due to pipe burst (KB2: so_037). All pickups transferred to DEN-045. Ship-from-store rerouted.
- **Chicago Michigan Ave (CHI-001):** Elevator outage (KB2: so_032) does not affect pickup SLA but may slow packing for upper-floor inventory.
- **Portland Pearl District (PDX-008):** Weather advisory expired (KB2: so_005). Normal SLA resumed.
- **Any store under emergency closure (KB2: so_010):** Hold timers auto-extend. Ship-from-store reroutes to nearest eligible store.

## Marketplace Seller Orders
- Store pickup is NOT available for marketplace seller orders (RunReady, FitTech, TrailBound, GearVault).
- Marketplace orders are seller-fulfilled and ship from seller warehouses.
- If customer asks "can I pick up my FitTech order at a store?": No. FitTech ships from Austin, TX. Provide tracking info instead.

## Severe Weather Exception
During active weather exceptions (e.g., pc_007 US-NW weather delay):
- Pickup SLA extends by 24 hours
- Ship-from-store SLA extends by 48 hours
- Customer communication: "Due to severe weather in your area, your order may be delayed by 1–2 business days. We appreciate your patience."
