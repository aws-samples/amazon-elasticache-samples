import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Plus, Trash2, Save, X, AlertTriangle } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';

interface SetEditorProps {
  value: any[];
  onChange: (value: any[]) => void;
  disabled?: boolean;
}

export function SetEditor({ value, onChange, disabled = false }: SetEditorProps) {
  const [setData, setSetData] = useState<string[]>([]);
  const [originalSetData, setOriginalSetData] = useState<string[]>([]);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [newMemberValue, setNewMemberValue] = useState('');
  const [isAddingMember, setIsAddingMember] = useState(false);
  const [duplicateError, setDuplicateError] = useState<string | null>(null);

  // Convert the incoming value to set format (unique values only)
  useEffect(() => {
    if (Array.isArray(value)) {
      const converted = value.map(item => 
        typeof item === 'object' ? JSON.stringify(item) : String(item)
      );
      // Remove duplicates to ensure set semantics
      const uniqueConverted = Array.from(new Set(converted));
      setSetData(uniqueConverted);
      setOriginalSetData([...uniqueConverted]);
    } else {
      setSetData([]);
      setOriginalSetData([]);
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

  const checkForDuplicate = (newValue: string, excludeIndex?: number): boolean => {
    return setData.some((item, index) => 
      item === newValue && (excludeIndex === undefined || index !== excludeIndex)
    );
  };

  const handleMemberChange = (index: number, newValue: string) => {
    setDuplicateError(null);
    
    // Check for duplicates
    if (checkForDuplicate(newValue, index)) {
      setDuplicateError(`"${newValue}" already exists in the set`);
      return;
    }

    const updated = [...setData];
    updated[index] = newValue;
    setSetData(updated);
    
    // Convert back to appropriate types and notify parent
    const convertedBack = updated.map(convertValue);
    onChange(convertedBack);
  };

  const handleDeleteMember = (index: number) => {
    const updated = setData.filter((_, i) => i !== index);
    setSetData(updated);
    
    const convertedBack = updated.map(convertValue);
    onChange(convertedBack);
    setDuplicateError(null);
  };

  const handleAddMember = () => {
    if (!newMemberValue.trim() && newMemberValue !== '0' && newMemberValue !== 'false') {
      setDuplicateError('Member value cannot be empty');
      return;
    }
    
    // Check for duplicates
    if (checkForDuplicate(newMemberValue)) {
      setDuplicateError(`"${newMemberValue}" already exists in the set`);
      return;
    }
    
    const updated = [...setData, newMemberValue];
    setSetData(updated);
    
    const convertedBack = updated.map(convertValue);
    onChange(convertedBack);
    
    // Reset add form
    setNewMemberValue('');
    setIsAddingMember(false);
    setDuplicateError(null);
  };

  const cancelAddMember = () => {
    setNewMemberValue('');
    setIsAddingMember(false);
    setDuplicateError(null);
  };

  // Check if current set data has changes compared to original
  const hasChanges = (): boolean => {
    if (originalSetData.length !== setData.length) {
      return true;
    }
    
    // Sort both arrays for comparison since sets are unordered
    const originalSorted = [...originalSetData].sort();
    const currentSorted = [...setData].sort();
    
    return !originalSorted.every((item, index) => item === currentSorted[index]);
  };

  return (
    <div className="space-y-4">
      <div className="bg-orange-50 border border-orange-200 rounded-lg p-3">
        <div className="flex items-center">
          <div className="text-orange-800 text-sm">
            <strong>Valkey Set Editor</strong> - Sets contain unique, unordered elements. Duplicates are automatically prevented.
          </div>
        </div>
      </div>

      {duplicateError && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>{duplicateError}</AlertDescription>
        </Alert>
      )}

      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-muted-foreground">
          Set Members ({setData.length})
        </h3>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setIsAddingMember(true)}
          disabled={disabled || isAddingMember}
        >
          <Plus className="h-4 w-4 mr-1" />
          Add Member
        </Button>
      </div>

      <div className="border rounded-lg">
        <ScrollArea className="h-80">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[80%]">Member Value</TableHead>
                <TableHead className="w-[20%]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {setData.map((member, index) => (
                <TableRow key={`${member}-${index}`}>
                  <TableCell>
                    {editingIndex === index ? (
                      <div className="space-y-2">
                        <Input
                          value={member}
                          onChange={(e) => {
                            // Update the local state immediately for responsive typing
                            const updated = [...setData];
                            updated[index] = e.target.value;
                            setSetData(updated);
                          }}
                          onBlur={() => {
                            handleMemberChange(index, member);
                            setEditingIndex(null);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              handleMemberChange(index, member);
                              setEditingIndex(null);
                            }
                            if (e.key === 'Escape') {
                              setEditingIndex(null);
                              // Reset to original value
                              setSetData(prev => {
                                const reset = [...prev];
                                reset[index] = originalSetData[index] || member;
                                return reset;
                              });
                              setDuplicateError(null);
                            }
                          }}
                          className="font-mono text-sm"
                          disabled={disabled}
                          autoFocus
                        />
                        {duplicateError && duplicateError.includes(member) && (
                          <div className="text-xs text-red-600">
                            This value already exists in the set
                          </div>
                        )}
                      </div>
                    ) : (
                      <div
                        className="font-mono text-sm cursor-pointer hover:bg-muted/50 p-2 rounded min-h-[32px] flex items-center"
                        onClick={() => !disabled && setEditingIndex(index)}
                        title="Click to edit"
                      >
                        {member || <span className="text-muted-foreground italic">empty</span>}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleDeleteMember(index)}
                      disabled={disabled}
                      className="h-8 w-8 p-0 text-red-600 hover:text-red-700 hover:bg-red-100"
                      title="Remove member"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              
              {isAddingMember && (
                <TableRow>
                  <TableCell>
                    <div className="space-y-2">
                      <Input
                        value={newMemberValue}
                        onChange={(e) => {
                          setNewMemberValue(e.target.value);
                          setDuplicateError(null);
                        }}
                        placeholder="New member value"
                        className="font-mono text-sm"
                        disabled={disabled}
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            handleAddMember();
                          }
                          if (e.key === 'Escape') {
                            cancelAddMember();
                          }
                        }}
                      />
                      {duplicateError && duplicateError.includes(newMemberValue) && (
                        <div className="text-xs text-red-600">
                          This value already exists in the set
                        </div>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex space-x-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={handleAddMember}
                        disabled={disabled || !newMemberValue.trim()}
                        className="h-8 w-8 p-0 text-green-600 hover:text-green-700 hover:bg-green-100"
                        title="Add member"
                      >
                        <Save className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={cancelAddMember}
                        disabled={disabled}
                        className="h-8 w-8 p-0"
                        title="Cancel"
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              )}
              
              {setData.length === 0 && !isAddingMember && (
                <TableRow>
                  <TableCell colSpan={2} className="text-center text-muted-foreground py-8">
                    No members in this set. Click "Add Member" to get started.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </ScrollArea>
      </div>

      <div className="text-xs text-muted-foreground space-y-1">
        <div>💡 Click on any member to edit it inline. Sets automatically prevent duplicate values.</div>
        <div>🔄 Sets are unordered - the display order may not reflect Valkey storage order.</div>
        {hasChanges() && (
          <div className="text-orange-600 font-medium">
            ⚠️ Set has unsaved changes. Use the save button to persist to Valkey.
          </div>
        )}
      </div>
    </div>
  );
}
