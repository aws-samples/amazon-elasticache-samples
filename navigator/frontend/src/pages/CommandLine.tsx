import { useState, useEffect, useRef, type KeyboardEvent } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { 
  Terminal, 
  Loader2, 
  Download,
  Trash2,
  Zap,
  Clock,
  CheckCircle,
  XCircle,
  Info,
  History,
  Copy,
  Search,
  Play,
  ChevronUp,
  ChevronDown
} from 'lucide-react';
import { valkeyApi } from '@/services/valkeyApi';
import { useConnection } from '@/contexts/ConnectionContext';

interface CommandEntry {
  id: string;
  command: string;
  timestamp: string;
  response?: any;
  success?: boolean;
  executionTime?: number;
  error?: string;
}

interface HistoryEntry {
  id: string;
  command: string;
  timestamp: string;
  success: boolean;
  executionTime?: number;
}

export function CommandLine() {
  const { activeConnection } = useConnection();
  const [command, setCommand] = useState('');
  const [commandHistory, setCommandHistory] = useState<string[]>([]);
  const [outputHistory, setOutputHistory] = useState<CommandEntry[]>([]);
  const [persistentHistory, setPersistentHistory] = useState<HistoryEntry[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [isExecuting, setIsExecuting] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<'connected' | 'disconnected' | 'checking'>('checking');
  const [showHistory, setShowHistory] = useState(false);
  const [historySearch, setHistorySearch] = useState('');
  
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const historyScrollRef = useRef<HTMLDivElement>(null);

  // Update API service connection whenever active connection changes
  useEffect(() => {
    if (activeConnection) {
      console.log('🔗 CommandLine: Updating API service with new connection:', activeConnection.name);
      valkeyApi.setConnection(activeConnection);
      // Recheck connection with new cluster
      checkConnection();
    }
  }, [activeConnection]);

  // Load persistent history from localStorage on mount
  useEffect(() => {
    const stored = localStorage.getItem('valkey-cli-history');
    if (stored) {
      try {
        const parsed = JSON.parse(stored);
        setPersistentHistory(parsed);
        setCommandHistory(parsed.map((entry: HistoryEntry) => entry.command));
      } catch (error) {
        console.error('Failed to parse stored history:', error);
      }
    }
  }, []);

  // Save persistent history to localStorage whenever it changes
  useEffect(() => {
    if (persistentHistory.length > 0) {
      localStorage.setItem('valkey-cli-history', JSON.stringify(persistentHistory));
    }
  }, [persistentHistory]);

  // Check connection status on mount
  useEffect(() => {
    checkConnection();
  }, []);

  // Auto-focus input and scroll to bottom when output changes
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
    if (scrollAreaRef.current) {
      const scrollContainer = scrollAreaRef.current.querySelector('[data-radix-scroll-area-viewport]');
      if (scrollContainer) {
        scrollContainer.scrollTop = scrollContainer.scrollHeight;
      }
    }
  }, [outputHistory]);

  const checkConnection = async () => {
    setConnectionStatus('checking');
    try {
      await valkeyApi.getHealth();
      setConnectionStatus('connected');
    } catch (error) {
      setConnectionStatus('disconnected');
    }
  };

  const formatResponse = (response: any, success: boolean): React.ReactElement => {
    if (!success) {
      return (
        <div className="text-red-400 font-mono text-sm">
          <XCircle className="inline h-4 w-4 mr-2" />
          Error: {response || 'Command failed'}
        </div>
      );
    }

    // Handle different response types
    if (typeof response === 'string') {
      return (
        <div className="text-green-400 font-mono text-sm whitespace-pre-wrap">
          <CheckCircle className="inline h-4 w-4 mr-2 text-green-500" />
          {response}
        </div>
      );
    }

    if (typeof response === 'number') {
      return (
        <div className="text-blue-400 font-mono text-sm">
          <Info className="inline h-4 w-4 mr-2 text-blue-500" />
          (integer) {response}
        </div>
      );
    }

    if (Array.isArray(response)) {
      return (
        <div className="text-yellow-400 font-mono text-sm">
          <CheckCircle className="inline h-4 w-4 mr-2 text-green-500" />
          <div className="ml-6">
            {response.length === 0 ? (
              <span className="text-gray-400">(empty array)</span>
            ) : (
              response.map((item, index) => (
                <div key={index} className="py-1">
                  {index + 1}) {typeof item === 'object' ? JSON.stringify(item) : String(item)}
                </div>
              ))
            )}
          </div>
        </div>
      );
    }

    if (typeof response === 'object' && response !== null) {
      return (
        <div className="text-cyan-400 font-mono text-sm">
          <CheckCircle className="inline h-4 w-4 mr-2 text-green-500" />
          <div className="ml-6">
            {Object.keys(response).length === 0 ? (
              <span className="text-gray-400">(empty object)</span>
            ) : (
              Object.entries(response).map(([key, value], index) => (
                <div key={index} className="py-1">
                  <span className="text-purple-400">{key}:</span> {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                </div>
              ))
            )}
          </div>
        </div>
      );
    }

    return (
      <div className="text-green-400 font-mono text-sm">
        <CheckCircle className="inline h-4 w-4 mr-2 text-green-500" />
        {String(response)}
      </div>
    );
  };

  const executeCommand = async (cmd: string) => {
    if (!cmd.trim()) return;

    const trimmedCmd = cmd.trim();
    const commandId = Date.now().toString();
    const timestamp = new Date().toISOString();

    // Add command to history
    setCommandHistory(prev => [...prev.filter(c => c !== trimmedCmd), trimmedCmd]);
    setHistoryIndex(-1);

    // Add command entry to output
    const commandEntry: CommandEntry = {
      id: commandId,
      command: trimmedCmd,
      timestamp,
    };

    setOutputHistory(prev => [...prev, commandEntry]);
    setIsExecuting(true);

    try {
      // Handle special built-in commands
      if (trimmedCmd.toLowerCase() === 'clear' || trimmedCmd.toLowerCase() === 'cls') {
        setOutputHistory([]);
        setIsExecuting(false);
        return;
      }

      if (trimmedCmd.toLowerCase() === 'help') {
        const helpResponse = `Available Commands:
        
Data Commands:
  GET key                    - Get the value of a key
  SET key value [EX seconds] - Set a key to a string value
  DEL key [key ...]         - Delete one or more keys
  EXISTS key [key ...]      - Check if key(s) exist
  TYPE key                  - Get the type of a key
  TTL key                   - Get time to live for a key
  EXPIRE key seconds        - Set a timeout on a key
  PERSIST key              - Remove timeout from a key

Hash Commands:
  HGET key field           - Get a hash field value
  HSET key field value     - Set a hash field value
  HGETALL key             - Get all hash fields and values
  HDEL key field [field...]- Delete hash fields

List Commands:
  LPUSH key value [value...] - Push values to list head
  RPUSH key value [value...] - Push values to list tail
  LPOP key                   - Pop value from list head
  RPOP key                   - Pop value from list tail
  LRANGE key start stop      - Get list elements by range

Set Commands:
  SADD key member [member...]  - Add members to set
  SREM key member [member...]  - Remove members from set
  SMEMBERS key                 - Get all set members
  SCARD key                    - Get set cardinality

Server Commands:
  PING                     - Test connection
  INFO [section]           - Get server information
  KEYS pattern            - Find keys matching pattern
  DBSIZE                  - Get number of keys in database

Built-in Commands:
  CLEAR, CLS              - Clear screen
  HELP                    - Show this help message
  
Example Usage:
  SET mykey "Hello World"
  GET mykey
  KEYS user:*
  HGETALL user:123`;

        setOutputHistory(prev => prev.map(entry => 
          entry.id === commandId 
            ? { ...entry, response: helpResponse, success: true, executionTime: 0 }
            : entry
        ));
        setIsExecuting(false);
        return;
      }

      // Execute the command via API
      const startTime = Date.now();
      const result = await valkeyApi.executeRedisCommand(trimmedCmd);
      const executionTime = Date.now() - startTime;

      // Update the command entry with results
      setOutputHistory(prev => prev.map(entry => 
        entry.id === commandId 
          ? { 
              ...entry, 
              response: result.success ? result.stdout : (result.stderr || result.message),
              success: result.success,
              executionTime
            }
          : entry
      ));

      // Add to persistent history
      const historyEntry: HistoryEntry = {
        id: commandId,
        command: trimmedCmd,
        timestamp,
        success: result.success,
        executionTime
      };
      setPersistentHistory(prev => {
        // Remove any duplicate commands and add new one at the end
        const filtered = prev.filter(entry => entry.command !== trimmedCmd);
        return [...filtered, historyEntry].slice(-100); // Keep last 100 commands
      });

    } catch (error) {
      // Update with error
      const errorSuccess = false;
      setOutputHistory(prev => prev.map(entry => 
        entry.id === commandId 
          ? { 
              ...entry, 
              response: error instanceof Error ? error.message : String(error),
              success: errorSuccess,
              executionTime: 0
            }
          : entry
      ));

      // Add to persistent history even for errors
      const historyEntry: HistoryEntry = {
        id: commandId,
        command: trimmedCmd,
        timestamp,
        success: errorSuccess,
        executionTime: 0
      };
      setPersistentHistory(prev => {
        const filtered = prev.filter(entry => entry.command !== trimmedCmd);
        return [...filtered, historyEntry].slice(-100);
      });

    } finally {
      setIsExecuting(false);
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !isExecuting) {
      e.preventDefault();
      executeCommand(command);
      setCommand('');
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (commandHistory.length > 0) {
        const newIndex = historyIndex === -1 ? commandHistory.length - 1 : Math.max(0, historyIndex - 1);
        setHistoryIndex(newIndex);
        setCommand(commandHistory[newIndex]);
      }
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (historyIndex !== -1) {
        const newIndex = historyIndex + 1;
        if (newIndex >= commandHistory.length) {
          setHistoryIndex(-1);
          setCommand('');
        } else {
          setHistoryIndex(newIndex);
          setCommand(commandHistory[newIndex]);
        }
      }
    } else if (e.key === 'Tab') {
      e.preventDefault();
      // Basic auto-completion for common commands
      const commonCommands = ['GET ', 'SET ', 'DEL ', 'KEYS ', 'HGETALL ', 'LPUSH ', 'RPUSH ', 'PING', 'INFO', 'TYPE '];
      const currentInput = command.toUpperCase();
      const matches = commonCommands.filter(cmd => cmd.startsWith(currentInput));
      if (matches.length === 1) {
        setCommand(matches[0]);
      }
    } else if (e.ctrlKey && e.key === 'l') {
      e.preventDefault();
      setOutputHistory([]);
    }
  };

  const clearHistory = () => {
    setOutputHistory([]);
  };

  const clearPersistentHistory = () => {
    setPersistentHistory([]);
    setCommandHistory([]);
    localStorage.removeItem('valkey-cli-history');
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      console.error('Failed to copy text: ', err);
    }
  };

  const executeHistoryCommand = (cmd: string) => {
    setCommand(cmd);
    executeCommand(cmd);
  };

  const populateCommand = (cmd: string) => {
    setCommand(cmd);
    if (inputRef.current) {
      inputRef.current.focus();
    }
  };

  const getFilteredHistory = () => {
    if (!historySearch.trim()) {
      return persistentHistory.slice().reverse(); // Show most recent first
    }
    
    const searchTerm = historySearch.toLowerCase();
    return persistentHistory
      .filter(entry => entry.command.toLowerCase().includes(searchTerm))
      .slice()
      .reverse();
  };

  const exportHistory = () => {
    const historyText = outputHistory.map(entry => {
      const timestamp = new Date(entry.timestamp).toLocaleString();
      const status = entry.success ? '✓' : '✗';
      const time = entry.executionTime ? ` (${entry.executionTime}ms)` : '';
      return `[${timestamp}] ${status} ${entry.command}${time}\n${entry.response || entry.error || ''}\n`;
    }).join('\n---\n');

    const blob = new Blob([historyText], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `valkey-cli-history-${new Date().toISOString().split('T')[0]}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const getConnectionStatusIcon = () => {
    switch (connectionStatus) {
      case 'connected':
        return <CheckCircle className="h-4 w-4 text-green-500" />;
      case 'disconnected':
        return <XCircle className="h-4 w-4 text-red-500" />;
      default:
        return <Loader2 className="h-4 w-4 text-yellow-500 animate-spin" />;
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">CLI Interface</h1>
          <p className="text-muted-foreground">
            Execute Valkey commands directly
          </p>
        </div>
        <div className="flex items-center space-x-2">
          <div className="flex items-center space-x-2 text-sm">
            {getConnectionStatusIcon()}
            <span className={connectionStatus === 'connected' ? 'text-green-600' : 'text-red-600'}>
              {connectionStatus === 'connected' ? 'Connected' : connectionStatus === 'disconnected' ? 'Disconnected' : 'Checking...'}
            </span>
          </div>
          <Button 
            variant={showHistory ? "default" : "outline"} 
            size="sm" 
            onClick={() => setShowHistory(!showHistory)}
          >
            <History className="mr-2 h-4 w-4" />
            History ({persistentHistory.length})
          </Button>
          <Button variant="outline" size="sm" onClick={exportHistory} disabled={outputHistory.length === 0}>
            <Download className="mr-2 h-4 w-4" />
            Export
          </Button>
          <Button variant="outline" size="sm" onClick={clearHistory} disabled={outputHistory.length === 0}>
            <Trash2 className="mr-2 h-4 w-4" />
            Clear
          </Button>
        </div>
      </div>

      {/* Terminal Interface */}
      <Card className="bg-gray-900 border-gray-700">
        <CardHeader className="border-b border-gray-700">
          <CardTitle className="flex items-center text-green-400">
            <Terminal className="mr-2 h-5 w-5" />
            Valkey CLI
          </CardTitle>
          <CardDescription className="text-gray-400">
            Type commands below. Use ↑/↓ for history, Tab for completion, Ctrl+L to clear.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {/* Output Area */}
          <ScrollArea ref={scrollAreaRef} className="h-96 w-full">
            <div className="p-4 font-mono text-sm space-y-3 bg-gray-900 text-green-400">
              {outputHistory.length === 0 ? (
                <div className="text-gray-500 text-center py-8">
                  <Zap className="h-8 w-8 mx-auto mb-2 opacity-50" />
                  <p>Welcome to Valkey CLI Interface</p>
                  <p className="text-xs mt-1">Type 'help' for available commands</p>
                </div>
              ) : (
                outputHistory.map((entry) => (
                  <div key={entry.id} className="space-y-2">
                    {/* Command */}
                    <div className="flex items-center space-x-2">
                      <span className="text-blue-400 select-none">valkey&gt;</span>
                      <span className="text-white">{entry.command}</span>
                      {entry.executionTime !== undefined && (
                        <span className="text-xs text-gray-500 ml-auto flex items-center">
                          <Clock className="h-3 w-3 mr-1" />
                          {entry.executionTime}ms
                        </span>
                      )}
                    </div>
                    {/* Response */}
                    {(entry.response !== undefined || entry.error) && (
                      <div className="ml-4 pb-2 border-b border-gray-800">
                        {formatResponse(entry.response || entry.error, entry.success ?? false)}
                      </div>
                    )}
                  </div>
                ))
              )}
              {/* Loading indicator */}
              {isExecuting && (
                <div className="flex items-center space-x-2 text-yellow-400">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>Executing command...</span>
                </div>
              )}
            </div>
          </ScrollArea>

          {/* Command Input */}
          <div className="border-t border-gray-700 bg-gray-900 p-4">
            <div className="flex items-center space-x-2">
              <span className="text-blue-400 font-mono select-none">valkey&gt;</span>
              <Input
                ref={inputRef}
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Enter Valkey command (try 'help' for available commands)"
                disabled={isExecuting}
                className="font-mono bg-transparent border-none text-white placeholder-gray-500 focus-visible:ring-0 focus-visible:ring-offset-0"
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Command History Panel */}
      {showHistory && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="flex items-center text-sm">
                <History className="mr-2 h-4 w-4" />
                Command History
              </CardTitle>
              <div className="flex items-center space-x-2">
                <Button 
                  variant="outline" 
                  size="sm" 
                  onClick={clearPersistentHistory}
                  disabled={persistentHistory.length === 0}
                >
                  <Trash2 className="h-4 w-4 mr-1" />
                  Clear All
                </Button>
                <Button 
                  variant="ghost" 
                  size="sm" 
                  onClick={() => setShowHistory(false)}
                >
                  {showHistory ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                </Button>
              </div>
            </div>
            <CardDescription>
              {persistentHistory.length > 0 
                ? `${persistentHistory.length} commands saved • Click to copy or run again`
                : 'No command history yet'
              }
            </CardDescription>
          </CardHeader>
          <CardContent>
            {persistentHistory.length > 0 ? (
              <div className="space-y-4">
                {/* Search */}
                <div className="flex items-center space-x-2">
                  <Search className="h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search command history..."
                    value={historySearch}
                    onChange={(e) => setHistorySearch(e.target.value)}
                    className="text-sm"
                  />
                </div>
                
                {/* History List */}
                <ScrollArea ref={historyScrollRef} className="h-64 w-full">
                  <div className="space-y-2">
                    {getFilteredHistory().map((entry) => (
                      <div
                        key={entry.id}
                        className="flex items-center justify-between p-3 rounded-lg border hover:bg-muted/50 group"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center space-x-2">
                            <code className="font-mono text-sm bg-muted px-2 py-1 rounded truncate">
                              {entry.command}
                            </code>
                            <Badge variant={entry.success ? "default" : "destructive"} className="text-xs">
                              {entry.success ? "✓" : "✗"}
                            </Badge>
                            {entry.executionTime && (
                              <Badge variant="secondary" className="text-xs">
                                {entry.executionTime}ms
                              </Badge>
                            )}
                          </div>
                          <div className="text-xs text-muted-foreground mt-1">
                            {new Date(entry.timestamp).toLocaleString()}
                          </div>
                        </div>
                        <div className="flex items-center space-x-1 opacity-0 group-hover:opacity-100 transition-opacity">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => copyToClipboard(entry.command)}
                            title="Copy command"
                            className="h-8 w-8 p-0"
                          >
                            <Copy className="h-3 w-3" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => populateCommand(entry.command)}
                            title="Load in input"
                            className="h-8 w-8 p-0"
                          >
                            <ChevronDown className="h-3 w-3" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => executeHistoryCommand(entry.command)}
                            disabled={isExecuting}
                            title="Run command"
                            className="h-8 w-8 p-0"
                          >
                            <Play className="h-3 w-3" />
                          </Button>
                        </div>
                      </div>
                    ))}
                    
                    {getFilteredHistory().length === 0 && historySearch.trim() && (
                      <div className="text-center py-8 text-muted-foreground">
                        <Search className="h-8 w-8 mx-auto mb-2 opacity-50" />
                        <p>No commands found matching "{historySearch}"</p>
                      </div>
                    )}
                  </div>
                </ScrollArea>
              </div>
            ) : (
              <div className="text-center py-8 text-muted-foreground">
                <History className="h-8 w-8 mx-auto mb-2 opacity-50" />
                <p>No command history yet</p>
                <p className="text-xs mt-1">Start running commands to build your history</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Quick Command Buttons */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Quick Commands</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2">
            {[
              'PING', 
              'INFO', 
              'DBSIZE', 
              'KEYS *', 
              'help'
            ].map((cmd) => (
              <Button
                key={cmd}
                variant="outline"
                size="sm"
                onClick={() => {
                  setCommand(cmd);
                  if (inputRef.current) {
                    inputRef.current.focus();
                  }
                }}
                disabled={isExecuting}
                className="font-mono text-xs"
              >
                {cmd}
              </Button>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
