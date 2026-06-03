# Case Pattern: Pairing Fails on iPhone After App Permission Denial

## Pattern Summary
Customers who deny Bluetooth permission during first setup may report repeated pairing failure even after reopening the app.

## Common Customer Phrasing
- "It sees the band but will not complete setup"
- "Pairing starts and then fails"
- "I accidentally clicked Don't Allow"

## Observed Root Cause
Bluetooth permission remains disabled for the app at OS settings level after initial denial.

## Successful Resolution Path (Curated)
1. Confirm customer is on iPhone
2. Ask customer to open iPhone Settings > NorthPeak Health App
3. Re-enable Bluetooth permission
4. Return to app and restart pairing flow
5. If device was partially paired, remove device entry in app and retry

## Notes
Do not guide pairing from iPhone Bluetooth menu directly. Pairing should be completed in the app.

## Confidence
High (repeatedly successful across similar cases)
