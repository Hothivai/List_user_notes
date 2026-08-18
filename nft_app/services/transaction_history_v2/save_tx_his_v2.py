from datetime import datetime, timezone, timedelta
from decimal import Decimal, getcontext
import requests
import time
import json
import os
from eth_abi import decode
from eth_utils import to_bytes

from services.excute_transaction_v2 import insert_transactions_v2, insert_detail_token_transfer_v2, accumulate_transfers
from services.transaction_history_v2.tx_his_v2 import (
    CHAIN_ID, CURRENCY_MAP, MAPPING_DATA, generate_params, 
    process_transaction_transfer, merge_internal_tx, decode_tx_input
)
from logging_setup import tx_history_logger as logger


# from db import get_connection

getcontext().prec = 50

# Api urls of blockchains v2
API_URL = 'https://api.etherscan.io/v2/api'
API_KEY = 'W961R5X6KISNKFMJ2QWVA7999S2IQDNF6U'
MAX_RESULTS = 10000
OFFSET = 1000

ERROR_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error_cache.json")


def load_error_block():
  if os.path.exists(ERROR_CACHE_PATH):
    with open(ERROR_CACHE_PATH, "r") as f:
      try:
        return json.load(f)
      except json.JSONDecodeError:
        return {}
  return {}

def remove_duplicate_nft(nfts:list):
  unique_nfts = []
  seen_pairs = set()
  for tx in nfts:
    pair = (tx["hash"], tx["token_id"])
    if pair not in seen_pairs:
      seen_pairs.add(pair)
      unique_nfts.append(tx)
  return unique_nfts

def get_current_block(chain:int):
    now = datetime.now(timezone.utc)
    current_block = 0
    params = generate_params(chain, "block", "getblocknobytime", "before", int(now.timestamp()), API_KEY)
    # response = requests.get(API_URL, params=params)
    response, is_error = safe_fetch_with_retry(API_URL, params=params)
    if response is None or is_error or "Error! No closest block found" in response.get("result",""):
      current_block = 99999999
    else:
      current_block = response.get("result",0)
    return int(current_block)


def safe_fetch_with_retry(url, params= None, max_retries=100, delay=2):
  for attempt in range(max_retries):
    try:
      if attempt > 0:
        logger.warning("[EVM-SAVE] API retry %d/%d", attempt, max_retries)
      response = requests.get(url, params=params, timeout=10)
      data = response.json()
      if data:
        logger.info("[EVM-SAVE] API response: %s", data)
      # check if the response is successful
      status = str(data.get("status"))
      result = data.get("result")
      if status == "1" or (status == "0" and isinstance(result, list)):
        return data, False
      
      logger.warning("[EVM-SAVE] API Error/Rate-limit: status=%s, result=%s", status, result)
      time.sleep(delay)
      
    except requests.RequestException as e:
      logger.warning("[EVM-SAVE] API retry %d/%d after exception: %s", attempt + 1, max_retries, e)
      time.sleep(delay)
  logger.error("[EVM-SAVE] Exhausted %d retries. URL=%s", max_retries, url)
  return None, True


def get_transaction_with_recursive(wallet_address: str, chain:int, module:str, action:str, start_block:int, end_block:int, mapping:dict, collected = None, depth = 0, list_hash = None):
  if collected is None:
    collected = []
  
  page = 1
  total_txs = []
  
  while True:
    params = {
      "chainid": chain,
      "module":module,
      "action": action,
      "address": wallet_address,
      "startblock": start_block,
      "endblock": end_block,
      "page": page,
      "offset": OFFSET,
      "apikey": API_KEY
    }
    
    logger.debug("[EVM-SAVE][%s][%s][Ch:%d] Fetching page=%d blocks=%d-%d depth=%d", action, wallet_address[:8], chain, page, start_block, end_block, depth)
    response, is_error = safe_fetch_with_retry(API_URL, params=params)
    
    # Check if response has no data
    if is_error or response is None or not response.get("result"):
      break
    
    txs = response.get("result", [])
    total_txs.extend(txs)
    if (len(txs) < OFFSET):
      break
    
    page += 1
    time.sleep(0.2)  # to avoid hitting rate limits

    if page * OFFSET > MAX_RESULTS:
      logger.warning("[EVM-SAVE][%s][%s][Ch:%d] Hit MAX_RESULTS at blocks %d-%d. Splitting recursively.", action, wallet_address[:8], chain, start_block, end_block)
      mid_block = (start_block + end_block) // 2
      collected, is_error = get_transaction_with_recursive(wallet_address, chain, module, action, start_block, mid_block, mapping, collected, depth + 1, list_hash=list_hash)
      collected, is_error = get_transaction_with_recursive(wallet_address, chain, module, action, mid_block + 1, end_block, mapping, collected, depth + 1, list_hash=list_hash)
      return collected, is_error
  
  # Process and normalize transactions
  wallet_address_lower = wallet_address.lower()
  field_to_key = [(field, mapping["mapping"][field]) for field in mapping['fields']]
  transactions = []
  for tx in total_txs:
    if list_hash is not None and tx["hash"] not in list_hash:
      continue

    transaction = process_transaction_transfer(tx, wallet_address_lower, mapping["type"], field_to_key)

    transactions.append(transaction)
  
  collected.extend(transactions)
  
  return collected, is_error

# fetch transactions 
def get_new_transactions(wallet_address: str, chain: str, lasted_block: int ):
  chain_id = CHAIN_ID.get(chain, "")
  current_block = get_current_block(chain_id)
  internal_txs = []

  # fetch main transactions
  fetch_start_block = lasted_block + 1
  fetch_end_block = current_block
  transactions, normal_is_error = get_transaction_with_recursive(wallet_address, chain_id, "account", "txlist", fetch_start_block, fetch_end_block, MAPPING_DATA["transaction"])
  if normal_is_error:
    return

  # add normal value to internal:
  for tx in transactions:
    if tx["amount"] != "0":
      internal_txs.append({
        "hash": tx["hash"],
        "block": tx["block"],
        "from_address": tx["from_address"],
        "to_address": tx["to_address"],
        "amount": tx["amount"],
        "contract": tx["contract"],
        "direct": tx["direct"],
        "symbol": CURRENCY_MAP.get(chain, "ETH"),
        "wallet": tx["wallet"]
      })

  list_hash = [tx["hash"] for tx in transactions]

  # fetch internal transactions - dùng range gốc để không bỏ sót
  res_internal_txs, internal_is_error = get_transaction_with_recursive(wallet_address, chain_id, "account", "txlistinternal", fetch_start_block, fetch_end_block, MAPPING_DATA["internal_transaction"])
  if internal_is_error:
    return

  # Add symbol of internal transaction
  internal_txs.extend(res_internal_txs)
  for tx in internal_txs:
    tx["symbol"] = CURRENCY_MAP[chain]
    
  merge_internal_txs = merge_internal_tx(wallet_address, internal_txs)

  # fetch erc20 transactions - dùng range gốc để không bỏ sót tx ở block không có main tx
  erc20_txs, erc20_is_error = get_transaction_with_recursive(wallet_address, chain_id, "account", "tokentx", fetch_start_block, fetch_end_block, MAPPING_DATA["erc20_token"])
  if erc20_is_error:
    return

  # fetch nft transactions - dùng range gốc
  # ===== ERC721 (NFT) transactions =====
  erc721_txs, nft_is_error = get_transaction_with_recursive(
      wallet_address, chain_id, "account", "tokennfttx",
      fetch_start_block, fetch_end_block, MAPPING_DATA['erc721_token']
  )
  if nft_is_error:
      return

  # Remove duplicates
  unique_nfts = remove_duplicate_nft(erc721_txs)

  # Bổ sung token_id bị thiếu (collect / increase / harvest)
  decoded_nfts = []

  for tx in transactions:
      decoded = decode_tx_input(tx.get("input", ""))
      if decoded and decoded.get("token_id") is not None:
          tx_hash = tx["hash"]
          token_id = str(decoded["token_id"])

          # Tìm trong unique_nfts xem có NFT nào cùng hash chưa
          nft_match = next((n for n in unique_nfts if n["hash"] == tx_hash), None)

          if not nft_match:
              if wallet_address == tx.get("to_address"):
                direct = "IN"
                wallet = tx.get("from_address")
              else:
                direct = "OUT"
                wallet = tx.get("to_address")

              new_nft = {
                  "hash": tx_hash,
                  "from_address": tx.get("from_address"),
                  "to_address": tx.get("to_address"),
                  "token_id": token_id,
                  "token_name": "Unknown",
                  "symbol": "Unknown",
                  "contract": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
                  "decimal": 0,
                  "direct": direct,
                  "wallet": wallet,
              }
              decoded_nfts.append(new_nft)
              logger.debug("[EVM-SAVE] Decoded NFT token_id=%s func=%s", token_id, decoded['function'])

  # Gộp NFT decode mới với NFT từ Etherscan
  all_nfts = unique_nfts + decoded_nfts

  from services.transaction_history_v2.tx_his_v2 import convert_block_time
  # Synthesize missing main transactions from token histories
  tx_dict = {tx["hash"]: tx for tx in transactions}

  def ensure_main_tx_db(tx_hash, etx):
      if tx_hash not in tx_dict:
          new_tx = {
              "hash": tx_hash,
              "block": etx.get("block", ""),
              "from_address": etx.get("from_address", ""),
              "to_address": etx.get("to_address", ""),
              "contract": etx.get("contract", ""),
              "tx_time": convert_block_time(etx["tx_time"]) if "tx_time" in etx else "1970-01-01 00:00:00",
              "is_error": "0",
              "amount": "0",
              "direct": etx.get("direct", "OUT"),
              "wallet": etx.get("wallet", wallet_address),
              "tx_fee": 0,
              "gas_fee": 0,
              "transaction_fee": 0
          }
          tx_dict[tx_hash] = new_tx
          transactions.append(new_tx)

  for itx in internal_txs:
      ensure_main_tx_db(itx["hash"], itx)
  for etx in erc20_txs:
      ensure_main_tx_db(etx["hash"], etx)
  for ntx in all_nfts:
      ensure_main_tx_db(ntx["hash"], ntx)

  # Merge NFTs into transactions metadata
  # NFT information will directly be added to main tx dict
  for tx in transactions:
      tx_hash = tx["hash"]
      # Find corresponding NFT if exists
      nft_match = next((n for n in all_nfts if n["hash"] == tx_hash), None)
      if nft_match:
          tx["contract"] = nft_match.get("contract")
          tx["token_id"] = nft_match.get("token_id")
      
      # For EVM, transaction_fee = gasUsed * gasPrice which is already mapped into tx["tx_fee"] inside process_transaction_transfer
      tx["transaction_fee"] = tx.get("tx_fee", 0)
      # Assuming gas_fee here means the base fee or the same as transaction_fee based on the user request context
      tx["gas_fee"] = tx.get("tx_fee", 0)

  # ===== Insert into DB =====
  # Gộp các hash từ token transfers và nft vào transactions nếu chưa có (ensure_main_tx_db đã xử lý)
  has_data = transactions or erc20_txs or merge_internal_txs or all_nfts
  if has_data:
      insert_transactions_v2(wallet_address, chain, transactions)
      
      # merge all details
      all_details = erc20_txs + erc721_txs + internal_txs
      
      # Accumulate (Plan B)
      logger.info("[EVM-SAVE][%s][Ch:%d] Before accumulation: %d details", wallet_address[:8], chain_id, len(all_details))
      all_details = accumulate_transfers(all_details)
      logger.info("[EVM-SAVE][%s][Ch:%d] After accumulation: %d details", wallet_address[:8], chain_id, len(all_details))
      
      insert_detail_token_transfer_v2(all_details)
