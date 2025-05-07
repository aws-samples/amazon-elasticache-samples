# VPC
output "vpc" {
  description = "The VPC created via this module."
  value       = aws_vpc.this
}
# Private Subnets
output "private_subnets" {
  description = "List of private subnets."
  value       = aws_subnet.private[*]
}
# Security Group id
output "security_group" {
  description = "The ID of the security group."
  value       = aws_security_group.custom_sg
}