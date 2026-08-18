from datetime import datetime, timezone, timedelta
from decimal import Decimal, getcontext
import requests
import time
import json
import os
from eth_abi import decode
from eth_utils import to_bytes

from services.excute_transaction import insert_transactions, insert_detail_token_transfer, insert_nft_token
from services.transaction_history.tx_his import CHAIN_ID, CURRENCY_MAP, MAPPING_DATA, generate_params, process_transaction_transfer, merge_internal_tx


# from db import get_connection

getcontext().prec = 50

# Api urls of blockchains v2
API_URL = 'https://api.etherscan.io/v2/api'
API_KEY = 'W961R5X6KISNKFMJ2QWVA7999S2IQDNF6U'
MAX_RESULTS = 10000
OFFSET = 1000

ERROR_CACHE_PATH = "/app/nft_app/services/transaction_history/error_cache.json"

FUNCTION_SELECTORS = {
    "0x219f5d17": {  # increaseLiquidity((uint256,uint256,uint256,uint256,uint256,uint256))
        "name": "increaseLiquidity",
        "types": ['(uint256,uint256,uint256,uint256,uint256,uint256)'],
        "is_tuple": True,
        "token_index": 0,
    },
    "0xfc6f7865": {  # collect((uint256,address,uint128,uint128))
        "name": "collect",
        "types": ['(uint256,address,uint128,uint128)'],
        "is_tuple": True,
        "token_index": 0,
    },
    "0x18fccc76": {  # harvest(uint256,address)
        "name": "harvest",
        "types": ['uint256', 'address'],
        "is_tuple": False,
        "token_index": 0,
    },
}

def decode_tx_input(input_data: str):
    """
    Decode tx input data to extract tokenId and parameters.
    Works for increaseLiquidity, collect, and harvest functions.
    """
    if not input_data or len(input_data) < 10:
        return None

    selector = input_data[:10]
    data = input_data[10:]

    if selector not in FUNCTION_SELECTORS:
        return None

    func = FUNCTION_SELECTORS[selector]
    types = func["types"]
    is_tuple = func.get("is_tuple", False)

    try:
        # Convert hex -> bytes
        data_bytes = to_bytes(hexstr=data)

        # Decode ABI params
        decoded = decode(types, data_bytes)

        # unwrap tuple if needed
        if is_tuple:
            decoded = decoded[0]

        token_id = decoded[func["token_index"]]
        return {
            "function": func["name"],
            "token_id": int(token_id),
            "params": decoded
        }

    except Exception as e:
        print(f"Decode error: {e}")
        return None

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
      print(f"fetch data with attemp {attempt}")
      response = requests.get(url, params=params, timeout=10)
      data = response.json()
      # check if the response is successful
      if data.get("status")==1 or "Free API access" not in str(data.get("result","")):
        return data, False
      print(data.get("result", ""))
      time.sleep(delay)
      
    except requests.RequestException as e:
      print(f"Retry {attempt + 1}/{max_retries} after exception: {e}")
      time.sleep(delay)
  print(f"Fail to fetch transaction")
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
    
    print(f"{ '  '*depth}->Fetching transactions of wallet {wallet_address} on chain {chain} from block {start_block} to {end_block}, page {page}...")
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
  transactions, normal_is_error = get_transaction_with_recursive(wallet_address, chain_id, "account", "txlist", lasted_block + 1, current_block, MAPPING_DATA["transaction"])
  if not transactions or normal_is_error:
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
        "wallet": tx["wallet"]
      })

  start_block = int(transactions[0]["block"])
  end_block = int(transactions[-1]["block"])
  list_hash = [tx["hash"] for tx in transactions]

  # fetch internal transactions
  res_internal_txs, internal_is_error = get_transaction_with_recursive(wallet_address, chain_id, "account", "txlistinternal", start_block, end_block, MAPPING_DATA["internal_transaction"], list_hash=list_hash)
  if internal_is_error:
    return

  # Add symbol of internal transaction
  internal_txs.extend(res_internal_txs)
  for tx in internal_txs:
    tx["symbol"] = CURRENCY_MAP[chain]
    
  merge_internal_txs = merge_internal_tx(wallet_address, internal_txs)

  # fetch erc20 transactions
  erc20_txs, erc20_is_error = get_transaction_with_recursive(wallet_address, chain_id, "account", "tokentx", start_block, end_block, MAPPING_DATA["erc20_token"], list_hash=list_hash)
  if erc20_is_error:
    return

  # fetch nft transactions
  # ===== ERC721 (NFT) transactions =====
  erc721_txs, nft_is_error = get_transaction_with_recursive(
      wallet_address, chain_id, "account", "tokennfttx",
      start_block, end_block, MAPPING_DATA['erc721_token'],
      list_hash=list_hash
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
              print(f"[+] Added new NFT from decode: token_id={token_id}, func={decoded['function']}")

  # Gộp NFT decode mới với NFT từ Etherscan
  all_nfts = unique_nfts + decoded_nfts

  # ===== Insert into DB =====
  if transactions:
      insert_transactions(wallet_address, chain, transactions)
      insert_detail_token_transfer(merge_internal_txs)
      insert_detail_token_transfer(erc20_txs)
      insert_nft_token(all_nfts)
