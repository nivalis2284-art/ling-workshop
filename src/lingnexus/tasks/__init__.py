"""Durable staged-workspace runtime, retained behind the unified chat loop.

C 阶段起，**对话是唯一入口**：模型在一条对话里就能读文件、改文件、跑命令
（经执行沙箱），不再要求用户先建任务、选目录、审批。任务的多步执行能力已经
下沉为对话循环本身的能力（``lingnexus.runtime``）。

这里保留 ``TaskRuntime``，因为它是"分阶段工作副本"这条能力线的实现：工作副本/
快照、变更审阅、成果写回确认、有现实副作用操作的事后核对。这些能力按契约必须
保留（§2.6、§四），因此任务路径**不再删除**，但**不再是必经之路**——它只在
用户明确需要"先看变更再写回"时使用。

``/tasks`` 的 HTTP 接口同样保留，供前端在"暂存工作副本"流程里使用；日常多步
目标走对话即可，无需经过它。
"""

from .sandbox import SandboxService
from .service import TaskRuntime
from .store import TASK_TABLES

__all__ = ["TASK_TABLES", "SandboxService", "TaskRuntime"]
