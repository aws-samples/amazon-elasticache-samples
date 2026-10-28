# Changelog

## 1.0.0 — Initial release

### Application
- Spring Boot 4.1.1 backend (Java 17) using the AWS Advanced JDBC Wrapper 4.4.0
  Remote Query Cache plugin with Amazon ElastiCache for Valkey. Caching is
  enabled per query with a `/* CACHE_PARAM(ttl=...) */` SQL hint.
- Shopping assistant built on the Amazon Bedrock Converse API with tool calls
  that run cached JDBC queries.
- React frontend with a live performance dashboard (cache hits, latency).
- JDBC URL components (host, port, database name) are validated against strict
  allowlists before the connection string is built.

### Infrastructure (`infra/cloudformation.yml`)
- Aurora PostgreSQL Serverless v2 (encrypted at rest, IAM database
  authentication enabled), ElastiCache Serverless for Valkey, and an EC2 app
  instance on Amazon Linux 2023 (AMI resolved via SSM public parameter).
- Security groups are restricted to caller-supplied CIDRs (`AllowedSshCidr`,
  `AllowedAppCidr`); the database and cache are reachable only from the app
  security group.
- The instance role is least privilege: `bedrock:InvokeModel` on the configured
  inference profile and model only, and `s3:GetObject` on the app tarball only.
- The DB password has no default and must be supplied at deploy time
  (`DB_PASSWORD` environment variable for `infra/deploy.sh`).

### Demo-only considerations
- The stack is intended for short-lived demos with synthetic data. The DB
  password is passed to the instance through UserData for simplicity. For real
  workloads, use AWS Secrets Manager (for example `ManageMasterUserPassword`)
  or IAM database authentication instead.
- The frontend runs with the React development server. For anything beyond a
  demo, build the frontend (`npm run build`) and serve the static files.
- The API endpoints have no authentication. Restrict access with the CIDR
  parameters and delete the stack after use.
