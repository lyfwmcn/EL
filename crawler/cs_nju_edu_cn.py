"""
南京大学计算机学院新闻/通知爬虫 (CS NJU Scraper)
爬取网站: https://cs.nju.edu.cn
爬取内容: 新闻动态、院内公告、研究生/本科生公告栏、获奖公告、会议讲座、就业信息、通知公告
存储方式: MySQL
运行模式: 定时每日运行 / 手动单次运行 (python3.11 cs_nju_edu_cn.py --once)
"""

import os
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

# 爬取分类: {分类名: 列表页路径}
NEWS_CATEGORIES = {
    "新闻动态":     "1660/list.htm",
    "院内公告":     "1702/list.htm",
    "研究生公告栏": "1703/list.htm",
    "本科生公告栏": "1704/list.htm",
    "获奖公告":     "1705/list.htm",
    "会议讲座信息": "1706/list.htm",
    "就业信息栏":   "1707/list.htm",
    "通知公告":     "1712/list.htm",
}

BASE_URL = "https://cs.nju.edu.cn"
OUTPUT_FILE = "cs_nju_news.csv"
REQUEST_DELAY = 0.5
MAX_PAGES_PER_CATEGORY = 3
MAX_RECORDS = 400
REQUEST_TIMEOUT = 15

# MySQL 配置
MYSQL_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "ZCW",
    "password": "SiKongZhen@2026",
    "database": "nju_news_db",
    "charset": "utf8mb4",
}

TABLE_NAME = "cs_nju_edu_cn_news"
SCHEDULE_TIME = "08:00"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
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

def extract_page_count(soup: BeautifulSoup) -> int:
    """从尾页链接提取总页数"""
    max_pages = 1
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        match = re.search(r"list(\d+)\.htm", href)
        if match and text == "尾页":
            return int(match.group(1))
    # 备选：扫描所有页码链接
    for a in soup.find_all("a", href=True):
        match = re.search(r"list(\d+)\.htm", a["href"])
        if match:
            max_pages = max(max_pages, int(match.group(1)))
    return max_pages


def parse_news_list(soup: BeautifulSoup, category: str, page_url: str = "") -> list[dict]:
    """解析列表页，提取新闻条目"""
    items = []

    # 找所有新闻条目
    for news_item in soup.find_all(class_="news"):
        # 找标题和链接
        title_elem = news_item.find(class_="news_title")
        if not title_elem:
            continue

        a_tag = title_elem.find("a") or news_item.find("a", href=True)
        if not a_tag:
            continue

        href = a_tag.get("href", "").strip()
        title = a_tag.get("title", "").strip() or title_elem.get_text(strip=True)
        if not href or not title:
            continue

        # 找日期 (class="news_meta")
        meta_elem = news_item.find(class_="news_meta")
        news_date = meta_elem.get_text(strip=True) if meta_elem else ""

        full_url = urljoin(page_url or BASE_URL, href)

        items.append({
            "category": category,
            "date": news_date,
            "title": title,
            "url": full_url,
        })

    return items


def fetch_page(url: str, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException:
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 2)
                continue
            return None
    return None


def scrape_category(
    category: str,
    list_path: str,
    max_pages: int | None = MAX_PAGES_PER_CATEGORY,
) -> list[dict]:
    all_items = []
    base_url = f"{BASE_URL}/{list_path}"

    page1_soup = fetch_page(base_url)
    if page1_soup is None:
        print(f"  ⚠️ [{category}] 首页获取失败")
        return all_items

    items = parse_news_list(page1_soup, category, base_url)
    all_items.extend(items)

    total_pages = extract_page_count(page1_soup)
    pages_to_scrape = total_pages if max_pages is None else min(total_pages, max_pages)
    print(f"  📰 [{category}] 首页 {len(items)} 条，共 {total_pages} 页，计划爬取 {pages_to_scrape} 页")

    if pages_to_scrape <= 1:
        return all_items

    # 分页: /1660/list2.htm, /1660/list3.htm ...
    dir_name = os.path.dirname(list_path)

    for page_idx in range(2, pages_to_scrape + 1):
        page_url = f"{BASE_URL}/{dir_name}/list{page_idx}.htm"

        try:
            soup = fetch_page(page_url)
            if soup is None:
                continue
            page_items = parse_news_list(soup, category, page_url)
            if not page_items:
                break
            all_items.extend(page_items)
            print(f"    → 第 {page_idx} 页: {len(page_items)} 条")
        except Exception:
            continue

        time.sleep(REQUEST_DELAY)

    print(f"  ✅ [{category}] 共爬取 {len(all_items)} 条")
    return all_items


# ========== 主流程 ==========

def run_once():
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"🚀 CS新闻爬虫启动 {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    init_database()

    all_news = []
    for category, list_path in NEWS_CATEGORIES.items():
        news_items = scrape_category(category, list_path)
        all_news.extend(news_items)

    if not all_news:
        print("❌ 未爬取到任何数据")
        return

    stats = Counter(item["category"] for item in all_news)
    print(f"\n📊 各分类爬取统计:")
    for cat, count in stats.items():
        print(f"   {cat}: {count} 条")

    save_to_mysql(all_news)
    cleanup_old_records(MAX_RECORDS)

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n⏱️  总耗时: {elapsed:.1f} 秒")
    print(f"{'='*60}\n")


def main():
    if "--once" in sys.argv:
        run_once()
        return

    print(f"⏰ CS定时模式启动，每天 {SCHEDULE_TIME} 执行")
    print(f"   按 Ctrl+C 停止，或用 python3.11 cs_nju_edu_cn.py --once 手动执行\n")

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
