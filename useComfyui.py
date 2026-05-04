#!/usr/bin/env python3
"""
useComfyui.py - A fastMCP tool that allows LLMs to generate ComfyUI workflows and send them to ComfyUI.

Features:
- Communicates with a local ComfyUI server via HTTP (httpx)
- Requests connection IP and port from user on first use, then caches them
- Checks available nodes and models on the server (/object_info endpoint)
- Generates workflows based on user's natural language request
- Sends workflow to ComfyUI and retrieves results (images)
- User can also request a workflow without sending it to ComfyUI

Usage: Run this script as an MCP server. It will be invoked by the MCP host.
"""

import json
import os
import sys
import base64
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

from fastmcp import FastMCP

# httpx for HTTP requests
import httpx

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------

# Default ComfyUI server address (user will be prompted if not set)
DEFAULT_COMFYUI_HOST = "127.0.0.1"
DEFAULT_COMFYUI_PORT = "8188"

# Cache file for connection settings
_CACHE_FILE = Path(__file__).parent / ".comfyui_cache.json"

# Output directory for generated images
OUTPUT_DIR = Path(__file__).parent / "comfyui_outputs"

# ---------------------------------------------------------------------------
# MCP Server Setup
# ---------------------------------------------------------------------------

mcp = FastMCP("useComfyui")

# ---------------------------------------------------------------------------
# Connection Management
# ---------------------------------------------------------------------------

def _load_cache() -> Dict[str, str]:
    """Load cached connection settings from disk."""
    if _CACHE_FILE.exists():
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"host": DEFAULT_COMFYUI_HOST, "port": DEFAULT_COMFYUI_PORT}


def _save_cache(data: Dict[str, str]) -> None:
    """Persist connection settings to disk."""
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except IOError as e:
        print(f"[useComfyui] Warning: Could not save cache: {e}", file=sys.stderr)


def _get_connection_settings() -> Tuple[str, str]:
    """Return (host, port) from cache or user input."""
    cache = _load_cache()
    host = cache.get("host", DEFAULT_COMFYUI_HOST)
    port = cache.get("port", DEFAULT_COMFYUI_PORT)

    # If either is empty/default, prompt the user once
    if not host or not port:
        host = input("ComfyUI server IP (e.g. 127.0.0.1): ").strip() or DEFAULT_COMFYUI_HOST
        port = input("ComfyUI server port (e.g. 8188): ").strip() or DEFAULT_COMFYUI_PORT
        _save_cache({"host": host, "port": port})

    return str(host), str(port)


def _build_base_url() -> str:
    """Build the base URL for ComfyUI API."""
    host, port = _get_connection_settings()
    return f"http://{host}:{port}"


def _http_client() -> httpx.Client:
    """Return a configured httpx.Client for ComfyUI requests."""
    return httpx.Client(
        base_url=_build_base_url(),
        timeout=120.0,
        follow_redirects=True,
    )

# ---------------------------------------------------------------------------
# Connection Status Check Tool
# ---------------------------------------------------------------------------

@mcp.tool("check_comfyui_status")
async def check_comfyui_status() -> str:
    """Check if the ComfyUI server is running and accessible.

    This tool verifies that the ComfyUI server is reachable at the configured host and port.
    It performs a simple HTTP GET to the root endpoint and returns the status of the server.
    Use this before generating or sending workflows to ensure the server is available.

    Returns:
        Status information about the ComfyUI server including whether it's running,
        the configured address, and any error messages if the server is unreachable.
    """
    host, port = _get_connection_settings()
    base_url = f"http://{host}:{port}"

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get("/")
            if resp.status_code == 200:
                return (f"ComfyUI server is RUNNING and accessible.\n"
                        f"Server Address: {host}:{port}\n"
                        f"Status Code: {resp.status_code}\n"
                        f"The server is ready to accept workflows.")
            else:
                return (f"ComfyUI server is running but returned status code {resp.status_code}.\n"
                        f"Server Address: {host}:{port}")
    except httpx.ConnectTimeout as e:
        return (f"ComfyUI server at {host}:{port} is NOT responding (timeout).\n"
                f"The server may be down or the address/port may be incorrect.\n"
                f"Please verify that ComfyUI is running and the connection settings are correct.")
    except httpx.ConnectError as e:
        return (f"ComfyUI server at {host}:{port} is NOT reachable (connection error).\n"
                f"The server may be down or the address/port may be incorrect.\n"
                f"Please verify that ComfyUI is running and the connection settings are correct.")
    except Exception as e:
        return f"Error checking ComfyUI status at {host}:{port}: {e}"


# ---------------------------------------------------------------------------
# Discovery: Available Nodes & Models
# ---------------------------------------------------------------------------

@mcp.tool("get_available_nodes")
async def get_available_nodes() -> str:
    """Get all available nodes and their inputs/outputs from the ComfyUI server.

    This queries the /object_info endpoint of a running ComfyUI instance to list every node,
    its inputs, outputs, and description. Use this first to discover what's available before generating workflows.
    """
    try:
        with _http_client() as client:
            resp = client.get("/object_info")
            resp.raise_for_status()
            data = resp.json()

        # Extract node names and brief descriptions
        node_list = []
        if isinstance(data, dict) and "nodes" in data:
            nodes_data = data["nodes"]
            for node in nodes_data:
                name = node.get("name", "unknown")
                desc = node.get("description", "")
                inputs = list(node.get("inputs", {}).keys()) if isinstance(node, dict) else []
                outputs = list(node.get("outputs", {}).keys()) if isinstance(node, dict) else []
                node_list.append(f"- **{name}**\n  Description: {desc}\n  Inputs: {', '.join(inputs) or 'none'}\n  Outputs: {', '.join(outputs) or 'none'}")

        return "\n\n".join(node_list if node_list else ["No nodes found on the server."])
    except httpx.ConnectError as e:
        return f"Could not connect to ComfyUI at {_build_base_url()}: {e}"
    except Exception as e:
        return f"Error fetching nodes: {e}"


# ---------------------------------------------------------------------------
# Workflow Generation
# ---------------------------------------------------------------------------

@mcp.tool("generate_workflow")
async def generate_workflow(
    request: str,
    send_to_comfyui: bool = False,
) -> str:
    """Generate a ComfyUI workflow based on the user's natural language request.

    This creates a minimal text-to-image pipeline (KSampler + CLIP Text Encode + SaveImage).
    The result is a JSON object ready to send to ComfyUI's /prompt endpoint.

    Args:
        request: Natural language description of what image you want (e.g., "a cat sitting on a windowsill")
        send_to_comfyui: If True, the workflow is sent immediately and results are returned.
                        If False (default), only the generated JSON is returned for inspection.

    Returns:
        If send_to_comfyui=True: The result from ComfyUI (prompt ID or image info).
        If send_to_comfyui=False: The workflow as a pretty-printed JSON string.
    """
    try:
        with _http_client() as client:
            # Fetch object_info to know what nodes are available
            info_resp = client.get("/object_info")
            info_resp.raise_for_status()
            object_info = info_resp.json()

        # Build a simple prompt template based on the request
        workflow = _build_workflow(request, object_info)

        if send_to_comfyui:
            return _send_and_wait(workflow)
        else:
            return json.dumps(workflow, indent=2)

    except httpx.ConnectError as e:
        return f"Could not connect to ComfyUI at {_build_base_url()}: {e}"
    except Exception as e:
        return f"Error generating workflow: {e}"


@mcp.tool("send_workflow")
async def send_workflow(
    workflow_json: str,
) -> str:
    """Send a complete ComfyUI workflow JSON to the running ComfyUI server.

    The workflow must be a valid ComfyUI graph (the format expected by /prompt).
    This is useful when you already have a workflow from another tool or generated it externally.

    Args:
        workflow_json: A JSON string representing a complete ComfyUI workflow graph.
                       Must be the raw workflow dict, NOT wrapped in {"workflow": ...}.

    Returns:
        The result from ComfyUI (prompt ID, status, and any output images).
    """
    try:
        workflow = json.loads(workflow_json) if isinstance(workflow_json, str) else workflow_json
        return _send_and_wait(workflow)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    except Exception as e:
        return f"Error sending workflow: {e}"


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

def _build_workflow(request: str, object_info: dict) -> Dict[str, Any]:
    """Build a minimal ComfyUI workflow from the user's request.

    This is a simplified generator that creates a proper ComfyUI graph format.
    The workflow follows the standard ComfyUI node-based structure with proper connections.
    """
    # Extract positive prompt from request (simple heuristic)
    pos_prompt = request.strip() or "a beautiful landscape"
    neg_prompt = ""

    if "not" in pos_prompt.lower():
        parts = pos_prompt.split(" not ", 1)
        pos_prompt, neg_prompt = parts[0], parts[1]

    # Build the workflow graph using ComfyUI's expected format.
    # Each node needs: class_type (matching a registered node name) and inputs (dict of parameters).
    # Connections use {"input": <value>} for primitives or {"node": <id>, "field": <name>},
    # where <id> is the numeric ID assigned to each node in the graph.

    workflow_graph = {
        "4": {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(datetime.now().timestamp()) % 10**8,
                "steps": 20,
                "cfg_scale": 7.5,
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
            },
        },
    }

    # Add a text prompt node (CLIP Text Encode)
    workflow_graph["5"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": pos_prompt},
    }
    workflow_graph["6"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": neg_prompt} if neg_prompt else {"text": ""},
    }

    # Wire up the graph (simplified)
    workflow_graph["4"]["inputs"]["positive_prompt"] = {"node": "5", "field": "text"}
    workflow_graph["4"]["inputs"]["negative_prompt"] = {"node": "6", "field": "text"}

    # Add a latent output and image save node
    workflow_graph["7"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 512, "height": 512},
    }
    workflow_graph["8"] = {
        "class_type": "SaveImage",
        "inputs": {},
    }

    # Wire KSampler -> SaveImage
    workflow_graph["4"]["inputs"]["latent"] = {"node": "7", "field": "samples"}
    workflow_graph["8"]["inputs"]["image"] = {"node": "4", "field": "output"}

    return {
        "graph": workflow_graph,
        "request": request,
        "timestamp": datetime.now().isoformat(),
    }


def _send_and_wait(workflow: Dict[str, Any], timeout: int = 300) -> str:
    """Send a workflow to ComfyUI and wait for the result."""
    try:
        with _http_client() as client:
            # Submit workflow (ComfyUI expects the workflow dict directly, NOT wrapped in a key)
            resp = client.post("/prompt", json=workflow)
            resp.raise_for_status()
            data = resp.json()
            prompt_id = data.get("prompt_id") or (data.get("extra, {}).get("queue_prompt", [{}])[-1].get("inputs", {}).get("_data", {}).get("prompt_id", ""))

            if not prompt_id:
                return f"Could not get prompt ID from server response: {json.dumps(data, indent=2)}"

            # Poll /history for completion
            start = datetime.now()
            while (datetime.now() - start).total_seconds() < timeout:
                hist_resp = client.get("/history")
                hist_resp.raise_for_status()
                history = hist_resp.json()

                if prompt_id in history and "outputs" in history[prompt_id]:
                    outputs = history[prompt_id]["outputs"]
                    # Find image outputs (SaveImage node typically named '8' or similar)
                    images = []
                    for node_id, node_data in outputs.items():
                        if "images" in node_data:
                            for img in node_data["images"]:
                                filename = img.get("filename", "")
                                subfolder = img.get("subfolder", "")"
                    return f"Prompt submitted. Prompt ID: {prompt_id}. Image saved as '{filename}' (subfolder: {subfolder}). Check ComfyUI output folder for results."

            return "Timed out waiting for ComfyUI to finish the workflow."

    except httpx.ConnectError as e:
        return f"Could not connect to ComfyUI at {_build_base_url()}: {e}"
    except Exception as e:
        return f"Error sending/waiting for workflow: {e}"


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if True:  # Always runs for MCP server mode (matches SwordMemory.py pattern)
    print("useComfyui.py - ComfyUI fastMCP tool")
    print("=" * 50)
    print("This script is meant to be run as an MCP server.")
    print("It exposes tools for generating and sending ComfyUI workflows.")
    print()
    print("Available tools:")
    print("  - check_comfyui_status: Check if the ComfyUI server is running")
    print("  - get_available_nodes: Get all available nodes from ComfyUI")
    print("  - generate_workflow: Generate a workflow based on natural language request")
    print("  - send_workflow: Send an existing workflow JSON to the server")
    mcp.run()