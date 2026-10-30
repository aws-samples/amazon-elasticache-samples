#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { Aspects } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { SessionStoreStack } from '../lib/session-store-stack';

const app = new cdk.App();

// Environment-agnostic on purpose: no account ID is baked into the template
// (ARNs use the AWS::AccountId pseudo-parameter) and no context lookups are
// cached in cdk.context.json. The account and region come from your
// credentials at deploy time.
new SessionStoreStack(app, 'DurableSessionStoreStack', {
  description:
    'Durable ElastiCache (Valkey) session store: Multi-AZ replication group, '
    + 'encryption at rest + in transit, automatic snapshots, private subnets.',
});

// Fail synth/deploy on AWS security best-practice violations.
Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

app.synth();
