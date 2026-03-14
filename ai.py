"""
调用 ChatGPT (OpenAI 兼容 API) 进行对话
支持官方 API 或代理商/代理接口。

配置来源（与 app 一致）：data/config.json
  openai_api_key   - API 密钥（必填）
  openai_base_url  - 接口地址（可选，不设则用官方 https://api.openai.com/v1）
未配置时回退到环境变量 OPENAI_API_KEY、OPENAI_BASE_URL。
"""
import os
import json
import unicodedata
from pathlib import Path
from openai import OpenAI

# 与 app 一致：数据目录与配置文件路径
_script_dir = Path(__file__).resolve().parent
_data_root = Path("/data")
_DATA_DIR = _data_root if (_data_root / "config.json").exists() else (_script_dir / "data")
_CONFIG_PATH = _DATA_DIR / "config.json"


def _load_openai_from_config():
    """从 data/config.json 读取 OpenAI 相关配置，与 app 配置一致。"""
    out = {"openai_api_key": None, "openai_base_url": None}
    if not _CONFIG_PATH.exists():
        return out
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            c = json.load(f)
        out["openai_api_key"] = (c.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")) or None
        out["openai_base_url"] = (c.get("openai_base_url") or os.environ.get("OPENAI_BASE_URL")) or None
    except Exception:
        pass
    return out


_openai_cfg = _load_openai_from_config()
OPENAI_BASE_URL = (_openai_cfg.get("openai_base_url") or "").strip() or None


def chat_with_gpt(
    user_message: str,
    model: str = "gpt-3.5-turbo",
    system_prompt: str = "你是一个有帮助的助手。",
    history: list[dict] | None = None,
    base_url: str | None = None,
) -> tuple[str, list[dict]]:
    """
    与 ChatGPT 单轮对话，支持多轮上下文。

    :param user_message: 用户输入
    :param model: 模型名，如 gpt-3.5-turbo / gpt-4
    :param system_prompt: 系统角色描述
    :param history: 历史消息 [{"role": "user/assistant", "content": "..."}, ...]
    :param base_url: 接口地址，不传则用 data/config.json 的 openai_base_url 或环境变量 OPENAI_BASE_URL 或官方地址
    :return: (助手回复, 更新后的 history)
    """
    api_key = _openai_cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("请在 data/config.json 中配置 openai_api_key，或设置环境变量 OPENAI_API_KEY")

    # 优先用参数，其次环境变量，都没有则用官方（不传 base_url）
    url = base_url or OPENAI_BASE_URL
    client = OpenAI(api_key=api_key, base_url=url) if url else OpenAI(api_key=api_key)
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    assistant_message = resp.choices[0].message.content or ""
    new_history = (history or []) + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": assistant_message},
    ]
    return assistant_message, new_history


# 文件名非法字符与常见标点（含全角），替换为下划线
_FILENAME_FORBIDDEN = set('/\\:*?"<>|()（）')
_FILENAME_PUNCT_TO_UNDERSCORE = set('，。、；：！？,.;:!?\'" \t\n\r')


def _sanitize_filename(s: str) -> str:
    """清理 AI 生成的文件名：只保留中英文、数字和部分安全字符，标点一律改为下划线。"""
    s = unicodedata.normalize("NFKC", s.strip().strip('"\''))
    cleaned: list[str] = []
    for ch in s:
        if ch in _FILENAME_FORBIDDEN or ch in _FILENAME_PUNCT_TO_UNDERSCORE:
            cleaned.append("_")
            continue
        cat = unicodedata.category(ch)
        if cat and cat[0] in ("L", "N"):
            cleaned.append(ch)
        elif ch in {"_", "-"}:
            cleaned.append(ch)
        else:
            cleaned.append("_")
    s2 = "".join(cleaned)
    s2 = "_".join(s2.split())
    while "__" in s2:
        s2 = s2.replace("__", "_")
    return s2.strip("_") or "video"


def generate_video_filename_from_text(
    text: str,
    model: str = "gpt-4o-mini",
    base_url: str | None = None,
) -> str:
    """
    从文本中严格提取关键词，根据关键词生成视频文件名。
    不去除露骨描述，严格按关键词提取，原样保留表述。

    :param text: 原始文本（描述、标题等）
    :param model: 使用的模型
    :param base_url: 接口地址，不传则用环境变量或默认
    :return: 适合作为视频文件名的字符串
    """
    system_prompt = """你是一个视频文件名生成助手。

你的任务：从用户给出的文本中提取**最核心的关键词**，组成一个**简短**的视频文件名。

长度与数量要求（必须遵守）：
- 只提取 **4～8 个** 最核心关键词，宁少勿多。
- 总长度（含下划线）控制在 **40 个字符以内**，越短越好。
- 优先保留最能概括内容、最具辨识度的词，次要的舍去。

格式规则：
1. 输出中**禁止出现空格和任何标点**（包括逗号、句号、顿号等），多个关键词只用**半角下划线**_连接，例如：词A_词B_词C。
2. 不要加文件扩展名、不要加引号、不要输出任何解释，只输出一行纯文件名。
3. 不要使用 / \\ : * ? " < > | 等非法文件名字符；可用中文、英文、数字、下划线、短横线，中文关键词请**直接保留中文**，不要转成拼音。
4. 只输出有意义的**实词/关键词**，不要输出虚词、助词（如：的、了、被、让、叫、是、在、把、让、给、着、过等）。
5. 关键词按原文提取，不要美化，不要去除露骨或成人向描述；若词过长可适当缩写。
"""

    user_message = f"请从以下文本中提取 4～8 个最核心关键词，生成简短的文件名（总长 40 字以内，只输出文件名一行）：\n\n{text}"
    reply, _ = chat_with_gpt(
        user_message,
        model=model,
        system_prompt=system_prompt,
        base_url=base_url,
    )
    return _sanitize_filename(reply)


def run_conversation(model: str = "gpt-3.5-turbo"):
    """在终端里与 ChatGPT 多轮对话，输入 quit 或 exit 退出。"""
    print("ChatGPT 对话 (输入 quit 或 exit 退出)\n")
    history: list[dict] = []
    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见。")
            break
        try:
            reply, history = chat_with_gpt(user_input, model=model, history=history)
            print(f"ChatGPT: {reply}\n")
        except Exception as e:
            print(f"错误: {e}\n")


if __name__ == "__main__":
    # run_conversation()
    print(generate_video_filename_from_text("""
    📋 消息原文:
#狙击手 (2022)

主演：陈永胜 / 章宇 / 张译 / 刘奕铁 / 黄炎 / 王梓屹 / 陈铭杨 / 王乃训 / 程泓鑫 / 赵琥成 / 李汶聪 / 林博洋 / 王佑名 / 代文博 / 李鲲 / 曹操 / 柯国庆 / 钱焜 / 暗真 / 柯南·何裴 / 李凯文 / 勃小龙 / 孟丹青 / 叶风光
类型：剧情 / 历史 / 战争
制片国家/地区：中国大陆
语言：汉语普通话 / 英语
上映日期：2022-02-01(中国大陆)
片长：96分钟

📄剧情简介
1952年冬至1953年初，抗美援朝战争进入僵持阶段。交战双方开始了低密度的狙击战，这就是历史上著名的“冷枪冷炮运动”。志愿军一方，班长刘文武（章宇 饰）带领的狙击五班本领过硬，枪法绝神，战功赫赫的同时，也成为了美军忌惮憎恶的红色杀神。为了打击狙击五班的“嚣张气焰”，美军司令部调来了约翰（曹操 Jonathan Kos-Read 饰）率领的狙击小队试图捉住章宇。而自视甚高的约翰仰仗着高超的枪击和先进的武器装备，设计了一个极其大胆的诱捕计划。他将负伤的侦察兵亮亮（刘奕铁 饰）作为诱饵，吸引狙击五班来到了他精心布置的陷阱中央。
寒冷肃杀的战场上，一场静默的较量即将展开……

➖➖➖➖➖➖
😃😃😃😃😃😃😃
铂莱大鳄捕鱼综合台，赞助10WU！
😀😃😄😁🥹😅🤣🥲☺️⚡️ 人民币注册 ⚡️ USDT注册⚡️彩票入口
⚡️⚡️⚡️⚡️⚡️⚡️⚡️⚡️ 支持汇旺USDT存提款
😂😂😂😂😂😂😂😂 不限ip，无须实名
❤️🔥❤️😁 不限ip  U存U取 无须实名  
😀 国际真人电子0审核出款  
😍😍😍😍😍😍  电子真人0审核秒出款
🩷❤️🧡💛💚🩵💙💜🖤🩶优惠最多，提款最快！凯旋一夜暴富不是梦

#电影 #高清 #高清电影院 #影视
    
    
    """))
