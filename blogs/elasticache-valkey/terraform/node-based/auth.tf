#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key
resource "aws_kms_key" "encrypt_secret" {
  enable_key_rotation     = true
  description             = "Key to encrypt secret for ${var.name}."
  deletion_window_in_days = 7
  tags = {
    Name = "${var.name}-encrypt-secret"
  }
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_alias
resource "aws_kms_alias" "encrypt_secret" {
  name          = "alias/${var.name}-encrypt-secret"
  target_key_id = aws_kms_key.encrypt_secret.key_id
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key_policy
resource "aws_kms_key_policy" "encrypt_secret_policy" {
  key_id = aws_kms_key.encrypt_secret.id
  policy = jsonencode({
    Id      = "${var.name}-encrypt-secret"
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow access through AWS Secrets Manager for all principals in the account that are authorized to use AWS Secrets Manager"
        Effect = "Allow"
        Principal = {
          AWS = ["*"]
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
          "kms:DescribeKey",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = "${data.aws_caller_identity.current.account_id}"
            "kms:ViaService"    = "secretsmanager.${var.region}.amazonaws.com"
          }
        }
      }
    ]
  })
}
#https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/auth.html#auth-overview
#https://registry.terraform.io/providers/hashicorp/random/latest/docs/resources/password
resource "random_password" "auth" {
  length           = 128
  special          = true
  override_special = "!&#$^<>-"
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/secretsmanager_secret
resource "aws_secretsmanager_secret" "elasticache_auth" {
  name                    = var.name
  recovery_window_in_days = 0
  kms_key_id              = aws_kms_key.encrypt_secret.id
  #checkov:skip=CKV2_AWS_57: Disabled Secrets Manager secrets automatic rotation
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/secretsmanager_secret_version
resource "aws_secretsmanager_secret_version" "auth" {
  secret_id     = aws_secretsmanager_secret.elasticache_auth.id
  secret_string = random_password.auth.result
}
