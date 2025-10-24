terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    opensearch = {
      source  = "opensearch-project/opensearch"
      version = "~> 2.2"
    }
  }
}

resource "random_string" "random" {
  length  = 8
  lower   = true
  special = false
  upper   = false
}

locals {
  name   = local.config.deployment_name
  region = local.config.aws_region
  python_cmd = substr(pathexpand("~"), 0, 1) == "/" ? "python3" : "python"

  vpc_cidr = "10.0.0.0/16"
  azs      = ["${local.config.aws_region}a", "${local.config.aws_region}b", "${local.config.aws_region}c"]
  tags = {
    environment = "development"
  }
  collection_name = "${local.config.deployment_name}-${random_string.random.result}"
}

################################################################################
# Providers
################################################################################

provider "aws" {
  region = local.region
}

provider "opensearch" {
  url         = aws_opensearchserverless_collection.knowledge_base.collection_endpoint
  healthcheck = false
  aws_region  = local.region
  insecure    = true
}

################################################################################
# Networking
################################################################################

resource "aws_vpc" "main" {
  cidr_block           = local.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(local.tags, {
    Name = local.name
  })
}

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(local.vpc_cidr, 4, count.index)
  availability_zone = local.azs[count.index]

  tags = merge(local.tags, {
    Name = "${local.name}-private-${count.index + 1}"
  })
}

resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(local.vpc_cidr, 8, count.index + 48)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.tags, {
    Name = "${local.name}-public-${count.index + 1}"
  })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.tags, {
    Name = local.name
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.tags, {
    Name = "${local.name}-public"
  })
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.tags, {
    Name = "${local.name}-private"
  })
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# VPC Endpoints for AWS services (no internet needed)
resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "bedrock_agent_runtime" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${local.region}.bedrock-agent-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

resource "aws_security_group" "vpc_endpoints" {
  name   = "vpc-endpoints-sg"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [local.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

################################################################################
# Valkey (ElastiCache)
################################################################################

resource "aws_security_group" "semantic_cache_sg" {
  name   = "semantic-cache-sg"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["10.0.0.0/8"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_elasticache_subnet_group" "semantic_cache_subnets" {
  name       = "semantic-cache-subnet-group"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "semantic_cache_cluster" {
  description              = "Valkey cluster for semantic cache"
  replication_group_id     = "semantic-cache-valkey"
  engine                   = "valkey"
  engine_version           = "8.2"
  node_type                = "cache.r6g.large"
  num_cache_clusters       = 1
  port                     = 6379
  parameter_group_name     = "default.valkey8"
  subnet_group_name        = aws_elasticache_subnet_group.semantic_cache_subnets.name
  security_group_ids       = [aws_security_group.semantic_cache_sg.id]
  at_rest_encryption_enabled = true
}

################################################################################
# Lambda
################################################################################

module "lambda" {
  source = "terraform-aws-modules/lambda/aws"
  version = "~> 4.0"

  tracing_mode = "Active"
  environment_variables = {
    "KNOWLEDGE_BASE_ID" : aws_bedrockagent_knowledge_base.docs_small.id
    "VALKEY_HOST" : aws_elasticache_replication_group.semantic_cache_cluster.primary_endpoint_address
    "REGION" : local.config.aws_region
    "AWS_ACCOUNT_ID" : local.config.aws_account_id
    "INFERENCE_PROFILE" : local.config.inference_profile
  }
  function_name = "semantic-cache-function-${random_string.random.result}"
  description   = "Semantic cache function with Valkey and Knowledge Base"
  handler       = "app.lambda_handler"
  runtime       = "python3.12"

  source_path = "./lambda"

  vpc_subnet_ids                     = aws_subnet.private[*].id
  vpc_security_group_ids             = [aws_security_group.semantic_cache_sg.id]
  attach_network_policy              = true
  replace_security_groups_on_destroy = true
  timeout                            = 30
  attach_policy_json                 = true
  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:RetrieveAndGenerate",
          "bedrock:Retrieve",
          "bedrock:GetInferenceProfile",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-7-sonnet-*",
          "arn:aws:bedrock:${local.region}::foundation-model/amazon.titan-embed-text-v2:0",
          "arn:aws:bedrock:${local.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*",
          "arn:aws:bedrock:${local.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*"
        ]
      }
    ]
  })
}

################################################################################
# API Gateway
################################################################################

resource "aws_api_gateway_rest_api" "semantic_cache_api" {
  name        = "semantic-cache-api"
  description = "Semantic cache API with IAM auth"
  
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = "*"
        Action = "execute-api:Invoke"
        Resource = "*"
        Condition = {
          IpAddress = {
            "aws:SourceIp" = local.config.allowed_ip_cidr
          }
        }
      }
    ]
  })
}

resource "aws_api_gateway_resource" "search" {
  rest_api_id = aws_api_gateway_rest_api.semantic_cache_api.id
  parent_id   = aws_api_gateway_rest_api.semantic_cache_api.root_resource_id
  path_part   = "search"
}

resource "aws_api_gateway_method" "method_post" {
  rest_api_id   = aws_api_gateway_rest_api.semantic_cache_api.id
  resource_id   = aws_api_gateway_resource.search.id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "lambda" {
  rest_api_id = aws_api_gateway_rest_api.semantic_cache_api.id
  resource_id = aws_api_gateway_method.method_post.resource_id
  http_method = aws_api_gateway_method.method_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.lambda.lambda_function_invoke_arn
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.lambda.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.semantic_cache_api.execution_arn}/*"
}

resource "aws_api_gateway_deployment" "semantic_cache_api_dev" {
  depends_on = [
    aws_api_gateway_integration.lambda
  ]

  rest_api_id = aws_api_gateway_rest_api.semantic_cache_api.id
  stage_name  = "dev"
}

################################################################################
# OpenSearch Serverless
################################################################################

resource "aws_opensearchserverless_security_policy" "encryption_policy" {
  name        = "encryption-policy-${random_string.random.result}"
  type        = "encryption"
  description = "encryption policy for ${local.collection_name}"
  policy = jsonencode({
    Rules = [
      {
        Resource = [
          "collection/${local.collection_name}"
        ],
        ResourceType = "collection"
      }
    ],
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "network_policy" {
  name        = "network-policy-${random_string.random.result}"
  type        = "network"
  description = "public access for dashboard, public access for collection endpoint"
  policy = jsonencode([
    {
      Description = "Public access for endpoint",
      Rules = [
        {
          ResourceType = "collection",
          Resource = [
            "collection/${local.collection_name}"
          ]
        },
        {
          ResourceType = "dashboard"
          Resource = [
            "collection/${local.collection_name}"
          ]
        }
      ],
      AllowFromPublic = true
    }
  ])
}

data "aws_caller_identity" "current" {}

resource "aws_opensearchserverless_access_policy" "admin_data_access_policy" {
  name        = "access-policy-${random_string.random.result}"
  type        = "data"
  description = "allow index and collection access"
  policy = jsonencode([
    {
      Rules = [
        {
          ResourceType = "index",
          Resource = [
            "index/${local.collection_name}/*"
          ],
          Permission = [
            "aoss:*"
          ]
        },
        {
          ResourceType = "collection",
          Resource = [
            "collection/${local.collection_name}"
          ],
          Permission = [
            "aoss:*"
          ]
        }
      ],
      Principal = [
        data.aws_caller_identity.current.arn,
        aws_iam_role.knowledge_base_role.arn
      ]
    }
  ])

  # Sleep for policy propagation
  provisioner "local-exec" {
    command = "${local.python_cmd} sleep.py 120"
  }
}

resource "aws_opensearchserverless_collection" "knowledge_base" {
  name = local.collection_name
  type = "VECTORSEARCH"
  depends_on = [
    aws_opensearchserverless_security_policy.encryption_policy,
    aws_opensearchserverless_security_policy.network_policy
  ]
}

################################################################################
# Bedrock Knowledge Base
################################################################################

resource "aws_s3_bucket" "knowledge_base" {
  bucket        = "semantic-cache-kb-${random_string.random.result}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "knowledge_base" {
  bucket = aws_s3_bucket.knowledge_base.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_iam_role" "knowledge_base_role" {
  name = "AmazonBedrockExecutionRoleForKnowledgeBase_${local.region}_${random_string.random.result}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Sid    = "AmazonBedrockKnowledgeBaseTrustPolicy"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock:${local.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"
          }
        }
      }
    ]
  })

  inline_policy {
    name = "AmazonBedrockFoundationModelPolicyForKnowledgeBase_${local.region}"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Action   = ["bedrock:InvokeModel"]
          Effect   = "Allow"
          Resource = "arn:aws:bedrock:${local.region}::foundation-model/amazon.titan-embed-text-v2:0"
        }
      ]
    })
  }

  inline_policy {
    name = "AmazonBedrockOSSPolicyForKnowledgeBase_${local.region}"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Action   = ["aoss:APIAccessAll"]
          Effect   = "Allow"
          Resource = aws_opensearchserverless_collection.knowledge_base.arn
        }
      ]
    })
  }

  inline_policy {
    name = "S3ListBucketStatement_${local.region}"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Action   = ["s3:ListBucket"]
          Effect   = "Allow"
          Resource = aws_s3_bucket.knowledge_base.arn
        },
        {
          Action   = ["s3:GetObject"]
          Effect   = "Allow"
          Resource = "${aws_s3_bucket.knowledge_base.arn}/*"
        }
      ]
    })
  }
}

resource "opensearch_index" "bedrock_knowledge_base_default_index" {
  name               = "bedrock-knowledge-base-default-index"
  number_of_shards   = "2"
  number_of_replicas = 0
  index_knn          = true
  force_destroy      = true
  depends_on = [
    aws_opensearchserverless_collection.knowledge_base,
    aws_opensearchserverless_access_policy.admin_data_access_policy
  ]
  lifecycle {
    ignore_changes = [mappings]
  }
  mappings = jsonencode({
    properties = {
      AMAZON_BEDROCK_METADATA = {
        type  = "text"
        index = false
      }
      AMAZON_BEDROCK_TEXT_CHUNK = {
        type = "text"
      }
      "bedrock-knowledge-base-default-vector" = {
        type      = "knn_vector"
        dimension = 1024
        method = {
          engine     = "faiss"
          space_type = "l2"
          name       = "hnsw"
          parameters = {}
        }
      }
      id = {
        type = "text"
        fields = {
          keyword = {
            type         = "keyword"
            ignore_above = 256
          }
        }
      }
      "x-amz-bedrock-kb-source-uri" = {
        type = "text"
        fields = {
          keyword = {
            type         = "keyword"
            ignore_above = 256
          }
        }
      }
    }
  })

  provisioner "local-exec" {
    command = "${local.python_cmd} sleep.py 30"
  }
}

resource "aws_bedrockagent_knowledge_base" "docs_small" {
  name     = "semantic-cache-kb-${random_string.random.result}"
  role_arn = aws_iam_role.knowledge_base_role.arn
  knowledge_base_configuration {
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${local.region}::foundation-model/amazon.titan-embed-text-v2:0"
    }
    type = "VECTOR"
  }
  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.knowledge_base.arn
      vector_index_name = "bedrock-knowledge-base-default-index"
      field_mapping {
        vector_field   = "bedrock-knowledge-base-default-vector"
        text_field     = "AMAZON_BEDROCK_TEXT_CHUNK"
        metadata_field = "AMAZON_BEDROCK_METADATA"
      }
    }
  }
  depends_on = [opensearch_index.bedrock_knowledge_base_default_index]
}

resource "aws_bedrockagent_data_source" "s3" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.docs_small.id
  name              = "s3-default"
  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.knowledge_base.arn
    }
  }
}

################################################################################
# Outputs
################################################################################

################################################################################
# Valkey Index Creation
################################################################################

module "valkey_index_creator" {
  source = "terraform-aws-modules/lambda/aws"
  version = "~> 4.0"

  function_name = "valkey-index-creator-${local.region}-${random_string.random.result}"
  handler       = "index.handler"
  runtime       = "python3.12"
  timeout       = 60

  source_path = [
    {
      path = "${path.module}/lambda_valkey"
      pip_requirements = true
    }
  ]

  vpc_subnet_ids         = aws_subnet.private[*].id
  vpc_security_group_ids = [aws_security_group.semantic_cache_sg.id]
  attach_network_policy  = true

  environment_variables = {
    VALKEY_HOST = aws_elasticache_replication_group.semantic_cache_cluster.primary_endpoint_address
  }

  depends_on = [aws_elasticache_replication_group.semantic_cache_cluster]
}

resource "aws_lambda_invocation" "valkey_index_creation" {
  function_name = module.valkey_index_creator.lambda_function_name
  input = "{}"
  depends_on = [module.valkey_index_creator]
}

################################################################################
# Outputs
################################################################################

output "api_gateway_url" {
  description = "API Gateway URL"
  value       = "https://${aws_api_gateway_rest_api.semantic_cache_api.id}.execute-api.${local.region}.amazonaws.com/dev/search"
}

output "valkey_endpoint" {
  description = "Valkey cluster endpoint"
  value       = aws_elasticache_replication_group.semantic_cache_cluster.primary_endpoint_address
}

output "opensearch_collection_endpoint" {
  description = "OpenSearch collection endpoint"
  value       = aws_opensearchserverless_collection.knowledge_base.collection_endpoint
}

output "knowledge_base_id" {
  description = "Bedrock Knowledge Base ID"
  value       = aws_bedrockagent_knowledge_base.docs_small.id
}

output "s3_bucket" {
  description = "S3 bucket for knowledge base data"
  value       = aws_s3_bucket.knowledge_base.bucket
}

output "data_source_id" {
  description = "Bedrock Knowledge Base data source ID"
  value       = aws_bedrockagent_data_source.s3.data_source_id
}

# Cleanup helper for opensearch index during destroy
resource "null_resource" "opensearch_cleanup" {
  triggers = {
    always_run = timestamp()
  }
  
  provisioner "local-exec" {
    when    = destroy
    command = "terraform state rm opensearch_index.bedrock_knowledge_base_default_index 2>/dev/null || true"
  }
}
