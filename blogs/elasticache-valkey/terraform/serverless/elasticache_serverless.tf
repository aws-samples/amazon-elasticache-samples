#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/elasticache_serverless_cache
resource "aws_elasticache_serverless_cache" "serverless_cache" {
  engine = "valkey"
  name   = var.name
  cache_usage_limits {
    data_storage {
      maximum = 10
      unit    = "GB"
    }
    ecpu_per_second {
      maximum = 5000
    }
  }
  daily_snapshot_time      = "09:00"
  description              = "Valkey cache server for ${var.name}"
  major_engine_version     = "7"
  snapshot_retention_limit = 1
  security_group_ids       = [module.vpc.security_group.id]
  subnet_ids               = module.vpc.private_subnets.*.id
  kms_key_id               = aws_kms_key.custom_kms_key.arn
}