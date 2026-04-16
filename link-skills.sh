#!/bin/bash

# Script to link individual skill folders from skills/ to ~/.claude/skills/
# Each skill folder will be linked individually
# Exit immediately if any command fails
set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${SCRIPT_DIR}/skills"
TARGET_DIR="${HOME}/.claude/skills"

# Step 1: Check if skills directory exists
if [ ! -d "$SKILLS_DIR" ]; then
    echo -e "${RED}Error: Skills directory not found: $SKILLS_DIR${NC}" >&2
    exit 1
fi

# Step 2: Create target directory if it doesn't exist
if [ ! -d "$TARGET_DIR" ]; then
    echo -e "${YELLOW}Creating target directory: $TARGET_DIR${NC}"
    mkdir -p "$TARGET_DIR"
fi

echo -e "${GREEN}Linking BenchRouter skills from $SKILLS_DIR to $TARGET_DIR${NC}"
echo ""

# Step 3: Link all skill folders
for skill_dir in "$SKILLS_DIR"/*; do
    # Check if it's a directory
    if [ ! -d "$skill_dir" ]; then
        continue
    fi

    skill_name=$(basename "$skill_dir")
    target_link="${TARGET_DIR}/${skill_name}"

    # Check if SKILL.md exists in the skill directory
    if [ ! -f "${skill_dir}/SKILL.md" ]; then
        echo -e "${RED}Error: SKILL.md not found in ${skill_name}${NC}" >&2
        exit 1
    fi

    # Check if link already exists
    if [ -L "$target_link" ]; then
        # It's a symlink, check if it points to the correct location
        current_target=$(readlink "$target_link")
        if [ "$current_target" = "$skill_dir" ]; then
            echo -e "${GREEN}✓${NC} ${skill_name} (already linked, skipping)"
            continue
        else
            echo -e "${YELLOW}⚠${NC} ${skill_name} (link exists but points elsewhere, removing and re-linking)"
            rm "$target_link"
        fi
    elif [ -e "$target_link" ]; then
        # It's a file or directory, not a symlink
        echo -e "${RED}Error: Target exists but is not a symlink: $target_link${NC}" >&2
        exit 1
    fi

    # Create new symlink
    ln -s "$skill_dir" "$target_link"
    echo -e "${GREEN}✓${NC} ${skill_name}"
done

echo ""
echo -e "${GREEN}All BenchRouter skills linked successfully!${NC}"
echo -e "Skills are now available in Claude Code."
