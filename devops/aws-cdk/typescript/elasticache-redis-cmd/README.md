# Amazon ElastiCache for Redis Cluster Mode Disabled [CDK Stack]

Pre-requisites
- [Git](https://git-scm.com/)
- [NodeJS](https://nodejs.org/en)
- [AWS CDK](https://github.com/aws/aws-cdk)

Clone this repository
```bash
git clone git@github.com:aws-samples/amazon-elasticache-samples.git
```

Enter this folder
```bash
devops/aws-cdk/typescript/elasticache-redis-cmd/
```

Install dependencies
```bash
npm install
```

Deploy Amazon ElastiCache for Redis Cluster Mode Disabled
```bash
cdk synth; cdk deploy --require-approval never
```

Cleanup your environment
```bash
cdk destroy -f
```

## How does it work?

Snippet from `src/elasticache-redis-cmd-stack.ts`

```typescript
const elastiCacheRedisCMD = new ElastiCache.CfnReplicationGroup(
    this,
    elastiCacheRedisCMDName.toLowerCase(),
    {
        replicationGroupDescription: "ElastiCache for Redis Cluster Mode Enabled CDK",
        numCacheClusters: 2,
        automaticFailoverEnabled: true,
        engine: "redis",
        cacheNodeType: "cache.t4g.micro",
        cacheSubnetGroupName: elastiCacheSubnetGroup.ref,
        securityGroupIds: [elastiCacheSecurityGroup.securityGroupId]
    }
);
```

## Useful commands

* `npm run build`   compile typescript to js
* `npm run watch`   watch for changes and compile
* `npm run test`    perform the jest unit tests
* `npx cdk deploy`  deploy this stack to your default AWS account/region
* `npx cdk diff`    compare deployed stack with current state
* `npx cdk synth`   emits the synthesized CloudFormation template
