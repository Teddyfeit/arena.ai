# FIELDKIT

FIELDKIT brings the **Roblox Avatar Finder** and **Discord Name Radar** together behind one start page and one local Python server.

```bash
python tool_hub.py
```

It opens at `http://127.0.0.1:8765`. Choose either tool from the start page, then use **Home** / **Tool Desk** to switch back. Python's standard library is all that is required.

For an Arena/sandbox preview:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

Discord Name Radar is a manual username watchlist with settings and reminders. It does not query Discord, automate username changes, or store Discord credentials/tokens.
