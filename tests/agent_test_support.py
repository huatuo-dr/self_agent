"""Legacy business fixtures bypass OS isolation only inside unit tests.

Real boundary enforcement is covered separately by test_agent_isolation.
"""
from contextlib import contextmanager


@contextmanager
def isolated_fixture(policy, cfg, work, executable):
    class Box:
        path_map = {}
        work_path = work
        def map_path(self, path): return path
        def wrap(self, args): return args
        def publish(self): pass
    yield Box()


def install_agent_fixture(case, service):
    from unittest.mock import patch
    def start(patcher):
        patcher.start()
        case.addCleanup(patcher.stop)
    start(patch('assistant.agent_runner.isolated', isolated_fixture))
    async def run(args, timeout, input_text=None, cwd=None, on_event=None):
        return await service.run_command(args, timeout, input_text, cwd=cwd)
    start(patch('assistant.agent_runner.execute', run))
    async def scope(cfg, history, text, root):
        return {'kind':'report' if cfg.get('REPORT_SKILL_ENABLED') else 'chat',
                'gitlab_mode':'read' if cfg.get('GITLAB_SKILL_ENABLED') else 'none',
                'projects':[], 'repositories':False, 'deliver_files':False}
    start(patch('assistant.agent_tasks.plan_task', scope))
