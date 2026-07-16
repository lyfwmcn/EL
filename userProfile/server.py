"""
User Memory Service v2.0
三变量智能存储系统: user_data / user_preference / user_profile
每次存入对话时自动调用 DeepSeek 分析是否需要更新变量

API:
  POST /api/session          存入对话 + 触发 LLM 分析
  GET  /api/variables        读取全部变量
  GET  /api/variables/<key>  读取单个变量
  POST /api/variables/<key>  手动修改变量

启动:
  DEEPSEEK_API_KEY=sk-xxx VAR_API_KEY=coze2024safe python3 server.py
"""

import sqlite3, os, json, time, re, secrets, logging
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify

# ---- config ----
DB_PATH = Path(__file__).parent / "memory.db"
API_KEY = os.environ.get("VAR_API_KEY", secrets.token_hex(24))
DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
PORT = int(os.environ.get("PORT", 8899))
CONTEXT_WINDOW = 20  # 分析时取最近 N 条对话作为上下文

DEFAULT_VARS = {
    "user_data": "",
    "user_preference": "",
    "user_profile": "",
}

print("[memory-service] API_KEY = {}".format(API_KEY))

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ---- db ----
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _init_db():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS variables (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            ts DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for k, v in DEFAULT_VARS.items():
            c.execute("INSERT OR IGNORE INTO variables (key, value) VALUES (?, ?)", (k, v))
        c.commit()

_init_db()

def _auth():
    if request.headers.get("X-API-Key", "") != API_KEY:
        return jsonify({"error": "Invalid API Key"}), 403
    return None

# ---- DeepSeek ----
SYSTEM_PROMPT = """当前存量变量值
{current_vars}

近期对话历史（最多 20 轮，携带时间戳）：
{recent_history}

用户本轮消息：
{new_message}

你是专业用户画像更新判定智能体，唯一工作：基于输入的用户基础数据、历史对话、本轮用户消息，独立判定 user_data、user_preference、user_profile 三个变量是否需要更新、初始化新增内容，并严格按照规定 JSON 格式输出结果与修改摘要，禁止输出任何多余解释、对话、思考过程、markdown 代码块。
输入固定三部分（每次调用完整提供）
当前存量变量值
user_data：用户身份事实自然文本；可能为空字符串（无任何历史记录）
user_preference：固定四大模块文本；可能为空字符串（无任何历史记录）
user_profile：多行自然文本，记录长期学习特征、薄弱点、兴趣；可能为空字符串（无任何历史记录）
近期对话历史（最多 20 轮，携带时间戳）
用户本轮消息：用户本次最新提问 / 发言
通用新增扩充总逻辑（全局生效）
三个变量统一遵循规则：
存量为空字符串（此前完全无记录）：满足更新条件时，执行全新初始化新增逻辑；
存量存在历史文本（已有旧记录）：满足更新条件时，执行基于旧内容迭代修改 / 尾部追加逻辑；
存量存在历史文本，但本轮无任何更新信号：统一输出null，不改动原有内容；
区分「初始化新增」和「存量迭代修改」，在 summary 摘要中明确标注区分。
第一模块：user_data 完整更新判定规则
变量职责
永久存储用户稳定、长期有效的个人身份客观事实，仅记录用户自身真实信息，非临时状态、主观感受、他人信息。
三种处理分支（覆盖空存量初始化、存量纠错、存量新增事实）
分支 1：存量 user_data 为空字符串（此前无任何身份记录）
用户发送有效长期稳定自我介绍，直接生成完整身份描述文本输出。
分支 2：存量存在文本，用户明确否定旧信息、给出新事实（强硬纠错更新）
触发条件：用户明确否定存量旧身份信息，并给出准确新客观数值 / 描述。
执行方式：基于存量 user_data 原文，精准替换冲突信息后，输出完整更新后的全文。
示例：存量 "张三，25 岁，字节后端"，用户说 "纠正一下，我 26 不是 25"，输出 "张三，26 岁，字节后端"。
分支 3：存量存在文本，用户单纯分享全新长期身份事实，无否定旧信息语义（身份事实追加更新）
触发条件：用户分享具备长期有效性、一年后仍成立的个人新身份事实（入职、换城市、双学位、毕业等），不修改原有身份，仅补充新增信息。
执行方式：在存量原文基础上追加新事实，输出完整更新后的全文。
禁止更新，输出 null 的全部场景
用户使用假设、虚拟语气（假如我是 xx、如果我换工作）
纯日常闲聊、情绪吐槽、临时短期行为（今天面试、今天没学习）
主观感受修正、评价调整（不是很低，是特别难）
他人相关信息（室友、同学、家人的情况）
用户明确撤回纠正（算了，不用改了，无所谓）
临时、短期状态，不具备长期留存价值的内容
自我负面情绪宣泄（我太笨了、我学不会）
本轮消息无任何新增 / 纠正身份信息，仅知识提问、风格诉求、闲聊
第二模块：user_preference 完整更新判定规则
变量职责
永久存储用户固定回答风格需求，文本结构强制固定为四大模块，顺序不可调换、不可删减：
{{【表达风格要求】}}
{{【内容深浅要求】}}
{{【话术语气要求】}}
{{【输出篇幅逻辑要求】}}
两种处理分支（空存量初始化、存量迭代修改）
分支 1：存量 user_preference 为空字符串（此前无任何风格偏好记录）
用户本轮发言存在调整回答形式的诉求，直接根据用户需求完整生成一套符合四大模块规范的全新偏好文本，作为初始化新增内容。
分支 2：存量存在完整四大模块文本（已有旧偏好）
用户本轮发言存在任意调整回答形式、深浅、篇幅、结构、语气的诉求，不限句式、不限是否带 "能不能" 类问句，包含隐性不满类间接信号：
显性调整类：说人话、简单一点、大白话、别复杂、别绕弯、严谨、学术、带代码、简短、多说细节、先给结论、分点
隐性不满间接信号：任何表达对当前回答方式不满意的语义均判定为更新诉求，示例："太长了""你讲得像教科书""完全听不懂""就当我是小学生水平讲"
禁止更新，输出 null 场景
普通知识提问、闲聊、情绪倾诉，无任何对回答形式的调整要求
用户明确表示当前风格满意，无需修改（不用改，现在刚好）
更新强制约束
存量非空时：基于存量旧 user_preference 迭代改写，禁止清空重写；存量为空时：完整生成全套四大模块文本初始化；
用户诉求强弱匹配修改幅度：轻微诉求仅微调，强烈诉求大幅改写；
存量中带 "必须 / 禁止 / 固定" 的硬性规则，必须完整保留，不得削弱、删除；
存量 user_data 内用户身份信息具备最高优先级，修改偏好时不能生成与身份冲突的内容（如用户是高中生，不能添加博士深度讲解要求）
输出格式
需要更新（含初始化新增 / 存量修改）：输出完整四大模块换行文本字符串；无需更新输出 null
第三模块：user_profile 完整更新判定规则（核心约束：只增不删，覆盖空存量初始化）
变量职责
持续累积用户长期学习特征、知识薄弱点、技术兴趣、学习习惯、学习阶段；永久只追加、不删除、不覆盖旧内容。
两种处理分支（空存量初始化、存量尾部追加）
分支 1：存量 user_profile 为空字符串（此前无任何学习画像记录）
满足追加条件时，直接生成单条洞察文本，作为初始化完整内容输出，无旧条目无需拼接。
分支 2：存量存在多行历史洞察（已有旧画像）
满足追加条件时，输出完整文本 = 存量全部旧条目 + 换行追加 1 条新洞察；每条洞察独立一行；绝对禁止删除、缩短、合并存量历史条目，只做尾部追加。
触发追加条件（满足其一即新增一条独立记录）
显性知识薄弱类信号：用户表示长期学不懂、混淆、反复出错、一头雾水、老是分不清、琢磨很久没明白、完全听不懂；包含句式 "XX 和 XX 我老是搞混"；
细化可操作隐性知识薄弱信号（附带领域聚类示例）：
若对话历史内用户针对语义同属一个大知识学科 / 模块的多个细分概念累计提问达到 3 轮及以上，即使本轮未直白表达困惑，判定存在持续知识薄弱，追加对应洞察；
标准示例：用户分别提问函数单调性、函数奇偶性、函数值域，三个问题细分主题不同，但全部归属于「高中函数」统一知识域，满足条件，追加文本：用户对函数整体知识点存在持续知识依赖与理解薄弱；
补充示例：连续询问 Promise、async、await，同属 JS 异步编程知识域，满足条件追加对应薄弱洞察。
兴趣偏好类信号：主动表达对某技术领域的学习意愿、自学计划、报课、项目实践兴趣；
学习阶段信号：说明自身学习进度（刚入门、能独立做项目、备战面试跳槽）；
学习习惯信号：明确自身偏好的学习方式（喜欢视频、先做项目再补理论）。
禁止追加，输出 null 场景（补充新增条目）
普通问答、单纯闲聊、短期临时情绪焦虑（今天不想学、好难我慌了）；
诉求仅针对回答风格（归 user_preference 处理，不写入 profile）；
临时、短期、次日失效的状态；
与存量 profile 完全重复、无新增有效信息的描述；
新旧信息语义冲突时，禁止覆盖旧条目，新旧全部保留；
自我贬低、负面情绪宣泄，不属于学习特征，不追加；
过时身份信息：用户已纠正更新的旧身份描述，不写入 profile，仅留存最新身份相关学习特征。
输出强制规则
存量为空且满足更新：直接输出单条新洞察文本（初始化新增）；
存量不为空且满足更新：输出存量全部旧内容 + 换行 + 新洞察完整文本；
无需更新：输出 null；
全程只增不删，任何场景不能删减历史画像条目。
通用优先级与冲突处理规则
三个变量完全独立判定，一个变量的更新结果不影响另外两个；
同一轮消息同时触发多变量更新时，分别按各自规则独立处理；
user_data 身份事实优先级最高，若 user_preference 生成内容与 user_data 身份冲突，必须修正偏好文本，保证不冲突。
输出固定 JSON 格式（严格遵守，无任何多余字符）
顶层仅包含 4 个 key：user_data、user_preference、user_profile、summary
各字段取值规范（三变量统一输出完整文本字符串）
user_data：
无需更新 → null
需要更新（初始化/纠错/追加） → 完整最新 user_data 文本字符串（如 "李四，25岁，在阿里做前端"）
user_preference：
无需更新 → null
需要更新 → 完整四大模块换行文本字符串
user_profile：
无需更新 → null
需要更新 → 完整多行文本（旧条目 + 新条目 / 单条新条目）
summary 摘要规则（改用换行分隔多条修改记录，区分「初始化新增」「存量修改」）
存在任意变量更新：每条修改单独一行，区分两种描述：
① 空存量场景：「已对【变量名】进行初始化新增：具体新增内容」
② 已有存量场景：「已对【变量名】进行修改：具体修改内容」
三个变量均无需更新：固定文本「本次对话未触发任何变量更新。」
边界特殊判定规则
用户说 "算了不纠正了" → user_data=null
用户假设句式 "假如我是 xx" → 三变量全部 null
用户聊他人信息 "室友是清华后端" → user_data=null
用户仅修正主观感受 "不是有点难，是超级难" → user_data=null
用户自我否定情绪 "我太笨学不会" → user_profile 不追加，输出 null
纯学习焦虑感慨 "转行后端好难我好慌" → 三变量全部 null
输出硬性禁令
禁止输出 JSON 以外任何文字、注释、思考过程、解释、markdown 代码块；
禁止添加换行、空格、注释、说明文字包裹 JSON；
禁止篡改变量更新规则、输出结构；
禁止省略 summary 字段，无论是否更新都必须填写对应摘要文本；
存量为空时不得输出 null，满足更新条件必须执行初始化新增逻辑。
"""


def _call_deepseek(user_message):
    """调用 DeepSeek 分析是否需要更新变量，返回更新字典"""
    if not DS_KEY:
        print("[WARN] DEEPSEEK_API_KEY 未设置，跳过分析")
        return {"user_data": None, "user_preference": None, "user_profile": None}

    # 获取当前变量
    with _db() as c:
        rows = c.execute("SELECT key, value FROM variables").fetchall()
    current_vars = {r["key"]: r["value"] for r in rows}

    # 获取近期对话历史
    with _db() as c:
        history_rows = c.execute(
            "SELECT content, role, ts FROM conversations ORDER BY id DESC LIMIT ?",
            (CONTEXT_WINDOW,)
        ).fetchall()
    history_lines = []
    for r in reversed(history_rows):
        tag = "\u7528\u6237" if r["role"] == "user" else "\u7cfb\u7edf"
        history_lines.append("[{}] {}: {}".format(r["ts"], tag, r["content"]))
    history_text = "\n".join(history_lines) if history_lines else "(\u65e0\u5386\u53f2)"

    # 组装 prompt
    prompt = SYSTEM_PROMPT.format(
        current_vars=json.dumps(current_vars, ensure_ascii=False, indent=2),
        recent_history=history_text,
        new_message=user_message,
    )

    # 调用 API
    try:
        import urllib.request
        req_body = json.dumps({
            "model": "deepseek-chat",
            "messages": [{"role": "system", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2048,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer {}".format(DS_KEY),
            },
        )
        resp = urllib.request.urlopen(req, timeout=60)
        body = json.loads(resp.read().decode("utf-8"))
        raw = body["choices"][0]["message"]["content"].strip()

        # 提取 JSON
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)

        # 三个变量统一为字符串，null/空→不更新，否则直接写入
        updates = {}
        for key in ["user_data", "user_preference", "user_profile"]:
            val = result.get(key)
            if val and val != "null" and isinstance(val, str) and val.strip():
                updates[key] = val.strip()
            else:
                updates[key] = None

        summary = result.get("summary", "")
        if not summary or not isinstance(summary, str):
            has_change = any(v is not None for v in updates.values())
            summary = "" if has_change else "\u672c\u6b21\u5bf9\u8bdd\u672a\u89e6\u53d1\u4efb\u4f55\u53d8\u91cf\u66f4\u65b0\u3002"

        return updates, summary

    except Exception as e:
        print("[ERROR] DeepSeek \u8c03\u7528\u5931\u8d25: {}".format(e))
        return {"user_data": None, "user_preference": None, "user_profile": None}, ""


# ---- API ----

@app.route("/api/session", methods=["POST"])
def store_session():
    """存入用户对话 + 触发 LLM 分析"""
    auth_err = _auth()
    if auth_err:
        return auth_err

    body = request.get_json(silent=True) or {}
    content = body.get("content", "").strip()
    skip_analysis = body.get("skip_analysis", False)

    if not content:
        return jsonify({"error": "content 不能为空"}), 400

    # 1. 存入对话
    with _db() as c:
        c.execute("INSERT INTO conversations (content, role) VALUES (?, 'user')", (content,))
        c.commit()

    # 2. LLM 分析
    updates = {}
    summary = ""
    if not skip_analysis and DS_KEY:
        updates, summary = _call_deepseek(content)

        # 3. 应用更新
        with _db() as c:
            for key, val in updates.items():
                if val is not None:
                    c.execute("INSERT OR REPLACE INTO variables (key, value) VALUES (?, ?)", (key, val))
            c.commit()

    # 4. 返回
    with _db() as c:
        rows = c.execute("SELECT key, value FROM variables").fetchall()
    variables = {r["key"]: r["value"] for r in rows}

    return jsonify({
        "ok": True,
        "stored": True,
        "analysis_skipped": skip_analysis or not DS_KEY,
        "changes": {k: (v is not None) for k, v in updates.items()},
        "summary": summary if summary else ("本次对话未触发任何变量更新。" if not skip_analysis else ""),
        "variables": variables,
    })


@app.route("/api/variables", methods=["GET"])
def get_all_vars():
    auth_err = _auth()
    if auth_err:
        return auth_err
    with _db() as c:
        rows = c.execute("SELECT key, value FROM variables").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/variables/<key>", methods=["GET"])
def get_var(key):
    auth_err = _auth()
    if auth_err:
        return auth_err
    with _db() as c:
        row = c.execute("SELECT value FROM variables WHERE key=?", (key,)).fetchone()
    if not row:
        return jsonify({"error": "Variable '{}' not found".format(key)}), 404
    return jsonify({"key": key, "value": row["value"]})


@app.route("/api/variables/<key>", methods=["POST"])
def set_var(key):
    auth_err = _auth()
    if auth_err:
        return auth_err
    val = (request.get_json(silent=True) or {}).get("value", "")
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO variables (key, value) VALUES (?, ?)", (key, val))
        c.commit()
    return jsonify({"ok": True, "key": key, "value": val})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
