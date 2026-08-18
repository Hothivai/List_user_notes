import sys
import os
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
sys.path.append(PROJECT_ROOT)

import json
from datetime import datetime
from collections import defaultdict
from decimal import Decimal
from services.db_connect import get_connection
from services.transaction_history.sol_tx_his import prioritize_token, sum_token_transfers
from services.transaction_history.tx_his import calculate_separate_tokens

_DETAIL_CONTEXT_COLUMN_EXISTS = None

def convert(obj):
  if isinstance(obj, datetime):
    return obj.strftime("%Y-%m-%d %H:%M:%S")
  return str(obj)

def detail_context_column_exists(cursor) -> bool:
  global _DETAIL_CONTEXT_COLUMN_EXISTS
  if _DETAIL_CONTEXT_COLUMN_EXISTS is not None:
    return _DETAIL_CONTEXT_COLUMN_EXISTS

  try:
    cursor.execute("SHOW COLUMNS FROM transaction_detail_v2_bk LIKE 'is_context'")
    _DETAIL_CONTEXT_COLUMN_EXISTS = cursor.fetchone() is not None
  except Exception:
    _DETAIL_CONTEXT_COLUMN_EXISTS = False
  return _DETAIL_CONTEXT_COLUMN_EXISTS

def non_context_details(details: list) -> list:
  return [detail for detail in details or [] if not detail.get("is_context", False)]

def calculate_non_context_totals(transactions: list, symbol: str):
  total_transactions = [
    {**tx, "details": non_context_details(tx.get("details", []))}
    for tx in transactions
  ]
  return calculate_separate_tokens(total_transactions, symbol)

def normalize_is_context(detail: dict):
  detail["is_context"] = bool(detail.get("is_context", False))
  return detail

def get_symbol_cache_from_db() -> dict:
  """
  Lấy danh sách token symbol từ bảng pool_sol_info trong database.
  Trả về dict {mint_address: symbol} với token address không trùng lặp.
  Symbol được chuẩn hóa: loại bỏ null bytes, whitespace thừa.
  """
  conn = get_connection()
  cursor = conn.cursor()
  query = """
    SELECT token0_mint, token0_symbol, token1_mint, token1_symbol 
    FROM pool_sol_info 
    WHERE token0_mint IS NOT NULL AND token1_mint IS NOT NULL
  """
  symbol_cache = {}
  try:
    cursor.execute(query)
    rows = cursor.fetchall()
    for row in rows:
      token0_mint, token0_symbol, token1_mint, token1_symbol = row
      
      # Chuẩn hóa symbol: loại bỏ null bytes và whitespace thừa
      if token0_symbol:
        token0_symbol = token0_symbol.replace('\x00', '').strip()
      if token1_symbol:
        token1_symbol = token1_symbol.replace('\x00', '').strip()
      
      if token0_mint and token0_symbol and token0_mint not in symbol_cache:
        symbol_cache[token0_mint] = token0_symbol
      if token1_mint and token1_symbol and token1_mint not in symbol_cache:
        symbol_cache[token1_mint] = token1_symbol
  except Exception as e:
    print(f"Error fetching symbol cache from DB: {e}")
  finally:
    conn.close()
  print(f"Loaded {len(symbol_cache)} unique token symbols from pool_sol_info")
  return symbol_cache

def get_lasted_signature_v2(wallet:str)->str:
  conn = get_connection()
  cursor = conn.cursor()
  query = """
  SELECT hash, date_time FROM transaction_history_v2_bk WHERE wallet = %s ORDER BY date_time DESC LIMIT 1
  """
  signature = ""
  try:
    cursor.execute(query, (wallet,))
    result = cursor.fetchone()
    if result is None:
      signature = ""
    else:
      signature = result[0]
  except Exception as e:
    print(f"Error fetching lasted signature v2: {e}")
  finally:
    conn.close()
  return signature

def get_lasted_block_v2(wallet: str, chain: str) -> int:
  conn = get_connection()
  cursor = conn.cursor()
  query = """
    SELECT block from transaction_history_v2_bk WHERE wallet =%s AND chain = %s ORDER BY cast(block AS UNSIGNED) DESC LIMIT 1
    """
  block = 0
  try:
    cursor.execute(query, (wallet, chain))
    result = cursor.fetchone()
    if result is None:
      block = 0
    else:
      block = int(result[0])
  except Exception as e:
    print(f"Error fetching lasted block v2: {e}")
    return block
  finally:
    conn.close()
  print(f"Lasted block for wallet {wallet} on chain {chain} v2 is: {block}")
  if block is None:
    return 0
  return block + 1

def insert_transactions_v2(wallet: str, chain: str, transactions: list):
  batch_hash = []
  batch_size = 500    
  conn = get_connection()
  cursor = conn.cursor()
  print(f"Inserting {len(transactions)} transactions into the v2 database...")
    
  try:
    for tx in transactions:
      batch_hash.append((
        tx.get("hash"),
        tx.get("block"),
        chain,
        wallet,
        tx.get("tx_time"),        
        tx.get("contract", ""),
        tx.get("token_id", ""),
        tx.get("transaction_fee", 0),
        tx.get("gas_fee", 0)
      ))
      
      if len(batch_hash) >= batch_size:
        cursor.executemany("""
          INSERT IGNORE INTO transaction_history_v2_bk (hash, block, chain, wallet, date_time, contract, token_id, transaction_fee, gas_fee) 
          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
          """, batch_hash)
        batch_hash.clear()
    
    if batch_hash:
      cursor.executemany("""
          INSERT IGNORE INTO transaction_history_v2_bk (hash, block, chain, wallet, date_time, contract, token_id, transaction_fee, gas_fee) 
          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, batch_hash)
  except Exception as e:
    print(f"Error inserting transactions v2: {e}")
    conn.rollback() 
  finally:
    conn.commit()
    conn.close()
  print("Insertion completed (V2).")

def insert_detail_token_transfer_v2(details):
  batch = []
  batch_size = 500    
  conn = get_connection()
  cursor = conn.cursor()
  print(f"Inserting {len(details)} details into the v2 database...")
  has_is_context = detail_context_column_exists(cursor)
    
  try:
    for tx in details:
      values = [
        tx["hash"],
        tx["from_address"],
        tx["to_address"],
        tx["contract"],
        tx["amount"],
        tx["symbol"],
        tx["wallet"]
      ]
      if has_is_context:
        values.append(1 if tx.get("is_context", False) else 0)
      batch.append(tuple(values))
      
      if len(batch) >= batch_size:
        if has_is_context:
          cursor.executemany("""
            INSERT IGNORE INTO transaction_detail_v2_bk (hash, from_address, to_address, contract, amount, symbol, wallet, is_context) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, batch)
        else:
          cursor.executemany("""
            INSERT IGNORE INTO transaction_detail_v2_bk (hash, from_address, to_address, contract, amount, symbol, wallet) 
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, batch)
        batch.clear()
    
    if batch:
      if has_is_context:
        cursor.executemany("""
          INSERT IGNORE INTO transaction_detail_v2_bk (hash, from_address, to_address, contract, amount, symbol, wallet, is_context) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
          """, batch)
      else:
        cursor.executemany("""
          INSERT IGNORE INTO transaction_detail_v2_bk (hash, from_address, to_address, contract, amount, symbol, wallet) 
            VALUES (%s, %s, %s, %s, %s, %s, %s)
          """, batch)
  except Exception as e:
    print(f"Error inserting transaction details v2: {e}")
    conn.rollback() 
  finally:
    conn.commit()
    conn.close()
  print("Insertion completed (V2).")
  
def accumulate_transfers(details: list) -> list:
    """
    Groups transfers by (hash, from_address, to_address, contract, symbol, wallet, token_id)
    and sums their amounts using Decimal for precision.
    """
    if not details:
        return []

    grouped = {}
    for tx in details:
        # Create a unique key for grouping
        key = (
            tx.get("hash"),
            tx.get("from_address"),
            tx.get("to_address"),
            tx.get("contract"),
            tx.get("symbol"),
            tx.get("wallet"),
            tx.get("token_id", ""),  # Handle NFT token_id if present
            bool(tx.get("is_context", False))
        )
        
        # Convert amount to Decimal for accurate summing
        try:
            amount = Decimal(str(tx.get("amount", 0)))
        except (ValueError, TypeError):
            amount = Decimal(0)

        if key in grouped:
            grouped[key]["amount"] += amount
        else:
            # Create a copy to avoid mutating the original dict
            new_tx = tx.copy()
            new_tx["amount"] = amount
            grouped[key] = new_tx

    # Convert Decimal back to string or float for DB compatibility
    # Here we keep it as string or normalized float as per existing code patterns
    results = []
    for tx in grouped.values():
        tx["amount"] = str(tx["amount"])
        results.append(tx)
        
    return results

def get_transaction_v2(wallet:str, chains:list, start_time:str, end_time:str, symbol:str, contract:str = "", batch_size:int = 500):
  if not chains:
    return {"transactions": [], "max_length": 0, "total": {}}
    
  conn = get_connection()
  cursor = conn.cursor(dictionary=True)
  
  base_query = """
    SELECT hash, block, chain, date_time as tx_time, wallet, contract as nft_contract, token_id as nft_token_id, transaction_fee, gas_fee 
    FROM transaction_history_v2_bk 
    WHERE wallet = %s AND chain IN ({placeholders})
  """
  
  date_condition = ""
  if start_time and end_time:
    date_condition = " AND DATE(date_time) BETWEEN %s AND %s "
  elif start_time:
    date_condition = " AND DATE(date_time) >= %s "
  elif end_time:
    date_condition = " AND DATE(date_time) <= %s "
    
  transaction_query = base_query + date_condition + " ORDER BY date_time DESC"

  transactions = []
  hashs = []
  results = []
  hash_list = []
  max_length = 0
  
  try:
    has_is_context = detail_context_column_exists(cursor)
    chain_placeholders = ','.join(['%s']*len(chains))
    transaction_query = transaction_query.format(placeholders = chain_placeholders)
    params =  [wallet] + chains
    if start_time and end_time:
      params.extend([start_time, end_time])
    elif start_time:
      params.append(start_time)
    elif end_time:
      params.append(end_time)
      
    cursor.execute(transaction_query,params)
    data = cursor.fetchall()
    hashs.extend(data)
    
    results = json.loads(json.dumps(hashs,default=convert, ensure_ascii=False))
    hash_list = [row["hash"] for row in hashs]
    map_detail = defaultdict(list)
    
    detail_columns = "hash, from_address, to_address, contract, amount, symbol, wallet"
    if has_is_context:
      detail_columns += ", is_context"
    detail_token_query = f"""
      SELECT {detail_columns} FROM transaction_detail_v2_bk WHERE hash IN ({{placeholders}})
    """

    for i in range(0, len(hash_list), batch_size):
      batch = hash_list[i: i+batch_size]
      placeholders = ','.join(['%s']*len(batch))
      
      d_query = detail_token_query.format(placeholders = placeholders)
      
      # get detail token 
      cursor.execute(d_query, batch)
      details = cursor.fetchall()

      for detail in details:
        detail = normalize_is_context(detail)
        hash_value = detail.get("hash")
        amount_val = float(detail.get("amount", 0)) if detail.get("amount") else 0
        from_addr = detail.get("from_address", "")
        to_addr = detail.get("to_address", "")
        
        keep_detail = False
        if from_addr.lower() == wallet.lower():
            keep_detail = amount_val < 0
        elif to_addr.lower() == wallet.lower():
            keep_detail = amount_val > 0
        else:
            keep_detail = amount_val > 0
            
        if keep_detail:
            del detail["hash"]
            map_detail[hash_value].append(detail)
      
    for tx in results:
      hash_value = tx["hash"]
      tx["details"] = map_detail.get(hash_value,[])
      
      # Map NFT data back into the expected structure for frontend if present
      nft_contract = tx.pop("nft_contract", "")
      nft_token_id = tx.pop("nft_token_id", "")
      if nft_token_id:
          tx["nft"] = {"contract": nft_contract, "token_id": nft_token_id, "wallet": tx["wallet"]}
      else:
          tx["nft"] = {}
      
  except Exception as e:
    print(f"Get transaction v2 error: {e}")
  finally:
    conn.close()
    
  for tx in results:
    searchable_details = non_context_details(tx.get("details", []))
    if symbol and contract:
      has_match = any(detail["symbol"].strip().lower() == symbol.strip().lower() and detail["contract"].strip().lower() == contract.strip().lower() for detail in searchable_details)
    elif symbol:
      has_match = any(detail["symbol"].strip().lower() == symbol.strip().lower() for detail in searchable_details)
    else:
      has_match = True

    if has_match:
      tx["details"] = prioritize_token(tx["details"], symbol, contract)
      if chains[0] == "SOL":
        changed_token = sum_token_transfers(tx["details"], False)
        tx["changed_token"] = changed_token
      length_tx = len(tx["details"])
      if length_tx > max_length:
        max_length = length_tx
      transactions.append(tx)
  
  separate_total_symbol = calculate_non_context_totals(transactions, symbol)
  return {"transactions":transactions, "max_length":max_length, "total":separate_total_symbol}

def get_existing_wallet_v2(wallet:str):
  conn = get_connection()
  cursor = conn.cursor()
  existing_wallet = False
  try:
    cursor.execute("""
                    SELECT wallet FROM transaction_history_v2_bk WHERE wallet = %s LIMIT 1
                   """,(wallet,))
    result = cursor.fetchone()
  except Exception as e:
    print(f"Get existing wallet v2 error: {e}")
  finally:
    conn.close()
  if result:
    existing_wallet = True
  return existing_wallet

def search_tx_by_nft_id(nft_id_partial: str, chain: str = None, batch_size: int = 500) -> dict:
  """
  Search transactions by partial NFT ID (LIKE match) with optional chain filter.
  - nft_id_partial : partial string, matched with LIKE '%nft_id_partial%'
  - chain          : None / "" → all chains; "SOL" → Solana only; "BAS" / "ETH" / etc. → specific EVM chain
  Output format mirrors get_transaction_v2:
    {
      "transactions" : [ { hash, block, chain, tx_time, wallet, transaction_fee, gas_fee, nft, details, changed_token? } ],
      "max_length"   : int,
      "total"        : {},
      "count"        : int
    }
  """
  conn = get_connection()
  cursor = conn.cursor(dictionary=True)

  results = []
  max_length = 0

  try:
    has_is_context = detail_context_column_exists(cursor)
    # ── 1. Fetch matching main transactions ─────────────────────────────────
    base_query = """
      SELECT hash, block, chain, date_time AS tx_time, wallet,
             contract AS nft_contract, token_id AS nft_token_id,
             transaction_fee, gas_fee
      FROM transaction_history_v2_bk
      WHERE token_id LIKE %s
    """
    params = [f"%{nft_id_partial}%"]

    if chain:
      base_query += " AND chain = %s"
      params.append(chain.upper())

    base_query += " ORDER BY date_time DESC"

    cursor.execute(base_query, params)
    rows = cursor.fetchall()

    # Serialize datetime objects
    import json
    from datetime import datetime as _dt

    def _convert(obj):
      if isinstance(obj, _dt):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
      return str(obj)

    results = json.loads(json.dumps(rows, default=_convert, ensure_ascii=False))

    if not results:
      return {"transactions": [], "max_length": 0, "total": {}, "count": 0}

    # ── 2. Batch-fetch detail token transfers ───────────────────────────────
    hash_list = [row["hash"] for row in results]
    map_detail = defaultdict(list)

    detail_columns = "hash, from_address, to_address, contract, amount, symbol, wallet"
    if has_is_context:
      detail_columns += ", is_context"
    detail_query_tpl = f"""
      SELECT {detail_columns}
      FROM transaction_detail_v2_bk
      WHERE hash IN ({{placeholders}})
    """

    for i in range(0, len(hash_list), batch_size):
      batch = hash_list[i: i + batch_size]
      placeholders = ",".join(["%s"] * len(batch))
      d_query = detail_query_tpl.format(placeholders=placeholders)
      cursor.execute(d_query, batch)
      details = cursor.fetchall()

      for detail in details:
        detail = normalize_is_context(detail)
        hash_value = detail.get("hash")
        # Find the wallet for this tx to apply directional filter
        tx_wallet = next((r["wallet"] for r in results if r["hash"] == hash_value), "").lower()
        amount_val = float(detail.get("amount", 0)) if detail.get("amount") else 0
        from_addr  = (detail.get("from_address") or "").lower()
        to_addr    = (detail.get("to_address")   or "").lower()

        keep = False
        if from_addr == tx_wallet:
          keep = amount_val < 0
        elif to_addr == tx_wallet:
          keep = amount_val > 0
        else:
          keep = amount_val > 0

        if keep:
          del detail["hash"]
          map_detail[hash_value].append(detail)

    # ── 3. Assemble final output ────────────────────────────────────────────
    is_sol = (chain or "").upper() == "SOL"
    transactions = []

    for tx in results:
      hash_value    = tx["hash"]
      nft_contract  = tx.pop("nft_contract", "") or ""
      nft_token_id  = tx.pop("nft_token_id", "") or ""

      # Build nft object
      if nft_token_id:
        tx["nft"] = {
          "contract" : nft_contract,
          "token_id" : nft_token_id,
          "wallet"   : tx.get("wallet", "")
        }
      else:
        tx["nft"] = {}

      # Attach details
      tx_details = map_detail.get(hash_value, [])
      tx["details"] = tx_details

      # SOL: compute changed_token summary
      if is_sol:
        changed = {}
        for d in non_context_details(tx_details):
          sym = d.get("symbol", "")
          amt = float(d.get("amount", 0))
          changed[sym] = changed.get(sym, 0) + amt
        tx["changed_token"] = {k: v for k, v in changed.items() if v != 0}

      length = len(tx_details)
      if length > max_length:
        max_length = length

      transactions.append(tx)

  except Exception as e:
    print(f"search_tx_by_nft_id error: {e}")
  finally:
    conn.close()

  return {
    "transactions" : transactions,
    "max_length"   : max_length,
    "total"        : {},
    "count"        : len(transactions)
  }
