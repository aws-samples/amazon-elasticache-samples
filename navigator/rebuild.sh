#!/bin/sh
docker-compose down
docker image rm elasticache-navigator-frontend
docker image rm elasticache-navigator-backend

# Repo Fix as folders 'lib' are in .gitignore
cp -r frontend/src/src_lib frontend/src/lib

docker-compose build
docker-compose up
