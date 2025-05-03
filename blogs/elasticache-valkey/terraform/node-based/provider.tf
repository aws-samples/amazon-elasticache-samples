terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.95.0"
    }
        random = {
      source  = "hashicorp/random"
      version = "3.6.3"
    }
  }
}

provider "aws" {
  region     = var.region
  access_key = var.access_key
  secret_key = var.secret_key
  default_tags {
    tags = {
      Source = "https://github.com/aws-samples/amazon-elasticache-samples/tree/main/blogs"
      Type   = "elasticache-valkey-node-based"
    }
  }
}
provider "random" {
  # Configuration options
}