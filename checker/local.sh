#!/bin/sh

export ENOCHECKER_SERVICE_ADDRESS=${ENOCHECKER_SERVICE_ADDRESS:-$(docker inspect telepwn | jq -r '.[].NetworkSettings.Networks[].Gateway' | head -1)}
export ENOCHECKER_SERVICE_PORT=9400
export ENOCHECKER_CHECKER_ADDRESS=$(docker network inspect telepwn-checker | jq -r '.[].IPAM.Config[].Gateway')
export ENOCHECKER_CHECKER_PORT=8000

enochecker_test --flag-regex 'ECSC\{[A-Za-z0-9]{32}\}' "$@"
