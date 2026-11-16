# Getting started — start here

This is the one path from an empty AWS account to a running link. Every other
"quick start" in this repo is either the bundled sample ([TESTING.md](../TESTING.md),
which deploys its own demo database) or legacy (marked as such at the top of the
file).

| Step | What | Time |
|---|---|---|
| 0 | Tools, credentials and permissions | — |
| 1 | Publish the images into your account (`./release.sh push`) | 10–30 min |
| 2 | Deploy the control plane (UI + API) | ~5 min |
| 3 | Create a link (UI or `ecdl apply`) | 10–15 min |
| 4 | Verify, and subscribe to the alarms | 5 min |
| 5 | Tear down | 10 min |

---

## 0. Before you start

**Tools** on the machine you run this from:

| Tool | Used for |
|---|---|
| AWS CLI v2, with credentials for the target account | everything |
| Docker with `buildx` (Docker Desktop, or `docker-buildx-plugin`) | `./release.sh push` |
| Python 3.9+ with `pip install pyyaml boto3` | the `ecdl` CLI |
| `git` | cloning the repo |

Optional: `mysql` / `psql` (to create the signalling table, see the
[prerequisites](prerequisites.md)), `valkey-cli` (to read the cache).

**The `ecdl` CLI** is `ecdl-cli/ecdl` in this repo (run it as
`python3 ecdl-cli/ecdl ...` or put `ecdl-cli/` on your `PATH`). It uses the
standard AWS environment: `--profile` defaults to `$AWS_PROFILE` and `--region`
to `$AWS_REGION` / `$AWS_DEFAULT_REGION`, falling back to your profile's region.

**Permissions for the person running steps 1–2.** The control plane deploys links
with its OWN role (created by its template, scoped to resources named `ecdl-*`),
so you only need enough to publish images and create that one stack:

- ECR: `CreateRepository`, `DescribeRepositories`, `PutImageTagMutability`,
  `GetAuthorizationToken`, `InitiateLayerUpload`, `UploadLayerPart`,
  `CompleteLayerUpload`, `BatchCheckLayerAvailability`, `PutImage`,
  `BatchGetImage`
- CloudFormation: `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`,
  `DescribeStackEvents` on `ecdl-control-plane*`
- IAM: create a role and instance profile and pass the role to EC2
  (`CreateRole`, `PutRolePolicy`, `AttachRolePolicy`, `CreateInstanceProfile`,
  `AddRoleToInstanceProfile`, `PassRole`, and the `Delete*` counterparts)
- EC2: `RunInstances`, `CreateSecurityGroup`, `AuthorizeSecurityGroupIngress`,
  `Describe*`, `TerminateInstances`, `DeleteSecurityGroup`, `CreateTags`
- EC2 launch template (the instance takes its image from one):
  `CreateLaunchTemplate`, `CreateLaunchTemplateVersion`, `DeleteLaunchTemplate`
- SSM (AWS Systems Manager): `GetParameters` on
  `/aws/service/ami-amazon-linux-latest/*`, which EC2 reads to pick the image
  when it launches the instance


If you deploy links from the CLI instead of the control plane, your own identity
needs what the control plane's `InstanceRole` grants (see
`infra/ecdl-control-plane.yaml`, policies `resolve-links` and `deploy-links`).

**The source and the target must already exist**, and meet the
[prerequisites](prerequisites.md): binlog (MySQL) / WAL (PostgreSQL write-ahead log) settings, a database user and its
Secrets Manager secret, **log retention long enough to cover a paused capture**
(24h or more), and the signalling table.

**Network.** ElastiCache has no public endpoint. The control plane and the
pipeline must run in the VPC of the target cache (and reach the source). Their
subnets need a route to the AWS APIs — a NAT gateway, interface endpoints, or a
public IP.

---

## 1. Publish the images

The templates pull every image from **your account's own ECR**
(`<account>.dkr.ecr.<region>.amazonaws.com/ecdl/<image>:<version>`). Nothing is
published centrally yet, so until you push, the ECS tasks and the control-plane
instance fail with `CannotPullContainerError` / `manifest unknown`.

```bash
git clone <this-repo> elasticache-data-link && cd elasticache-data-link
export AWS_REGION=us-east-1            # the region you will deploy into
./release.sh show                      # what will be built, and where it goes
./release.sh push                      # builds and pushes all five images
```

It publishes `ecdl-sink`, `ecdl-debezium-server`, `ecdl-loader`,
`ecdl-control-plane-api` and `ecdl-control-plane-ui` at the version in `VERSION`,
into repositories with immutable tags. A multi-architecture build needs a host
with 4 GB of memory or more; see [README → Build host](../README.md#build-host-and-cpu-architecture)
for the faster single-architecture option.

---

## 2. Deploy the control plane

The control plane is one EC2 instance running the API (port 5000) and the UI
(port 3000), with an IAM role that can deploy links. Anyone who can use the UI
can create and delete pipelines with that role, so **how the UI is reached is
the access control**. You choose it with `AccessMode`:

| `AccessMode` | Who can use the UI | You provide |
|---|---|---|
| `ssm` (default) | People whose IAM permissions let them start an SSM session on the instance. No inbound ports; each session is recorded in CloudTrail and the tunnel is encrypted. | Nothing beyond IAM permissions |
| `load-balancer` | People who sign in through your own HTTPS load balancer, for example with your company SSO. The instance accepts traffic only from that load balancer. | The load balancer, certificate, domain and identity provider (see [2b](#2b-optional-sign-in-through-your-own-load-balancer-sso)) |
| `cidr` | Anyone inside `AllowedCIDR`, with **no login** and plain HTTP | A narrow CIDR, never a whole company network |

Start with `ssm`. Move to `load-balancer` when a team needs browser sign-in
with your identity provider.

```bash
aws cloudformation create-stack \
  --stack-name ecdl-control-plane \
  --template-body file://infra/ecdl-control-plane.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-0123456789abcdef0 \
    ParameterKey=SubnetId,ParameterValue=subnet-0123456789abcdef0 \
    ParameterKey=DeployVpcId,ParameterValue=vpc-0123456789abcdef0
aws cloudformation wait stack-create-complete --stack-name ecdl-control-plane
aws cloudformation describe-stacks --stack-name ecdl-control-plane \
  --query 'Stacks[0].Outputs' --output table
```

That creates it in `ssm` mode. The `OpenUi` output says how to open the UI in
whichever mode you chose.

**Open the UI (`ssm` mode).** On your machine you need the AWS CLI v2 and the
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
Then:

```bash
ecdl-cli/ecdl-ui ecdl-control-plane --region us-east-1
```

and open http://localhost:3000. The script forwards the UI (3000) and API (5000)
from the instance to your machine and closes both tunnels on Ctrl-C. The
`UiTunnelCommand` output shows the underlying `aws ssm start-session` command if
you prefer to run it yourself.

To let a person use the UI without giving them a shell on the instance, allow
only the port-forwarding document:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "ssm:StartSession",
    "Resource": [
      "arn:aws:ec2:us-east-1:111122223333:instance/i-0123456789abcdef0",
      "arn:aws:ssm:us-east-1::document/AWS-StartPortForwardingSession"
    ],
    "Condition": {"BoolIfExists": {"ssm:SessionDocumentAccessCheck": "true"}}
  }, {
    "Effect": "Allow",
    "Action": ["ssm:TerminateSession", "ssm:ResumeSession"],
    "Resource": "arn:aws:ssm:*:*:session/${aws:userid}-*"
  }, {
    "Effect": "Allow",
    "Action": "cloudformation:DescribeStacks",
    "Resource": "arn:aws:cloudformation:us-east-1:111122223333:stack/ecdl-control-plane/*"
  }]
}
```

Other parameters:

- **`AssignPublicIp`**: `true` when the subnet's only route out is an internet
  gateway (no NAT gateway or VPC endpoints), otherwise the instance cannot pull
  its images and the stack fails. `false` (the default) means no public IP even
  in a subnet that assigns them automatically. It opens no port; inbound access
  is decided by `AccessMode`.
- **`DeployVpcId`** confines the security groups it may create to that VPC.
  Recommended.
- **`MonitorSourceSecretArns`** (optional, comma separated): source secrets the
  Pipeline Monitor may read. Without one, the monitor shows the broker, sink,
  cache and delivery integrity but not the source-side panels (database
  activity, source-vs-cache rows, the **Measure now** lag probe), because those
  log in to the source. With one, anyone who can open the UI sees sample rows
  from that database.
- **`PipelineSecretArns`** (optional, comma separated): the source and target
  secrets the pipeline tasks may read. Every task role the control plane creates
  carries a permissions boundary (`TaskRoleBoundary`) that limits it to pulling
  images, writing its logs, reading these secrets and publishing ECDL metrics,
  whatever policy is attached to it. Empty allows any secret, because a
  database secret can have any name.
- **`AllowedCIDR`** (`cidr` mode only): the range allowed in, with no login. It
  cannot be `0.0.0.0/0`. Open the UI by the instance's IP address (the
  `PrivateUrl` or `PublicUrl` output): the API accepts changes only from pages it
  can recognise as its own, and does not trust a DNS name on another port.

In every mode the API refuses changes sent by a web page from any other site,
so a page you visit while the SSM tunnel is open cannot deploy or delete
anything through it.

### 2b. Optional: sign in through your own load balancer (SSO)

Every organisation's sign-in is different, so ECDL does not create a load
balancer or connect to an identity provider. It accepts traffic from yours.

1. **Create an internet-facing or internal Application Load Balancer** in the
   control plane's VPC, with an HTTPS listener using your certificate
   (AWS Certificate Manager) and your domain name.
2. **Add authentication to the listener.** Use the `authenticate-oidc` action
   with your identity provider (IAM Identity Center, Okta, Microsoft Entra ID and
   others support OpenID Connect), or `authenticate-cognito` with a Cognito user
   pool federated to a SAML provider. See
   [Authenticate users using an Application Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html).
3. **Create two target groups** for the control-plane instance (the
   `InstanceId` output), protocol HTTP:
   - API: port 5000, health check path `/api/health`
   - UI: port 3000, health check path `/`
4. **Listener rules, after the authenticate action:** path `/api/*` forwards to
   the API target group; the default forwards to the UI target group. The UI
   calls the API on its own address, so one sign-in covers both.
5. **Update the control-plane stack** with `AccessMode=load-balancer` and
   `LoadBalancerSecurityGroupId` set to the load balancer's security group. The
   instance then accepts ports 3000 and 5000 from that group only, so nothing
   can reach it without passing the sign-in.
6. **Check the sign-in is enforced:** open the load balancer's URL in a private
   browser window. You should be sent to your identity provider before any ECDL
   page loads. `curl -i https://<your-domain>/api/health` should return a
   redirect to the identity provider (302), not ECDL's JSON.

The ALB adds the signed-in user's identity in the `x-amzn-oidc-identity` header.
ECDL does not read it yet, so it does not record which person made a change;
your load balancer access logs do.

The stack only reports `CREATE_COMPLETE` once the API answers on :5000. If it
fails, the instance's `/var/log/cloud-init-output.log` (via SSM) names the step.

**Local alternative (development only).** `docker compose up` in the repo root
runs the same API and UI on your machine, bound to loopback only (UI
http://localhost:3001, API :5006), using your local AWS credentials and region
`us-east-1` unless you change `AWS_DEFAULT_REGION` in `docker-compose.yml`. A
laptop cannot reach ElastiCache, so the monitor shows broker and cache figures
as unknown.

---

## 3. Create a link

A **link** is one file naming the source, the target and the tables. The fields
are documented in the [README, "A link"](../README.md#a-link) and the schema
(`ecdl_link/link.schema.json`). In the UI: **1. Define → Build Pipeline** walks you through
it and shows the plan before deploying. From the CLI:

```bash
python3 ecdl-cli/ecdl validate my-link.yaml   # no AWS calls
python3 ecdl-cli/ecdl plan my-link.yaml       # the ordered steps; changes nothing
python3 ecdl-cli/ecdl apply my-link.yaml      # deploys, printing each step
```

What a first `initial_load_and_cdc` deploy does, in order: broker → Debezium and
sink → wait for Debezium's first committed position → pause capture → bulk load
every table → resume capture. If any step after the pause fails, capture is
resumed before the error is reported, and the error says what is left to do.

Applying the same link again **updates the stacks in place**: capture keeps
running and nothing is reloaded. To run the bulk load again (for example after a
load failed), use `ecdl apply --reload` or **Update and reload all tables** in
the UI.

**Alarms.** Each link gets CloudWatch alarms (capture not running, sink not
running, sink lag, dead letters, broker memory, sustained errors) on an SNS
topic. To be emailed, add to the link:

```yaml
advanced:
  alarms:
    email: oncall@example.com
```

and confirm the subscription email. Or subscribe anything else to the CDC (change data capture) stack's
`AlarmTopicArn` output. What each alarm means: [operations](operations.md).

---

## 4. Verify

- **UI:** **2. Deploy → Deployments** shows the link's stacks and tasks;
  **3. Observe → Pipeline Monitor**: every hop green, a CDC lag figure, and
  delivery integrity `ok`.
- **Cache:** from inside the VPC,
  `valkey-cli -h <target endpoint> -p 6379 --tls -c HGETALL <table>:<pk>` (pk is the row's primary key). Each
  hash carries one extra field, `_ecdl_v`, the version the sink uses to keep a
  retried change from overwriting a newer one; it is not a column.
- **Live:** update a row in the source and read the key again a second later.

---

## 5. Tear down

In the UI: **Deployments**, select the pipeline, **Delete pipeline**, type its
name to confirm. Or from a terminal:

```bash
python3 ecdl-cli/ecdl destroy --link <link>      # the link's stacks; keeps source and target
aws cloudformation delete-stack --stack-name ecdl-control-plane
```

Destroy every link **before** the control plane. Each link's broker admits the
control plane's security group (so the dashboard can read it), and while that
rule exists the control-plane stack fails to delete with `SecurityGroup ... has
a dependent object`.

`destroy` prints what it leaves for you, most importantly on **PostgreSQL** the
`pg_drop_replication_slot(...)` statement: an abandoned slot keeps WAL until the
source's disk fills. The ECR repositories are kept; delete them with
`aws ecr delete-repository --force --repository-name ecdl/<image>` if wanted.

---

When something is wrong, or to change a running link:
**[operations.md](operations.md)** (alarms, troubleshooting, dead-letter replay,
adding tables, changing transforms, rotating credentials).
