// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as path from 'path';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecsPatterns from 'aws-cdk-lib/aws-ecs-patterns';
import * as elasticache from 'aws-cdk-lib/aws-elasticache';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as logs from 'aws-cdk-lib/aws-logs';
import { NagSuppressions } from 'cdk-nag';

/**
 * Provisions a durable, secure session store on Amazon ElastiCache for Valkey 9.1.
 *
 * Topology:
 *   - Cluster mode ENABLED, one shard (node group): 1 primary + 2 replicas
 *   - Clients connect via the cluster configuration endpoint
 *
 * Durability:
 *   - Synchronous durability ('sync'): acknowledged writes are persisted to the
 *     Multi-AZ transactional log before the client is told the write succeeded
 *   - Multi-AZ with automatic failover (a replica is promoted on primary loss)
 *   - Two replicas across AZs
 *   - Daily automatic snapshots retained for 7 days (point-in-time recovery)
 *
 * Security (AWS best practices):
 *   - At-rest encryption with a customer-managed KMS key (rotation enabled)
 *   - In-transit encryption (TLS) required
 *   - AUTH token stored in Secrets Manager, never in source or state in plaintext
 *   - Cache deployed into private, isolated subnets; access granted per-SG
 *   - Secrets (AUTH token, Flask key) generated into Secrets Manager
 *
 * Demo app:
 *   - Cluster-aware Flask storefront on ECS Fargate (private subnets)
 *   - Internet-facing ALB (public subnets) so a browser can reach it
 *   - Reads secrets at runtime; granted elasticache:TestFailover for the
 *     "fail over the primary" durability demo button
 */
export class SessionStoreStack extends cdk.Stack {
  /** Security group clients must be a member of (or be granted ingress) to connect. */
  public readonly clientSecurityGroup: ec2.SecurityGroup;
  /** Secret holding the Valkey AUTH token. */
  public readonly authTokenSecret: secretsmanager.Secret;
  /** Configuration endpoint address of the (cluster-mode-enabled) replication group. */
  public readonly configurationEndpointAddress: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const port = 6379;

    // --- Networking ---------------------------------------------------------
    // Three subnet tiers:
    //   public       - internet-facing ALB only (reached by the browser)
    //   app-private  - Fargate tasks; egress via NAT to pull image + read secret
    //   cache-isolated - Valkey cluster; no internet path in or out
    // One NAT gateway keeps the demo cheap; use 2+ for production HA.

    // --- Encryption key -----------------------------------------------------
    // Created first so it can also encrypt the CloudWatch log groups below,
    // including the VPC flow-log group.
    const encryptionKey = new kms.Key(this, 'SessionStoreKmsKey', {
      description: 'CMK for the Valkey session store (cache, secrets, logs)',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    // Let CloudWatch Logs use the key, but only for log groups in this account
    // and region (encryption-context condition).
    encryptionKey.addToResourcePolicy(
      new iam.PolicyStatement({
        principals: [
          new iam.ServicePrincipal(`logs.${cdk.Aws.REGION}.amazonaws.com`),
        ],
        actions: [
          'kms:Encrypt*',
          'kms:Decrypt*',
          'kms:ReEncrypt*',
          'kms:GenerateDataKey*',
          'kms:Describe*',
        ],
        resources: ['*'],
        conditions: {
          ArnLike: {
            'kms:EncryptionContext:aws:logs:arn': `arn:${cdk.Aws.PARTITION}:logs:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:log-group:*`,
          },
        },
      }),
    );

    const flowLogGroup = new logs.LogGroup(this, 'VpcFlowLog', {
      retention: logs.RetentionDays.ONE_MONTH,
      encryptionKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const vpc = new ec2.Vpc(this, 'SessionStoreVpc', {
      maxAzs: 2,
      natGateways: 1,
      // CDK would otherwise add a Lambda-backed custom resource that strips
      // the default security group's rules. Nothing in this stack uses the
      // default SG, so we skip it rather than ship an extra Lambda + inline
      // IAM policy. Enforce "default SG closed" account-wide with AWS Config
      // (VPC_DEFAULT_SECURITY_GROUP_CLOSED) if you need it.
      restrictDefaultSecurityGroup: false,
      subnetConfiguration: [
        {
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
          // The ALB and NAT gateway get their own addresses; nothing here
          // needs an auto-assigned public IP.
          mapPublicIpOnLaunch: false,
        },
        {
          name: 'app-private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
        {
          name: 'cache-isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
      flowLogs: {
        'vpc-flow-logs': {
          trafficType: ec2.FlowLogTrafficType.ALL,
          destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogGroup),
        },
      },
    });

    // Security group for the cache cluster itself.
    const cacheSecurityGroup = new ec2.SecurityGroup(this, 'CacheSg', {
      vpc,
      description: 'ElastiCache Valkey session store - cache tier',
      allowAllOutbound: false,
    });
    // The cache initiates no connections. Give it one narrow, explicit egress
    // rule (to itself on the Valkey port) so CDK doesn't emit its catch-all
    // placeholder rule.
    cacheSecurityGroup.addEgressRule(
      cacheSecurityGroup,
      ec2.Port.tcp(port),
      'Valkey traffic within the cache tier only',
    );

    // Security group that other clients can attach to in order to be allowed in.
    // Outbound is limited to the cache on the Valkey port.
    this.clientSecurityGroup = new ec2.SecurityGroup(this, 'ClientSg', {
      vpc,
      description: 'Clients permitted to reach the Valkey session store',
      allowAllOutbound: false,
    });
    this.clientSecurityGroup.addEgressRule(
      cacheSecurityGroup,
      ec2.Port.tcp(port),
      'Valkey access to the cache tier only',
    );

    // Only members of the client SG may reach the cache on the Valkey port.
    cacheSecurityGroup.addIngressRule(
      ec2.Peer.securityGroupId(this.clientSecurityGroup.securityGroupId),
      ec2.Port.tcp(port),
      'Allow Valkey access from approved client security group only',
    );

    // --- AUTH token ---------------------------------------------------------
    // Generated at deploy time and stored in Secrets Manager. The app reads it
    // at runtime; it is never written to source control.
    this.authTokenSecret = new secretsmanager.Secret(this, 'ValkeyAuthToken', {
      description: 'Valkey AUTH token for the session store',
      encryptionKey,
      generateSecretString: {
        passwordLength: 64,
        // AUTH tokens must be printable and exclude characters Valkey disallows.
        excludePunctuation: true,
        excludeCharacters: '"@/\\ \'',
      },
    });

    // Flask session-signing key, also generated into Secrets Manager.
    const flaskSecretKey = new secretsmanager.Secret(this, 'FlaskSecretKey', {
      description: 'Flask SECRET_KEY for signing the session cookie',
      encryptionKey,
      generateSecretString: {
        passwordLength: 64,
        excludePunctuation: true,
      },
    });

    // --- Subnet group -------------------------------------------------------
    const subnetGroup = new elasticache.CfnSubnetGroup(this, 'CacheSubnetGroup', {
      description: 'Isolated subnets for the Valkey session store',
      subnetIds: vpc.selectSubnets({
        subnetGroupName: 'cache-isolated',
      }).subnetIds,
      cacheSubnetGroupName: 'durable-session-store-subnets',
    });

    // --- Log delivery -------------------------------------------------------
    const slowLogGroup = new logs.LogGroup(this, 'ValkeySlowLog', {
      retention: logs.RetentionDays.ONE_MONTH,
      encryptionKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const engineLogGroup = new logs.LogGroup(this, 'ValkeyEngineLog', {
      retention: logs.RetentionDays.ONE_MONTH,
      encryptionKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // --- Replication group (the durable session store) ----------------------
    const replicationGroup = new elasticache.CfnReplicationGroup(
      this,
      'SessionStoreReplicationGroup',
      {
        replicationGroupDescription: 'Durable Valkey 9.1 session store',
        engine: 'valkey',
        engineVersion: '9.1',
        cacheNodeType: 'cache.r7g.large',
        port,

        // --- Cluster mode enabled: 1 shard, 1 primary + 2 replicas ---
        // clusterMode 'enabled' makes this a cluster-mode-enabled (CME)
        // replication group. A single node group (shard) with two replicas
        // keeps all keys in one slot range while still giving Multi-AZ failover.
        // Clients MUST use a cluster-aware connection (configuration endpoint).
        clusterMode: 'enabled',
        numNodeGroups: 1, // one shard (one primary)
        replicasPerNodeGroup: 2, // two replicas in that shard

        // --- Durability / high availability ---
        // 'sync' durability (Valkey 9.0+): each write is persisted to the
        // Multi-AZ transactional log BEFORE the client gets an acknowledgement,
        // so an acknowledged write survives a primary failure. Durability is
        // fixed at create time. Enabling it auto-enables at-rest encryption.
        durability: 'sync',
        automaticFailoverEnabled: true, // required for cluster mode enabled
        multiAzEnabled: true,
        snapshotRetentionLimit: 7,
        snapshotWindow: '03:00-05:00',
        preferredMaintenanceWindow: 'sun:05:00-sun:07:00',

        // --- Security ---
        atRestEncryptionEnabled: true,
        kmsKeyId: encryptionKey.keyId,
        transitEncryptionEnabled: true,
        transitEncryptionMode: 'required',
        authToken: this.authTokenSecret.secretValue.unsafeUnwrap(),

        cacheSubnetGroupName: subnetGroup.cacheSubnetGroupName,
        securityGroupIds: [cacheSecurityGroup.securityGroupId],

        autoMinorVersionUpgrade: true,

        logDeliveryConfigurations: [
          {
            destinationType: 'cloudwatch-logs',
            destinationDetails: {
              cloudWatchLogsDetails: { logGroup: slowLogGroup.logGroupName },
            },
            logFormat: 'json',
            logType: 'slow-log',
          },
          {
            destinationType: 'cloudwatch-logs',
            destinationDetails: {
              cloudWatchLogsDetails: { logGroup: engineLogGroup.logGroupName },
            },
            logFormat: 'json',
            logType: 'engine-log',
          },
        ],
      },
    );
    replicationGroup.addDependency(subnetGroup);
    // Cluster-mode-enabled clients connect to the configuration endpoint, which
    // transparently routes to the correct shard. (A CME replication group does
    // not expose a single primary endpoint.)
    this.configurationEndpointAddress =
      replicationGroup.attrConfigurationEndPointAddress;

    // --- Storefront app: ECS Fargate behind an internet-facing ALB ----------
    const ecsCluster = new ecs.Cluster(this, 'AppCluster', {
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // Build the container image from the repo root at deploy time.
    const appImage = ecs.ContainerImage.fromAsset(
      path.join(__dirname, '..', '..'),
    );

    const appLogGroup = new logs.LogGroup(this, 'AppLog', {
      retention: logs.RetentionDays.ONE_MONTH,
      encryptionKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // The ALB serves HTTPS ONLY (443, TLS 1.3). There is no HTTP:80 listener and
    // the ALB security group never opens port 80, so no plaintext ingress
    // exists. The ACM certificate ARN is a REQUIRED CloudFormation parameter
    // (no default), so deploys fail without one, and no ARN (which contains an
    // account ID) is ever written into the synthesized template.
    //   cdk deploy --parameters CertificateArn=arn:aws:acm:...
    const certificateArnParam = new cdk.CfnParameter(this, 'CertificateArn', {
      type: 'String',
      description:
        'ARN of an ACM certificate in this region for the HTTPS load balancer',
      allowedPattern: '^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[A-Za-z0-9-]+$',
      constraintDescription: 'must be an ACM certificate ARN',
    });
    const certificate = acm.Certificate.fromCertificateArn(
      this,
      'AlbCert',
      certificateArnParam.valueAsString,
    );

    // The ALB ingress CIDR MUST be scoped to a specific network (your office,
    // VPN, or corporate range) rather than the whole internet. Pass it via
    // `-c allowedCidr=...` or ALB_ALLOWED_CIDR. Synth fails fast if it is
    // missing or set to 0.0.0.0/0, so the template never ships an
    // open-to-the-world security group.
    const allowedCidr =
      (this.node.tryGetContext('allowedCidr') as string | undefined)
      ?? process.env.ALB_ALLOWED_CIDR;
    if (!allowedCidr || allowedCidr.trim() === '0.0.0.0/0') {
      throw new Error(
        'A restricted ALB ingress CIDR is required. Provide one with:  '
        + 'cdk deploy -c allowedCidr=<your-cidr>  (or set ALB_ALLOWED_CIDR). '
        + '0.0.0.0/0 is not allowed — scope it to your office/VPN range.',
      );
    }

    // Explicit service SG: TCP only, no all-protocols egress. 443 reaches the
    // AWS APIs the task needs (ECR image pull, CloudWatch Logs, Secrets
    // Manager, ElastiCache API) via the NAT gateway; 6379 to the cache is
    // added below by `cacheSecurityGroup.connections.allowFrom`.
    const serviceSecurityGroup = new ec2.SecurityGroup(this, 'StorefrontServiceSg', {
      vpc,
      description: 'Storefront Fargate tasks',
      allowAllOutbound: false,
    });
    serviceSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'HTTPS to AWS service APIs (ECR, Logs, Secrets Manager, ElastiCache)',
    );

    const fargate = new ecsPatterns.ApplicationLoadBalancedFargateService(
      this,
      'Storefront',
      {
        cluster: ecsCluster,
        securityGroups: [serviceSecurityGroup],
        cpu: 512,
        memoryLimitMiB: 1024,
        desiredCount: 2,
        publicLoadBalancer: true,
        // HTTPS only. No HTTP listener, no HTTP->HTTPS redirect (which would
        // require opening port 80). Clients connect with https://.
        protocol: elbv2.ApplicationProtocol.HTTPS,
        listenerPort: 443,
        certificate,
        sslPolicy: elbv2.SslPolicy.TLS13_RES, // TLS 1.3
        redirectHTTP: false,
        // Don't let the pattern auto-open 443 to 0.0.0.0/0; we add a scoped
        // ingress rule from allowedCidr below instead.
        openListener: false,
        // Match the architecture of locally-built images. Building on Apple
        // Silicon produces arm64; running arm64 Fargate avoids an
        // 'exec format error' and is cheaper. (Graviton.)
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.ARM64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
        // Fail a bad deployment fast and roll back instead of waiting for hours
        // if tasks never become healthy.
        circuitBreaker: { rollback: true },
        minHealthyPercent: 50,
        // Tasks run in the private (egress) subnets; the ALB sits in public ones.
        taskSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        taskImageOptions: {
          image: appImage,
          containerPort: 8080,
          logDriver: ecs.LogDrivers.awsLogs({
            streamPrefix: 'storefront',
            logGroup: appLogGroup,
          }),
          environment: {
            REDIS_HOST: replicationGroup.attrConfigurationEndPointAddress,
            REDIS_PORT: '6379',
            REDIS_SSL: 'true',
            REDIS_CLUSTER_MODE: 'true',
            // ALB is HTTPS-only, so always send Secure cookies.
            SESSION_COOKIE_SECURE: 'true',
            ENABLE_FAILOVER_DEMO: 'true',
            REDIS_REPLICATION_GROUP_ID: replicationGroup.ref,
            REDIS_NODE_GROUP_ID: '0001',
            AWS_REGION: this.region,
          },
          secrets: {
            REDIS_AUTH_TOKEN: ecs.Secret.fromSecretsManager(
              this.authTokenSecret,
            ),
            SECRET_KEY: ecs.Secret.fromSecretsManager(flaskSecretKey),
          },
        },
      },
    );

    // Health check hits the app's backend-aware probe.
    fargate.targetGroup.configureHealthCheck({
      path: '/healthz',
      healthyHttpCodes: '200',
    });

    // Scoped ALB ingress: HTTPS (443) from the approved CIDR only — never
    // 0.0.0.0/0. (openListener:false above suppressed the pattern's default
    // internet-wide rule.)
    fargate.loadBalancer.connections.allowFrom(
      ec2.Peer.ipv4(allowedCidr),
      ec2.Port.tcp(443),
      'Allow HTTPS from the approved client network only',
    );

    // Let the Fargate tasks reach the cache. Grant ingress on the Valkey port
    // directly from the service's security group — this is more reliable than
    // attaching the client SG to the service (which does not always propagate
    // into the service's awsvpc networkConfiguration).
    cacheSecurityGroup.connections.allowFrom(
      fargate.service,
      ec2.Port.tcp(port),
      'Allow Valkey access from the storefront Fargate service',
    );

    // The failover demo button calls elasticache:TestFailover on THIS cluster.
    // ElastiCache replication-group ARNs use a COLON separator
    // (arn:...:replicationgroup:NAME), not the default slash — otherwise the
    // policy scopes to a non-matching ARN and TestFailover gets AccessDenied.
    fargate.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['elasticache:TestFailover'],
        resources: [
          cdk.Arn.format(
            {
              service: 'elasticache',
              resource: 'replicationgroup',
              resourceName: replicationGroup.ref,
              arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
            },
            this,
          ),
        ],
      }),
    );

    // The UI polls live failover progress via DescribeReplicationGroups. This
    // is a list/describe action that does not support resource-level scoping,
    // so it must target '*'. It is read-only.
    fargate.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['elasticache:DescribeReplicationGroups'],
        resources: ['*'],
      }),
    );
    NagSuppressions.addResourceSuppressions(
      fargate.taskDefinition.taskRole,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'elasticache:DescribeReplicationGroups is a read-only list action '
            + 'that does not support resource-level permissions and must use "*".',
          appliesTo: ['Resource::*'],
        },
      ],
      true,
    );

    // --- Outputs ------------------------------------------------------------
    new cdk.CfnOutput(this, 'ConfigurationEndpoint', {
      value: replicationGroup.attrConfigurationEndPointAddress,
      description:
        'Cluster configuration endpoint for the Valkey session store (REDIS_HOST)',
    });
    new cdk.CfnOutput(this, 'ConfigurationPort', {
      value: replicationGroup.attrConfigurationEndPointPort,
      description: 'Cluster configuration endpoint port (REDIS_PORT)',
    });
    new cdk.CfnOutput(this, 'AuthTokenSecretArn', {
      value: this.authTokenSecret.secretArn,
      description: 'Secrets Manager ARN holding the Valkey AUTH token',
    });
    new cdk.CfnOutput(this, 'ClientSecurityGroupId', {
      value: this.clientSecurityGroup.securityGroupId,
      description: 'Attach this SG to clients that need session-store access',
    });
    new cdk.CfnOutput(this, 'StorefrontUrl', {
      value: `https://${fargate.loadBalancer.loadBalancerDnsName}`,
      description: 'Open this in a browser (HTTPS) to use the storefront',
    });

    // --- cdk-nag suppressions (documented, justified exceptions) ------------
    NagSuppressions.addResourceSuppressions(
      this.authTokenSecret,
      [
        {
          id: 'AwsSolutions-SMG4',
          reason:
            'Valkey AUTH tokens cannot be rotated without a brief connection '
            + 'reset; rotation is handled operationally during maintenance '
            + 'windows rather than via automatic Secrets Manager rotation.',
        },
      ],
    );
    NagSuppressions.addResourceSuppressions(flaskSecretKey, [
      {
        id: 'AwsSolutions-SMG4',
        reason:
          'The Flask signing key is a static demo secret; rotation would '
          + 'invalidate active session cookies and is out of scope for this '
          + 'sample. Rotate manually if adopted for production.',
      },
    ]);

    // ALB is intentionally internet-facing: this is a public storefront demo.
    NagSuppressions.addResourceSuppressions(
      fargate.loadBalancer,
      [
        {
          id: 'AwsSolutions-ELB2',
          reason:
            'Access logging to S3 is omitted to keep the demo self-contained; '
            + 'enable it (and a log bucket) before any production use.',
        },
      ],
    );
    // Note: no AwsSolutions-EC23 suppression needed — the ALB security group
    // only allows 443 from the scoped `allowedCidr`, never 0.0.0.0/0.
    // Non-secret runtime configuration (endpoint, flags) is passed as plain
    // environment variables; real secrets use the `secrets` injection above.
    NagSuppressions.addResourceSuppressions(
      fargate.taskDefinition,
      [
        {
          id: 'AwsSolutions-ECS2',
          reason:
            'Only non-sensitive configuration is set as plaintext environment '
            + 'variables; the AUTH token and Flask key are injected from '
            + 'Secrets Manager via the task definition secrets.',
        },
      ],
    );
    // The ECS execution role uses AWS-managed wildcard permissions to pull the
    // image from ECR and write logs; this is the standard Fargate pattern.
    NagSuppressions.addResourceSuppressions(
      fargate.taskDefinition.executionRole!,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'ECS execution role requires ECR auth-token and CloudWatch Logs '
            + 'permissions that are inherently wildcard; scoped to this task '
            + 'only by the managed Fargate execution policy.',
        },
      ],
      true,
    );
  }
}
