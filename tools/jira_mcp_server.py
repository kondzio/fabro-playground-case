import base64
import json
import os
import re
import sys

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jira")
_client: "JiraClient | None" = None


class JiraClient:
    def __init__(self) -> None:
        base_url = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_BASE_PATH") or "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        missing = []
        if not base_url:
            missing.append("JIRA_BASE_URL")
        if not email:
            missing.append("JIRA_EMAIL")
        if not token:
            missing.append("JIRA_API_TOKEN")
        if missing:
            print(f"Error: Missing required env vars: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        self.base_url = base_url
        is_cloud = "atlassian.net" in base_url.lower()
        if is_cloud:
            creds = base64.b64encode(f"{email}:{token}".encode()).decode()
            self.headers = {
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            self.api_version = "3"
        else:
            self.headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            self.api_version = "2"

    def request(self, method: str, path: str, **kwargs) -> "dict | list | str":
        url = f"{self.base_url}/rest/api/{self.api_version}/{path}"
        print(f"[jira-mcp] {method} {url}", file=sys.stderr)
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as http:
                response = http.request(method, url, headers=self.headers, **kwargs)
        except httpx.ConnectError:
            return f"Error: Could not reach Jira at {self.base_url}. Check JIRA_BASE_URL."
        except httpx.TimeoutException:
            return "Error: Request to Jira timed out."
        if response.status_code in (401, 403):
            return "Error: Jira authentication failed. Check JIRA_EMAIL and JIRA_API_TOKEN."
        if response.status_code == 404:
            return f"Error: Resource not found at {path}."
        if not response.is_success:
            return f"Error: Jira API returned {response.status_code}: {response.text}"
        if not response.content:
            return {}
        return response.json()


def _text_to_adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _format_text(text: str) -> "str | dict":
    if _client and getattr(_client, "api_version", "3") == "2":
        return text
    return _text_to_adf(text)


def _extract_comment_text(body: "dict | str | None") -> str:
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if not content or not isinstance(content, list):
        return ""
    text_parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_content = block.get("content")
        if not block_content or not isinstance(block_content, list):
            continue
        for inline in block_content:
            if isinstance(inline, dict):
                text_parts.append(inline.get("text", ""))
    return "".join(text_parts)


@mcp.tool()
def jira_search_by_jql(jql: str, fields: list[str] | None = None) -> str:
    """Search for Jira tickets using JQL. Returns up to 50 results."""
    params: dict = {"jql": jql, "maxResults": 50}
    if fields:
        params["fields"] = ",".join(fields)
    result = _client.request("GET", "search", params=params)
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_get_ticket(key: str, fields: list[str] | None = None) -> str:
    """Get a specific Jira ticket by key with optional field filtering."""
    params: dict = {}
    if fields:
        params["fields"] = ",".join(fields)
    result = _client.request("GET", f"issue/{key}", params=params)
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_get_subtasks(key: str) -> str:
    """Get all subtasks of a Jira ticket."""
    if not re.match(r'^[A-Z][A-Z0-9_]+-\d+$', key):
        return f"Error: Invalid ticket key format: '{key}'. Expected format: PROJECT-123."
    jql = f"parent = {key} AND issueType in (subtask, sub-task, 'sub task')"
    result = _client.request("GET", "search", params={"jql": jql, "maxResults": 50})
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_create_ticket_basic(project: str, issue_type: str, summary: str, description: str) -> str:
    """Create a new Jira ticket with basic fields (project, issue type, summary, description)."""
    body = {
        "fields": {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": _format_text(description),
        }
    }
    result = _client.request("POST", "issue", json=body)
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_create_ticket_with_parent(
    project: str, issue_type: str, summary: str, description: str, parent_key: str
) -> str:
    """Create a new Jira ticket with a parent relationship."""
    body = {
        "fields": {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": _format_text(description),
            "parent": {"key": parent_key},
        }
    }
    result = _client.request("POST", "issue", json=body)
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_update_description(key: str, description: str) -> str:
    """Update the description of a Jira ticket."""
    body = {"fields": {"description": _format_text(description)}}
    result = _client.request("PUT", f"issue/{key}", json=body)
    if isinstance(result, str) and result.startswith("Error"):
        return result
    return f"Description updated for {key}."


@mcp.tool()
def jira_assign_ticket_to(key: str, account_id: str) -> str:
    """Assign a Jira ticket to a user by account ID. Use jira_get_account_by_email to find account IDs."""
    result = _client.request("PUT", f"issue/{key}/assignee", json={"accountId": account_id})
    if isinstance(result, str) and result.startswith("Error"):
        return result
    return f"Ticket {key} assigned to account {account_id}."


@mcp.tool()
def jira_move_to_status(key: str, status_name: str) -> str:
    """Move a Jira ticket to a specific status by name (case-insensitive)."""
    transitions = _client.request("GET", f"issue/{key}/transitions")
    if isinstance(transitions, str) and transitions.startswith("Error"):
        return transitions
    matched = [t for t in transitions.get("transitions", []) if t["to"]["name"].lower() == status_name.lower()]
    if not matched:
        available = [t["to"]["name"] for t in transitions.get("transitions", [])]
        return f"Error: Status '{status_name}' not found. Available: {available}"
    result = _client.request("POST", f"issue/{key}/transitions", json={"transition": {"id": matched[0]["id"]}})
    if isinstance(result, str) and result.startswith("Error"):
        return result
    return f"Ticket {key} moved to '{matched[0]['to']['name']}'."


@mcp.tool()
def jira_post_comment(key: str, comment: str) -> str:
    """Post a comment to a Jira ticket."""
    body = {"body": _format_text(comment)}
    result = _client.request("POST", f"issue/{key}/comment", json=body)
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_get_comments(key: str) -> str:
    """Get all comments for a Jira ticket."""
    result = _client.request("GET", f"issue/{key}/comment")
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_post_comment_if_not_exists(key: str, comment: str) -> str:
    """Post a comment only if no existing comment starts with the same 50 characters."""
    existing = _client.request("GET", f"issue/{key}/comment")
    if isinstance(existing, str) and existing.startswith("Error"):
        return existing
    prefix = comment[:50]
    for c in existing.get("comments", []):
        body = c.get("body", {})
        existing_text = _extract_comment_text(body)
        if existing_text[:50] == prefix:
            return f"Comment already exists on {key}. Skipped."
    body = {"body": _format_text(comment)}
    result = _client.request("POST", f"issue/{key}/comment", json=body)
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_get_account_by_email(email: str) -> str:
    """Get a Jira user account by email address. Returns accountId needed for jira_assign_ticket_to."""
    result = _client.request("GET", "user/search", params={"query": email})
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_get_transitions(key: str) -> str:
    """Get all available workflow transitions for a Jira ticket."""
    result = _client.request("GET", f"issue/{key}/transitions")
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_get_components(project: str) -> str:
    """Get all components for a Jira project."""
    result = _client.request("GET", f"project/{project}/components")
    if isinstance(result, str):
        return result
    return json.dumps(result)


@mcp.tool()
def jira_get_fix_versions(project: str) -> str:
    """Get all fix versions for a Jira project."""
    result = _client.request("GET", f"project/{project}/versions")
    if isinstance(result, str):
        return result
    return json.dumps(result)


if __name__ == "__main__":
    _client = JiraClient()
    print(f"Jira MCP server started (base_url={_client.base_url})", file=sys.stderr)
    mcp.run()
