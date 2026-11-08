# arena.ai

## NEXUS Memory Map

A standalone, local-first interactive knowledge graph is available in `memory_map.py`. It uses only the Python standard library; no install or API key is required.

```bash
python memory_map.py
```

The app opens at `http://127.0.0.1:8765`. Notes and graph connections are autosaved to the ignored local file `memory_map_data.json`. Use **Export** for a portable JSON backup. For a hosted/sandbox preview, bind to all interfaces:

```bash
python memory_map.py --host 0.0.0.0 --port 8001 --no-browser
```