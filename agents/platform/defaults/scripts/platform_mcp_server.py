#!/usr/bin/env python3
# platform_mcp_server.py - Unified GKE Platform Control Plane MCP Server.
# Exposes secure cross-cluster A2A communication and cluster management as native LLM tools.

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# Initialize the FastMCP server
mcp = FastMCP("GKE Platform Control Plane")

def log(msg: str):
    print(f"[PLATFORM-MCP-SERVER] {msg}", file=sys.stderr)

def get_hermes_home() -> Path:
    """Return the active HERMES_HOME directory."""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

def get_state_file(agent_id: str) -> Path:
    """Return the path to the corresponding agents JSONL state file based on agent type."""
    if agent_id.startswith("operator-"):
        return get_hermes_home() / "operator_agents.jsonl"
    else:
        return get_hermes_home() / "devteam_agents.jsonl"

def resolve_agent_credentials(agent_id: str) -> tuple[str, str]:
    """Retrieve the target agent's stable K8s Service FQDN and secure API key from the state registry."""
    state_file = get_state_file(agent_id)
    endpoint = ""
    api_key = "none"

    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("agent_id") == agent_id:
                        endpoint = entry.get("endpoint", "")
                        api_key = entry.get("api_key", "none")
                        log(f"Resolved credentials for '{agent_id}' from state registry.")
                        break
        except Exception as e:
            log(f"Warning: Failed to read state file '{state_file}': {e}")

    if not endpoint:
        # Fallback to standard GKE Multi-Cluster Services (MCS) FQDN
        endpoint = f"{agent_id}.agent-system.svc.clusterset.local:8642"
        log(f"Info: Using GKE Multi-Cluster Services (MCS) FQDN: {endpoint}")

    return endpoint, api_key

def call_agent_api(endpoint: str, api_key: str, query: str, agent_id: str) -> str:
    """Perform the synchronous HTTP POST call to the target agent's completions API using Bearer Token auth."""
    protocol = "https" if endpoint.startswith("https://") else "http"
    clean_endpoint = endpoint.replace("http://", "").replace("https://", "")
    
    url = f"{protocol}://{clean_endpoint}/v1/chat/completions"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": query}]
    }

    log(f"Sending secure synchronous call to '{agent_id}' at: {url}")
    req = urllib.request.Request(
        url, 
        data=json.dumps(payload).encode("utf-8"), 
        headers=headers,
        method="POST"
    )

    try:
        # 5-minute timeout to accommodate GKE Operator/DevTeam reasoning loops
        with urllib.request.urlopen(req, timeout=300) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            return resp_data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return f"ERROR: Target agent returned HTTP {e.code}: {err_body}"
    except Exception as e:
        return f"ERROR: Network communication failed: {e}"

# =============================================================================
# MCP Tool Declarations
# =============================================================================

@mcp.tool()
def call_agent(target_agent_id: str, query: str) -> str:
    """
    Directly and securely execute a synchronous, token-authorized completions API call
    to a GKE Operator or DevTeam agent across clusters in your GKE fleet.

    Args:
        target_agent_id: The unique ID of the target agent (e.g., 'operator-mercury-03-us-central1').
        query: The question, request, or operational instruction to send to the target agent.
    """
    endpoint, api_key = resolve_agent_credentials(target_agent_id)
    return call_agent_api(endpoint, api_key, query, target_agent_id)

# Note: Provision and de-provision tools will be added here in future updates.

if __name__ == "__main__":
    # Start the FastMCP stdio server
    mcp.run()
