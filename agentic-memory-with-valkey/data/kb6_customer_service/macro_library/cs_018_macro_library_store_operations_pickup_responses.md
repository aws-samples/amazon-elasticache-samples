# Macro Library: Store Operations and Pickup Responses

**Macro Pack Version:** MAC-STORE-v1.0
**Status:** Approved
**Last Updated:** 2026-02-24

---

### MACRO: Store Closed — Order Rerouted
**ID:** STORE-001
**Channel:** Chat / Email

> Hi [Customer Name],
>
> I see your pickup order was originally scheduled for our [Store Name] location, which is temporarily closed. Your order has been transferred to [Alternate Store Name] at [Address].
>
> If that location doesn't work for you, I can arrange free shipping to your address instead. Just let me know your preference!
>
> [Agent Name]

**Active closures:** Denver DEN-021 → rerouted to DEN-045 (KB2: so_037, KB5: pc_022)

---

### MACRO: Curbside Not Available at This Store
**ID:** STORE-002
**Channel:** Chat / Email

> Hi [Customer Name],
>
> I apologize for the confusion. Our [Store Name] location doesn't currently offer curbside pickup due to [reason — mall policy / no loading zone / weather suspension]. All pickups at this location are in-store.
>
> If you need assistance getting your order to your car, our store team is happy to help — just ask at the pickup desk!
>
> [Agent Name]

---

### MACRO: Pickup Hold Expired — Reorder Offer
**ID:** STORE-003
**Channel:** Chat / Email

> Hi [Customer Name],
>
> I'm sorry — it looks like your pickup order at [Store Name] was automatically cancelled because it wasn't picked up within our 72-hour hold window. I understand things come up!
>
> I'd be happy to help you reorder the same item with free expedited shipping to your address, or I can set up a new pickup order at the same store. Which would you prefer?
>
> [Agent Name]

---

### MACRO: Weather Delay — Store Pickup
**ID:** STORE-004
**Channel:** Chat / Email

> Hi [Customer Name],
>
> Due to severe weather in the [Region] area, your pickup order at [Store Name] may be delayed. We've extended the hold time on your order so it won't be cancelled while conditions improve.
>
> We'll send you an updated notification as soon as your order is ready. If you'd prefer, I can switch this to free home delivery instead.
>
> [Agent Name]

**Active weather exceptions:** US-NW through Feb 28 (KB5: pc_007)
