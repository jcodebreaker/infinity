# Deployment

The whole Superchat deployment: two images (`engine.Dockerfile`, `host.Dockerfile`), the
Compose file, and the scripts. The walkthrough, env keys and secrets are in
[docs/setup.md](../docs/setup.md). From the repo root:

```sh
./deployment/setup.sh                                          # first run: .env, builds, start
docker compose --env-file .env -f deployment/compose.yaml up -d --wait
docker compose --env-file .env -f deployment/compose.yaml logs -f engine
./deployment/stop.sh                                           # stop, keep data
```
