# Macro Library: Delivery Delay and Tracking Responses (US)
**Macro Pack Version:** MAC-SHIP-US-v2.4

## Purpose
Provide reusable messaging for delivery delays, tracking confusion, and carrier movement questions.

---

## Macro: DELAY_TRACKING_CHECK_01
**Use when:** Customer reports delayed package but tracking still shows movement  
**Template:**
Thanks for reaching out. I checked the shipment status, and it appears the package is still moving through the carrier network. Delivery timing can shift due to carrier processing or regional conditions. I can help review the latest tracking details with you and confirm next steps if the status stops updating.

---

## Macro: DELAY_WEATHER_ACK_02
**Use when:** Regional weather disruption is active and in scope  
**Template:**
Thanks for your patience. Deliveries to your area may be delayed due to severe weather and carrier disruptions. I can review your tracking details and help explain the latest status. If your order becomes eligible for additional options, I can help with those next steps too.

**Agent note:** Check active temporary shipping exception before use.

---

## Macro: DELAY_NO_GUARANTEE_03
**Use when:** Customer requests guaranteed revised delivery date without carrier commitment  
**Template:**
I understand the urgency. I can share the latest tracking information and estimated timing shown by the carrier, but I cannot guarantee a revised delivery date unless the carrier has confirmed one.

---

## Macro Selection Guidance
- Do not use weather macro without active in-scope disruption guidance
- Personalize with order status, but avoid promising exact dates
- Pair with policy or incident guidance when escalation thresholds are discussed
