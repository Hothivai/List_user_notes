from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import mysql.connector

from services.db_connect import get_connection
from datetime import datetime, timezone, timedelta


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ENV = "CONFIGURED_REBALANCER_CONFIG"
DEFAULT_CONFIG = "my_rebalance_config.json"
TX_EXPLORERS = {
    "BNB": "https://bscscan.com/tx/",
    "BAS": "https://basescan.org/tx/",
    "ETH": "https://etherscan.io/tx/",
    "ARB": "https://arbiscan.io/tx/",
    "LIN": "https://lineascan.build/tx/",
    "POL": "https://zkevm.polygonscan.com/tx/",
    "MON": "https://monadvision.com/tx/",
}

RISKY_STATUSES = {
    "PLANNED",
    "WITHDRAWN_UNBURNED",
    "SWAP_PENDING",
    "SWAP_BLOCKED",
    "MINTED_UNSTAKED",
    "RECOVERY_REQUIRED",
}
COMPLETED_STATUSES = {"REMINTED", "BURNED"}

UTC_PLUS_7 = timezone(timedelta(hours=7))

def _to_utc7_display(value):
    if not value:
        return None
    
    if isinstance(value, datetime):
        dt = value
    else:
        return value
    
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(UTC_PLUS_7).strftime("%Y-%m-%d %H:%M:%S")

def get_configured_rebalancer_context() -> dict[str, Any]:
    jobs, journal_available, journal_error = _fetch_rebalance_jobs(None)
    pool_info_by_key = _fetch_pool_info(_pool_keys_from_jobs(jobs))
    pool_summaries = _build_pool_wallet_summaries(jobs, pool_info_by_key)
    pool_aggregate_summaries = _build_pool_aggregate_summaries(jobs, pool_info_by_key)
    for job in jobs:
        summary = pool_aggregate_summaries.get(job["pool_key"])
        if summary:
            job["pool_name"] = summary["pool_name"]
    latest_jobs = _latest_jobs_by_pool_wallet(jobs)

    return {
        "pool_summaries": list(pool_summaries.values()),
        "pool_aggregate_summaries": list(pool_aggregate_summaries.values()),
        "jobs": jobs,
        "latest_jobs": latest_jobs,
        "journal_available": journal_available,
        "journal_error": journal_error,
        "risky_statuses": sorted(RISKY_STATUSES),
        "completed_statuses": sorted(COMPLETED_STATUSES),
    }


def enrich_nfts_with_configured_rebalancer(nft_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not nft_rows:
        return nft_rows

    jobs, _, _ = _fetch_rebalance_jobs(None)
    jobs_by_position = _jobs_by_position(jobs)
    risky_jobs_by_pool_wallet = _risky_jobs_by_pool_wallet(jobs)
    visible_counts_by_pool_wallet: dict[tuple[str, str, str], int] = {}
    for nft in nft_rows:
        key = (
            str(nft.get("chain") or "").upper(),
            _lower(nft.get("pool_address")),
            _lower(nft.get("wallet_address")),
        )
        visible_counts_by_pool_wallet[key] = visible_counts_by_pool_wallet.get(key, 0) + 1

    for nft in nft_rows:
        chain = str(nft.get("chain") or "").upper()
        pool_address = _lower(nft.get("pool_address"))
        wallet = _lower(nft.get("wallet_address"))
        nft_id = str(nft.get("nft_id") or "")
        job = jobs_by_position.get((chain, pool_address, wallet, nft_id))
        match_source = "token"
        if not job:
            pool_wallet_key = (chain, pool_address, wallet)
            if visible_counts_by_pool_wallet.get(pool_wallet_key) == 1:
                job = risky_jobs_by_pool_wallet.get(pool_wallet_key)
                match_source = "pool_wallet"

        if not job:
            nft["configured_rebalancer"] = None
            continue

        is_risky = bool(job.get("is_effective_risky"))
        nft["configured_rebalancer"] = {
            "is_configured_pool": True,
            "is_risky": is_risky,
            "pool_name": job.get("pool_name"),
            "status": job.get("status") if match_source == "token" else job.get("status"),
            "old_token_id": job.get("old_token_id"),
            "new_token_id": job.get("new_token_id"),
            "updated_at": job.get("updated_at"),
            "error_reason": job.get("last_recovery_error") or job.get("error_reason"),
            "badge_class": "rebalancer-badge-danger" if is_risky else "rebalancer-badge-info",
            "match_source": match_source,
        }
    return nft_rows


def enrich_pools_with_configured_rebalancer(pool_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    jobs, _, _ = _fetch_rebalance_jobs(None)
    pool_info_by_key = _fetch_pool_info(_pool_keys_from_jobs(jobs))
    pool_summaries = _build_pool_aggregate_summaries(jobs, pool_info_by_key)

    pools_with_jobs = 0
    active_pools = 0
    for pool in pool_rows:
        key = (str(pool.get("chain") or "").upper(), _lower(pool.get("pool_address")))
        summary = pool_summaries.get(key)
        if not summary:
            pool["configured_rebalancer"] = None
            continue
        pools_with_jobs += 1
        if summary["has_risky_job"]:
            active_pools += 1
        pool["configured_rebalancer"] = {
            "pool_name": summary["pool_name"],
            "range_label": summary["range_label"] if not summary["is_multi_wallet"] else None,
            "latest_status": summary["latest_status"] if not summary["is_multi_wallet"] else None,
            "latest_updated_at": summary["latest_updated_at"],
            "active_job_count": summary["active_job_count"],
            "active_wallet_count": summary["active_wallet_count"],
            "completed_job_count": summary["completed_job_count"],
            "total_job_count": summary["total_job_count"],
            "has_risky_job": summary["has_risky_job"],
            "wallet_count": summary["wallet_count"],
            "is_multi_wallet": summary["is_multi_wallet"],
        }

    return pool_rows, {
        "pools_with_jobs": pools_with_jobs,
        "active_pools": active_pools,
        "total_job_pools": len(pool_summaries),
        "total_jobs": len(jobs),
    }


def load_configured_rebalancer_config() -> dict[str, Any]:
    config_path = _resolve_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw_config = json.load(fh)
    except Exception as exc:
        return {
            "config_path": str(config_path),
            "config_error": str(exc),
            "dry_run": None,
            "pools": [],
        }

    pool_defaults = _dict_or_empty(raw_config.get("pool_defaults"))
    wallets = _dict_or_empty(raw_config.get("wallets"))
    dry_run = bool(raw_config.get("dry_run", True))
    pools = []
    for raw_pool in raw_config.get("pools", []) or []:
        if not isinstance(raw_pool, dict):
            continue
        pool = {**pool_defaults, **raw_pool}
        wallet_alias = pool.pop("wallet", None)
        if wallet_alias and wallet_alias in wallets and isinstance(wallets[wallet_alias], dict):
            pool = {**wallets[wallet_alias], **pool}
        if pool.get("bot_wallet") and not pool.get("managed_wallets"):
            pool["managed_wallets"] = [pool["bot_wallet"]]

        chain = str(pool.get("chain") or "").upper()
        pool_address = str(pool.get("pool_address") or "")
        managed_wallets = [str(wallet) for wallet in pool.get("managed_wallets", []) if wallet]
        range_config = _dict_or_empty(pool.get("rebalance_range"))
        range_label = "No range"
        range_title = "No range recorded"
        range_detail = "no config or journal percent yet"
        range_source = "none"
        range_lower_percent = None
        range_upper_percent = None
        if range_config.get("mode") == "price_percent":
            range_source = "config"
            range_title = "Config range"
            range_lower_percent = range_config.get("lower_percent")
            range_upper_percent = range_config.get("upper_percent")
            range_label = _range_label(range_lower_percent, range_upper_percent)
            range_detail = f"lower {_format_percent(range_lower_percent)} / upper {_format_percent(range_upper_percent, signed=True)}"

        pools.append(
            {
                "name": pool.get("name") or pool_address,
                "chain": chain,
                "pool_address": pool_address,
                "pool_key": (chain, _lower(pool_address)),
                "dex_type": pool.get("dex_type", "pancake_v3_masterchef"),
                "pid": pool.get("pid"),
                "bot_wallet": str(pool.get("bot_wallet") or ""),
                "managed_wallets": managed_wallets,
                "private_key_env": pool.get("private_key_env", "PARASITE_BOT_PRIVATE_KEY"),
                "dry_run": dry_run,
                "max_jobs_per_cycle": pool.get("max_jobs_per_cycle", 1),
                "execute_burn": bool(pool.get("execute_burn", True)),
                "slippage_bps": pool.get("slippage_bps", 50),
                "range_label": range_label,
                "range_title": range_title,
                "range_detail": range_detail,
                "range_source": range_source,
                "range_lower_percent": range_lower_percent,
                "range_upper_percent": range_upper_percent,
                "rebalance_range": range_config,
                "token0_address": pool.get("token0_address"),
                "token1_address": pool.get("token1_address"),
            }
        )

    return {
        "config_path": str(config_path),
        "config_error": None,
        "dry_run": dry_run,
        "pools": pools,
    }


def _resolve_config_path() -> Path:
    raw_path = os.getenv(CONFIG_ENV) or DEFAULT_CONFIG
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pool_map(pools: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {pool["pool_key"]: pool for pool in pools if pool.get("chain") and pool.get("pool_address")}


def _pool_wallet_map(pools: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    out = {}
    for pool in pools:
        if not pool.get("chain") or not pool.get("pool_address"):
            continue
        wallets = set(pool.get("managed_wallets") or [])
        if pool.get("bot_wallet"):
            wallets.add(pool["bot_wallet"])
        for wallet in wallets:
            if wallet:
                out[(pool["chain"], _lower(pool["pool_address"]), _lower(wallet))] = pool
    return out


def _fetch_pool_info(pool_map: dict[tuple[str, str], dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    if not pool_map:
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        for chain, pool_address in pool_map:
            cursor.execute(
                """
                SELECT chain, pool_address, token0_symbol, token1_symbol, token0_address, token1_address,
                       fee, alloc_point, pid, cake_per_day, total_value_lock, total_current_liquidity,
                       total_staked_liquidity, total_inactive_staked_liquidity, farm_apr, timestamp
                FROM pool_info
                WHERE chain=%s AND LOWER(pool_address)=LOWER(%s)
                LIMIT 1
                """,
                (chain, pool_address),
            )
            row = cursor.fetchone()
            if row:
                out[(chain, pool_address)] = row
    except mysql.connector.Error:
        return out
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    return out


def _fetch_rebalance_jobs(
    pool_map: dict[tuple[str, str], dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    if pool_map is not None and not pool_map:
        return [], True, None

    extended_columns = """
        id, chain, pool_address, wallet_address, old_token_id, new_token_id, status,
        old_tick_lower, old_tick_upper, new_tick_lower, new_tick_upper,
        range_lower_percent, range_upper_percent, range_percent_source,
        swap_tx_hash, withdraw_tx_hash, mint_tx_hash, stake_tx_hash, burn_tx_hash,
        error_reason, last_recovery_error, recovery_attempts,
        reserved_token0_raw, reserved_token1_raw, created_at, updated_at
    """
    fallback_columns = """
        id, chain, pool_address, wallet_address, old_token_id, new_token_id, status,
        old_tick_lower, old_tick_upper, new_tick_lower, new_tick_upper,
        swap_tx_hash, withdraw_tx_hash, mint_tx_hash, stake_tx_hash, burn_tx_hash,
        error_reason, created_at, updated_at
    """
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(f"SELECT {extended_columns} FROM configured_rebalance_jobs ORDER BY updated_at DESC")
        except mysql.connector.Error as exc:
            if exc.errno == 1146:
                return [], False, "configured_rebalance_jobs table is missing"
            if exc.errno != 1054:
                raise
            cursor.execute(f"SELECT {fallback_columns} FROM configured_rebalance_jobs ORDER BY updated_at DESC")
        rows = cursor.fetchall()
    except mysql.connector.Error as exc:
        return [], False, str(exc)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    jobs = []
    for row in rows:
        key = (str(row.get("chain") or "").upper(), _lower(row.get("pool_address")))
        if pool_map is not None and key not in pool_map:
            continue
        row["pool_key"] = key
        row["pool_name"] = pool_map[key]["name"] if pool_map is not None else _fallback_pool_name(row)
        row["status"] = str(row.get("status") or "").upper()
        row["is_completed"] = row["status"] in COMPLETED_STATUSES
        row["is_risky"] = _is_risky_job(row)
        row["status_class"] = f"status-{row['status'].lower().replace('_', '-')}"
        row["transactions"] = _job_transactions(row)
        row["_created_at_raw"] = row.get("created_at")
        row["_updated_at_raw"] = row.get("updated_at")
        row["created_at"] = _to_utc7_display(row.get("created_at"))
        row["updated_at"] = _to_utc7_display(row.get("updated_at"))
        jobs.append(row)
    _mark_effective_risky_jobs(jobs)
    return jobs, True, None


def _fetch_latest_positions(
    pool_map: dict[tuple[str, str], dict[str, Any]],
    jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not pool_map:
        return []

    jobs_by_position = _jobs_by_position(jobs)
    positions = []
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        for (chain, pool_address), pool in pool_map.items():
            wallets = pool.get("managed_wallets") or [pool.get("bot_wallet")]
            for wallet in wallets:
                if not wallet:
                    continue
                cursor.execute(
                    """
                    SELECT h.wallet_address, h.chain, h.nft_id, h.token0_symbol, h.token1_symbol,
                           h.pool_address, h.status, h.current_total_value, h.lower_price,
                           h.upper_price, h.current_price, h.created_at
                    FROM wallet_nft_position h
                    INNER JOIN (
                        SELECT nft_id, MAX(created_at) AS max_time
                        FROM wallet_nft_position
                        WHERE chain=%s
                          AND LOWER(pool_address)=LOWER(%s)
                          AND LOWER(wallet_address)=LOWER(%s)
                        GROUP BY nft_id
                    ) latest ON h.nft_id=latest.nft_id AND h.created_at=latest.max_time
                    WHERE h.chain=%s
                      AND LOWER(h.pool_address)=LOWER(%s)
                      AND LOWER(h.wallet_address)=LOWER(%s)
                    ORDER BY h.created_at DESC
                    """,
                    (chain, pool_address, wallet, chain, pool_address, wallet),
                )
                for row in cursor.fetchall():
                    token_id = str(row.get("nft_id") or "")
                    job = jobs_by_position.get((chain, pool_address, _lower(wallet), token_id))
                    row["pool_name"] = pool["name"]
                    row["job_status"] = (job or {}).get("status")
                    row["is_risky"] = bool(job and job.get("is_risky"))
                    positions.append(row)
    except mysql.connector.Error:
        return positions
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    return positions


def _pool_keys_from_jobs(jobs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    seen = set()
    keys = []
    for job in jobs:
        key = job.get("pool_key")
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _build_pool_wallet_summaries(
    jobs: list[dict[str, Any]],
    pool_info_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    job_summary = _job_summary_by_pool_wallet(jobs)
    summaries: dict[tuple[str, str, str], dict[str, Any]] = {}

    for job in jobs:
        key = _pool_wallet_key(job)
        summary = summaries.get(key)
        if summary is None:
            pool_key = job["pool_key"]
            db_info = pool_info_by_key.get(pool_key, {})
            summary = {
                "pool_wallet_key": key,
                "pool_key": pool_key,
                "chain": pool_key[0],
                "pool_address": job.get("pool_address"),
                "wallet_address": job.get("wallet_address") or "",
                "db_info": db_info,
                "pool_name": _db_pool_name(job, db_info),
                "pair": _pair_label(job, db_info),
                "latest_status": job.get("status"),
                "latest_status_class": job.get("status_class"),
                "latest_updated_at": job.get("updated_at"),
                "active_job_count": job_summary.get(key, {}).get("active", 0),
                "completed_job_count": job_summary.get(key, {}).get("completed", 0),
                "total_job_count": job_summary.get(key, {}).get("total", 0),
                "has_risky_job": job_summary.get(key, {}).get("active", 0) > 0,
                "range_label": "No range",
                "range_title": "No range recorded",
                "range_detail": "no journal percent yet",
                "range_source": "none",
                "range_updated_at": None,
            }
            _apply_range_display(summary, _range_from_job(job))
            summaries[key] = summary

    return summaries


def _build_pool_aggregate_summaries(
    jobs: list[dict[str, Any]],
    pool_info_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    job_summary = _job_summary_by_pool(jobs)
    pool_wallet_summaries = _build_pool_wallet_summaries(jobs, pool_info_by_key)
    summaries: dict[tuple[str, str], dict[str, Any]] = {}

    for job in jobs:
        key = job["pool_key"]
        summary = summaries.get(key)
        if summary is None:
            db_info = pool_info_by_key.get(key, {})
            summary = {
                "pool_key": key,
                "chain": key[0],
                "pool_address": job.get("pool_address"),
                "db_info": db_info,
                "pool_name": _db_pool_name(job, db_info),
                "pair": _pair_label(job, db_info),
                "wallets": [],
                "latest_status": job.get("status"),
                "latest_status_class": job.get("status_class"),
                "latest_updated_at": job.get("updated_at"),
                "active_job_count": job_summary.get(key, {}).get("active", 0),
                "completed_job_count": job_summary.get(key, {}).get("completed", 0),
                "total_job_count": job_summary.get(key, {}).get("total", 0),
                "has_risky_job": job_summary.get(key, {}).get("active", 0) > 0,
                "active_wallet_count": 0,
                "wallet_count": 0,
                "is_multi_wallet": False,
                "range_label": "No range",
                "range_title": "No range recorded",
                "range_detail": "no journal percent yet",
                "range_source": "none",
                "range_updated_at": None,
            }
            summaries[key] = summary

        wallet = job.get("wallet_address")
        if wallet and wallet not in summary["wallets"]:
            summary["wallets"].append(wallet)

    for key, summary in summaries.items():
        wallet_summaries = [
            item for item in pool_wallet_summaries.values() if item.get("pool_key") == key
        ]
        summary["wallet_count"] = len(wallet_summaries)
        summary["active_wallet_count"] = sum(1 for item in wallet_summaries if item.get("has_risky_job"))
        summary["is_multi_wallet"] = summary["wallet_count"] > 1
        if summary["wallet_count"] == 1:
            wallet_summary = wallet_summaries[0]
            for field in (
                "range_label",
                "range_title",
                "range_detail",
                "range_source",
                "range_updated_at",
                "range_lower_percent",
                "range_upper_percent",
            ):
                if field in wallet_summary:
                    summary[field] = wallet_summary[field]

    return summaries


def _jobs_by_position(jobs: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    out = {}
    for job in jobs:
        chain = str(job.get("chain") or "").upper()
        pool_address = _lower(job.get("pool_address"))
        wallet = _lower(job.get("wallet_address"))
        for token_key in ("old_token_id", "new_token_id"):
            token_id = job.get(token_key)
            if token_id is not None:
                out[(chain, pool_address, wallet, str(token_id))] = job
    return out


def _risky_jobs_by_pool_wallet(jobs: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    out = {}
    for job in jobs:
        if not job.get("is_effective_risky"):
            continue
        key = (
            str(job.get("chain") or "").upper(),
            _lower(job.get("pool_address")),
            _lower(job.get("wallet_address")),
        )
        out.setdefault(key, job)
    return out


def _latest_jobs_by_pool_wallet(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = {}
    for job in jobs:
        key = _pool_wallet_key(job)
        if key not in latest:
            latest[key] = job
    return list(latest.values())


def _pool_wallet_key(job: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(job.get("chain") or "").upper(),
        _lower(job.get("pool_address")),
        _lower(job.get("wallet_address")),
    )


def _mark_effective_risky_jobs(jobs: list[dict[str, Any]]) -> None:
    latest_completed_by_pool_wallet: dict[tuple[str, str, str], Any] = {}
    for job in jobs:
        key = (
            str(job.get("chain") or "").upper(),
            _lower(job.get("pool_address")),
            _lower(job.get("wallet_address")),
        )
        if key not in latest_completed_by_pool_wallet and job.get("is_completed"):
            latest_completed_by_pool_wallet[key] = job.get("_updated_at_raw") or job.get("updated_at")

    for job in jobs:
        key = (
            str(job.get("chain") or "").upper(),
            _lower(job.get("pool_address")),
            _lower(job.get("wallet_address")),
        )
        latest_completed_at = latest_completed_by_pool_wallet.get(key)
        job_updated_at = job.get("_updated_at_raw") or job.get("updated_at")
        stale_after_completion = bool(
            latest_completed_at
            and job_updated_at
            and job_updated_at < latest_completed_at
        )
        job["is_effective_risky"] = bool(job.get("is_risky") and not stale_after_completion)


def _job_summary_by_pool(jobs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, int]]:
    summary: dict[tuple[str, str], dict[str, int]] = {}
    for job in jobs:
        key = job["pool_key"]
        item = summary.setdefault(key, {"active": 0, "completed": 0, "total": 0})
        item["total"] += 1
        if job.get("is_effective_risky"):
            item["active"] += 1
        if job["is_completed"]:
            item["completed"] += 1
    return summary


def _job_summary_by_pool_wallet(jobs: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, int]]:
    summary: dict[tuple[str, str, str], dict[str, int]] = {}
    for job in jobs:
        key = _pool_wallet_key(job)
        item = summary.setdefault(key, {"active": 0, "completed": 0, "total": 0})
        item["total"] += 1
        if job.get("is_effective_risky"):
            item["active"] += 1
        if job["is_completed"]:
            item["completed"] += 1
    return summary


def _range_from_job(job: dict[str, Any]) -> dict[str, Any] | None:
    lower = job.get("range_lower_percent")
    upper = job.get("range_upper_percent")
    if lower is None or upper is None:
        return None
    return {
        "lower_percent": lower,
        "upper_percent": upper,
        "source": job.get("range_percent_source") or "journal",
        "updated_at": job.get("updated_at"),
    }


def _apply_range_display(pool: dict[str, Any], journal_range: dict[str, Any] | None) -> None:
    if pool.get("range_source") == "config":
        return
    if not journal_range:
        pool["range_label"] = "No range"
        pool["range_title"] = "No range recorded"
        pool["range_detail"] = "no config or journal percent yet"
        pool["range_source"] = "none"
        return

    lower = journal_range["lower_percent"]
    upper = journal_range["upper_percent"]
    pool["range_label"] = _range_label(lower, upper)
    pool["range_title"] = "Latest journal range"
    pool["range_detail"] = f"lower {_format_percent(lower)} / upper {_format_percent(upper, signed=True)}"
    pool["range_source"] = "journal"
    pool["range_lower_percent"] = lower
    pool["range_upper_percent"] = upper
    pool["range_updated_at"] = journal_range.get("updated_at")


def _is_risky_job(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").upper()
    if status in RISKY_STATUSES:
        return True
    if status != "FAILED":
        return False
    if job.get("last_recovery_error"):
        return True
    return _positive_int(job.get("reserved_token0_raw")) > 0 or _positive_int(job.get("reserved_token1_raw")) > 0


def _job_transactions(job: dict[str, Any]) -> list[dict[str, str]]:
    chain = str(job.get("chain") or "").upper()
    base_url = TX_EXPLORERS.get(chain)
    transactions = []
    for label, column in [
        ("withdraw", "withdraw_tx_hash"),
        ("swap", "swap_tx_hash"),
        ("mint", "mint_tx_hash"),
        ("stake", "stake_tx_hash"),
        ("burn", "burn_tx_hash"),
    ]:
        tx_hash = job.get(column)
        if not tx_hash:
            continue
        tx_hash = str(tx_hash)
        transactions.append(
            {
                "label": label,
                "hash": tx_hash,
                "short_hash": f"{tx_hash[:8]}...{tx_hash[-6:]}",
                "url": f"{base_url}{tx_hash}" if base_url else "",
            }
        )
    return transactions


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _pair_label(pool: dict[str, Any], db_info: dict[str, Any]) -> str:
    token0 = db_info.get("token0_symbol") or pool.get("token0_address") or "Token0"
    token1 = db_info.get("token1_symbol") or pool.get("token1_address") or "Token1"
    return f"{token0}/{token1}"


def _db_pool_name(job: dict[str, Any], db_info: dict[str, Any]) -> str:
    token0 = db_info.get("token0_symbol")
    token1 = db_info.get("token1_symbol")
    fee = db_info.get("fee")
    if token0 and token1:
        if fee is not None:
            try:
                return f"{token0}-{token1}-{float(fee) / 10000:g}"
            except (TypeError, ValueError):
                pass
        return f"{token0}-{token1}"
    return _fallback_pool_name(job)


def _fallback_pool_name(job: dict[str, Any]) -> str:
    chain = str(job.get("chain") or "").upper()
    pool_address = str(job.get("pool_address") or "")
    if not pool_address:
        return chain or "Unknown pool"
    return f"{chain}:{pool_address[:8]}...{pool_address[-6:]}"


def _range_label(lower: Any, upper: Any) -> str:
    return f"{_format_percent(lower)} / {_format_percent(upper, signed=True)}"


def _format_percent(value: Any, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}{number:.6g}%"


def _lower(value: Any) -> str:
    return str(value or "").lower()
