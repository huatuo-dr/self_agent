"""Selected AI backend for the /auto decision loop."""
from pathlib import Path

from assistant.auto_reply import AdapterUnavailable


class AutoReadOnlyQuery:
    def __init__(self, cfg, state):
        from assistant.agent_runner import AgentRunner
        self.runner = AgentRunner(cfg, Path(__file__).resolve().parents[1], state)

    async def preflight(self):
        from assistant.agent_backends.base import AgentError
        try:
            await self.runner.preflight()
        except (AgentError, OSError, ValueError) as error:
            raise AdapterUnavailable('/auto 所选后端或统一隔离环境不可用：' + str(error)) from None

    async def decide(self, prompt, schema):
        from assistant.agent_backends.base import RunRequest
        result = await self.runner.run(RunRequest(
            'auto_query', prompt, output_schema=schema, timeout=90))
        return result.payload
