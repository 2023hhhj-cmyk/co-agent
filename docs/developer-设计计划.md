# Developer 设计计划

Developer 只处理 CEO 派来的一项任务。项目怎么拆、派给谁，仍由 CEO 决定。Developer 可以在这一项内部再拆小计划，然后逐项调用 Claude Code。

## 1. 输入

Developer 只读 CEO 消息里的这些字段：

- `container_name` / `volume_name`：CEO 为该客户创建的长期容器和数据卷
- `client_dir`：容器内工作区 `/workspace`
- `design_doc_path`：工作区中的需求文档
- 任务说明：这一项要做什么
- `acceptance`：做完要交出的产物
- 上游产物路径

工作区是容器里的 `/workspace`。需求文档从 `design_doc_path` 读取。Developer 在这个工作区里建子目录，不另建客户容器，也不把宿主机目录当成客户边界。

## 2. 小计划

Developer 把这一项的做法写入容器工作区 `tasks/<任务 id>/plan.md`。任务已经可以一次做完时，计划里只有一项。每一项写清：

- 要改什么
- 依赖前面哪一项
- 做完留下哪个文件

实现时按这份文件逐项调用 Claude Code。拆分不交回 CLI 自己决定。

Claude Code 的 plan 模式不作为调度器。它是一次会话里的只读草稿，进程结束后不会交回结构化计划，下一步也接不上。当前沙箱使用 `acceptEdits`。某一项需要先看代码再动手时，可以让这一次调用先写出草稿，再由 Developer 落成 `plan.md`。之后的实现调用仍按这份文件走。

## 3. 每一步的上下文

每次调用 Claude Code 都重新带上上下文，不依赖上一次 CLI 会话还开着：

- 客户容器、数据卷和工作区 `/workspace`
- 当前小项的说明
- 前面已完成小项的结果路径，不传整段终端输出
- 这一项对应的验收要求

## 4. 返回给 CEO 的结果

结果写成容器工作区里的 `tasks/<任务 id>/result.md`，消息里交回这个路径。文档包含：

- 是否完成
- 计划里每一项的状态
- 实际写下的文件
- 对照 `acceptance` 的结论
- 给下一项用的短说明

验收要求的文件不存在时，结果标为失败。CEO 读这份文档决定完成或跳过下游。标准输出不再当作交付。

## 5. 收掉现有的自驱流程

文件：`agents/developer/agent.py`

收到 `ceo_task_assignment` 后，不再走内部的再拆分，也不再从架构设计自动进入第二遍实现。`review_code` 并入结果文档的验收，不再作为另一套由消息类型触发的动作。
