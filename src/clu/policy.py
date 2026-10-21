"""server 端硬规则（DESIGN §8）：危险模式黑名单（无条件）+ 登录节点重操作拦截。

极薄两层之外不设审批流；其余信任 Claude Code 的工具权限配置。
"""

import re
from dataclasses import dataclass

DANGEROUS = "dangerous"
HEAVY = "heavy"


@dataclass(frozen=True)
class PolicyDecision:
    """判定结果。allowed 为 False 时 message 必含拒绝原因。"""

    allowed: bool
    rule: str  # ok | dangerous | heavy
    message: str = ""
    suggestion: str = ""

    @property
    def reason(self) -> str:
        return self.message or self.rule


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns]


# 无条件拒绝（任何连接、任何命令形态）；rm 的递归删除目标用 token 判定
_DANGEROUS_PATTERNS: list[re.Pattern[str]] = _compile(
    [
        r"\bmkfs(\.\w+)?\b",
        r"\bdd\b[^|]*\bof=/dev/",
        r"\b(shutdown|reboot|halt|poweroff)\b",
        r"\bscancel\b.*(--all|-u\s+\S+)",
        r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
        r"\bchmod\s+(-R\s+)?777\s+/\s*$",
    ]
)

# 登录节点重操作拦截（仅 kind=cluster 连接；profile 可追加）
_DEFAULT_HEAVY_PATTERNS: list[str] = [
    r"\btorchrun\b",
    r"\bdeepspeed\b",
    r"\baccelerate\s+launch\b",
    r"\bpython\s+-m\s+torch\.distributed",
    r"\bmake(\s+-j\d*)?\b",
    r"\bcmake\s+--build\b",
    r"\bconda\s+(create|install)\b",
    r"\bpip\s+install\b",
]


_TOP_LEVEL_DIR = re.compile(r"^/[A-Za-z0-9_.-]+$")  # 单层系统目录（/var、/etc），不含 /tmp/x
_WHOLE_HOME = {"~", "$HOME"}


_RECURSIVE_FLAG = re.compile(r"-[A-Za-z]*[rR][A-Za-z]*")


def _rm_dangerous(command: str) -> bool:
    """rm 递归删除的目标是否为根/单层系统目录/整个家目录（token 级判定，避免正则深度误判）。"""
    tokens = command.split()
    for i, token in enumerate(tokens):
        if token != "rm":
            continue
        rest = tokens[i + 1 :]
        has_recursive = any(_RECURSIVE_FLAG.fullmatch(f) for f in rest if f.startswith("-"))
        if not has_recursive:
            continue
        for target in rest:
            if target.startswith("-"):
                continue  # 旗标
            if target == "/" or target in _WHOLE_HOME or _TOP_LEVEL_DIR.match(target):
                return True
    return False


def dangerous_hits(command: str) -> list[str]:
    """命中危险模式（含模式文本）。"""
    hits = [p.pattern for p in _DANGEROUS_PATTERNS if p.search(command)]
    if _rm_dangerous(command):
        hits.append("rm 递归删除根/系统目录/家目录")
    return hits


def check_exec(
    command: str,
    *,
    kind: str,
    extra_heavy: list[str] | None = None,
    heavy_guard: bool = True,
) -> PolicyDecision:
    """exec 前的完整判定：危险黑名单（任何 kind）+ 登录节点重操作（cluster）。

    heavy_guard=False（profile 显式关闭）时跳过重操作规则；危险黑名单不受影响。
    """
    hits = dangerous_hits(command)
    if hits:
        return PolicyDecision(
            allowed=False,
            rule=DANGEROUS,
            message=f"危险命令被拒绝（命中黑名单模式：{hits[0]}）",
            suggestion="如确需执行，请人工登录操作；此规则不可绕过",
        )
    if kind == "cluster" and heavy_guard:
        heavy = _DEFAULT_HEAVY_PATTERNS + list(extra_heavy or [])
        for pattern in heavy:
            if re.search(pattern, command):
                return PolicyDecision(
                    allowed=False,
                    rule=HEAVY,
                    message=f"登录节点不允许重操作（命中模式：{pattern}）",
                    suggestion="用 submit 提交批处理作业，或用 alloc 申请交互式资源后执行",
                )
    return PolicyDecision(allowed=True, rule="ok")


def check_submit_script(script: str) -> PolicyDecision:
    """submit 脚本内容只查危险黑名单（重计算本就该进计算节点）。"""
    hits = dangerous_hits(script)
    if hits:
        return PolicyDecision(
            allowed=False,
            rule=DANGEROUS,
            message=f"脚本含危险命令被拒绝（命中黑名单模式：{hits[0]}）",
        )
    return PolicyDecision(allowed=True, rule="ok")
