docker stop aerospike
docker rm aerospike
docker volume prune  # optional but will wipe all unused volumes
docker run -d --name aerospike -p 3000:3000 -p 3001:3001 -p 3002:3002 aerospike/aerospike-server
