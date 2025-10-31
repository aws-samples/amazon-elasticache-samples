#!/bin/sh
docker-compose down
docker image rm valkey-navigator-frontend
docker image rm valkey-navigator-backend
docker-compose build
docker-compose up
