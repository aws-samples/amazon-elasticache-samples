aws elasticache \
  --no-verify-ssl \
  --region us-east-1 \
  create-replication-group \
  --replication-group-id c7gn-cme-3 \
  --replication-group-description "Personal ElastiCache Dev Cluster Replication Group" \
  --num-node-groups 3 \
  --replicas-per-node-group 1 \
  --cache-node-type cache.m7g.xlarge \
  --engine redis \
  --engine-version 7.0 \
  --cache-subnet-group-name elasticache-c7gn \
  --cache-parameter-group-name default.redis7.cluster.on
