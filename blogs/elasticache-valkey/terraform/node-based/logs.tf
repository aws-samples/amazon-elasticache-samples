locals {
  principal_logs_arn = "logs.${var.region}.amazonaws.com"
  slow_log_arn       = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/elasticache/${var.name}/slow-log"
  engine_log_arn     = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/elasticache/${var.name}/engine-log"
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key
resource "aws_kms_key" "encrypt_logs" {
  enable_key_rotation     = true
  description             = "Key to encrypt logs for ${var.name}."
  deletion_window_in_days = 7
  tags = {
    Name = "${var.name}-encrypt-logs"
  }
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_alias
resource "aws_kms_alias" "encrypt_logs" {
  name          = "alias/${var.name}-encrypt-logs"
  target_key_id = aws_kms_key.encrypt_logs.key_id
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key_policy
resource "aws_kms_key_policy" "encrypt_logs_policy" {
  key_id = aws_kms_key.encrypt_logs.id
  policy = jsonencode({
    Id = "${var.name}-encrypt-logs"
    Statement = [
      {
        Action = ["kms:*"]
        Effect = "Allow"
        Principal = {
          AWS = "${local.principal_root_arn}"
        }
        Resource = "*"
        Sid      = "Enable IAM User Permissions"
      },
      {
        Sid    = "Allow ElastiCache to use the key"
        Effect = "Allow"
        Principal = {
          Service = "elasticache.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
          "kms:DescribeKey"
        ]
        Resource = "*"
      },
      {
        Effect : "Allow",
        Principal : {
          Service : "${local.principal_logs_arn}"
        },
        Action : [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ],
        Resource : "*",
        Condition : {
          ArnEquals : {
            "kms:EncryptionContext:aws:logs:arn" : [local.slow_log_arn, local.engine_log_arn]
          }
        }
      }
    ]
    Version = "2012-10-17"
  })
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group
resource "aws_cloudwatch_log_group" "slow_log" {
  name              = "/elasticache/${var.name}/slow-log"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.encrypt_logs.arn
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group
resource "aws_cloudwatch_log_group" "engine_log" {
  name              = "/elasticache/${var.name}/engine-log"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.encrypt_logs.arn
}