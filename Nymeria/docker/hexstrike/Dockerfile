FROM kalilinux/kali-rolling

ARG HEXSTRIKE_REF=9b8c780f324ce5145a322bfa23c98886f8424ba3

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    HEXSTRIKE_HOST=0.0.0.0 \
    HEXSTRIKE_PORT=8888 \
    HEXSTRIKE_ALLOWED_TOOLS_FILE=/opt/hexstrike-ai/allowed-tools.txt

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      nodejs \
      npm \
      python3 \
      python3-aiohttp \
      python3-bs4 \
      python3-flask \
      python3-pip \
      python3-psutil \
      python3-requests \
      python3-selenium \
      python3-venv \
      mitmproxy; \
    for pkg in \
      amass \
      arjun \
      dirb \
      dirsearch \
      dnsenum \
      ffuf \
      fierce \
      getallurls \
      gobuster \
      hakrawler \
      httpx-toolkit \
      nikto \
      nmap \
      nuclei \
      paramspider \
      sqlmap \
      subfinder \
      wafw00f \
    ; do \
      apt-get install -y --no-install-recommends "$pkg" || echo "WARN: skipped unavailable Kali package: $pkg"; \
    done; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    python3 -m pip install --no-cache-dir --break-system-packages \
      mcp==1.27.0 \
      webdriver-manager; \
    rm -f /usr/local/bin/httpx; \
    ln -sf /usr/bin/httpx-toolkit /usr/local/bin/httpx; \
    ln -sf /usr/bin/getallurls /usr/local/bin/gau; \
    npm install -g supergateway@3.4.3

RUN set -eux; \
    git clone https://github.com/0x4m4/hexstrike-ai.git /opt/hexstrike-ai; \
    cd /opt/hexstrike-ai; \
    git checkout "$HEXSTRIKE_REF"

COPY docker/hexstrike/allowed-tools.txt /opt/hexstrike-ai/allowed-tools.txt
COPY docker/hexstrike/hexstrike_mcp_limited.py /opt/hexstrike-ai/hexstrike_mcp_limited.py

RUN set -eux; \
    chmod +x /opt/hexstrike-ai/hexstrike_mcp_limited.py; \
    groupadd -r -g 999 hexstrike; \
    useradd -r -g hexstrike -u 999 -m -s /bin/bash hexstrike; \
    chown -R hexstrike:hexstrike /opt/hexstrike-ai /home/hexstrike

WORKDIR /opt/hexstrike-ai

USER hexstrike

EXPOSE 8888 8889
