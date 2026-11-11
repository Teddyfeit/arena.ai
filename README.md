# arena.ai

## FIELDKIT Tool Desk

Run the unified tool selector with Python's standard library:

```bash
python tool_hub.py
```

The start page opens at `http://127.0.0.1:8765` and offers three workspaces in one site: **Roblox Avatar Finder**, **NEXUS Memory Map**, and **Discord Name Radar**. Use the Home/Tool Desk link in each workspace to switch tools. Notes and watchlist settings are stored in local JSON files; export/backup controls are included.

For an Arena/sandbox preview:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

Discord Name Radar is a manual watchlist and reminder dashboard. It does not query Discord, automate username changes, or store Discord credentials/tokens.

The memory graph can also be launched on its own with `python memory_map.py`.
