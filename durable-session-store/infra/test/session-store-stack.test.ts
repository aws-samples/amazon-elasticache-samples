// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { SessionStoreStack } from '../lib/session-store-stack';

const TEST_CIDR = '203.0.113.0/24';

// Mirrors bin/infra.ts: environment-agnostic, so no account ID is baked in.
function synth(): Template {
  const app = new cdk.App({ context: { allowedCidr: TEST_CIDR } });
  const stack = new SessionStoreStack(app, 'TestStack');
  return Template.fromStack(stack);
}

describe('SessionStoreStack', () => {
  const template = synth();

  test('provisions a Valkey 9.1 replication group', () => {
    template.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      Engine: 'valkey',
      EngineVersion: '9.1',
    });
  });

  test('is durable: Multi-AZ, automatic failover, and snapshots', () => {
    template.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      MultiAZEnabled: true,
      AutomaticFailoverEnabled: true,
      SnapshotRetentionLimit: 7,
    });
  });

  test('cluster mode enabled: one shard with two replicas', () => {
    template.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      ClusterMode: 'enabled',
      NumNodeGroups: 1,
      ReplicasPerNodeGroup: 2,
    });
  });

  test('uses synchronous durability', () => {
    template.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      Durability: 'sync',
    });
  });

  test('encrypts data at rest with a customer-managed KMS key', () => {
    template.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      AtRestEncryptionEnabled: true,
      KmsKeyId: Match.anyValue(),
    });
    template.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  test('requires in-transit encryption (TLS)', () => {
    template.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      TransitEncryptionEnabled: true,
      TransitEncryptionMode: 'required',
    });
  });

  test('requires an AUTH token', () => {
    template.hasResourceProperties('AWS::ElastiCache::ReplicationGroup', {
      AuthToken: Match.anyValue(),
    });
  });

  test('generates the AUTH token and Flask key into Secrets Manager', () => {
    // Two secrets: the Valkey AUTH token and the Flask session-signing key.
    template.resourceCountIs('AWS::SecretsManager::Secret', 2);
  });

  test('cache security group accepts Valkey only from the client SG', () => {
    // The cache SG ingress must be SG-to-SG on 6379, never a CIDR.
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: 'ElastiCache Valkey session store - cache tier',
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({
          FromPort: 6379,
          ToPort: 6379,
          SourceSecurityGroupId: Match.anyValue(),
        }),
      ]),
    });
  });

  test('cache security group has no CIDR-based ingress', () => {
    // Find the cache SG specifically and assert none of its ingress rules
    // open a CIDR range (the ALB SG legitimately allows 0.0.0.0/0 on 80).
    const sgs = template.findResources('AWS::EC2::SecurityGroup');
    const cacheSg = Object.values(sgs).find(
      (sg: any) =>
        sg.Properties?.GroupDescription
        === 'ElastiCache Valkey session store - cache tier',
    ) as any;
    expect(cacheSg).toBeDefined();
    const hasCidrIngress = (cacheSg.Properties?.SecurityGroupIngress ?? []).some(
      (rule: any) => rule.CidrIp !== undefined || rule.CidrIpv6 !== undefined,
    );
    expect(hasCidrIngress).toBe(false);
  });

  test('cache is deployed into a subnet group', () => {
    template.resourceCountIs('AWS::ElastiCache::SubnetGroup', 1);
  });

  test('VPC flow logs are enabled', () => {
    template.resourceCountIs('AWS::EC2::FlowLog', 1);
  });

  // --- Storefront app (ECS Fargate + ALB) -----------------------------------

  test('runs the storefront as a Fargate service', () => {
    template.hasResourceProperties('AWS::ECS::Service', {
      LaunchType: 'FARGATE',
    });
  });

  test('is fronted by an internet-facing load balancer', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      { Scheme: 'internet-facing' },
    );
  });

  test('ALB health check targets /healthz', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::TargetGroup',
      { HealthCheckPath: '/healthz' },
    );
  });

  test('task injects AUTH token and Flask key as secrets, not plaintext', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Secrets: Match.arrayWith([
            Match.objectLike({ Name: 'REDIS_AUTH_TOKEN' }),
            Match.objectLike({ Name: 'SECRET_KEY' }),
          ]),
        }),
      ]),
    });
  });

  test('task role may trigger TestFailover for the demo button', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Action: 'elasticache:TestFailover' }),
        ]),
      },
    });
  });

  test('serves HTTPS on 443 with a TLS 1.3 security policy', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::Listener',
      {
        Protocol: 'HTTPS',
        Port: 443,
        SslPolicy: Match.stringLikeRegexp('TLS13'),
      },
    );
  });

  test('has NO HTTP listener (HTTPS only)', () => {
    const listeners = template.findResources(
      'AWS::ElasticLoadBalancingV2::Listener',
    );
    const httpListeners = Object.values(listeners).filter(
      (l: any) => l.Properties?.Protocol === 'HTTP',
    );
    expect(httpListeners).toHaveLength(0);
  });

  test('no security group opens port 80 to the internet', () => {
    const sgs = template.findResources('AWS::EC2::SecurityGroup');
    const port80Open = Object.values(sgs).some((sg: any) =>
      (sg.Properties?.SecurityGroupIngress ?? []).some(
        (r: any) =>
          r.FromPort === 80
          && (r.CidrIp === '0.0.0.0/0' || r.CidrIpv6 === '::/0'),
      ),
    );
    expect(port80Open).toBe(false);
  });

  test('the ALB SG allows 443 only from the scoped CIDR (never 0.0.0.0/0)', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({ FromPort: 443, CidrIp: TEST_CIDR }),
      ]),
    });
  });

  test('no security group allows 0.0.0.0/0 ingress on any port', () => {
    const sgs = template.findResources('AWS::EC2::SecurityGroup');
    const openToWorld = Object.values(sgs).some((sg: any) =>
      (sg.Properties?.SecurityGroupIngress ?? []).some(
        (r: any) => r.CidrIp === '0.0.0.0/0' || r.CidrIpv6 === '::/0',
      ),
    );
    expect(openToWorld).toBe(false);
  });
});

describe('SessionStoreStack guard rails', () => {
  test('certificate is a required deploy-time parameter (no default)', () => {
    const template = synth();
    template.hasParameter('CertificateArn', {
      Type: 'String',
      AllowedPattern: Match.stringLikeRegexp('acm'),
    });
    const param = template.toJSON().Parameters.CertificateArn;
    expect(param.Default).toBeUndefined();
  });

  test('synth fails fast without a restricted CIDR', () => {
    const app = new cdk.App();
    expect(() => new SessionStoreStack(app, 'NoCidrStack')).toThrow(
      /restricted ALB ingress CIDR is required/,
    );
  });

  test('synth rejects 0.0.0.0/0 as the allowed CIDR', () => {
    const app = new cdk.App({ context: { allowedCidr: '0.0.0.0/0' } });
    expect(() => new SessionStoreStack(app, 'OpenCidrStack')).toThrow(
      /0\.0\.0\.0\/0 is not allowed/,
    );
  });
});

describe('SessionStoreStack scanner hardening', () => {
  const template = synth();
  const json = template.toJSON();
  const resources = Object.values(json.Resources) as any[];
  const sgs = resources.filter((r) => r.Type === 'AWS::EC2::SecurityGroup');
  // Inline egress on each SG plus any standalone SecurityGroupEgress resources.
  const egressOf = (sg: any) => sg.Properties?.SecurityGroupEgress ?? [];
  const standaloneEgress = resources
    .filter((r) => r.Type === 'AWS::EC2::SecurityGroupEgress')
    .map((r) => r.Properties);

  test('every CloudWatch log group is KMS-encrypted', () => {
    const groups = resources.filter((r) => r.Type === 'AWS::Logs::LogGroup');
    expect(groups.length).toBeGreaterThanOrEqual(4);
    for (const g of groups) {
      expect(g.Properties.KmsKeyId).toBeDefined();
    }
  });

  test('the KMS key policy lets CloudWatch Logs use the key', () => {
    template.hasResourceProperties('AWS::KMS::Key', {
      KeyPolicy: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: Match.anyValue() },
            Condition: {
              ArnLike: {
                'kms:EncryptionContext:aws:logs:arn': Match.anyValue(),
              },
            },
          }),
        ]),
      },
    });
  });

  test('no security group has all-protocols (-1) egress', () => {
    const rules = [...sgs.flatMap(egressOf), ...standaloneEgress];
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) {
      expect(rule.IpProtocol).not.toBe('-1');
    }
  });

  test('no CDK placeholder egress rule remains', () => {
    for (const sg of sgs) {
      for (const rule of egressOf(sg)) {
        expect(rule.CidrIp).not.toBe('255.255.255.255/32');
      }
    }
  });

  test('no subnet auto-assigns public IPs', () => {
    const subnets = resources.filter((r) => r.Type === 'AWS::EC2::Subnet');
    expect(subnets.length).toBeGreaterThan(0);
    for (const s of subnets) {
      expect(s.Properties.MapPublicIpOnLaunch).toBe(false);
    }
  });

  test('no Lambda functions or inline role policies are created', () => {
    expect(
      resources.filter((r) => r.Type === 'AWS::Lambda::Function'),
    ).toHaveLength(0);
    for (const role of resources.filter((r) => r.Type === 'AWS::IAM::Role')) {
      expect(role.Properties.Policies).toBeUndefined();
    }
  });

  test('no AWS account ID is written into the template', () => {
    // Account IDs leak as part of an ARN (a 12-digit run between colons) or as
    // a bare quoted 12-digit value. Digit runs inside generated logical IDs are
    // not account IDs, so they are deliberately not matched.
    const text = JSON.stringify(json);
    expect(text).not.toMatch(/:[0-9]{12}:/);
    expect(text).not.toMatch(/"[0-9]{12}"/);
  });
});
