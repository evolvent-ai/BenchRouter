"""
Patch run_single_containerized.sh: add "local_servers" to FILES_TO_COPY so that
yahoo-finance-mcp and arxiv-latex-mcp are available inside inner containers.

The copy loop in run_single_containerized.sh reads files from the outer container's
$PROJECT_ROOT (/opt/toolathlon) and copies them into the inner container via
  $CONTAINER_RUNTIME cp $PROJECT_ROOT/$item $CONTAINER_NAME:/workspace/$item

local_servers is a directory, so dirname("local_servers") == "." and no parent
mkdir is needed.  The venv python fix in yahoo-finance.yaml + 120s timeout ensure
the 157 MB docker cp fits comfortably within the MCP handshake budget.
"""
import sys
from pathlib import Path

scripts_dir = Path(sys.argv[1])
target = scripts_dir / "run_single_containerized.sh"
text = target.read_text()

# Append "local_servers" as the last entry in FILES_TO_COPY, just before the
# closing paren.  Use a broad anchor to ensure uniqueness.
OLD = '    "utils"\n    "main.py"\n)'
NEW = '    "utils"\n    "main.py"\n    "local_servers"\n)'

if OLD not in text:
    print(f"Anchor not found in {target}; skipping", file=sys.stderr)
    sys.exit(0)

patched = text.replace(OLD, NEW, 1)
target.write_text(patched)
print(f'Patched {target}: added "local_servers" to FILES_TO_COPY')
