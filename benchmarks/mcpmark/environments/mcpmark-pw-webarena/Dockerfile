FROM evalsysorg/mcpmark:latest

# Add Docker CE for DinD
# Remove broken nodesource repo (expired SHA1 key), then install Docker CE
RUN rm -f /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && ARCH=$(dpkg --print-architecture) \
    && CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME") \
    && echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${CODENAME} stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io \
    && rm -rf /var/lib/apt/lists/*

# Pre-install @playwright/mcp and matching Chromium so the browser revision
# aligns with the version the MCPMark agent uses at runtime.
RUN npm install -g @playwright/mcp@latest \
    && npx playwright install chromium
