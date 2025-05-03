#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/default_security_group
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { "Name" = "${var.vpc_name}-default" })
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group
resource "aws_security_group" "custom_sg" {
  name        = "${var.vpc_name}_allow_inbound_access"
  description = "manage traffic for ${var.vpc_name}"
  vpc_id      = aws_vpc.this.id
  tags = {
    "Name" = "${var.vpc_name}-sg"
  }
  #checkov:skip=CKV2_AWS_5: "Ensure that Security Groups are attached to another resource"
  #This security group is attached to the Amazon ElastiCache Serverless resource
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group_rule
resource "aws_security_group_rule" "ingress_custom_sg" {
  description       = "allow traffic to the cache cluster"
  type              = "ingress"
  from_port         = 6379
  to_port           = 6379
  protocol          = "tcp"
  cidr_blocks       = [var.vpc_cidr]
  security_group_id = aws_security_group.custom_sg.id
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group_rule
resource "aws_security_group_rule" "egress_custom_sg" {
  description       = "allow traffic to reach outside the vpc"
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = [var.vpc_cidr]
  security_group_id = aws_security_group.custom_sg.id
}