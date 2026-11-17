// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { aws_elasticache as ElastiCache } from "aws-cdk-lib";

export class ElasticacheServerlessMinimalStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    const elastiCacheServerlessName = "ElastiCacheServerlessValkey";
    new ElastiCache.CfnServerlessCache(this, "ServerlessCache", {
      engine: "valkey",
      serverlessCacheName: elastiCacheServerlessName.toLowerCase(),
    });
  }
}
