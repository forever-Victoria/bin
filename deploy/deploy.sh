#!/usr/bin/env bash
set -Eeuo pipefail

image="${1:?image tag is required}"
deploy_path="${2:?deploy path is required}"
archive="${3:?image archive is required}"
compose_file="$deploy_path/compose.yaml"
env_file="$deploy_path/.env"

if [[ ! -f "$compose_file" ]]; then
  echo "Missing Compose file: $compose_file" >&2
  exit 1
fi

if [[ ! -f "$env_file" ]]; then
  echo "Missing environment file: $env_file" >&2
  exit 1
fi

if [[ ! -f "$archive" ]]; then
  echo "Missing image archive: $archive" >&2
  exit 1
fi

cd "$deploy_path"

previous_image="$(
  sudo -n docker ps \
    --filter label=com.docker.compose.service=bin-gateway \
    --format '{{.Image}}' |
    head -n 1
)"

gzip -dc -- "$archive" | sudo -n docker load

rollback() {
  echo "Deployment failed; rolling back to ${previous_image:-no previous image}" >&2
  sudo -n env BIN_GATEWAY_IMAGE="$image" \
    docker compose -f "$compose_file" logs --tail 100 --no-color || true

  if [[ -n "$previous_image" ]]; then
    sudo -n env BIN_GATEWAY_IMAGE="$previous_image" \
      docker compose -f "$compose_file" up -d --remove-orphans
  else
    sudo -n env BIN_GATEWAY_IMAGE="$image" \
      docker compose -f "$compose_file" down
  fi
}

if ! sudo -n env BIN_GATEWAY_IMAGE="$image" \
  docker compose -f "$compose_file" up -d --remove-orphans; then
  rollback
  exit 1
fi

healthy=false
for _ in {1..30}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8767/healthz >/dev/null; then
    healthy=true
    break
  fi
  sleep 2
done

if [[ "$healthy" != true ]]; then
  rollback
  exit 1
fi

rm -f -- "$archive"
sudo -n env BIN_GATEWAY_IMAGE="$image" \
  docker compose -f "$compose_file" ps
echo "Deployment healthy: $image"
