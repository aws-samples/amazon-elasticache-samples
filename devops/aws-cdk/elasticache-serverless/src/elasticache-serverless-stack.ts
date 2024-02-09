import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as EC2 from "aws-cdk-lib/aws-ec2";
import { aws_elasticache as ElastiCache } from "aws-cdk-lib";
import { SecurityGroup, Peer, Port } from "aws-cdk-lib/aws-ec2";

export class ElasticacheServerlessStack extends cdk.Stack {
  private vpc: EC2.Vpc;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    const elastiCacheRedisPort = 6379;
    const elastiCacheRedisPortReadOnly = 6380;
    const elastiCacheVPCName = "ElastiCacheVPC";
    const elastiCacheSubnetIds = [];
    const elastiCacheSubnetGroupName = "ElastiCacheSubnetGroup";
    const elastiCacheSecurityGroupName = "ElastiCacheSecurityGroup";
    const elastiCacheServerlessName = "ElastiCacheServerless";

    this.vpc = new EC2.Vpc(this, elastiCacheVPCName.toLowerCase());

    for (const subnet of this.vpc.privateSubnets) {
      console.log(`Private Subnet Id: ${subnet.subnetId}`);
      elastiCacheSubnetIds.push(subnet.subnetId);
    }

    const elastiCacheSubnetGroup = new ElastiCache.CfnSubnetGroup(
      this,
      elastiCacheSubnetGroupName.toLowerCase(),
      {
        description: "ElastiCache Subnet Group CDK",
        cacheSubnetGroupName: elastiCacheSubnetGroupName.toLowerCase(),
        subnetIds: elastiCacheSubnetIds,
      }
    );

    const elastiCacheSecurityGroup = new SecurityGroup(this, elastiCacheSecurityGroupName.toLowerCase(), {
      vpc: this.vpc,
      allowAllOutbound: true,
      description: "ElastiCache Security Group CDK",
      securityGroupName: elastiCacheSecurityGroupName.toLowerCase(),
    });
    elastiCacheSecurityGroup.addIngressRule(Peer.anyIpv4(), Port.tcp(elastiCacheRedisPort), "ElastiCache for Redis Port");
    elastiCacheSecurityGroup.addIngressRule(Peer.anyIpv4(), Port.tcp(elastiCacheRedisPortReadOnly), "ElastiCache for Redis Read Only Port");
    
    const elastiCacheServerless = new ElastiCache.CfnServerlessCache(this, elastiCacheServerlessName.toLowerCase(), {
      engine: "redis",
      serverlessCacheName: elastiCacheServerlessName.toLowerCase(),
      securityGroupIds: [elastiCacheSecurityGroup.securityGroupId],
      subnetIds: elastiCacheSubnetIds,
    });
  }
}
