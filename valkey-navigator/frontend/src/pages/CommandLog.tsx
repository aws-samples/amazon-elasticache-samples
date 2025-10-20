import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Trash2, RefreshCw, AlertTriangle, Search, Filter, Info, CheckCircle } from 'lucide-react';
import { valkeyApi } from '@/services/valkeyApi';
import type { CommandLogResponse, CommandLogType, CommandLogTypeConfig } from '@/types';

export function CommandLog() {
  const [commandLogData, setCommandLogData] = useState<CommandLogResponse>({ 
    entries: [], 
    count: 0, 
    requested_count: 0,
    timestamp: '',
    log_type: 'slow',
    command_used: '',
    fallback_used: false,
    version_supported: true
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshInterval, setRefreshInterval] = useState<number>(5000);
  const [searchTerm, setSearchTerm] = useState('');
  const [durationFilter, setDurationFilter] = useState<string>('all');
  const [maxEntries, setMaxEntries] = useState<number | 'all'>(100);
  const [selectedLogType, setSelectedLogType] = useState<CommandLogType>('slow');
  const [logTypeSupport, setLogTypeSupport] = useState<{[key in CommandLogType]: boolean}>({
    'slow': true,
    'large-request': true, // Will be updated after first API call
    'large-reply': true   // Will be updated after first API call
  });

  // Log type configurations
  const logTypeConfigs: CommandLogTypeConfig[] = [
    {
      type: 'slow',
      label: 'Slow Commands',
      description: 'Commands that exceeded the slowlog threshold',
      minVersion: 'Any',
      supported: true
    },
    {
      type: 'large-request',
      label: 'Large Requests',
      description: 'Commands with large request payloads',
      minVersion: 'Valkey 8.1+',
      supported: logTypeSupport['large-request']
    },
    {
      type: 'large-reply',
      label: 'Large Replies', 
      description: 'Commands with large response payloads',
      minVersion: 'Valkey 8.1+',
      supported: logTypeSupport['large-reply']
    }
  ];

  const formatDuration = (microseconds: number): string => {
    if (microseconds >= 1000000) {
      return `${(microseconds / 1000000).toFixed(2)}s`;
    } else if (microseconds >= 1000) {
      return `${(microseconds / 1000).toFixed(2)}ms`;
    } else {
      return `${microseconds}μs`;
    }
  };

  const formatTimestamp = (timestamp: number): string => {
    // Convert Unix timestamp (seconds) to milliseconds for JavaScript Date
    return new Date(timestamp * 1000).toLocaleString();
  };

  const formatCommand = (command: string): string => {
    return command;
  };

  const getDurationBadgeVariant = (executionTime: number) => {
    if (executionTime >= 500000) return 'destructive'; // >= 500ms
    if (executionTime >= 100000) return 'secondary'; // >= 100ms
    return 'outline'; // < 100ms
  };

  const loadCommandLogData = useCallback(async () => {
    try {
      setError(null);
      const data = await valkeyApi.getCommandLogEntries(
        selectedLogType, 
        maxEntries === 'all' ? undefined : maxEntries
      );
      
      // Check if this response indicates a fallback was used
      const isFallbackUsed = data.source === 'SLOWLOG' && selectedLogType !== 'slow';
      const isUnsupported = data.note && data.note.includes('not supported');
      
      setCommandLogData({
        ...data,
        log_type: selectedLogType,
        fallback_used: isFallbackUsed,
        version_supported: !isUnsupported && !isFallbackUsed
      });

      // Update support status for this log type
      if (isFallbackUsed || isUnsupported) {
        setLogTypeSupport(prev => ({
          ...prev,
          [selectedLogType]: false
        }));
        
        // Show informative message instead of error
        if (selectedLogType !== 'slow') {
          setError(`${selectedLogType === 'large-request' ? 'Large Requests' : 'Large Replies'} logging is not supported on this Valkey/Redis version. Showing SLOWLOG data as fallback.`);
        }
      } else {
        // Mark as supported if we got actual data
        setLogTypeSupport(prev => ({
          ...prev,
          [selectedLogType]: true
        }));
      }
      
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to load command log data';
      
      // Check if this is a version compatibility error
      if (errorMessage.includes('command not supported') || 
          errorMessage.includes('CLUSTER SLOT-STATS') ||
          errorMessage.includes('Bad Request') ||
          errorMessage.includes('not supported by this Redis/Valkey version')) {
        
        // Set fallback data for unsupported log types
        setCommandLogData({
          entries: [],
          count: 0,
          requested_count: 0,
          timestamp: new Date().toISOString(),
          log_type: selectedLogType,
          command_used: '',
          fallback_used: false,
          version_supported: false
        });
        
        // Update support status
        setLogTypeSupport(prev => ({
          ...prev,
          [selectedLogType]: false
        }));
        
        // Show user-friendly message
        setError(`${selectedLogType === 'large-request' ? 'Large Requests' : selectedLogType === 'large-reply' ? 'Large Replies' : 'This log type'} is not supported on your current Valkey/Redis version.`);
      } else {
        setError(errorMessage);
      }
    } finally {
      setLoading(false);
    }
  }, [selectedLogType, maxEntries]);

  const handleClearCommandLog = async () => {
    try {
      setClearing(true);
      await valkeyApi.clearCommandLog(selectedLogType);
      // Reload data after clearing
      await loadCommandLogData();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear command log');
    } finally {
      setClearing(false);
    }
  };

  const handleRefresh = () => {
    setLoading(true);
    loadCommandLogData();
  };

  const handleLogTypeChange = (logType: CommandLogType) => {
    setSelectedLogType(logType);
    setLoading(true);
    // Clear any previous errors when switching log types
    setError(null);
  };

  // Filter entries based on search term and duration filter
  const filteredEntries = commandLogData.entries.filter((entry) => {
    // Search filter
    if (searchTerm) {
      const searchLower = searchTerm.toLowerCase();
      const commandText = formatCommand(entry.command).toLowerCase();
      
      if (!commandText.includes(searchLower)) {
        return false;
      }
    }

    // Duration filter
    if (durationFilter !== 'all') {
      switch (durationFilter) {
        case 'fast':
          if (entry.execution_time_microseconds >= 100000) return false; // >= 100ms
          break;
        case 'slow':
          if (entry.execution_time_microseconds < 100000 || entry.execution_time_microseconds >= 500000) return false; // 100ms - 500ms
          break;
        case 'very-slow':
          if (entry.execution_time_microseconds < 500000) return false; // >= 500ms
          break;
      }
    }

    return true;
  });

  // Auto-refresh effect
  useEffect(() => {
    let intervalId: NodeJS.Timeout;

    if (autoRefresh) {
      intervalId = setInterval(() => {
        loadCommandLogData();
      }, refreshInterval);
    }

    return () => {
      if (intervalId) {
        clearInterval(intervalId);
      }
    };
  }, [autoRefresh, refreshInterval, loadCommandLogData]);

  // Load data when log type changes
  useEffect(() => {
    loadCommandLogData();
  }, [loadCommandLogData]);

  const currentLogTypeConfig = logTypeConfigs.find(config => config.type === selectedLogType);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Command Log</h1>
          <p className="text-muted-foreground">
            Monitor and analyze Valkey command execution patterns
          </p>
        </div>
        <div className="flex items-center gap-2">
          {commandLogData.fallback_used && (
            <Badge variant="secondary" className="flex items-center gap-1">
              <Info className="h-3 w-3" />
              Using SLOWLOG fallback
            </Badge>
          )}
          {commandLogData.command_used && (
            <Badge variant="outline" className="text-xs">
              {commandLogData.command_used}
            </Badge>
          )}
        </div>
      </div>

      {/* Log Type Selection */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Filter className="h-5 w-5" />
            Log Type Selection
          </CardTitle>
          <CardDescription>
            Choose which type of command log to view
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {logTypeConfigs.map((config) => (
              <Card 
                key={config.type}
                className={`cursor-pointer transition-all hover:shadow-md ${
                  selectedLogType === config.type 
                    ? 'ring-2 ring-primary' 
                    : config.supported ? '' : 'opacity-50'
                }`}
                onClick={() => config.supported && handleLogTypeChange(config.type)}
              >
                <CardContent className="p-4">
                  <div className="flex items-start justify-between">
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <h3 className="font-medium">{config.label}</h3>
                        {selectedLogType === config.type && (
                          <CheckCircle className="h-4 w-4 text-primary" />
                        )}
                      </div>
                      <p className="text-sm text-muted-foreground">
                        {config.description}
                      </p>
                      <Badge variant="outline" className="text-xs">
                        {config.minVersion}
                      </Badge>
                    </div>
                  </div>
                  {!config.supported && (
                    <Alert className="mt-3">
                      <AlertTriangle className="h-4 w-4" />
                      <AlertDescription className="text-xs">
                        Not supported on current Valkey version. COMMANDLOG feature required.
                      </AlertDescription>
                    </Alert>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Controls */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Filter className="h-5 w-5" />
            Controls & Filters
          </CardTitle>
          <CardDescription>
            Manage command log settings and filter entries
            {currentLogTypeConfig && ` • Currently viewing: ${currentLogTypeConfig.label}`}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Action buttons */}
          <div className="flex items-center gap-2">
            <Button 
              onClick={handleRefresh} 
              disabled={loading}
              variant="outline"
              size="sm"
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </Button>
            
            <Button 
              onClick={handleClearCommandLog}
              disabled={clearing}
              variant="destructive"
              size="sm"
            >
              <Trash2 className="h-4 w-4 mr-2" />
              {clearing ? 'Clearing...' : 'Clear Log'}
            </Button>

            <div className="flex items-center gap-2 ml-4">
              <label className="text-sm font-medium">Auto-refresh:</label>
              <Button
                onClick={() => setAutoRefresh(!autoRefresh)}
                variant={autoRefresh ? "default" : "outline"}
                size="sm"
              >
                {autoRefresh ? 'ON' : 'OFF'}
              </Button>
              
              {autoRefresh && (
                <Select value={refreshInterval.toString()} onValueChange={(value) => setRefreshInterval(parseInt(value))}>
                  <SelectTrigger className="w-20">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="5000">5s</SelectItem>
                    <SelectItem value="10000">10s</SelectItem>
                    <SelectItem value="30000">30s</SelectItem>
                    <SelectItem value="60000">1m</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </div>
          </div>

          {/* Filters */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div className="space-y-2">
              <label className="text-sm font-medium">Search</label>
              <div className="relative">
                <Search className="absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  placeholder="Search commands..."
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className="pl-8"
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium">Duration Filter</label>
              <Select value={durationFilter} onValueChange={setDurationFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Commands</SelectItem>
                  <SelectItem value="fast">Fast (&lt; 100ms)</SelectItem>
                  <SelectItem value="slow">Slow (100ms - 500ms)</SelectItem>
                  <SelectItem value="very-slow">Very Slow (&gt; 500ms)</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium">Max Entries</label>
              <Select value={maxEntries.toString()} onValueChange={(value) => setMaxEntries(value === 'all' ? 'all' : parseInt(value))}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="50">50</SelectItem>
                  <SelectItem value="100">100</SelectItem>
                  <SelectItem value="200">200</SelectItem>
                  <SelectItem value="500">500</SelectItem>
                  <SelectItem value="all">All</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium">Results</label>
              <div className="flex items-center h-10 px-3 rounded-md border bg-muted">
                <span className="text-sm text-muted-foreground">
                  {filteredEntries.length} / {commandLogData.count}
                </span>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Error Display */}
      {error && (
        <Alert variant={error.includes('not supported') || error.includes('fallback') ? "default" : "destructive"}>
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Command Log Table */}
      <Card>
        <CardHeader>
          <CardTitle>Command Log Entries</CardTitle>
          <CardDescription>
            {currentLogTypeConfig?.description}
            {commandLogData.timestamp && ` • Last updated: ${new Date(commandLogData.timestamp).toLocaleString()}`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <RefreshCw className="h-6 w-6 animate-spin mr-2" />
              <span>Loading command log entries...</span>
            </div>
          ) : filteredEntries.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              {commandLogData.entries.length === 0 ? (
                commandLogData.fallback_used ? (
                  <div className="space-y-2">
                    <p>No entries found using SLOWLOG fallback.</p>
                    <p className="text-sm">
                      {currentLogTypeConfig?.label} logging requires COMMANDLOG support (Valkey 8.1+).
                      Only slow queries are available via SLOWLOG fallback.
                    </p>
                  </div>
                ) : (
                  `No ${currentLogTypeConfig?.label.toLowerCase()} entries found. This could mean all commands are executing efficiently!`
                )
              ) : (
                "No entries match your current filters."
              )}
            </div>
          ) : (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>ID</TableHead>
                    <TableHead>Timestamp</TableHead>
                    <TableHead>Duration</TableHead>
                    <TableHead className="max-w-md">Command</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredEntries.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell className="font-mono text-sm">
                        {entry.id}
                      </TableCell>
                      <TableCell className="text-sm">
                        {formatTimestamp(entry.timestamp)}
                      </TableCell>
                      <TableCell>
                        <Badge variant={getDurationBadgeVariant(entry.execution_time_microseconds)}>
                          {formatDuration(entry.execution_time_microseconds)}
                        </Badge>
                      </TableCell>
                      <TableCell className="max-w-md">
                        <div className="max-w-md overflow-x-auto">
                          <code className="text-sm bg-muted px-2 py-1 rounded whitespace-nowrap">
                            {formatCommand(entry.command)}
                          </code>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
