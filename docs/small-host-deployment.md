# tgForwarder Small Host Deployment

Target host: Windows 10 small host.

Default small-host paths:

- Repo: `E:\services\tgForwarder`
- Deploy logs: `E:\logs\tgForwarder-deploy`
- Runtime exports copied to the small host: `E:\exports\tgForwarder-runtime`

## First Migration

1. Stop the local tgForwarder stack before moving the Telegram session:

```powershell
cd D:\Users\ThinkDeep\Documents\Code\tgForwarder
& "C:\Program Files\Docker\Docker\resources\bin\docker.exe" compose down
```

2. Export runtime state on the main PC. By default this writes under the repo's ignored `runtime_exports` directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\export_tgforwarder_runtime_state.ps1 -StopLocalCompose
```

3. Copy the generated export to the small host under `E:\exports\tgForwarder-runtime`.

4. On the small host, clone or copy the repo to:

```text
E:\services\tgForwarder
```

5. Restore state and start Docker on the small host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:\services\tgForwarder\scripts\small_host_deploy.ps1 -RuntimeStatePath E:\exports\tgForwarder-runtime\<export-folder-or-zip>
```

Only one machine should run the Telegram session at a time.

## Routine Deploy

After changing code on the main PC, push the change to Git. On the small host, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:\services\tgForwarder\scripts\small_host_deploy.ps1
```

The deploy script runs `git pull --ff-only` and then `docker compose up -d --build`.

## Optional Scheduled Deploy

On the small host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File E:\services\tgForwarder\scripts\install_small_host_deploy_task.ps1
```

This registers a scheduled task that checks for updates and redeploys every 30 minutes.
