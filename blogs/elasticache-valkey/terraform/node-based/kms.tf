data "aws_caller_identity" "current" {}
locals {
  principal_root_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key
resource "aws_kms_key" "encrypt_cache" {
  description             = "KMS key to encrypt cache for ${var.name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_alias
resource "aws_kms_alias" "key" {
  name          = "alias/${var.name}-encrypt-cache"
  target_key_id = aws_kms_key.encrypt_cache.id
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key_policy
resource "aws_kms_key_policy" "encrypt_policy" {
  key_id = aws_kms_key.encrypt_cache.id
  policy = jsonencode({
    Id = "${var.name}-encrypt-cache"
    Statement = [
      {
        Action = "kms:*"
        Effect = "Allow"
        Principal = {
          AWS = "${local.principal_root_arn}"
        }
        Resource = "*"
        Sid      = "Enable IAM User Permissions"
      },
      {
        Sid    = "Allow ElastiCache Service"
        Effect = "Allow"
        Principal = {
          Service = "elasticache.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:CreateGrant",
          "kms:ReEncrypt*",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "elasticache.${var.region}.amazonaws.com"
          }
        }
      }
    ]
    Version = "2012-10-17"
  })
}