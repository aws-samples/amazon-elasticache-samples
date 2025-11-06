#!/bin/sh
docker-compose down
docker image rm elasticache-navigator-frontend
docker image rm elasticache-navigator-backend
docker-compose build
docker-compose up
