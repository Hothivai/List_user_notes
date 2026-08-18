import json
from datetime import datetime
from collections import defaultdict
from services.db_connect import get_connection
from services.transaction_history.sol_tx_his import prioritize_token, sum_token_transfers
from services.transaction_history.tx_his import calculate_separate_tokens

def convert(obj):
  if isinstance(obj, datetime):
    return obj.strftime("%Y-%m-%d %H:%M:%S")
  return str(obj)

def get_lasted_signature(wallet:str)->str:
  conn = get_connection()
  cursor = conn.cursor()
  query = """
  SELECT tx_hash.hash, tx_hash.tx_time, tx.wallet FROM hash_txs AS tx_hash INNER JOIN transactions AS tx ON tx_hash.hash = tx.hash WHERE tx.wallet = %s ORDER BY tx_hash.tx_time DESC LIMIT 1
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
    print(f"Error fetching lasted signature: {e}")
    conn.close()
  finally:
    conn.close()
  return signature

def get_lasted_block(wallet: str, chain: str) -> int:
  conn = get_connection()
  cursor = conn.cursor()
  query = """
    SELECT tx_hash.block, tx.wallet from hash_txs AS tx_hash INNER JOIN transactions AS tx ON tx_hash.hash = tx.hash WHERE wallet =%s AND chain = %s ORDER BY cast(tx_hash.block AS UNSIGNED) DESC LIMIT 1
    """
  # query = """
  #   SELECT block from transactions WHERE wallet =%s AND chain = %s ORDER BY cast(block AS BIGINT) DESC LIMIT 1
  #   """
  block = 0
  try:
    cursor.execute(query, (wallet, chain))
    result = cursor.fetchone()
    if result is None:
      block = 0
    else:
      block = int(result[0])
  except Exception as e:
    print(f"Error fetching lasted block: {e}")
    return block
  finally:
    conn.close()
  print(f"Lasted block for wallet {wallet} on chain {chain} is: {block}")
  if block is None:
    return 0
  return block + 1

def insert_transactions(wallet: str, chain: str, transactions: list):
  batch_hash = []
  batch_wallet = []
  batch_size = 500    
  conn = get_connection()
  cursor = conn.cursor()
  print(f"Inserting {len(transactions)} transactions into the database...")
    
  try:
    for tx in transactions:
      batch_hash.append((
        tx["hash"],
        tx["block"],
        chain,
        tx["tx_time"],        
      ))
      batch_wallet.append((
        tx["hash"],
        wallet
      ))
      
      if len(batch_hash) >= batch_size:
        cursor.executemany("""
          INSERT IGNORE INTO hash_txs (hash, block, chain, tx_time) 
          VALUES (%s, %s, %s, %s)
          """, batch_hash)
        cursor.executemany("""
          INSERT INTO transactions (hash, wallet) 
          VALUES (%s, %s)
          """, batch_wallet)
        batch_hash.clear()
        batch_wallet.clear()
    
    if batch_hash:
      cursor.executemany("""
        INSERT IGNORE INTO hash_txs (hash, block, chain, tx_time) 
        VALUES (%s, %s, %s, %s)
        """, batch_hash)
      cursor.executemany("""
        INSERT INTO transactions (hash, wallet) 
        VALUES (%s, %s)
        """, batch_wallet)
  except Exception as e:
    print(f"Error inserting transactions: {e}")
    conn.rollback() 
  finally:
    conn.commit()
    conn.close()
  print("Insertion completed.")

def insert_detail_token_transfer(details):
  batch = []
  batch_size = 500    
  conn = get_connection()
  cursor = conn.cursor()
  print(f"Inserting {len(details)} transactions into the database...")
    
  try:
    for tx in details:
      batch.append((
        tx["hash"],
        tx["from_address"],
        tx["to_address"],
        tx["contract"],
        tx["amount"],
        tx["symbol"],
        tx["wallet"]
      ))
      
      if len(batch) >= batch_size:
        cursor.executemany("""
          INSERT INTO detail_token_transactions (hash, from_address, to_address, contract, amount, symbol, wallet) 
          VALUES (%s, %s, %s, %s, %s, %s, %s)
          """, batch)
        batch.clear()
    
    if batch:
      cursor.executemany("""
        INSERT INTO detail_token_transactions (hash, from_address, to_address, contract, amount, symbol, wallet) 
          VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, batch)
  except Exception as e:
    print(f"Error inserting transactions: {e}")
    conn.rollback() 
  finally:
    conn.commit()
    conn.close()
  print("Insertion completed.")
  
def insert_nft_token(details):
  batch = []
  batch_size = 500    
  conn = get_connection()
  cursor = conn.cursor()
  print(f"Inserting {len(details)} nft transactions into the database...")
    
  try:
    for tx in details:
      batch.append((
        tx["hash"],
        tx["contract"],
        tx["token_id"],
        tx["wallet"]
      ))
      
      if len(batch) >= batch_size:
        cursor.executemany("""
          INSERT INTO nft_token_transactions (hash, contract, token_id, wallet) 
          VALUES (%s, %s, %s, %s)
          """, batch)
        batch.clear()
    
    if batch:
      cursor.executemany("""
        INSERT INTO nft_token_transactions (hash, contract, token_id, wallet) 
          VALUES (%s, %s, %s, %s)
        """, batch)
  except Exception as e:
    print(f"Error inserting transactions: {e}")
    conn.rollback() 
  finally:
    conn.commit()
    conn.close()
  print("Insertion completed.")

def get_transaction(wallet:str, chains:list, start_time:str, end_time:str, symbol:str, contract:str = "", batch_size:int = 500):
  conn = get_connection()
  cursor = conn.cursor(dictionary=True)
  
  transaction_query = """
    SELECT h.hash, h.block, h.chain ,h.tx_time, t.wallet from hash_txs h JOIN transactions t ON t.hash = h.hash WHERE t.wallet = %s AND h.chain IN ({placeholders}) AND DATE(h.tx_time) BETWEEN %s AND %s ORDER BY h.tx_time DESC
  """
  
  detail_token_query = """
    SELECT hash, from_address, to_address, contract, amount, symbol, wallet FROM detail_token_transactions WHERE hash IN ({placeholders})
  """
  
  nft_query = """
    SELECT hash, token_id, contract, wallet FROM nft_token_transactions WHERE hash IN ({placeholders})
  """
  
  transactions = []
  hashs = []
  results = []
  hash_list = []
  max_length = 0
  
  try:
    chain_placeholders = ','.join(['%s']*len(chains))
    transaction_query = transaction_query.format(placeholders = chain_placeholders)
    params =  [wallet] + chains + [start_time, end_time]
    cursor.execute(transaction_query,params)
    data = cursor.fetchall()
    hashs.extend(data)
    
    results = json.loads(json.dumps(hashs,default=convert, ensure_ascii=False))
    hash_list = [row["hash"] for row in hashs]
    map_detail = defaultdict(list)
    map_nft = {}
    for i in range(0, len(hash_list), batch_size):
      batch = hash_list[i: i+batch_size]
      placeholders = ','.join(['%s']*len(batch))
      
      d_query = detail_token_query.format(placeholders = placeholders)
      n_query = nft_query.format(placeholders = placeholders  )
      
      # get detail token 
      cursor.execute(d_query, batch)
      details = cursor.fetchall()
      
      # get nft token
      cursor.execute(n_query, batch)
      nfts = cursor.fetchall()

      for detail in details:
        hash_value = detail["hash"]
        del detail["hash"]
        map_detail[hash_value].append(detail)
      
      for nft in nfts:
        hash_value = nft["hash"]
        del nft["hash"]
        map_nft[hash_value] = nft
      
    for tx in results:
      hash_value = tx["hash"]
      tx["details"] = map_detail.get(hash_value,[])
      tx["nft"] = map_nft.get(hash_value,{})
      
  except Exception as e:
    print(f"Get transaction error: {e}")
  finally:
    conn.close()
  print(results)
  for tx in results:
    has_symbol = any(detail["symbol"].lower() == symbol.lower() for detail in tx["details"])
    if has_symbol:
      tx["details"] = prioritize_token(tx["details"], symbol, contract)
      if chains[0] == "SOL":
        changed_token = sum_token_transfers(tx["details"], False)
        tx["changed_token"] = changed_token
      length_tx = len(tx["details"])
      if length_tx > max_length:
        max_length = length_tx
      transactions.append(tx)
    # for detail in tx["details"]:
    #   if detail["symbol"].lower() == symbol.lower():
    #     tx["details"] = prioritize_token(tx["details"], symbol, contract)
    #     transactions.append(tx)
        
    #     if chains[0] == "SOL":
    #       changed_token = sum_token_transfers(tx["details"], False)
    #       tx["changed_token"] = changed_token
        
    #     length_tx = len(tx["details"])
    #     if length_tx > max_length:
    #       max_length = length_tx
    #     break
  separate_total_symbol = calculate_separate_tokens(transactions, symbol)
  if chains[0] == "SOL":
    
    return {"transactions":transactions, "max_length":max_length, "total":separate_total_symbol}
  return {"transactions":transactions, "max_length":max_length, "total":separate_total_symbol}

def get_existing_wallet(wallet:str):
  conn = get_connection()
  cursor = conn.cursor()
  existing_wallet = False
  try:
    cursor.execute("""
                    SELECT wallet FROM transactions WHERE wallet = %s
                   """,(wallet,))
    result = cursor.fetchone()
  except Exception as e:
    print("Get existing wallet error: {e}")
  finally:
    conn.close()
  if result:
    existing_wallet = True
  return existing_wallet

def test_get_transaction():
  conn = get_connection()
  cursor = conn.cursor()
  start_time = "2025-10-27"
  end_time = "2025-10-30"
  query = """
    SELECT * FROM hash_txs WHERE chain = %s AND DATE(tx_time) BETWEEN %s AND %s
  """
  cursor.execute(query,("BAS", start_time, end_time,))
  result = cursor.fetchall()
  
  return result