module "vpc" {
  source              = "../network"
  vpc_name            = var.name
  region              = var.region
  vpc_cidr            = var.vpc_cidr
  subnet_cidr_private = var.subnet_cidr_private
}