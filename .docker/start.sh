#!/bin/bash

DIR_DB="$1"
DIR_DATA="$2"
DIR_MODULE_LIB="$3"
DIR_SSH="$4"

[ -z "$DIR_DB" -o ! -d "$DIR_DB" ] && { echo "ERR: Given DB path is invalid"; exit 1; }
[ -z "$DIR_DATA" -o ! -d "$DIR_DATA" ] && { echo "ERR: Given data path is invalid"; exit 1; }
[ -z "$DIR_SSH" -o ! -d "$DIR_SSH" ] && { echo "ERR: Given SSH path is invalid"; exit 1; }

echo "Selected options are"
echo "- DB: $DIR_DB"
echo "- DATA: $DIR_DATA"

docker stop gymbk-prg &>/dev/null
docker rm gymbk-prg &>/dev/null
docker run \
  -p 3032:3032 \
  -v "$DIR_DB:/var/ksi-be.ro/" \
  -v "$DIR_DATA:/var/ksi-data.ro/" \
  -v "$DIR_SSH:/home/ksi/.ssh/" \
  --device /dev/fuse \
  --cap-add=SYS_ADMIN \
  --privileged=true \
  --security-opt apparmor:unconfined \
  -it \
  --name gymbk-prg \
  gymbk-prg

