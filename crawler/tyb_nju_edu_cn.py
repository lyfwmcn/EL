"""
南京大学体育部爬虫 (TYB NJU Scraper)
爬取网站: https://tyb.nju.edu.cn
爬取内容: 最新动态、协会动态
存储方式: MySQL
运行模式: 定时每日运行 / 手动单次运行 (python3.11 tyb_nju_edu_cn.py --once)
"""

import re
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urljoin

import pymysql
import requests
import schedule
from bs4 import BeautifulSoup

# ========== 配置区 ==========

BASE_URL = "https://tyb.nju.edu.cn"

NEWS_PAGES = {
    "最新动态": "fzlm/zxdt/index.html",
    "协会动态": "fzlm/xhdt/index.html",
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

TABLE_NAME = "tyb_nju_edu_cn_news"
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

def scrape_tyb() -> list[dict]:
    """从自定义 HTML 页面提取新闻"""
    items = []

    for category, path in NEWS_PAGES.items():
        url = f"{BASE_URL}/{path}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
        except requests.RequestException as e:
            print(f"  ⚠️ [{category}] 获取失败: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        found = 0

        for a in soup.find_all("a", href=True):
            href = a["href"]
            title = a.get_text(strip=True)

            # 过滤无关链接
            if not title or len(title) < 5:
                continue
            if ("fzlm" not in href and "/tyb/" not in href):
                continue
            if "index" in href:
                continue

            full_url = urljoin(url, href)
            if "DFS" in full_url:
                continue  # 跳过 PDF 附件

            # 尝试从标题或 URL 中提取日期
            news_date = ""
            date_match = re.search(r"/(\d{4})(\d{2})(\d{2})/", full_url)
            if date_match:
                y, m, d = date_match.groups()
                news_date = f"{y}-{m}-{d}"

            items.append({
                "category": category,
                "date": news_date,
                "title": title,
                "url": full_url,
            })
            found += 1

        print(f"  📰 {category}: {found} 条")

    return items


# ========== 主流程 ==========

def run_once():
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"🚀 体育部爬虫启动 {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    init_database()

    items = scrape_tyb()
    if not items:
        print("❌ 未爬取到任何数据")
        return

    stats = Counter(item["category"] for item in items)
    for cat, cnt in stats.items():
        print(f"📊 {cat}: {cnt} 条")

    save_to_mysql(items)
    cleanup_old_records(MAX_RECORDS)

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n⏱️  总耗时: {elapsed:.1f} 秒")
    print(f"{'='*60}\n")


def main():
    if "--once" in sys.argv:
        run_once()
        return

    print(f"⏰ 体育部定时模式启动，每天 {SCHEDULE_TIME} 执行")
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
