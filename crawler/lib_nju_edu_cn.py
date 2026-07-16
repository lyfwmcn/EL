"""
南京大学新闻爬虫 (NJU News Scraper)
爬取网站: https://lib.nju.edu.cn
爬取内容: 综合新闻、校园动态、媒体传真、科技动态、社科动态等各类新闻
存储方式: MySQL (主) + CSV (备份)
运行模式: 定时每日运行 / 手动单次运行 (python3.11 爬虫代码.py --once)
"""

import csv
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

# 新闻分类
NEWS_CATEGORIES = {
    "新闻通知": "xw/xwtz.htm",
}

BASE_URL = "https://lib.nju.edu.cn"
OUTPUT_FILE = "lib_nju_news.csv"
REQUEST_DELAY = 0.5
MAX_PAGES_PER_CATEGORY = 5
MAX_RECORDS = 400
REQUEST_TIMEOUT = 15

# MySQL 配置（部署在云服务器上时使用 localhost）
MYSQL_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "ZCW",
    "password": "SiKongZhen@2026",
    "database": "nju_news_db",
    "charset": "utf8mb4",
}

# 定时任务配置
SCHEDULE_TIME = "08:00"  # 每天固定时间执行 (24小时制)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ========== MySQL 数据库操作 ==========

def get_db_connection() -> pymysql.Connection | None:
    """获取 MySQL 数据库连接"""
    try:
        conn = pymysql.connect(**MYSQL_CONFIG, connect_timeout=5)
        return conn
    except pymysql.Error as e:
        print(f"⚠️ MySQL 连接失败: {e}")
        return None


def init_database():
    """初始化数据库和表（幂等操作）"""
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS lib_nju_edu_cn_news ("
                "  id INT AUTO_INCREMENT PRIMARY KEY,"
                "  scrape_date DATETIME NOT NULL,"
                "  category VARCHAR(50) NOT NULL,"
                "  news_date VARCHAR(20) DEFAULT '',"
                "  title VARCHAR(500) NOT NULL,"
                "  url VARCHAR(500) NOT NULL ,"
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
    """批量写入 MySQL，按 url 去重，返回新增条数"""
    if not items:
        return 0

    conn = get_db_connection()
    if conn is None:
        return 0

    scrape_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0

    sql = (
        "INSERT IGNORE INTO lib_nju_edu_cn_news (scrape_date, category, news_date, title, url) "
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
    """删除超出上限的旧数据，保留最新的 max_rows 条，返回删除条数"""
    conn = get_db_connection()
    if conn is None:
        return 0

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM lib_nju_edu_cn_news")
            total = cursor.fetchone()[0]
            if total <= max_rows:
                print(f"✅ 数据量 {total}/{max_rows}，无需清理")
                return 0

            delete_count = total - max_rows
            cursor.execute(
                "DELETE FROM lib_nju_edu_cn_news WHERE id NOT IN ("
                "  SELECT id FROM ("
                "    SELECT id FROM lib_nju_edu_cn_news ORDER BY id DESC LIMIT %s"
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


# ========== 工具函数 ==========

def clean_title(title: str) -> str:
    """清理标题：去除空白、移除「置顶」等前缀标签"""
    title = title.strip()
    for prefix in ("置顶", "推荐", "头条"):
        if title.startswith(prefix):
            title = title[len(prefix):]
    return re.sub(r"\s+", " ", title)


def extract_page_count(soup: BeautifulSoup) -> int:
    """从分页器中提取总页数（从页面链接的显示文本中获取）"""
    page_div = soup.find("div", class_="page")
    if not page_div:
        return 1
    max_pages = 1
    for span in page_div.find_all("span", class_="p_no"):
        a = span.find("a")
        if a and a.text.strip().isdigit():
            max_pages = max(max_pages, int(a.text.strip()))
    return max_pages


def parse_news_list(soup: BeautifulSoup, category: str, page_url: str = "") -> list[dict]:
    items = []
    container = soup.find("div", class_="gqzx-list")
    if not container:
        return items
    for a in container.find_all("a", href=True):
        href = a.get("href", "").strip()
        if "/info/" not in href:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        full_url = urljoin(page_url or BASE_URL, href)
        items.append({"category": category, "date": "", "title": title, "url": full_url})
    return items

    news_list_ul = kxyj_div.find("ul", class_="flex")
    if not news_list_ul:
        return items

    for li in news_list_ul.find_all("li"):
        a_tag = li.find("a")
        if not a_tag:
            continue

        href = a_tag.get("href", "").strip()
        if not href:
            continue

        date_div = li.find("div", class_="kxdt-l")
        day = ""
        year = ""
        if date_div:
            day_p = date_div.find("p")
            year_span = date_div.find("span")
            if day_p:
                day = day_p.text.strip()
            if year_span:
                year = year_span.text.strip()

        title_div = li.find("div", class_="kxdt-r")
        title = ""
        if title_div:
            h3 = title_div.find("h3")
            if h3:
                title = h3.get_text(strip=True)
                for i_tag in h3.find_all("i"):
                    title = title.replace(i_tag.text, "")

        title = clean_title(title)

        if title and href:
            full_url = urljoin(page_url or BASE_URL, href)
            news_date = f"{year}-{day}" if year and day else ""

            items.append({
                "category": category,
                "date": news_date,
                "title": title,
                "url": full_url,
            })

    return items


def fetch_page(url: str, retries: int = 3) -> BeautifulSoup | None:
    """获取网页并返回BeautifulSoup对象"""
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


# ========== 爬取逻辑 ==========

def scrape_category(
    category: str,
    list_path: str,
    max_pages: int | None = MAX_PAGES_PER_CATEGORY,
) -> list[dict]:
    """爬取单个新闻分类"""
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

    dir_path = os.path.dirname(list_path)
    base_name = os.path.basename(list_path).replace(".htm", "")

    for page_idx in range(2, pages_to_scrape + 1):
        page_num = total_pages - page_idx + 1
        page_url = f"{BASE_URL}/{dir_path}/{base_name}/{page_num}.htm"

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


def save_to_csv(items: list[dict], filepath: str):
    """将结果保存为CSV文件（备份）"""
    scrape_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["爬取日期", "新闻分类", "新闻日期", "标题", "链接"])
        for item in items:
            writer.writerow([scrape_date, item["category"], item["date"], item["title"], item["url"]])

    print(f"✅ CSV 备份已保存: {filepath} ({len(items)} 条)")


# ========== 主流程 ==========

def run_once():
    """执行一次完整的爬取任务"""
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"🚀 开始爬取 {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 初始化数据库
    init_database()

    all_news = []
    for category, list_path in NEWS_CATEGORIES.items():
        news_items = scrape_category(category, list_path)
        all_news.extend(news_items)

    if not all_news:
        print("❌ 未爬取到任何新闻数据")
        return

    # 分类统计
    stats = Counter(item["category"] for item in all_news)
    print(f"\n📊 各分类爬取统计:")
    for cat, count in stats.items():
        print(f"   {cat}: {count} 条")

    # 写入 MySQL（主存储）
    inserted = save_to_mysql(all_news)

    # 清理超出上限的旧数据
    cleanup_old_records(MAX_RECORDS)
    
    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n⏱️  总耗时: {elapsed:.1f} 秒")
    print(f"{'='*60}\n")


def main():
    """入口：根据参数选择单次运行或定时模式"""
    if "--once" in sys.argv:
        run_once()
        return

    print(f"⏰ 定时模式已启动，每天 {SCHEDULE_TIME} 执行爬取任务")
    print("   按 Ctrl+C 停止，或用 python3.11 爬虫代码.py --once 手动执行一次")
    print(f"   日志输出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    schedule.every().day.at(SCHEDULE_TIME).do(run_once)

    # 首次启动时立即执行一次
    run_once()

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n🛑 定时任务已停止")


if __name__ == "__main__":
    main()
