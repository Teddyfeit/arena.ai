# arena.ai

## FIELDKIT Tool Desk

Run the tool selector and both workspaces with Python's standard library only:

```bash
python tool_hub.py
```

It opens at `http://127.0.0.1:8765`. Choose **NEXUS Memory Map** or **Discord Name Radar** from the start page. Each tool has its own local JSON save file; export/backup controls are included.

For an Arena/sandbox preview:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

The Discord tool is a manual watchlist and reminder dashboard. It does not connect to Discord, check name availability, automate account changes, or store Discord credentials/tokens.

## NEXUS Memory Map (standalone)

The graph can also be launched on its own:

```bash
python memory_map.py
```
