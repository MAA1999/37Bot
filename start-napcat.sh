#!/bin/bash
# NapCat Docker 启动脚本

docker run -d \
  -e NAPCAT_GID=$(id -g) \
  -e NAPCAT_UID=$(id -u) \
  -p 3000:3000 \
  -p 3001:3001 \
  -p 6099:6099 \
  -v /home/37Bot/napcat/QQ:/app/.config/QQ \
  -v /home/37Bot/napcat/config:/app/napcat/config \
  -v /home/37Bot/data:/home/37Bot/data \
  --name napcat \
  --restart=always \
  mlikiowa/napcat-docker:latest
