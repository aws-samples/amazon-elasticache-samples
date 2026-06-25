import { useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useSettings } from '@/contexts/SettingsContext';
import { useConnection } from '@/contexts/ConnectionContext';


interface Props {
  open: boolean;
  onOpenChange: (o: boolean) => void;
}

export function SettingsDialog({ open, onOpenChange }: Props) {
  const { settings, updateSettings } = useSettings();
  const { connections, updateConnection, activeConnection, setActiveConnection } = useConnection();
  const [dockerEndpoint, setDockerEndpoint] = useState(settings.dockerEndpoint ?? 'localhost');
  const [influxUrl, setInfluxUrl] = useState(settings.influxUrl ?? '');
  const [influxPort, setInfluxPort] = useState<number>(settings.influxPort ?? 8086);
  const [influxToken, setInfluxToken] = useState(settings.influxToken ?? '');
  const [influxBucket, setInfluxBucket] = useState(settings.influxBucket ?? '');
  const [influxOrg, setInfluxOrg] = useState(settings.influxOrg ?? '');

  const onSave = async () => {
    // Update global settings
    const newPort = Number(influxPort) || 8086;
    updateSettings({ dockerEndpoint, influxUrl, influxPort: newPort, influxToken, influxBucket, influxOrg });

    // Also update all existing connections to reflect the latest Influx settings
    try {
      // Pull over all Influx fields from settings/dialog state
      // including URL/port and secrets
      const updates = {
        influxEndpointUrl: influxUrl,
        influxPort: newPort,
        influxToken,
        influxBucket,
        influxOrg,
      } as const;

      // Use updateConnection for each existing connection
      // so they persist via localStorage through ConnectionContext
      // (no-op if there are no connections yet)
      let updatedActive: typeof connections[number] | null = null;
      connections.forEach((conn) => {
        const merged = { ...conn, ...updates };
        updateConnection(merged);
        if (activeConnection && conn.id === activeConnection.id) {
          updatedActive = merged;
        }
      });

      // If there is an active connection, reconnect it to apply new settings
      if (updatedActive) {
        try {
          await setActiveConnection(updatedActive);
        } catch (err) {
          console.warn('Reconnecting active connection failed after saving settings:', err);
        }
      }
    } catch (e) {
      // Best-effort update; ignore errors here
      console.warn('Failed to propagate settings to connections:', e);
    }

    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
          <DialogDescription>Configure Docker and InfluxDB settings</DialogDescription>
        </DialogHeader>

        <div className="space-y-6">
          <div>
            <h3 className="text-md font-semibold mb-2">Docker</h3>
            <div className="grid grid-cols-4 items-center gap-4">
              <Label htmlFor="dockerEndpoint" className="text-right">Docker Endpoint</Label>
              <div className="col-span-3 flex gap-2">
                <Input id="dockerEndpoint" className="flex-1" value={dockerEndpoint} onChange={(e) => setDockerEndpoint(e.target.value)} placeholder="localhost" />
                <Button
                  type="button"
                  variant="secondary"
                  title="Use the current browser host"
                  aria-label="Populate from Session"
                  onClick={() => {
                    try {
                      const host = typeof window !== 'undefined' ? window.location.hostname : '';
                      if (host) setDockerEndpoint(host);
                    } catch {
                      /* no-op */
                    }
                  }}
                >
                  Populate from Session
                </Button>
              </div>
            </div>
          </div>

          <div>
            <h3 className="text-md font-semibold mb-2">InfluxDB</h3>
            <div className="grid grid-cols-4 items-center gap-4 mb-3">
              <Label htmlFor="influxUrl" className="text-right">InfluxDB host</Label>
              <Input id="influxUrl" className="col-span-3" value={influxUrl} onChange={(e) => setInfluxUrl(e.target.value)} placeholder="<instance_ID>.<region>.timestream-influxdb.amazonaws.com" />
            </div>
            <div className="grid grid-cols-4 items-center gap-4">
              <Label htmlFor="influxPort" className="text-right">InfluxDB Port</Label>
              <Input id="influxPort" type="number" className="col-span-3" value={influxPort} onChange={(e) => setInfluxPort(parseInt(e.target.value || '8086'))} placeholder="8086" />
            </div>
            <div className="grid grid-cols-4 items-center gap-4 mt-3">
              <Label htmlFor="influxToken" className="text-right">Token</Label>
              <Input id="influxToken" type="password" className="col-span-3" value={influxToken} onChange={(e) => setInfluxToken(e.target.value)} placeholder="••••••••" />
            </div>
            <div className="grid grid-cols-4 items-center gap-4 mt-3">
              <Label htmlFor="influxBucket" className="text-right">Bucket</Label>
              <Input id="influxBucket" className="col-span-3" value={influxBucket} onChange={(e) => setInfluxBucket(e.target.value)} placeholder="my-bucket" />
            </div>
            <div className="grid grid-cols-4 items-center gap-4 mt-3">
              <Label htmlFor="influxOrg" className="text-right">Organization</Label>
              <Input id="influxOrg" className="col-span-3" value={influxOrg} onChange={(e) => setInfluxOrg(e.target.value)} placeholder="my-org" />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button onClick={onSave}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
