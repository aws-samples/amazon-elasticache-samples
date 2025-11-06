import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { RefreshCw, Search, Users, Database, Activity, HardDrive } from 'lucide-react';

interface ClientConnection {
  id: string;
  addr: string;
  laddr: string;
  fd: string;
  name: string;
  age: string;
  idle: string;
  flags: string;
  db: string;
  sub: string;
  psub: string;
  ssub: string;
  multi: string;
  watch: string;
  qbuf: string;
  "qbuf-free": string;
  "argv-mem": string;
  "multi-mem": string;
  rbs: string;
  rbp: string;
  obl: string;
  oll: string;
  omem: string;
  "tot-mem": string;
  events: string;
  cmd: string;
  user: string;
  redir: string;
  resp: string;
  "lib-name": string;
  "lib-ver": string;
  "tot-net-in": string;
  "tot-net-out": string;
  "tot-cmds": string;
}

interface ConnectionsModalProps {
  isOpen: boolean;
  onClose: () => void;
  connections: ClientConnection[] | undefined;
  totalConnections: number;
  onRefresh: () => void;
  isLoading?: boolean;
}

export function ConnectionsModal({
  isOpen,
  onClose,
  connections,
  totalConnections,
  onRefresh,
  isLoading = false
}: ConnectionsModalProps) {
  const [searchQuery, setSearchQuery] = useState('');

  const filteredConnections = connections?.filter(connection => 
    connection.addr.toLowerCase().includes(searchQuery.toLowerCase()) ||
    connection.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    connection.cmd.toLowerCase().includes(searchQuery.toLowerCase()) ||
    connection.user.toLowerCase().includes(searchQuery.toLowerCase())
  ) || [];

  const formatBytes = (bytes: string) => {
    const num = parseInt(bytes);
    if (!num || num === 0) return '0 B';
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(num) / Math.log(1024));
    return `${(num / Math.pow(1024, i)).toFixed(1)} ${sizes[i]}`;
  };

  const formatDuration = (seconds: string) => {
    const num = parseInt(seconds);
    if (!num) return '0s';
    
    const hours = Math.floor(num / 3600);
    const minutes = Math.floor((num % 3600) / 60);
    const secs = num % 60;
    
    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent
        className="max-w-[90vw] max-h-[80vh] overflow-hidden resize-x"
        style={{ width: 'min(95vw, 1600px)', height: 'min(85vh, 900px)', minWidth: '640px', minHeight: '300px' }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Active Connections ({totalConnections})
          </DialogTitle>
          <DialogDescription>
            Detailed view of all active client connections to your Valkey instance
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Controls */}
          <div className="flex items-center gap-4">
            <div className="relative flex-1 max-w-sm">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search connections by IP, name, command, or user..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-10"
              />
            </div>
            <Button variant="outline" onClick={onRefresh} disabled={isLoading}>
              <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
              Refresh
            </Button>
          </div>

          {/* Summary Stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="flex items-center gap-2 p-3 bg-muted/50 rounded-lg">
              <Users className="h-4 w-4 text-blue-500" />
              <div>
                <p className="text-sm font-medium">{totalConnections}</p>
                <p className="text-xs text-muted-foreground">Total Active</p>
              </div>
            </div>
            <div className="flex items-center gap-2 p-3 bg-muted/50 rounded-lg">
              <Database className="h-4 w-4 text-green-500" />
              <div>
                <p className="text-sm font-medium">
                  {[...new Set(connections?.map(c => c.db) || [])].length}
                </p>
                <p className="text-xs text-muted-foreground">Databases</p>
              </div>
            </div>
            <div className="flex items-center gap-2 p-3 bg-muted/50 rounded-lg">
              <Activity className="h-4 w-4 text-orange-500" />
              <div>
                <p className="text-sm font-medium">
                  {connections?.filter(c => c.cmd && c.cmd !== 'NULL').length || 0}
                </p>
                <p className="text-xs text-muted-foreground">Active Commands</p>
              </div>
            </div>
            <div className="flex items-center gap-2 p-3 bg-muted/50 rounded-lg">
              <HardDrive className="h-4 w-4 text-purple-500" />
              <div>
                <p className="text-sm font-medium">
                  {formatBytes(
                    connections?.reduce((sum, c) => sum + parseInt(c["tot-mem"] || "0"), 0).toString() || "0"
                  )}
                </p>
                <p className="text-xs text-muted-foreground">Total Memory</p>
              </div>
            </div>
          </div>

          {/* Connections Table */}
          <div className="border rounded-lg overflow-hidden">
            <div className="max-h-96 overflow-auto">
              <Table>
                <TableHeader className="sticky top-0 bg-background">
                  <TableRow>
                    <TableHead>Client</TableHead>
                    <TableHead>Address</TableHead>
                    <TableHead>Database</TableHead>
                    <TableHead>Age</TableHead>
                    <TableHead>Idle</TableHead>
                    <TableHead>Command</TableHead>
                    <TableHead>Memory</TableHead>
                    <TableHead>User</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredConnections.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={8} className="text-center py-8 text-muted-foreground">
                        {searchQuery ? 'No connections match your search' : 'No connection details available'}
                      </TableCell>
                    </TableRow>
                  ) : (
                    filteredConnections.map((connection) => (
                      <TableRow key={connection.id}>
                        <TableCell className="font-mono text-xs">
                          {connection.id}
                          {connection.name && connection.name !== 'null' && (
                            <div className="text-muted-foreground">{connection.name}</div>
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {connection.addr}
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className="text-xs">
                            db{connection.db}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {formatDuration(connection.age)}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {formatDuration(connection.idle)}
                        </TableCell>
                        <TableCell>
                          {connection.cmd && connection.cmd !== 'NULL' ? (
                            <Badge variant="secondary" className="text-xs">
                              {connection.cmd}
                            </Badge>
                          ) : (
                            <span className="text-muted-foreground text-xs">idle</span>
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {formatBytes(connection["tot-mem"])}
                        </TableCell>
                        <TableCell className="text-xs">
                          {connection.user && connection.user !== 'default' ? (
                            <Badge variant="outline" className="text-xs">
                              {connection.user}
                            </Badge>
                          ) : (
                            <span className="text-muted-foreground">default</span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
          </div>

          {searchQuery && (
            <p className="text-sm text-muted-foreground">
              Showing {filteredConnections.length} of {connections?.length || 0} connections
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
