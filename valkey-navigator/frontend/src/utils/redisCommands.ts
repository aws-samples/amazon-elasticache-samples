// Utility functions to generate Redis commands based on data type operations

/**
 * Escapes a string value for use in Redis commands
 * Uses Redis protocol format for proper handling of spaces and special characters
 */
function escapeRedisValue(value: any): string {
  const stringValue = String(value);
  
  // If the value contains spaces, newlines, or special characters, use Redis bulk string format
  if (stringValue.includes(' ') || 
      stringValue.includes('\n') || 
      stringValue.includes('\r') || 
      stringValue.includes('\t') ||
      stringValue.includes('"') ||
      stringValue.includes("'")) {
    
    // Use Redis bulk string format: $<length>\r\n<data>\r\n
    // But since we're sending this as a command string, we'll use quoted format
    // with proper escaping for the backend to parse
    const escapedValue = stringValue
      .replace(/\\/g, '\\\\')  // Escape backslashes first
      .replace(/"/g, '\\"')    // Escape quotes
      .replace(/\n/g, '\\n')   // Escape newlines  
      .replace(/\r/g, '\\r')   // Escape carriage returns
      .replace(/\t/g, '\\t');  // Escape tabs
    
    return `"${escapedValue}"`;
  }
  
  // For simple values without spaces or special chars, no quotes needed
  // This helps avoid over-escaping simple values
  if (/^[a-zA-Z0-9._-]+$/.test(stringValue)) {
    return stringValue;
  }
  
  // Default case: use quotes for safety
  return `"${stringValue}"`;
}

/**
 * Generate Redis commands for hash operations
 * Compares old and new hash states to generate optimal HSET/HDEL commands
 */
export function generateHashCommands(
  key: string,
  oldHash: Record<string, any> = {},
  newHash: Record<string, any> = {}
): string[] {
  const commands: string[] = [];
  
  // Find fields to set/update (batch them into one HSET command)
  const fieldsToSet: string[] = [];
  Object.entries(newHash).forEach(([field, value]) => {
    // Use deepEqual for better comparison, especially for object values
    if (!deepEqual(oldHash[field], value)) {
      fieldsToSet.push(escapeRedisValue(field), escapeRedisValue(value));
    }
  });
  
  if (fieldsToSet.length > 0) {
    commands.push(`HSET ${escapeRedisValue(key)} ${fieldsToSet.join(' ')}`);
  }
  
  // Find fields to delete
  const fieldsToDelete = Object.keys(oldHash).filter(
    field => !(field in newHash)
  );
  
  if (fieldsToDelete.length > 0) {
    const escapedFields = fieldsToDelete.map(field => escapeRedisValue(field)).join(' ');
    commands.push(`HDEL ${escapeRedisValue(key)} ${escapedFields}`);
  }
  
  return commands;
}


/**
 * Generate Redis commands for list operations
 * Compares old and new list states to generate optimal list commands
 */
export function generateListCommands(
  key: string,
  oldList: any[] = [],
  newList: any[] = []
): string[] {
  const commands: string[] = [];
  const escapedKey = escapeRedisValue(key);
  
  // If the list is completely empty, we just need to create it
  if (oldList.length === 0 && newList.length > 0) {
    // Push all items to the end
    const values = newList.map(item => escapeRedisValue(item)).join(' ');
    commands.push(`RPUSH ${escapedKey} ${values}`);
    return commands;
  }
  
  // If the new list is empty, delete everything
  if (newList.length === 0 && oldList.length > 0) {
    commands.push(`DEL ${escapedKey}`);
    return commands;
  }
  
  // For more complex changes, we'll use a simpler approach:
  // Delete the entire list and recreate it with the new values
  // This is less efficient but more reliable for complex reorderings
  if (oldList.length > 0) {
    commands.push(`DEL ${escapedKey}`);
  }
  
  if (newList.length > 0) {
    const values = newList.map(item => escapeRedisValue(item)).join(' ');
    commands.push(`RPUSH ${escapedKey} ${values}`);
  }
  
  return commands;
}

/**
 * Generate optimized list commands for common operations
 * This is a more sophisticated version that handles specific patterns
 */
export function generateOptimizedListCommands(
  key: string,
  oldList: any[] = [],
  newList: any[] = []
): string[] {
  const commands: string[] = [];
  const escapedKey = escapeRedisValue(key);
  
  // Handle empty cases
  if (oldList.length === 0 && newList.length > 0) {
    const values = newList.map(item => escapeRedisValue(item)).join(' ');
    commands.push(`RPUSH ${escapedKey} ${values}`);
    return commands;
  }
  
  if (newList.length === 0) {
    commands.push(`DEL ${escapedKey}`);
    return commands;
  }
  
  // Check for simple append operation (all old items unchanged, new items at the end)
  if (newList.length > oldList.length && 
      oldList.every((item, index) => item === newList[index])) {
    const newItems = newList.slice(oldList.length);
    const values = newItems.map(item => escapeRedisValue(item)).join(' ');
    commands.push(`RPUSH ${escapedKey} ${values}`);
    return commands;
  }
  
  // Check for simple prepend operation (all old items unchanged, new items at the beginning)
  if (newList.length > oldList.length &&
      oldList.every((item, index) => item === newList[index + (newList.length - oldList.length)])) {
    const newItems = newList.slice(0, newList.length - oldList.length);
    const values = newItems.map(item => escapeRedisValue(item)).join(' ');
    commands.push(`LPUSH ${escapedKey} ${values}`);
    return commands;
  }
  
  // For other complex changes, fall back to delete and recreate
  commands.push(`DEL ${escapedKey}`);
  const values = newList.map(item => escapeRedisValue(item)).join(' ');
  commands.push(`RPUSH ${escapedKey} ${values}`);
  
  return commands;
}

/**
 * Generate Redis commands for set operations
 */
export function generateSetCommands(
  key: string,
  oldSet: any[] = [],
  newSet: any[] = []
): string[] {
  const commands: string[] = [];
  const escapedKey = escapeRedisValue(key);
  
  const oldSetValues = new Set(oldSet);
  const newSetValues = new Set(newSet);
  
  // Find items to add
  const toAdd = newSet.filter(item => !oldSetValues.has(item));
  if (toAdd.length > 0) {
    const values = toAdd.map(item => escapeRedisValue(item)).join(' ');
    commands.push(`SADD ${escapedKey} ${values}`);
  }
  
  // Find items to remove
  const toRemove = oldSet.filter(item => !newSetValues.has(item));
  if (toRemove.length > 0) {
    const values = toRemove.map(item => escapeRedisValue(item)).join(' ');
    commands.push(`SREM ${escapedKey} ${values}`);
  }
  
  return commands;
}

/**
 * Generate Redis commands for sorted set operations
 */
export function generateSortedSetCommands(
  key: string,
  oldSortedSet: Array<{member: string, score: number}> = [],
  newSortedSet: Array<{member: string, score: number}> = []
): string[] {
  const commands: string[] = [];
  const escapedKey = escapeRedisValue(key);
  
  // Create maps for easier comparison
  const oldMap = new Map(oldSortedSet.map(item => [item.member, item.score]));
  const newMap = new Map(newSortedSet.map(item => [item.member, item.score]));
  
  // Find members to add or update (ZADD handles both)
  const toAddOrUpdate: Array<{member: string, score: number}> = [];
  for (const item of newSortedSet) {
    const oldScore = oldMap.get(item.member);
    if (oldScore === undefined || oldScore !== item.score) {
      toAddOrUpdate.push(item);
    }
  }
  
  if (toAddOrUpdate.length > 0) {
    // ZADD command format: ZADD key score1 member1 score2 member2 ...
    const scoreMemberPairs = toAddOrUpdate
      .map(item => `${item.score} ${escapeRedisValue(item.member)}`)
      .join(' ');
    commands.push(`ZADD ${escapedKey} ${scoreMemberPairs}`);
  }
  
  // Find members to remove
  const toRemove = oldSortedSet
    .map(item => item.member)
    .filter(member => !newMap.has(member));
  
  if (toRemove.length > 0) {
    const members = toRemove.map(member => escapeRedisValue(member)).join(' ');
    commands.push(`ZREM ${escapedKey} ${members}`);
  }
  
  return commands;
}

/**
 * Generate JSONPath expression for nested object access
 */
function generateJSONPath(pathArray: string[]): string {
  if (pathArray.length === 0) return '$';
  
  let path = '$';
  
  for (const segment of pathArray) {
    // Handle array indices - add [index] directly without dot
    if (/^\d+$/.test(segment)) {
      path += `[${segment}]`;
    }
    // Handle regular property names - add .property
    else if (/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(segment)) {
      path += `.${segment}`;
    }
    // Handle property names with special characters - add ["property"]
    else {
      path += `["${segment.replace(/"/g, '\\"')}"]`;
    }
  }
  
  return path;
}

/**
 * Escape JSON value for RedisJSON commands
 * Formats values to match Redis CLI format: '"string value"' for strings
 */
function escapeJSONValue(value: any): string {
  if (value === null) return 'null';
  if (value === undefined) return 'null';
  if (typeof value === 'boolean') return value.toString();
  if (typeof value === 'number') return value.toString();
  if (typeof value === 'string') {
    // Create JSON string and wrap in single quotes for command-line safety
    // This matches the format: JSON.SET user:1000 $.address.street '"125 Main St"'
    const jsonString = `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
    return `'${jsonString}'`;
  }
  // For objects and arrays, wrap JSON.stringify result in single quotes
  const jsonValue = JSON.stringify(value);
  return `'${jsonValue}'`;
}

/**
 * Find differences between two JSON objects and generate change operations
 */
interface JSONChange {
  type: 'set' | 'delete';
  path: string[];
  value?: any;
  oldValue?: any;
}

function findJSONDifferences(oldObj: any, newObj: any, currentPath: string[] = []): JSONChange[] {
  const changes: JSONChange[] = [];
  
  // Handle type changes at the current path
  if (typeof oldObj !== typeof newObj || 
      Array.isArray(oldObj) !== Array.isArray(newObj) ||
      (oldObj === null) !== (newObj === null)) {
    
    if (newObj === undefined) {
      // Value was deleted
      changes.push({
        type: 'delete',
        path: currentPath,
        oldValue: oldObj
      });
    } else {
      // Value was set/changed
      changes.push({
        type: 'set',
        path: currentPath,
        value: newObj,
        oldValue: oldObj
      });
    }
    return changes;
  }
  
  // Handle primitive values
  if (typeof newObj !== 'object' || newObj === null) {
    if (oldObj !== newObj) {
      changes.push({
        type: 'set',
        path: currentPath,
        value: newObj,
        oldValue: oldObj
      });
    }
    return changes;
  }
  
  // Handle arrays
  if (Array.isArray(newObj)) {
    const oldArray = Array.isArray(oldObj) ? oldObj : [];
    const maxLength = Math.max(oldArray.length, newObj.length);
    
    for (let i = 0; i < maxLength; i++) {
      const oldItem = i < oldArray.length ? oldArray[i] : undefined;
      const newItem = i < newObj.length ? newObj[i] : undefined;
      
      if (newItem === undefined) {
        // Item was removed - for arrays, we'll handle this by recreating the array
        // since JSON.DEL on array indices can be complex
        continue;
      } else if (oldItem === undefined) {
        // Item was added
        changes.push(...findJSONDifferences(undefined, newItem, [...currentPath, i.toString()]));
      } else {
        // Item potentially changed
        changes.push(...findJSONDifferences(oldItem, newItem, [...currentPath, i.toString()]));
      }
    }
    
    // If array length changed, we might need to recreate the entire array
    if (oldArray.length !== newObj.length) {
      // For simplicity, recreate the entire array when length changes
      changes.length = 0; // Clear individual changes
      changes.push({
        type: 'set',
        path: currentPath,
        value: newObj,
        oldValue: oldObj
      });
    }
    
    return changes;
  }
  
  // Handle objects
  const oldObject = (typeof oldObj === 'object' && oldObj !== null) ? oldObj : {};
  const allKeys = new Set([...Object.keys(oldObject), ...Object.keys(newObj)]);
  
  for (const key of allKeys) {
    const oldValue = oldObject[key];
    const newValue = newObj[key];
    
    if (!(key in newObj)) {
      // Property was deleted
      changes.push({
        type: 'delete',
        path: [...currentPath, key],
        oldValue: oldValue
      });
    } else if (!(key in oldObject)) {
      // Property was added
      changes.push({
        type: 'set',
        path: [...currentPath, key],
        value: newValue
      });
    } else {
      // Property potentially changed
      changes.push(...findJSONDifferences(oldValue, newValue, [...currentPath, key]));
    }
  }
  
  return changes;
}

/**
 * Generate RedisJSON commands for JSON data operations
 * Uses JSON.SET and JSON.DEL for granular updates instead of replacing entire document
 */
export function generateJSONCommands(
  key: string,
  oldJSON: any = {},
  newJSON: any = {}
): string[] {
  const commands: string[] = [];
  const escapedKey = escapeRedisValue(key);
  
  // If we're starting with no data, set the entire document
  if (oldJSON == null || (typeof oldJSON === 'object' && Object.keys(oldJSON).length === 0)) {
    const jsonValue = escapeJSONValue(newJSON);
    commands.push(`JSON.SET ${escapedKey} $ ${jsonValue}`);
    return commands;
  }
  
  // If the new value is empty/null, delete the entire key
  if (newJSON == null || (typeof newJSON === 'object' && Object.keys(newJSON).length === 0)) {
    commands.push(`DEL ${escapedKey}`);
    return commands;
  }
  
  // Find differences and generate granular updates
  const changes = findJSONDifferences(oldJSON, newJSON);
  
  // If there are too many changes (> 50% of properties), it might be more efficient
  // to replace the entire document
  const totalProperties = Math.max(
    typeof oldJSON === 'object' ? Object.keys(oldJSON || {}).length : 1,
    typeof newJSON === 'object' ? Object.keys(newJSON || {}).length : 1
  );
  
  if (changes.length > totalProperties * 0.5) {
    // Replace entire document if too many changes
    const jsonValue = escapeJSONValue(newJSON);
    commands.push(`JSON.SET ${escapedKey} $ ${jsonValue}`);
    return commands;
  }
  
  // Sort changes: deletions first, then sets (to avoid conflicts)
  const deletions = changes.filter(c => c.type === 'delete');
  const sets = changes.filter(c => c.type === 'set');
  
  // Generate JSON.DEL commands for deletions
  for (const change of deletions) {
    const jsonPath = generateJSONPath(change.path);
    commands.push(`JSON.DEL ${escapedKey} ${jsonPath}`);
  }
  
  // Generate JSON.SET commands for additions/modifications
  for (const change of sets) {
    const jsonPath = generateJSONPath(change.path);
    const jsonValue = escapeJSONValue(change.value);
    commands.push(`JSON.SET ${escapedKey} ${jsonPath} ${jsonValue}`);
  }
  
  return commands;
}

/**
 * Generate Bloom Filter commands for BF.ADD, BF.MADD, and BF.INSERT operations
 */
export function generateBloomFilterCommands(
  key: string,
  oldValue: any = {},
  newValue: any = {}
): string[] {
  const commands: string[] = [];
  const escapedKey = escapeRedisValue(key);

  console.log(oldValue) //// NORBERT 9-10 //

  // Check if newValue has the bloom filter operations structure
  if (newValue && typeof newValue === 'object' && newValue.type === 'bloom_filter' && newValue.operations) {
    const operations = newValue.operations;
    
    for (const operation of operations) {
      const { type, items, options } = operation;
      
      // Filter out empty items
      const validItems = items.filter((item: string) => item && item.trim());
      if (validItems.length === 0) continue;
      
      switch (type) {
        case 'BF.ADD':
          // BF.ADD key item
          for (const item of validItems) {
            commands.push(`BF.ADD ${escapedKey} ${escapeRedisValue(item)}`);
          }
          break;
          
        case 'BF.MADD':
          // BF.MADD key item1 item2 item3 ...
          if (validItems.length > 0) {
            const escapedItems = validItems.map((item: string) => escapeRedisValue(item)).join(' ');
            commands.push(`BF.MADD ${escapedKey} ${escapedItems}`);
          }
          break;
          
        case 'BF.INSERT':
          // BF.INSERT key [CAPACITY capacity] [ERROR error] [EXPANSION expansion] [NONSCALING] ITEMS item1 item2 ...
          let insertCommand = `BF.INSERT ${escapedKey}`;
          
          // Add optional parameters
          if (options?.capacity) {
            insertCommand += ` CAPACITY ${options.capacity}`;
          }
          if (options?.error) {
            insertCommand += ` ERROR ${options.error}`;
          }
          if (options?.expansion) {
            insertCommand += ` EXPANSION ${options.expansion}`;
          }
          if (options?.nonscaling) {
            insertCommand += ` NONSCALING`;
          }
          
          // Add items
          if (validItems.length > 0) {
            const escapedItems = validItems.map((item: string) => escapeRedisValue(item)).join(' ');
            insertCommand += ` ITEMS ${escapedItems}`;
            commands.push(insertCommand);
          }
          break;
          
        case 'BF.EXISTS':
          // BF.EXISTS key item
          for (const item of validItems) {
            commands.push(`BF.EXISTS ${escapedKey} ${escapeRedisValue(item)}`);
          }
          break;
          
        case 'BF.MEXISTS':
          // BF.MEXISTS key item1 item2 item3 ...
          if (validItems.length > 0) {
            const escapedItems = validItems.map((item: string) => escapeRedisValue(item)).join(' ');
            commands.push(`BF.MEXISTS ${escapedKey} ${escapedItems}`);
          }
          break;
      }
    }
    
    return commands;
  }
  
  // Fallback: if the value is an array, treat it as items for BF.MADD
  if (Array.isArray(newValue) && newValue.length > 0) {
    const validItems = newValue.filter(item => item && String(item).trim());
    if (validItems.length > 0) {
      const escapedItems = validItems.map(item => escapeRedisValue(String(item))).join(' ');
      commands.push(`BF.MADD ${escapedKey} ${escapedItems}`);
    }
    return commands;
  }
  
  // Fallback: if the value is a string, treat it as a single item for BF.ADD
  if (typeof newValue === 'string' && newValue.trim()) {
    commands.push(`BF.ADD ${escapedKey} ${escapeRedisValue(newValue)}`);
    return commands;
  }
  
  // If no valid operations, return empty array
  return commands;
}

/**
 * Main function to generate commands based on data type
 */
export function generateRedisCommands(
  key: string,
  dataType: string,
  oldValue: any,
  newValue: any
): string[] {
  const normalizedType = dataType?.toLowerCase() || 'string';
  
  switch (normalizedType) {
    case 'hash':
      return generateHashCommands(key, oldValue || {}, newValue || {});
    
    case 'list':
      // Use optimized commands for better performance
      return generateOptimizedListCommands(key, oldValue || [], newValue || []);
    
    case 'set':
      return generateSetCommands(key, oldValue || [], newValue || []);
    
    case 'zset':
    case 'sortedset':
      // Use sorted set commands for ZADD/ZREM operations
      return generateSortedSetCommands(key, oldValue || [], newValue || []);
    
    case 'json':
      // Use RedisJSON commands for granular JSON updates
      return generateJSONCommands(key, oldValue, newValue);
    
    case 'bloomfltr':
    case 'bloom_filter':
    case 'bloom':
      // Use Bloom Filter commands for BF.ADD, BF.MADD, BF.INSERT operations
      return generateBloomFilterCommands(key, oldValue, newValue);
    
    case 'string':
    default:
      // For strings and other types, we'll use the SET command
      return [`SET ${escapeRedisValue(key)} ${escapeRedisValue(newValue)}`];
  }
}

/**
 * Utility function to check if two values are deeply equal
 */
export function deepEqual(a: any, b: any): boolean {
  if (a === b) return true;
  
  if (a == null || b == null) return false;
  
  if (typeof a !== typeof b) return false;
  
  if (typeof a === 'object') {
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    
    if (Array.isArray(a)) {
      if (a.length !== b.length) return false;
      for (let i = 0; i < a.length; i++) {
        if (!deepEqual(a[i], b[i])) return false;
      }
      return true;
    } else {
      const aKeys = Object.keys(a);
      const bKeys = Object.keys(b);
      if (aKeys.length !== bKeys.length) return false;
      
      for (let key of aKeys) {
        if (!(key in b)) return false;
        if (!deepEqual(a[key], b[key])) return false;
      }
      return true;
    }
  }
  
  return false;
}
