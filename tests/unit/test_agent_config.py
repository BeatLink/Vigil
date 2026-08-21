"""Agent config loading — how a deployment supplies the agent its token."""

import pytest

class TestAgentConfig:
    def test_a_token_file_is_read_and_trimmed(self, tmp_path, monkeypatch):
        from vigil_agent.config import AgentConfig

        token_file = tmp_path / "token"
        token_file.write_text("s3cret\n")
        config_file = tmp_path / "agent.yaml"
        config_file.write_text(
            f"url: ws://vigil/api/agent/ws\nid: node-a\ntoken_file: {token_file}\n"
        )
        for var in ('VIGIL_AGENT_URL', 'VIGIL_AGENT_ID', 'VIGIL_AGENT_TOKEN'):
            monkeypatch.delenv(var, raising=False)

        assert AgentConfig.load(str(config_file)).token == "s3cret"

    def test_an_unreadable_token_file_fails_loudly(self, tmp_path, monkeypatch):
        """Falling through to no token would surface as a confusing auth
        rejection at the server instead of a deployment error here."""
        from vigil_agent.config import AgentConfig

        config_file = tmp_path / "agent.yaml"
        config_file.write_text(
            f"url: ws://vigil/api/agent/ws\nid: node-a\ntoken_file: {tmp_path}/missing\n"
        )
        monkeypatch.delenv('VIGIL_AGENT_TOKEN', raising=False)

        with pytest.raises(SystemExit, match="token_file"):
            AgentConfig.load(str(config_file))

    def test_an_explicit_token_wins_over_the_file(self, tmp_path, monkeypatch):
        from vigil_agent.config import AgentConfig

        config_file = tmp_path / "agent.yaml"
        config_file.write_text(
            "url: ws://vigil/api/agent/ws\nid: node-a\n"
            f"token: inline\ntoken_file: {tmp_path}/missing\n"
        )
        monkeypatch.delenv('VIGIL_AGENT_TOKEN', raising=False)
        assert AgentConfig.load(str(config_file)).token == "inline"

