import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
// NORBERT 9-10 //  import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Card, CardContent, CardDescription, CardHeader} from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Plus, Trash2, Info, Search, CheckCircle, XCircle, Loader2 } from 'lucide-react';
import { Separator } from '@/components/ui/separator';
import { valkeyApi } from '@/services/valkeyApi';

interface BloomFilterEditorProps {
  value: any;
  onChange: (value: any) => void;
  disabled?: boolean;
  keyName?: string;
}

interface BloomFilterOperation {
  type: 'BF.ADD' | 'BF.MADD' | 'BF.INSERT' | 'BF.EXISTS' | 'BF.MEXISTS';
  items: string[];
  options?: {
    capacity?: number;
    error?: number;
    expansion?: number;
    nonscaling?: boolean;
  };
}

interface QueryResult {
  item: string;
  exists: boolean;
  operation: 'BF.EXISTS' | 'BF.MEXISTS';
}

export function BloomFilterEditor({ value, onChange, disabled = false, keyName }: BloomFilterEditorProps) {
  // Query state for BF.EXISTS and BF.MEXISTS
  const [queryResults, setQueryResults] = useState<{[operationIndex: number]: QueryResult[]}>({});
  const [queryLoading, setQueryLoading] = useState<{[operationIndex: number]: boolean}>({});
  
  // Parse the current value into operations
  const [operations, setOperations] = useState<BloomFilterOperation[]>(() => {
    if (Array.isArray(value) && value.length > 0) {
      // If value is an array of items, create a BF.MADD operation
      return [{
        type: 'BF.MADD',
        items: value.map(String)
      }];
    } else if (typeof value === 'string' && value.trim()) {
      // If value is a single string, create a BF.ADD operation
      return [{
        type: 'BF.ADD',
        items: [value]
      }];
    } else {
      // Start with an empty BF.ADD operation
      return [{
        type: 'BF.ADD',
        items: ['']
      }];
    }
  });

  const [newItem, setNewItem] = useState('');
  const [bulkItems, setBulkItems] = useState('');

  // Update parent value when operations change
  const updateValue = (newOperations: BloomFilterOperation[]) => {
    setOperations(newOperations);
    
    // Convert operations to a format that can be processed by the command generator
    const commandData = {
      operations: newOperations,
      type: 'bloom_filter'
    };
    
    onChange(commandData);
  };

  const addOperation = () => {
    const newOp: BloomFilterOperation = {
      type: 'BF.ADD',
      items: ['']
    };
    updateValue([...operations, newOp]);
  };

  const removeOperation = (index: number) => {
    const newOps = operations.filter((_, i) => i !== index);
    updateValue(newOps.length > 0 ? newOps : [{
      type: 'BF.ADD',
      items: ['']
    }]);
  };

  const updateOperation = (index: number, updates: Partial<BloomFilterOperation>) => {
    const newOps = [...operations];
    newOps[index] = { ...newOps[index], ...updates };
    updateValue(newOps);
  };

  const addItemToOperation = (operationIndex: number, item: string) => {
    if (!item.trim()) return;
    
    const newOps = [...operations];
    newOps[operationIndex].items.push(item.trim());
    updateValue(newOps);
  };

  const removeItemFromOperation = (operationIndex: number, itemIndex: number) => {
    const newOps = [...operations];
    newOps[operationIndex].items = newOps[operationIndex].items.filter((_, i) => i !== itemIndex);
    updateValue(newOps);
  };

  const updateItemInOperation = (operationIndex: number, itemIndex: number, newValue: string) => {
    const newOps = [...operations];
    newOps[operationIndex].items[itemIndex] = newValue;
    updateValue(newOps);
  };

  const addBulkItems = (operationIndex: number) => {
    if (!bulkItems.trim()) return;
    
    const items = bulkItems
      .split('\n')
      .map(item => item.trim())
      .filter(item => item.length > 0);
    
    if (items.length === 0) return;
    
    const newOps = [...operations];
    newOps[operationIndex].items.push(...items);
    updateValue(newOps);
    setBulkItems('');
  };

  const getOperationDescription = (type: string) => {
    switch (type) {
      case 'BF.ADD':
        return 'Add a single item to the bloom filter';
      case 'BF.MADD':
        return 'Add multiple items to the bloom filter';
      case 'BF.INSERT':
        return 'Insert items with optional filter creation parameters';
      case 'BF.EXISTS':
        return 'Check if a single item exists in the bloom filter';
      case 'BF.MEXISTS':
        return 'Check if multiple items exist in the bloom filter';
      default:
        return 'Bloom filter operation';
    }
  };

  // Execute query operations (BF.EXISTS, BF.MEXISTS) immediately
  const executeQuery = async (operationIndex: number, keyName: string) => {
    const operation = operations[operationIndex];
    const validItems = operation.items.filter(item => item.trim());
    
    if (validItems.length === 0) {
      return;
    }

    // Set loading state
    setQueryLoading(prev => ({ ...prev, [operationIndex]: true }));
    
    try {
      const results: QueryResult[] = [];
      
      if (operation.type === 'BF.EXISTS') {
        // Execute individual BF.EXISTS commands
        for (const item of validItems) {
          const command = `BF.EXISTS "${keyName}" "${item.replace('"', '\\"')}"`;
          const result = await valkeyApi.executeRedisCommand(command);
          
          if (result.success) {
            const exists = result.stdout?.trim() === '1';
            results.push({
              item: item,
              exists: exists,
              operation: 'BF.EXISTS'
            });
          } else {
            results.push({
              item: item,
              exists: false,
              operation: 'BF.EXISTS'
            });
          }
        }
      } else if (operation.type === 'BF.MEXISTS') {
        // Execute single BF.MEXISTS command
        const escapedItems = validItems.map(item => `"${item.replace(/"/g, '\\"')}"`).join(' ');
        const command = `BF.MEXISTS "${keyName}" ${escapedItems}`;
        const result = await valkeyApi.executeRedisCommand(command);
        
        if (result.success && result.stdout) {
          // Parse the array result like [1, 0, 1]
          const stdout = result.stdout.trim();
          let existsArray: number[] = [];
          
          // Try to parse as JSON array first
          try {
            if (stdout.startsWith('[') && stdout.endsWith(']')) {
              existsArray = JSON.parse(stdout);
            } else {
              // Fallback: split by whitespace and parse numbers
              existsArray = stdout.split(/\s+/).map(s => parseInt(s)).filter(n => !isNaN(n));
            }
          } catch (e) {
            // If parsing fails, assume all don't exist
            existsArray = validItems.map(() => 0);
          }
          
          validItems.forEach((item, index) => {
            const exists = (existsArray[index] === 1);
            results.push({
              item: item,
              exists: exists,
              operation: 'BF.MEXISTS'
            });
          });
        } else {
          // If command failed, assume all don't exist
          validItems.forEach(item => {
            results.push({
              item: item,
              exists: false,
              operation: 'BF.MEXISTS'
            });
          });
        }
      }
      
      // Update results
      setQueryResults(prev => ({ ...prev, [operationIndex]: results }));
      
    } catch (error) {
      console.error('Query execution failed:', error);
      
      // Set error state - mark all as not found
      const errorResults: QueryResult[] = validItems.map(item => ({
        item: item,
        exists: false,
        operation: operation.type as 'BF.EXISTS' | 'BF.MEXISTS'
      }));
      
      setQueryResults(prev => ({ ...prev, [operationIndex]: errorResults }));
      
    } finally {
      setQueryLoading(prev => ({ ...prev, [operationIndex]: false }));
    }
  };

  // Helper to get the key name from the parent context
  const getKeyName = () => {
    return keyName || 'bloom_filter_key';
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="bg-orange-50 border border-orange-200 rounded-lg p-4">
        <div className="flex items-start space-x-3">
          <Info className="h-5 w-5 text-orange-600 mt-0.5 flex-shrink-0" />
          <div>
            <h3 className="text-orange-800 font-semibold text-sm mb-1">
              Bloom Filter Editor
            </h3>
            <p className="text-orange-700 text-xs leading-relaxed">
              Configure bloom filter operations. Use <code className="bg-orange-100 px-1 rounded">BF.ADD</code> for single items, 
              <code className="bg-orange-100 px-1 rounded">BF.MADD</code> for multiple items, 
              <code className="bg-orange-100 px-1 rounded">BF.INSERT</code> for advanced insertion, 
              <code className="bg-orange-100 px-1 rounded">BF.EXISTS</code> to check single items, 
              or <code className="bg-orange-100 px-1 rounded">BF.MEXISTS</code> to check multiple items.
            </p>
          </div>
        </div>
      </div>

      {/* Operations */}
      <div className="space-y-4">
        {operations.map((operation, operationIndex) => (
          <Card key={operationIndex}>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                  <Select
                    value={operation.type}
                    onValueChange={(value: 'BF.ADD' | 'BF.MADD' | 'BF.INSERT' | 'BF.EXISTS' | 'BF.MEXISTS') => 
                      updateOperation(operationIndex, { type: value })
                    }
                    disabled={disabled}
                  >
                    <SelectTrigger className="w-32">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="BF.ADD">BF.ADD</SelectItem>
                      <SelectItem value="BF.MADD">BF.MADD</SelectItem>
                      <SelectItem value="BF.INSERT">BF.INSERT</SelectItem>
                      <SelectItem value="BF.EXISTS">BF.EXISTS</SelectItem>
                      <SelectItem value="BF.MEXISTS">BF.MEXISTS</SelectItem>
                    </SelectContent>
                  </Select>
                  <Badge variant="outline" className="text-xs">
                    {operation.items.filter(item => item.trim()).length} items
                  </Badge>
                </div>
                {operations.length > 1 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => removeOperation(operationIndex)}
                    disabled={disabled}
                    className="text-red-600 hover:text-red-700 hover:bg-red-100"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                )}
              </div>
              <CardDescription className="text-xs">
                {getOperationDescription(operation.type)}
              </CardDescription>
            </CardHeader>
            
            <CardContent className="space-y-4">
              {/* BF.INSERT Options */}
              {operation.type === 'BF.INSERT' && (
                <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 space-y-3">
                  <h4 className="text-blue-800 font-medium text-sm">Filter Creation Options</h4>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="text-xs font-medium text-blue-700 block mb-1">
                        Capacity
                      </label>
                      <Input
                        type="number"
                        placeholder="1000"
                        value={operation.options?.capacity || ''}
                        onChange={(e) => updateOperation(operationIndex, {
                          options: {
                            ...operation.options,
                            capacity: e.target.value ? parseInt(e.target.value) : undefined
                          }
                        })}
                        disabled={disabled}
                        className="h-8 text-xs"
                      />
                    </div>
                    <div>
                      <label className="text-xs font-medium text-blue-700 block mb-1">
                        Error Rate
                      </label>
                      <Input
                        type="number"
                        step="0.001"
                        placeholder="0.01"
                        value={operation.options?.error || ''}
                        onChange={(e) => updateOperation(operationIndex, {
                          options: {
                            ...operation.options,
                            error: e.target.value ? parseFloat(e.target.value) : undefined
                          }
                        })}
                        disabled={disabled}
                        className="h-8 text-xs"
                      />
                    </div>
                  </div>
                </div>
              )}

              {/* Items List */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <label className="text-sm font-medium">
                    {(operation.type === 'BF.EXISTS' || operation.type === 'BF.MEXISTS') ? 'Items to Check' : 'Items to Add'}
                  </label>
                  <div className="flex items-center space-x-2">
                    <Input
                      placeholder="Add new item..."
                      value={newItem}
                      onChange={(e) => setNewItem(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          addItemToOperation(operationIndex, newItem);
                          setNewItem('');
                        }
                      }}
                      disabled={disabled}
                      className="h-8 w-48 text-xs"
                    />
                    <Button
                      size="sm"
                      onClick={() => {
                        addItemToOperation(operationIndex, newItem);
                        setNewItem('');
                      }}
                      disabled={disabled || !newItem.trim()}
                      className="h-8 px-3"
                    >
                      <Plus className="h-3 w-3" />
                    </Button>
                  </div>
                </div>

                {/* Individual Items */}
                <div className="max-h-40 overflow-y-auto border rounded-lg">
                  {operation.items.length === 0 ? (
                    <div className="p-4 text-center text-sm text-muted-foreground">
                      No items added yet. Add items using the input above.
                    </div>
                  ) : (
                    <div className="p-2 space-y-1">
                      {operation.items.map((item, itemIndex) => (
                        <div key={itemIndex} className="flex items-center space-x-2 group">
                          <Input
                            value={item}
                            onChange={(e) => updateItemInOperation(operationIndex, itemIndex, e.target.value)}
                            disabled={disabled}
                            className="h-8 text-xs font-mono flex-1"
                            placeholder="Enter item value..."
                          />
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => removeItemFromOperation(operationIndex, itemIndex)}
                            disabled={disabled}
                            className="h-8 w-8 p-0 opacity-0 group-hover:opacity-100 text-red-600 hover:text-red-700 hover:bg-red-100"
                          >
                            <Trash2 className="h-3 w-3" />
                          </Button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Query Button for BF.EXISTS and BF.MEXISTS */}
                {(operation.type === 'BF.EXISTS' || operation.type === 'BF.MEXISTS') && (
                  <div className="flex justify-center py-2">
                    <Button
                      variant="default"
                      size="sm"
                      onClick={() => executeQuery(operationIndex, getKeyName())}
                      disabled={disabled || queryLoading[operationIndex] || operation.items.filter(item => item.trim()).length === 0}
                      className="bg-blue-600 hover:bg-blue-700"
                    >
                      {queryLoading[operationIndex] ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Checking...
                        </>
                      ) : (
                        <>
                          <Search className="h-4 w-4 mr-2" />
                          Check Items
                        </>
                      )}
                    </Button>
                  </div>
                )}

                {/* Query Results Display */}
                {(operation.type === 'BF.EXISTS' || operation.type === 'BF.MEXISTS') && queryResults[operationIndex] && (
                  <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 space-y-2">
                    <h4 className="text-gray-800 font-medium text-sm flex items-center">
                      <Search className="h-4 w-4 mr-2" />
                      Query Results
                    </h4>
                    <div className="max-h-32 overflow-y-auto space-y-1">
                      {queryResults[operationIndex].map((result, resultIndex) => (
                        <div key={resultIndex} className="flex items-center justify-between bg-white rounded p-2 text-xs">
                          <span className="font-mono text-gray-700 flex-1 truncate" title={result.item}>
                            "{result.item}"
                          </span>
                          <div className="flex items-center space-x-2">
                            {result.exists ? (
                              <>
                                <CheckCircle className="h-4 w-4 text-green-600" />
                                <span className="text-green-700 font-medium">Might Exist</span>
                              </>
                            ) : (
                              <>
                                <XCircle className="h-4 w-4 text-red-600" />
                                <span className="text-red-700 font-medium">Not Found</span>
                              </>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="text-xs text-gray-600 bg-yellow-50 border border-yellow-200 rounded p-2">
                      <strong>Note:</strong> Bloom filters may have false positives. "Might Exist" means the item could be in the filter, 
                      but "Not Found" is definitive.
                    </div>
                  </div>
                )}

                {/* Bulk Add */}
                {(operation.type !== 'BF.EXISTS' && operation.type !== 'BF.MEXISTS') && (
                  <div className="space-y-2">
                    <Separator />
                    <details className="group">
                      <summary className="text-xs font-medium text-muted-foreground cursor-pointer hover:text-foreground">
                        Bulk Add Items (click to expand)
                      </summary>
                      <div className="mt-2 space-y-2">
                        <Textarea
                          placeholder="Enter multiple items, one per line..."
                          value={bulkItems}
                          onChange={(e) => setBulkItems(e.target.value)}
                          disabled={disabled}
                          className="h-20 text-xs font-mono"
                        />
                        <Button
                          size="sm"
                          onClick={() => addBulkItems(operationIndex)}
                          disabled={disabled || !bulkItems.trim()}
                          className="h-8"
                        >
                          Add All Items
                        </Button>
                      </div>
                    </details>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Add Operation Button */}
      <Button
        variant="outline"
        onClick={addOperation}
        disabled={disabled}
        className="w-full"
      >
        <Plus className="h-4 w-4 mr-2" />
        Add Another Operation
      </Button>

      {/* Summary */}
      <div className="bg-muted/50 rounded-lg p-3 text-xs text-muted-foreground">
        <strong>Summary:</strong> {operations.length} operation{operations.length !== 1 ? 's' : ''} • {' '}
        {operations.reduce((total, op) => total + op.items.filter(item => item.trim()).length, 0)} total items
      </div>
    </div>
  );
}
