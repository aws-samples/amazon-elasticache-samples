#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/elasticache_subnet_group
resource "aws_elasticache_subnet_group" "elasticache_subnet" {
  name       = "${var.name}-cache-subnet"
  subnet_ids = [for subnet in module.vpc.private_subnets : subnet.id]
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/elasticache_replication_group
resource "aws_elasticache_replication_group" "node_based" {
  automatic_failover_enabled = true
  subnet_group_name          = aws_elasticache_subnet_group.elasticache_subnet.name
  replication_group_id       = var.name
  description                = "ElastiCache cluster for ${var.name}"
  node_type                  = var.node_type
  engine                     = "valkey"
  engine_version             = var.engine_version
  parameter_group_name       = var.parameter_group_name
  port                       = var.port
  multi_az_enabled           = true
  num_node_groups            = var.num_node_groups
  replicas_per_node_group    = var.replicas_per_node_group
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.encrypt_cache.id
  transit_encryption_enabled = true
  auth_token                 = aws_secretsmanager_secret_version.auth.secret_string
  security_group_ids         = [module.vpc.security_group.id]
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.slow_log.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.engine_log.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "engine-log"
  }
  lifecycle {
    ignore_changes = [kms_key_id]
  }
  apply_immediately = true
}