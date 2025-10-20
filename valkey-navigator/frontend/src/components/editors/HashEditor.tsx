import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Plus, Trash2, Save, X } from 'lucide-react';

interface HashEditorProps {
  value: { [key: string]: any };
  onChange: (value: { [key: string]: any }) => void;
  disabled?: boolean;
}

export function HashEditor({ value, onChange, disabled = false }: HashEditorProps) {
  const [hashData, setHashData] = useState<{ [key: string]: string }>({});
  const [editingField, setEditingField] = useState<string | null>(null);
  const [newFieldKey, setNewFieldKey] = useState('');
  const [newFieldValue, setNewFieldValue] = useState('');
  const [isAddingField, setIsAddingField] = useState(false);

  // Convert the incoming value to hash format and store original for comparison
  useEffect(() => {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const converted: { [key: string]: string } = {};
      Object.entries(value).forEach(([k, v]) => {
        converted[k] = typeof v === 'object' ? JSON.stringify(v) : String(v);
      });
      setHashData(converted);
    } else {
      setHashData({});
    }
  }, [value]);

  // Utility function to convert string values back to appropriate types
  const convertStringToValue = (str: string): any => {
    try {
      // Try to parse as JSON first for objects/arrays
      if (str.trim().startsWith('{') || str.trim().startsWith('[')) {
        return JSON.parse(str);
      } else if (str === 'true' || str === 'false') {
        return str === 'true';
      } else if (!isNaN(Number(str)) && str.trim() !== '') {
        return Number(str);
      } else {
        return str;
      }
    } catch {
      return str;
    }
  };

  // Convert hash data back to proper types
  const convertHashToTypedObject = (hash: { [key: string]: string }): { [key: string]: any } => {
    const convertedBack: { [key: string]: any } = {};
    Object.entries(hash).forEach(([k, v]) => {
      convertedBack[k] = convertStringToValue(v);
    });
    return convertedBack;
  };


  const handleFieldChange = (field: string, newValue: string) => {
    const updated = { ...hashData, [field]: newValue };
    setHashData(updated);
    
    // Don't notify the parent immediately on every change - let them handle it
    // when they're ready to save. This prevents excessive onChange calls.
    // The parent should compare the current state with committed state when saving.
    const convertedBack = convertHashToTypedObject(updated);
    onChange(convertedBack);
  };

  const handleDeleteField = (field: string) => {
    const updated = { ...hashData };
    delete updated[field];
    setHashData(updated);
    
    // Always notify on delete since it's removing a field
    const convertedBack = convertHashToTypedObject(updated);
    onChange(convertedBack);
  };

  const handleAddField = () => {
    if (!newFieldKey.trim()) return;
    
    const updated = { ...hashData, [newFieldKey]: newFieldValue };
    setHashData(updated);
    
    // Always notify on add since it's a new field
    const convertedBack = convertHashToTypedObject(updated);
    onChange(convertedBack);
    
    // Reset add form
    setNewFieldKey('');
    setNewFieldValue('');
    setIsAddingField(false);
  };

  const cancelAddField = () => {
    setNewFieldKey('');
    setNewFieldValue('');
    setIsAddingField(false);
  };

  const fields = Object.entries(hashData);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-muted-foreground">
          Hash Fields ({fields.length})
        </h3>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setIsAddingField(true)}
          disabled={disabled || isAddingField}
        >
          <Plus className="h-4 w-4 mr-1" />
          Add Field
        </Button>
      </div>

      <div className="border rounded-lg">
        <ScrollArea className="h-80">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[30%]">Field Name</TableHead>
                <TableHead className="w-[60%]">Value</TableHead>
                <TableHead className="w-[10%]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fields.map(([field, fieldValue]) => (
                <TableRow key={field}>
                  <TableCell className="font-mono text-sm font-medium">
                    {field}
                  </TableCell>
                  <TableCell>
                    {editingField === field ? (
                      <Input
                        value={fieldValue}
                        onChange={(e) => handleFieldChange(field, e.target.value)}
                        onBlur={() => setEditingField(null)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') setEditingField(null);
                          if (e.key === 'Escape') {
                            setEditingField(null);
                            // Reset to original value
                            setHashData(prev => ({ ...prev, [field]: fieldValue }));
                          }
                        }}
                        className="font-mono text-sm"
                        disabled={disabled}
                        autoFocus
                      />
                    ) : (
                      <div
                        className="font-mono text-sm cursor-pointer hover:bg-muted/50 p-1 rounded min-h-[24px]"
                        onClick={() => !disabled && setEditingField(field)}
                        title="Click to edit"
                      >
                        {fieldValue || <span className="text-muted-foreground italic">empty</span>}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleDeleteField(field)}
                      disabled={disabled}
                      className="h-6 w-6 p-0 text-red-600 hover:text-red-700 hover:bg-red-100"
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              
              {isAddingField && (
                <TableRow>
                  <TableCell>
                    <Input
                      value={newFieldKey}
                      onChange={(e) => setNewFieldKey(e.target.value)}
                      placeholder="Field name"
                      className="font-mono text-sm"
                      disabled={disabled}
                      autoFocus
                    />
                  </TableCell>
                  <TableCell>
                    <Input
                      value={newFieldValue}
                      onChange={(e) => setNewFieldValue(e.target.value)}
                      placeholder="Field value"
                      className="font-mono text-sm"
                      disabled={disabled}
                    />
                  </TableCell>
                  <TableCell>
                    <div className="flex space-x-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={handleAddField}
                        disabled={disabled || !newFieldKey.trim()}
                        className="h-6 w-6 p-0 text-green-600 hover:text-green-700 hover:bg-green-100"
                      >
                        <Save className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={cancelAddField}
                        disabled={disabled}
                        className="h-6 w-6 p-0"
                      >
                        <X className="h-3 w-3" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              )}
              
              {fields.length === 0 && !isAddingField && (
                <TableRow>
                  <TableCell colSpan={3} className="text-center text-muted-foreground py-8">
                    No fields in this hash. Click "Add Field" to get started.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </ScrollArea>
      </div>

      <div className="text-xs text-muted-foreground">
        💡 Click on any value to edit it inline. Values are automatically converted to appropriate types (string, number, boolean, or JSON).
      </div>
    </div>
  );
}
