# SOP: Store Pickup Issue Resolution Workflow

## Purpose
Guide agents through resolving customer issues with store pickup orders, including delays, store closures, and rerouting.

## Common Scenarios

### Scenario 1: "My order says ready but the store says they can't find it"
1. Verify order status in system — confirm "Ready for Pickup" notification was sent.
2. Ask customer which store. Check KB2 for store-specific notes.
3. If store is under advisory (construction, closure, etc.): order may have been rerouted. Check rerouting exceptions in KB5.
4. If no advisory: likely a store-side inventory issue. Contact store directly via internal line. Do NOT make customer call the store.
5. If item truly lost: offer to reship to customer's address (free) or cancel with full refund.

### Scenario 2: "The store is closed, I have a pickup order"
Check KB2 and KB5 for active closures:
- **Denver DEN-021:** Closed (pipe burst, KB2: so_037). Orders rerouted to DEN-045 Park Meadows (KB5: pc_022). Offer free shipping alternative.
- **Any store under emergency closure (KB2: so_010):** Hold timers extended. Customer notified of reopening.
- **Weather closure:** Check KB2 temporary advisories for the store's region.

### Scenario 3: "I missed my pickup window"
- Standard hold: 48 hours, reminder at 48, auto-cancel at 72 (KB5: pc_016, KB2: so_009).
- If order was auto-cancelled: offer to reorder with free expedited shipping.
- If within grace period (48–72 hours): contact store to see if item is still available.
- High-value items (>$200): 5-day hold with manager approval — check if this was applied.

### Scenario 4: "Curbside isn't working at my store"
Check KB2 store notes for curbside availability:
- Some stores don't offer curbside: NYC-001 SoHo (no loading zone), LAX-011 Beverly Center (mall policy), HOU-002 Galleria (mall policy), all Mall of America, NorthPark, Brickell, Somerset, SouthPark, Fashion Show Mall.
- Some stores have conditional curbside: PHX-004 Scottsdale (suspended during extreme heat), CHI-001 Michigan Ave (suspended during wind chill below -10°F).
- If curbside is supposed to be available but isn't working: escalate to store operations.

### Scenario 5: "I want to pick up at a different store"
- Inter-store transfer possible via StoreLink (KB2: so_009). SLA: shipped within 1 business day, received within 3.
- Footwear transfers require size verification photo.
- Cannot transfer final-sale or clearance items.
- If customer wants to pick up a marketplace seller order at a store: NOT possible. Marketplace orders are seller-fulfilled (KB5: pc_016).
