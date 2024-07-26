output "redshift_spectrum_arn" {
  value = aws_iam_role.s3_spectrum_role.arn
}

output "credit_history_table" {
  value = aws_glue_catalog_table.credit_history_table.name
}

output "zipcode_features_table" {
  value = aws_glue_catalog_table.zipcode_features_table.name
}

output "redshift_cluster_identifier" {
  value = aws_redshift_cluster.feast_redshift_cluster.cluster_identifier
}