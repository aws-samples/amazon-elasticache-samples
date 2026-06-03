# Temporary Policy Exception: Denver Cherry Creek Store Closure — Order Rerouting
**Exception ID:** TEMP-STORE-DEN021-CLOSURE-2026-02
**Region:** US-CO
**Status:** Active
**Effective:** 2026-02-23 to 2026-03-07
**Overrides:** SHIP-SFS-US-v1.5 (pc_016)
**Exception Priority:** 1

## Exception
Denver Cherry Creek store (DEN-021) is closed due to pipe burst (KB2: so_037). The following temporary exceptions apply:

### Pickup Orders
- All pending pickups for DEN-021 have been transferred to **DEN-045 (Park Meadows)**, 12 miles south.
- Customers were notified via email/SMS with new pickup location.
- Hold timers extended by duration of closure (no auto-cancel).
- If customer cannot travel to DEN-045: offer free shipping to their address instead. Use exception code DEN021-SHIP.

### Ship-From-Store
- DEN-021 removed from ship-from-store pool.
- Orders rerouted to DEN-045 or nearest eligible store.

### New Online Orders
- DEN-021 suppressed from pickup options on website/app.
- Will be re-enabled when store reopens (estimated Mar 1–7 per KB2: so_037).

### Staff
- DEN-021 staff temporarily reassigned to DEN-045 and BLD-001 (Boulder).
- If customer calls DEN-021 store phone: IVR redirects to DEN-045.

## Interaction with Other Policies
- Severe weather exception (pc_007) is for shipping delays, not store closures. Different exception.
- Standard emergency closure playbook (KB2: so_010) governs the operational process. This policy exception governs the customer-facing SLA overrides.
- Inventory hold policy (KB2: so_009) hold timers are extended per this exception.
