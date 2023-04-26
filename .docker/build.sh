#!/bin/bash
cd "$(realpath "$(dirname "$0")")/.." &&
# docker rmi -f ksi-be &>/dev/null &&
docker build -f .docker/Dockerfile -t gymbk-prg . &&
if [[ "$1" == "--run" ]]; then
  ./.docker/start.sh "$2" "$3" "$4"
fi
