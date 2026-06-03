import { useState, useEffect, useRef } from 'react';
import './ConversationSidebar.css';

interface ConversationSidebarProps {
  conversationIds: string[];
  activeConversationId: string;
  conversationNames: Map<string, string>;
  onSelectConversation: (id: string) => void;
  onNewChat: () => void;
  showHumanChat?: boolean;
  hasUnreadHumanMessages?: boolean;
  onSelectHumanChat?: () => void;
  viewingHumanChat?: boolean;
  fullCacheMode: 'off' | 'hot' | 'new';
  onFullCacheModeChange: (mode: 'off' | 'hot' | 'new') => void;
  fullCacheThreshold: number;
  onFullCacheThresholdChange: (v: number) => void;
  kbCacheMode: 'off' | 'hot' | 'new';
  onKbCacheModeChange: (mode: 'off' | 'hot' | 'new') => void;
  kbCacheThreshold: number;
  onKbCacheThresholdChange: (v: number) => void;
  userId: string;
  onUserIdChange: (userId: string) => void;
  shortTermMemoryEnabled: boolean;
  onShortTermMemoryChange: (enabled: boolean) => void;
  longTermMemoryEnabled: boolean;
  onLongTermMemoryChange: (enabled: boolean) => void;
  onUserSelected: (userId: string) => void;
}

export default function ConversationSidebar({
  conversationIds,
  activeConversationId,
  conversationNames,
  onSelectConversation,
  onNewChat,
  showHumanChat = false,
  hasUnreadHumanMessages = false,
  onSelectHumanChat,
  viewingHumanChat = false,
  fullCacheMode,
  onFullCacheModeChange,
  fullCacheThreshold,
  onFullCacheThresholdChange,
  kbCacheMode,
  onKbCacheModeChange,
  kbCacheThreshold,
  onKbCacheThresholdChange,
  userId,
  onUserIdChange,
  shortTermMemoryEnabled,
  onShortTermMemoryChange,
  longTermMemoryEnabled,
  onLongTermMemoryChange,
  onUserSelected,
}: ConversationSidebarProps) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [showUserDropdown, setShowUserDropdown] = useState(false);
  const [knownUsers, setKnownUsers] = useState<string[]>([]);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Fetch known users when dropdown opens
  const fetchKnownUsers = async () => {
    if (knownUsers.length > 0) return; // already loaded
    setLoadingUsers(true);
    try {
      const res = await fetch('/api/known-users');
      const data = await res.json();
      setKnownUsers(data.users || []);
    } catch (e) {
      console.error('Failed to fetch known users:', e);
    } finally {
      setLoadingUsers(false);
    }
  };

  // Close dropdown on outside click
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowUserDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleSelectUser = (selectedUserId: string) => {
    onUserIdChange(selectedUserId);
    onLongTermMemoryChange(true);
    setShowUserDropdown(false);
    onUserSelected(selectedUserId);
  };

  const handleUserInputKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && userId.trim()) {
      if (!longTermMemoryEnabled) onLongTermMemoryChange(true);
      setShowUserDropdown(false);
      onUserSelected(userId.trim());
    }
  };

  const flushCache = async (mode: string) => {
    try {
      await fetch('/api/flush-cache', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
    } catch (e) {
      console.error('Flush failed:', e);
    }
  };

  return (
    <div className={`conversation-sidebar ${isCollapsed ? 'collapsed' : ''}`}>
      <button
        className="sidebar-toggle"
        onClick={() => setIsCollapsed(!isCollapsed)}
        aria-label={isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        {isCollapsed ? '▶' : '◀'}
      </button>

      {!isCollapsed && (
        <div className="sidebar-content">
          <button className="new-chat-button" onClick={onNewChat}>
            + New Chat
          </button>

          {showHumanChat && onSelectHumanChat && (
            <div className="human-chat-section-wrapper">
              <button
                className={`human-chat-section ${viewingHumanChat ? 'active' : ''} ${hasUnreadHumanMessages ? 'unread' : ''}`}
                onClick={onSelectHumanChat}
              >
                <div className="human-chat-icon">👤</div>
                <div className="human-chat-label">Human chat</div>
                {hasUnreadHumanMessages && <div className="unread-indicator"></div>}
              </button>
            </div>
          )}

          <div className="conversations-list">
            <h3 className="conversations-header">Conversations</h3>
            {conversationIds.map((id) => (
              <button
                key={id}
                className={`conversation-item ${!viewingHumanChat && id === activeConversationId ? 'active' : ''}`}
                onClick={() => onSelectConversation(id)}
                title={id}
              >
                <div className="conversation-id">
                  {conversationNames.get(id) || 'New chat'}
                </div>
              </button>
            ))}
          </div>

          {/* Memory System */}
          <div className="cache-toggle-section">
            <div className="cache-toggle-name">🧠 Memory System</div>
            
            {/* User ID Input — combo dropdown */}
            <div className="user-id-input-wrapper" ref={dropdownRef}>
              <div className="user-id-combo">
                <input
                  type="text"
                  value={userId}
                  onChange={(e) => onUserIdChange(e.target.value)}
                  onFocus={() => { setShowUserDropdown(true); fetchKnownUsers(); }}
                  onKeyDown={handleUserInputKeyDown}
                  placeholder="Enter or select User ID"
                  className="user-id-input"
                />
                <button
                  className="user-id-dropdown-toggle"
                  onClick={() => { setShowUserDropdown(!showUserDropdown); fetchKnownUsers(); }}
                  aria-label="Show known users"
                >
                  ▾
                </button>
              </div>
              {showUserDropdown && (
                <div className="user-id-dropdown">
                  {loadingUsers ? (
                    <div className="user-id-dropdown-item loading">Loading users...</div>
                  ) : knownUsers.length === 0 ? (
                    <div className="user-id-dropdown-item empty">No previous users found</div>
                  ) : (
                    knownUsers
                      .filter(u => !userId || u.toLowerCase().includes(userId.toLowerCase()))
                      .map(u => (
                        <button
                          key={u}
                          className={`user-id-dropdown-item ${u === userId ? 'selected' : ''}`}
                          onClick={() => handleSelectUser(u)}
                        >
                          {u}
                        </button>
                      ))
                  )}
                </div>
              )}
            </div>

            {/* Short-term Memory Toggle */}
            <div className="memory-toggle-row">
              <label className="memory-toggle-label">
                <input
                  type="checkbox"
                  checked={shortTermMemoryEnabled}
                  onChange={(e) => onShortTermMemoryChange(e.target.checked)}
                  className="memory-checkbox"
                />
                <span className="memory-label-text">Short-term (Session)</span>
              </label>
              <span className="memory-info">30d TTL</span>
            </div>

            {/* Long-term Memory Toggle */}
            <div className="memory-toggle-row">
              <label className="memory-toggle-label">
                <input
                  type="checkbox"
                  checked={longTermMemoryEnabled}
                  onChange={(e) => onLongTermMemoryChange(e.target.checked)}
                  disabled={!userId}
                  className="memory-checkbox"
                />
                <span className="memory-label-text">Long-term (User)</span>
              </label>
              <span className="memory-info">90d TTL</span>
            </div>

            {longTermMemoryEnabled && !userId && (
              <div className="memory-warning">
                ⚠️ Enter User ID to enable long-term memory
              </div>
            )}
          </div>

          {/* Cache Toggles */}
          <div className="cache-toggle-section">
            {/* Full App Cache */}
            <div className="cache-toggle-name">Full App Cache</div>
            <div className="cache-mode-selector">
              <button className={`cache-mode-btn ${fullCacheMode === 'off' ? 'active' : ''}`} onClick={() => onFullCacheModeChange('off')}>Off</button>
              <button className={`cache-mode-btn ${fullCacheMode === 'hot' ? 'active' : ''}`} onClick={() => onFullCacheModeChange('hot')}>🔥 Hot</button>
              <button className={`cache-mode-btn ${fullCacheMode === 'new' ? 'active cold' : ''}`} onClick={() => { flushCache('full'); onFullCacheModeChange('new'); }}>❄️ New</button>
            </div>
            {fullCacheMode !== 'off' && (
              <div className="cache-threshold-row">
                <span className="cache-threshold-label">Similarity: {fullCacheThreshold.toFixed(2)}</span>
                <input type="range" min="0" max="1" step="0.01" value={fullCacheThreshold}
                  onChange={e => onFullCacheThresholdChange(parseFloat(e.target.value))}
                  className="cache-threshold-slider" />
              </div>
            )}

            {/* KB Cache */}
            <div className="cache-toggle-name" style={{marginTop: '0.75rem'}}>Knowledge Base Cache</div>
            <div className="cache-mode-selector">
              <button className={`cache-mode-btn ${kbCacheMode === 'off' ? 'active' : ''}`} onClick={() => onKbCacheModeChange('off')}>Off</button>
              <button className={`cache-mode-btn ${kbCacheMode === 'hot' ? 'active' : ''}`} onClick={() => onKbCacheModeChange('hot')}>🔥 Hot</button>
              <button className={`cache-mode-btn ${kbCacheMode === 'new' ? 'active cold' : ''}`} onClick={() => { flushCache('subagent'); onKbCacheModeChange('new'); }}>❄️ New</button>
            </div>
            {kbCacheMode !== 'off' && (
              <div className="cache-threshold-row">
                <span className="cache-threshold-label">Similarity: {kbCacheThreshold.toFixed(2)}</span>
                <input type="range" min="0" max="1" step="0.01" value={kbCacheThreshold}
                  onChange={e => onKbCacheThresholdChange(parseFloat(e.target.value))}
                  className="cache-threshold-slider" />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
