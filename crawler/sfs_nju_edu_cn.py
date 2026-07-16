"""
南京大学外国语学院通知公告爬虫 (Software NJU Scraper)
爬取网站: https://sfs.nju.edu.cn
爬取内容: 通知公告 (页面内嵌JS数据)
存储方式: MySQL
运行模式: 定时每日运行 / 手动单次运行 (python3.11 software_nju_edu_cn.py --once)
"""

import json
import re
import sys
import time
from collections import Counter
from datetime import datetime

import pymysql
import requests
import schedule

# ========== 配置区 ==========

BASE_URL = "https://sfs.nju.edu.cn"
NEWS_PAGES = {
    "外院动态": "xwgg/wydt/index.html",
    "各类通知": "xwgg/gltz/index.html",
    "教务公告栏": "rcpy/jwggl/index.html",
    "团学动态": "dqjs/tw/index.html",
}

MAX_RECORDS = 400
REQUEST_TIMEOUT = 15

MYSQL_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "ZCW",
    "password": "SiKongZhen@2026",
    "database": "nju_news_db",
    "charset": "utf8mb4",
}

TABLE_NAME = "sfs_nju_edu_cn_news"
SCHEDULE_TIME = "08:00"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ========== MySQL 操作 ==========

def get_db_connection() -> pymysql.Connection | None:
    try:
        return pymysql.connect(**MYSQL_CONFIG, connect_timeout=5)
    except pymysql.Error as e:
        print(f"⚠️ MySQL 连接失败: {e}")
        return None

def init_database():
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} ("
                "  id INT AUTO_INCREMENT PRIMARY KEY,"
                "  scrape_date DATETIME NOT NULL,"
                "  category VARCHAR(50) NOT NULL,"
                "  news_date VARCHAR(20) DEFAULT '',"
                "  title VARCHAR(500) NOT NULL,"
                "  url VARCHAR(500) NOT NULL,"
                "  UNIQUE KEY uk_url (url),"
                "  INDEX idx_category (category),"
                "  INDEX idx_news_date (news_date),"
                "  INDEX idx_scrape_date (scrape_date)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        conn.commit()
        return True
    except pymysql.Error as e:
        print(f"⚠️ 数据库初始化失败: {e}")
        return False
    finally:
        conn.close()

def save_to_mysql(items: list[dict]) -> int:
    if not items:
        return 0
    conn = get_db_connection()
    if conn is None:
        return 0
    scrape_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    sql = (
        f"INSERT IGNORE INTO {TABLE_NAME} (scrape_date, category, news_date, title, url) "
        "VALUES (%s, %s, %s, %s, %s)"
    )
    try:
        with conn.cursor() as cursor:
            rows = [
                (scrape_time, item["category"], item["date"], item["title"], item["url"])
                for item in items
            ]
            cursor.executemany(sql, rows)
            inserted = cursor.rowcount
        conn.commit()
        print(f"✅ MySQL 写入完成：新增 {inserted} 条（共 {len(items)} 条，跳过 {len(items) - inserted} 条重复）")
    except pymysql.Error as e:
        print(f"⚠️ MySQL 写入失败: {e}")
    finally:
        conn.close()
    return inserted

def cleanup_old_records(max_rows: int = MAX_RECORDS) -> int:
    conn = get_db_connection()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            total = cursor.fetchone()[0]
            if total <= max_rows:
                print(f"✅ 数据量 {total}/{max_rows}，无需清理")
                return 0
            delete_count = total - max_rows
            cursor.execute(
                f"DELETE FROM {TABLE_NAME} WHERE id NOT IN ("
                "  SELECT id FROM ("
                f"    SELECT id FROM {TABLE_NAME} ORDER BY id DESC LIMIT %s"
                "  ) AS t"
                ")",
                (max_rows,),
            )
        conn.commit()
        print(f"🗑️  数据清理完成：删除 {delete_count} 条，保留最新 {max_rows} 条")
        return delete_count
    except pymysql.Error as e:
        print(f"⚠️ 数据清理失败: {e}")
        return 0
    finally:
        conn.close()

# ========== 爬取逻辑 ==========

def scrape_all() -> list[dict]:
    """从各页面内嵌JS数据中提取内容"""
    items = []

    for category, path in NEWS_PAGES.items():
        url = f"{BASE_URL}/dqjs/tw/index.html"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            html = resp.text
        except requests.RequestException as e:
            print(f"  ⚠️ [{category}] 获取失败: {e}")
            continue

        match = re.search(r'var dataList=(\[.*\]);', html, re.DOTALL)
        if not match:
            print(f"  ⚠️ [{category}] 未找到 dataList")
            continue

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            raw = match.group(1)
            raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', raw)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"  ⚠️ [{category}] JSON 解析失败: {e}")
                continue

        cat_items = 0
        for section in data:
            articles = section.get("infolist", [])
            for article in articles:
                title = article.get("title", "").strip()
                url = article.get("linktitle", "").strip() or article.get("url", "").strip()
                release_time = article.get("releaseTime", 0)

                if not title or not url:
                    continue

                news_date = ""
                if release_time:
                    try:
                        news_date = datetime.fromtimestamp(release_time / 1000).strftime("%Y-%m-%d")
                    except (OSError, ValueError):
                        pass

                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = BASE_URL + url

                items.append({
                    "category": category,
                    "date": news_date,
                    "title": title,
                    "url": url,
                })
                cat_items += 1

        print(f"  📰 {category}: {cat_items} 条")

    return items
# ========== 主流程 ==========

def run_once():
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"🚀 外国语学院爬虫启动 {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    init_database()

    items = scrape_all()

    if not items:
        print("❌ 未爬取到任何数据")
        return

    print(f"\n📊 各分类爬取统计:")
    save_to_mysql(items)
    cleanup_old_records(MAX_RECORDS)

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n⏱️  总耗时: {elapsed:.1f} 秒")
    print(f"{'='*60}\n")

def main():
    if "--once" in sys.argv:
        run_once()
        return

    print(f"⏰ 外国语学院定时模式启动，每天 {SCHEDULE_TIME} 执行")
    schedule.every().day.at(SCHEDULE_TIME).do(run_once)
    run_once()

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n🛑 定时任务已停止")

if __name__ == "__main__":
    main()
