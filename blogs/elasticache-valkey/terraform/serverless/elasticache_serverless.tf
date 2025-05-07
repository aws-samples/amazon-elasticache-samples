#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/elasticache_serverless_cache
resource "aws_elasticache_serverless_cache" "serverless_cache" {
  engine = "valkey"
  name   = var.name
  cache_usage_limits {
    data_storage {
      maximum = var.data_storage
      unit    = "GB"
    }
    ecpu_per_second {
      maximum = var.ecpu_per_second
    }
  }
  daily_snapshot_time      = var.daily_snapshot_time
  description              = "ElastiCache cluster for ${var.name}"
  major_engine_version     = var.major_engine_version
  snapshot_retention_limit = var.snapshot_retention_limit
  security_group_ids       = [module.vpc.security_group.id]
  subnet_ids               = module.vpc.private_subnets.*.id
  kms_key_id               = aws_kms_key.encrypt_cache.arn
}