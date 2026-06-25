import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Plus, Trash2, Save, X, AlertTriangle, ArrowUp, ArrowDown } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';

interface SortedSetMember {
  member: string;
  score: number;
}

interface SortedSetEditorProps {
  value: any;
  onChange: (value: SortedSetMember[]) => void;
  disabled?: boolean;
}

export function SortedSetEditor({ value, onChange, disabled = false }: SortedSetEditorProps) {
  const [sortedSetData, setSortedSetData] = useState<SortedSetMember[]>([]);
  const [originalSortedSetData, setOriginalSortedSetData] = useState<SortedSetMember[]>([]);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [newMember, setNewMember] = useState('');
  const [newScore, setNewScore] = useState('');
  const [isAddingMember, setIsAddingMember] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('asc');

  // Convert incoming value to sorted set format
  useEffect(() => {
    if (value) {
      let converted: SortedSetMember[] = [];
      
      if (Array.isArray(value)) {
        // Handle array of objects with member/score structure
        converted = value.map((item, index) => {
          // Handle [member, score] array format (Redis ZRANGE response)
          if (Array.isArray(item) && item.length === 2) {
            return {
              member: String(item[0]),
              score: typeof item[1] === 'number' ? item[1] : (Number(item[1]) || 0)
            };
          }
          
          if (typeof item === 'object' && item !== null) {
            // Handle {member: "value", score: number} format
            if ('member' in item && 'score' in item) {
              return {
                member: String(item.member),
                score: typeof item.score === 'number' ? item.score : (Number(item.score) || 0)
              };
            }
            // Handle {value: "member", score: number} format
            if ('value' in item && 'score' in item) {
              return {
                member: String(item.value),
                score: typeof item.score === 'number' ? item.score : (Number(item.score) || 0)
              };
            }
            // Handle objects as JSON strings with default score
            return {
              member: JSON.stringify(item),
              score: 0
            };
          }
          // Handle simple values with default score
          return {
            member: String(item),
            score: index // Use index as default score
          };
        });
      } else if (typeof value === 'object' && value !== null) {
        // Handle object format where keys are members and values are scores
        converted = Object.entries(value).map(([member, score]) => ({
          member: String(member),
          score: Number(score) || 0
        }));
      }
      
      // Sort by score and member
      const sorted = converted.sort((a, b) => {
        if (sortDirection === 'asc') {
          return a.score !== b.score ? a.score - b.score : a.member.localeCompare(b.member);
        } else {
          return a.score !== b.score ? b.score - a.score : a.member.localeCompare(b.member);
        }
      });
      
      setSortedSetData(sorted);
      setOriginalSortedSetData([...sorted]);
    } else {
      setSortedSetData([]);
      setOriginalSortedSetData([]);
    }
  }, [value, sortDirection]);

  const checkForDuplicateMember = (newMember: string, excludeIndex?: number): boolean => {
    return sortedSetData.some((item, index) => 
      item.member === newMember && (excludeIndex === undefined || index !== excludeIndex)
    );
  };

  const validateScore = (scoreStr: string): { isValid: boolean; value: number; error?: string } => {
    // Allow empty scores to default to 0
    if (scoreStr.trim() === '') {
      return { isValid: true, value: 0 };
    }
    
    const num = Number(scoreStr);
    if (isNaN(num)) {
      return { isValid: false, value: 0, error: 'Score must be a valid number' };
    }
    
    if (!isFinite(num)) {
      return { isValid: false, value: 0, error: 'Score must be a finite number' };
    }
    
    return { isValid: true, value: num };
  };

  const handleMemberChange = (index: number, newMember: string, newScore: string) => {
    setError(null);
    
    // Check for duplicate member
    if (checkForDuplicateMember(newMember, index)) {
      setError(`Member "${newMember}" already exists in the sorted set`);
      return;
    }

    // Validate score
    const scoreValidation = validateScore(newScore);
    if (!scoreValidation.isValid) {
      setError(scoreValidation.error || 'Invalid score');
      return;
    }

    const updated = [...sortedSetData];
    updated[index] = { member: newMember, score: scoreValidation.value };
    setSortedSetData(updated);
    onChange(updated);
  };

  const handleDeleteMember = (index: number) => {
    const updated = sortedSetData.filter((_, i) => i !== index);
    setSortedSetData(updated);
    onChange(updated);
    setError(null);
  };

  const handleAddMember = () => {
    setError(null);
    
    if (!newMember.trim()) {
      setError('Member value cannot be empty');
      return;
    }
    
    // Check for duplicates
    if (checkForDuplicateMember(newMember)) {
      setError(`Member "${newMember}" already exists in the sorted set`);
      return;
    }
    
    // Validate score
    const scoreValidation = validateScore(newScore);
    if (!scoreValidation.isValid) {
      setError(scoreValidation.error || 'Invalid score');
      return;
    }
    
    const updated = [...sortedSetData, { member: newMember, score: scoreValidation.value }];
    const sorted = updated.sort((a, b) => {
      if (sortDirection === 'asc') {
        return a.score !== b.score ? a.score - b.score : a.member.localeCompare(b.member);
      } else {
        return a.score !== b.score ? b.score - a.score : a.member.localeCompare(b.member);
      }
    });
    
    setSortedSetData(sorted);
    onChange(sorted);
    
    // Reset add form
    setNewMember('');
    setNewScore('');
    setIsAddingMember(false);
  };

  const cancelAddMember = () => {
    setNewMember('');
    setNewScore('');
    setIsAddingMember(false);
    setError(null);
  };

  const toggleSortDirection = () => {
    setSortDirection(prev => prev === 'asc' ? 'desc' : 'asc');
  };

  // Check if current sorted set data has changes compared to original
  const hasChanges = (): boolean => {
    if (originalSortedSetData.length !== sortedSetData.length) {
      return true;
    }
    
    return !originalSortedSetData.every((original, index) => {
      const current = sortedSetData[index];
      return current && original.member === current.member && original.score === current.score;
    });
  };

  return (
    <div className="space-y-4">
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
        <div className="flex items-center">
          <div className="text-blue-800 text-sm">
            <strong>Valkey Sorted Set Editor</strong> - Sorted sets contain unique members with numeric scores. Members are automatically sorted by score.
          </div>
        </div>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-4">
          <h3 className="text-sm font-medium text-muted-foreground">
            Sorted Set Members ({sortedSetData.length})
          </h3>
          <Button
            variant="outline"
            size="sm"
            onClick={toggleSortDirection}
            disabled={disabled}
            className="flex items-center space-x-1"
          >
            {sortDirection === 'asc' ? (
              <>
                <ArrowUp className="h-3 w-3" />
                <span className="text-xs">Score ↑</span>
              </>
            ) : (
              <>
                <ArrowDown className="h-3 w-3" />
                <span className="text-xs">Score ↓</span>
              </>
            )}
          </Button>
        </div>
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
                <TableHead className="w-[15%]">Score</TableHead>
                <TableHead className="w-[65%]">Member Value</TableHead>
                <TableHead className="w-[20%]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sortedSetData.map((item, index) => (
                <TableRow key={`${item.member}-${item.score}-${index}`}>
                  <TableCell>
                    {editingIndex === index ? (
                      <Input
                        value={item.score.toString()}
                        onChange={(e) => {
                          // Update local state immediately for responsive typing
                          const updated = [...sortedSetData];
                          const scoreValidation = validateScore(e.target.value);
                          if (scoreValidation.isValid) {
                            updated[index] = { ...updated[index], score: scoreValidation.value };
                            setSortedSetData(updated);
                          }
                        }}
                        onBlur={() => {
                          handleMemberChange(index, item.member, item.score.toString());
                          setEditingIndex(null);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            handleMemberChange(index, item.member, item.score.toString());
                            setEditingIndex(null);
                          }
                          if (e.key === 'Escape') {
                            setEditingIndex(null);
                            // Reset to original value
                            setSortedSetData(prev => {
                              const reset = [...prev];
                              const original = originalSortedSetData.find(orig => orig.member === item.member);
                              if (original) {
                                reset[index] = { ...original };
                              }
                              return reset;
                            });
                            setError(null);
                          }
                        }}
                        className="font-mono text-sm w-20"
                        disabled={disabled}
                        type="number"
                        step="any"
                      />
                    ) : (
                      <div
                        className="font-mono text-sm font-bold cursor-pointer hover:bg-muted/50 p-1 rounded min-h-[24px] text-blue-600"
                        onClick={() => !disabled && setEditingIndex(index)}
                        title="Click to edit score"
                      >
                        {item.score}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    {editingIndex === index ? (
                      <Input
                        value={item.member}
                        onChange={(e) => {
                          // Update local state immediately for responsive typing
                          const updated = [...sortedSetData];
                          updated[index] = { ...updated[index], member: e.target.value };
                          setSortedSetData(updated);
                        }}
                        onBlur={() => {
                          handleMemberChange(index, item.member, item.score.toString());
                          setEditingIndex(null);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            handleMemberChange(index, item.member, item.score.toString());
                            setEditingIndex(null);
                          }
                          if (e.key === 'Escape') {
                            setEditingIndex(null);
                            // Reset to original value
                            setSortedSetData(prev => {
                              const reset = [...prev];
                              const original = originalSortedSetData.find(orig => orig.member === item.member);
                              if (original) {
                                reset[index] = { ...original };
                              }
                              return reset;
                            });
                            setError(null);
                          }
                        }}
                        className="font-mono text-sm"
                        disabled={disabled}
                        autoFocus={editingIndex === index}
                      />
                    ) : (
                      <div
                        className="font-mono text-sm cursor-pointer hover:bg-muted/50 p-1 rounded min-h-[24px] flex items-center"
                        onClick={() => !disabled && setEditingIndex(index)}
                        title="Click to edit member"
                      >
                        {item.member || <span className="text-muted-foreground italic">empty</span>}
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
                    <Input
                      value={newScore}
                      onChange={(e) => {
                        setNewScore(e.target.value);
                        setError(null);
                      }}
                      placeholder="0"
                      className="font-mono text-sm w-20"
                      disabled={disabled}
                      type="number"
                      step="any"
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          handleAddMember();
                        }
                        if (e.key === 'Escape') {
                          cancelAddMember();
                        }
                      }}
                    />
                  </TableCell>
                  <TableCell>
                    <Input
                      value={newMember}
                      onChange={(e) => {
                        setNewMember(e.target.value);
                        setError(null);
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
                  </TableCell>
                  <TableCell>
                    <div className="flex space-x-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={handleAddMember}
                        disabled={disabled || !newMember.trim()}
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
              
              {sortedSetData.length === 0 && !isAddingMember && (
                <TableRow>
                  <TableCell colSpan={3} className="text-center text-muted-foreground py-8">
                    No members in this sorted set. Click "Add Member" to get started.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </ScrollArea>
      </div>

      <div className="text-xs text-muted-foreground space-y-1">
        <div>💡 Click on any score or member to edit it inline. Members must be unique, but can have the same score.</div>
        <div>🔢 Scores must be valid numbers (integers or decimals). Members are sorted by score, then alphabetically.</div>
        <div>⬆️⬇️ Use the sort button to toggle between ascending and descending score order.</div>
        {hasChanges() && (
          <div className="text-blue-600 font-medium">
            ⚠️ Sorted set has unsaved changes. Use the save button to persist to Valkey.
          </div>
        )}
      </div>
    </div>
  );
}
