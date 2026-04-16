"""
Patch utils/api_model/model_provider.py to sanitize MCP tool schemas that have
type=null (Python None). Some MCP servers (e.g. playwright_with_chunk) emit
type=null for no-parameter tools, which strict OpenAI-compatible APIs reject
with: "Invalid schema for function '...': schema must be a JSON Schema of
'type: "object"', got 'type: "None"'."
"""
import sys
from pathlib import Path

utils_dir = Path(sys.argv[1])
target = utils_dir / "api_model" / "model_provider.py"
text = target.read_text()

OLD = (
    "        converted_tools = [ConverterWithExplicitReasoningContent.tool_to_openai(tool) "
    "for tool in tools] if tools else []\n"
)
NEW = (
    "        converted_tools = [ConverterWithExplicitReasoningContent.tool_to_openai(tool) "
    "for tool in tools] if tools else []\n"
    "\n"
    "        # Sanitize tool schemas: some MCP servers emit type=null for\n"
    "        # no-parameter tools; strict APIs (gpt-5.x) reject these.\n"
    "        def _fix_null_type(obj):\n"
    "            if isinstance(obj, dict):\n"
    "                if obj.get('type') is None and 'type' in obj:\n"
    "                    obj['type'] = 'object'\n"
    "                    obj.setdefault('properties', {})\n"
    "                for v in list(obj.values()):\n"
    "                    _fix_null_type(v)\n"
    "            elif isinstance(obj, list):\n"
    "                for item in obj:\n"
    "                    _fix_null_type(item)\n"
    "        for _t in converted_tools:\n"
    "            _fix_null_type(_t)\n"
    "\n"
)

if OLD not in text:
    print(f"Pattern not found in {target}, skipping", file=sys.stderr)
    sys.exit(0)

target.write_text(text.replace(OLD, NEW, 1))
print(f"Patched {target}: added _fix_null_type sanitizer for tool schemas")
