# Temporary Workaround for Firmware 4.2.1 Sync Delay
**Related Known Issue:** Sync Delays After Firmware 4.2.1  
**Status:** Active

## Approved Temporary Workaround
Use this sequence before advising reinstall or factory reset:

1. Ask customer to open NorthPeak Health App and keep it in foreground
2. Confirm phone Bluetooth is on
3. Wait 60 seconds on dashboard
4. Pull to refresh once
5. If no sync, restart the band
6. Reopen app and keep phone within 3 feet for 2 minutes
7. Trigger manual sync from device page

## Expected Outcome
Many customers report delayed data appears after the restart + foreground app sync sequence.

## Escalate If
- No data sync after 2 full attempts
- Customer reports sync failure began before firmware 4.2.1
- App displays explicit server error code
- Customer is unable to keep app in foreground due to app crash

## Notes
This workaround is preferred over uninstall/reinstall while known issue remains active.
