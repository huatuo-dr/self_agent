# Pipeline 耗时报告仓库模板

本目录用于初始化独立的 GitLab Pages 报告仓库，复制后可独立构建运行，不依赖 self_agent 源码。包含平均耗时分析、阶段总览、Job 时间线、失败原因筛选、Pages CI、数据白名单和测试；`tasks/` 初始为空。

模板提供可复用的站点源码与 CI 配置，Runner 标签由部署者按环境设置。不包含真实监控记录、收件人、凭证、构建产物或 Git 历史。

## 初始化新报告仓库

1. 在 GitLab 创建空项目，记录 SSH 地址和默认分支。确认项目支持 GitLab Pages（模板使用 GitLab 18.0+ 的 `pages.publish`）。
2. 在 self_agent 根目录执行下列命令，将示例地址、仓库名替换为实际值。先检查 `.project/` 是否已有同一仓库，已有则核对 origin 后复用；同名目录属于其他项目时不要覆盖。

   ```bash
   git clone git@gitlab.example.com:team/my-reports.git .project/my-reports
   cp -rn templates/pipeline-pages/. .project/my-reports/
   ```

   复制命令保留目标已有文件。新建仓库若已有 README 或 CI，请手动核对差异；模板升级也需审查合并，不能用此命令当作覆盖升级工具。不要复制现有报告仓库的 `tasks/` 数据到另一个项目。

3. 配置 Runner：需要可使用 `node:22-alpine`、`python:3.12-slim` 镜像的 Runner。模板默认使用可接收无标签任务的 Runner；若组织要求标签，在 `.gitlab-ci.yml` 的 `default` 中添加实际 `tags`。Pages 访问权限由该报告仓库管理员配置。
4. 在报告仓库根目录验证：

   ```bash
   python3 -m unittest discover -s tests
   node --test tests/analytics.test.cjs
   python3 scripts/build.py
   ```

5. 将源码提交并推送至报告仓库的默认分支；等待 `analytics_tests` 和 `pages` 成功。空仓库也能生成页面，之后由 self_agent 写入监控数据。
6. 在 self_agent 的 `.env` 配置并重启服务：

   ```dotenv
   PIPELINE_PAGES_REPO_URL=git@gitlab.example.com:team/my-reports.git
   PIPELINE_PAGES_BRANCH=main
   PIPELINE_MONITOR_ENABLED=true
   PIPELINE_PUBLISH_ENABLED=true
   ```

   发布分支必须与该项目默认分支一致，本地报告工作区也应检出该分支。工作区地址由仓库名推导为 `.project/my-reports`。还需要已有的 GitLab 监控配置，以及目标 GitLab 主机的 glab 登录和 SSH 权限。Pages URL 在部署后通过 API 获取，无需硬填。

## 数据和发布约定

- `tasks/<task_id>/manifest.json` 描述监控任务，月份 JSON 保存允许公开的执行数据；构建时按 `scripts/public_schema.py` 重新过滤，不能直接复制未脱敏数据到 `public/`。
- 默认先展示 Pipeline 与 Job 平均耗时，成功／失败分别筛选；单次详情先展示阶段总览，再展示选定阶段的 Job 时间线及明细。
- `pages` job 和 `pipeline-pages` 环境名是 self_agent 的部署核验约定，请保留。CI 仅在默认分支运行，使用 resource_group 串行发布并拒绝过期提交。
- 修改站点代码、数据白名单或 CI 后，需要按 self_agent 的共享发布代码审查流程接受具体版本，自动发布不会覆盖源码。
- 已有发布记录绑定仓库和分支。更改 `.env` 不会迁移旧数据、发布版本或旧链接；检测到目标变化会阻止发布，历史迁移需单独处理，不能通过清空运行数据库来绕过。

## 模板维护

`templates/pipeline-pages/` 是 self_agent 随仓库分发的新建模板，独立报告仓库是实际运行站点。两者不自动双向同步，也不会在服务启动时自动安装或覆盖。后续通用站点改动应在审查后同步到模板，项目专属数据、权限、Runner 设置保留在目标仓库。

在 self_agent 根目录运行 `python3 -m unittest discover -s tests -p 'test_pipeline*.py'`，会默认验证发布端与此模板的公开数据契约；设置 `PIPELINE_PAGES_TEST_ROOT` 可以改为核对指定独立报告仓库。

站点按任务 manifest 的 `timezone` 解释日期和展示时间；旧数据没有该字段时兼容 `Asia/Shanghai`。修改公开数据结构后同时核对助手发布器与模板输入白名单。
