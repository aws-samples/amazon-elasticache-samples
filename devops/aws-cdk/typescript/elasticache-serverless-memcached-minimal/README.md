# Amazon ElastiCache Serverless for Memcached [Minimal CDK]

Pre-requisites
- [Git](https://git-scm.com/)
- [NodeJS](https://nodejs.org/en)
- [AWS CDK](https://github.com/aws/aws-cdk)

Clone this repository
```bash
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
```

Enter this folder
```bash
cd devops/aws-cdk/typescript/elasticache-serverless-memcached-minimal/
```

Install dependencies
```bash
npm install
cdk bootstrap
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

Snippet from `src/elasticache-serverless-minimal-stack.ts`

```typescript
const elastiCacheServerlessName = "ElastiCacheServerlessMemcached";
new ElastiCache.CfnServerlessCache(this, "ServerlessCache", {
    engine: "memcached",
    serverlessCacheName: elastiCacheServerlessName.toLowerCase(),
});
```

## Useful commands

* `npm run build`   compile typescript to js
* `npm run watch`   watch for changes and compile
* `npm run test`    perform the jest unit tests
* `npx cdk deploy`  deploy this stack to your default AWS account/region
* `npx cdk diff`    compare deployed stack with current state
* `npx cdk synth`   emits the synthesized CloudFormation template
