# Systemd Service (DEPRECATED)

**This systemd service is deprecated.** The recommended deployment method is now Docker with Traefik ForwardAuth.

See the main [DEPLOYMENT.md](../DEPLOYMENT.md) for the current Docker-based deployment instructions.

## Why Docker instead of systemd?

1. **ForwardAuth** - Traefik ForwardAuth requires the API and Traefik to be on the same Docker network
2. **Simplified deployment** - Single `docker compose up` instead of multiple manual steps
3. **Consistent environment** - No Python version or dependency conflicts
4. **Easy updates** - `docker compose up -d --build` rebuilds and restarts

## If you must use systemd

The `usermanagement.service` file is kept for reference but may not work with the current ForwardAuth setup. You would need to:

1. Run Traefik outside of Docker or configure external network access
2. Configure ForwardAuth to reach the host-based API
3. Handle database path and permissions manually

This is not recommended or supported.
