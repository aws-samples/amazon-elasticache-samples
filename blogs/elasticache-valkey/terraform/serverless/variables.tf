#Define AWS Region
variable "region" {
  description = "Infrastructure region"
  type        = string
  default     = "us-east-2"
}
#Define IAM User Access Key
variable "access_key" {
  description = "The access_key that belongs to the IAM user"
  type        = string
  sensitive   = true
  default     = ""
}
#Define IAM User Secret Key
variable "secret_key" {
  description = "The secret_key that belongs to the IAM user"
  type        = string
  sensitive   = true
  default     = ""
}
variable "name" {
  description = "The name of the application."
  type        = string
  default     = "elasticache-valkey-serverless-demo"
}
variable "vpc_cidr" {
  description = "The CIDR of the VPC."
  type        = string
  default     = "12.25.15.0/25"
}
variable "subnet_cidr_private" {
  description = "The CIDR blocks for the public subnets."
  type        = list(any)
  default     = ["12.25.15.0/27", "12.25.15.32/27"]
}
variable "data_storage" {
  description = "The maximum data storage limit in the cache, expressed in Gigabytes."
  type        = number
  default     = 10
}
variable "ecpu_per_second" {
  description = "The configuration for the number of ElastiCache Processing Units (ECPU) the cache can consume per second."
  type        = number
  default     = 5000
}
variable "daily_snapshot_time" {
  description = "The daily time that snapshots will be created from the new serverless cache."
  type        = string
  default     = "09:00"
}
variable "major_engine_version" {
  description = "The version of the cache engine that will be used to create the serverless cache."
  type        = string
  default     = "8"
}
variable "snapshot_retention_limit" {
  description = "The number of snapshots that will be retained for the serverless cache that is being created."
  type        = number
  default     = 1
}