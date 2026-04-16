"""
Patch configs/mcp_servers/yahoo-finance.yaml:

Before:
  params:
    command: uv
    args:
      - "run"
      - "${local_servers_paths}/yahoo-finance-mcp/server.py"
  client_session_timeout_seconds: 60

After:
  params:
    command: "${local_servers_paths}/yahoo-finance-mcp/.venv/bin/python"
    args:
      - "${local_servers_paths}/yahoo-finance-mcp/server.py"
  client_session_timeout_seconds: 120

Rationale: "uv run" takes 5-30s inside a copied venv, which causes the
60s MCP handshake timeout. Using the venv Python directly starts in <1s.
"""
import sys
from pathlib import Path

target = Path(sys.argv[1])
text = target.read_text()

OLD = (
    'params:\n'
    '  command: uv\n'
    '  args:\n'
    '    - "run"\n'
    '    - "${local_servers_paths}/yahoo-finance-mcp/server.py"\n'
    '  # cwd: "${agent_workspace}" # do not add this for compatibility\n'
    'client_session_timeout_seconds: 60'
)
NEW = (
    'params:\n'
    '  command: "${local_servers_paths}/yahoo-finance-mcp/.venv/bin/python"\n'
    '  args:\n'
    '    - "${local_servers_paths}/yahoo-finance-mcp/server.py"\n'
    '  # cwd: "${agent_workspace}" # do not add this for compatibility\n'
    'client_session_timeout_seconds: 120'
)

if OLD not in text:
    print(f"Pattern not found in {target}; skipping", file=sys.stderr)
    print("Current content:", repr(text), file=sys.stderr)
    sys.exit(0)

result = text.replace(OLD, NEW, 1)
target.write_text(result)
print(f"Patched {target}")
print(result)
