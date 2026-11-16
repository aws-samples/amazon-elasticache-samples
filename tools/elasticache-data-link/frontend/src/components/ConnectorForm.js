import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './Form.css';
import API_URL from '../apiUrl';


function FilterBuilder({ filters, onChange }) {
  const addFilter = () => onChange([...filters, { table: '', field: '', operator: '=', value: '' }]);
  const removeFilter = (i) => onChange(filters.filter((_, idx) => idx !== i));
  const updateFilter = (i, key, val) => {
    const updated = [...filters];
    updated[i] = { ...updated[i], [key]: val };
    onChange(updated);
  };

  return (
    <div style={{ border: '1px solid #e0e0e0', borderRadius: '6px', padding: '10px', background: '#fafafa' }}>
      {filters.map((f, i) => (
        <div key={i} style={{ display: 'flex', gap: '8px', marginBottom: '6px', alignItems: 'center' }}>
          <input type="text" value={f.table || ''} placeholder="table name (required)"
            onChange={(e) => updateFilter(i, 'table', e.target.value)}
            style={{ width: '120px', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
          <input type="text" value={f.field} placeholder="column"
            onChange={(e) => updateFilter(i, 'field', e.target.value)}
            style={{ flex: 1, padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
          <select value={f.operator} onChange={(e) => updateFilter(i, 'operator', e.target.value)}
            style={{ padding: '6px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }}>
            <option value="=">=</option>
            <option value="!=">!=</option>
            <option value=">">&gt;</option>
            <option value="<">&lt;</option>
            <option value=">=">&gt;=</option>
            <option value="<=">&lt;=</option>
            <option value="contains">contains</option>
            <option value="not_contains">not contains</option>
            <option value="in">in (comma-sep)</option>
            <option value="not_in">not in (comma-sep)</option>
          </select>
          <input type="text" value={f.value} placeholder="value"
            onChange={(e) => updateFilter(i, 'value', e.target.value)}
            style={{ flex: 1, padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
          <button type="button" onClick={() => removeFilter(i)}
            style={{ background: '#e74c3c', color: 'white', border: 'none', borderRadius: '4px',
              padding: '4px 8px', cursor: 'pointer', fontSize: '13px' }}>✕</button>
        </div>
      ))}
      <button type="button" onClick={addFilter}
        style={{ background: 'none', border: '1px dashed #aaa', borderRadius: '4px',
          padding: '6px 12px', cursor: 'pointer', fontSize: '12px', color: '#666', width: '100%' }}>
        + Add Filter
      </button>
      {filters.length === 0 && (
        <p style={{ fontSize: '12px', color: '#999', margin: '6px 0 0 0', textAlign: 'center' }}>
          No filters — all rows will be synced
        </p>
      )}
    </div>
  );
}

function ColumnSelector({ columns, onChange }) {
  const tables = Object.keys(columns);
  const addTable = () => {
    const name = prompt('Table name:');
    if (name && !columns[name]) onChange({ ...columns, [name]: [] });
  };
  const removeTable = (t) => {
    const updated = { ...columns };
    delete updated[t];
    onChange(updated);
  };
  const updateColumns = (t, val) => onChange({ ...columns, [t]: val.split(',').map(c => c.trim()).filter(Boolean) });

  return (
    <div style={{ border: '1px solid #e0e0e0', borderRadius: '6px', padding: '10px', background: '#fafafa' }}>
      {tables.map(t => (
        <div key={t} style={{ display: 'flex', gap: '8px', marginBottom: '6px', alignItems: 'center' }}>
          <span style={{ width: '120px', fontSize: '13px', fontWeight: 600 }}>{t}</span>
          <input type="text" value={(columns[t] || []).join(', ')} placeholder="col1, col2, col3"
            onChange={(e) => updateColumns(t, e.target.value)}
            style={{ flex: 1, padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
          <button type="button" onClick={() => removeTable(t)}
            style={{ background: '#e74c3c', color: 'white', border: 'none', borderRadius: '4px', padding: '4px 8px', cursor: 'pointer', fontSize: '13px' }}>✕</button>
        </div>
      ))}
      <button type="button" onClick={addTable}
        style={{ background: 'none', border: '1px dashed #aaa', borderRadius: '4px', padding: '6px 12px', cursor: 'pointer', fontSize: '12px', color: '#666', width: '100%' }}>
        + Add Table
      </button>
      {tables.length === 0 && <p style={{ fontSize: '12px', color: '#999', margin: '6px 0 0', textAlign: 'center' }}>No column selection — all columns synced</p>}
    </div>
  );
}

function ComputedFieldsBuilder({ fields, onChange }) {
  const add = () => onChange([...fields, { table: '', name: '', expression: '' }]);
  const remove = (i) => onChange(fields.filter((_, idx) => idx !== i));
  const update = (i, key, val) => { const u = [...fields]; u[i] = { ...u[i], [key]: val }; onChange(u); };

  return (
    <div style={{ border: '1px solid #e0e0e0', borderRadius: '6px', padding: '10px', background: '#fafafa' }}>
      {fields.map((f, i) => (
        <div key={i} style={{ display: 'flex', gap: '8px', marginBottom: '6px', alignItems: 'center' }}>
          <input type="text" value={f.table} placeholder="table"
            onChange={(e) => update(i, 'table', e.target.value)}
            style={{ width: '120px', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
          <input type="text" value={f.name} placeholder="new field name"
            onChange={(e) => update(i, 'name', e.target.value)}
            style={{ width: '140px', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
          <input type="text" value={f.expression} placeholder="expression (e.g. price * quantity)"
            onChange={(e) => update(i, 'expression', e.target.value)}
            style={{ flex: 1, padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px', fontFamily: 'monospace' }} />
          <button type="button" onClick={() => remove(i)}
            style={{ background: '#e74c3c', color: 'white', border: 'none', borderRadius: '4px', padding: '4px 8px', cursor: 'pointer', fontSize: '13px' }}>✕</button>
        </div>
      ))}
      <button type="button" onClick={add}
        style={{ background: 'none', border: '1px dashed #aaa', borderRadius: '4px', padding: '6px 12px', cursor: 'pointer', fontSize: '12px', color: '#666', width: '100%' }}>
        + Add Computed Field
      </button>
      {fields.length === 0 && <p style={{ fontSize: '12px', color: '#999', margin: '6px 0 0', textAlign: 'center' }}>No computed fields</p>}
    </div>
  );
}

function AggregationsBuilder({ aggregations, onChange }) {
  const add = () => onChange([...aggregations, { table: '', function: 'count', field: '', group_by: '', target_key: '' }]);
  const remove = (i) => onChange(aggregations.filter((_, idx) => idx !== i));
  const update = (i, key, val) => { const u = [...aggregations]; u[i] = { ...u[i], [key]: val }; onChange(u); };

  return (
    <div style={{ border: '1px solid #e0e0e0', borderRadius: '6px', padding: '10px', background: '#fafafa' }}>
      {aggregations.map((a, i) => (
        <div key={i} style={{ marginBottom: '8px', padding: '8px', background: '#fff', borderRadius: '4px', border: '1px solid #eee' }}>
          <div style={{ display: 'flex', gap: '8px', marginBottom: '6px', alignItems: 'center' }}>
            <input type="text" value={a.table} placeholder="table"
              onChange={(e) => update(i, 'table', e.target.value)}
              style={{ width: '120px', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            <select value={a.function} onChange={(e) => update(i, 'function', e.target.value)}
              style={{ padding: '6px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }}>
              <option value="count">COUNT</option>
              <option value="sum">SUM</option>
              <option value="avg">AVG</option>
              <option value="min">MIN</option>
              <option value="max">MAX</option>
            </select>
            <input type="text" value={a.field} placeholder="field (for sum/avg/min/max)"
              onChange={(e) => update(i, 'field', e.target.value)}
              style={{ flex: 1, padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            <button type="button" onClick={() => remove(i)}
              style={{ background: '#e74c3c', color: 'white', border: 'none', borderRadius: '4px', padding: '4px 8px', cursor: 'pointer', fontSize: '13px' }}>✕</button>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <input type="text" value={a.group_by} placeholder="group by field (optional)"
              onChange={(e) => update(i, 'group_by', e.target.value)}
              style={{ flex: 1, padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            <input type="text" value={a.target_key} placeholder="Valkey key (e.g. orders:total:{category})"
              onChange={(e) => update(i, 'target_key', e.target.value)}
              style={{ flex: 1, padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px', fontFamily: 'monospace' }} />
          </div>
        </div>
      ))}
      <button type="button" onClick={add}
        style={{ background: 'none', border: '1px dashed #aaa', borderRadius: '4px', padding: '6px 12px', cursor: 'pointer', fontSize: '12px', color: '#666', width: '100%' }}>
        + Add Aggregation
      </button>
      {aggregations.length === 0 && <p style={{ fontSize: '12px', color: '#999', margin: '6px 0 0', textAlign: 'center' }}>No aggregations — rows written individually</p>}
    </div>
  );
}

function DenormalizeBuilder({ rules, onChange }) {
  const add = () => onChange([...rules, { child_table: '', parent_table: '', join_field: '', fields: '', prefix: '', on_update: 'merge' }]);
  const remove = (i) => onChange(rules.filter((_, idx) => idx !== i));
  const update = (i, key, val) => { const u = [...rules]; u[i] = { ...u[i], [key]: val }; onChange(u); };

  return (
    <div style={{ border: '1px solid #e0e0e0', borderRadius: '6px', padding: '10px', background: '#fafafa' }}>
      {rules.map((r, i) => (
        <div key={i} style={{ marginBottom: '10px', padding: '10px', background: '#fff', borderRadius: '4px', border: '1px solid #eee' }}>
          <div style={{ display: 'flex', gap: '8px', marginBottom: '6px', alignItems: 'center' }}>
            <div style={{ flex: 1 }}>
              <label style={{ fontSize: '11px', color: '#666', display: 'block', marginBottom: '2px' }}>Child Table</label>
              <input type="text" value={r.child_table} placeholder="addresses"
                onChange={(e) => update(i, 'child_table', e.target.value)}
                style={{ width: '100%', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ fontSize: '11px', color: '#666', display: 'block', marginBottom: '2px' }}>Parent Table</label>
              <input type="text" value={r.parent_table} placeholder="customers"
                onChange={(e) => update(i, 'parent_table', e.target.value)}
                style={{ width: '100%', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ fontSize: '11px', color: '#666', display: 'block', marginBottom: '2px' }}>Join Field (FK in child)</label>
              <input type="text" value={r.join_field} placeholder="customer_id"
                onChange={(e) => update(i, 'join_field', e.target.value)}
                style={{ width: '100%', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            </div>
            <button type="button" onClick={() => remove(i)}
              style={{ background: '#e74c3c', color: 'white', border: 'none', borderRadius: '4px', padding: '4px 8px', cursor: 'pointer', fontSize: '13px', marginTop: '16px' }}>✕</button>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <div style={{ flex: 2 }}>
              <label style={{ fontSize: '11px', color: '#666', display: 'block', marginBottom: '2px' }}>Fields to Merge (comma-separated)</label>
              <input type="text" value={r.fields} placeholder="street, city, state, zip"
                onChange={(e) => update(i, 'fields', e.target.value)}
                style={{ width: '100%', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ fontSize: '11px', color: '#666', display: 'block', marginBottom: '2px' }}>Field Prefix</label>
              <input type="text" value={r.prefix} placeholder="address_"
                onChange={(e) => update(i, 'prefix', e.target.value)}
                style={{ width: '100%', padding: '6px 8px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }} />
            </div>
            <div style={{ width: '100px' }}>
              <label style={{ fontSize: '11px', color: '#666', display: 'block', marginBottom: '2px' }}>Strategy</label>
              <select value={r.on_update || 'merge'} onChange={(e) => update(i, 'on_update', e.target.value)}
                style={{ width: '100%', padding: '6px', border: '1px solid #ddd', borderRadius: '4px', fontSize: '13px' }}>
                <option value="merge">Merge</option>
                <option value="replace">Replace</option>
              </select>
            </div>
          </div>
        </div>
      ))}
      <button type="button" onClick={add}
        style={{ background: 'none', border: '1px dashed #aaa', borderRadius: '4px', padding: '6px 12px', cursor: 'pointer', fontSize: '12px', color: '#666', width: '100%' }}>
        + Add Denormalization Rule
      </button>
      {rules.length === 0 && <p style={{ fontSize: '12px', color: '#999', margin: '6px 0 0', textAlign: 'center' }}>No denormalization — each table writes to its own keys</p>}
    </div>
  );
}

function TransformTabs({ formData, setFormData }) {
  const [activeTab, setActiveTab] = useState('filters');
  const getTransforms = () => { try { return JSON.parse(formData.transforms || '{}'); } catch { return {}; } };
  const setTransforms = (key, value) => {
    const existing = getTransforms();
    existing[key] = value;
    setFormData(prev => ({ ...prev, transforms: JSON.stringify(existing) }));
  };
  const t = getTransforms();
  const tabs = [
    { id: 'filters', label: 'Row Filters', count: (t.row_filters || []).length },
    { id: 'columns', label: 'Columns', count: Object.keys(t.columns || {}).length },
    { id: 'computed', label: 'Computed Fields', count: (t.computed_fields || []).length },
    { id: 'aggregations', label: 'Aggregations', count: (t.aggregations || []).length },
    { id: 'denormalize', label: 'Denormalize', count: (t.denormalize || []).length },
  ];

  return (
    <div>
      <div style={{ display: 'flex', gap: '2px', marginBottom: '10px', flexWrap: 'wrap' }}>
        {tabs.map(tab => (
          <button key={tab.id} type="button" onClick={() => setActiveTab(tab.id)}
            style={{
              padding: '8px 14px', border: '1px solid #ddd', borderBottom: activeTab === tab.id ? '2px solid #3498db' : '1px solid #ddd',
              background: activeTab === tab.id ? '#fff' : '#f5f5f5', cursor: 'pointer', fontSize: '13px',
              fontWeight: activeTab === tab.id ? 600 : 400, borderRadius: '4px 4px 0 0', color: activeTab === tab.id ? '#3498db' : '#666'
            }}>
            {tab.label} {tab.count > 0 && <span style={{ background: '#3498db', color: '#fff', borderRadius: '10px', padding: '1px 6px', fontSize: '11px', marginLeft: '4px' }}>{tab.count}</span>}
          </button>
        ))}
      </div>
      {activeTab === 'filters' && (
        <div>
          <p style={{ fontSize: '12px', color: '#666', margin: '0 0 8px 0' }}>Only sync rows matching these conditions.</p>
          <FilterBuilder filters={t.row_filters || []} onChange={(v) => setTransforms('row_filters', v)} />
        </div>
      )}
      {activeTab === 'columns' && (
        <div>
          <p style={{ fontSize: '12px', color: '#666', margin: '0 0 8px 0' }}>Select which columns to sync per table. Empty = all columns.</p>
          <ColumnSelector columns={t.columns || {}} onChange={(v) => setTransforms('columns', v)} />
        </div>
      )}
      {activeTab === 'computed' && (
        <div>
          <p style={{ fontSize: '12px', color: '#666', margin: '0 0 8px 0' }}>Add calculated fields using expressions (e.g. price * quantity).</p>
          <ComputedFieldsBuilder fields={t.computed_fields || []} onChange={(v) => setTransforms('computed_fields', v)} />
        </div>
      )}
      {activeTab === 'aggregations' && (
        <div>
          <p style={{ fontSize: '12px', color: '#666', margin: '0 0 8px 0' }}>Aggregate data (count, sum, avg, min, max) and write summaries to Valkey.</p>
          <AggregationsBuilder aggregations={t.aggregations || []} onChange={(v) => setTransforms('aggregations', v)} />
        </div>
      )}
      {activeTab === 'denormalize' && (
        <div>
          <p style={{ fontSize: '12px', color: '#666', margin: '0 0 8px 0' }}>Join related tables into a single Valkey key. Child table fields merge into the parent key.</p>
          <DenormalizeBuilder rules={t.denormalize || []} onChange={(v) => setTransforms('denormalize', v)} />
        </div>
      )}
    </div>
  );
}

function ConnectorForm({ connector, onSave, onCancel }) {
  const [formData, setFormData] = useState({
    name: '', source_id: '', connector_type: 'debezium-postgres',
    tables: '', snapshot_mode: 'initial', slot_name: '',
    publication_name: '', transforms: '', config_json: '', status: 'new',
    tls_to_source: 1, tls_to_kafka: 0
  });
  const [sources, setSources] = useState([]);
  const [scanning, setScanning] = useState(false);
  const [showTablePicker, setShowTablePicker] = useState(false);
  const [availableTables, setAvailableTables] = useState([]);
  const [selectedTables, setSelectedTables] = useState(new Set());
  const [scanError, setScanError] = useState('');

  useEffect(() => {
    fetchSources();
    if (connector) setFormData(connector);
  }, [connector]);

  const fetchSources = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/sources`);
      setSources(res.data);
    } catch (e) { console.error('Error fetching sources:', e); }
  };

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e) => { e.preventDefault(); onSave(formData); };

  const handleScanTables = async () => {
    if (!formData.source_id) { alert('Select a data source first'); return; }
    setScanning(true);
    setScanError('');
    try {
      const res = await axios.get(`${API_URL}/api/sources/${formData.source_id}/scan-tables`);
      const tables = res.data.tables || [];
      setAvailableTables(tables);
      // Pre-select tables that are already in the field
      const existing = formData.tables ? formData.tables.split(',').map(t => t.trim()) : [];
      const preSelected = new Set();
      tables.forEach(t => {
        if (existing.includes(t.table)) {
          preSelected.add(t.table);
        }
      });
      setSelectedTables(preSelected);
      setShowTablePicker(true);
    } catch (e) {
      setScanError(e.response?.data?.error || e.message);
    } finally { setScanning(false); }
  };

  const toggleTable = (tableName) => {
    setSelectedTables(prev => {
      const next = new Set(prev);
      if (next.has(tableName)) next.delete(tableName);
      else next.add(tableName);
      return next;
    });
  };

  const toggleAll = () => {
    if (selectedTables.size === availableTables.length) {
      setSelectedTables(new Set());
    } else {
      setSelectedTables(new Set(availableTables.map(t => t.table)));
    }
  };

  const confirmTableSelection = () => {
    const tableList = Array.from(selectedTables).sort().join(',');
    setFormData(prev => ({ ...prev, tables: tableList }));
    setShowTablePicker(false);
  };

  return (
    <div className="form-container">
      <h2>{connector ? 'Edit Connector' : 'New Connector'}</h2>
      <form onSubmit={handleSubmit}>
        <div className="form-row">
          <div className="form-group">
            <label>Name</label>
            <input type="text" name="name" value={formData.name} onChange={handleChange} required />
          </div>
          <div className="form-group">
            <label>Data Source</label>
            <select name="source_id" value={formData.source_id} onChange={handleChange} required>
              <option value="">Select a source</option>
              {sources.map(s => <option key={s.id} value={s.id}>{s.name} ({s.engine})</option>)}
            </select>
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Connector Type</label>
            <select name="connector_type" value={formData.connector_type} onChange={handleChange}>
              <option value="debezium-postgres">Debezium PostgreSQL</option>
              <option value="debezium-mysql">Debezium MySQL</option>
              <option value="debezium-oracle">Debezium Oracle</option>
              <option value="debezium-sqlserver">Debezium SQL Server</option>
            </select>
          </div>
          <div className="form-group">
            <label>Snapshot Mode</label>
            <select name="snapshot_mode" value={formData.snapshot_mode} onChange={handleChange}>
              <option value="when_needed">Smart (recommended)</option>
              <option value="initial">Full Sync + CDC</option>
              <option value="never">CDC Only</option>
              <option value="initial_only">One-Time Load</option>
            </select>
          </div>
        </div>
        <div className="form-group">
          <label>Tables (comma-separated, e.g. {formData.connector_type === 'debezium-mysql' ? 'orders,products' : 'public.users,public.orders'})</label>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-start' }}>
            <input type="text" name="tables" value={formData.tables} onChange={handleChange}
              placeholder={formData.connector_type === 'debezium-mysql' ? 'orders,products' : 'schema.table1,schema.table2'} style={{ flex: 1 }} />
            <button type="button" className="btn btn-secondary"
              onClick={handleScanTables}
              disabled={scanning || !formData.source_id}
              style={{ whiteSpace: 'nowrap', minWidth: '120px' }}>
              {scanning ? 'Scanning...' : '🔍 Scan Tables'}
            </button>
          </div>
          {scanError && (
            <div style={{ color: '#e74c3c', fontSize: '13px', marginTop: '5px' }}>{scanError}</div>
          )}
        </div>

        {/* Table Picker Modal */}
        {showTablePicker && (
          <div style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', zIndex: 1000
          }}>
            <div style={{
              background: 'white', borderRadius: '10px', padding: '25px',
              width: '500px', maxHeight: '70vh', display: 'flex', flexDirection: 'column',
              boxShadow: '0 10px 30px rgba(0,0,0,0.3)'
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '15px' }}>
                <h3 style={{ margin: 0, color: '#2c3e50' }}>
                  Select Tables ({availableTables.length} found)
                </h3>
                <button type="button" onClick={toggleAll}
                  style={{ background: 'none', border: '1px solid #ddd', borderRadius: '4px',
                    padding: '4px 10px', cursor: 'pointer', fontSize: '12px' }}>
                  {selectedTables.size === availableTables.length ? 'Deselect All' : 'Select All'}
                </button>
              </div>
              <div style={{ flex: 1, overflowY: 'auto', border: '1px solid #e0e0e0',
                borderRadius: '6px', marginBottom: '15px' }}>
                {availableTables.length === 0 ? (
                  <div style={{ padding: '20px', textAlign: 'center', color: '#999' }}>
                    No tables found in this data source.
                  </div>
                ) : (
                  availableTables.map(t => {
                    const isSelected = selectedTables.has(t.table);
                    return (
                      <label key={t.table} style={{
                        display: 'flex', alignItems: 'center', padding: '8px 12px',
                        cursor: 'pointer', borderBottom: '1px solid #f0f0f0',
                        background: isSelected ? '#e8f4fd' : 'transparent',
                        transition: 'background 0.15s'
                      }}>
                        <input type="checkbox" checked={isSelected}
                          onChange={() => toggleTable(t.table)}
                          style={{ marginRight: '10px' }} />
                        <span style={{ fontFamily: 'monospace', fontSize: '13px', fontWeight: 600 }}>
                          {t.table}
                        </span>
                      </label>
                    );
                  })
                )}
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '13px', color: '#666' }}>
                  {selectedTables.size} table{selectedTables.size !== 1 ? 's' : ''} selected
                </span>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button type="button" className="btn btn-secondary"
                    onClick={() => setShowTablePicker(false)}>Cancel</button>
                  <button type="button" className="btn btn-primary"
                    onClick={confirmTableSelection}>Confirm Selection</button>
                </div>
              </div>
            </div>
          </div>
        )}

        {formData.connector_type === 'debezium-postgres' && (
        <div className="form-row">
          <div className="form-group">
            <label>Replication Slot Name <span style={{ fontSize: '11px', color: '#999', fontWeight: 400 }}>(PostgreSQL only)</span></label>
            <input type="text" name="slot_name" value={formData.slot_name} onChange={handleChange}
              placeholder="debezium_slot" />
          </div>
          <div className="form-group">
            <label>Publication Name <span style={{ fontSize: '11px', color: '#999', fontWeight: 400 }}>(PostgreSQL only)</span></label>
            <input type="text" name="publication_name" value={formData.publication_name}
              onChange={handleChange} placeholder="dbz_publication" />
          </div>
        </div>
        )}
        <div className="form-row">
          <div className="form-group">
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input type="checkbox" name="tls_to_source"
                checked={formData.tls_to_source === 1 || formData.tls_to_source === true}
                onChange={(e) => setFormData(prev => ({ ...prev, tls_to_source: e.target.checked ? 1 : 0 }))}
                style={{ width: 'auto' }}
              />
              TLS to Source DB
            </label>
            <span style={{ fontSize: '12px', color: '#999' }}>Encrypt Debezium → Database connection (uses source SSL mode)</span>
          </div>
          <div className="form-group">
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input type="checkbox" name="tls_to_kafka"
                checked={formData.tls_to_kafka === 1 || formData.tls_to_kafka === true}
                onChange={(e) => setFormData(prev => ({ ...prev, tls_to_kafka: e.target.checked ? 1 : 0 }))}
                style={{ width: 'auto' }}
              />
              TLS to Kafka/MSK
            </label>
            <span style={{ fontSize: '12px', color: '#999' }}>Encrypt Debezium → Kafka producer connection</span>
          </div>
        </div>
        <div className="form-group">
          <label>Transformations</label>
          <TransformTabs formData={formData} setFormData={setFormData} />
        </div>
        <div className="form-group">
          <label>Additional Transforms (optional JSON)</label>
          <textarea name="transforms" value={formData.transforms || ''} onChange={handleChange} rows="2"
            placeholder='e.g. route messages, filter columns...' />
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Status</label>
            <select name="status" value={formData.status} onChange={handleChange}>
              <option value="new">New</option>
              <option value="configured">Configured</option>
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
            </select>
          </div>
        </div>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary">Save</button>
          <button type="button" className="btn btn-secondary" onClick={onCancel}>Cancel</button>
          {connector && (
            <button type="button" className="btn btn-success"
              style={{ background: '#27ae60', borderColor: '#27ae60', marginLeft: 'auto' }}
              onClick={async () => {
                const instanceId = prompt('Enter EC2 Instance ID to apply changes to:\n(e.g., i-0123456789abcdef0)');
                if (!instanceId || !instanceId.startsWith('i-')) {
                  alert('Please enter a valid instance ID (starts with i-)');
                  return;
                }
                try {
                  const res = await axios.post(`${API_URL}/api/connectors/${connector.id}/apply-changes`, {
                    instance_id: instanceId
                  });
                  alert(`✅ ${res.data.message}`);
                } catch (e) {
                  alert(`❌ Error: ${e.response?.data?.error || e.message}`);
                }
              }}>
              🔄 Apply Changes to EC2
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

export default ConnectorForm;
