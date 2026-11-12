# 通用线上报告仓库模板

用于新建或迁移 `self_report` 类型的 GitLab Pages 站点。复制后是独立仓库，不依赖 self_agent 源码，也不需要安装第三方 Python 包。

模板包含独立构建器、目录页样式和测试，提供以下默认内容：

- `reports/` 初始只有 `.gitkeep`，不包含历史报告或资源。
- `index.html` 保留空站点提示，已移除历史报告标题和链接。
- README 不包含原站点域名、个人仓库地址或业务内容。
- CI 只在向默认分支 push 时发布。

## 目录结构

```text
.gitlab-ci.yml          Pages 发布任务
.gitignore             排除 public/ 和 Python 缓存
index.html             目录页及报告列表生成标记
reports/.gitkeep        空报告目录
scripts/build_pages.py 构建器：校验编号、更新目录页、生成 public/
tests/test_build_pages.py
```

## 初始化新仓库

1. 在目标 GitLab 创建空项目，确定 SSH 地址和默认分支。项目需要可执行 `python:3.12-alpine` 镜像的 Runner 及 GitLab Pages 支持；若 Runner 要求标签，在 `.gitlab-ci.yml` 的 `pages` job 中填写实际 `tags`。
2. 在 self_agent 根目录执行以下示例，先将地址和仓库名换成实际值。已有同一仓库时先核对 origin 和工作区状态后复用，不重复 clone；同名目录被其他项目占用时不要覆盖。

   ```bash
   git clone git@gitlab.example.com:team/self_report.git .project/self_report
   cp -rn templates/self-report/. .project/self_report/
   ```

   `cp -rn` 保留目标已有文件。若新项目自带 README、CI 或目录页，应逐项审查差异；它不是覆盖升级命令。模板目录名是 `self-report`，实际仓库目录按目标仓库名命名，示例为 `self_report`。
3. 在新报告仓库根目录验证并预览：

   ```bash
   python3 -m unittest discover -s tests -v
   python3 scripts/build_pages.py
   python3 -m http.server 8000 --directory public
   ```

   打开 `http://localhost:8000/`，空站点应显示“暂无报告”。构建只依赖 Python 标准库；`public/` 是可重新生成的发布目录，不要放手工维护文件。
4. 检出目标项目的默认分支，审阅后提交源码和目录页并 push。等待 CI 的 `pages` job 成功，在 GitLab 的 **Deploy → Pages** 获取实际入口。站点地址由目标 GitLab 分配，不复制原站点的域名。
5. 按使用范围设置新项目和 Pages 的访问权限。新建仓库不会继承原仓库的成员、Runner、保护分支或 Pages 访问设置。

## 添加报告

- 报告 HTML 放在 `reports/` 第一层，扩展名使用小写 `.html`。
- 名称必须从 `00-名称.html` 开始，以两位数字连续递增；重复编号、缺号或格式不符会使构建失败。当前两位编号最多使用 `00`～`99`，达到上限前需调整构建和 bot skill 的共同规则。
- 报告可以自包含，也可引用 `reports/` 内的相对资源；构建会复制整个 `reports/`，目录页只列出第一层 HTML。这里只放准备发布的内容，不能存放凭证、原始私聊记录或未审阅附件。
- `index.html` 的 `REPORTS_START`／`REPORTS_END` 标记必须各保留一组。运行构建器会更新这一区域，以及 `public/index.html`、`public/reports/`；CI 不会自动把生成的根目录页提交回 Git。
- 报告直达链接使用实际 Pages 根地址与经过 URL 编码的 `reports/<文件名>` 拼接；中文、空格和特殊字符不能直接替换成猜测的 URL。

## 迁移历史报告

模板默认创建空站点。若需要历史内容，另行从旧仓库选择并审查 `reports/` 中的文件和资源，再复制到新仓库，不复制 `.git`、`public/` 或凭证。

保留原始文件名和编号可以维持相对报告路径。若只迁移部分报告导致缺号，当前构建会拒绝；需要迁移完整连续序列，或明确重新编号及对应链接变更。检查报告正文是否仍引用旧 Pages 域名和跨报告链接。运行测试和构建，再发布并验证目录链接与报告资源；新站点验收前保留旧站点。该流程不会自动迁移旧域名、重定向或访问权限。

## 接入 self_agent

服务已支持通用报告目标配置。先按根目录约定准备并核实 SSH 工作区，再配置：

```dotenv
GITLAB_SKILL_ENABLED=true
GITLAB_HOST=gitlab.example.com
REPORT_SKILL_ENABLED=true
REPORT_PUBLISH_ENABLED=true
REPORT_PAGES_REPO_URL=git@gitlab.example.com:team/self_report.git
REPORT_PAGES_BRANCH=main
```

仓库地址没有默认值，分支应与实际目标一致；修改配置后重启服务生效。bot skill 使用服务本轮提供的仓库地址、分支及本地目录，不需要修改技能文件。启用配置本身不触发发布，只有当前任务明确要求线上报告时才向该目标提交和 push，随后核实 CI 和 Pages 部署。

Pipeline 的 `PIPELINE_PAGES_REPO_URL` 管理另一类报告，不替代上述通用报告设置。Codex 本地报告不需要启用线上发布或 GitLab。字段和依赖见助手项目的 `docs/configuration.md`。

模板与运行站点不会自动双向同步。后续通用改进可审查后同步到模板，实际报告和项目专属部署设置留在对应独立仓库。
