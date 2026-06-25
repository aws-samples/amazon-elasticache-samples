import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Plus, Trash2, Save, X, ChevronUp, ChevronDown } from 'lucide-react';

interface ListEditorProps {
  value: any[];
  onChange: (value: any[]) => void;
  disabled?: boolean;
}

export function ListEditor({ value, onChange, disabled = false }: ListEditorProps) {
  const [listData, setListData] = useState<string[]>([]);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [newItemValue, setNewItemValue] = useState('');
  const [isAddingItem, setIsAddingItem] = useState(false);

  // Convert the incoming value to list format
  useEffect(() => {
    if (Array.isArray(value)) {
      const converted = value.map(item => 
        typeof item === 'object' ? JSON.stringify(item) : String(item)
      );
      setListData(converted);
    } else {
      setListData([]);
    }
  }, [value]);

  const convertValue = (stringValue: string): any => {
    try {
      // Try to parse as JSON first
      if (stringValue.trim().startsWith('{') || stringValue.trim().startsWith('[')) {
        return JSON.parse(stringValue);
      } else if (stringValue === 'true' || stringValue === 'false') {
        return stringValue === 'true';
      } else if (!isNaN(Number(stringValue)) && stringValue.trim() !== '') {
        return Number(stringValue);
      } else {
        return stringValue;
      }
    } catch {
      return stringValue;
    }
  };

  const handleItemChange = (index: number, newValue: string) => {
    const updated = [...listData];
    updated[index] = newValue;
    setListData(updated);
    
    // Convert back to appropriate types and notify parent
    const convertedBack = updated.map(convertValue);
    onChange(convertedBack);
  };

  const handleDeleteItem = (index: number) => {
    const updated = listData.filter((_, i) => i !== index);
    setListData(updated);
    
    const convertedBack = updated.map(convertValue);
    onChange(convertedBack);
  };

  const handleAddItem = (position: 'start' | 'end' = 'end') => {
    if (!newItemValue.trim() && newItemValue !== '0' && newItemValue !== 'false') return;
    
    const updated = position === 'start' 
      ? [newItemValue, ...listData]
      : [...listData, newItemValue];
    setListData(updated);
    
    const convertedBack = updated.map(convertValue);
    onChange(convertedBack);
    
    // Reset add form
    setNewItemValue('');
    setIsAddingItem(false);
  };

  const handleMoveItem = (fromIndex: number, direction: 'up' | 'down') => {
    const toIndex = direction === 'up' ? fromIndex - 1 : fromIndex + 1;
    if (toIndex < 0 || toIndex >= listData.length) return;
    
    const updated = [...listData];
    [updated[fromIndex], updated[toIndex]] = [updated[toIndex], updated[fromIndex]];
    setListData(updated);
    
    const convertedBack = updated.map(convertValue);
    onChange(convertedBack);
  };

  const cancelAddItem = () => {
    setNewItemValue('');
    setIsAddingItem(false);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-muted-foreground">
          List Items ({listData.length})
        </h3>
        <div className="flex space-x-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setIsAddingItem(true)}
            disabled={disabled || isAddingItem}
          >
            <Plus className="h-4 w-4 mr-1" />
            Add Item
          </Button>
        </div>
      </div>

      <div className="border rounded-lg">
        <ScrollArea className="h-80">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[10%]">Index</TableHead>
                <TableHead className="w-[70%]">Value</TableHead>
                <TableHead className="w-[20%]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {listData.map((item, index) => (
                <TableRow key={index}>
                  <TableCell className="font-mono text-sm font-medium text-muted-foreground">
                    [{index}]
                  </TableCell>
                  <TableCell>
                    {editingIndex === index ? (
                      <Input
                        value={item}
                        onChange={(e) => handleItemChange(index, e.target.value)}
                        onBlur={() => setEditingIndex(null)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') setEditingIndex(null);
                          if (e.key === 'Escape') {
                            setEditingIndex(null);
                            // Reset to original value
                            setListData(prev => {
                              const reset = [...prev];
                              reset[index] = item;
                              return reset;
                            });
                          }
                        }}
                        className="font-mono text-sm"
                        disabled={disabled}
                        autoFocus
                      />
                    ) : (
                      <div
                        className="font-mono text-sm cursor-pointer hover:bg-muted/50 p-1 rounded min-h-[24px]"
                        onClick={() => !disabled && setEditingIndex(index)}
                        title="Click to edit"
                      >
                        {item || <span className="text-muted-foreground italic">empty</span>}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex space-x-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleMoveItem(index, 'up')}
                        disabled={disabled || index === 0}
                        className="h-6 w-6 p-0"
                        title="Move up"
                      >
                        <ChevronUp className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleMoveItem(index, 'down')}
                        disabled={disabled || index === listData.length - 1}
                        className="h-6 w-6 p-0"
                        title="Move down"
                      >
                        <ChevronDown className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDeleteItem(index)}
                        disabled={disabled}
                        className="h-6 w-6 p-0 text-red-600 hover:text-red-700 hover:bg-red-100"
                        title="Delete item"
                      >
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              
              {isAddingItem && (
                <TableRow>
                  <TableCell className="font-mono text-sm font-medium text-muted-foreground">
                    [new]
                  </TableCell>
                  <TableCell>
                    <Input
                      value={newItemValue}
                      onChange={(e) => setNewItemValue(e.target.value)}
                      placeholder="Item value"
                      className="font-mono text-sm"
                      disabled={disabled}
                      autoFocus
                    />
                  </TableCell>
                  <TableCell>
                    <div className="flex space-x-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleAddItem('start')}
                        disabled={disabled}
                        className="h-6 w-6 p-0 text-blue-600 hover:text-blue-700 hover:bg-blue-100"
                        title="Add to start"
                      >
                        ↑
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleAddItem('end')}
                        disabled={disabled}
                        className="h-6 w-6 p-0 text-green-600 hover:text-green-700 hover:bg-green-100"
                        title="Add to end"
                      >
                        <Save className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={cancelAddItem}
                        disabled={disabled}
                        className="h-6 w-6 p-0"
                      >
                        <X className="h-3 w-3" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              )}
              
              {listData.length === 0 && !isAddingItem && (
                <TableRow>
                  <TableCell colSpan={3} className="text-center text-muted-foreground py-8">
                    No items in this list. Click "Add Item" to get started.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </ScrollArea>
      </div>

      <div className="text-xs text-muted-foreground">
        💡 Click on any value to edit it inline. Use ↑/↓ arrows to reorder items. Values are automatically converted to appropriate types.
      </div>
    </div>
  );
}
