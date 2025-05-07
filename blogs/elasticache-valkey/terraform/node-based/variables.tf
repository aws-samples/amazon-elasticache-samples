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
  default     = "elasticache-valkey-node-based-demo"
}
variable "vpc_cidr" {
  description = "The CIDR of the VPC."
  type        = string
  default     = "12.25.25.0/25"
}
variable "subnet_cidr_private" {
  description = "The CIDR blocks for the public subnets."
  type        = list(any)
  default     = ["12.25.25.0/27", "12.25.25.32/27"]
}
variable "node_type" {
  description = "Instance class to be used."
  type        = string
  default     = "cache.t2.small"
}
variable "engine_version" {
  description = "Version number of the cache engine to be used for the cache clusters in this replication group."
  type        = string
  default     = "8.0"
}
variable "port" {
  description = "Port number on which each of the cache nodes will accept connections."
  type        = number
  default     = 6379
}
variable "num_node_groups" {
  description = "Number of node groups (shards) for the replication group. Changing this number will trigger a resizing operation before other settings modifications"
  type        = number
  default     = 3
}
variable "replicas_per_node_group" {
  description = "Number of replica nodes in each node group."
  type        = number
  default     = 2
}
variable "parameter_group_name" {
  description = "Name of the parameter group to associate with this replication group."
  type        = string
  default     = "default.valkey8.cluster.on"
}