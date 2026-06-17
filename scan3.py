#!/usr/bin/env python3

import asyncio
import json
import sys
import time
import traceback
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from tqdm import tqdm

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

__version__ = "1.0.0"

DEFAULT_TIMEOUT = 15
DEFAULT_SSE_READ_TIMEOUT = 30
DEFAULT_INPUT_FILE = "fofamcp_20260322.json"
DEFAULT_OUTPUT_FILE = "mcp_scan_results2.json"

PATH_TRANSPORTS = {
    "/sse": "sse",
    "/mcp": "streamable_http",
}


def model_to_dict(model: Any) -> Any:
    if model is None:
        return None
    return json.loads(model.model_dump_json(indent=2))


def parse_auth_info(headers: Dict[str, str], status_code: int) -> Dict[str, Any]:
    auth_info = {
        "auth_type": None,
        "auth_resource_metadata": None,
        "auth_scope": None,
        "auth_raw_header": None,
        "auth_status_code": status_code,
    }
    www_auth = headers.get("WWW-Authenticate") or headers.get("www-authenticate")
    if www_auth:
        auth_info["auth_raw_header"] = www_auth
        if www_auth.startswith("Bearer "):
            auth_info["auth_type"] = "Bearer"
        elif www_auth.startswith("Basic "):
            auth_info["auth_type"] = "Basic"
        # 可以添加更多类型
    return auth_info


def exception_to_dict(exc: BaseException) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }

    response = getattr(exc, "response", None)
    if response is not None:
        payload["status_code"] = getattr(response, "status_code", None)
        payload["headers"] = dict(getattr(response, "headers", {}) or {})

    return payload


def build_headers(auth_header: Optional[str] = None) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if auth_header:
        headers["Authorization"] = auth_header
    return headers


def join_path(prefix: str, suffix: str) -> str:
    if not prefix or prefix == "/":
        return suffix
    return f"{prefix.rstrip('/')}{suffix}"


def replace_path_suffix(path: str, suffix: str) -> str:
    normalized = path.rstrip("/")
    for known_suffix in PATH_TRANSPORTS:
        if normalized == known_suffix:
            return suffix
        if normalized.endswith(known_suffix):
            return f"{normalized[:-len(known_suffix)]}{suffix}" or suffix
    return join_path(normalized, suffix)


def resolve_transport_for_path(path: str) -> Optional[str]:
    normalized = path.rstrip("/")
    for suffix, transport in PATH_TRANSPORTS.items():
        if normalized == suffix or normalized.endswith(suffix):
            return transport
    return None


def build_candidate_targets(raw_target: str) -> List[Dict[str, str]]:
    target = raw_target.strip()
    if not target:
        return []

    if not target.startswith(("http://", "https://")):
        target = f"http://{target}"

    parsed = urlparse(target)
    path = parsed.path.rstrip("/")
    candidate_urls: List[str] = []

    for suffix in PATH_TRANSPORTS:
        candidate_path = replace_path_suffix(path, suffix)
        candidate_url = urlunparse(
            parsed._replace(path=candidate_path, params="", query="", fragment="")
        )
        if candidate_url not in candidate_urls:
            candidate_urls.append(candidate_url)

    targets: List[Dict[str, str]] = []
    for candidate_url in candidate_urls:
        candidate_path = urlparse(candidate_url).path.rstrip("/") or "/"
        transport = resolve_transport_for_path(candidate_path)
        if transport is None:
            continue
        targets.append(
            {
                "path": candidate_path,
                "transport": transport,
                "url": candidate_url,
            }
        )

    return targets


def extract_tool_names(tools_payload: Any) -> List[str]:
    if not isinstance(tools_payload, dict):
        return []

    tools = tools_payload.get("tools")
    if not isinstance(tools, list):
        return []

    names = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name"):
            names.append(tool["name"])
    return names


def extract_capability_names(init_payload: Any) -> List[str]:
    if not isinstance(init_payload, dict):
        return []

    result = init_payload.get("result")
    if not isinstance(result, dict):
        return []

    capabilities = result.get("capabilities")
    if not isinstance(capabilities, dict):
        return []

    return sorted(capabilities.keys())


class MCPClient:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout

    async def try_sse_connection(
        self,
        target_url: str,
        auth_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = build_headers(auth_header)
        response: Dict[str, Any] = {
            "url": target_url,
            "path": "/sse",
            "transport": "sse",
            "success": False,
            "session_id": None,
            "init": None,
            "tools_response": None,
            "tool_names": [],
            "capability_names": [],
            "error": None,
        }

        try:
            async with sse_client(
                target_url,
                headers=headers,
                timeout=self.timeout,
                sse_read_timeout=DEFAULT_SSE_READ_TIMEOUT,
            ) as client_streams:
                read_stream, write_stream = client_streams
                async with ClientSession(read_stream, write_stream) as session:
                    init_result = await session.initialize()
                    tools_result = await session.list_tools()

                response["init"] = model_to_dict(init_result)
                response["tools_response"] = model_to_dict(tools_result)
                response["tool_names"] = extract_tool_names(response["tools_response"])
                response["capability_names"] = extract_capability_names(response["init"])
                response["success"] = True
        except Exception as exc:
            response["error"] = exception_to_dict(exc)
            status_code = response["error"].get("status_code")
            if status_code in (401, 403):
                response["auth_required"] = True
                auth_info = parse_auth_info(response["error"]["headers"], status_code)
                response.update(auth_info)
            else:
                response["auth_required"] = False
        else:
            response["auth_required"] = False

        return response

    async def try_streamable_http_connection(
        self,
        target_url: str,
        auth_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = build_headers(auth_header)
        response: Dict[str, Any] = {
            "url": target_url,
            "path": "/mcp",
            "transport": "streamable_http",
            "success": False,
            "session_id": None,
            "init": None,
            "tools_response": None,
            "tool_names": [],
            "capability_names": [],
            "error": None,
        }

        try:
            async with streamablehttp_client(
                target_url,
                headers=headers,
                timeout=self.timeout,
                sse_read_timeout=DEFAULT_SSE_READ_TIMEOUT,
            ) as client_streams:
                read_stream, write_stream, get_session_id = client_streams
                async with ClientSession(read_stream, write_stream) as session:
                    init_result = await session.initialize()
                    tools_result = await session.list_tools()

                response["session_id"] = get_session_id()
                response["init"] = model_to_dict(init_result)
                response["tools_response"] = model_to_dict(tools_result)
                response["tool_names"] = extract_tool_names(response["tools_response"])
                response["capability_names"] = extract_capability_names(response["init"])
                response["success"] = True
        except Exception as exc:
            response["error"] = exception_to_dict(exc)
            status_code = response["error"].get("status_code")
            if status_code in (401, 403):
                response["auth_required"] = True
                auth_info = parse_auth_info(response["error"]["headers"], status_code)
                response.update(auth_info)
            else:
                response["auth_required"] = False
        else:
            response["auth_required"] = False

        return response

    async def try_mcp_connection(
        self,
        target: Dict[str, str],
        auth_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        if target["transport"] == "sse":
            return await self.try_sse_connection(target["url"], auth_header)
        return await self.try_streamable_http_connection(target["url"], auth_header)

    async def inspect_server(
        self,
        raw_target: str,
        auth_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        server = raw_target.strip()
        results: Dict[str, Any] = {
            "server": server,
            "accessible": False,
            "requires_authentication": False,
            "auth_type": None,
            "auth_resource_metadata": None,
            "auth_scope": None,
            "auth_raw_header": None,
            "auth_status_code": None,
            "responses": [],
            "tools": [],
            "capabilities": [],
            "transport_type": None,
            "tested_paths": [],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        targets = build_candidate_targets(server)
        for target in targets:
            results["tested_paths"].append(target["path"])
            try:
                response = await asyncio.wait_for(
                    self.try_mcp_connection(target, auth_header),
                    timeout=self.timeout + DEFAULT_SSE_READ_TIMEOUT,
                )
            except Exception as exc:
                response = {
                    "url": target["url"],
                    "path": target["path"],
                    "transport": target["transport"],
                    "success": False,
                    "auth_required": False,
                    "error": exception_to_dict(exc),
                }

            results["responses"].append(response)

            if response.get("success"):
                results["accessible"] = True
                if results["transport_type"] is None:
                    results["transport_type"] = response["transport"]
                results["tools"].extend(response.get("tool_names", []))
                results["capabilities"].extend(response.get("capability_names", []))
            elif response.get("auth_required"):
                results["requires_authentication"] = True
                if results["auth_type"] is None:
                    results["auth_type"] = response.get("auth_type")
                    results["auth_resource_metadata"] = response.get("auth_resource_metadata")
                    results["auth_scope"] = response.get("auth_scope")
                    results["auth_raw_header"] = response.get("auth_raw_header")
                    results["auth_status_code"] = response.get("auth_status_code")

        results["tools"] = sorted(set(results["tools"]))
        results["capabilities"] = sorted(set(results["capabilities"]))
        results["tested_paths"] = list(dict.fromkeys(results["tested_paths"]))

        if not results["accessible"] and not results["requires_authentication"]:
            results["error"] = "all transport attempts failed"

        return results

    async def inspect_all_servers(
        self,
        servers: List[str],
        auth_header: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        semaphore = asyncio.Semaphore(50)

        async def inspect_with_semaphore(server: str) -> Dict[str, Any]:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self.inspect_server(server, auth_header),
                        timeout=(self.timeout + DEFAULT_SSE_READ_TIMEOUT) * 2 + 5,
                    )
                except asyncio.TimeoutError:
                    return {
                        "server": server.strip(),
                        "accessible": False,
                        "requires_authentication": False,
                        "auth_type": None,
                        "auth_resource_metadata": None,
                        "auth_scope": None,
                        "auth_raw_header": None,
                        "auth_status_code": None,
                        "responses": [],
                        "tools": [],
                        "capabilities": [],
                        "transport_type": None,
                        "tested_paths": list(PATH_TRANSPORTS.keys()),
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "error": "timeout",
                    }

        tasks = [inspect_with_semaphore(server) for server in servers if server.strip()]
        results: List[Dict[str, Any]] = []

        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scanning servers"):
            try:
                result = await asyncio.wait_for(coro, timeout=(self.timeout + DEFAULT_SSE_READ_TIMEOUT) * 2 + 10)
                results.append(result)
            except Exception:
                continue

        return results


def save_results(results: List[Dict[str, Any]], output_file: str) -> None:
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


def load_servers(input_file: str) -> List[str]:
    servers: List[str] = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            json_data = json.loads(line)
            link = json_data.get("link")
            if link:
                servers.append(link)
    return servers


async def main() -> None:
    servers = load_servers(DEFAULT_INPUT_FILE)
    auth_header = None

    client = MCPClient(timeout=DEFAULT_TIMEOUT)
    results = await client.inspect_all_servers(servers, auth_header)

    save_results(results, DEFAULT_OUTPUT_FILE)
    print(f"\nResults saved to {DEFAULT_OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
