#!/bin/sh

# Validate the project-owned proxy inputs and derive the tunnel client's
# control-plane-specific proxy URL. Never print values from the environment.
proxy_host=${SERVERFS_PROXY_HOST:-}
proxy_port=${SERVERFS_PROXY_PORT:-}
proxy_username=${SERVERFS_PROXY_USERNAME:-}
proxy_password=${SERVERFS_PROXY_PASSWORD:-}

fail() {
  printf '%s\n' "openai-tunnel proxy configuration error: $1" >&2
  exit 2
}

unset CONTROL_PLANE_HTTP_PROXY

if [ -z "$proxy_host" ]; then
  [ -z "$proxy_port" ] || fail 'PORT requires HOST'
  [ -z "$proxy_username" ] || fail 'USERNAME requires HOST'
  [ -z "$proxy_password" ] || fail 'PASSWORD requires HOST'
else
  [ -n "$proxy_port" ] || fail 'HOST requires PORT'
  case "$proxy_host" in
    *[!A-Za-z0-9.-]*|.*|*-|*.) fail 'HOST must be a DNS name or IPv4 address' ;;
  esac
  case "$proxy_host" in
    *..*|-*|*.-*|*-.*) fail 'HOST must be a DNS name or IPv4 address' ;;
  esac
  case "$proxy_port" in
    *[!0-9]*) fail 'PORT must be an integer from 1 to 65535' ;;
  esac

  normalized_port=$proxy_port
  while [ "${normalized_port#0}" != "$normalized_port" ]; do
    normalized_port=${normalized_port#0}
  done
  [ -n "$normalized_port" ] || normalized_port=0
  [ "${#normalized_port}" -le 5 ] || fail 'PORT must be an integer from 1 to 65535'
  [ "$normalized_port" -ge 1 ] 2>/dev/null && [ "$normalized_port" -le 65535 ] 2>/dev/null \
    || fail 'PORT must be an integer from 1 to 65535'

  if [ -z "$proxy_username" ] && [ -n "$proxy_password" ]; then
    fail 'PASSWORD requires USERNAME'
  fi

  if [ -n "$proxy_username" ]; then
    percent_encode() {
      encoded_hex=$(printf '%s' "$1" | od -An -v -tx1 | tr -d ' \n')
      encoded=
      while [ -n "$encoded_hex" ]; do
        byte=${encoded_hex%"${encoded_hex#??}"}
        encoded_hex=${encoded_hex#??}
        encoded="${encoded}%${byte}"
      done
      printf '%s' "$encoded"
    }

    encoded_username=$(percent_encode "$proxy_username")
    encoded_password=$(percent_encode "$proxy_password")
    CONTROL_PLANE_HTTP_PROXY="http://${encoded_username}:${encoded_password}@${proxy_host}:${normalized_port}"
    export CONTROL_PLANE_HTTP_PROXY
  else
    CONTROL_PLANE_HTTP_PROXY="http://${proxy_host}:${normalized_port}"
    export CONTROL_PLANE_HTTP_PROXY
  fi
fi

exec /usr/bin/tunnel-client run
