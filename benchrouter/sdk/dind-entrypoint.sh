#!/bin/bash
set -e

# Start Docker daemon — try overlay2 first, fallback to vfs
DOCKER_STARTED=false
for driver in overlay2 vfs; do
    echo "Trying storage driver: $driver"
    dockerd --storage-driver="$driver" \
            --host=unix:///var/run/docker.sock \
            &>/var/log/dockerd.log &
    DOCKERD_PID=$!

    # Wait for daemon ready (max 30s per attempt)
    for i in $(seq 1 30); do
        if docker info &>/dev/null; then
            echo "Docker daemon started with driver: $driver"
            DOCKER_STARTED=true
            break 2
        fi
        # Check if dockerd process died
        if ! kill -0 "$DOCKERD_PID" 2>/dev/null; then
            echo "dockerd exited with driver $driver, trying next..."
            break
        fi
        sleep 1
    done

    # If still running but not responding, kill it
    kill "$DOCKERD_PID" 2>/dev/null || true
    wait "$DOCKERD_PID" 2>/dev/null || true
done

if [ "$DOCKER_STARTED" != "true" ]; then
    echo "dockerd failed to start with any storage driver"
    cat /var/log/dockerd.log
    exit 1
fi

# Load pre-cached images (only those declared in docker_images, or all if unset)
if [ -d "/var/lib/docker-cache" ]; then
    if [ -n "$BENCHROUTER_DIND_IMAGES" ]; then
        # Selective load: only images listed in BENCHROUTER_DIND_IMAGES
        IFS=',' read -ra WANT <<< "$BENCHROUTER_DIND_IMAGES"
        for tar in /var/lib/docker-cache/*.tar; do
            [ -f "$tar" ] || continue
            base=$(basename "$tar" .tar)
            match=false
            for img in "${WANT[@]}"; do
                # Match by image name: convert "repo/name:tag" to cache filename
                # Cache files use underscores for slashes and colons
                normalized=$(echo "$img" | tr '/:' '_')
                if [ "$base" = "$normalized" ] || [ "$base" = "$img" ]; then
                    match=true
                    break
                fi
            done
            if [ "$match" = "true" ]; then
                echo "Loading cached image: $tar ($(du -sh "$tar" 2>/dev/null | cut -f1))"
                if ! docker load -i "$tar"; then
                    echo "WARNING: Failed to load $tar"
                fi
            else
                echo "Skipping cached image (not needed): $tar"
            fi
        done
    else
        # Fallback: load everything (backward compatible)
        for tar in /var/lib/docker-cache/*.tar; do
            if [ -f "$tar" ]; then
                echo "Loading cached image: $tar"
                docker load -i "$tar" || echo "WARNING: Failed to load $tar"
            fi
        done
    fi
fi

# Run evaluation
exec python -m benchrouter.sdk.runner
