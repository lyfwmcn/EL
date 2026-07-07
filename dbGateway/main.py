from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv
import os

# 加载.env环境配置文件
load_dotenv()
app = FastAPI(title="数据库只读中转API", version="1.0")

# -------------------------- 读取环境配置 --------------------------
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT"))
DB_USER = os.getenv("DB_USER")
DB_PWD = os.getenv("DB_PASSWORD")
DB_DEFAULT = os.getenv("DB_NAME")
API_TOKEN = os.getenv("API_SECRET_TOKEN")

# -------------------------- Header 鉴权配置（优化点） --------------------------
api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)

def check_token(api_key: str = Depends(api_key_header)):
    """从请求头校验密钥，无/错误直接返回401"""
    if not api_key or api_key != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token无效，请检查请求头 X-API-Token")
    return True

# -------------------------- 请求体校验模型 --------------------------
class QueryRequest(BaseModel):
    sql: str

# -------------------------- MySQL 连接工具函数 --------------------------
def get_mysql_conn(database: str = None):
    conn = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PWD,
        database=database if database else None,
        charset="utf8mb4"
    )
    return conn

# -------------------------- 核心只读查询接口 --------------------------
@app.post("/api/query")
def query_database(req: QueryRequest, token_ok: bool = Depends(check_token)):
    raw_sql = req.sql.strip()
    sql_lower = raw_sql.lower()

    # 拦截高危修改类SQL关键字
    forbidden_keywords = [
        "insert", "delete", "update", "drop", "alter", "truncate",
        "create", "rename", "replace", "grant", "revoke"
    ]
    for word in forbidden_keywords:
        if word in sql_lower:
            raise HTTPException(status_code=403, detail="禁止执行增删改/建删表等操作，仅允许SELECT查询")

    # 强制必须以 select 开头
    if not sql_lower.startswith("select"):
        raise HTTPException(status_code=403, detail="仅支持以 SELECT 开头的查询语句")

    conn = None
    cur = None
    try:
        conn = get_mysql_conn(DB_DEFAULT if DB_DEFAULT else None)
        cur = conn.cursor(DictCursor)
        cur.execute(raw_sql)
        result_data = cur.fetchall()

        return {
            "code": 200,
            "msg": "success",
            "data": result_data
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"数据库执行异常: {str(err)}")
    finally:
        # 无论成功失败都关闭游标和连接，防止连接耗尽
        if cur:
            cur.close()
        if conn:
            conn.close()

# -------------------------- 服务健康检测接口 --------------------------
@app.get("/health")
def health_check():
    return {"code": 200, "status": "service running normally"}

# -------------------------- 程序启动入口 --------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        workers=2
    )