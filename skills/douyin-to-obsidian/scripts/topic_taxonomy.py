"""Topic tree for knowledge notes and explicit vault migration.

The folder says what problem a note answers.  Source, format, and interview
context are metadata, never physical categories.
"""
from __future__ import annotations

import re

ROOT = "知识库"
AGENT = ("基础与架构", "记忆管理", "上下文管理", "工具与技能", "规划与控制", "质量与安全", "实践与案例")
MEMORY = ("体系与分层", "写入与更新", "检索与调用", "遗忘与评测")
CONTEXT = ("组织与注入", "压缩与摘要", "长任务与恢复")
RAG = ("数据准备", "检索与重排", "生成与评测", "Agentic RAG")


def validate_category(category: list[str]) -> list[str]:
    if not isinstance(category, list) or not 2 <= len(category) <= 4:
        raise ValueError("主题分类须为 2–4 级")
    if any(not isinstance(part, str) or not part or part != part.strip()
           or re.search(r'[\\/:*?"<>|\[\]#^\x00-\x1f]', part) or part.startswith(".")
           for part in category):
        raise ValueError("主题目录名无效")
    if category[0] == "面试题库":
        # Knowledge notes must use semantic topic directories.
        raise ValueError("/dy 知识笔记不能放入面试题库，请使用知识主题目录")
    if category[0] == ROOT or any(part in ("抖音", "DY", "视频", "文章", "面试") for part in category):
        raise ValueError("来源、媒体形式与面试不能作为主题目录")
    if category[:2] == ["AI", "Agent"]:
        if len(category) < 3 or category[2] not in AGENT:
            raise ValueError("Agent 须使用固定的七个主题主干")
        if len(category) == 4:
            allowed = {"记忆管理": MEMORY, "上下文管理": CONTEXT,
                       "基础与架构": ("概念与组成", "框架与编排", "多智能体协作"),
                       "工具与技能": ("工具调用", "Skill设计", "MCP与集成"),
                       "规划与控制": ("任务规划", "循环与停止", "失败恢复与回滚"),
                       "质量与安全": ("评测与观测", "安全与权限"),
                       "实践与案例": ("项目实现", "产品与行业")}
            if category[3] not in allowed[category[2]]:
                raise ValueError("Agent 子主题不在固定目录中")
    if category[:2] == ["AI", "RAG"] and len(category) >= 3:
        if category[2] not in RAG:
            raise ValueError("RAG 须使用固定的四个主题主干")
        if len(category) == 4:
            allowed = {"数据准备": ("解析与切片", "索引与更新"),
                       "检索与重排": ("向量与混合检索", "重排与性能"),
                       "生成与评测": ("答案质量", "评测方法"), "Agentic RAG": ()}
            if category[3] not in allowed[category[2]]:
                raise ValueError("RAG 子主题不在固定目录中")
    return category


def _hit(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, flags=re.I))


def classify_note(old_path: str, title: str, insight: str = "", excerpt: str = "") -> tuple[list[str], str]:
    """Conservative title-first classification; ambiguous overviews stay at a parent.

    Return the semantic directory and a compact, reviewable reason.  The old
    directory only breaks ties after title and insight.  The body excerpt is a
    final tie-breaker; it never overrides a clear title.
    """
    path = old_path.casefold()
    title = title.casefold()
    insight = insight.casefold()
    excerpt = excerpt[:1200].casefold()

    def choose(pattern: str) -> str | None:
        if _hit(title, pattern):
            return "标题"
        if _hit(insight, pattern):
            return "核心洞察"
        return None

    # The title's central question wins over an old catch-all folder.
    title_has_other_problem = _hit(title, r"skill|mcp|工具|意图|路由|多.?agent|多智能体|架构|框架|评测|评估|安全|权限|规划|计划|loop|循环|react|训练|项目|产品")
    memory = ("标题" if _hit(title, r"记忆|memory|mem0|memu|长期保存|跨会话保存|遗忘")
              else "核心洞察" if not title_has_other_problem and _hit(insight, r"记忆|memory|长期保存") else None)
    context = ("标题" if _hit(title, r"上下文|context|compact|提示词压缩|prompt压缩|窗口压缩")
               else "核心洞察" if not title_has_other_problem and _hit(insight, r"上下文|context|compact") else None)
    retrieval = choose(r"rag|检索|召回|重排|向量数据库|embedding|bm25|知识图谱")
    security = ("标题" if _hit(title, r"prompt.?注入|提示词注入|越权|安全|权限|沙箱|评测|评估|观测")
                else "核心洞察" if not title_has_other_problem and _hit(insight, r"安全|权限|评测|评估") else None)
    tools = choose(r"skill|mcp|工具调用|function.?call|插件|tool.?use")
    planning = ("标题" if _hit(title, r"规划|计划|loop|循环|终止|中止|取消|回滚|重试|反思|恢复|意图识别|路由")
                else "核心洞察" if not title_has_other_problem and _hit(insight, r"规划|计划|loop|循环|终止|回滚") else None)

    parent_parts = path.rsplit("/", 1)[0].split("/")
    is_agent_path = any(part in ("agent", "agent技能", "agent控制", "agent架构", "agent记忆") for part in parent_parts)
    is_rag_path = any(part == "rag" or "检索增强" in part for part in parent_parts)
    is_llm_path = "/llm" in path or "大模型" in path or "/多模态/" in path or (
        "/基础设施/" in path and _hit(title, r"训练|推理|模型|kv.?cache|mxfp|gpu"))
    is_tool_path = path.startswith("工具/") or "claude-code/" in path or path.startswith("knowledge-base/")
    is_system_path = path.startswith("系统/")

    if is_agent_path and _hit(title, r"事故诊断"):
        return ["AI", "Agent", "质量与安全", "评测与观测"], "标题：诊断 Agent 的证据与质量"
    if is_agent_path and _hit(title, r"大多数.?agent.*工作流"):
        return ["AI", "Agent", "基础与架构", "概念与组成"], "标题：Agent 与工作流的概念边界"
    if is_agent_path and _hit(title, r"步搭建.*agent"):
        return ["AI", "Agent", "实践与案例", "项目实现"], "标题：最小 Agent 实现路线"
    if _hit(title, r"agentic.?rl|后训练|训练数据|训练实战|reward|sft|dpo|grpo|数据飞轮") and (
        is_agent_path or _hit(title, r"agent|智能体")):
        return ["AI", "大模型", "模型与训练", "训练与对齐"], "标题或文件名：Agent 的训练数据、奖励或后训练"
    if _hit(title, r"kv.?cache|前缀缓存|语义缓存"):
        return ["AI", "大模型", "推理与成本", "推理服务"], "标题：模型推理缓存与服务"
    if is_agent_path and _hit(title, r"长任务.*(断点|续跑|恢复)|checkpoint|检查点"):
        return ["AI", "Agent", "上下文管理", "长任务与恢复"], "标题：长任务状态检查点与恢复"
    if is_agent_path and _hit(title, r"prompt系统|prompt.?组装|提示词组装"):
        return ["AI", "Agent", "上下文管理", "组织与注入"], "标题：当前窗口提示词装配"
    if is_agent_path and _hit(title, r"(文件编辑|选错工具|工具设计|cli.*agent)"):
        return ["AI", "Agent", "工具与技能", "工具调用"], "标题：Agent 工具选择与调用设计"
    if is_agent_path and _hit(title, r"工具误调用|事故诊断|评价指标|评估指标"):
        leaf = "安全与权限" if _hit(title, r"误调用") else "评测与观测"
        return ["AI", "Agent", "质量与安全", leaf], "标题：Agent 质量与安全"
    if is_agent_path and _hit(title, r"jev|结构化决策"):
        return ["AI", "Agent", "规划与控制", "任务规划"], "标题：Agent 结构化决策"
    if is_agent_path and _hit(title, r"cli为什么突然火|vibe.?coding|agent.*瓶颈|agent.*配置思维"):
        return ["AI", "Agent", "实践与案例", "项目实现"], "标题：Agent 工程实践与落地"
    if is_agent_path and _hit(title, r"langchain streaming"):
        return ["开发工具", "编程与数据"], "标题：LangChain 流式接口"
    if is_agent_path and _hit(title, r"claude code.*检索.*rag"):
        return ["AI", "RAG", "检索与重排", "向量与混合检索"], "标题：代码检索与 RAG 方案边界"
    if _hit(title, r"最长回文|算法题") and not _hit(title, r"agent.*架构"):
        return ["开发工具", "编程与数据"], "标题：编程算法题"
    if _hit(title, r"大会.*议题|奇点智能.*大会"):
        return ["方法与应用", "行业观察"], "标题：行业大会与议题"
    if _hit(title, r"软件工程师.*面试") and not _hit(title, r"agent|rag"):
        return ["方法与应用", "通用模式"], "标题：软件工程岗位经验"

    if memory and (is_agent_path or _hit(title, r"agent|智能体|coding agent|claude|sitina")):
        base = ["AI", "Agent", "记忆管理"]
        if choose(r"写入|更新|保存|存储|策展|提取|沉淀"):
            return base + ["写入与更新"], f"{memory}：跨任务记忆的写入或更新"
        if choose(r"检索|读取|召回|调用|取回"):
            return base + ["检索与调用"], f"{memory}：跨任务记忆的检索或调用"
        if choose(r"遗忘|清理|评测|评估|过期"):
            return base + ["遗忘与评测"], f"{memory}：记忆的遗忘或评测"
        if choose(r"分层|架构|体系|结构|框架|类型|分类"):
            return base + ["体系与分层"], f"{memory}：记忆体系与分层"
        return base, f"{memory}：记忆管理概览，保留在父目录"

    if context and (is_agent_path or is_tool_path or _hit(title, r"agent|智能体|claude")):
        base = ["AI", "Agent", "上下文管理"]
        if choose(r"压缩|摘要|compact|总结|截断|瘦身"):
            return base + ["压缩与摘要"], f"{context}：当前上下文压缩与摘要"
        if choose(r"长任务|恢复|跨会话|断点|续跑"):
            return base + ["长任务与恢复"], f"{context}：长任务的上下文恢复"
        if choose(r"组织|注入|装配|动态|窗口|管理|工程"):
            return base + ["组织与注入"], f"{context}：当前窗口的信息组织与注入"
        return base, f"{context}：上下文管理概览，保留在父目录"

    if is_rag_path or (retrieval and not is_agent_path):
        base = ["AI", "RAG"]
        if choose(r"agentic.?rag|动态检索|自主检索|检索停止"):
            return base + ["Agentic RAG"], "标题或洞察：Agentic RAG 的动态检索"
        if choose(r"解析|切片|分块|chunk|清洗|摄取|索引构建|索引更新"):
            leaf = "索引与更新" if choose(r"索引|更新") else "解析与切片"
            return base + ["数据准备", leaf], "标题或洞察：RAG 数据准备"
        if choose(r"检索|召回|重排|向量|embedding|bm25|混合|搜索|search"):
            leaf = "重排与性能" if _hit(title, r"重排|性能|延迟|成本") else "向量与混合检索"
            return base + ["检索与重排", leaf], "标题或洞察：RAG 检索与重排"
        if choose(r"生成|答案|幻觉|评测|评估|质量|引用"):
            leaf = "评测方法" if choose(r"评测|评估|指标|基准") else "答案质量"
            return base + ["生成与评测", leaf], "标题或洞察：RAG 生成与评测"
        return base, "RAG 综合概览，保留在父目录"

    if is_agent_path or _hit(title, r"agent|智能体|multi.?agent|多.?agent|多智能体|react|langgraph"):
        base = ["AI", "Agent"]
        if _hit(title, r"react") and _hit(title, r"cot|什么是|区别"):
            return base + ["基础与架构", "概念与组成"], "标题：ReAct 与推理范式的基础概念"
        if _hit(title, r"意图识别"):
            return base + ["规划与控制", "任务规划"], "标题：Agent 意图判断与任务路由"
        if security:
            leaf = "安全与权限" if choose(r"安全|权限|注入|越权|沙箱") else "评测与观测"
            return base + ["质量与安全", leaf], f"{security}：Agent 质量或安全"
        if tools and (tools == "标题" or not title_has_other_problem):
            leaf = "Skill设计" if choose(r"skill") else "MCP与集成" if choose(r"mcp|插件") else "工具调用"
            return base + ["工具与技能", leaf], f"{tools}：Agent 工具与技能"
        if planning:
            leaf = "循环与停止" if choose(r"loop|循环|终止|中止|取消|死循环") else "失败恢复与回滚" if choose(r"失败|恢复|回滚|重试") else "任务规划"
            return base + ["规划与控制", leaf], f"{planning}：Agent 规划与控制"
        if choose(r"多智能体|多\s*agent|multi.?agent|协作|通信"):
            return base + ["基础与架构", "多智能体协作"], "标题或洞察：多智能体协作架构"
        if choose(r"框架|架构|编排|graph|langgraph|react|harness"):
            return base + ["基础与架构", "框架与编排"], "标题或洞察：Agent 架构与编排"
        if choose(r"项目|实践|案例|落地|企业|产品|行业|学习路线"):
            leaf = "产品与行业" if choose(r"企业|产品|行业") else "项目实现"
            return base + ["实践与案例", leaf], "标题或洞察：Agent 实践与案例"
        if choose(r"概念|什么是|原理|组成|能力|入门"):
            return base + ["基础与架构", "概念与组成"], "标题或洞察：Agent 基础概念"
        return base + ["基础与架构"], "Agent 综合文章，保留在主题父目录"

    if is_llm_path or _hit(title, r"llm|大模型|模型训练|moe|transformer|推理服务|量化|kv.?cache|mxfp|视觉模型|识图|超长文本"):
        base = ["AI", "大模型"]
        if choose(r"多模态|视觉|语音|视频|vlm"):
            return base + ["多模态"], "标题或洞察：多模态模型"
        if choose(r"训练|对齐|微调|rlhf|grpo|sft|lora"):
            return base + ["模型与训练", "训练与对齐"], "标题或洞察：模型训练与对齐"
        if choose(r"架构|结构|选型|moe|transformer"):
            return base + ["模型与训练", "模型架构与选型"], "标题或洞察：模型架构与选型"
        if choose(r"上下文|长文本|长序列"):
            return base + ["推理与成本", "长上下文"], "标题或洞察：模型长上下文能力"
        if choose(r"推理|解码|量化|成本|服务|吞吐|gpu|性能"):
            return base + ["推理与成本", "推理服务"], "标题或洞察：模型推理与成本"
        return base, "大模型综合概览，保留在父目录"

    if path.startswith("knowledge-base/cc-connect/"):
        return ["开发工具", "cc-connect"], "原目录：cc-connect 运行与配置"
    if is_system_path or "工具/devops" in path or (
        not is_tool_path and choose(r"部署|运维|服务器|devops|配置|调度|基础设施|流式输出|sse|websocket")):
        if "vim/" in path or "/go/" in path:
            return ["开发工具", "编程与数据"], "原目录：编程工具与依赖"
        leaf = "部署与故障" if choose(r"部署|故障|运维|服务|流式输出|sse|websocket") else "配置与调度"
        return ["系统与运维", leaf], "标题或原目录：系统与运维"

    if is_tool_path or _hit(title, r"claude.?code|cc-connect|obsidian|python|pandas|polars|duckdb"):
        base = ["开发工具"]
        if _hit(title + path, r"claude.?code|claudecode|claudehub"):
            return base + ["Claude Code"], "标题或原目录：Claude Code 工具"
        if _hit(title + path, r"cc-connect"):
            return base + ["cc-connect"], "标题或原目录：cc-connect 工具"
        if _hit(title + path, r"obsidian"):
            return base + ["Obsidian"], "标题或原目录：Obsidian 工具"
        return base + ["编程与数据"], "标题或原目录：编程与数据工具"

    if choose(r"prompt|提示词|意图|路由") or "意图识别" in path:
        return ["方法与应用", "Prompt与意图理解"], "标题或原目录：Prompt 与意图理解"
    if choose(r"行业|趋势|动态|大会|资讯"):
        return ["方法与应用", "行业观察"], "标题或洞察：行业观察"
    if "学习路线" in path:
        return ["方法与应用", "通用模式"], "原目录：通用学习方法"
    if _hit(excerpt, r"agent|智能体"):
        return ["AI", "Agent", "基础与架构"], "正文主旨：Agent；子主题不唯一，保留父目录"
    return ["方法与应用", "通用模式"], "标题与洞察未指向单一 AI 子主题，保留通用主题"
