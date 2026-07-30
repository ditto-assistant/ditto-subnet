#!/bin/sh
set -eu

if docker info >/dev/null 2>&1; then
  status='200 OK'
  body='ok'
else
  status='503 Service Unavailable'
  body='unavailable'
fi
printf 'HTTP/1.1 %s\r\nContent-Type: text/plain\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s' \
  "$status" "${#body}" "$body"
