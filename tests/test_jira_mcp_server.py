import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
import tools.jira_mcp_server as server
from tools.jira_mcp_server import JiraClient


class TestJiraClientInit:
    def test_missing_all_env_vars_exits(self, monkeypatch):
        for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(SystemExit) as exc:
            JiraClient()
        assert exc.value.code == 1

    def test_missing_one_env_var_exits(self, monkeypatch):
        monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "test@test.com")
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            JiraClient()

    def test_valid_env_vars_sets_base_url(self, monkeypatch):
        monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net/")
        monkeypatch.setenv("JIRA_EMAIL", "test@test.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "secret")
        client = JiraClient()
        assert client.base_url == "https://test.atlassian.net"  # trailing slash stripped

    def test_valid_env_vars_sets_auth_header(self, monkeypatch):
        import base64
        monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "user@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "tok123")
        client = JiraClient()
        expected = "Basic " + base64.b64encode(b"user@example.com:tok123").decode()
        assert client.headers["Authorization"] == expected


class TestJiraClientRequest:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "test@test.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "secret")
        return JiraClient()

    def _mock_http(self, status_code, json_data=None, text=""):
        mock_http = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.is_success = 200 <= status_code < 300
        mock_response.content = b"x" if json_data is not None else b""
        mock_response.json.return_value = json_data or {}
        mock_response.text = text
        mock_http.__enter__.return_value.request.return_value = mock_response
        return mock_http

    def test_success_returns_parsed_json(self, client):
        mock = self._mock_http(200, {"key": "TEST-1"})
        with patch("httpx.Client", return_value=mock):
            result = client.request("GET", "issue/TEST-1")
        assert result == {"key": "TEST-1"}

    def test_empty_body_returns_empty_dict(self, client):
        mock = self._mock_http(204)
        with patch("httpx.Client", return_value=mock):
            result = client.request("PUT", "issue/TEST-1")
        assert result == {}

    def test_401_returns_auth_error(self, client):
        mock = self._mock_http(401)
        with patch("httpx.Client", return_value=mock):
            result = client.request("GET", "issue/TEST-1")
        assert "authentication failed" in result.lower()

    def test_403_returns_auth_error(self, client):
        mock = self._mock_http(403)
        with patch("httpx.Client", return_value=mock):
            result = client.request("GET", "issue/TEST-1")
        assert "authentication failed" in result.lower()

    def test_404_returns_not_found(self, client):
        mock = self._mock_http(404)
        with patch("httpx.Client", return_value=mock):
            result = client.request("GET", "issue/BAD-1")
        assert "not found" in result.lower()

    def test_500_returns_api_error(self, client):
        mock = self._mock_http(500, text="Internal Server Error")
        with patch("httpx.Client", return_value=mock):
            result = client.request("GET", "issue/TEST-1")
        assert "500" in result

    def test_connect_error_returns_message(self, client):
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value.request.side_effect = httpx.ConnectError("fail")
            result = client.request("GET", "issue/TEST-1")
        assert "Could not reach Jira" in result

    def test_timeout_returns_message(self, client):
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value.request.side_effect = httpx.TimeoutException("timeout")
            result = client.request("GET", "issue/TEST-1")
        assert "timed out" in result.lower()


class TestTextToAdf:
    def test_wraps_text_in_adf_doc(self):
        from tools.jira_mcp_server import _text_to_adf
        result = _text_to_adf("hello world")
        assert result["type"] == "doc"
        assert result["version"] == 1
        assert result["content"][0]["type"] == "paragraph"
        assert result["content"][0]["content"][0]["text"] == "hello world"


@pytest.fixture
def mock_client(monkeypatch):
    """Patch the module-level _client with a MagicMock."""
    m = MagicMock()
    monkeypatch.setattr(server, "_client", m)
    return m


class TestSearchTools:
    def test_search_by_jql_calls_search_endpoint(self, mock_client):
        mock_client.request.return_value = {"issues": [], "total": 0}
        result = server.jira_search_by_jql("project = TEST")
        mock_client.request.assert_called_once_with(
            "GET", "search", params={"jql": "project = TEST", "maxResults": 50}
        )
        assert json.loads(result)["total"] == 0

    def test_search_by_jql_with_fields(self, mock_client):
        mock_client.request.return_value = {"issues": []}
        server.jira_search_by_jql("project = TEST", fields=["summary", "status"])
        call_kwargs = mock_client.request.call_args[1]
        assert call_kwargs["params"]["fields"] == "summary,status"

    def test_search_by_jql_no_fields_omits_fields_param(self, mock_client):
        mock_client.request.return_value = {"issues": []}
        server.jira_search_by_jql("project = TEST")
        call_kwargs = mock_client.request.call_args[1]
        assert "fields" not in call_kwargs["params"]

    def test_get_ticket_calls_issue_endpoint(self, mock_client):
        mock_client.request.return_value = {"key": "TEST-1", "fields": {}}
        result = server.jira_get_ticket("TEST-1")
        mock_client.request.assert_called_once_with("GET", "issue/TEST-1", params={})
        assert json.loads(result)["key"] == "TEST-1"

    def test_get_ticket_with_fields(self, mock_client):
        mock_client.request.return_value = {"key": "TEST-1"}
        server.jira_get_ticket("TEST-1", fields=["summary"])
        call_kwargs = mock_client.request.call_args[1]
        assert call_kwargs["params"]["fields"] == "summary"

    def test_get_subtasks_uses_parent_jql(self, mock_client):
        mock_client.request.return_value = {"issues": []}
        server.jira_get_subtasks("TEST-1")
        call_args = mock_client.request.call_args
        assert call_args[0] == ("GET", "search")
        assert "parent = TEST-1" in call_args[1]["params"]["jql"]

    def test_search_by_jql_returns_error_string_on_error(self, mock_client):
        mock_client.request.return_value = "Error: Jira authentication failed."
        result = server.jira_search_by_jql("project = TEST")
        assert result == "Error: Jira authentication failed."
        assert not result.startswith('"')  # must not be JSON-encoded

    def test_get_subtasks_rejects_invalid_key(self, mock_client):
        result = server.jira_get_subtasks("not-valid OR project=EVIL")
        assert result.startswith("Error:")
        mock_client.request.assert_not_called()


class TestCreateTools:
    def test_create_ticket_basic_posts_to_issue(self, mock_client):
        mock_client.request.return_value = {"key": "TEST-2", "id": "10001"}
        result = server.jira_create_ticket_basic("TEST", "Bug", "Login fails", "Steps to reproduce")
        call_args = mock_client.request.call_args
        assert call_args[0] == ("POST", "issue")
        body = call_args[1]["json"]
        assert body["fields"]["project"]["key"] == "TEST"
        assert body["fields"]["issuetype"]["name"] == "Bug"
        assert body["fields"]["summary"] == "Login fails"
        assert body["fields"]["description"]["type"] == "doc"
        assert json.loads(result)["key"] == "TEST-2"

    def test_create_ticket_with_parent_includes_parent_key(self, mock_client):
        mock_client.request.return_value = {"key": "TEST-3"}
        server.jira_create_ticket_with_parent("TEST", "Sub-task", "Subtask title", "desc", "TEST-1")
        body = mock_client.request.call_args[1]["json"]
        assert body["fields"]["parent"]["key"] == "TEST-1"

    def test_create_ticket_basic_returns_json_string(self, mock_client):
        mock_client.request.return_value = {"key": "TEST-2"}
        result = server.jira_create_ticket_basic("TEST", "Bug", "s", "d")
        parsed = json.loads(result)
        assert parsed["key"] == "TEST-2"


class TestUpdateTools:
    def test_update_description_puts_to_issue(self, mock_client):
        mock_client.request.return_value = {}
        result = server.jira_update_description("TEST-1", "New description")
        call_args = mock_client.request.call_args
        assert call_args[0] == ("PUT", "issue/TEST-1")
        body = call_args[1]["json"]
        assert body["fields"]["description"]["type"] == "doc"
        assert "TEST-1" in result

    def test_update_description_propagates_error(self, mock_client):
        mock_client.request.return_value = "Error: Jira authentication failed."
        result = server.jira_update_description("TEST-1", "desc")
        assert result.startswith("Error:")

    def test_assign_ticket_puts_to_assignee(self, mock_client):
        mock_client.request.return_value = {}
        result = server.jira_assign_ticket_to("TEST-1", "acc123")
        call_args = mock_client.request.call_args
        assert call_args[0] == ("PUT", "issue/TEST-1/assignee")
        assert call_args[1]["json"] == {"accountId": "acc123"}
        assert "TEST-1" in result

    def test_move_to_status_finds_transition_by_name(self, mock_client):
        mock_client.request.side_effect = [
            {"transitions": [{"id": "31", "to": {"name": "In Progress"}}, {"id": "41", "to": {"name": "Done"}}]},
            {},
        ]
        result = server.jira_move_to_status("TEST-1", "In Progress")
        assert mock_client.request.call_count == 2
        second_call = mock_client.request.call_args_list[1]
        assert second_call[0] == ("POST", "issue/TEST-1/transitions")
        assert second_call[1]["json"] == {"transition": {"id": "31"}}
        assert "In Progress" in result

    def test_move_to_status_case_insensitive(self, mock_client):
        mock_client.request.side_effect = [
            {"transitions": [{"id": "31", "to": {"name": "In Progress"}}]},
            {},
        ]
        result = server.jira_move_to_status("TEST-1", "in progress")
        assert "In Progress" in result

    def test_move_to_status_unknown_status_returns_error(self, mock_client):
        mock_client.request.return_value = {"transitions": [{"id": "31", "to": {"name": "Done"}}]}
        result = server.jira_move_to_status("TEST-1", "Nonexistent")
        assert "Error" in result
        assert "Nonexistent" in result


class TestCommentTools:
    def test_post_comment_posts_to_comment_endpoint(self, mock_client):
        mock_client.request.return_value = {"id": "10001", "body": {}}
        server.jira_post_comment("TEST-1", "This is a comment")
        call_args = mock_client.request.call_args
        assert call_args[0] == ("POST", "issue/TEST-1/comment")
        assert call_args[1]["json"]["body"]["type"] == "doc"

    def test_post_comment_returns_json_string(self, mock_client):
        mock_client.request.return_value = {"id": "10001"}
        result = server.jira_post_comment("TEST-1", "comment")
        assert json.loads(result)["id"] == "10001"

    def test_get_comments_calls_comment_endpoint(self, mock_client):
        mock_client.request.return_value = {"comments": [], "total": 0}
        result = server.jira_get_comments("TEST-1")
        mock_client.request.assert_called_once_with("GET", "issue/TEST-1/comment")
        assert json.loads(result)["total"] == 0

    def test_post_comment_if_not_exists_skips_when_match(self, mock_client):
        long_text = "A" * 60
        existing_body = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": long_text}]}],
        }
        mock_client.request.return_value = {"comments": [{"body": existing_body}]}
        result = server.jira_post_comment_if_not_exists("TEST-1", long_text + " different ending")
        assert mock_client.request.call_count == 1
        assert "Skipped" in result

    def test_post_comment_if_not_exists_posts_when_no_match(self, mock_client):
        mock_client.request.side_effect = [
            {"comments": []},
            {"id": "10002"},
        ]
        result = server.jira_post_comment_if_not_exists("TEST-1", "Brand new comment")
        assert mock_client.request.call_count == 2
        second_call = mock_client.request.call_args_list[1]
        assert second_call[0] == ("POST", "issue/TEST-1/comment")

    def test_post_comment_if_not_exists_propagates_get_error(self, mock_client):
        mock_client.request.return_value = "Error: Resource not found at issue/BAD-1/comment."
        result = server.jira_post_comment_if_not_exists("BAD-1", "comment")
        assert result.startswith("Error")
        assert mock_client.request.call_count == 1


class TestMetadataTools:
    def test_get_account_by_email_queries_user_search(self, mock_client):
        mock_client.request.return_value = [{"accountId": "acc123", "emailAddress": "user@test.com"}]
        result = server.jira_get_account_by_email("user@test.com")
        mock_client.request.assert_called_once_with("GET", "user/search", params={"query": "user@test.com"})
        assert "acc123" in result

    def test_get_transitions_calls_transitions_endpoint(self, mock_client):
        mock_client.request.return_value = {"transitions": [{"id": "11", "to": {"name": "To Do"}}]}
        result = server.jira_get_transitions("TEST-1")
        mock_client.request.assert_called_once_with("GET", "issue/TEST-1/transitions")
        assert "To Do" in result

    def test_get_components_calls_project_components(self, mock_client):
        mock_client.request.return_value = [{"id": "1", "name": "Backend"}]
        result = server.jira_get_components("TEST")
        mock_client.request.assert_called_once_with("GET", "project/TEST/components")
        assert "Backend" in result

    def test_get_fix_versions_calls_project_versions(self, mock_client):
        mock_client.request.return_value = [{"id": "1", "name": "v1.0"}]
        result = server.jira_get_fix_versions("TEST")
        mock_client.request.assert_called_once_with("GET", "project/TEST/versions")
        assert "v1.0" in result


class TestFormatAndExtractText:
    def test_format_text_api_v3(self, monkeypatch):
        mock_c = MagicMock()
        mock_c.api_version = "3"
        monkeypatch.setattr(server, "_client", mock_c)
        res = server._format_text("hello")
        assert isinstance(res, dict)
        assert res["type"] == "doc"

    def test_format_text_api_v2(self, monkeypatch):
        mock_c = MagicMock()
        mock_c.api_version = "2"
        monkeypatch.setattr(server, "_client", mock_c)
        res = server._format_text("hello")
        assert res == "hello"

    def test_extract_comment_text_str(self):
        res = server._extract_comment_text("hello world")
        assert res == "hello world"

    def test_extract_comment_text_dict_adf(self):
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "hello "},
                        {"type": "text", "text": "world"},
                    ]
                }
            ]
        }
        res = server._extract_comment_text(adf)
        assert res == "hello world"


class TestMcpRegistration:
    def test_all_15_tools_registered(self):
        tool_names = set(server.mcp._tool_manager._tools.keys())
        expected = {
            "jira_search_by_jql",
            "jira_get_ticket",
            "jira_get_subtasks",
            "jira_create_ticket_basic",
            "jira_create_ticket_with_parent",
            "jira_update_description",
            "jira_assign_ticket_to",
            "jira_move_to_status",
            "jira_post_comment",
            "jira_get_comments",
            "jira_post_comment_if_not_exists",
            "jira_get_account_by_email",
            "jira_get_transitions",
            "jira_get_components",
            "jira_get_fix_versions",
        }
        assert expected == tool_names
