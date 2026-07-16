"""
微信公众号云服务器版 — pymysql 直连本地 MySQL
"""

import sys, time
from datetime import datetime
import requests, pymysql

API_BASE = "http://localhost:3000/api/public/v1"
AUTH_KEY = "e0dba95bf8484590ba53decc0cac58f1"
PAGE_SIZE = 20
REQUEST_DELAY = 0.5
MAX_RECORDS = 50
HEADERS = {"X-Auth-Key": AUTH_KEY}

DB = {"host":"127.0.0.1","port":3306,"user":"ZCW","password":"SiKongZhen@2026",
      "database":"nju_news_db","charset":"utf8mb4"}

ACCOUNTS = {
    "南京大学":"MzAxODAzMjQ1NQ==","南京大学学生会":"MjM5MjY4NTY5NQ==",
    "南京大学新生学院":"MzkwNDE4ODYyMg==","南大全球交流":"MzAwMDYzNDc4MQ==",
    "南大后勤":"Mzg4NzIzODkzNA==","南大就业":"MzAxMDA3MjIwMw==",
    "南大青年":"MzA3NzExMDEyMQ==","南大青协":"MzA3NTQ2ODUyOA==",
    "南大社团":"MzIxNTg4MjY0NA==","南大体育":"MzI2ODcyNTU2OQ==",
    "南大研会":"MzU1MTYxNDcxMw==","南大研招":"MzA4NTU0MDI2NA==",
    "南大育教":"MzI1MzIyMzEyMg==","南大招生小蓝鲸":"MzA3NzQ2ODEwNQ==",
    "南京大学本科教育":"Mzk3NTc1MjE0OA==","南京大学开甲书院":"Mzk0MjE5MDI5Nw==",
    "南京大学图书馆":"MjM5NTE5Mjk1Mg==","南京大学心理中心":"MzA5NDYzNTE3Ng==",
    "南京大学研究生教育":"MzkwMTIyMTE2Mg==","南哪助手":"MzkxNDMxNTU5Nw==",
    "南青科创":"MzI4MjM3OTYyNw==","南青实践":"MzUxMDA1ODAxMQ==",
}

def fetch(account, fid):
    arts, begin = [], 0
    while len(arts) < MAX_RECORDS:
        data = requests.get(f"{API_BASE}/article?fakeid={fid}&begin={begin}&size={PAGE_SIZE}",
                           headers=HEADERS, timeout=30).json()
        batch = data.get("articles", [])
        if not batch: break
        for a in batch:
            ts = a.get("create_time", 0)
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            arts.append(dict(title=a["title"], date=dt, url=a["link"]))
        if len(batch) < PAGE_SIZE: break
        begin += PAGE_SIZE; time.sleep(REQUEST_DELAY)
    return arts[:MAX_RECORDS]

def main():
    start = datetime.now()
    print(f"🚀 {start.strftime('%Y-%m-%d %H:%M:%S')}  {len(ACCOUNTS)}个号")
    conn = pymysql.connect(**DB, connect_timeout=5)
    cur = conn.cursor()
    for account, fid in ACCOUNTS.items():
        arts = fetch(account, fid)
        if not arts: continue
        table = f"wx_{account}"
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INT AUTO_INCREMENT PRIMARY KEY,"
            " scrape_date DATETIME NOT NULL, account VARCHAR(50) NOT NULL,"
            " news_date VARCHAR(20) DEFAULT '', title VARCHAR(500) NOT NULL,"
            " url VARCHAR(500) NOT NULL, UNIQUE KEY uk_url(url)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        st = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [(st, account, a["date"], a["title"], a["url"]) for a in arts]
        cur.executemany(f"INSERT IGNORE INTO {table} (scrape_date,account,news_date,title,url) VALUES (%s,%s,%s,%s,%s)", rows)
        conn.commit()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        total = cur.fetchone()[0]
        if total > MAX_RECORDS:
            cur.execute(f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM (SELECT id FROM {table} ORDER BY id DESC LIMIT {MAX_RECORDS}) AS t)")
            conn.commit()
        print(f"  {account}: {len(arts)}篇, 表{min(total,MAX_RECORDS)}条")
        time.sleep(1)
    cur.close(); conn.close()
    print(f"✅ 耗时: {(datetime.now()-start).total_seconds():.0f}s")

if __name__ == "__main__":
    main()
