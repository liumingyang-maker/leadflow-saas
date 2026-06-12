"""
database.py — 数据库初始化 + 所有 CRUD 操作
==============================================
所有模块通过这里的函数读写数据库，不直接写 SQL。
使用 SQLite（Python 内置，无需安装）。

表结构：
  leads           — 主表：潜在客户
  outreach_log    — 联系记录
  collection_log  — 每次采集的运行日志

使用方式：
    from database import db
    db.init()
    leads = db.get_leads_by_grade("A")
"""

import sqlite3
import json
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Optional
try:
    from log_setup import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "leads.db"


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEFAULT_DB_PATH

    # ─────────────────────────────────────────
    # 连接管理
    # ─────────────────────────────────────────

    @contextmanager
    def get_conn(self):
        """
        上下文管理器，自动提交/回滚/关闭。
        用法：
            with db.get_conn() as conn:
                conn.execute(...)
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row   # 让查询结果支持 row["column"] 访问
        conn.execute("PRAGMA journal_mode=WAL")  # 提升并发写入性能
        conn.execute("PRAGMA foreign_keys=ON")   # 启用外键约束
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败，已回滚: {e}")
            raise
        finally:
            conn.close()

    # ─────────────────────────────────────────
    # 初始化
    # ─────────────────────────────────────────

    def init(self):
        """创建所有表和索引（如果不存在则创建，已存在则跳过）"""
        with self.get_conn() as conn:
            conn.executescript("""
                -- ── 主表：潜在客户 ──────────────────────────────
                CREATE TABLE IF NOT EXISTS leads (
                    id                   TEXT PRIMARY KEY,
                    company_name         TEXT NOT NULL,
                    company_name_norm    TEXT,          -- 标准化名称，用于去重
                    country              TEXT,
                    country_iso          TEXT,          -- ISO 3166-1 alpha-2，如 "NG"
                    region               TEXT,          -- Africa/SEA/LatAm/SouthAsia/Other
                    website              TEXT,
                    email                TEXT,
                    phone                TEXT,
                    contact_name         TEXT,
                    contact_title        TEXT,
                    linkedin_url         TEXT,
                    hs_codes             TEXT,          -- JSON 数组，如 '["8407","8714"]'
                    import_count_6m      INTEGER,       -- 近6个月进口次数
                    last_import_date     TEXT,          -- YYYY-MM 格式
                    estimated_value_usd  REAL,          -- 估计进口金额（美元）
                    sources              TEXT,          -- JSON 数组，如 '["importyeti"]'
                    rule_score           INTEGER,       -- 规则打分（0-100）
                    ai_score_adjustment  INTEGER,       -- AI调整分（-10 到 +10）
                    final_score          INTEGER,       -- 最终分 = rule_score + ai_score_adjustment
                    grade                TEXT,          -- A/B/C/D
                    ai_reasoning         TEXT,          -- AI评估理由
                    recommended_approach TEXT,          -- 建议联系策略
                    risk_flags           TEXT,          -- JSON 数组，风险点
                    status               TEXT DEFAULT 'new',
                                                       -- new/scored/contacted/replied/converted/rejected
                    notes                TEXT,          -- 人工备注
                    created_at           TEXT,
                    updated_at           TEXT,
                    contacted_at         TEXT,
                    replied_at           TEXT,
                    converted_at         TEXT
                );

                -- ── 联系记录表 ────────────────────────────────────
                CREATE TABLE IF NOT EXISTS outreach_log (
                    id          TEXT PRIMARY KEY,
                    lead_id     TEXT NOT NULL,
                    channel     TEXT NOT NULL,   -- email/linkedin/whatsapp/phone
                    direction   TEXT NOT NULL,   -- sent/received
                    subject     TEXT,
                    content     TEXT,
                    sent_at     TEXT,
                    opened_at   TEXT,            -- 邮件打开时间（如有追踪像素）
                    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
                );

                -- ── 采集运行日志 ──────────────────────────────────
                CREATE TABLE IF NOT EXISTS collection_log (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at           TEXT NOT NULL,
                    source           TEXT NOT NULL,   -- importyeti/linkedin/google
                    query            TEXT,
                    results_count    INTEGER DEFAULT 0,
                    new_leads_count  INTEGER DEFAULT 0,
                    dupes_count      INTEGER DEFAULT 0,
                    errors_count     INTEGER DEFAULT 0,
                    duration_secs    REAL
                );

                -- ── AI 缓存（减少重复调用，降低费用） ────────────
                CREATE TABLE IF NOT EXISTS ai_cache (
                    cache_key   TEXT PRIMARY KEY,
                    purpose     TEXT,
                    result_json TEXT,
                    created_at  TEXT,
                    expires_at  TEXT,
                    hit_count   INTEGER DEFAULT 0,
                    last_hit_at TEXT
                );

                -- ── AI 费用追踪 ──────────────────────────────────
                CREATE TABLE IF NOT EXISTS ai_usage (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    model           TEXT NOT NULL,
                    purpose         TEXT,
                    input_tokens    INTEGER DEFAULT 0,
                    output_tokens   INTEGER DEFAULT 0,
                    cost_usd        REAL DEFAULT 0,
                    cache_hit       INTEGER DEFAULT 0
                );

                -- ── 索引（加速常用查询） ─────────────────────────
                CREATE INDEX IF NOT EXISTS idx_leads_grade
                    ON leads(grade);
                CREATE INDEX IF NOT EXISTS idx_leads_status
                    ON leads(status);
                CREATE INDEX IF NOT EXISTS idx_leads_country
                    ON leads(country);
                CREATE INDEX IF NOT EXISTS idx_leads_score
                    ON leads(final_score DESC);
                CREATE INDEX IF NOT EXISTS idx_leads_norm
                    ON leads(company_name_norm);
                CREATE INDEX IF NOT EXISTS idx_outreach_lead
                    ON outreach_log(lead_id);
            """)
        logger.info(f"数据库初始化完成: {self.db_path}")

    # ─────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _to_json(value) -> Optional[str]:
        """列表/字典 → JSON字符串，None保持None"""
        if value is None:
            return None
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _from_json(value: Optional[str]):
        """JSON字符串 → Python对象"""
        if value is None:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        """sqlite3.Row → 普通dict，JSON字段自动解析"""
        if row is None:
            return None
        d = dict(row)
        for json_field in ("hs_codes", "sources", "risk_flags"):
            if json_field in d:
                d[json_field] = Database._from_json(d[json_field])
        return d

    # ─────────────────────────────────────────
    # leads 表 — 写入
    # ─────────────────────────────────────────

    def insert_lead(self, lead: dict) -> str:
        """
        插入一条新 lead。
        如果 lead 没有 id，自动生成。
        返回 lead 的 id。
        """
        lead_id = lead.get("id") or self._new_id()
        now = self._now()

        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO leads (
                    id, company_name, company_name_norm, country, country_iso,
                    region, website, email, phone, contact_name, contact_title,
                    linkedin_url, hs_codes, import_count_6m, last_import_date,
                    estimated_value_usd, sources, rule_score, ai_score_adjustment,
                    final_score, grade, ai_reasoning, recommended_approach,
                    risk_flags, status, notes, created_at, updated_at
                ) VALUES (
                    :id, :company_name, :company_name_norm, :country, :country_iso,
                    :region, :website, :email, :phone, :contact_name, :contact_title,
                    :linkedin_url, :hs_codes, :import_count_6m, :last_import_date,
                    :estimated_value_usd, :sources, :rule_score, :ai_score_adjustment,
                    :final_score, :grade, :ai_reasoning, :recommended_approach,
                    :risk_flags, :status, :notes, :created_at, :updated_at
                )
            """, {
                "id": lead_id,
                "company_name": lead.get("company_name", ""),
                "company_name_norm": lead.get("company_name_norm"),
                "country": lead.get("country"),
                "country_iso": lead.get("country_iso"),
                "region": lead.get("region"),
                "website": lead.get("website"),
                "email": lead.get("email"),
                "phone": lead.get("phone"),
                "contact_name": lead.get("contact_name"),
                "contact_title": lead.get("contact_title"),
                "linkedin_url": lead.get("linkedin_url"),
                "hs_codes": self._to_json(lead.get("hs_codes")),
                "import_count_6m": lead.get("import_count_6m"),
                "last_import_date": lead.get("last_import_date"),
                "estimated_value_usd": lead.get("estimated_value_usd"),
                "sources": self._to_json(lead.get("sources", [])),
                "rule_score": lead.get("rule_score"),
                "ai_score_adjustment": lead.get("ai_score_adjustment"),
                "final_score": lead.get("final_score"),
                "grade": lead.get("grade"),
                "ai_reasoning": lead.get("ai_reasoning"),
                "recommended_approach": lead.get("recommended_approach"),
                "risk_flags": self._to_json(lead.get("risk_flags")),
                "status": lead.get("status", "new"),
                "notes": lead.get("notes"),
                "created_at": now,
                "updated_at": now,
            })
        return lead_id

    def bulk_insert_leads(self, leads: list[dict]) -> tuple[int, int]:
        """
        批量插入，跳过已存在（根据 company_name_norm + country 判重）。
        返回 (插入数, 跳过数)
        """
        inserted = 0
        skipped = 0
        for lead in leads:
            norm = lead.get("company_name_norm", "")
            country = lead.get("country", "")
            if self.lead_exists(norm, country):
                skipped += 1
                logger.debug(f"跳过重复: {lead.get('company_name')} ({country})")
            else:
                self.insert_lead(lead)
                inserted += 1
        logger.info(f"批量插入完成: 新增 {inserted} 条，跳过重复 {skipped} 条")
        return inserted, skipped

    def update_lead(self, lead_id: str, fields: dict) -> bool:
        """
        更新指定字段。fields 是要更新的字段字典。
        自动更新 updated_at。
        返回是否成功。
        """
        if not fields:
            return False

        fields["updated_at"] = self._now()

        # 处理JSON字段
        for json_field in ("hs_codes", "sources", "risk_flags"):
            if json_field in fields:
                fields[json_field] = self._to_json(fields[json_field])

        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        fields["lead_id"] = lead_id

        with self.get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE leads SET {set_clause} WHERE id = :lead_id",
                fields
            )
        return cursor.rowcount > 0

    def update_lead_score(self, lead_id: str, rule_score: int,
                          ai_adjustment: int = 0, ai_reasoning: str = "",
                          recommended_approach: str = "", risk_flags: list = None) -> None:
        """专门用于更新评分结果"""
        final_score = rule_score + ai_adjustment
        grade = self._calc_grade(final_score)
        self.update_lead(lead_id, {
            "rule_score": rule_score,
            "ai_score_adjustment": ai_adjustment,
            "final_score": final_score,
            "grade": grade,
            "ai_reasoning": ai_reasoning,
            "recommended_approach": recommended_approach,
            "risk_flags": risk_flags or [],
            "status": "scored",
        })

    def update_lead_status(self, lead_id: str, status: str, notes: str = None) -> None:
        """更新状态，自动记录时间戳"""
        fields = {"status": status}
        if notes:
            fields["notes"] = notes
        now = self._now()
        if status == "contacted":
            fields["contacted_at"] = now
        elif status == "replied":
            fields["replied_at"] = now
        elif status == "converted":
            fields["converted_at"] = now
        self.update_lead(lead_id, fields)

    @staticmethod
    def _calc_grade(score: int) -> str:
        if score >= 80:
            return "A"
        elif score >= 60:
            return "B"
        elif score >= 40:
            return "C"
        return "D"

    # ─────────────────────────────────────────
    # leads 表 — 查询
    # ─────────────────────────────────────────

    def lead_exists(self, company_name_norm: str, country: str) -> bool:
        """根据标准化名称+国家判断是否已存在"""
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE company_name_norm = ? AND country = ? LIMIT 1",
                (company_name_norm, country)
            ).fetchone()
        return row is not None

    def get_lead(self, lead_id: str) -> Optional[dict]:
        """按ID获取单条lead"""
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
        return self._row_to_dict(row)

    def get_leads_by_grade(self, grade: str, status: str = None,
                           limit: int = 100, offset: int = 0) -> list[dict]:
        """
        按等级查询leads，可选过滤状态。
        按 final_score 降序排列。
        """
        sql = "SELECT * FROM leads WHERE grade = ?"
        params = [grade]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY final_score DESC LIMIT ? OFFSET ?"
        params += [limit, offset]

        with self.get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_leads_for_scoring(self) -> list[dict]:
        """获取所有待评分的leads（status = 'new'）"""
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM leads WHERE status = 'new' ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_leads_for_outreach(self, grade: str = "A") -> list[dict]:
        """获取待联系的leads（status = 'scored' 且等级匹配）"""
        with self.get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM leads
                   WHERE grade = ? AND status = 'scored'
                   AND email IS NOT NULL AND email != ''
                   ORDER BY final_score DESC""",
                (grade,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search_leads(self, keyword: str = None, country: str = None,
                     grade: str = None, status: str = None,
                     min_score: int = None, source: str = None,
                     notes_like: str = None,
                     limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """
        多条件搜索，返回 (结果列表, 总数)。
        用于看板的列表页。
        """
        conditions = []
        params = []

        if keyword:
            conditions.append("(company_name LIKE ? OR contact_name LIKE ? OR email LIKE ?)")
            kw = f"%{keyword}%"
            params += [kw, kw, kw]
        if country:
            conditions.append("country = ?")
            params.append(country)
        if grade:
            conditions.append("grade = ?")
            params.append(grade)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if min_score is not None:
            conditions.append("final_score >= ?")
            params.append(min_score)
        if source:
            # sources 列存的是 JSON 串如 '["competitor_radar"]'，按 token 模糊匹配
            conditions.append("sources LIKE ?")
            params.append(f'%"{source}"%')
        if notes_like:
            conditions.append("notes LIKE ?")
            params.append(f"%{notes_like}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self.get_conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM leads {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM leads {where} ORDER BY final_score DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()

        return [self._row_to_dict(r) for r in rows], total

    def get_stats(self) -> dict:
        """
        返回看板统计数据。
        """
        with self.get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            by_grade = dict(conn.execute(
                "SELECT grade, COUNT(*) FROM leads WHERE grade IS NOT NULL GROUP BY grade"
            ).fetchall())
            by_status = dict(conn.execute(
                "SELECT status, COUNT(*) FROM leads GROUP BY status"
            ).fetchall())
            by_country = conn.execute(
                """SELECT country, COUNT(*) as cnt FROM leads
                   GROUP BY country ORDER BY cnt DESC LIMIT 10"""
            ).fetchall()
            contacted = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE status IN ('contacted','replied','converted')"
            ).fetchone()[0]
            replied = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE status IN ('replied','converted')"
            ).fetchone()[0]
            converted = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE status = 'converted'"
            ).fetchone()[0]

        reply_rate = round(replied / contacted * 100, 1) if contacted > 0 else 0

        return {
            "total": total,
            "by_grade": by_grade,
            "by_status": by_status,
            "top_countries": [{"country": r[0], "count": r[1]} for r in by_country],
            "contacted": contacted,
            "replied": replied,
            "converted": converted,
            "reply_rate": reply_rate,
        }

    def get_all_countries(self) -> list[str]:
        """获取数据库中所有国家（用于看板过滤下拉框）"""
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT country FROM leads WHERE country IS NOT NULL ORDER BY country"
            ).fetchall()
        return [r[0] for r in rows]

    # ─────────────────────────────────────────
    # outreach_log 表
    # ─────────────────────────────────────────

    def log_outreach(self, lead_id: str, channel: str, direction: str,
                     subject: str = None, content: str = None) -> str:
        """记录一次联系行为"""
        log_id = self._new_id()
        now = self._now()
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO outreach_log
                    (id, lead_id, channel, direction, subject, content, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (log_id, lead_id, channel, direction, subject, content, now))
        return log_id

    def get_outreach_history(self, lead_id: str) -> list[dict]:
        """获取某个lead的所有联系记录"""
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM outreach_log WHERE lead_id = ? ORDER BY sent_at ASC",
                (lead_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ─────────────────────────────────────────
    # collection_log 表
    # ─────────────────────────────────────────

    def log_collection(self, source: str, query: str, results_count: int,
                       new_leads: int, dupes: int, errors: int,
                       duration_secs: float) -> None:
        """记录一次采集运行"""
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO collection_log
                    (run_at, source, query, results_count, new_leads_count,
                     dupes_count, errors_count, duration_secs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (self._now(), source, query, results_count,
                  new_leads, dupes, errors, duration_secs))

    def get_collection_history(self, limit: int = 20) -> list[dict]:
        """获取最近N次采集记录"""
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ─────────────────────────────────────────
    # 数据维护
    # ─────────────────────────────────────────

    def delete_lead(self, lead_id: str) -> bool:
        """删除lead（同时级联删除outreach_log）"""
        with self.get_conn() as conn:
            cursor = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        return cursor.rowcount > 0

    def export_to_csv(self, filepath: str, grade: str = None,
                      status: str = None) -> int:
        """
        导出leads到CSV文件。
        返回导出条数。
        """
        import csv

        conditions = []
        params = []
        if grade:
            conditions.append("grade = ?")
            params.append(grade)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self.get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM leads {where} ORDER BY final_score DESC", params
            ).fetchall()

        if not rows:
            return 0

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

        logger.info(f"导出 {len(rows)} 条leads到 {filepath}")
        return len(rows)

    # ─────────────────────────────────────────
    # AI 缓存
    # ─────────────────────────────────────────

    def ai_cache_get(self, cache_key: str) -> Optional[str]:
        """读取缓存，未命中或过期返回 None"""
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT result_json, expires_at FROM ai_cache WHERE cache_key=?",
                (cache_key,)
            ).fetchone()
        if not row:
            return None
        expires_at = row["expires_at"] or ""
        if expires_at and expires_at < self._now():
            return None
        # 更新命中次数
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE ai_cache SET hit_count=hit_count+1, last_hit_at=? WHERE cache_key=?",
                (self._now(), cache_key)
            )
        return row["result_json"]

    def ai_cache_set(self, cache_key: str, purpose: str,
                     result_json: str, ttl_days: int = 30):
        """写入缓存"""
        from datetime import timedelta
        expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)
                   ).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO ai_cache
                (cache_key, purpose, result_json, created_at, expires_at, hit_count)
                VALUES (?,?,?,?,?,0)
            """, (cache_key, purpose, result_json, self._now(), expires))

    def ai_cache_stats(self) -> dict:
        """返回缓存统计"""
        with self.get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0]
            hits  = conn.execute("SELECT SUM(hit_count) FROM ai_cache").fetchone()[0] or 0
        return {"cached_entries": total, "total_hits": hits}

    # ─────────────────────────────────────────
    # AI 费用追踪
    # ─────────────────────────────────────────

    def ai_log_usage(self, model: str, purpose: str,
                     input_tokens: int, output_tokens: int,
                     cost_usd: float, cache_hit: bool = False):
        with self.get_conn() as conn:
            conn.execute("""
                INSERT INTO ai_usage
                (timestamp, model, purpose, input_tokens, output_tokens, cost_usd, cache_hit)
                VALUES (?,?,?,?,?,?,?)
            """, (self._now(), model, purpose,
                  input_tokens, output_tokens, cost_usd, int(cache_hit)))

    def ai_usage_stats(self, days: int = 30) -> dict:
        """返回最近 N 天的费用统计"""
        cutoff = (datetime.now(timezone.utc).replace(day=1)
                  ).strftime("%Y-%m-%d 00:00:00")
        with self.get_conn() as conn:
            rows = conn.execute("""
                SELECT model,
                       SUM(cost_usd) as cost,
                       SUM(input_tokens) as inp,
                       SUM(output_tokens) as out,
                       SUM(cache_hit) as hits,
                       COUNT(*) as calls
                FROM ai_usage WHERE timestamp >= ?
                GROUP BY model
            """, (cutoff,)).fetchall()
        result = {}
        for r in rows:
            result[r["model"]] = {
                "cost_usd": round(r["cost"] or 0, 4),
                "cost_cny": round((r["cost"] or 0) * 7.2, 2),
                "calls": r["calls"],
                "cache_hits": r["hits"] or 0,
                "input_tokens": r["inp"] or 0,
                "output_tokens": r["out"] or 0,
            }
        return result


# 单例
db = Database()


# ─────────────────────────────────────────
# 直接运行此文件 = 初始化数据库并做基础测试
# ─────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    print("=" * 50)
    print("初始化数据库...")
    db.init()

    print("\n插入测试数据...")
    test_lead = {
        "company_name": "ABC Motors Nigeria",
        "company_name_norm": "abc motors nigeria",
        "country": "Nigeria",
        "country_iso": "NG",
        "region": "Africa",
        "website": "https://abcmotors.ng",
        "email": "purchase@abcmotors.ng",
        "contact_name": "John Okafor",
        "contact_title": "Purchasing Manager",
        "hs_codes": ["8407", "8714"],
        "import_count_6m": 8,
        "last_import_date": "2024-11",
        "estimated_value_usd": 45000,
        "sources": ["importyeti"],
    }
    lead_id = db.insert_lead(test_lead)
    print(f"插入成功，ID: {lead_id}")

    print("\n查询测试...")
    lead = db.get_lead(lead_id)
    print(f"公司: {lead['company_name']}")
    print(f"HS编码: {lead['hs_codes']}")  # 应为 list，不是字符串
    print(f"来源: {lead['sources']}")

    print("\n重复插入测试（应被跳过）...")
    inserted, skipped = db.bulk_insert_leads([test_lead])
    print(f"插入: {inserted}, 跳过: {skipped}")  # 应为 0, 1

    print("\n更新评分...")
    db.update_lead_score(lead_id, rule_score=75, ai_adjustment=5,
                         ai_reasoning="高频进口商，产品匹配")
    lead = db.get_lead(lead_id)
    print(f"最终分: {lead['final_score']}, 等级: {lead['grade']}")  # 80, A

    print("\n统计数据...")
    stats = db.get_stats()
    print(f"总数: {stats['total']}, 按等级: {stats['by_grade']}")

    print("\n✅ 数据库测试全部通过")
    print("=" * 50)
