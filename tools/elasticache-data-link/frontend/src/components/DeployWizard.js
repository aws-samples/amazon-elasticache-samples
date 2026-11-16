import React, { useState } from 'react';
import './Form.css';

const EC2_INSTANCE_TYPES = [
  { value: 't3.medium', label: 't3.medium (2 vCPU, 4 GB)', desc: 'Dev/test, <10K rows/sec', cost: '$30/mo' },
  { value: 'c5.xlarge', label: 'c5.xlarge (4 vCPU, 8 GB)', desc: 'Small production, <50K rows/sec', cost: '$124/mo' },
  { value: 'c5.2xlarge', label: 'c5.2xlarge (8 vCPU, 16 GB)', desc: 'Medium production, <100K rows/sec', cost: '$248/mo' },
  { value: 'c5.4xlarge', label: 'c5.4xlarge (16 vCPU, 32 GB)', desc: 'High throughput, <200K rows/sec', cost: '$496/mo' },
];

const FARGATE_CPU_OPTIONS = [
  { value: '256', label: '0.25 vCPU', memory: ['512', '1024', '2048'] },
  { value: '512', label: '0.5 vCPU', memory: ['1024', '2048', '3072', '4096'] },
  { value: '1024', label: '1 vCPU', memory: ['2048', '3072', '4096', '5120', '6144', '7168', '8192'] },
  { value: '2048', label: '2 vCPU', memory: ['4096', '5120', '6144', '7168', '8192', '16384'] },
  { value: '4096', label: '4 vCPU', memory: ['8192', '16384', '30720'] },
];

const REGIONS = [
  { value: 'us-east-1', label: 'US East (N. Virginia)' },
  { value: 'us-east-2', label: 'US East (Ohio)' },
  { value: 'us-west-2', label: 'US West (Oregon)' },
  { value: 'eu-west-1', label: 'EU (Ireland)' },
  { value: 'ap-southeast-1', label: 'Asia Pacific (Singapore)' },
];

function DeployWizard({ pipelines, onDeploy, onPreview, onDownload, onCancel }) {
  const [step, setStep] = useState(1);
  const [config, setConfig] = useState({
    pipeline_id: '',
    deploy_target: 'ec2',
    region: 'us-east-2',
    // EC2 options
    instance_type: 'c5.xlarge',
    vpc_id: '',
    subnet_id: '',
    security_group_id: '',
    existing_instance_id: '',
    deploy_to_existing: false,
    // ECS Fargate options
    sink_cpu: '1024',
    sink_memory: '2048',
    sink_count: 5,
    auto_scale: true,
    auto_scale_min: 5,
    auto_scale_max: 30,
    auto_scale_cpu_target: 70,
    debezium_cpu: '512',
    debezium_memory: '1024',
    ecs_vpc_id: '',
    ecs_subnet_ids: '',
    ecs_security_group_id: '',
  });

  const updateConfig = (key, value) => setConfig(prev => ({ ...prev, [key]: value }));

  const selectedPipeline = pipelines.find(p => String(p.id) === String(config.pipeline_id));

  const getMemoryOptions = () => {
    const cpuOption = FARGATE_CPU_OPTIONS.find(c => c.value === config.sink_cpu);
    return cpuOption ? cpuOption.memory : ['2048'];
  };

  const estimateCost = () => {
    if (config.deploy_target === 'ec2') {
      const instance = EC2_INSTANCE_TYPES.find(i => i.value === config.instance_type);
      return instance ? instance.cost : '$?/mo';
    } else {
      const cpuCost = (parseInt(config.sink_cpu) / 1024) * config.sink_count * 0.04048 * 730;
      const memCost = (parseInt(config.sink_memory) / 1024) * config.sink_count * 0.004445 * 730;
      const mskCost = 180;
      const fargateCost = Math.round(cpuCost + memCost);
      return `~$${fargateCost + mskCost}/mo (Fargate: $${fargateCost} + MSK: $${mskCost})`;
    }
  };

  // Step 1: Select Pipeline & Target
  const renderStep1 = () => (
    <div>
      <h3 style={{ marginBottom: '20px', color: '#2c3e50' }}>Step 1: Choose Pipeline & Deploy Target</h3>

      <div className="form-group">
        <label>Pipeline</label>
        <select value={config.pipeline_id} onChange={(e) => updateConfig('pipeline_id', e.target.value)}
          style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
          <option value="">Select a pipeline</option>
          {pipelines.map(p => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.source_name} → {p.target_name})
            </option>
          ))}
        </select>
      </div>

      {selectedPipeline && (
        <div style={{ background: '#f0f8ff', padding: '12px 16px', borderRadius: '6px', marginBottom: '20px', fontSize: '13px' }}>
          <strong>Pipeline:</strong> {selectedPipeline.source_name} ({selectedPipeline.source_engine}) →
          {selectedPipeline.broker_name} → {selectedPipeline.target_name}
        </div>
      )}

      <div className="form-group">
        <label>Deploy Target</label>
        <div style={{ display: 'flex', gap: '15px', marginTop: '8px' }}>
          <label style={{
            flex: 1, padding: '20px', border: config.deploy_target === 'ec2' ? '2px solid #3498db' : '2px solid #e0e0e0',
            borderRadius: '10px', cursor: 'pointer', background: config.deploy_target === 'ec2' ? '#e8f4fd' : '#fff',
            transition: 'all 0.2s'
          }}>
            <input type="radio" name="deploy_target" value="ec2"
              checked={config.deploy_target === 'ec2'}
              onChange={(e) => updateConfig('deploy_target', e.target.value)}
              style={{ display: 'none' }} />
            <div style={{ fontSize: '18px', fontWeight: 700, marginBottom: '6px' }}>🖥️ EC2</div>
            <div style={{ fontSize: '13px', color: '#666' }}>Single instance, Docker Compose</div>
            <div style={{ fontSize: '12px', color: '#999', marginTop: '4px' }}>Best for: dev/test, small workloads</div>
            <div style={{ fontSize: '13px', color: '#27ae60', fontWeight: 600, marginTop: '8px' }}>From $30/mo</div>
          </label>
          <label style={{
            flex: 1, padding: '20px', border: config.deploy_target === 'ecs' ? '2px solid #3498db' : '2px solid #e0e0e0',
            borderRadius: '10px', cursor: 'pointer', background: config.deploy_target === 'ecs' ? '#e8f4fd' : '#fff',
            transition: 'all 0.2s'
          }}>
            <input type="radio" name="deploy_target" value="ecs"
              checked={config.deploy_target === 'ecs'}
              onChange={(e) => updateConfig('deploy_target', e.target.value)}
              style={{ display: 'none' }} />
            <div style={{ fontSize: '18px', fontWeight: 700, marginBottom: '6px' }}>🚀 ECS Fargate + MSK</div>
            <div style={{ fontSize: '13px', color: '#666' }}>Serverless, auto-scaling, managed Kafka</div>
            <div style={{ fontSize: '12px', color: '#999', marginTop: '4px' }}>Best for: production, high availability</div>
            <div style={{ fontSize: '13px', color: '#27ae60', fontWeight: 600, marginTop: '8px' }}>From $300/mo</div>
          </label>
        </div>
      </div>

      <div className="form-group">
        <label>Region</label>
        <select value={config.region} onChange={(e) => updateConfig('region', e.target.value)}
          style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
          {REGIONS.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
      </div>
    </div>
  );

  // Step 2: Configure Infrastructure
  const renderStep2 = () => (
    <div>
      <h3 style={{ marginBottom: '20px', color: '#2c3e50' }}>
        Step 2: Configure {config.deploy_target === 'ec2' ? 'EC2 Instance' : 'ECS Fargate'}
      </h3>

      {config.deploy_target === 'ec2' ? renderEC2Config() : renderECSConfig()}
    </div>
  );

  const renderEC2Config = () => (
    <div>
      <div style={{ marginBottom: '20px', padding: '15px', background: '#f8f9fa', borderRadius: '8px' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: '10px', fontWeight: 600, cursor: 'pointer' }}>
          <input type="checkbox" checked={config.deploy_to_existing}
            onChange={(e) => updateConfig('deploy_to_existing', e.target.checked)}
            style={{ width: 'auto' }} />
          Deploy to existing EC2 instance
        </label>
        {config.deploy_to_existing && (
          <div style={{ marginTop: '12px' }}>
            <label style={{ fontSize: '13px', color: '#555', display: 'block', marginBottom: '4px' }}>Instance ID</label>
            <input type="text" value={config.existing_instance_id}
              onChange={(e) => updateConfig('existing_instance_id', e.target.value)}
              placeholder="i-0123456789abcdef0"
              style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px', fontFamily: 'monospace', fontSize: '14px' }} />
            <span style={{ fontSize: '12px', color: '#999' }}>EC2 must have Docker, SSM agent, and network access to your DB and Valkey</span>
          </div>
        )}
      </div>

      {!config.deploy_to_existing && (<>
      <div className="form-group">
        <label>Instance Type</label>
        {EC2_INSTANCE_TYPES.map(inst => (
          <label key={inst.value} style={{
            display: 'flex', alignItems: 'center', padding: '12px 16px', marginBottom: '8px',
            border: config.instance_type === inst.value ? '2px solid #3498db' : '1px solid #e0e0e0',
            borderRadius: '8px', cursor: 'pointer',
            background: config.instance_type === inst.value ? '#e8f4fd' : '#fff'
          }}>
            <input type="radio" name="instance_type" value={inst.value}
              checked={config.instance_type === inst.value}
              onChange={(e) => updateConfig('instance_type', e.target.value)}
              style={{ marginRight: '12px' }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: '14px' }}>{inst.label}</div>
              <div style={{ fontSize: '12px', color: '#666' }}>{inst.desc}</div>
            </div>
            <div style={{ fontWeight: 600, color: '#27ae60' }}>{inst.cost}</div>
          </label>
        ))}
      </div>

      <div className="form-row" style={{ display: 'flex', gap: '15px' }}>
        <div className="form-group" style={{ flex: 1 }}>
          <label>VPC ID</label>
          <input type="text" value={config.vpc_id}
            onChange={(e) => updateConfig('vpc_id', e.target.value)}
            placeholder="vpc-xxxxxxxxx (leave empty for default VPC)"
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px', fontFamily: 'monospace' }} />
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label>Subnet ID</label>
          <input type="text" value={config.subnet_id}
            onChange={(e) => updateConfig('subnet_id', e.target.value)}
            placeholder="subnet-xxxxxxxxx (private subnet recommended)"
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px', fontFamily: 'monospace' }} />
        </div>
      </div>

      <div className="form-row" style={{ display: 'flex', gap: '15px' }}>
        <div className="form-group" style={{ flex: 1 }}>
          <label>Security Group ID</label>
          <input type="text" value={config.security_group_id}
            onChange={(e) => updateConfig('security_group_id', e.target.value)}
            placeholder="sg-xxxxxxxxx (auto-created if empty)"
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px', fontFamily: 'monospace' }} />
        </div>
      </div>

      <div style={{ background: '#e3f2fd', padding: '12px 16px', borderRadius: '6px', fontSize: '13px', marginTop: '10px' }}>
        <strong>ℹ️ Access:</strong> EC2 will be configured with SSM (Session Manager) — no SSH key pair needed.
        Connect via: <code>aws ssm start-session --target i-xxxxx</code>
      </div>
      </>)}
    </div>
  );

  const renderECSConfig = () => (
    <div>
      <div style={{ background: '#fff3cd', padding: '12px 16px', borderRadius: '6px', marginBottom: '20px', fontSize: '13px', border: '1px solid #ffc107' }}>
        <strong>ℹ️ ECS Fargate + MSK:</strong> Sink workers run as serverless containers. Kafka is managed by AWS (MSK). Auto-scaling adjusts worker count based on CPU load.
      </div>

      <h4 style={{ color: '#34495e', marginBottom: '12px' }}>Sink Worker Configuration</h4>
      <div className="form-row" style={{ display: 'flex', gap: '15px', marginBottom: '15px' }}>
        <div className="form-group" style={{ flex: 1 }}>
          <label>CPU per Worker</label>
          <select value={config.sink_cpu}
            onChange={(e) => {
              updateConfig('sink_cpu', e.target.value);
              const cpuOpt = FARGATE_CPU_OPTIONS.find(c => c.value === e.target.value);
              if (cpuOpt && !cpuOpt.memory.includes(config.sink_memory)) {
                updateConfig('sink_memory', cpuOpt.memory[0]);
              }
            }}
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
            {FARGATE_CPU_OPTIONS.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label>Memory per Worker</label>
          <select value={config.sink_memory}
            onChange={(e) => updateConfig('sink_memory', e.target.value)}
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
            {getMemoryOptions().map(m => (
              <option key={m} value={m}>{(parseInt(m) / 1024).toFixed(1)} GB</option>
            ))}
          </select>
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label>Initial Worker Count</label>
          <input type="number" value={config.sink_count} min="1" max="50"
            onChange={(e) => updateConfig('sink_count', parseInt(e.target.value) || 1)}
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }} />
        </div>
      </div>

      <h4 style={{ color: '#34495e', marginBottom: '12px' }}>Auto-Scaling</h4>
      <div style={{ padding: '15px', background: '#f8f9fa', borderRadius: '8px', marginBottom: '20px' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px', fontWeight: 600 }}>
          <input type="checkbox" checked={config.auto_scale}
            onChange={(e) => updateConfig('auto_scale', e.target.checked)}
            style={{ width: 'auto' }} />
          Enable Auto-Scaling
        </label>
        {config.auto_scale && (
          <div className="form-row" style={{ display: 'flex', gap: '15px' }}>
            <div className="form-group" style={{ flex: 1 }}>
              <label>Min Workers</label>
              <input type="number" value={config.auto_scale_min} min="1" max="50"
                onChange={(e) => updateConfig('auto_scale_min', parseInt(e.target.value) || 1)}
                style={{ width: '100%', padding: '8px', border: '1px solid #ddd', borderRadius: '5px' }} />
            </div>
            <div className="form-group" style={{ flex: 1 }}>
              <label>Max Workers</label>
              <input type="number" value={config.auto_scale_max} min="1" max="100"
                onChange={(e) => updateConfig('auto_scale_max', parseInt(e.target.value) || 30)}
                style={{ width: '100%', padding: '8px', border: '1px solid #ddd', borderRadius: '5px' }} />
            </div>
            <div className="form-group" style={{ flex: 1 }}>
              <label>CPU Target (%)</label>
              <input type="number" value={config.auto_scale_cpu_target} min="30" max="90"
                onChange={(e) => updateConfig('auto_scale_cpu_target', parseInt(e.target.value) || 70)}
                style={{ width: '100%', padding: '8px', border: '1px solid #ddd', borderRadius: '5px' }} />
            </div>
          </div>
        )}
      </div>

      <h4 style={{ color: '#34495e', marginBottom: '12px' }}>Debezium (CDC Connector)</h4>
      <div className="form-row" style={{ display: 'flex', gap: '15px', marginBottom: '20px' }}>
        <div className="form-group" style={{ flex: 1 }}>
          <label>Debezium CPU</label>
          <select value={config.debezium_cpu}
            onChange={(e) => updateConfig('debezium_cpu', e.target.value)}
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
            <option value="256">0.25 vCPU</option>
            <option value="512">0.5 vCPU</option>
            <option value="1024">1 vCPU</option>
          </select>
        </div>
        <div className="form-group" style={{ flex: 1 }}>
          <label>Debezium Memory</label>
          <select value={config.debezium_memory}
            onChange={(e) => updateConfig('debezium_memory', e.target.value)}
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
            <option value="512">0.5 GB</option>
            <option value="1024">1 GB</option>
            <option value="2048">2 GB</option>
          </select>
        </div>
      </div>

      <h4 style={{ color: '#34495e', marginBottom: '12px' }}>Networking</h4>
      <div className="form-row" style={{ display: 'flex', gap: '15px' }}>
        <div className="form-group" style={{ flex: 1 }}>
          <label>VPC ID</label>
          <input type="text" value={config.ecs_vpc_id}
            onChange={(e) => updateConfig('ecs_vpc_id', e.target.value)}
            placeholder="vpc-xxxxxxxxx"
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px', fontFamily: 'monospace' }} />
        </div>
        <div className="form-group" style={{ flex: 2 }}>
          <label>Subnet IDs (comma-separated, private subnets)</label>
          <input type="text" value={config.ecs_subnet_ids}
            onChange={(e) => updateConfig('ecs_subnet_ids', e.target.value)}
            placeholder="subnet-aaa,subnet-bbb"
            style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '5px', fontFamily: 'monospace' }} />
        </div>
      </div>
    </div>
  );

  // Step 3: Review & Deploy
  const renderStep3 = () => (
    <div>
      <h3 style={{ marginBottom: '20px', color: '#2c3e50' }}>Step 3: Review & Deploy</h3>

      <div style={{ background: '#f8f9fa', borderRadius: '10px', padding: '20px', marginBottom: '20px' }}>
        <h4 style={{ margin: '0 0 15px 0', color: '#2c3e50' }}>Deployment Summary</h4>

        <table style={{ width: '100%', fontSize: '14px', borderCollapse: 'collapse' }}>
          <tbody>
            <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
              <td style={{ padding: '8px 0', fontWeight: 600, width: '200px' }}>Pipeline</td>
              <td style={{ padding: '8px 0' }}>{selectedPipeline?.name || '—'}</td>
            </tr>
            <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
              <td style={{ padding: '8px 0', fontWeight: 600 }}>Deploy Target</td>
              <td style={{ padding: '8px 0' }}>
                {config.deploy_target === 'ec2' ? '🖥️ EC2 (Docker Compose)' : '🚀 ECS Fargate + MSK'}
              </td>
            </tr>
            <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
              <td style={{ padding: '8px 0', fontWeight: 600 }}>Region</td>
              <td style={{ padding: '8px 0' }}>{config.region}</td>
            </tr>

            {config.deploy_target === 'ec2' ? (
              <>
                <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
                  <td style={{ padding: '8px 0', fontWeight: 600 }}>Instance Type</td>
                  <td style={{ padding: '8px 0' }}>{config.instance_type}</td>
                </tr>
                <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
                  <td style={{ padding: '8px 0', fontWeight: 600 }}>Access</td>
                  <td style={{ padding: '8px 0' }}>SSM Session Manager (no SSH key needed)</td>
                </tr>
                <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
                  <td style={{ padding: '8px 0', fontWeight: 600 }}>Components</td>
                  <td style={{ padding: '8px 0' }}>Kafka + Debezium + Sink Workers (all on one instance)</td>
                </tr>
              </>
            ) : (
              <>
                <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
                  <td style={{ padding: '8px 0', fontWeight: 600 }}>Sink Workers</td>
                  <td style={{ padding: '8px 0' }}>
                    {config.sink_count} × {(parseInt(config.sink_cpu) / 1024).toFixed(1)} vCPU / {(parseInt(config.sink_memory) / 1024).toFixed(1)} GB
                  </td>
                </tr>
                <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
                  <td style={{ padding: '8px 0', fontWeight: 600 }}>Auto-Scaling</td>
                  <td style={{ padding: '8px 0' }}>
                    {config.auto_scale
                      ? `${config.auto_scale_min}–${config.auto_scale_max} workers (CPU > ${config.auto_scale_cpu_target}%)`
                      : 'Disabled'}
                  </td>
                </tr>
                <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
                  <td style={{ padding: '8px 0', fontWeight: 600 }}>Debezium</td>
                  <td style={{ padding: '8px 0' }}>
                    {(parseInt(config.debezium_cpu) / 1024).toFixed(1)} vCPU / {(parseInt(config.debezium_memory) / 1024).toFixed(1)} GB
                  </td>
                </tr>
                <tr style={{ borderBottom: '1px solid #e0e0e0' }}>
                  <td style={{ padding: '8px 0', fontWeight: 600 }}>Kafka</td>
                  <td style={{ padding: '8px 0' }}>Amazon MSK (managed, 2 brokers)</td>
                </tr>
              </>
            )}
          </tbody>
        </table>
      </div>

      <div style={{
        background: '#e8f5e9', borderRadius: '10px', padding: '16px 20px',
        border: '1px solid #a5d6a7', marginBottom: '20px'
      }}>
        <div style={{ fontSize: '14px', fontWeight: 600, color: '#2e7d32', marginBottom: '4px' }}>
          💰 Estimated Cost
        </div>
        <div style={{ fontSize: '18px', fontWeight: 700, color: '#1b5e20' }}>
          {estimateCost()}
        </div>
      </div>

      {config.deploy_target === 'ec2' && (
        <div style={{ background: '#e3f2fd', borderRadius: '8px', padding: '12px 16px', fontSize: '13px', marginBottom: '15px' }}>
          <strong>What will happen:</strong> A new EC2 instance will be launched with Docker pre-installed.
          The ECDL pipeline (Kafka, Debezium, Sink Workers) will start automatically via docker-compose.
        </div>
      )}
      {config.deploy_target === 'ecs' && (
        <div style={{ background: '#e3f2fd', borderRadius: '8px', padding: '12px 16px', fontSize: '13px', marginBottom: '15px' }}>
          <strong>What will happen:</strong> An ECS cluster, MSK Kafka cluster, and Fargate services will be created.
          Sink workers will auto-scale based on CPU utilization. Debezium runs as a Fargate task.
        </div>
      )}
    </div>
  );

  const canProceed = () => {
    if (step === 1) return !!config.pipeline_id;
    if (step === 2) return true;
    return true;
  };

  return (
    <div style={{
      background: 'white', borderRadius: '12px', padding: '30px',
      boxShadow: '0 4px 12px rgba(0,0,0,0.1)', marginBottom: '20px'
    }}>
      {/* Progress Steps */}
      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '30px', gap: '0' }}>
        {[1, 2, 3].map(s => (
          <div key={s} style={{ display: 'flex', alignItems: 'center' }}>
            <div style={{
              width: '32px', height: '32px', borderRadius: '50%',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              background: step >= s ? '#3498db' : '#e0e0e0',
              color: step >= s ? '#fff' : '#999',
              fontWeight: 700, fontSize: '14px'
            }}>{s}</div>
            <div style={{ marginLeft: '8px', fontSize: '13px', color: step >= s ? '#2c3e50' : '#999', fontWeight: step === s ? 600 : 400 }}>
              {s === 1 ? 'Pipeline & Target' : s === 2 ? 'Configure' : 'Review & Deploy'}
            </div>
            {s < 3 && <div style={{ width: '40px', height: '2px', background: step > s ? '#3498db' : '#e0e0e0', margin: '0 12px' }} />}
          </div>
        ))}
      </div>

      {/* Step Content */}
      {step === 1 && renderStep1()}
      {step === 2 && renderStep2()}
      {step === 3 && renderStep3()}

      {/* Navigation */}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '25px', paddingTop: '20px', borderTop: '1px solid #e0e0e0' }}>
        <button type="button" className="btn btn-secondary"
          onClick={step === 1 ? onCancel : () => setStep(step - 1)}>
          {step === 1 ? 'Cancel' : '← Back'}
        </button>
        <div style={{ display: 'flex', gap: '10px' }}>
          {step === 3 && (
            <>
              <button type="button" className="btn btn-secondary"
                onClick={() => onPreview && onPreview(config)}>
                👁️ Preview Config
              </button>
              <button type="button" className="btn btn-secondary"
                onClick={() => onDownload && onDownload(config)}
                style={{ background: '#f39c12', borderColor: '#f39c12', color: '#fff' }}>
                ⬇️ Download Package
              </button>
              <button type="button" className="btn btn-primary"
                onClick={() => onDeploy(config)}
                style={{ background: '#27ae60', borderColor: '#27ae60', padding: '10px 24px', fontSize: '15px' }}>
                🚀 Deploy Now
              </button>
            </>
          )}
          {step < 3 && (
            <button type="button" className="btn btn-primary"
              onClick={() => setStep(step + 1)}
              disabled={!canProceed()}>
              Next →
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default DeployWizard;
