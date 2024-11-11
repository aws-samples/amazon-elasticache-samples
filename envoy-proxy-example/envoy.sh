#!/bin/bash
docker run -d --name envoy \
    --rm \
    -v $(pwd)/envoy.yaml:/envoy.yaml \
    -v $(pwd)/log:/var/log \
    -p 9901:9901 -p 6379:6379 \
    envoyproxy/envoy-dev:latest \
    -c /envoy.yaml \
    --log-path /var/log/customer.log

