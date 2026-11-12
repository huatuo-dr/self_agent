"""Explicit, bounded CLI probes. No Feishu sends or real GitLab mutations."""
import shutil
import struct
import tempfile
from pathlib import Path
import zlib

from .agent_backends.base import RunRequest, AgentError
from .agent_runner import AgentRunner, execute
from .agent_native_config import native_config
from .agent_policy import policy_for
from .agent_isolation import isolated


def sample_image(path):
    def chunk(tag, data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
    rows=(b'\x00'+b'\xff\x00\x00'*32+b'\x00\x00\xff'*32)*32
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',64,32,8,2,0,0,0))+
                     chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))


async def static_check(cfg, root):
    effective=native_config(cfg)
    runner=AgentRunner(effective,root)
    executable=await runner.preflight()
    with tempfile.TemporaryDirectory(prefix='agent-check-') as directory:
        work=Path(directory)
        with isolated(policy_for(effective,'classify',root),effective,work,executable) as box:
            version=await execute(box.wrap([executable,'--version']),30,cwd=work)
    # Only a conventional version banner is reported; arbitrary output stays private.
    import re
    found=re.search(r'\b[0-9]+\.[0-9]+\.[0-9]+\b',version)
    if not found:
        raise AgentError('所选 CLI 未返回可识别版本，请核对工具路径')
    return {'backend':runner.backend.name,'version':found.group(0),'isolation':'passed',
            'model_authentication':'not_checked'}


async def live_check(cfg):
    """Use a synthetic image and private temporary source/report fixtures only."""
    with tempfile.TemporaryDirectory(prefix='agent-live-check-') as directory:
        root=Path(directory); picture=root/'colors.png';sample_image(picture)
        (root/'sample.py').write_text('value = 1\n')
        runner=AgentRunner(cfg,root)
        schema={'type':'object','properties':{key:{'type':'string','enum':['red','blue']}
                for key in ('left','right')},'required':['left','right'],'additionalProperties':False}
        result=await runner.run(RunRequest('classify',
            'Identify the two solid colors in the attached image. Return left and right. Do not call tools.',
            output_schema=schema,images=(picture,),timeout=180))
        if result.payload!={'left':'red','right':'blue'}:
            raise AgentError('所选模型的图像与结构化能力探针未通过')
        checked={'backend':runner.backend.name,'model_and_image':'passed'}
        if cfg.get('REPORT_SKILL_ENABLED'):
            result=await runner.run(RunRequest('report',
                'Create a standalone UTF-8 HTML file named probe.html containing the text agent-check '
                'in the provided report workspace. Actually write the file. No network tools or business operations.',timeout=180))
            if not any(p.name=='probe.html' for p in result.artifacts):
                raise AgentError('所选后端未完成报告文件生成探针')
            checked['local_report']='passed'
        if cfg.get('EVOLUTION_ENABLED'):
            result=await runner.run(RunRequest('develop_discuss',
                f'Read {root / "sample.py"}. Describe it briefly. Do not modify anything.',timeout=180))
            if (root/'sample.py').read_text()!='value = 1\n':
                raise AgentError('讨论探针改写了只读源码')
            await runner.run(RunRequest('develop_execute',
                f'Edit only {root / "sample.py"} to exactly value = 2 followed by newline. '
                'Do not use git, network tools or business services.',writable_roots=(root,),
                approval_ref='explicit-live-fixture-probe',timeout=180))
            if (root/'sample.py').read_text()!='value = 2\n':
                raise AgentError('所选后端未完成临时源码改写探针')
            checked['development']='passed'
        return checked
