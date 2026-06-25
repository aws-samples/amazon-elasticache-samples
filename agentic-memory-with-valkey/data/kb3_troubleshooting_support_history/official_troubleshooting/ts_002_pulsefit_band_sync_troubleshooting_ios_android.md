# PulseFit Band Sync Troubleshooting (iOS/Android)

## Symptom
Customer reports one or more of the following:
- Steps or workouts not syncing
- App shows stale data
- Sync spins indefinitely
- Band is connected but recent activity is missing

## Scope
Applies to PulseFit Band 4 and PulseFit Band 4 Pro using NorthPeak Health App.

## Troubleshooting Flow

### Step 1: Check current known issues
Before deep troubleshooting, review active known-issue bulletins for:
- firmware version-specific sync delays
- app release incidents
- backend sync service degradation

### Step 2: Confirm device state
- Band battery above 10%
- Bluetooth enabled
- App logged in to correct account
- Device shown as connected in app

### Step 3: Basic sync recovery
1. Pull down to refresh in app dashboard
2. Keep app open for 60-90 seconds
3. Toggle Bluetooth off/on
4. Force-close and reopen app
5. Reboot phone
6. Restart band

### Step 4: Re-auth / reconnect (if safe to do)
- Remove device from app (only if customer confirms recent data already synced or understands risk of unsynced data delay)
- Re-pair through app flow (not phone settings)
- Trigger manual sync

### Step 5: Escalation conditions
Escalate if:
- Repeated sync failure persists after re-auth
- Customer reports issue started immediately after firmware update
- Multiple customers report same issue in short window
- App shows server sync error code or outage notice

## Notes
When active known-issue bulletin exists, follow bulletin-specific workaround before using full reinstall/re-pair steps.
