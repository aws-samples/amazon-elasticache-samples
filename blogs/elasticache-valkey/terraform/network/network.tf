#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/availability_zones
data "aws_availability_zones" "available" {
  state = "available"
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc
resource "aws_vpc" "this" {
  cidr_block = var.vpc_cidr
  tags       = merge(var.tags, { "Name" = var.vpc_name })
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/subnet
resource "aws_subnet" "private" {
  count             = length(var.subnet_cidr_private)
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.subnet_cidr_private[count.index]
  availability_zone = data.aws_availability_zones.available.names[count.index % 3]
  tags              = merge(var.tags, { "Name" = "${var.vpc_name}-private-${count.index + 1}" })
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route_table
resource "aws_route_table" "private" {
  count  = length(var.subnet_cidr_private) > 0 ? length(var.subnet_cidr_private) : 0
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { "Name" = "${var.vpc_name}-private-${count.index + 1}" })
}
#https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route_table_association
resource "aws_route_table_association" "private" {
  count          = length(var.subnet_cidr_private) > 0 ? length(var.subnet_cidr_private) : 0
  subnet_id      = element(aws_subnet.private.*.id, count.index)
  route_table_id = aws_route_table.private[count.index].id
}