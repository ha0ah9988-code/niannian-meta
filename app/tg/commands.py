"""TG 系统命令解析 —— /status /rules /clear /memory 等"""

from .sender import send
from .api import CHAT_ID


def handle(command: str, agent, chat_id: str = CHAT_ID) -> str:
    """处理系统命令，返回命令执行的文本回复

    Args:
        command: 原始命令字符串（包含 /）
        agent: Agent 实例
        chat_id: 消息来源

    Returns:
        回复文本（如为空表示命令已自行发送回复）
    """
    cmd = command.strip().lower()

    # 委托给 agent 的 system_command
    return agent.system_command(command)
