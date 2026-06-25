# Inventory Hold & Transfer Playbook (US)

## Purpose
Defines how stores handle inventory holds for online orders, inter-store transfers, and ship-from-store fulfillment. Baseline for all US locations.

## Hold Policy
- Online orders: inventory held for **48 hours** from "Ready for Pickup" notification.
- After 48 hours: one reminder sent. After 72 hours: order auto-cancelled, inventory released.
- Exception: items over $200 held for 5 business days with manager approval.

## Inter-Store Transfer
- Requested via StoreLink system. Target SLA: shipped within 1 business day, received within 3.
- Footwear transfers require size verification photo uploaded to StoreLink before dispatch.
- Apparel transfers: standard packaging. No transfers for final-sale or clearance items.

## Ship-From-Store
- Eligible stores flagged in StoreLink. Currently ~60% of US locations.
- Packing SLA: same business day if order received before 2 PM local time.
- Carrier: regional default (UPS for East/Central, FedEx for West). Override via StoreLink if needed.

## Escalation
- Transfer delays > 5 business days: escalate to Regional Ops Manager via ticket (category: STORE-TRANSFER-DELAY).
- Missing inventory on arrival: file shrinkage report + notify customer with replacement or refund options.
